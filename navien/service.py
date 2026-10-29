"""Wires source -> reader thread -> event loop -> state model -> hub. PLAN.md §1.1.

The reader and raw-log threads never touch the state model. The reader hands frames to
the event loop with call_soon_threadsafe; from then on everything (state, hub, HTTP
handlers) runs on the loop thread, so no locks are needed. Keep HTTP handlers `async def`
for that reason: a plain `def` handler runs in a worker thread and would race the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .backfill import prime
from .config import Config
from .hub import Hub
from .influx import InfluxWriter
from .rawlog import RawLogger
from .source import Reader, ReplaySource, SerialSource
from .state import StateModel

log = logging.getLogger(__name__)

TICK_S = 0.25


class Service:
    def __init__(self, cfg: Config, replay: str | None = None, speed: float = 1.0,
                 loop_replay: bool = False) -> None:
        self.cfg = cfg
        self.state = StateModel(cfg.state.history_seconds, cfg.state.frame_buffer)
        self.hub = Hub()
        self.started_at = time.time()
        self.last_frame_wall: float | None = None

        # Raw logging is always on for the live bus; replayed bytes are never re-logged.
        if replay:
            source = ReplaySource(replay, speed=speed, loop=loop_replay)
            self.rawlog = None
            self.source_desc = f"replay:{replay}"
        else:
            source = SerialSource(cfg.serial.port, cfg.serial.baud)
            self.rawlog = RawLogger(cfg.rawlog)
            self.source_desc = f"serial:{cfg.serial.port}"
        # With replay at speed 0 frame times are historical, so wall-clock ticks would
        # close every cycle early; only tick when frame time tracks the wall clock.
        self.wall_clock_frames = not replay or speed > 0
        # Influx gets the live bus only: a dev replay must not write historical cycles into
        # the production database (loading raw logs is the job of `backfill`, Phase 4b).
        self.influx = None
        if cfg.influx.enabled and replay:
            log.info("influx: disabled for --replay")
        elif cfg.influx.enabled:
            self.influx = InfluxWriter(cfg.influx, stale_s=cfg.state.bus_timeout_s,
                                       bus_extra=self._bus_extra, events=cfg.events)
        self._influx_task: asyncio.Task | None = None
        self.reader = Reader(source, on_frames=self._from_reader,
                             on_bytes=self.rawlog.put if self.rawlog else None,
                             on_finished=self._reader_finished)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.finished: asyncio.Event | None = None
        self._ticker: asyncio.Task | None = None

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.finished = asyncio.Event()
        if self.influx and self.rawlog:
            # before the raw logger reopens this hour's file and before any live cycle
            try:
                lines = await asyncio.to_thread(prime, self.influx.recorder,
                                                Path(self.cfg.rawlog.dir), time.time())
                self.influx.add(lines)
                log.info("events: primed from this hour's raw log (%d points)", len(lines))
            except Exception:
                log.exception("events: could not prime from the raw log")
        if self.rawlog:
            self.rawlog.start()
        self.reader.start()
        self._ticker = asyncio.create_task(self._tick_loop())
        if self.influx:
            sub = self.hub.subscribe({"cycle"}, maxsize=600, name="influx")
            self._influx_task = asyncio.create_task(self.influx.run(sub))
            log.info("influx: writing to %s db %s", self.cfg.influx.url, self.cfg.influx.database)
        log.info("service started, source %s", self.source_desc)

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        await asyncio.to_thread(self.reader.stop)
        if self.rawlog:
            await asyncio.to_thread(self.rawlog.stop)
        if self._influx_task:
            self._influx_task.cancel()
            await asyncio.gather(self._influx_task, return_exceptions=True)
            await self.influx.close()
        b = self.state.bus_stats()
        log.info("service stopped: frames %d, checksum failures %d, resyncs %d%s",
                 b["frames"], b["checksum_fail"], self.reader.parser.resyncs,
                 f", raw log dropped chunks {self.rawlog.dropped_chunks}" if self.rawlog else "")

    # ------------------------------------------------------ reader -> event loop

    def _from_reader(self, frames) -> None:          # reader thread
        try:
            self.loop.call_soon_threadsafe(self._on_frames, frames)
        except RuntimeError:                         # loop already closed at shutdown
            pass

    def _reader_finished(self) -> None:              # reader thread
        try:
            self.loop.call_soon_threadsafe(self.finished.set)
        except RuntimeError:
            pass

    def _on_frames(self, frames) -> None:            # event loop
        self.last_frame_wall = time.time()
        for frame in frames:
            snap = self.state.ingest(frame)
            if snap:
                self.hub.publish("cycle", snap)
            self.hub.publish("frame", frame)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            if self.wall_clock_frames:
                snap = self.state.tick(time.time())
                if snap:
                    self.hub.publish("cycle", snap)

    # ---------------------------------------------------------------- queries

    def _bus_extra(self) -> dict:
        """Service-level counters for the navien_bus measurement."""
        return {
            "resyncs": self.reader.parser.resyncs,
            "reader_bytes": self.reader.bytes_total,
            "rawlog_dropped_chunks": self.rawlog.dropped_chunks if self.rawlog else 0,
            "ws_clients": sum(1 for s in self.hub.subscribers if s.name.startswith("ws")),
        }

    def latest(self) -> dict:
        """The last completed cycle (or the partial state before the first one closes)."""
        return self.state.history[-1] if self.state.history else self.state.snapshot()

    def health(self) -> dict:
        now = time.time()
        age = None if self.last_frame_wall is None else round(now - self.last_frame_wall, 3)
        bus_alive = age is not None and age < self.cfg.state.bus_timeout_s
        rawlog = None
        if self.rawlog:
            rawlog = {
                "file": str(self.rawlog.current_path) if self.rawlog.current_path else None,
                "bytes_written": self.rawlog.bytes_written,
                "dropped_chunks": self.rawlog.dropped_chunks,
                "queued": self.rawlog.q.qsize(),
            }
        return {
            "status": "ok" if bus_alive else "no_data",
            "bus_alive": bus_alive,
            "last_frame_age_s": age,
            "source": self.source_desc,
            "reader": {
                "connected": self.reader.connected,
                "finished": bool(self.finished and self.finished.is_set()),
                "last_error": self.reader.last_error,
                "bytes": self.reader.bytes_total,
                "resyncs": self.reader.parser.resyncs,
            },
            "bus": self.state.bus_stats(),
            "history_cycles": len(self.state.history),
            "rawlog": rawlog,
            "influx": self.influx.health() if self.influx else None,
            "hub": {"published": self.hub.published, "subscribers": self.hub.stats()},
            "uptime_s": round(now - self.started_at, 1),
        }
