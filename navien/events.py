"""Recorder: events and hourly rollups from cycle snapshots. PLAN.md §1.10.

Pure, like StateModel: no I/O and no clock of its own (cycle timestamps only), so the live
service and `backfill` produce identical points from the same bus data.

Each event type is a `Tracker` over a per-cycle condition, debounced: a run must hold for
`debounce_s` before it counts (shorter blips are discarded), and must be off for
`debounce_s` before it ends (shorter dropouts do not split it). Burns start on their first
cycle and only the end is debounced. An event is written when it ends, timestamped at its
start, so re-running a backfill overwrites rather than duplicates.

Gaps: a jump of more than GAP_S between cycles (service restart, adapter unplugged, a
missing raw log) ends every open event at its last cycle with `truncated=true`. After a gap,
and at the first cycle, a condition that is already on is ignored until it goes off: its
start is unknown.

Hourly rollups per unit ("0", "1") and "all": per-cycle integrals land in the UTC hour of
the cycle, event counts in the hour the event ended. An hour is written once it has been
over for longer than the debounce (so events ending in its last seconds still count), and
whatever is open at `finish()`. Timestamp = hour start, so a later full-hour backfill
overwrites a partial hour.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from .config import EventsCfg
from .influx import MODE_DHW, MODE_RECIRC, SCHEMA, line

GAP_S = 10.0          # longer jump between cycles = data gap
MAX_DT_S = 2.0        # integration step cap (cycles are 1 s)
LONG_DRAW_S = 120.0   # draws this long give the cold-supply estimate min_supply_c
HOUR_GRACE_S = 5.0    # an hour is written debounce_s + this after it ends

# water[10] stages from startup to the end of combustion (PROTOCOL_NOTES §5)
BURN_STAGES = {0x20, 0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x33, 0x34}

HOURLY_FIELDS = SCHEMA["navien_hourly"][1]


def _num(v, default=0.0) -> float:
    return default if v is None else float(v)


# ------------------------------------------------------------------- events

class Acc:
    """Accumulates one event. `add` is called for every cycle the condition holds."""

    def __init__(self, t: float) -> None:
        self.start = self.last = t
        self.active_s = 0.0
        self.truncated = False            # ended by a data gap, not by the condition

    def add(self, t: float, dt: float, ctx) -> None:
        self.last = t
        self.active_s += dt

    def fields(self) -> dict:
        return {"duration_s": round(self.active_s, 1)}


class Draw(Acc):
    """ctx: list of the unit dicts in DHW demand this cycle."""

    def __init__(self, t: float, hot_band_c: float) -> None:
        super().__init__(t)
        self.hot_band_c = hot_band_c
        self.volume_l = self.gas_kcal = self.peak = self.heat = 0.0
        self.min_inlet = math.inf
        self.time_to_hot: float | None = None
        self.units_max = 0

    def add(self, t, dt, units) -> None:
        super().add(t, dt, units)
        flow = sum(_num(u.get("flow_lpm")) for u in units)
        self.volume_l += flow * dt / 60
        self.gas_kcal += sum(_num(u.get("gas_kcalh")) for u in units) * dt / 3600
        self.peak = max(self.peak, flow)
        self.units_max = max(self.units_max, len(units))
        if flow <= 0:
            return
        outlet = sum(_num(u.get("outlet_c")) * _num(u.get("flow_lpm")) for u in units) / flow
        self.heat += outlet * flow * dt / 60
        for u in units:
            if _num(u.get("flow_lpm")) > 0 and u.get("inlet_c") is not None:
                self.min_inlet = min(self.min_inlet, u["inlet_c"])
        target = max(_num(u.get("set_c")) for u in units) - self.hot_band_c
        if self.time_to_hot is None and outlet >= target:
            self.time_to_hot = round(t - self.start, 1)

    def fields(self) -> dict:
        return dict(super().fields(), volume_l=round(self.volume_l, 2),
                    gas_kcal=round(self.gas_kcal, 1), peak_flow_lpm=self.peak,
                    mean_outlet_c=round(self.heat / self.volume_l, 2) if self.volume_l else None,
                    min_inlet_c=self.min_inlet if self.min_inlet < math.inf else None,
                    time_to_hot_s=self.time_to_hot, units_max=self.units_max)


class Recirc(Acc):
    """ctx: the unit dict."""

    def __init__(self, t: float, since_last: float | None) -> None:
        super().__init__(t)
        self.since_last = since_last
        self.volume_l = self.gas_kcal = 0.0
        self.start_inlet = self.end_outlet = None

    def add(self, t, dt, u) -> None:
        super().add(t, dt, u)
        self.volume_l += _num(u.get("flow_lpm")) * dt / 60
        self.gas_kcal += _num(u.get("gas_kcalh")) * dt / 3600
        if self.start_inlet is None:
            self.start_inlet = u.get("inlet_c")
        self.end_outlet = u.get("outlet_c")

    def fields(self) -> dict:
        return dict(super().fields(), volume_l=round(self.volume_l, 2),
                    gas_kcal=round(self.gas_kcal, 1), start_inlet_c=self.start_inlet,
                    end_outlet_c=self.end_outlet,
                    since_last_s=None if self.since_last is None else round(self.since_last, 1))


class Burn(Acc):
    """ctx: the unit dict. One burn may hold several ignitions (flame off -> re-ignite
    within the debounce)."""

    def __init__(self, t: float, short_burn_s: float) -> None:
        super().__init__(t)
        self.short_burn_s = short_burn_s
        self.flame_s = 0.0
        self.ignition_s: float | None = None
        self.ignitions = 0
        self.flame_prev = False
        self.mode_s: Counter[int] = Counter()      # flame seconds per heating mode
        self.mode_start: int | None = None

    def add(self, t, dt, u) -> None:
        super().add(t, dt, u)
        flame = bool(u.get("flame"))
        if flame:
            self.flame_s += dt
            if not self.flame_prev:
                self.ignitions += 1
                if self.ignition_s is None:
                    self.ignition_s = t - self.start
        self.flame_prev = flame
        mode = u.get("heating_mode_code")
        if self.mode_start is None:
            self.mode_start = mode
        if flame and mode is not None:
            self.mode_s[mode] += dt

    def fields(self) -> dict:
        failed = self.ignitions == 0
        return dict(super().fields(), flame_s=round(self.flame_s, 1),
                    ignition_s=None if self.ignition_s is None else round(self.ignition_s, 1),
                    ignitions=self.ignitions,
                    mode_code=self.mode_s.most_common(1)[0][0] if self.mode_s else self.mode_start,
                    failed=failed, short=not failed and self.flame_s < self.short_burn_s)


class Stage2(Acc):
    """ctx: the master's unit dict (or {})."""

    def __init__(self, t: float, master: dict) -> None:
        super().__init__(t)
        self.flow = master.get("flow_lpm")
        self.capacity = master.get("capacity_pct")

    def fields(self) -> dict:
        return dict(super().fields(), trigger_flow_lpm=self.flow,
                    trigger_capacity_pct=self.capacity)


class Error(Acc):
    """ctx: the unit dict."""

    def __init__(self, t: float, u: dict) -> None:
        super().__init__(t)
        self.code = u.get("error_code")
        self.level = u.get("error_level")

    def fields(self) -> dict:
        return dict(super().fields(), code=self.code, level=self.level)


class Tracker:
    """Debounced runs of one boolean condition."""

    def __init__(self, type_: str, unit: str, factory, on_s: float, off_s: float) -> None:
        self.type, self.unit = type_, unit
        self.factory = factory            # (t, ctx) -> Acc
        self.on_s, self.off_s = on_s, off_s
        self.acc: Acc | None = None
        self.confirmed = False
        self.off_since: float | None = None
        self.unknown_since: float | None = None
        self.blocked = True               # start unknown: wait for the condition to go off

    def step(self, t: float, dt: float, active: bool | None, ctx) -> Acc | None:
        """Feed one cycle; active=None means no fresh data for this condition.
        Returns the finished event, if this cycle ended one."""
        if active is None:
            if self.unknown_since is None:
                self.unknown_since = t
            return self.cut() if t - self.unknown_since > GAP_S else None
        self.unknown_since = None
        if self.acc is None:
            if not active:
                self.blocked = False
            elif not self.blocked:
                self.acc = self.factory(t, ctx)
                self.acc.add(t, dt, ctx)
                self.confirmed = self.on_s <= 0
                self.off_since = None
            return None
        if active:
            self.off_since = None
            self.acc.add(t, dt, ctx)
            if t - self.acc.start >= self.on_s:
                self.confirmed = True
            return None
        if not self.confirmed:            # blip: never long enough to count
            self.acc = None
            return None
        if self.off_since is None:
            self.off_since = t
        if t - self.off_since >= self.off_s:
            done, self.acc = self.acc, None
            return done
        return None

    def cut(self) -> Acc | None:
        """Data gap: return the open event (if confirmed) and wait for a clean start."""
        done = self.acc if self.acc is not None and self.confirmed else None
        if done is not None:
            done.truncated = True
        self.acc = None
        self.blocked = True
        return done


# ------------------------------------------------------------------- hourly

@dataclass
class Bucket:
    hour: int
    v: dict

    @classmethod
    def new(cls, hour: int) -> "Bucket":
        return cls(hour, {k: (0 if t is int else 0.0) for k, t in HOURLY_FIELDS.items()}
                   | {"min_supply_c": None})


# ------------------------------------------------------------------ recorder

class Recorder:
    def __init__(self, cfg: EventsCfg | None = None, stale_s: float = 3.0) -> None:
        self.cfg = cfg or EventsCfg()
        self.stale_s = stale_s
        c = self.cfg
        self.last_recirc_start: dict[str, float] = {}
        self.trackers: list[Tracker] = [
            Tracker("draw", "all", lambda t, ctx: Draw(t, c.hot_band_c), c.debounce_s, c.debounce_s),
            Tracker("stage2", "all", Stage2, c.debounce_s, c.debounce_s),
        ]
        for unit in ("0", "1"):
            self.trackers += [
                Tracker("draw_unit", unit, lambda t, ctx: Draw(t, c.hot_band_c), c.debounce_s, c.debounce_s),
                Tracker("recirc", unit, self._recirc_factory(unit), c.debounce_s, c.debounce_s),
                Tracker("burn", unit, lambda t, ctx: Burn(t, c.short_burn_s), 0, c.debounce_s),
                Tracker("error", unit, Error, c.debounce_s, c.debounce_s),
            ]
        self.prev_ts: float | None = None
        self.buckets: dict[tuple[int, str], Bucket] = {}   # (hour, unit)
        self.closed_before = -1                             # hours before this are written
        self.counts: Counter[str] = Counter()               # events written, by type

    def _recirc_factory(self, unit: str):
        def make(t, ctx):
            prev = self.last_recirc_start.get(unit)        # set when a run is written
            return Recirc(t, None if prev is None else t - prev)
        return make

    # ----------------------------------------------------------------- input

    def feed(self, snap: dict) -> list[tuple[str, str]]:
        t = snap["ts"]
        out: list[tuple[str, str]] = []
        if self.prev_ts is not None and t - self.prev_ts > GAP_S:
            for tr in self.trackers:
                acc = tr.cut()
                if acc is not None:
                    out += self._event(tr, acc)
            self.prev_ts = None
        dt = 0.0 if self.prev_ts is None else min(max(t - self.prev_ts, 0.0), MAX_DT_S)
        self.prev_ts = t

        units = {k: u for k, u in snap.get("units", {}).items()
                 if max(u.get("age_s", {}).values(), default=0) <= self.stale_s}
        c = snap.get("cascade")
        cascade = c if c and c.get("age_s", 0) <= self.stale_s else {}
        dhw = [u for _, u in sorted(units.items()) if u.get("heating_mode_code") == MODE_DHW]

        for tr in self.trackers:
            u = units.get(tr.unit, {})
            mode = u.get("heating_mode_code")
            if tr.type == "draw":
                active, ctx = (bool(dhw) if units else None), dhw
            elif tr.type == "stage2":
                active, ctx = (bool(cascade.get("stage2")) if cascade else None), units.get("0", {})
            elif not u:
                active, ctx = None, u
            elif tr.type == "draw_unit":
                active, ctx = mode == MODE_DHW, [u]
            elif tr.type == "recirc":
                active, ctx = mode == MODE_RECIRC, u
            elif tr.type == "burn":
                active, ctx = u.get("stage_code") in BURN_STAGES or bool(u.get("flame")), u
            else:
                active, ctx = bool(u.get("error_code")), u
            acc = tr.step(t, dt, active, ctx)
            if acc is not None:
                out += self._event(tr, acc)

        self._integrate(t, dt, units, cascade, dhw)
        out += self._emit_hours(t)
        return out

    def finish(self) -> list[tuple[str, str]]:
        """Shutdown / end of backfill: open events (truncated) and every open hour."""
        out: list[tuple[str, str]] = []
        for tr in self.trackers:
            acc = tr.cut()
            if acc is not None:
                out += self._event(tr, acc)
        out += self._emit_hours(math.inf)
        self.prev_ts = None
        return out

    # ---------------------------------------------------------------- events

    def _event(self, tr: Tracker, acc: Acc) -> list[tuple[str, str]]:
        self.counts[tr.type] += 1
        if tr.type == "recirc":
            self.last_recirc_start[tr.unit] = acc.start
        f = acc.fields()
        f["truncated"] = acc.truncated
        self._count(tr, acc, f)
        ln = line("navien_event", {"type": tr.type, "unit": tr.unit}, f, round(acc.start * 1000))
        return [("rollup", ln)] if ln else []

    def _count(self, tr: Tracker, acc: Acc, f: dict) -> None:
        hour = int(acc.last // 3600)
        keys = {"draw": ["all"], "draw_unit": [tr.unit], "stage2": []}.get(tr.type, [tr.unit, "all"])
        for key in keys:
            b = self._bucket(hour, key)
            v = b.v
            if tr.type in ("draw", "draw_unit"):
                v["draws"] += 1
                if f["duration_s"] >= LONG_DRAW_S and f.get("min_inlet_c") is not None:
                    v["min_supply_c"] = min(f["min_inlet_c"], v["min_supply_c"] or math.inf)
            elif tr.type == "recirc":
                v["recirc_runs"] += 1
            elif tr.type == "burn":
                v["burns"] += 1
                v["ignitions"] += f["ignitions"]
                v["short_burns"] += f["short"]
                v["failed_ignitions"] += f["failed"]

    # ---------------------------------------------------------------- hourly

    def _bucket(self, hour: int, unit: str) -> Bucket:
        # an event ending in an hour that is already written counts in the oldest open one
        hour = max(hour, self.closed_before)
        key = (hour, unit)
        if key not in self.buckets:
            self.buckets[key] = Bucket.new(hour)
        return self.buckets[key]

    def _integrate(self, t: float, dt: float, units: dict, cascade: dict, dhw: list) -> None:
        if dt <= 0:
            return
        hour = int(t // 3600)
        a = self._bucket(hour, "all").v
        a["coverage_s"] += dt
        if cascade.get("stage2"):
            a["stage2_s"] += dt
        a["peak_flow_lpm"] = max(a["peak_flow_lpm"], sum(_num(u.get("flow_lpm")) for u in dhw))
        for unit, u in units.items():
            v = self._bucket(hour, unit).v
            mode = u.get("heating_mode_code")
            flow, gas = _num(u.get("flow_lpm")), _num(u.get("gas_kcalh"))
            litres, kcal = flow * dt / 60, gas * dt / 3600
            water = "water_dhw_l" if mode == MODE_DHW else "water_recirc_l" if mode == MODE_RECIRC else None
            gas_key = ("gas_dhw_kcal" if mode == MODE_DHW else
                       "gas_recirc_kcal" if mode == MODE_RECIRC else "gas_other_kcal")
            for b in (v, a):
                if water:
                    b[water] += litres
                b[gas_key] += kcal
                if u.get("flame"):
                    b["flame_s"] += dt
            v["coverage_s"] += dt
            if mode == MODE_DHW:
                v["peak_flow_lpm"] = max(v["peak_flow_lpm"], flow)

    def _emit_hours(self, now: float) -> list[tuple[str, str]]:
        out = []
        for (hour, unit) in sorted(self.buckets):
            if (hour + 1) * 3600 + self.cfg.debounce_s + HOUR_GRACE_S > now:
                continue
            b = self.buckets.pop((hour, unit))
            self.closed_before = max(self.closed_before, hour + 1)
            if b.v["coverage_s"] <= 0 and unit != "all":
                continue
            f = {k: (round(x, 3) if isinstance(x, float) else x) for k, x in b.v.items()}
            ln = line("navien_hourly", {"unit": unit}, f, hour * 3600 * 1000)
            if ln:
                out.append(("rollup", ln))
        return out
