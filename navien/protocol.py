"""Link layer: checksum, frame splitting, frame classification.

All byte offsets are frame indices (0 = the 0xF7 marker). See PROTOCOL_NOTES.md §1-§3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MARKER = 0xF7
HDR_SIZE = 6
MAX_PAYLOAD = 64          # anything larger is treated as a framing error

SEED_MASTER = 0x4B        # every frame from src 0x50
SEED_OTHER = 0x62         # everything else (slaves 0x51.., controller 0x0F)

ADDR_MASTER = 0x50
ADDR_CONTROLLER = 0x0F

DIR_STATUS = 0x90
DIR_CONTROL = 0x10

# Packet type byte (frame[6]) for the frame kinds seen on this cascade.
TYPE_POLL = 0x40          # master -> 0x52 (absent unit 2?), sys_type 0x13, `40 00 04`
TYPE_CASCADE = 0x41       # master -> slave, master's own state one cycle late
TYPE_WATER = 0x42
TYPE_MASTER_44 = 0x44     # master -> itself, constant, unknown
TYPE_GAS = 0x45
TYPE_ANNOUNCE = 0x4A      # NaviLink present
TYPE_COMMAND = 0x4F


def navien_checksum(data: bytes, seed: int) -> int:
    """Checksum over header + payload (everything except the checksum byte)."""
    r = 0xFF
    for b in data:
        r <<= 1
        if r > 0xFF:
            r = (r & 0xFF) ^ seed
        r = (r & 0xFF) ^ b
    return r & 0xFF


def seed_for_src(src: int) -> int:
    return SEED_MASTER if src == ADDR_MASTER else SEED_OTHER


def classify(src: int, dst: int, direction: int, ptype: int | None) -> str:
    if direction == DIR_STATUS:
        if dst == ADDR_MASTER:
            return "water"
        if dst == ADDR_CONTROLLER:
            return "gas"
        return "status?"
    if direction == DIR_CONTROL:
        if ptype == TYPE_ANNOUNCE:
            return "announce"
        if ptype == TYPE_COMMAND:
            return "command"
        if ptype == TYPE_CASCADE:
            return "cascade"
        if ptype == TYPE_POLL:
            return "poll"
        if ptype == TYPE_MASTER_44:
            return "master_44"
        return "control?"
    return "unknown"


@dataclass(frozen=True)
class Frame:
    ts: float                 # arrival time of the first byte (epoch seconds)
    raw: bytes                # whole frame, marker through checksum
    seed: int | None = field(default=None)   # seed that validated it, None = bad checksum

    @property
    def sys_type(self) -> int:
        return self.raw[1]

    @property
    def src(self) -> int:
        return self.raw[2]

    @property
    def dst(self) -> int:
        return self.raw[3]

    @property
    def direction(self) -> int:
        return self.raw[4]

    @property
    def length(self) -> int:
        return self.raw[5]

    @property
    def payload(self) -> bytes:
        return self.raw[HDR_SIZE:-1]

    @property
    def ptype(self) -> int | None:
        return self.raw[HDR_SIZE] if self.length else None

    @property
    def checksum(self) -> int:
        return self.raw[-1]

    @property
    def checksum_ok(self) -> bool:
        return self.seed is not None

    @property
    def seed_expected(self) -> bool:
        """True when the validating seed is the one the seed-by-source rule predicts."""
        return self.seed == seed_for_src(self.src)

    @property
    def stream(self) -> str:
        return f"{self.src:02X}>{self.dst:02X}/{self.direction:02X}"

    @property
    def kind(self) -> str:
        return classify(self.src, self.dst, self.direction, self.ptype)

    def to_dict(self) -> dict:
        return {
            "ts": round(self.ts, 4),
            "stream": self.stream,
            "kind": self.kind,
            "sys_type": self.sys_type,
            "src": self.src,
            "dst": self.dst,
            "dir": self.direction,
            "len": self.length,
            "checksum_ok": self.checksum_ok,
            "seed": self.seed,
            "hex": self.raw.hex(" "),
        }


def _validating_seed(frame: bytes) -> int | None:
    body, cs = frame[:-1], frame[-1]
    expected = seed_for_src(frame[2])
    for seed in (expected, SEED_OTHER if expected == SEED_MASTER else SEED_MASTER):
        if navien_checksum(body, seed) == cs:
            return seed
    return None


class FrameParser:
    """Incremental parser: feed() bytes as they arrive, get back complete frames.

    Ported from navien-capture.py. A frame with a bad checksum is still returned
    (seed=None) when the next byte is a marker, i.e. the boundary looks right;
    otherwise the parser slides one byte and resynchronises.

    Unlike the capture script (and upstream), any sys_type is accepted: the master
    sends a valid sys_type 0x13 frame every cycle (PROTOCOL_NOTES §4.6), which the old
    1..9 filter discarded as noise.
    """

    def __init__(self) -> None:
        self.buf = bytearray()
        self.buf_ts: list[float] = []     # arrival timestamp per byte in buf
        self.resyncs = 0

    def feed(self, data: bytes, ts: float) -> list[Frame]:
        self.buf.extend(data)
        self.buf_ts.extend([ts] * len(data))
        out: list[Frame] = []
        while True:
            try:
                i = self.buf.index(MARKER)
            except ValueError:
                self._drop(len(self.buf))
                break
            if i:
                self.resyncs += 1
                self._drop(i)
            if len(self.buf) < HDR_SIZE:
                break
            if self.buf[5] > MAX_PAYLOAD:
                self.resyncs += 1
                self._drop(1)
                continue
            total = HDR_SIZE + self.buf[5] + 1
            if len(self.buf) < total:
                break
            raw = bytes(self.buf[:total])
            seed = _validating_seed(raw)
            if seed is None:
                if len(self.buf) <= total:
                    break                       # need the next byte to decide
                if self.buf[total] != MARKER:
                    self.resyncs += 1
                    self._drop(1)
                    continue
            out.append(Frame(self.buf_ts[0], raw, seed))
            self._drop(total)
        return out

    def _drop(self, n: int) -> None:
        del self.buf[:n]
        del self.buf_ts[:n]
