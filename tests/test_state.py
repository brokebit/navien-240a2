from navien.protocol import Frame
from navien.source import load_chunks
from navien.protocol import FrameParser
from navien.state import StateModel

from conftest import MAIN_CAPTURE


def replay_frames(path):
    p = FrameParser()
    out = []
    for ts, data in load_chunks(path):
        out += p.feed(data, ts)
    return out


def run(frames, **kw):
    sm = StateModel(**kw)
    snaps = [s for s in (sm.ingest(f) for f in frames) if s]
    return sm, snaps


def test_one_snapshot_per_cycle():
    frames = replay_frames(MAIN_CAPTURE)
    sm, snaps = run(frames)
    n_cycles = sum(1 for f in frames if f.kind == "cascade")
    assert len(snaps) == n_cycles - 1          # the last cycle is still open
    assert [s["seq"] for s in snaps] == list(range(1, len(snaps) + 1))
    for s in snaps[1:]:
        assert set(s["units"]) == {"0", "1"}
        assert s["units"]["0"]["role"] == "master" and s["units"]["1"]["role"] == "slave"
        assert s["cascade"]["x28"] == s["cascade"]["x26"] + 40
        assert 900 <= s["bus"]["cycle_ms"] <= 1100
    assert snaps[-1]["bus"]["checksum_fail"] == 0


def test_missing_slave_water_keeps_value_with_age():
    # PROTOCOL_NOTES §3: some cycles have two slave gas frames and no slave water.
    frames = replay_frames(MAIN_CAPTURE)
    _, snaps = run(frames)
    ages = [s["units"]["1"]["age_s"]["water"] for s in snaps[1:]]
    assert max(ages) > 1.0                     # carried over from an earlier cycle
    assert all("stage" in s["units"]["1"] for s in snaps[1:])


def test_bad_checksum_does_not_update_state():
    frames = replay_frames(MAIN_CAPTURE)
    sm, _ = run(frames[:30])
    before = sm.snapshot()["units"]["0"]["outlet_c"]
    water = next(f for f in frames[30:] if f.stream == "50>50/90")
    raw = bytearray(water.raw)
    raw[12] = 0xFE                             # outlet 127 °C, checksum now wrong
    sm.ingest(Frame(water.ts, bytes(raw), None))
    assert sm.snapshot()["units"]["0"]["outlet_c"] == before
    assert sm.checksum_fail == 1


def test_history_window():
    frames = replay_frames(MAIN_CAPTURE)
    sm, snaps = run(frames, history_seconds=10)
    assert 9 <= len(sm.history) <= 11
    assert sm.history[-1] is snaps[-1]
    assert len(sm.history_since(5)) in (5, 6, 7)


def test_timeout_closes_cycle_without_cascade_frames():
    # A single unit (no 0x41 frames) still produces snapshots via the timeout.
    frames = [f for f in replay_frames(MAIN_CAPTURE) if f.kind != "cascade"]
    sm, snaps = run(frames)
    assert len(snaps) > 30
    assert snaps[-1]["cascade"] is None
