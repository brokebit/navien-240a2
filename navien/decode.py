"""Field tables and decoders for the frame kinds on this cascade.

The tables are the single source of truth for the byte map in code; keep them in step
with PROTOCOL_NOTES.md §4/§5. Indices are frame indices (0 = 0xF7).

confidence:
  measured    confirmed on this installation's captures
  upstream    from htumanyan/navien, consistent with our data but not independently proven
  unverified  upstream meaning, values here look implausible (PROTOCOL_NOTES §6 q3)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .protocol import Frame, HDR_SIZE

# Operating state / burner stage (water[10]); upstream names, PROTOCOL_NOTES §5.
STAGES = {
    0x14: "standby", 0x15: "demand", 0x20: "startup", 0x28: "pre_purge_1",
    0x29: "pre_purge_2", 0x2A: "pre_ignition", 0x2B: "ignition", 0x2C: "flame_on",
    0x2D: "ramp_up", 0x33: "active_combustion", 0x34: "water_adjustment",
    0x3C: "flame_off", 0x46: "post_purge_1", 0x47: "post_purge_2",
    0x48: "shutting_down", 0x49: "dhw_wait",
}

# Heating mode (water[8]); an enum, not a bit field.
HEATING_MODES = {
    0x00: "idle", 0x08: "dhw_recirc", 0x10: "space_heating",
    0x20: "dhw_demand", 0x40: "dhw_indirect",
}

# Device type (gas[8]); upstream enum, after rudybrian/PyNavienSmartController.
DEVICE_TYPES = {
    0: "NO_DEVICE", 1: "NPE", 2: "NCB", 3: "NHB", 4: "CAS_NPE", 5: "CAS_NHB",
    6: "NFB", 7: "CAS_NFB", 8: "NFC", 9: "NPN", 10: "CAS_NPN", 11: "NPE2",
    12: "CAS_NPE2", 13: "NCB_H", 14: "NVW", 15: "CAS_NVW", 16: "NHB_H",
}


@dataclass(frozen=True)
class Field:
    name: str
    index: int
    type: str = "u8"          # u8 | u16 (little-endian) | bool | enum | version
    scale: float = 1.0        # numeric value = raw * scale
    mask: int = 0             # bool: raw & mask != 0
    enum: dict | None = None
    unit: str = ""
    confidence: str = "measured"
    note: str = ""

    @property
    def size(self) -> int:
        return 2 if self.type == "u16" else 1

    def extract(self, raw: bytes):
        v = raw[self.index] if self.size == 1 else raw[self.index] | raw[self.index + 1] << 8
        if self.type == "bool":
            return bool(v & self.mask)
        if self.type == "enum":
            return self.enum.get(v, f"0x{v:02X}")
        if self.type == "version":
            # decimal digits: 0x0C = 12 -> "1.2"
            s = f"{v:02d}"
            return f"{s[:-1]}.{s[-1]}"
        if self.scale != 1.0:
            return round(v * self.scale, 2)
        return v


F = Field

WATER_FIELDS = [
    F("heating_mode", 8, "enum", enum=HEATING_MODES, confidence="upstream",
      note="00 idle, 20 DHW demand measured; 08 recirc from upstream"),
    F("power_on", 9, "bool", mask=0x01, confidence="upstream"),
    F("no_hotbutton", 9, "bool", mask=0x20, confidence="upstream"),
    F("stage_code", 10),
    F("stage", 10, "enum", enum=STAGES),
    F("set_c", 11, scale=0.5, unit="°C"),
    F("outlet_c", 12, scale=0.5, unit="°C"),
    F("inlet_c", 13, scale=0.5, unit="°C"),
    F("error_code", 14, "u16", confidence="upstream", note="never non-zero here yet"),
    F("error_level", 16, confidence="upstream"),
    F("capacity_pct", 17, scale=0.5, unit="%"),
    F("flow_lpm", 18, scale=0.1, unit="L/min"),
    F("display_celsius", 24, "bool", mask=0x08),
    F("recirc_int_scheduled", 24, "bool", mask=0x01, confidence="upstream"),
    F("recirc_ext_scheduled", 24, "bool", mask=0x02, confidence="upstream"),
    F("flame", 27, "bool", mask=0x01),
    F("hotbutton_active", 33, "bool", mask=0x01, confidence="upstream"),
    F("recirc_scheduled_enabled", 33, "bool", mask=0x02, confidence="upstream"),
]

GAS_FIELDS = [
    F("device_type", 8, "enum", enum=DEVICE_TYPES, confidence="upstream"),
    F("controller_version", 10, "version", confidence="upstream"),
    F("panel_version", 12, "version", confidence="upstream"),
    F("gas_set_c", 14, scale=0.5, unit="°C"),
    F("gas_outlet_c", 15, scale=0.5, unit="°C"),
    F("gas_inlet_c", 16, scale=0.5, unit="°C"),
    F("heat_capacity_pct", 20, scale=0.5, unit="%",
      note="= gas_kcalh / 50 400 kcal/h rating; 0 during ignition"),
    F("display_f", 21, "bool", mask=0x01, confidence="upstream"),
    F("hotbutton_enabled", 21, "bool", mask=0x04, confidence="upstream"),
    F("gas_kcalh", 22, "u16", unit="kcal/h", note="raw = kcal/h (upstream's x10 is wrong here)"),
    F("gas_total_m3", 24, "u16", scale=0.1, unit="m³", confidence="unverified"),
    F("days_since_install", 28, "u16", unit="d", confidence="unverified"),
    F("dhw_uses", 30, "u16", confidence="unverified"),
    F("operating_hours", 36, "u16", unit="h", confidence="unverified"),
    F("dhw_hours", 38, "u16", unit="h", confidence="unverified"),
]

# Master -> slave 0x41 frame (PROTOCOL_NOTES §4.4): the master's own state, one cycle late.
CASCADE_FIELDS = [
    # Master -> slave, once per cycle: cascade-wide values, not the master's own state
    # (PROTOCOL_NOTES §4.3). Temperatures, capacity and flow are over the units in DHW demand.
    F("consumption", 8, "bool", mask=0x20),
    F("status", 9, note="bits: 01 power, 02 a burner lit, 04 DHW demand, 08 recirc"),
    F("demand", 9, "bool", mask=0x04),
    F("flame", 9, "bool", mask=0x02, note="any unit's burner, incl. the slave's"),
    F("recirc", 9, "bool", mask=0x08),
    F("set_c", 11, scale=0.5, unit="°C"),
    F("outlet_c", 12, scale=0.5, unit="°C", note="mean of units in demand; holds when idle"),
    F("inlet_c", 13, scale=0.5, unit="°C", note="mean of units in demand; holds when idle"),
    F("units_in_demand", 16, note="0 idle/recirc, 1, 2"),
    F("capacity_pct", 17, scale=0.5, unit="%", note="mean of units in demand"),
    F("flow_lpm", 18, scale=0.1, unit="L/min", note="total of units in demand"),
    F("stage2", 20, "bool", mask=0x02, note="slave called in; set ~2 s before its valve opens"),
    F("toggle_25", 25, "bool", mask=0x04, note="toggles ~13 s, unknown"),
    F("x26", 26, note="= 153 - [13] (min 60) at setpoint 51.5 °C; unknown"),
    F("x27", 27, note="function of x26, rule depends on units_in_demand; unknown"),
    F("x28", 28, note="always x26 + 40"),
]

FIELDS = {"water": WATER_FIELDS, "gas": GAS_FIELDS, "cascade": CASCADE_FIELDS}

# Expected payload length per kind; frames of other lengths are not decoded.
LENGTHS = {"water": 34, "gas": 42, "cascade": 34}


def decode(frame: Frame) -> dict | None:
    """Decode a checksum-valid frame of a known kind. Returns None otherwise."""
    kind = frame.kind
    fields = FIELDS.get(kind)
    if fields is None or not frame.checksum_ok or frame.length != LENGTHS[kind]:
        return None
    raw = frame.raw
    d = {f.name: f.extract(raw) for f in fields}
    if kind == "water":
        d["recirc_running"] = d["heating_mode"] == "dhw_recirc" or d["hotbutton_active"]
    return d


def unit_index(frame: Frame) -> int:
    """Cascade index of the unit that sent a status frame (0 = master)."""
    return frame.src - 0x50


def fields_table() -> list[dict]:
    """The field tables as plain data, for the API and the debug view."""
    out = []
    for kind, fields in FIELDS.items():
        for f in fields:
            d = asdict(f)
            d["kind"] = kind
            d["payload_index"] = f.index - HDR_SIZE
            if f.enum is not None:
                d["enum"] = {f"0x{k:02X}": v for k, v in f.enum.items()}
            out.append(d)
    return out
