"""State model: merges decoded frames into one snapshot per bus cycle.

Pure and synchronous (no threads, no asyncio) so it is easy to test; the service owns
one instance and feeds it frames in arrival order.

A bus cycle starts with the master's 0x41 frame (PROTOCOL_NOTES §3). That frame closes
the previous cycle and a snapshot is emitted. If no 0x41 arrives for `cycle_timeout`
seconds (a single unit, or the master stopped polling), tick() closes the cycle on time
instead. Frames missing from a cycle keep their last value and report their age.
"""

from __future__ import annotations

from collections import Counter, deque

from .decode import decode, unit_index
from .protocol import ADDR_MASTER, Frame

STATUS_KINDS = ("water", "gas")


class StateModel:
    def __init__(self, history_seconds: float = 300, frame_buffer: int = 600,
                 cycle_timeout: float = 1.5) -> None:
        self.history_seconds = history_seconds
        self.cycle_timeout = cycle_timeout
        self.history: deque[dict] = deque()
        self.frames: deque[Frame] = deque(maxlen=frame_buffer)

        self.latest: dict[tuple[int, str], tuple[float, dict]] = {}   # (unit, kind) -> (ts, decoded)
        self.cascade: tuple[float, dict] | None = None

        self.seq = 0
        self.cycle_start: float | None = None
        self.prev_cycle_start: float | None = None
        self.pending = 0                   # frames seen since cycle_start
        self.last_frame_ts: float | None = None

        self.frames_total = 0
        self.checksum_fail = 0
        self.seed_unexpected = 0
        self.per_stream: Counter[str] = Counter()
        self.last_seen: dict[str, float] = {}

    # ------------------------------------------------------------------ input

    def ingest(self, frame: Frame) -> dict | None:
        """Add one frame. Returns a snapshot if this frame closed a cycle."""
        snap = self.tick(frame.ts)

        self.frames.append(frame)
        self.frames_total += 1
        self.per_stream[frame.stream] += 1
        self.last_seen[frame.stream] = frame.ts
        self.last_frame_ts = frame.ts
        if not frame.checksum_ok:
            self.checksum_fail += 1
            return snap
        if not frame.seed_expected:
            self.seed_unexpected += 1

        if frame.kind == "cascade" and frame.src == ADDR_MASTER:
            if self.cycle_start is not None and self.pending:
                snap = self._close(frame.ts)
            self.prev_cycle_start, self.cycle_start = self.cycle_start, frame.ts
        elif self.cycle_start is None:
            self.cycle_start = frame.ts

        self.pending += 1
        d = decode(frame)
        if d is not None:
            if frame.kind in STATUS_KINDS:
                self.latest[(unit_index(frame), frame.kind)] = (frame.ts, d)
            elif frame.kind == "cascade":
                self.cascade = (frame.ts, d)
        return snap

    def tick(self, now: float) -> dict | None:
        """Close the current cycle if its 0x41 successor is overdue."""
        if self.cycle_start is not None and self.pending and now - self.cycle_start > self.cycle_timeout:
            snap = self._close(now)
            self.prev_cycle_start, self.cycle_start = self.cycle_start, now
            return snap
        return None

    # ----------------------------------------------------------------- output

    def _close(self, now: float) -> dict:
        self.seq += 1
        snap = self.snapshot(now)
        self.pending = 0
        self.history.append(snap)
        cutoff = now - self.history_seconds
        while self.history and self.history[0]["ts"] < cutoff:
            self.history.popleft()
        return snap

    def snapshot(self, now: float | None = None) -> dict:
        """Current merged state; `now` (default: last frame time) is used for ages."""
        if now is None:
            now = self.last_frame_ts or 0.0
        units: dict[str, dict] = {}
        for (unit, kind), (ts, d) in sorted(self.latest.items()):
            u = units.setdefault(str(unit), {"role": "master" if unit == 0 else "slave",
                                             "age_s": {}})
            u.update(d)
            u["age_s"][kind] = round(now - ts, 3)
        cascade = None
        if self.cascade is not None:
            ts, d = self.cascade
            cascade = dict(d, age_s=round(now - ts, 3))
        return {
            "type": "cycle",
            "ts": round(now, 3),
            "seq": self.seq,
            "units": units,
            "cascade": cascade,
            "bus": self.bus_stats(),
        }

    def bus_stats(self) -> dict:
        cycle_ms = None
        if self.cycle_start is not None and self.prev_cycle_start is not None:
            cycle_ms = round((self.cycle_start - self.prev_cycle_start) * 1000)
        return {
            "frames": self.frames_total,
            "checksum_fail": self.checksum_fail,
            "seed_unexpected": self.seed_unexpected,
            "cycle_ms": cycle_ms,
            "streams": dict(self.per_stream),
        }

    def history_since(self, seconds: float) -> list[dict]:
        if not self.history:
            return []
        cutoff = self.history[-1]["ts"] - seconds
        return [s for s in self.history if s["ts"] >= cutoff]
