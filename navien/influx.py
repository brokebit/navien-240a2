"""InfluxDB 1.x writer. PLAN.md §1.10.

The writer is a Hub subscriber on "cycle". `Points` turns each cycle snapshot into
line-protocol lines (pure, no I/O); `InfluxWriter` queues them and POSTs batches to
`/write`. Everything runs on the event loop in one task, so the queues need no locks.

Each cycle also goes through an events.Recorder (navien_event, navien_hourly). Two queues,
one per retention policy: "samples" (navien_unit, navien_cascade, every cycle) and
"rollup" (everything else). Over `max_queue` points the oldest *samples* are
dropped first. Failures:
  - network error, timeout, 5xx, 429, and 401/403/404 (auth, database or RP missing:
    fixable on the server) -> keep the batch, back off 1 s .. 60 s, retry;
  - any other 4xx (bad line, type conflict, points beyond the retention policy) -> the
    batch is not retryable: drop it, count the points as rejected, log the error.
Influx overwrites a point with the same measurement, tags and timestamp, so a retried
batch that had in fact landed does no harm.

SCHEMA is the single list of what we write; the Grafana dashboards are tested against it.
Field types never change once written (Influx rejects a float field written as int), so
every value is coerced to its schema type here.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from itertools import islice
from typing import Callable

import httpx

from .config import EventsCfg, InfluxCfg

log = logging.getLogger(__name__)

BATCH_LINES = 5000
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
RETRY_STATUS = {401, 403, 404, 429}

# measurement -> (tags, {field: type}). Types: float, int, bool, str.
SCHEMA: dict[str, tuple[tuple[str, ...], dict[str, type]]] = {
    "navien_unit": (("unit", "role"), {
        "set_c": float, "outlet_c": float, "inlet_c": float, "flow_lpm": float,
        "capacity_pct": float, "gas_kcalh": int, "heat_capacity_pct": float,
        "stage_code": int, "mode_code": int, "flame": bool, "power_on": bool,
        "recirc_running": bool, "error_code": int, "error_level": int,
        # mode-split copies: the value while in that heating mode, else 0 (PLAN §1.10)
        "flow_dhw_lpm": float, "flow_recirc_lpm": float,
        "gas_dhw_kcalh": int, "gas_recirc_kcalh": int,
    }),
    "navien_cascade": ((), {
        "units_in_demand": int, "flow_lpm": float, "capacity_pct": float, "set_c": float,
        "inlet_c": float, "outlet_c": float, "status": int, "demand": bool, "flame": bool,
        "recirc": bool, "stage2": bool, "toggle_25": bool, "x26": int, "x27": int, "x28": int,
    }),
    "navien_counters": (("unit", "role"), {
        "gas_total_m3": float, "days_since_install": int, "dhw_uses": int,
        "operating_hours": int, "dhw_hours": int, "device_type": str,
        "controller_version": str, "panel_version": str,
    }),
    "navien_bus": ((), {
        "frames": int, "checksum_fail": int, "seed_unexpected": int, "cycle_ms": int,
        "resyncs": int, "reader_bytes": int, "rawlog_dropped_chunks": int, "ws_clients": int,
        "influx_queued": int, "influx_dropped": int, "influx_rejected": int,
    }),
    # events.py: one point per event, at its start; fields by type in PLAN §1.10
    "navien_event": (("type", "unit"), {
        "duration_s": float, "truncated": bool,
        "volume_l": float, "gas_kcal": float, "peak_flow_lpm": float, "mean_outlet_c": float,
        "min_inlet_c": float, "time_to_hot_s": float, "units_max": int,          # draw, draw_unit
        "start_inlet_c": float, "end_outlet_c": float, "since_last_s": float,    # recirc
        "flame_s": float, "ignition_s": float, "ignitions": int, "mode_code": int,
        "short": bool, "failed": bool,                                           # burn
        "trigger_flow_lpm": float, "trigger_capacity_pct": float,                # stage2
        "code": int, "level": int,                                               # error
    }),
    # events.py: one point per UTC hour and unit ("0", "1", "all"), at the hour start
    "navien_hourly": (("unit",), {
        "water_dhw_l": float, "water_recirc_l": float,
        "gas_dhw_kcal": float, "gas_recirc_kcal": float, "gas_other_kcal": float,
        "draws": int, "recirc_runs": int, "burns": int, "ignitions": int, "short_burns": int,
        "failed_ignitions": int, "flame_s": float, "stage2_s": float, "peak_flow_lpm": float,
        "min_supply_c": float, "coverage_s": float,
    }),
}
SAMPLE_MEASUREMENTS = ("navien_unit", "navien_cascade")

MODE_DHW = 0x20
MODE_RECIRC = 0x08

# Unit fields that are not decoded fields of the same name.
UNIT_SOURCE = {"mode_code": "heating_mode_code"}


# ---------------------------------------------------------------- line protocol

def _escape(s: str, chars: str) -> str:
    for c in chars:
        s = s.replace(c, "\\" + c)
    return s


def format_value(value, typ: type) -> str | None:
    """Line-protocol field value of the schema type, or None if it cannot be written."""
    if value is None:
        return None
    if typ is bool:
        return "true" if value else "false"
    if typ is int:
        return f"{int(value)}i"
    if typ is float:
        v = float(value)
        return repr(v) if math.isfinite(v) else None
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def line(measurement: str, tags: dict, fields: dict, ts_ms: int) -> str | None:
    """One line, fields coerced to SCHEMA types. None if no field has a value."""
    types = SCHEMA[measurement][1]
    parts = []
    for name, value in fields.items():
        v = format_value(value, types[name])
        if v is not None:
            parts.append(f"{_escape(name, ',= ')}={v}")
    if not parts:
        return None
    head = _escape(measurement, ", ")
    for k, v in sorted(tags.items()):
        head += f",{_escape(k, ',= ')}={_escape(str(v), ',= ')}"
    return f"{head} {','.join(parts)} {ts_ms}"


# ----------------------------------------------------------- snapshot -> points

def unit_fields(u: dict) -> dict:
    out = {name: u.get(UNIT_SOURCE.get(name, name)) for name in SCHEMA["navien_unit"][1]}
    mode = u.get("heating_mode_code")
    flow = u.get("flow_lpm") or 0.0
    gas = u.get("gas_kcalh") or 0
    out["flow_dhw_lpm"] = flow if mode == MODE_DHW else 0.0
    out["flow_recirc_lpm"] = flow if mode == MODE_RECIRC else 0.0
    out["gas_dhw_kcalh"] = gas if mode == MODE_DHW else 0
    out["gas_recirc_kcalh"] = gas if mode == MODE_RECIRC else 0
    return out


class Points:
    """Turns cycle snapshots into (queue, line) pairs. Pure: no I/O, no wall clock.

    Samples go out every cycle (or at most every `sample_every_s`); counters and bus
    stats once per minute. A unit or cascade frame older than `stale_s` is not written,
    so a silent unit does not repeat its last values forever.
    """

    def __init__(self, sample_every_s: float = 1.0, stale_s: float = 3.0, bus: bool = True) -> None:
        self.sample_every_s = sample_every_s
        self.stale_s = stale_s
        self.bus = bus            # navien_bus describes this process; backfill leaves it out
        self._sample_bucket: int | None = None
        self._minute: int | None = None

    def from_cycle(self, snap: dict, bus_extra: dict | None = None) -> list[tuple[str, str]]:
        ts = snap["ts"]
        ts_ms = round(ts * 1000)
        out: list[tuple[str, str]] = []

        bucket = int(ts // self.sample_every_s) if self.sample_every_s > 1 else None
        if bucket is None or bucket != self._sample_bucket:
            self._sample_bucket = bucket
            for unit, u in sorted(snap.get("units", {}).items()):
                if max(u.get("age_s", {}).values(), default=0) > self.stale_s:
                    continue
                tags = {"unit": unit, "role": u.get("role", "")}
                out.append(("samples", line("navien_unit", tags, unit_fields(u), ts_ms)))
            c = snap.get("cascade")
            if c and c.get("age_s", 0) <= self.stale_s:
                fields = {name: c.get(name) for name in SCHEMA["navien_cascade"][1]}
                out.append(("samples", line("navien_cascade", {}, fields, ts_ms)))

        minute = int(ts // 60)
        if minute != self._minute:
            self._minute = minute
            for unit, u in sorted(snap.get("units", {}).items()):
                if u.get("age_s", {}).get("gas", math.inf) > self.stale_s:
                    continue
                fields = {name: u.get(name) for name in SCHEMA["navien_counters"][1]}
                out.append(("rollup", line("navien_counters",
                                           {"unit": unit, "role": u.get("role", "")}, fields, ts_ms)))
            if self.bus:
                bus = dict(snap.get("bus", {}))
                bus.update(bus_extra or {})
                fields = {name: bus.get(name) for name in SCHEMA["navien_bus"][1]}
                out.append(("rollup", line("navien_bus", {}, fields, ts_ms)))

        return [(q, ln) for q, ln in out if ln is not None]


# ------------------------------------------------------------------- writer

class InfluxWriter:
    def __init__(self, cfg: InfluxCfg, stale_s: float = 3.0,
                 bus_extra: Callable[[], dict] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 events: EventsCfg | None = None, bus: bool = True) -> None:
        from .events import Recorder          # events imports this module
        self.cfg = cfg
        self.points = Points(cfg.sample_every_s, stale_s, bus=bus)
        self.recorder = Recorder(events, stale_s)
        self.bus_extra = bus_extra
        self.queues: dict[str, deque[str]] = {"rollup": deque(), "samples": deque()}
        self.rp = {"samples": cfg.retention_policy, "rollup": cfg.rollup_retention_policy}
        auth = (cfg.username, cfg.password) if cfg.username else None
        self.client = httpx.AsyncClient(base_url=cfg.url.rstrip("/"), auth=auth,
                                        timeout=cfg.timeout_s, transport=transport)
        self.written = 0
        self.dropped = 0          # queue overflow
        self.rejected = 0         # non-retryable 4xx
        self.batches = 0
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.failing_since: float | None = None   # first retryable failure since the last success
        self.last_drop_at: float | None = None
        self.last_rejected_at: float | None = None
        self.backoff_s = 0.0
        self.retry_at = 0.0       # monotonic
        self.sub = None

    # ------------------------------------------------------------- queueing

    @property
    def queued(self) -> int:
        return sum(len(q) for q in self.queues.values())

    def add(self, items: list[tuple[str, str]]) -> None:
        for queue, ln in items:
            self.queues[queue].append(ln)
        excess = self.queued - self.cfg.max_queue
        for queue in ("samples", "rollup"):
            while excess > 0 and self.queues[queue]:
                self.queues[queue].popleft()
                self.dropped += 1
                self.last_drop_at = time.time()
                excess -= 1

    def ingest(self, snap: dict) -> None:
        extra = dict(self.bus_extra() if self.bus_extra else {},
                     influx_queued=self.queued, influx_dropped=self.dropped,
                     influx_rejected=self.rejected)
        self.add(self.points.from_cycle(snap, extra) + self.recorder.feed(snap))

    # -------------------------------------------------------------- sending

    async def flush(self) -> bool:
        """Send everything queued, batch by batch. False if it stopped on a retryable error."""
        for queue in ("rollup", "samples"):
            q = self.queues[queue]
            while q:
                batch = list(islice(q, BATCH_LINES))
                outcome = await self._post(queue, batch)
                if outcome == "retry":
                    return False
                for _ in batch:
                    q.popleft()
        return True

    async def _post(self, queue: str, batch: list[str]) -> str:
        params = {"db": self.cfg.database, "precision": "ms"}
        if self.rp[queue]:
            params["rp"] = self.rp[queue]
        try:
            r = await self.client.post("/write", params=params, content="\n".join(batch))
        except httpx.HTTPError as e:
            return self._failed(f"{type(e).__name__}: {e}")
        if r.status_code == 204:
            self.written += len(batch)
            self.batches += 1
            self.last_ok = time.time()
            self.failing_since = None
            self.backoff_s = 0.0
            return "ok"
        msg = f"HTTP {r.status_code}: {r.text.strip()[:300]}"
        if r.status_code >= 500 or r.status_code in RETRY_STATUS:
            return self._failed(msg)
        # Not retryable. Influx may have written part of it ("partial write: … dropped=N").
        self.rejected += len(batch)
        self.last_error, self.last_error_at = msg, time.time()
        self.last_rejected_at = self.last_error_at
        log.warning("influx rejected %d %s points: %s", len(batch), queue, msg)
        return "rejected"

    def _failed(self, msg: str) -> str:
        self.backoff_s = min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, self.backoff_s * 2))
        self.retry_at = time.monotonic() + self.backoff_s
        if msg != self.last_error:
            log.warning("influx write failed (%s); retrying in %.0f s", msg, self.backoff_s)
        self.last_error, self.last_error_at = msg, time.time()
        if self.failing_since is None:
            self.failing_since = self.last_error_at
        return "retry"

    # ------------------------------------------------------------ lifecycle

    async def run(self, sub) -> None:
        """Consume cycles from a Hub subscription; flush every batch_seconds."""
        self.sub = sub
        next_flush = time.monotonic() + self.cfg.batch_seconds
        while True:
            timeout = max(0.0, next_flush - time.monotonic())
            try:
                _topic, snap = await asyncio.wait_for(sub.get(), timeout)
                try:
                    self.ingest(snap)
                except Exception:                    # never let one bad snapshot stop writing
                    log.exception("influx: could not convert cycle %s", snap.get("seq"))
            except TimeoutError:
                pass
            now = time.monotonic()
            due = now >= next_flush or self.queued >= BATCH_LINES
            if due and now >= self.retry_at:
                await self.flush()
                next_flush = time.monotonic() + self.cfg.batch_seconds
            elif due:
                next_flush = self.retry_at

    async def close(self) -> None:
        """Convert what the subscription still holds and try one last flush."""
        if self.sub is not None:
            while not self.sub.queue.empty():
                _topic, snap = self.sub.queue.get_nowait()
                self.ingest(snap)
        self.add(self.recorder.finish())          # open events (truncated), the partial hour
        if self.queued:
            try:
                await asyncio.wait_for(self.flush(), self.cfg.timeout_s)
            except TimeoutError:
                pass
            if self.queued:
                log.warning("influx: %d points not written at shutdown", self.queued)
        await self.client.aclose()

    def health(self) -> dict:
        return {
            "url": self.cfg.url,
            "database": self.cfg.database,
            "now": time.time(),                  # the ages below are relative to this clock
            "batch_seconds": self.cfg.batch_seconds,
            "max_queue": self.cfg.max_queue,
            "queued": {k: len(q) for k, q in self.queues.items()},
            "written": self.written,
            "batches": self.batches,
            "dropped": self.dropped,
            "rejected": self.rejected,
            "events": dict(self.recorder.counts),
            "subscription_dropped": self.sub.dropped if self.sub else 0,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "failing_since": self.failing_since,
            "last_drop_at": self.last_drop_at,
            "last_rejected_at": self.last_rejected_at,
            "retry_in_s": round(max(0.0, self.retry_at - time.monotonic()), 1) if self.backoff_s else 0,
        }
