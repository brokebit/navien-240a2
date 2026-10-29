"""navien-dash backfill — load raw logs into InfluxDB. PLAN.md §1.10.

Runs the raw logs through the same FrameParser -> StateModel -> InfluxWriter (with its
events.Recorder) as the live service, at full speed with the original timestamps. Influx
overwrites a point with the same series and timestamp, so re-running is harmless and a
decoder or event-logic fix can be applied to history by running it again. Needs no
serial port; it can run beside the service. navien_bus is not written: it describes the
live process, not the bus history.

Files are processed in name order (= time order) as one stream, so events and hours that
cross file boundaries come out whole. A gap in the logs (service stopped) ends open events
as truncated, exactly as the live recorder does at shutdown.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from .influx import BATCH_LINES, InfluxWriter
from .protocol import FrameParser
from .source import load_chunks
from .state import StateModel

log = logging.getLogger("navien.backfill")

CONFIG_CANDIDATES = (Path("navien.toml"), Path("/etc/navien-dash/navien.toml"))
NAME_RE = re.compile(r"navien_(\d{8})(?:T(\d{2})Z)?\.raw(\.gz)?$")
MAX_FAILURES = 8


def period(path: Path) -> tuple[float, float] | None:
    """UTC [start, end) epoch seconds a rawlog file covers, from its name."""
    m = NAME_RE.search(path.name)
    if not m:
        return None
    day = datetime.strptime(m[1], "%Y%m%d").replace(tzinfo=timezone.utc)
    if m[2] is None:
        return day.timestamp(), (day + timedelta(days=1)).timestamp()
    start = day + timedelta(hours=int(m[2]))
    return start.timestamp(), (start + timedelta(hours=1)).timestamp()


def find_logs(paths: list[Path], t_from: float, t_to: float) -> list[Path]:
    """Raw logs under `paths` (files or directories) overlapping [t_from, t_to)."""
    found = []
    for p in paths:
        candidates = sorted(p.iterdir()) if p.is_dir() else [p]
        for f in candidates:
            if not f.name.endswith((".raw", ".raw.gz")):
                continue
            span = period(f)
            if span is None or (span[1] > t_from and span[0] < t_to):
                found.append(f)
    return sorted(set(found), key=lambda f: f.name)


def parse_time(s: str, end: bool = False) -> float:
    """YYYY-MM-DD (local midnight; with end=True the following one) or an ISO datetime."""
    try:
        d = date.fromisoformat(s)
        dt = datetime(d.year, d.month, d.day) + (timedelta(days=1) if end else timedelta())
    except ValueError:
        dt = datetime.fromisoformat(s)
    return dt.timestamp()


def cycles(files: list[Path], t_from: float, t_to: float):
    """Cycle snapshots from the raw logs, in order."""
    parser, state = FrameParser(), StateModel(history_seconds=0, frame_buffer=1)
    for f in files:
        for ts, data in load_chunks(f):
            if ts < t_from:
                continue
            if ts >= t_to:
                return
            for frame in parser.feed(data, ts):
                snap = state.ingest(frame)
                if snap:
                    yield snap


def prime(recorder, raw_dir: Path, now: float) -> list[tuple[str, str]]:
    """Service start: replay the current hour's raw log into `recorder`, so a restart
    mid-hour keeps the hour's totals whole (and a restart shorter than events.GAP_S does
    not even split an event). Returns its output: events the previous process already
    wrote (rewritten identically) and anything it left open."""
    hour_start = now - now % 3600
    if not raw_dir.is_dir():
        return []
    out = []
    for snap in cycles(find_logs([raw_dir], hour_start, now), hour_start, now):
        out += recorder.feed(snap)
    return out


async def backfill(cfg: config_mod.Config, files: list[Path], t_from: float, t_to: float,
                   dry_run: bool = False, transport=None, out=print) -> dict:
    icfg = copy.copy(cfg.influx)
    icfg.max_queue = sys.maxsize                    # never drop: we wait for Influx instead
    writer = InfluxWriter(icfg, stale_s=cfg.state.bus_timeout_s, events=cfg.events,
                          bus=False, transport=transport)
    by_measurement: Counter[str] = Counter()
    n_cycles = 0

    async def drain(final: bool = False) -> None:
        if dry_run:
            for q in writer.queues.values():
                by_measurement.update(ln.split(",", 1)[0].split(" ", 1)[0] for ln in q)
                q.clear()
            return
        if not final and writer.queued < BATCH_LINES:
            return
        failures = 0
        while not await writer.flush():
            failures += 1
            if failures >= MAX_FAILURES:
                raise RuntimeError(f"influx unreachable: {writer.last_error}")
            out(f"  influx: {writer.last_error}; retrying in {writer.backoff_s:.0f} s")
            await asyncio.sleep(writer.backoff_s)

    t0 = time.monotonic()
    try:
        for snap in cycles(files, t_from, t_to):
            writer.ingest(snap)
            n_cycles += 1
            await drain()
        writer.add(writer.recorder.finish())
        await drain(final=True)
    finally:
        await writer.client.aclose()
    return {
        "cycles": n_cycles,
        "events": dict(writer.recorder.counts),
        "written": writer.written,
        "rejected": writer.rejected,
        "lines": dict(by_measurement),
        "seconds": round(time.monotonic() - t0, 1),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="navien-dash backfill", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path,
                    help="raw log files or directories (default: rawlog.dir)")
    ap.add_argument("--config", help="TOML config (default: ./navien.toml, else "
                                     "/etc/navien-dash/navien.toml)")
    ap.add_argument("--from", dest="t_from", metavar="WHEN",
                    help="start, local time: YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    ap.add_argument("--to", dest="t_to", metavar="WHEN",
                    help="end (exclusive; a date means the end of that day)")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect events and count points, write nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config_path = args.config or next((p for p in CONFIG_CANDIDATES if p.exists()), None)
    cfg = config_mod.load(config_path)
    t_from = parse_time(args.t_from) if args.t_from else 0.0
    t_to = parse_time(args.t_to, end=True) if args.t_to else float("inf")
    files = find_logs(args.paths or [Path(cfg.rawlog.dir)], t_from, t_to)
    if not files:
        print("no raw logs found", file=sys.stderr)
        return 1
    target = "dry run" if args.dry_run else f"{cfg.influx.url} db {cfg.influx.database}"
    print(f"config {config_path or 'built-in defaults'}; {len(files)} files "
          f"({files[0].name} .. {files[-1].name}) -> {target}")
    try:
        r = asyncio.run(backfill(cfg, files, t_from, t_to, dry_run=args.dry_run))
    except RuntimeError as e:
        print(f"backfill stopped: {e}", file=sys.stderr)
        return 2
    events = ", ".join(f"{k} {v}" for k, v in sorted(r["events"].items())) or "none"
    print(f"{r['cycles']} cycles in {r['seconds']} s; events: {events}")
    if args.dry_run:
        print("points: " + ", ".join(f"{k} {v}" for k, v in sorted(r["lines"].items())))
    else:
        print(f"points written {r['written']}, rejected {r['rejected']}")
    return 0 if not r["rejected"] else 3
