"""Always-on raw byte log with rotation, compression and retention. PLAN.md §1.5.

Files, per period (UTC, so DST never repeats an hour):
  navien_20261028T12Z.raw   every received byte, unmodified (same format as navien-capture.py)
  navien_20261028T12Z.idx   text, one line per burst of bytes: "<byte offset>\t<epoch ts>"
Closed periods are gzipped to .raw.gz / .idx.gz. ReplaySource reads all four forms.

The reader thread calls put(), which never blocks: if the disk stalls and the queue
fills, chunks are dropped and counted rather than delaying reception.
"""

from __future__ import annotations

import gzip
import logging
import os
import queue
import shutil
import threading
import time
from pathlib import Path

from .config import RawlogCfg

log = logging.getLogger(__name__)

PREFIX = "navien_"
KEY_FORMATS = {"hourly": "%Y%m%dT%HZ", "daily": "%Y%m%dZ"}

# A chunk arriving within this long of the previous one is part of the same burst
# (pyserial hands a frame over in several small reads) and gets no .idx line of its own.
# Frames are >= ~20 ms apart, so every frame start still gets an exact timestamp.
IDX_GAP_S = 0.005


def period_key(ts: float, rotate: str) -> str:
    return time.strftime(KEY_FORMATS[rotate], time.gmtime(ts))


def _group(path: Path) -> str:
    """navien_20261028T12Z.raw.gz -> navien_20261028T12Z"""
    return path.name.split(".", 1)[0]


class RawLogger(threading.Thread):
    def __init__(self, cfg: RawlogCfg, queue_size: int = 4096) -> None:
        super().__init__(name="rawlog", daemon=True)
        self.cfg = cfg
        self.dir = Path(cfg.dir)
        self.q: queue.Queue[tuple[float, bytes]] = queue.Queue(maxsize=queue_size)
        self._stop_evt = threading.Event()

        self.dropped_chunks = 0
        self.bytes_written = 0
        self.current_path: Path | None = None

        self._key: str | None = None
        self._raw_f = None
        self._idx_f = None
        self._offset = 0
        self._prev_ts: float | None = None
        self._last_flush = 0.0

    # --------------------------------------------------------------- producer

    def put(self, ts: float, data: bytes) -> bool:
        try:
            self.q.put_nowait((ts, data))
            return True
        except queue.Full:
            self.dropped_chunks += 1
            return False

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        self.join(timeout)

    # --------------------------------------------------------------- consumer

    def run(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            while not self._stop_evt.is_set():
                try:
                    ts, data = self.q.get(timeout=0.5)
                except queue.Empty:
                    self._maybe_flush()
                    continue
                self.write(ts, data)
            while True:                          # drain on shutdown
                try:
                    self.write(*self.q.get_nowait())
                except queue.Empty:
                    break
        finally:
            self.close()

    def write(self, ts: float, data: bytes) -> None:
        key = period_key(ts, self.cfg.rotate)
        if key != self._key:
            self._rotate(key)
            self._prev_ts = None
        if self._prev_ts is None or ts - self._prev_ts >= IDX_GAP_S:
            self._idx_f.write(f"{self._offset}\t{ts:.4f}\n")
        self._prev_ts = ts
        self._raw_f.write(data)
        self._offset += len(data)
        self.bytes_written += len(data)
        self._maybe_flush()

    def close(self) -> None:
        if self._raw_f is not None:
            self._close_files()
            self._key = None

    # ---------------------------------------------------------------- helpers

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if self._raw_f is not None and now - self._last_flush >= self.cfg.flush_seconds:
            self._raw_f.flush()
            self._idx_f.flush()
            self._last_flush = now

    def _close_files(self) -> None:
        for f in (self._raw_f, self._idx_f):
            f.flush()
            os.fsync(f.fileno())
            f.close()
        self._raw_f = self._idx_f = None

    def _stem_for(self, key: str) -> str:
        """A stem whose files are not already compressed (clock stepped backwards)."""
        stem, n = PREFIX + key, 1
        while (self.dir / f"{stem}.raw.gz").exists():
            n += 1
            stem = f"{PREFIX}{key}-{n}"
        return stem

    def _rotate(self, key: str) -> None:
        self.close()
        self._key = key
        stem = self._stem_for(key)
        raw_path = self.dir / f"{stem}.raw"
        self._raw_f = open(raw_path, "ab")
        self._idx_f = open(self.dir / f"{stem}.idx", "a")
        self._offset = self._raw_f.tell()
        self._last_flush = time.monotonic()
        self.current_path = raw_path
        log.info("raw log -> %s", raw_path)
        if self.cfg.compress:
            self._compress_closed(stem)
        self._enforce_retention(stem)

    def _compress_closed(self, current_stem: str) -> None:
        for path in sorted(self.dir.glob(f"{PREFIX}*")):
            if path.suffix in (".raw", ".idx") and _group(path) != current_stem:
                gz = path.with_name(path.name + ".gz")
                with open(path, "rb") as src, gzip.open(gz, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                path.unlink()

    def _enforce_retention(self, current_stem: str) -> None:
        groups: dict[str, list[Path]] = {}
        for path in self.dir.glob(f"{PREFIX}*"):
            groups.setdefault(_group(path), []).append(path)
        groups.pop(current_stem, None)

        def mtime(paths):
            return max(p.stat().st_mtime for p in paths)

        ordered = sorted(groups.items(), key=lambda kv: mtime(kv[1]))
        cutoff = time.time() - self.cfg.keep_days * 86400
        limit = self.cfg.max_total_mb * 1024 * 1024
        total = sum(p.stat().st_size for _, paths in ordered for p in paths)
        if self.current_path is not None and self.current_path.exists():
            total += self.current_path.stat().st_size
        for stem, paths in ordered:
            if mtime(paths) >= cutoff and total <= limit:
                break
            for p in paths:
                total -= p.stat().st_size
                p.unlink()
            log.info("raw log retention: deleted %s", stem)
