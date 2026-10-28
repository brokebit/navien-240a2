from navien.decode import FIELDS, decode, fields_table

from conftest import CAPTURES, parse_file


def decoded(frames, stream):
    return [decode(f) for f in frames if f.stream == stream]


def test_identity_fields(main_frames):
    for stream in ("50>0F/90", "51>0F/90"):
        for d in decoded(main_frames, stream):
            assert d["device_type"] == "CAS_NPE2"
            assert (d["controller_version"], d["panel_version"]) == ("1.2", "2.7")
            assert d["display_f"] and not d["hotbutton_enabled"]


def test_master_burn_cycle(main_frames):
    water = decoded(main_frames, "50>50/90")
    stages = {d["stage"] for d in water}
    assert {"standby", "startup", "pre_purge_2", "flame_on", "ramp_up",
            "active_combustion", "flame_off", "post_purge_1"} <= stages
    for d in water:
        assert d["flame"] == (d["stage"] == "active_combustion")
        assert d["set_c"] == 51.5
        # demand never without flow; small blips (<= 1.3 L/min) can still read idle
        assert d["heating_mode"] in ("idle", "dhw_demand")
        if d["heating_mode"] == "dhw_demand":
            assert d["flow_lpm"] > 0
        if d["flow_lpm"] >= 1.5:
            assert d["heating_mode"] == "dhw_demand"
        assert d["power_on"] and d["error_code"] == 0
        assert not d["display_celsius"]
    gas = decoded(main_frames, "50>0F/90")
    assert max(d["gas_kcalh"] for d in gas) == 23647      # light-off peak, PROTOCOL_NOTES §5


def test_heat_capacity_matches_gas_rate():
    # PROTOCOL_NOTES §4.2: gas[20]/2 == kcal/h as % of the 50 400 kcal/h rating.
    n = 0
    for path in CAPTURES:
        for d in decoded(parse_file(path)[0], "50>0F/90"):
            if d["heat_capacity_pct"]:
                assert -0.6 <= d["heat_capacity_pct"] - d["gas_kcalh"] / 504 <= 0.1
                n += 1
    assert n > 50


def test_slave_idle(main_frames):
    for d in decoded(main_frames, "51>50/90"):
        assert d["stage"] == "standby" and not d["flame"]
    for d in decoded(main_frames, "51>0F/90"):
        assert d["gas_kcalh"] == 0


def test_cascade_frame(main_frames):
    for d in decoded(main_frames, "50>51/10"):
        assert d["x28"] == d["x26"] + 40
        assert d["status"] in (0x01, 0x05, 0x07)


def test_undecoded_kinds(main_frames):
    for f in main_frames:
        if f.kind in ("poll", "master_44"):
            assert decode(f) is None


def test_fields_table_is_plain_data():
    rows = fields_table()
    assert len(rows) == sum(len(v) for v in FIELDS.values())
    stage = next(r for r in rows if r["name"] == "stage")
    assert stage["payload_index"] == 4 and stage["enum"]["0x33"] == "active_combustion"
