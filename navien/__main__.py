"""python -m navien — the Navien cascade monitor service.

Phase 0: headless only. Prints one line per bus cycle. The web server arrives in Phase 1.
Receive-only: nothing in this package ever writes to the bus.
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import time

from . import config as config_mod
from .rawlog import RawLogger
from .source import Reader, ReplaySource, SerialSource
from .state import StateModel

log = logging.getLogger("navien")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="python -m navien", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="TOML config file (default: built-in defaults)")
    ap.add_argument("--port", help="serial port (overrides serial.port)")
    ap.add_argument("--replay", metavar="FILE", help="replay a .raw/.raw.gz log instead of the serial port")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed, 0 = as fast as possible")
    ap.add_argument("--loop", action="store_true", help="loop the replay")
    ap.add_argument("--headless", action="store_true", help="no web server; print cycles (Phase 0 default)")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def fmt_unit(tag: str, u: dict | None) -> str:
    if not u:
        return f"{tag}: --"
    flame = "FLAME" if u.get("flame") else "     "
    s = (f"{tag}: {u.get('stage', '?'):<17} {flame} out {u.get('outlet_c', 0):4.1f} "
         f"in {u.get('inlet_c', 0):4.1f} set {u.get('set_c', 0):4.1f} "
         f"flow {u.get('flow_lpm', 0):4.1f} cap {u.get('capacity_pct', 0):4.1f}% "
         f"gas {u.get('gas_kcalh', 0):5d}")
    if u.get("error_code"):
        s += f" ERR {u['error_code']}/{u.get('error_level')}"
    return s


def fmt_cycle(snap: dict) -> str:
    units = snap["units"]
    c = snap["cascade"] or {}
    t = time.strftime("%H:%M:%S", time.localtime(snap["ts"]))
    x = f"x {c.get('x26', '-')}/{c.get('x27', '-')}/{c.get('x28', '-')}"
    bus = snap["bus"]
    return (f"{t} #{snap['seq']:<5} {fmt_unit('M', units.get('0'))} | "
            f"{fmt_unit('S', units.get('1'))} | {x} | cs_fail {bus['checksum_fail']}")


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load(args.config)
    if args.port:
        cfg.serial.port = args.port

    # Raw logging is always on for the live bus; replayed bytes are never re-logged.
    rawlog = None
    if args.replay:
        source = ReplaySource(args.replay, speed=args.speed, loop=args.loop)
    else:
        source = SerialSource(cfg.serial.port, cfg.serial.baud)
        rawlog = RawLogger(cfg.rawlog)
        rawlog.start()

    state = StateModel(cfg.state.history_seconds, cfg.state.frame_buffer)
    frames_q: queue.Queue = queue.Queue()
    done = object()
    reader = Reader(source, on_frames=frames_q.put,
                    on_bytes=rawlog.put if rawlog else None,
                    on_finished=lambda: frames_q.put(done))

    def shutdown(*_):
        frames_q.put(done)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("source: %s", args.replay or cfg.serial.port)
    reader.start()
    last_frame = time.time()
    warned = False
    try:
        while True:
            try:
                item = frames_q.get(timeout=1.0)
            except queue.Empty:
                snap = state.tick(time.time()) if not args.replay else None
                if snap:
                    print(fmt_cycle(snap), flush=True)
                if not warned and time.time() - last_frame > cfg.state.bus_timeout_s:
                    log.warning("no frames for %.0f s (reader connected=%s, last error=%s)",
                                time.time() - last_frame, reader.connected, reader.last_error)
                    warned = True
                continue
            if item is done:
                break
            for frame in item:
                last_frame, warned = time.time(), False
                snap = state.ingest(frame)
                if snap:
                    print(fmt_cycle(snap), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        if rawlog:
            rawlog.stop()
    b = state.bus_stats()
    log.info("frames %d, checksum failures %d, unexpected seeds %d, resyncs %d%s",
             b["frames"], b["checksum_fail"], b["seed_unexpected"], reader.parser.resyncs,
             f", raw log dropped chunks {rawlog.dropped_chunks}" if rawlog else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
