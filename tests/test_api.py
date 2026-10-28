import time

import pytest
from fastapi.testclient import TestClient

from navien import config
from navien.api import create_app
from navien.service import Service

from conftest import MAIN_CAPTURE


def wait_for(client, pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        h = client.get("/api/health").json()
        if pred(h):
            return h
        time.sleep(0.05)
    raise AssertionError(f"timed out; last health: {h}")


@pytest.fixture(scope="module")
def replay_client():
    """The service over a full replay of the main capture (speed 0), finished."""
    cfg = config.load()
    cfg.state.history_seconds = 30
    app = create_app(Service(cfg, replay=str(MAIN_CAPTURE), speed=0))
    with TestClient(app) as client:
        wait_for(client, lambda h: h["reader"]["finished"])
        yield client


def test_index_and_docs(replay_client):
    assert "/api/state" in replay_client.get("/api").json()["endpoints"]
    assert replay_client.get("/docs").status_code == 200
    paths = replay_client.get("/openapi.json").json()["paths"]
    assert {"/api/state", "/api/history", "/api/frames", "/api/fields", "/api/health"} <= set(paths)


def test_dashboard_is_served(replay_client):
    r = replay_client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<title>Navien cascade</title>" in r.text and r.headers["cache-control"] == "no-cache"
    for name in ("app.js", "units.js", "debug.js", "style.css"):
        r = replay_client.get(f"/{name}")
        assert r.status_code == 200, name
    assert "javascript" in replay_client.get("/app.js").headers["content-type"]
    assert replay_client.get("/nope.js").status_code == 404


def test_state(replay_client):
    s = replay_client.get("/api/state").json()
    assert s["type"] == "cycle" and s["seq"] == 59
    assert s["units"]["0"]["stage"] == "active_combustion"
    assert s["units"]["1"]["stage"] == "standby"
    assert s["units"]["0"]["device_type"] == "CAS_NPE2"
    assert s["cascade"]["x28"] == s["cascade"]["x26"] + 40


def test_history(replay_client):
    full = replay_client.get("/api/history").json()
    assert 29 <= len(full) <= 31                       # history_seconds = 30
    assert [h["seq"] for h in full] == sorted(h["seq"] for h in full)
    assert len(replay_client.get("/api/history?seconds=5").json()) in (5, 6, 7)
    assert replay_client.get("/api/history?seconds=31").status_code == 422
    assert replay_client.get("/api/history?seconds=0").status_code == 422


def test_frames(replay_client):
    frames = replay_client.get("/api/frames?limit=7").json()
    assert len(frames) == 7
    assert len({f["stream"] for f in frames}) >= 6
    polls = replay_client.get("/api/frames?kind=poll&limit=600").json()
    assert polls and all(f["hex"] == "f7 13 50 52 10 03 40 00 04 a6" for f in polls)
    one = replay_client.get("/api/frames", params={"stream": "50>51/10", "limit": 2}).json()
    assert [f["kind"] for f in one] == ["cascade", "cascade"]
    assert replay_client.get("/api/frames?limit=601").status_code == 422


def test_fields(replay_client):
    names = {(r["kind"], r["name"]) for r in replay_client.get("/api/fields").json()}
    assert ("water", "error_code") in names and ("gas", "gas_kcalh") in names


def test_health(replay_client):
    h = replay_client.get("/api/health").json()
    assert h["bus"]["frames"] == 420 and h["bus"]["checksum_fail"] == 0
    assert h["reader"]["resyncs"] == 0 and h["rawlog"] is None
    assert h["history_cycles"] > 0 and h["bus"]["cycle_ms"] is not None


def test_missing_serial_port_is_reported_not_fatal(tmp_path):
    cfg = config.load()
    cfg.serial.port = "/dev/does-not-exist"
    cfg.rawlog.dir = str(tmp_path)
    with TestClient(create_app(Service(cfg))) as client:
        h = wait_for(client, lambda h: h["reader"]["last_error"])
        assert h["status"] == "no_data" and not h["bus_alive"]
        assert not h["reader"]["connected"]
        assert h["source"] == "serial:/dev/does-not-exist"
        s = client.get("/api/state").json()
        assert s["units"] == {} and s["seq"] == 0


def test_second_opener_of_the_port_is_refused(tmp_path):
    # Two readers on one tty would each get part of the bytes; the flock prevents it.
    import os
    import serial
    from navien.source import SerialSource
    master, slave = os.openpty()
    path = os.ttyname(slave)
    first = SerialSource(path)
    first.open()
    try:
        with pytest.raises(serial.SerialException):
            SerialSource(path).open()
    finally:
        first.close()
        os.close(master)
        os.close(slave)
