import json
from pathlib import Path

import pytest

from navien.protocol import Frame, FrameParser

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = sorted((ROOT / "captures").glob("*.raw"))
MAIN_CAPTURE = ROOT / "captures" / "navien_capture_20261028_123030.raw"


def parse_file(path: Path) -> tuple[list[Frame], FrameParser]:
    p = FrameParser()
    frames = p.feed(path.read_bytes(), 0.0)
    return frames, p


def jsonl_frames(raw_path: Path) -> list[bytes]:
    with open(raw_path.with_suffix(".jsonl")) as f:
        return [bytes.fromhex(json.loads(line)["raw"]) for line in f]


@pytest.fixture(scope="session")
def main_frames() -> list[Frame]:
    return parse_file(MAIN_CAPTURE)[0]
