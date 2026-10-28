"""Byte sources and the reader thread. PLAN.md §1.1, §1.3.

The Reader thread is the only code that touches the serial port. Per read it does three
non-blocking things: hand the raw chunk to the raw logger, parse, and hand complete
frames to a callback. It never transmits.
"""

from __future__ import annotations

import gzip
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from .protocol import Frame, FrameParser

log = logging.getLogger(__name__)

# Nominal offset of each stream within the 1 s cycle (PROTOCOL_NOTES §3), used to give
# frames plausible timestamps when replaying a .raw file that has no .idx sidecar.
NOMINAL_OFFSET_S = {
    "50>51/10": 0.000, "51>50/90": 0.064, "51>0F/90": 0.112,
    "50>52/10": 0.170,   # not timed yet: only known to follow the slave's gas frame
    "50>50/90": 0.400, "50>0F/90": 0.592, "50>50/10": 0.800,
}
CYCLE_START_STREAM = "50>51/10"


class SerialSource:
    """Blocking pyserial reads with a short timeout."""

    def __init__(self, port: str, baud: int = 19200) -> None:
        self.port = port
        self.baud = baud
        self.ser = None
        self.finished = False             # a serial port never runs out

    def open(self) -> None:
        import serial
        self.ser = serial.Serial(self.port, self.baud, bytesize=8, parity="N",
                                 stopbits=1, timeout=0.05)
        self.ser.reset_input_buffer()

    def read(self) -> tuple[float, bytes]:
        data = self.ser.read(self.ser.in_waiting or 1)
        return time.time(), data

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None


def _open_maybe_gz(path: Path, mode: str):
    return gzip.open(path, mode) if path.suffix == ".gz" else open(path, mode)


def _sidecar(raw_path: Path) -> Path | None:
    stem = raw_path.name.split(".", 1)[0]
    for name in (f"{stem}.idx", f"{stem}.idx.gz"):
        p = raw_path.with_name(name)
        if p.exists():
            return p
    return None


def load_chunks(raw_path: str | Path) -> list[tuple[float, bytes]]:
    """Split a raw log into (original_ts, bytes) chunks.

    With an .idx sidecar the original read chunks and times are reproduced exactly.
    Without one (navien-capture.py output) every frame becomes a chunk, timed from the
    nominal schedule: each master 0x41 frame starts a new 1 s cycle, and the last chunk
    lands on the file's modification time.
    """
    raw_path = Path(raw_path)
    with _open_maybe_gz(raw_path, "rb") as f:
        data = f.read()
    idx = _sidecar(raw_path)
    if idx is not None:
        with _open_maybe_gz(idx, "rt") as f:
            marks = [(int(o), float(t)) for o, t in (line.split("\t") for line in f if line.strip())]
        chunks = []
        for i, (off, ts) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(data)
            chunks.append((ts, data[off:end]))
        return chunks

    parser = FrameParser()
    chunks, cycle_t, last_t = [], 0.0, -1.0
    pos = 0
    for frame in parser.feed(data, 0.0):
        start = data.index(frame.raw, pos)
        if frame.stream == CYCLE_START_STREAM:
            cycle_t = max(cycle_t + 1.0, last_t + 0.05) if last_t >= 0 else 0.0
            t = cycle_t
        else:
            t = max(cycle_t + NOMINAL_OFFSET_S.get(frame.stream, 0.8), last_t + 0.05)
        if start > pos:                       # bytes between frames go with this frame
            chunks.append((t, data[pos:start]))
        chunks.append((t, frame.raw))
        pos, last_t = start + len(frame.raw), t
    if pos < len(data):
        chunks.append((last_t + 0.05, data[pos:]))
    if chunks:
        base = raw_path.stat().st_mtime - chunks[-1][0]
        chunks = [(t + base, d) for t, d in chunks]
    return chunks


class ReplaySource:
    """Feeds a raw log back at real-time pace (speed=1), faster, or instantly (speed=0).

    Timestamps are shifted so the first chunk is 'now' (speed=0 keeps the original ones).
    """

    def __init__(self, path: str | Path, speed: float = 1.0, loop: bool = False) -> None:
        self.path = Path(path)
        self.speed = speed
        self.loop = loop
        self.finished = False
        self._chunks: list[tuple[float, bytes]] = []
        self._i = 0
        self._t0_orig = 0.0
        self._t0_wall = 0.0
        self._offset = 0.0                    # added per loop so time keeps moving forward

    def open(self) -> None:
        self._chunks = load_chunks(self.path)
        if not self._chunks:
            self.finished = True
            return
        self._t0_orig = self._chunks[0][0]
        self._t0_wall = time.time()
        self._i = 0

    def read(self) -> tuple[float, bytes]:
        if self._i >= len(self._chunks):
            if not self.loop:
                self.finished = True
                return time.time(), b""
            span = self._chunks[-1][0] - self._t0_orig + 1.0
            self._offset += span
            self._i = 0
        t_orig, data = self._chunks[self._i]
        self._i += 1
        rel = t_orig - self._t0_orig + self._offset
        if self.speed <= 0:
            return t_orig + self._offset, data
        target = self._t0_wall + rel / self.speed
        delay = target - time.time()
        if delay > 0:
            time.sleep(delay)
        return target, data

    def close(self) -> None:
        pass


class Reader(threading.Thread):
    """Owns the source. Reconnects with backoff if a serial adapter disappears."""

    def __init__(self, source, on_frames: Callable[[list[Frame]], None],
                 on_bytes: Callable[[float, bytes], object] | None = None,
                 on_finished: Callable[[], None] | None = None) -> None:
        super().__init__(name="reader", daemon=True)
        self.source = source
        self.on_frames = on_frames
        self.on_bytes = on_bytes
        self.on_finished = on_finished
        self.parser = FrameParser()
        self._stop_evt = threading.Event()
        self.connected = False
        self.last_error: str | None = None
        self.bytes_total = 0

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_evt.set()
        self.join(timeout)

    def run(self) -> None:
        backoff = 1.0
        try:
            while not self._stop_evt.is_set():
                try:
                    self.source.open()
                    self.connected = True
                    self.last_error = None
                    backoff = 1.0
                    self._pump()
                    if self.source.finished:
                        break
                except Exception as e:           # serial.SerialException, OSError, ...
                    self.connected = False
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.warning("source error (%s); retrying in %.0f s", self.last_error, backoff)
                    self._stop_evt.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                finally:
                    self.connected = False
                    self.source.close()
        finally:
            if self.on_finished is not None:
                self.on_finished()

    def _pump(self) -> None:
        while not self._stop_evt.is_set() and not self.source.finished:
            ts, data = self.source.read()
            if not data:
                continue
            self.bytes_total += len(data)
            if self.on_bytes is not None:
                self.on_bytes(ts, data)
            frames = self.parser.feed(data, ts)
            if frames:
                self.on_frames(frames)
