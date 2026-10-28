"""python -m navien — the Navien cascade monitor service.

Default: web server (REST API now, dashboard from Phase 3a). --headless: no web server,
print one line per bus cycle. Receive-only: nothing in this package writes to the bus.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

from . import config as config_mod
from .service import Service

log = logging.getLogger("navien")

DEFAULT_CONFIG = Path("navien.toml")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="python -m navien", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help=f"TOML config file (default: ./{DEFAULT_CONFIG} if present)")
    ap.add_argument("--port", help="serial port (overrides serial.port)")
    ap.add_argument("--http-port", type=int, help="HTTP port (overrides http.port)")
    ap.add_argument("--replay", metavar="FILE", help="replay a .raw/.raw.gz log instead of the serial port")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed, 0 = as fast as possible")
    ap.add_argument("--loop", action="store_true", help="loop the replay")
    ap.add_argument("--headless", action="store_true", help="no web server; print cycles")
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
    return (f"{t} #{snap['seq']:<5} {fmt_unit('M', units.get('0'))} | "
            f"{fmt_unit('S', units.get('1'))} | {x} | cs_fail {snap['bus']['checksum_fail']}")


async def run_headless(service: Service) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    sub = service.hub.subscribe({"cycle"}, maxsize=1000, name="headless")
    await service.start()
    getter = None
    try:
        waits = {asyncio.ensure_future(stop.wait()), asyncio.ensure_future(service.finished.wait())}
        while True:
            getter = asyncio.ensure_future(sub.get())
            done, _ = await asyncio.wait(waits | {getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                print(fmt_cycle(getter.result()[1]), flush=True)
                continue
            # stopping: print whatever is still queued, then leave
            getter.cancel()
            while not sub.queue.empty():
                print(fmt_cycle(sub.queue.get_nowait()[1]), flush=True)
            break
        for w in waits:
            w.cancel()
    finally:
        sub.close()
        await service.stop()


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)
    cfg = config_mod.load(config_path)
    if args.port:
        cfg.serial.port = args.port
    if args.http_port:
        cfg.http.port = args.http_port
    log.info("config: %s", config_path or "built-in defaults")

    service = Service(cfg, replay=args.replay, speed=args.speed, loop_replay=args.loop)
    if args.headless:
        asyncio.run(run_headless(service))
        return 0

    import uvicorn
    from .api import create_app
    # One worker only: exactly one process may own the serial port (CLAUDE.md).
    uvicorn.run(create_app(service), host=cfg.http.host, port=cfg.http.port, workers=1,
                log_level="debug" if args.verbose else "info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
