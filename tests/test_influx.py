import asyncio
import math
import re

import httpx
import pytest

from navien.config import InfluxCfg
from navien.hub import Hub
from navien.influx import SCHEMA, InfluxWriter, Points, format_value, line
from navien.protocol import FrameParser
from navien.source import load_chunks
from navien.state import StateModel

from conftest import MAIN_CAPTURE

LINE_RE = re.compile(r"^(?P<m>[a-z_]+)(?P<tags>(,[a-z_]+=[^ ,]+)*) (?P<fields>\S+) (?P<ts>\d{13})$")


@pytest.fixture(scope="module")
def snaps():
    p, sm, out = FrameParser(), StateModel(), []
    for ts, data in load_chunks(MAIN_CAPTURE):
        for f in p.feed(data, ts):
            s = sm.ingest(f)
            if s:
                out.append(s)
    return out


def parse(ln):
    m = LINE_RE.match(ln)
    assert m, ln
    fields = dict(kv.split("=", 1) for kv in m["fields"].split(","))
    tags = dict(kv.split("=", 1) for kv in m["tags"].lstrip(",").split(",")) if m["tags"] else {}
    return m["m"], tags, fields, int(m["ts"])


# ---------------------------------------------------------------- line protocol

def test_format_value_types():
    assert format_value(True, bool) == "true" and format_value(0, bool) == "false"
    assert format_value(51, int) == "51i" and format_value(51.0, int) == "51i"
    assert format_value(51, float) == "51.0" and format_value(4.3, float) == "4.3"
    assert format_value(math.nan, float) is None and format_value(None, int) is None
    assert format_value('a "b" \\c', str) == '"a \\"b\\" \\\\c"'


def test_line_escaping_and_coercion():
    ln = line("navien_counters", {"unit": "0", "role": "mas ter,x=y"},
              {"controller_version": "1.2", "dhw_uses": 5.0, "gas_total_m3": None}, 1234)
    assert ln == 'navien_counters,role=mas\\ ter\\,x\\=y,unit=0 controller_version="1.2",dhw_uses=5i 1234'
    assert line("navien_cascade", {}, {"x26": None}, 1) is None


# ------------------------------------------------------------ snapshot -> points

def test_points_from_capture(snaps):
    pts = Points()
    lines = [x for s in snaps for x in pts.from_cycle(s, {"resyncs": 0})]
    by_m = {}
    for queue, ln in lines:
        m, tags, fields, ts = parse(ln)
        by_m.setdefault(m, []).append((tags, fields))
        assert queue == ("samples" if m in ("navien_unit", "navien_cascade") else "rollup")
        assert set(tags) == set(SCHEMA[m][0])
        for name, v in fields.items():                       # every value has its schema type
            typ = SCHEMA[m][1][name]
            assert {int: v.endswith("i"), bool: v in ("true", "false"),
                    str: v.startswith('"'), float: re.fullmatch(r"-?\d+\.\d+(e-?\d+)?", v) is not None}[typ], (m, name, v)
    n = len(snaps)
    assert len(by_m["navien_unit"]) == 2 * n and len(by_m["navien_cascade"]) == n
    assert 1 <= len(by_m["navien_bus"]) <= 3                  # once per minute, 60 s capture
    assert len(by_m["navien_counters"]) == 2 * len(by_m["navien_bus"])
    # mode split: while in DHW demand the dhw copies equal the totals, recirc copies are 0
    master = [f for t, f in by_m["navien_unit"] if t["unit"] == "0"]
    dhw = [f for f in master if f["mode_code"] == "32i"]
    assert dhw and all(f["flow_dhw_lpm"] == f["flow_lpm"] and f["gas_dhw_kcalh"] == f["gas_kcalh"]
                       and f["flow_recirc_lpm"] == "0.0" for f in dhw)
    idle = [f for f in master if f["mode_code"] == "0i"]
    assert idle and all(f["flow_dhw_lpm"] == "0.0" and f["gas_dhw_kcalh"] == "0i" for f in idle)


def test_sample_every_and_stale(snaps):
    pts = Points(sample_every_s=5)
    n = sum(1 for s in snaps for q, ln in pts.from_cycle(s) if ln.startswith("navien_cascade"))
    assert abs(n - len(snaps) / 5) <= 2
    s = dict(snaps[10])
    s["units"] = {k: dict(u, age_s={"water": 9.0, "gas": 0.1}) for k, u in s["units"].items()}
    lines = [ln for q, ln in Points(stale_s=3).from_cycle(s)]
    assert not any(ln.startswith("navien_unit") for ln in lines)
    assert any(ln.startswith("navien_cascade") for ln in lines)


# ------------------------------------------------------------------- writer

class FakeInflux:
    """httpx transport that records writes and answers from a script of status codes."""

    def __init__(self, statuses=()):
        self.statuses = list(statuses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 204
        if status == "timeout":
            raise httpx.ConnectTimeout("timed out", request=request)
        body = {"error": "partial write: points beyond retention policy dropped=1"} if status == 400 else {}
        return httpx.Response(status, json=body if status != 204 else None)

    def lines(self):
        return [ln for r in self.requests for ln in r.content.decode().split("\n")]


def writer(fake, **cfg):
    c = InfluxCfg(enabled=True, url="http://influx.test:8086/", database="navien",
                  retention_policy="samples_90d", rollup_retention_policy="forever", **cfg)
    return InfluxWriter(c, transport=httpx.MockTransport(fake))


def test_writer_batches_by_retention_policy(snaps):
    async def go():
        fake = FakeInflux()
        w = writer(fake)
        for s in snaps:
            w.ingest(s)
        assert await w.flush() and w.queued == 0
        rps = [r.url.params["rp"] for r in fake.requests]
        assert rps[0] == "forever" and set(rps) == {"forever", "samples_90d"}
        for r in fake.requests:
            assert r.url.path == "/write" and r.method == "POST"
            assert r.url.params["db"] == "navien" and r.url.params["precision"] == "ms"
        assert w.written == len(fake.lines()) and w.dropped == w.rejected == 0
        await w.client.aclose()
    asyncio.run(go())


def test_retryable_errors_keep_points_and_back_off(snaps):
    async def go():
        fake = FakeInflux([500, "timeout", 404, 204, 204])
        w = writer(fake)
        for s in snaps[:5]:
            w.ingest(s)
        n = w.queued
        for expected_backoff in (1, 2, 4):
            assert not await w.flush()
            assert w.queued == n and w.backoff_s == expected_backoff
        assert "404" in w.last_error
        assert await w.flush() and w.queued == 0 and w.written == n and w.backoff_s == 0
        await w.client.aclose()
    asyncio.run(go())


def test_bad_request_is_rejected_not_retried(snaps):
    async def go():
        fake = FakeInflux([400])
        w = writer(fake)
        w.ingest(snaps[0])
        rollup, samples = len(w.queues["rollup"]), len(w.queues["samples"])
        assert await w.flush()                               # rollup batch rejected, samples written
        assert w.queued == 0 and w.rejected == rollup and w.written == samples
        assert "beyond retention policy" in w.last_error
        await w.client.aclose()
    asyncio.run(go())


def test_overflow_drops_oldest_samples_first(snaps):
    async def go():
        w = writer(FakeInflux(), max_queue=20)
        for s in snaps:
            w.ingest(s)
        assert w.queued == 20 and w.dropped > 0
        assert len(w.queues["rollup"]) >= 3                  # counters + bus of the first minute kept
        newest = parse(w.queues["samples"][-1])[3]
        assert newest == round(snaps[-1]["ts"] * 1000)
        await w.client.aclose()
    asyncio.run(go())


def test_run_loop_and_close_write_everything(snaps):
    async def go():
        fake = FakeInflux([503])
        w = writer(fake, batch_seconds=0.05)
        hub = Hub()
        task = asyncio.create_task(w.run(hub.subscribe({"cycle"}, maxsize=600, name="influx")))
        for s in snaps[:30]:
            hub.publish("cycle", s)
        await asyncio.sleep(1.3)                              # first flush 503, retry after 1 s
        for s in snaps[30:]:
            hub.publish("cycle", s)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await w.close()
        expected = [ln for s in snaps for q, ln in Points().from_cycle(s)]
        got = fake.lines()
        assert sum(1 for ln in got if ln.startswith("navien_unit")) == \
            sum(1 for ln in expected if ln.startswith("navien_unit"))
        assert w.queued == 0 and w.batches >= 2
    asyncio.run(go())


def test_replay_never_writes_to_influx():
    from navien.config import Config
    from navien.service import Service
    cfg = Config()
    cfg.influx.enabled = True
    assert Service(cfg, replay=str(MAIN_CAPTURE)).influx is None
