import os
import time

from navien.config import RawlogCfg
from navien.protocol import FrameParser
from navien.rawlog import RawLogger, period_key
from navien.source import ReplaySource, load_chunks

from conftest import MAIN_CAPTURE

T0 = 1793190600.0                     # 2026-10-28 12:30:00 UTC


def chunks_from_capture():
    return load_chunks(MAIN_CAPTURE)


def logger(tmp_path, **kw):
    return RawLogger(RawlogCfg(dir=str(tmp_path), flush_seconds=0, **kw))


def test_period_keys():
    assert period_key(T0, "hourly") == "20261028T12Z"
    assert period_key(T0, "daily") == "20261028Z"


def test_rotation_compression_and_roundtrip(tmp_path):
    chunks = chunks_from_capture()
    rl = logger(tmp_path)
    # Spread the capture over three hours: rotate twice.
    shifted = [(T0 + i * 200.0, d) for i, (_, d) in enumerate(chunks[:60])]
    for ts, d in shifted:
        rl.write(ts, d)
    rl.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "navien_20261028T12Z.raw.gz" in names and "navien_20261028T12Z.idx.gz" in names
    assert "navien_20261028T13Z.raw.gz" in names
    assert names[-2:] == ["navien_20261028T15Z.idx", "navien_20261028T15Z.raw"]   # open one

    # Every byte comes back, with the original chunk times.
    back = []
    for path in sorted(tmp_path.glob("*.raw*")):
        back += load_chunks(path)
    assert back == [(round(ts, 4), d) for ts, d in shifted]


def test_restart_appends_to_current_period(tmp_path):
    chunks = chunks_from_capture()
    for start in (0, 10):
        rl = logger(tmp_path)
        for i, (_, d) in enumerate(chunks[start:start + 10]):
            rl.write(T0 + start + i, d)
        rl.close()
    back = load_chunks(tmp_path / "navien_20261028T12Z.raw")
    assert [d for _, d in back] == [d for _, d in chunks[:20]]


def test_bursts_share_one_idx_line(tmp_path):
    rl = logger(tmp_path)
    for ts, d in [(T0, b"\xf7\x02"), (T0 + 0.001, b"\x50\x50"), (T0 + 0.004, b"\x90"),
                  (T0 + 0.030, b"\xf7"), (T0 + 0.031, b"\x02")]:
        rl.write(ts, d)
    rl.close()
    idx = (tmp_path / "navien_20261028T12Z.idx").read_text().splitlines()
    assert idx == [f"0\t{T0:.4f}", f"5\t{T0 + 0.03:.4f}"]
    assert load_chunks(tmp_path / "navien_20261028T12Z.raw") == [
        (T0, b"\xf7\x02\x50\x50\x90"), (round(T0 + 0.03, 4), b"\xf7\x02")]


def test_retention_by_age_and_size(tmp_path):
    old = time.time() - 90 * 86400
    for key in ("20260101T00Z", "20260101T01Z", "20261027T00Z"):
        for ext in (".raw.gz", ".idx.gz"):
            p = tmp_path / f"navien_{key}{ext}"
            p.write_bytes(b"x" * 1000)
            if key.startswith("202601"):
                os.utime(p, (old, old))
    rl = logger(tmp_path, keep_days=60)
    rl.write(time.time(), b"\xf7")
    rl.close()
    stems = {p.name.split(".")[0] for p in tmp_path.iterdir()}
    assert "navien_20260101T00Z" not in stems and "navien_20261027T00Z" in stems

    rl = logger(tmp_path, keep_days=60, max_total_mb=0)
    rl.write(time.time(), b"\xf7")
    rl.close()
    stems = {p.name.split(".")[0] for p in tmp_path.iterdir()}
    assert stems == {f"navien_{period_key(time.time(), 'hourly')}"}


def test_thread_put_never_blocks(tmp_path):
    rl = RawLogger(RawlogCfg(dir=str(tmp_path)), queue_size=2)
    assert rl.put(T0, b"a") and rl.put(T0, b"b")
    assert not rl.put(T0, b"c")          # thread not started: queue full, chunk dropped
    assert rl.dropped_chunks == 1
    rl.start()
    rl.stop()
    assert (tmp_path / "navien_20261028T12Z.raw").read_bytes() == b"ab"


def test_replay_source_speed0_reproduces_frames(tmp_path):
    src = ReplaySource(MAIN_CAPTURE, speed=0)
    src.open()
    p, frames = FrameParser(), []
    while not src.finished:
        ts, d = src.read()
        frames += p.feed(d, ts)
    data = MAIN_CAPTURE.read_bytes()
    joined = b"".join(f.raw for f in frames)
    start = data.index(b"\xf7")                # the capture starts mid-frame
    assert data[start:start + len(joined)] == joined
    assert len(data) - len(joined) < 50        # only the partial frames at either end
    assert all(b.ts > a.ts for a, b in zip(frames, frames[1:]))
