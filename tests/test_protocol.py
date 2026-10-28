import pytest

from navien.protocol import (Frame, FrameParser, SEED_MASTER, SEED_OTHER, navien_checksum,
                             seed_for_src)

from conftest import CAPTURES, jsonl_frames, parse_file

# 240A2 cascade vectors from htumanyan/navien src/checksum.cpp (commit b49bdfc).
UPSTREAM_SLAVE_WATER = bytes.fromhex(
    "F7 02 51 50 90 22 42 20 00 25 14 5C 57 4D 00 00 00 00 00 00 A0 BE 00 20 02 0C 00 00 "
    "06 00 12 00 00 02 00 00 00 00 00 00 A2")
# Upstream labels this one seed 0x62 ("TODO: verify"); it validates with 0x4B.
UPSTREAM_CASCADE_41 = bytes.fromhex(
    "F7 02 50 51 10 22 41 00 00 01 00 5C 56 4B 00 00 00 00 00 00 01 00 00 20 02 0C 43 20 "
    "6B 0A 0E 00 06 02 00 00 00 C2 00 00 BE")


@pytest.mark.parametrize("vec, seed", [(UPSTREAM_SLAVE_WATER, SEED_OTHER),
                                       (UPSTREAM_CASCADE_41, SEED_MASTER)])
def test_upstream_vectors(vec, seed):
    assert navien_checksum(vec[:-1], seed) == vec[-1]
    assert seed_for_src(vec[2]) == seed


@pytest.mark.parametrize("path", CAPTURES, ids=lambda p: p.stem)
def test_capture_parses_clean(path):
    frames, parser = parse_file(path)
    # The capture starts mid-frame: exactly one resync, at the very start.
    assert parser.resyncs == (0 if path.read_bytes()[0] == 0xF7 else 1)
    assert all(f.checksum_ok and f.seed_expected for f in frames)
    # Everything navien-capture.py found, plus one sys_type 0x13 poll per cycle it dropped.
    found = [f.raw for f in frames if f.sys_type == 0x02]
    assert found == jsonl_frames(path)
    polls = [f for f in frames if f.kind == "poll"]
    cycles = [f for f in frames if f.kind == "cascade"]
    assert len(polls) == len(cycles)
    assert {p.raw.hex() for p in polls} == {"f71350521003400004a6"}


def test_byte_by_byte_with_garbage_prefix(main_frames):
    data = b"\x00\x13\xf7\x55" + b"".join(f.raw for f in main_frames[:20])
    p = FrameParser()
    out = []
    for i, b in enumerate(data):
        out += p.feed(bytes([b]), float(i))
    assert [f.raw for f in out] == [f.raw for f in main_frames[:20]]
    assert out[0].ts == 4.0            # timestamp of the frame's first byte


def test_bad_checksum_kept_when_boundary_looks_right(main_frames):
    good = main_frames[0].raw
    bad = good[:-1] + bytes([good[-1] ^ 0xFF])
    out = FrameParser().feed(bad + main_frames[1].raw, 0.0)
    assert [f.checksum_ok for f in out] == [False, True]


def test_frame_properties(main_frames):
    f = next(f for f in main_frames if f.kind == "water" and f.src == 0x50)
    assert (f.stream, f.length, f.ptype) == ("50>50/90", 34, 0x42)
    d = f.to_dict()
    assert d["kind"] == "water" and d["checksum_ok"] and d["seed"] == SEED_MASTER


def test_kinds_per_cycle(main_frames):
    kinds = {f.stream: f.kind for f in main_frames}
    assert kinds == {
        "50>51/10": "cascade", "51>50/90": "water", "51>0F/90": "gas", "50>52/10": "poll",
        "50>50/90": "water", "50>0F/90": "gas", "50>50/10": "master_44",
    }
    assert isinstance(main_frames[0], Frame)
