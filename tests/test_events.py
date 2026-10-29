import asyncio
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from navien.backfill import backfill, cycles, find_logs, parse_time, period
from navien.config import Config, EventsCfg
from navien.events import Recorder
from navien.protocol import FrameParser
from navien.source import load_chunks

from test_influx import FakeInflux, parse

# 2026-10-28 18:17:30-18:32:30 CDT from the service log: an aborted start, a one-unit
# draw, the two-unit draw with stage 2 (PROTOCOL_NOTES §4.4.1), both units' recirc runs.
EXCERPT = Path(__file__).parent / "data" / "navien_20261028T23Z_excerpt.raw.gz"


def at(hms: str) -> float:
    return datetime.fromisoformat(f"2026-10-28T{hms}").timestamp()


def record(snaps, cfg=None):
    rec = Recorder(cfg)
    lines = [ln for s in snaps for _, ln in rec.feed(s)] + [ln for _, ln in rec.finish()]
    return [parse(ln) for ln in lines]


def events(points, type_=None, unit=None):
    return [(tags, f, ts / 1000) for m, tags, f, ts in points if m == "navien_event"
            and (type_ is None or tags["type"] == type_) and (unit is None or tags["unit"] == unit)]


def num(v: str) -> float:
    return float(v.rstrip("i"))


@pytest.fixture(scope="module")
def excerpt_snaps():
    return list(cycles([EXCERPT], 0, float("inf")))


@pytest.fixture(scope="module")
def excerpt_points(excerpt_snaps):
    return record(excerpt_snaps)


# ------------------------------------------------------------ excerpt events

def test_excerpt_draws(excerpt_points):
    draws = events(excerpt_points, "draw", "all")
    assert len(draws) == 2
    (_, small, t1), (_, big, t2) = draws
    assert abs(t1 - at("18:18:29")) < 2 and small["units_max"] == "1i"
    assert 100 < num(small["duration_s"]) < 115 and 7.5 < num(small["volume_l"]) < 9
    assert abs(t2 - at("18:23:06")) < 2 and big["units_max"] == "2i"
    assert 420 < num(big["duration_s"]) < 440 and 120 < num(big["volume_l"]) < 135
    assert num(big["peak_flow_lpm"]) > 19 and 50 < num(big["mean_outlet_c"]) < 52.5
    assert all(f["truncated"] == "false" for _, f, _ in draws)
    # per-unit draws add up to the cascade draw
    per_unit = sum(num(f["volume_l"]) for _, f, t in events(excerpt_points, "draw_unit")
                   if t >= at("18:23:00"))
    assert per_unit == pytest.approx(num(big["volume_l"]), rel=0.01)


def test_excerpt_stage2_recirc_and_burns(excerpt_points):
    (_, s2, t), = events(excerpt_points, "stage2")
    assert abs(t - at("18:23:23")) < 2
    assert num(s2["trigger_flow_lpm"]) == 16.8 and num(s2["trigger_capacity_pct"]) == 56.0
    rec = [(tags["unit"], round(t - at("18:00:00")), f) for tags, f, t in events(excerpt_points, "recirc")]
    assert [u for u, _, _ in rec] == ["0", "0", "1"]
    assert all(abs(t - want) <= 2 for (_, t, _), want in zip(rec, (1217, 1814, 1816)))
    (_, _, m1), (_, _, m2), (_, _, s1) = rec
    assert 75 < num(m1["duration_s"]) < 85 and 50 < num(s1["duration_s"]) < 65
    assert num(m2["since_last_s"]) == pytest.approx(597, abs=2)          # 18:20:17 -> 18:30:14
    assert "since_last_s" not in m1 and "since_last_s" not in s1         # first run seen
    burns = events(excerpt_points, "burn", "0")
    failed = [f for _, f, _ in burns if f["failed"] == "true"]
    assert len(failed) == 1 and failed[0]["ignitions"] == "0i"           # 18:18:29 aborted start
    lit = [f for _, f, _ in burns if f["failed"] == "false"]
    assert all(num(f["ignition_s"]) < 15 and f["mode_code"] in ("8i", "32i") for f in lit)
    slave = [f for _, f, _ in events(excerpt_points, "burn", "1")]
    assert any(f["mode_code"] == "32i" and num(f["flame_s"]) > 400 for f in slave)


def test_excerpt_hourly_matches_frame_level_integral(excerpt_points):
    """Hourly totals against an independent calculation straight from the frames."""
    water = {"dhw": 0.0, "recirc": 0.0}
    gas = 0.0
    prev, mode = {}, {}
    p = FrameParser()
    for ts, data in load_chunks(EXCERPT):
        for f in p.feed(data, ts):
            if not f.checksum_ok:
                continue
            key = (f.kind, f.src)
            dt = min(ts - prev[key], 2.0) if key in prev else 0.0
            prev[key] = ts
            if f.kind == "water":
                mode[f.src] = {0x20: "dhw", 0x08: "recirc"}.get(f.raw[8])
                if mode[f.src]:
                    water[mode[f.src]] += f.raw[18] / 10 * dt / 60
            elif f.kind == "gas":
                gas += (f.raw[22] | f.raw[23] << 8) * dt / 3600
    hourly = [f for m, tags, f, _ in excerpt_points if m == "navien_hourly" and tags["unit"] == "all"]
    assert len(hourly) == 1                                               # all in 23Z
    h = hourly[0]
    assert num(h["water_dhw_l"]) == pytest.approx(water["dhw"], rel=0.02)
    assert num(h["water_recirc_l"]) == pytest.approx(water["recirc"], rel=0.02)
    total_gas = sum(num(h[k]) for k in ("gas_dhw_kcal", "gas_recirc_kcal", "gas_other_kcal"))
    assert total_gas == pytest.approx(gas, rel=0.02)
    assert h["draws"] == "2i" and h["recirc_runs"] == "3i" and h["failed_ignitions"] == "1i"
    assert 470 < num(h["stage2_s"]) < 490 and 890 < num(h["coverage_s"]) <= 900
    units = {tags["unit"]: f for m, tags, f, _ in excerpt_points if m == "navien_hourly"}
    assert num(units["0"]["water_dhw_l"]) + num(units["1"]["water_dhw_l"]) == \
        pytest.approx(num(h["water_dhw_l"]), abs=0.01)


def test_recording_is_deterministic(excerpt_snaps, excerpt_points):
    assert record(excerpt_snaps) == excerpt_points


def test_restart_mid_hour_rewrites_the_same_series(excerpt_snaps):
    """A partial hour (shutdown) and the later full hour land on the same series+time."""
    first = record(excerpt_snaps[:300])
    full = record(excerpt_snaps)
    key = lambda pts: {(tuple(sorted(t.items())), ts) for m, t, f, ts in pts if m == "navien_hourly"}
    assert key(first) == key(full)


# ---------------------------------------------------------- synthetic cycles

T0 = 1_800_000_000.0 - 1_800_000_000.0 % 3600 + 600         # 10 minutes into a UTC hour


def snap(t, mode=0, flow=0.0, stage=0x14, flame=False, stage2=False, error=0, units=("0",)):
    u = {"heating_mode_code": mode, "flow_lpm": flow, "gas_kcalh": 20000 if flame else 0,
         "stage_code": stage, "flame": flame, "outlet_c": 50.0, "inlet_c": 20.0, "set_c": 51.5,
         "capacity_pct": 40.0, "error_code": error, "error_level": 0,
         "age_s": {"water": 0.1, "gas": 0.1}}
    return {"ts": t, "units": {k: dict(u) for k in units},
            "cascade": {"stage2": stage2, "age_s": 0.1}}


def run(pattern, t0=T0, **kw):
    """pattern: string, one char per 1 s cycle: '.' idle, 'D' draw, '_' no cycle."""
    out, t = [], t0
    for c in pattern:
        if c != "_":
            out.append(snap(t, **(dict(mode=0x20, flow=5.0) if c == "D" else {}), **kw))
        t += 1.0
    return out


def test_debounce_blip_and_dropout():
    assert events(record(run("." * 5 + "DD" + "." * 10)), "draw") == []   # 2 s blip
    one = events(record(run("." * 5 + "D" * 10 + ".." + "D" * 10 + "." * 10)), "draw")
    assert len(one) == 1 and num(one[0][1]["duration_s"]) == 20.0          # 2 s dropout bridged
    two = events(record(run("." * 5 + "D" * 10 + "." * 5 + "D" * 10 + "." * 10)), "draw")
    assert len(two) == 2
    # the debounce comes from [events]
    assert len(events(record(run("." * 5 + "DD" + "." * 10), EventsCfg(debounce_s=1.0)), "draw")) == 1


def test_gap_truncates_and_blocks_unknown_start():
    snaps = run("." * 3 + "D" * 10 + "_" * 15 + "D" * 10 + "." * 10)
    draws = events(record(snaps), "draw")
    assert len(draws) == 1 and draws[0][1]["truncated"] == "true"
    # missing data (stale unit) is not "off": the run continuing after it is not a new draw
    stale = run("." * 3 + "D" * 10) + [dict(s, units={}) for s in run("." * 15, T0 + 13)] \
        + run("D" * 10 + "." * 10, T0 + 28)
    draws = events(record(stale), "draw")
    assert len(draws) == 1 and draws[0][1]["truncated"] == "true"
    assert events(record(run("D" * 10 + "." * 10)), "draw") == []          # on at the first cycle


def test_burn_short_and_failed():
    lit = [snap(T0 + i) for i in range(3)] + \
          [snap(T0 + 3 + i, stage=0x33, flame=True) for i in range(10)] + \
          [snap(T0 + 13 + i) for i in range(10)]
    (_, b, _), = events(record(lit), "burn")
    assert b["short"] == "true" and b["failed"] == "false" and num(b["flame_s"]) == 10.0
    assert b["ignitions"] == "1i" and num(b["ignition_s"]) == 0.0
    aborted = [snap(T0 + i) for i in range(3)] + [snap(T0 + 3, stage=0x20)] + \
              [snap(T0 + 4 + i, stage=0x46) for i in range(10)]
    (_, b, _), = events(record(aborted), "burn")
    assert b["failed"] == "true" and b["short"] == "false"


def test_hour_boundary():
    end = T0 - 600 + 3600                                          # end of T0's hour
    snaps = run("." * 3 + "D" * 20 + "." * 30, t0=end - 23)        # draw ends at end - 1
    rec = Recorder()
    out = []
    for s in snaps:
        out += [(s["ts"], parse(ln)) for _, ln in rec.feed(s)]
    hourly = [(t_written, p) for t_written, p in out if p[0] == "navien_hourly"]
    assert {p[1]["unit"] for _, p in hourly} == {"0", "all"}
    for t_written, (_, tags, f, ts) in hourly:
        assert ts / 1000 == end - 3600 and t_written > end + 3   # written after the grace
        assert f["draws"] == "1i"                                # draw ending at :59:59 counts
    assert not [p for _, p in out if p[0] == "navien_hourly" and p[3] / 1000 == end]
    rest = [parse(ln) for _, ln in rec.finish()]                 # shutdown: the open hour
    assert {p[3] / 1000 for p in rest if p[0] == "navien_hourly"} == {end}


# ------------------------------------------------------------------ backfill

def test_period_and_find_logs(tmp_path):
    assert period(Path("navien_20261028T23Z.raw.gz")) == (1793228400.0, 1793232000.0)
    assert period(Path("navien_20261028.raw"))[1] - period(Path("navien_20261028.raw"))[0] == 86400
    for name in ("navien_20261028T22Z.raw.gz", "navien_20261028T23Z.raw.gz",
                 "navien_20261029T00Z.raw", "navien_20261029T00Z.idx", "notes.txt"):
        (tmp_path / name).write_bytes(b"")
    names = [f.name for f in find_logs([tmp_path], 1793228400.0, 1793232000.0)]
    assert names == ["navien_20261028T23Z.raw.gz"]
    assert len(find_logs([tmp_path], 0, float("inf"))) == 3
    assert parse_time("2026-10-28", end=True) - parse_time("2026-10-28") == 86400


def test_backfill_writes_and_is_idempotent():
    cfg = Config()
    cfg.influx.url = "http://influx.test:8086"

    def go():
        fake = FakeInflux()
        r = asyncio.run(backfill(cfg, [EXCERPT], 0, float("inf"),
                                 transport=httpx.MockTransport(fake), out=lambda *a: None))
        return r, fake

    r1, f1 = go()
    r2, f2 = go()
    lines = f1.lines()
    assert r1["written"] == len(lines) and r1["rejected"] == 0
    assert not any(ln.startswith("navien_bus") for ln in lines)
    assert r1["events"]["draw"] == 2 and r1["events"]["recirc"] == 3
    assert sorted(lines) == sorted(f2.lines())                      # re-run: identical points
    rps = {r.url.params["rp"] for r in f1.requests}
    assert rps == {"forever", "samples_90d"}


def test_backfill_time_window():
    snaps = list(cycles([EXCERPT], at("18:20:00"), at("18:21:00")))
    assert 58 <= len(snaps) <= 61
    assert at("18:20:00") <= snaps[0]["ts"] and snaps[-1]["ts"] < at("18:21:01")


def test_prime_keeps_the_hour_whole_across_a_restart(excerpt_snaps, tmp_path, monkeypatch):
    """Restart at 18:25: the new process primes from the log, the hour comes out whole."""
    import navien.backfill as bf
    cut = at("18:25:00")
    before = [s for s in excerpt_snaps if s["ts"] < cut]
    after = [s for s in excerpt_snaps if s["ts"] >= cut + 5]          # 5 s restart
    monkeypatch.setattr(bf, "find_logs", lambda dirs, a, b: [EXCERPT])
    rec = Recorder()
    lines = bf.prime(rec, tmp_path, cut)
    assert rec.prev_ts == before[-1]["ts"]
    lines += [ln for s in after for ln in rec.feed(s)] + rec.finish()
    pts = [parse(ln) for _, ln in lines]
    h = [f for m, t, f, _ in pts if m == "navien_hourly" and t["unit"] == "all"][0]
    full = [f for m, t, f, _ in record(excerpt_snaps) if m == "navien_hourly" and t["unit"] == "all"][0]
    assert num(h["water_dhw_l"]) == pytest.approx(num(full["water_dhw_l"]), rel=0.02)
    assert h["draws"] == full["draws"] and h["recirc_runs"] == full["recirc_runs"]
    big = [f for _, f, _ in events(pts, "draw") if f["units_max"] == "2i"]
    assert len(big) == 1 and big[0]["truncated"] == "false"           # not split by the restart
