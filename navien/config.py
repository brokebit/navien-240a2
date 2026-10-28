"""Configuration: TOML file (every key optional) with dataclass defaults. PLAN.md §1.8."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path


@dataclass
class SerialCfg:
    port: str = "/dev/navien"
    baud: int = 19200


@dataclass
class HttpCfg:
    host: str = "0.0.0.0"
    port: int = 8000


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
    retention_policy: str = ""
    batch_seconds: float = 10.0
    max_queue: int = 3600


@dataclass
class Config:
    serial: SerialCfg = field(default_factory=SerialCfg)
    http: HttpCfg = field(default_factory=HttpCfg)
    state: StateCfg = field(default_factory=StateCfg)
    rawlog: RawlogCfg = field(default_factory=RawlogCfg)
    influx: InfluxCfg = field(default_factory=InfluxCfg)


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
    return cfg
