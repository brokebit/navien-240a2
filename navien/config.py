"""Configuration: TOML file (every key optional) with dataclass defaults. PLAN.md §1.8."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path


@dataclass
class SerialCfg:
    port: str = "/dev/navien"
    baud: int = 19200
    stall_reopen_s: float = 10.0       # reopen the port after this long without a byte; 0 = off
    stall_reopen_max_s: float = 60.0   # while it stays silent, the wait doubles up to this


@dataclass
class HttpCfg:
    host: str = "0.0.0.0"
    port: int = 8000
    ws_queue: int = 256                # per-client outbound messages before drop-oldest
    ws_send_timeout_s: float = 30.0    # a client that accepts nothing this long is dropped


@dataclass
class StateCfg:
    history_seconds: int = 300
    frame_buffer: int = 600
    bus_timeout_s: float = 3.0


@dataclass
class RawlogCfg:
    dir: str = "raw"
    rotate: str = "hourly"          # hourly | daily
    compress: bool = True
    keep_days: int = 60
    max_total_mb: int = 2048
    flush_seconds: float = 5.0


@dataclass
class InfluxCfg:
    enabled: bool = False
    url: str = "http://localhost:8086"
    database: str = "navien"
    username: str = ""
    password: str = ""
    retention_policy: str = "samples_90d"      # samples (navien_unit, navien_cascade); "" = db default
    rollup_retention_policy: str = "forever"   # everything else; "" = db default
    batch_seconds: float = 10.0
    max_queue: int = 50000              # points held while Influx is unreachable
    timeout_s: float = 10.0
    sample_every_s: float = 1.0         # per-cycle samples at most this often


@dataclass
class EventsCfg:
    debounce_s: float = 3.0             # a run must hold / stay off this long (PLAN §1.10)
    short_burn_s: float = 30.0          # burns with less flame time count as short
    hot_band_c: float = 1.0             # "hot" = outlet within this of the setpoint


@dataclass
class Config:
    serial: SerialCfg = field(default_factory=SerialCfg)
    http: HttpCfg = field(default_factory=HttpCfg)
    state: StateCfg = field(default_factory=StateCfg)
    rawlog: RawlogCfg = field(default_factory=RawlogCfg)
    influx: InfluxCfg = field(default_factory=InfluxCfg)
    events: EventsCfg = field(default_factory=EventsCfg)


def _apply(obj, data: dict, where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key {where}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config section {where}{key} must be a table")
            _apply(current, value, f"{where}{key}.")
        else:
            if isinstance(current, float) and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if type(value) is not type(current):
                raise ValueError(f"config key {where}{key} must be {type(current).__name__}, "
                                 f"got {type(value).__name__}")
            setattr(obj, key, value)


def load(path: str | Path | None = None) -> Config:
    cfg = Config()
    if path is not None:
        with open(path, "rb") as f:
            _apply(cfg, tomllib.load(f), "")
    if cfg.rawlog.rotate not in ("hourly", "daily"):
        raise ValueError("rawlog.rotate must be 'hourly' or 'daily'")
    if cfg.influx.batch_seconds <= 0 or cfg.influx.max_queue < 1 or cfg.influx.timeout_s <= 0:
        raise ValueError("influx.batch_seconds, max_queue and timeout_s must be positive")
    if cfg.events.debounce_s < 0 or cfg.events.short_burn_s < 0 or cfg.events.hot_band_c < 0:
        raise ValueError("events.debounce_s, short_burn_s and hot_band_c must not be negative")
    return cfg
