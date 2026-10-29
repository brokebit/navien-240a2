"""WebSocket push protocol. PLAN.md §1.6.

Server -> client (JSON text messages):
  {"type": "hello", ...}                    once, on connect
  {"type": "history", "cycles": [...]}      on connect, and on request
  {"type": "cycle", ...}                    every bus cycle (same shape as GET /api/state)
  {"type": "frame", ...}                    every raw frame, only after subscribing to "frames"
  {"type": "gap", "dropped": n}             messages were dropped since the last one
  {"type": "error", "message": "..."}       a client message could not be understood

Client -> server:
  {"subscribe": ["frames"]} / {"unsubscribe": ["frames"]}
  {"get": "history", "seconds": 60}         seconds optional, capped at history_seconds

Each connection has one Hub subscription (bounded, drop-oldest) and two tasks: a writer that
is the only code sending on the socket, and a reader for client messages. A client that
accepts nothing for send_timeout seconds is dropped; it is expected to reconnect and re-sync
from the history message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from .service import Service
from .version import VERSION

log = logging.getLogger(__name__)

API_VERSION = "1"
CLIENT_TOPICS = {"frames": "frame"}         # client-facing name -> hub topic


class WsSession:
    def __init__(self, service: Service, send: Callable[[str], Awaitable[None]],
                 receive: Callable[[], Awaitable[str]], name: str = "ws") -> None:
        self.service = service
        self.send = send
        self.receive = receive
        http = service.cfg.http
        self.send_timeout = http.ws_send_timeout_s
        self.sub = service.hub.subscribe({"cycle", "control"}, maxsize=http.ws_queue, name=name)

    # ---------------------------------------------------------------- running

    async def run(self) -> None:
        writer = asyncio.create_task(self._write_loop())
        reader = asyncio.create_task(self._read_loop())
        try:
            done, _ = await asyncio.wait({writer, reader}, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    log.debug("%s closed: %s: %s", self.sub.name, type(exc).__name__, exc)
        finally:
            for t in (writer, reader):
                t.cancel()
            await asyncio.gather(writer, reader, return_exceptions=True)
            self.sub.close()

    async def _write_loop(self) -> None:
        await self._send(self._hello())
        await self._send(self._history(None))
        while True:
            topic, msg = await self.sub.get()
            if self.sub.gap:
                self.sub.gap = False
                await self._send({"type": "gap", "dropped": self.sub.dropped})
            await self._send(self._encode(topic, msg))

    async def _read_loop(self) -> None:
        while True:
            text = await self.receive()
            reply = self.handle(text)
            if reply is not None:
                self.sub.offer("control", reply)

    async def _send(self, msg: dict) -> None:
        # TimeoutError ends the session: the client stopped accepting data.
        await asyncio.wait_for(self.send(json.dumps(msg, separators=(",", ":"))), self.send_timeout)

    # ---------------------------------------------------------------- messages

    def handle(self, text: str) -> dict | None:
        """Apply one client message; returns a reply to queue, if any."""
        try:
            msg = json.loads(text)
            if not isinstance(msg, dict):
                raise ValueError("expected a JSON object")
            if "subscribe" in msg or "unsubscribe" in msg:
                for name in msg.get("subscribe", []):
                    self.sub.topics.add(self._topic(name))
                for name in msg.get("unsubscribe", []):
                    self.sub.topics.discard(self._topic(name))
                return None
            if msg.get("get") == "history":
                seconds = msg.get("seconds")
                if seconds is not None and not isinstance(seconds, (int, float)):
                    raise ValueError("seconds must be a number")
                return self._history(seconds)
            raise ValueError(f"unknown message: {text[:100]}")
        except (ValueError, TypeError) as e:
            return {"type": "error", "message": str(e)}

    @staticmethod
    def _topic(name: str) -> str:
        if name not in CLIENT_TOPICS:
            raise ValueError(f"unknown topic {name!r}; known: {sorted(CLIENT_TOPICS)}")
        return CLIENT_TOPICS[name]

    def _hello(self) -> dict:
        cfg = self.service.cfg
        return {"type": "hello", "version": VERSION, "api_version": API_VERSION,
                "source": self.service.source_desc,
                "history_seconds": cfg.state.history_seconds,
                "topics": sorted(CLIENT_TOPICS)}

    def _history(self, seconds: float | None) -> dict:
        limit = self.service.cfg.state.history_seconds
        seconds = limit if seconds is None else max(0.0, min(float(seconds), limit))
        return {"type": "history", "seconds": seconds,
                "cycles": self.service.state.history_since(seconds)}

    @staticmethod
    def _encode(topic: str, msg) -> dict:
        if topic == "frame":
            return {"type": "frame", **msg.to_dict()}
        return msg                               # cycle snapshots and control replies
