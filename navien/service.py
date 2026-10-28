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

from .config import Config
from .hub import Hub
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
        if self.rawlog:
            self.rawlog.start()
        self.reader.start()
        self._ticker = asyncio.create_task(self._tick_loop())
        log.info("service started, source %s", self.source_desc)

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        await asyncio.to_thread(self.reader.stop)
        if self.rawlog:
            await asyncio.to_thread(self.rawlog.stop)
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
            "hub": {"published": self.hub.published, "subscribers": self.hub.stats()},
            "uptime_s": round(now - self.started_at, 1),
        }
