"""FastAPI app: REST endpoints over the Service. PLAN.md §1.6.

All handlers are `async def` so they run on the event loop thread, the only thread that
touches the state model (see service.py).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Query, WebSocket
from fastapi.staticfiles import StaticFiles

from .decode import fields_table
from .service import Service
from .ws import API_VERSION, WsSession

WEB_DIR = Path(__file__).parent / "web"


class DashboardFiles(StaticFiles):
    """Static dashboard; `no-cache` makes browsers revalidate (ETag) so an upgrade shows at once."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(service: Service) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="Navien cascade monitor",
        version=API_VERSION,
        description="Passive, receive-only monitor of two Navien NPE-240A2 units on RS-485. "
                    "All values are metric (°C, L/min, kcal/h); unit conversion is the client's job. "
                    "Field meanings and confidence: GET /api/fields.",
        lifespan=lifespan,
    )
    app.state.service = service
    max_history = service.cfg.state.history_seconds
    max_frames = service.cfg.state.frame_buffer

    @app.get("/api", tags=["meta"], summary="API index")
    async def index():
        return {"name": "navien", "api_version": API_VERSION,
                "endpoints": ["/api/state", "/api/history", "/api/frames", "/api/fields",
                              "/api/health", "/ws", "/docs"]}

    @app.get("/api/state", tags=["data"], summary="Latest completed bus cycle")
    async def state():
        return service.latest()

    @app.get("/api/history", tags=["data"], summary="Recent cycle snapshots, oldest first")
    async def history(seconds: float = Query(max_history, gt=0, le=max_history)):
        return service.state.history_since(seconds)

    @app.get("/api/frames", tags=["debug"], summary="Recent raw frames, oldest first")
    async def frames(limit: int = Query(100, ge=1, le=max_frames),
                     stream: str | None = Query(None, description='e.g. "50>51/10"'),
                     kind: str | None = Query(None, description="water, gas, cascade, poll, …")):
        out = [f for f in service.state.frames
               if (stream is None or f.stream == stream) and (kind is None or f.kind == kind)]
        return [f.to_dict() for f in out[-limit:]]

    @app.get("/api/fields", tags=["meta"], summary="Decoded field table (frame indices, units, confidence)")
    async def fields():
        return fields_table()

    @app.get("/api/health", tags=["meta"], summary="Bus, reader, raw log and hub status")
    async def health():
        return service.health()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        """Live push; protocol in navien/ws.py. Not listed in /docs (OpenAPI has no WebSockets)."""
        await websocket.accept()
        client = websocket.client
        name = f"ws:{client.host}:{client.port}" if client else "ws"
        await WsSession(service, websocket.send_text, websocket.receive_text, name).run()
        try:                                     # no-op if the client already went away
            await asyncio.wait_for(websocket.close(), 1.0)
        except Exception:
            pass

    # Last: the mount at "/" would otherwise shadow the routes above.
    app.mount("/", DashboardFiles(directory=WEB_DIR, html=True), name="dashboard")
    return app
