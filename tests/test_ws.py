import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from navien import config
from navien.api import create_app
from navien.protocol import FrameParser
from navien.service import Service
from navien.source import load_chunks
from navien.ws import WsSession

from conftest import MAIN_CAPTURE


def capture_frames():
    p, out = FrameParser(), []
    for ts, data in load_chunks(MAIN_CAPTURE):
        out += p.feed(data, ts)
    return out


FRAMES = capture_frames()


def make_service(**http):
    cfg = config.load()
    for k, v in http.items():
        setattr(cfg.http, k, v)
    return Service(cfg, replay=str(MAIN_CAPTURE), speed=0)   # never started: we feed it


class FakeSocket:
    """send() can be paused to simulate a client that stops reading."""

    def __init__(self):
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.open = asyncio.Event()
        self.open.set()

    async def send(self, text):
        await self.open.wait()
        self.sent.append(json.loads(text))

    async def receive(self):
        return await self.inbox.get()

    def types(self):
        return [m["type"] for m in self.sent]


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


def test_hello_history_then_cycles():
    async def go():
        svc = make_service()
        svc._on_frames(FRAMES[:70])                  # ~10 cycles before the client connects
        sock = FakeSocket()
        task = asyncio.create_task(WsSession(svc, sock.send, sock.receive).run())
        await settle()
        assert sock.types()[:2] == ["hello", "history"]
        assert sock.sent[0]["topics"] == ["frames"]
        assert len(sock.sent[1]["cycles"]) >= 8
        svc._on_frames(FRAMES[70:140])
        await settle()
        cycles = [m for m in sock.sent[2:] if m["type"] == "cycle"]
        assert len(cycles) >= 9 and "frame" not in sock.types()
        assert [c["seq"] for c in cycles] == sorted(c["seq"] for c in cycles)
        task.cancel()
    asyncio.run(go())


def test_frames_subscription_and_errors():
    async def go():
        svc = make_service()
        sock = FakeSocket()
        task = asyncio.create_task(WsSession(svc, sock.send, sock.receive).run())
        await settle()
        sock.inbox.put_nowait(json.dumps({"subscribe": ["frames"]}))
        await settle()
        svc._on_frames(FRAMES[:14])
        await settle()
        frames = [m for m in sock.sent if m["type"] == "frame"]
        assert len(frames) == 14 and frames[0]["hex"].startswith("f7")
        sock.inbox.put_nowait(json.dumps({"unsubscribe": ["frames"]}))
        await settle()
        n = len(sock.sent)
        svc._on_frames(FRAMES[14:28])
        await settle()
        assert all(m["type"] != "frame" for m in sock.sent[n:])

        for bad in ("not json", "[1]", '{"subscribe": ["nope"]}', '{"get": "history", "seconds": "x"}'):
            sock.inbox.put_nowait(bad)
        sock.inbox.put_nowait(json.dumps({"get": "history", "seconds": 1}))
        await settle()
        tail = sock.sent[-5:]
        assert [m["type"] for m in tail] == ["error"] * 4 + ["history"]
        assert tail[-1]["seconds"] == 1
        task.cancel()
    asyncio.run(go())


def test_stalled_client_drops_oldest_and_others_unaffected():
    async def go():
        svc = make_service(ws_queue=5, ws_send_timeout_s=60)
        slow, fast = FakeSocket(), FakeSocket()
        tasks = [asyncio.create_task(WsSession(svc, s.send, s.receive, name).run())
                 for s, name in ((slow, "slow"), (fast, "fast"))]
        await settle()
        slow.open.clear()                            # slow client stops reading
        for i in range(0, len(FRAMES), 7):           # one cycle at a time, as on the bus
            svc._on_frames(FRAMES[i:i + 7])
            await settle()
        fast_cycles = [m["seq"] for m in fast.sent if m["type"] == "cycle"]
        assert fast_cycles == list(range(1, 60))     # every cycle, in order
        stats = {s["name"]: s for s in svc.hub.stats()}
        assert stats["slow"]["dropped"] > 40 and stats["fast"]["dropped"] == 0

        slow.open.set()                              # client resumes
        await settle()
        after = slow.sent[2:]
        gap = next(m for m in after if m["type"] == "gap")
        assert gap["dropped"] == stats["slow"]["dropped"]
        newest = [m["seq"] for m in after if m["type"] == "cycle"]
        assert newest[-1] == 59                      # kept the newest, dropped the oldest
        for t in tasks:
            t.cancel()
    asyncio.run(go())


def test_send_timeout_ends_session_and_unsubscribes():
    async def go():
        svc = make_service(ws_send_timeout_s=0.05)
        sock = FakeSocket()
        sock.open.clear()                            # never accepts anything
        await asyncio.wait_for(WsSession(svc, sock.send, sock.receive).run(), 2)
        assert svc.hub.subscribers == []
    asyncio.run(go())


# ------------------------------------------------------------- through the real route

@pytest.fixture
def live_client():
    cfg = config.load()
    app = create_app(Service(cfg, replay=str(MAIN_CAPTURE), speed=20, loop_replay=True))
    with TestClient(app) as client:
        yield client


def test_websocket_route(live_client):
    with live_client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["history_seconds"] == 300
        assert hello["version"]
        assert ws.receive_json()["type"] == "history"
        seqs = []
        while len(seqs) < 5:
            m = ws.receive_json()
            if m["type"] == "cycle":
                seqs.append(m["seq"])
        assert seqs == sorted(seqs)
        ws.send_json({"subscribe": ["frames"]})
        kinds = set()
        for _ in range(60):
            m = ws.receive_json()
            if m["type"] == "frame":
                kinds.add(m["kind"])
        assert {"water", "gas", "cascade", "poll"} <= kinds
        h = live_client.get("/api/health").json()
        assert [s["name"] for s in h["hub"]["subscribers"]] == ["ws:testclient:50000"]
    # disconnect cleans up the subscription
    for _ in range(50):
        if not live_client.get("/api/health").json()["hub"]["subscribers"]:
            break
        asyncio.run(asyncio.sleep(0.02))
    assert live_client.get("/api/health").json()["hub"]["subscribers"] == []
