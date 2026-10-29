#!/usr/bin/env python3
"""Generates grafana/dashboards/*.json. PLAN.md §1.10 (Grafana).

    venv/bin/python grafana/build.py          # rewrite the dashboard files

The JSON files are committed (they are what gets imported); this script is their source,
and tests/test_grafana.py checks that they are up to date and that every query names only
measurements, fields and tags in navien.influx.SCHEMA.

Conventions: stored data is metric; queries convert to US units (°F, gallons, GPM, therms,
BTU/h). Daily figures are SUM() over navien_hourly grouped in local time (`$tz`). Every
panel uses the `datasource` variable, so the files import through the UI and through file
provisioning alike. Targets Grafana 10.4+ (InfluxQL data source).
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"

GAL = 3.78541           # L per US gallon
THERM = 25200           # kcal per therm
BTU = 3.96832           # BTU per kcal
F = "* 9 / 5 + 32"      # °C -> °F
DF = "* 9 / 5"          # °C difference -> °F difference

DS = {"type": "influxdb", "uid": "${datasource}"}
HOURLY = '"forever"."navien_hourly"'
EVENT = '"forever"."navien_event"'
UNIT = '"samples_90d"."navien_unit"'
CASCADE = '"samples_90d"."navien_cascade"'
COUNTERS = '"forever"."navien_counters"'
BUS = '"forever"."navien_bus"'
TZ = "tz('$tz')"
DAY = f"GROUP BY time(1d) {TZ}"
UNITS = (("0", "Master"), ("1", "Slave"))

COLORS = {"Master": "blue", "Slave": "orange", "Hot water": "red", "Recirculation": "purple",
          "Other": "text", "Cascade": "green"}

# water[10] stage codes (decimal) and water[8] heating modes, for value mappings
STAGES = {20: "standby", 21: "demand", 32: "startup", 40: "pre-purge 1", 41: "pre-purge 2",
          42: "pre-ignition", 43: "ignition", 44: "flame on", 45: "ramp up", 51: "combustion",
          52: "water adjust", 60: "flame off", 70: "post-purge 1", 71: "post-purge 2",
          72: "shutting down", 73: "DHW wait"}
STAGE_COLORS = {20: "transparent", 21: "text", 32: "yellow", 40: "yellow", 41: "yellow",
                42: "orange", 43: "orange", 44: "red", 45: "red", 51: "red", 52: "red",
                60: "purple", 70: "blue", 71: "blue", 72: "blue", 73: "text"}
MODES = {0: ("idle", "transparent"), 8: ("recirculation", "purple"), 32: ("hot water", "red")}


# ------------------------------------------------------------------ targets

def q(query: str, alias: str = "", fmt: str = "time_series") -> dict:
    # InfluxQL wants tz() last, after fill()
    if TZ in query:
        query = query.replace(" " + TZ, "").replace(TZ, "").rstrip() + " " + TZ
    return {"datasource": DS, "query": query, "rawQuery": True, "resultFormat": fmt,
            "alias": alias}


def per_unit(template: str) -> list[dict]:
    """One target per unit; `{u}` in the template becomes the unit tag value."""
    return [q(template.format(u=u), name) for u, name in UNITS]


# ------------------------------------------------------------------- panels

def _base(type_: str, title: str, targets: list[dict], w: int, h: int, desc: str = "",
          unit: str = "", decimals: int | None = None, **extra) -> dict:
    defaults: dict = {"unit": unit} if unit else {}
    if decimals is not None:
        defaults["decimals"] = decimals
    overrides = [{"matcher": {"id": "byName", "options": name},
                  "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                 for name, c in COLORS.items()
                 if any(t.get("alias") == name for t in targets)]
    p = {"type": type_, "title": title, "description": desc, "datasource": DS,
         "targets": [dict(t, refId=chr(65 + i)) for i, t in enumerate(targets)],
         "fieldConfig": {"defaults": defaults, "overrides": overrides},
         "options": {}, "_w": w, "_h": h}
    for k, v in extra.items():
        if k == "custom":
            p["fieldConfig"]["defaults"]["custom"] = v
        elif k in ("mappings", "thresholds", "min", "max", "color"):
            p["fieldConfig"]["defaults"][k] = v
        else:
            p[k] = v
    return p


def stat(title, query, unit="", w=4, h=4, decimals=None, desc="", time_from=None, calc="sum"):
    p = _base("stat", title, [q(query)], w, h, desc, unit, decimals,
              color={"mode": "fixed", "fixedColor": "text"})
    p["options"] = {"reduceOptions": {"calcs": [calc], "fields": "", "values": False},
                    "colorMode": "none", "graphMode": "none", "textMode": "value",
                    "justifyMode": "center", "orientation": "auto"}
    if time_from:
        p["timeFrom"] = time_from
    return p


def series(title, targets, unit="", w=12, h=8, style="line", stack=False, decimals=None,
           desc="", time_from=None, points=False, interval=None, min_=None):
    custom = {"drawStyle": "points" if points else style, "lineWidth": 1 if style == "bars" else 2,
              "fillOpacity": 80 if style == "bars" else 0, "showPoints": "always" if points else "never",
              "pointSize": 6, "spanNulls": False, "barAlignment": -1 if style == "bars" else 0,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "axisSoftMin": 0 if min_ is None else min_}
    if min_ is False:
        del custom["axisSoftMin"]
    p = _base("timeseries", title, targets, w, h, desc, unit, decimals, custom=custom)
    p["options"] = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "none"}}
    if time_from:
        p["timeFrom"] = time_from
    if interval:
        p["interval"] = interval
    return p


def bars(title, targets, unit="", **kw):
    return series(title, targets, unit, style="bars", **kw)


def table(title, query, columns: dict, w=24, h=10, desc=""):
    """columns: {source column: (display name, unit, decimals)}; others are hidden."""
    p = _base("table", title, [q(query, fmt="table")], w, h, desc)
    p["options"] = {"showHeader": True, "cellHeight": "sm",
                    "sortBy": [{"displayName": "Time", "desc": True}]}
    p["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": col},
         "properties": [{"id": "displayName", "value": name}]
         + ([{"id": "unit", "value": unit}] if unit else [])
         + ([{"id": "decimals", "value": dec}] if dec is not None else [])}
        for col, (name, unit, dec) in columns.items()]
    return p


def histogram(title, query, unit="", w=8, h=8, desc="", bucket=None):
    p = _base("histogram", title, [q(query, fmt="time_series")], w, h, desc, unit)
    p["options"] = {"legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                    "combine": True}
    if bucket:
        p["options"]["bucketSize"] = bucket
    p["fieldConfig"]["defaults"]["custom"] = {"fillOpacity": 80, "lineWidth": 1}
    return p


def timeline(title, targets, mappings: dict, w=24, h=4, desc=""):
    """mappings: {code: (text, color)}"""
    m = [{"type": "value", "options": {str(code): {"text": text, "color": color, "index": i}
                                       for i, (code, (text, color)) in enumerate(mappings.items())}}]
    p = _base("state-timeline", title, targets, w, h, desc, mappings=m,
              color={"mode": "fixed", "fixedColor": "text"})
    p["options"] = {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                    "alignValue": "left", "legend": {"showLegend": False, "displayMode": "list",
                                                     "placement": "bottom"},
                    "tooltip": {"mode": "single"}}
    return p


def hour_of_day(title, query, unit, w=12, h=8, desc=""):
    """Bars per hour of day: hourly points, formatted to HH and summed (Grafana 10.4+)."""
    p = _base("barchart", title, [q(query, "Gallons")], w, h, desc, unit, 1)
    p["transformations"] = [
        {"id": "formatTime", "options": {"timeField": "Time", "outputFormat": "HH", "useTimezone": True}},
        {"id": "groupBy", "options": {"fields": {"Time": {"aggregations": [], "operation": "groupby"},
                                                 "Gallons": {"aggregations": ["sum"], "operation": "aggregate"}}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Time"}]}},
    ]
    p["options"] = {"xField": "Time", "orientation": "vertical", "showValue": "never",
                    "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                    "barWidth": 0.8, "groupWidth": 0.7, "xTickLabelSpacing": 0}
    p["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": "red"}
    return p


def row(title: str) -> dict:
    return {"type": "row", "title": title, "collapsed": False, "panels": [], "_w": 24, "_h": 1}


def text(content: str, w=24, h=3) -> dict:
    return {"type": "text", "title": "", "options": {"mode": "markdown", "content": content},
            "_w": w, "_h": h}


# ---------------------------------------------------------------- dashboard

DATASOURCE = "influxdb-navien"   # this installation's data source name (user, 2026-10-29)


def variables(price=True, shower=False) -> list[dict]:
    v = [{"type": "datasource", "name": "datasource", "label": "Data source", "query": "influxdb",
          "regex": "", "hide": 0, "refresh": 1, "options": [],
          # preselects this installation's data source; Grafana falls back to the first InfluxDB one
          "current": {"text": DATASOURCE, "value": DATASOURCE}},
         {"type": "constant", "name": "tz", "query": "America/Chicago", "hide": 2,
          "current": {"text": "America/Chicago", "value": "America/Chicago"}, "options": []}]
    if price:
        v.append({"type": "textbox", "name": "price_per_therm", "label": "Gas $/therm",
                  "query": "0.65", "hide": 0,
                  "current": {"text": "0.65", "value": "0.65"}, "options": []})
    if shower:
        v.append({"type": "textbox", "name": "shower_min_s", "label": "Shower ≥ s",
                  "query": "240", "hide": 0,
                  "current": {"text": "240", "value": "240"}, "options": []})
    return v


def layout(panels: list[dict]) -> list[dict]:
    x = y = row_h = 0
    out = []
    for i, p in enumerate(panels):
        w, h = p.pop("_w"), p.pop("_h")
        if x + w > 24 or p["type"] == "row":
            x, y, row_h = 0, y + row_h, 0
        p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        p["id"] = i + 1
        x += w
        row_h = max(row_h, h)
        if p["type"] == "row":
            x, y, row_h = 0, y + h, 0
        out.append(p)
    return out


def dashboard(uid: str, title: str, panels: list[dict], time_from: str, desc: str,
              refresh: str = "", **var_kw) -> dict:
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["navien"],
        "timezone": "browser", "editable": True, "graphTooltip": 1, "schemaVersion": 39,
        "time": {"from": time_from, "to": "now"}, "refresh": refresh,
        "templating": {"list": variables(**var_kw)},
        "links": [{"type": "dashboards", "tags": ["navien"], "asDropdown": False,
                   "title": "Navien", "includeVars": True, "keepTime": True}],
        "annotations": {"list": []},
        "panels": layout(panels),
    }


# --------------------------------------------------------------- dashboards

def hsum(field: str, scale: str = "", unit_tag: str = "all", group: str = DAY) -> str:
    return (f'SELECT SUM("{field}"){scale} FROM {HOURLY} WHERE "unit" = \'{unit_tag}\' '
            f"AND $timeFilter {group} fill(0)").replace("  ", " ")


def ecount(type_: str, cond: str = "", group: str = DAY) -> str:
    return (f'SELECT COUNT("duration_s") FROM {EVENT} WHERE "type" = \'{type_}\' {cond} '
            f"AND $timeFilter {group} fill(0)").replace("  ", " ")


COST = f" / {THERM} * $price_per_therm"
RECIRC_SHARE = (f'SELECT SUM("gas_recirc_kcal") / (SUM("gas_dhw_kcal") + SUM("gas_recirc_kcal")) * 100 '
                f"FROM {HOURLY} WHERE \"unit\" = 'all' AND $timeFilter")
SHOWER = 'AND "duration_s" >= $shower_min_s'
COMPLETE_HOURS = "From the hourly totals: complete hours only, so up to an hour behind."


def tiles(label: str, time_from: str) -> list[dict]:
    t = dict(time_from=time_from, desc=COMPLETE_HOURS)
    return [
        row(label),
        stat("Hot water", hsum("water_dhw_l", f" / {GAL}", group=""), "suffix: gal", decimals=0, **t),
        stat("Gas", f'SELECT (SUM("gas_dhw_kcal") + SUM("gas_recirc_kcal") + SUM("gas_other_kcal")) / {THERM} '
             f"FROM {HOURLY} WHERE \"unit\" = 'all' AND $timeFilter", "suffix: thm", decimals=2, **t),
        stat("Gas cost", f'SELECT (SUM("gas_dhw_kcal") + SUM("gas_recirc_kcal") + SUM("gas_other_kcal")){COST} '
             f"FROM {HOURLY} WHERE \"unit\" = 'all' AND $timeFilter", "currencyUSD", decimals=2, **t),
        stat("Recirculation share of gas", RECIRC_SHARE, "percent", decimals=0, **t),
        stat("Draws", ecount("draw", group=""), "", w=2, time_from=time_from,
             desc="All hot-water draws in the period."),
        stat("Showers", ecount("draw", SHOWER, group=""), "", w=2, time_from=time_from,
             desc="Draws of at least $shower_min_s seconds."),
        stat("Slave called in", ecount("stage2", group=""), "", w=4, time_from=time_from,
             desc="Times the master brought in the second unit (0x41[20] bit 0x02)."),
    ]


def overview() -> dict:
    panels = tiles("Today", "now/d") + tiles("Last 7 days", "7d") + [
        row("Per day"),
        bars("Hot water per day", [q(hsum("water_dhw_l", f" / {GAL}"), "Hot water")],
             "suffix: gal", w=12, decimals=0, desc=COMPLETE_HOURS),
        bars("Gas per day", [q(hsum("gas_dhw_kcal", f" / {THERM}"), "Hot water"),
                             q(hsum("gas_recirc_kcal", f" / {THERM}"), "Recirculation")],
             "suffix: thm", w=12, stack=True, decimals=2, desc=COMPLETE_HOURS),
        row("Last 24 hours"),
        series("Outlet temperature", per_unit(
            f'SELECT MEAN("outlet_c") {F} FROM {UNIT} WHERE "unit" = \'{{u}}\' AND "flow_lpm" > 0 '
            "AND $timeFilter GROUP BY time($__interval) fill(null)"),
            "fahrenheit", w=12, time_from="24h", min_=False,
            desc="While water flows (hot-water draws and recirculation)."),
        series("Flow", per_unit(
            f'SELECT MEAN("flow_dhw_lpm") / {GAL} FROM {UNIT} WHERE "unit" = \'{{u}}\' '
            "AND $timeFilter GROUP BY time($__interval) fill(0)"),
            "flowgpm", w=12, time_from="24h", decimals=1, desc="Hot-water flow per unit."),
    ]
    return dashboard("navien-overview", "Navien — Overview", panels, "now-30d",
                     "Today, last 7 days, and daily hot water and gas.", shower=True)


def hot_water() -> dict:
    panels = [
        bars("Hot water per hour", [q(hsum("water_dhw_l", f" / {GAL}", group="GROUP BY time(1h)"),
                                      "Hot water")], "suffix: gal", w=24, h=7, decimals=1),
        hour_of_day("Hot water by hour of day (total over the range)",
                    hsum("water_dhw_l", f" / {GAL}", group="GROUP BY time(1h)"), "suffix: gal",
                    desc="Each complete hour's gallons, added up by local hour of day. "
                         "Needs Grafana 10.4+ (Format time transformation)."),
        bars("Draws and showers per day", [q(ecount("draw"), "Draws"),
                                           q(ecount("draw", SHOWER), "Showers")], "", w=12,
             decimals=0, desc="Showers: draws of at least $shower_min_s seconds."),
        histogram("Draw duration", f'SELECT "duration_s" / 60 FROM {EVENT} WHERE "type" = \'draw\' '
                  "AND $timeFilter", "m", w=8),
        histogram("Draw volume", f'SELECT "volume_l" / {GAL} FROM {EVENT} WHERE "type" = \'draw\' '
                  "AND $timeFilter", "suffix: gal", w=8),
        series("Time to hot", [q(f'SELECT "time_to_hot_s" FROM {EVENT} WHERE "type" = \'draw\' '
                                 "AND $timeFilter", "Time to hot")], "s", w=8, points=True,
               desc="From the start of a draw until the outlet is within 1 °C of the setpoint. "
                    "Missing for draws that never got there."),
        table("Draws", f'SELECT "duration_s", "volume_l" / {GAL} AS "gal", "peak_flow_lpm" / {GAL} AS "gpm", '
                       f'"mean_outlet_c" {F} AS "outlet_f", "min_inlet_c" {F} AS "inlet_f", '
                       f'"time_to_hot_s", "gas_kcal" / {THERM} AS "therms", "units_max", "truncated" '
                       f"FROM {EVENT} WHERE \"type\" = 'draw' AND $timeFilter ORDER BY time DESC",
              {"duration_s": ("Duration", "s", 0), "gal": ("Gallons", "", 1),
               "gpm": ("Peak GPM", "", 1), "outlet_f": ("Mean outlet", "fahrenheit", 0),
               "inlet_f": ("Lowest inlet", "fahrenheit", 0), "time_to_hot_s": ("Time to hot", "s", 0),
               "therms": ("Gas", "suffix: thm", 3), "units_max": ("Units", "", 0),
               "truncated": ("Cut by gap", "", None)},
              desc="One row per draw, newest first. Units = 2: the slave was called in."),
    ]
    return dashboard("navien-hot-water", "Navien — Hot water", panels, "now-7d",
                     "Hot-water draws: when, how much, how long, how fast hot.",
                     price=False, shower=True)


def gas_cost() -> dict:
    all_gas = f'(SUM("gas_dhw_kcal") + SUM("gas_recirc_kcal") + SUM("gas_other_kcal"))'
    panels = [
        stat("Gas", f"SELECT {all_gas} / {THERM} FROM {HOURLY} WHERE \"unit\" = 'all' AND $timeFilter",
             "suffix: thm", w=6, decimals=2, desc=COMPLETE_HOURS),
        stat("Cost", f"SELECT {all_gas}{COST} FROM {HOURLY} WHERE \"unit\" = 'all' AND $timeFilter",
             "currencyUSD", w=6, decimals=2, desc="At $price_per_therm per therm (dashboard variable)."),
        stat("Recirculation share", RECIRC_SHARE, "percent", w=6, decimals=0),
        stat("Gas per gallon of hot water",
             f'SELECT SUM("gas_dhw_kcal") * {BTU} / (SUM("water_dhw_l") / {GAL}) FROM {HOURLY} '
             "WHERE \"unit\" = 'all' AND $timeFilter", "suffix: BTU/gal", w=6, decimals=0),
        bars("Gas per day", [q(hsum("gas_dhw_kcal", f" / {THERM}"), "Hot water"),
                             q(hsum("gas_recirc_kcal", f" / {THERM}"), "Recirculation"),
                             q(hsum("gas_other_kcal", f" / {THERM}"), "Other")],
             "suffix: thm", w=12, stack=True, decimals=2),
        bars("Cost per day", [q(hsum("gas_dhw_kcal", COST), "Hot water"),
                              q(hsum("gas_recirc_kcal", COST), "Recirculation")],
             "currencyUSD", w=12, stack=True, decimals=2),
        bars("Gas per week", [q(hsum("gas_dhw_kcal", f" / {THERM}", group="GROUP BY time(1w) tz('$tz')"), "Hot water"),
                              q(hsum("gas_recirc_kcal", f" / {THERM}", group="GROUP BY time(1w) tz('$tz')"), "Recirculation")],
             "suffix: thm", w=12, stack=True, decimals=1,
             desc="InfluxQL has no calendar months; weeks start on Thursday (epoch weeks)."),
        series("Gas per gallon", [q(f'SELECT SUM("gas_dhw_kcal") * {BTU} / (SUM("water_dhw_l") / {GAL}) '
                                    f"FROM {HOURLY} WHERE \"unit\" = 'all' AND $timeFilter {DAY}", "Hot water")],
               "suffix: BTU/gal", w=12, points=True, decimals=0,
               desc="Gas burnt in hot-water mode per gallon drawn, per day. Rises with colder supply water."),
        series("Delivered heat ÷ reported gas", [q(
            'SELECT SUM("h") / SUM("g") * 100 FROM (SELECT "flow_dhw_lpm" * ("outlet_c" - "inlet_c") / 60 AS "h", '
            f'"gas_dhw_kcalh" / 3600 AS "g" FROM {UNIT} WHERE $timeFilter) WHERE $timeFilter {DAY}', "Ratio")],
            "percent", w=12, points=True, decimals=0, min_=False,
            desc="Flow × (outlet − inlet) over the gas the heaters report, hot-water mode, per day. "
                 "A consistency check of the decoded values, not a measured efficiency."),
        series("Cold supply temperature", [q(
            f'SELECT MIN("min_supply_c") {F} FROM {HOURLY} WHERE "unit" = \'all\' AND $timeFilter {DAY}',
            "Supply")], "fahrenheit", w=12, points=True, decimals=0, min_=False,
            desc="Lowest inlet temperature during draws longer than 2 minutes, per day."),
    ]
    return dashboard("navien-gas", "Navien — Gas & cost", panels, "now-30d",
                     "Gas use and cost by purpose, gas per gallon, supply temperature.")


def recirculation() -> dict:
    ev = f"FROM {EVENT} WHERE \"type\" = 'recirc' AND \"unit\" = '{{u}}' AND $timeFilter"
    panels = [
        bars("Runs per day", per_unit(f'SELECT COUNT("duration_s") {ev} {DAY} fill(0)'), "", w=12,
             stack=True, decimals=0),
        bars("Recirculation gas cost per day", [q(hsum("gas_recirc_kcal", COST, unit_tag=u), name)
                                                for u, name in UNITS],
             "currencyUSD", w=12, stack=True, decimals=2),
        series("Interval between runs", per_unit(f'SELECT "since_last_s" / 60 {ev}'), "m", w=12,
               points=True, decimals=0,
               desc="Minutes since the unit's previous run started. Shorter = the loop cools faster "
                    "(a heat-loss proxy), or a hot-water draw re-triggered it."),
        series("Run duration", per_unit(f'SELECT "duration_s" {ev}'), "s", w=12, points=True, decimals=0),
        series("Gas per run", per_unit(f'SELECT "gas_kcal"{COST} {ev}'), "currencyUSD", w=12,
               points=True, decimals=3),
        series("Loop temperature at start / end", [
            q(f'SELECT "start_inlet_c" {F} {ev.format(u=u)}', f"{name} start") for u, name in UNITS] + [
            q(f'SELECT "end_outlet_c" {F} {ev.format(u=u)}', f"{name} end") for u, name in UNITS],
            "fahrenheit", w=12, points=True, decimals=0, min_=False,
            desc="Inlet when the run starts (how cold the loop got) and outlet when it ends."),
        table("Runs", f'SELECT "unit", "duration_s", "volume_l" / {GAL} AS "gal", "gas_kcal" / {THERM} AS "therms", '
                      f'"start_inlet_c" {F} AS "start_f", "end_outlet_c" {F} AS "end_f", "since_last_s" / 60 AS "since_m" '
                      f"FROM {EVENT} WHERE \"type\" = 'recirc' AND $timeFilter ORDER BY time DESC",
              {"unit": ("Unit", "", None), "duration_s": ("Duration", "s", 0), "gal": ("Gallons", "", 1),
               "therms": ("Gas", "suffix: thm", 3), "start_f": ("Loop at start", "fahrenheit", 0),
               "end_f": ("Outlet at end", "fahrenheit", 0), "since_m": ("Since previous", "m", 0)},
              desc="Unit 0 = master, 1 = slave."),
    ]
    return dashboard("navien-recirc", "Navien — Recirculation", panels, "now-7d",
                     "Recirculation runs per unit: how often, how long, what they cost.")


def cascade() -> dict:
    burn = f"FROM {EVENT} WHERE \"type\" = 'burn' AND \"unit\" = '{{u}}' AND $timeFilter"
    panels = [
        bars("Burner hours per day", [q(hsum("flame_s", " / 3600", unit_tag=u), name) for u, name in UNITS],
             "h", w=12, decimals=1),
        bars("Burns per day", per_unit(f'SELECT COUNT("duration_s") {burn} {DAY} fill(0)'), "", w=12,
             decimals=0),
        bars("Short burns and failed starts per day",
             [q(hsum("short_burns", unit_tag=u), f"{name} short") for u, name in UNITS]
             + [q(hsum("failed_ignitions", unit_tag=u), f"{name} failed") for u, name in UNITS],
             "", w=12, decimals=0,
             desc="Short: less than 30 s of flame. Failed: a start that never lit (aborted)."),
        series("Time to ignition", per_unit(f'SELECT "ignition_s" {burn}'), "s", w=12, points=True,
               decimals=0, desc="Startup to first flame, per burn."),
        bars("Peak combined hot-water flow per day", [q(
            f'SELECT MAX("peak_flow_lpm") / {GAL} FROM {HOURLY} WHERE "unit" = \'all\' AND $timeFilter {DAY}',
            "Cascade")], "flowgpm", w=12, decimals=1),
        series("Outlet − setpoint while drawing", per_unit(
            f'SELECT MEAN("d") FROM (SELECT ("outlet_c" - "set_c") {DF} AS "d" FROM {UNIT} '
            "WHERE \"unit\" = '{u}' AND \"flow_dhw_lpm\" > 0 AND $timeFilter) WHERE $timeFilter "
            "GROUP BY time($__interval)"),
            "fahrenheit", w=12, min_=False, decimals=1, interval="1m",
            desc="Mean outlet deviation from the setpoint during hot-water flow. Draw starts pull it down."),
        table("Slave called in", f'SELECT "duration_s", "trigger_flow_lpm" / {GAL} AS "gpm", '
                                 f'"trigger_capacity_pct", "truncated" FROM {EVENT} '
                                 "WHERE \"type\" = 'stage2' AND $timeFilter ORDER BY time DESC",
              {"duration_s": ("Duration", "s", 0), "gpm": ("Master flow at call-in", "flowgpm", 1),
               "trigger_capacity_pct": ("Master capacity", "percent", 0),
               "truncated": ("Cut by gap", "", None)}, w=12, h=8),
        table("Errors", f'SELECT "unit", "code", "level", "duration_s" FROM {EVENT} '
                        "WHERE \"type\" = 'error' AND $timeFilter ORDER BY time DESC",
              {"unit": ("Unit", "", None), "code": ("Code", "", None), "level": ("Level", "", None),
               "duration_s": ("Duration", "s", 0)}, w=12, h=8,
              desc="Non-zero water[14..15]. None seen on this installation yet."),
        series("Gas meter (unverified counter)", [q(
            f'SELECT LAST("gas_total_m3") FROM {COUNTERS} WHERE "unit" = \'{u}\' AND $timeFilter '
            "GROUP BY time(1h) fill(none)", name) for u, name in UNITS],
            "suffix: m³", w=24, h=6, min_=False, decimals=1,
            desc="gas[24..25] ×0.1 m³ — the community decode; values look implausibly low here "
                 "(PROTOCOL_NOTES §6)."),
    ]
    return dashboard("navien-cascade", "Navien — Cascade & equipment", panels, "now-30d",
                     "Burner runtime and cycling, ignition, staging, errors.", price=False)


def detail() -> dict:
    s = f"FROM {UNIT} WHERE \"unit\" = '{{u}}' AND $timeFilter GROUP BY time($__interval)"
    panels = [
        series("Outlet / inlet / setpoint", [
            q(f'SELECT MEAN("outlet_c") {F} {s.format(u=u)} fill(null)', f"{name} outlet") for u, name in UNITS] + [
            q(f'SELECT MEAN("inlet_c") {F} {s.format(u=u)} fill(null)', f"{name} inlet") for u, name in UNITS] + [
            q(f'SELECT LAST("set_c") {F} {s.format(u="0")} fill(null)', "Setpoint")],
            "fahrenheit", w=24, h=8, min_=False, decimals=1),
        series("Flow", per_unit(f'SELECT MEAN("flow_lpm") / {GAL} {s} fill(null)'), "flowgpm", w=12,
               decimals=1, desc="All flow, hot-water draws and recirculation."),
        series("Gas rate", per_unit(f'SELECT MEAN("gas_kcalh") * {BTU} {s} fill(null)'), "suffix: BTU/h",
               w=12, decimals=0),
        timeline("Burner stage", [q(f'SELECT LAST("stage_code") {s.format(u=u)} fill(previous)', name)
                                  for u, name in UNITS],
                 {c: (n, STAGE_COLORS[c]) for c, n in STAGES.items()}, h=5),
        timeline("Heating mode", [q(f'SELECT LAST("mode_code") {s.format(u=u)} fill(previous)', name)
                                  for u, name in UNITS], MODES, h=4),
        series("Capacity", per_unit(f'SELECT MEAN("capacity_pct") {s} fill(null)'), "percent", w=12,
               decimals=0),
        series("Cascade: units in demand, slave called in", [
            q(f'SELECT MAX("units_in_demand") FROM {CASCADE} WHERE $timeFilter GROUP BY time($__interval) fill(null)',
              "Units in demand"),
            q(f'SELECT MAX("stage2") FROM {CASCADE} WHERE $timeFilter GROUP BY time($__interval) fill(null)',
              "Slave called in")], "", w=12, decimals=0,
            desc="0x41[16] and [20] bit 0x02 (one cycle late)."),
        series("0x41 unknowns x26 / x27 / x28", [
            q(f'SELECT MEAN("{x}") FROM {CASCADE} WHERE $timeFilter GROUP BY time($__interval) fill(null)', x)
            for x in ("x26", "x27", "x28")], "", w=12, min_=False, decimals=0,
            desc="x26 = max(60, 153 − inlet×2) at setpoint 51.5 °C; meaning unknown (PROTOCOL_NOTES §4.4.1)."),
        series("Bus errors per minute", [
            q(f'SELECT NON_NEGATIVE_DIFFERENCE(LAST("checksum_fail")) FROM {BUS} WHERE $timeFilter '
              "GROUP BY time(1m) fill(none)", "Checksum failures"),
            q(f'SELECT NON_NEGATIVE_DIFFERENCE(LAST("resyncs")) FROM {BUS} WHERE $timeFilter '
              "GROUP BY time(1m) fill(none)", "Resyncs")], "", w=12, decimals=0),
        series("Influx writer", [
            q(f'SELECT LAST("influx_queued") FROM {BUS} WHERE $timeFilter GROUP BY time(1m) fill(none)', "Queued"),
            q(f'SELECT NON_NEGATIVE_DIFFERENCE(LAST("influx_dropped")) FROM {BUS} WHERE $timeFilter '
              "GROUP BY time(1m) fill(none)", "Dropped / min")], "", w=12, decimals=0),
        series("Cycle time", [q(f'SELECT MEAN("cycle_ms") FROM {BUS} WHERE $timeFilter GROUP BY time(1m) fill(none)',
                                "Cycle")], "ms", w=12, min_=False, decimals=0),
    ]
    return dashboard("navien-detail", "Navien — Detail", panels, "now-3h",
                     "Per-second samples: temperatures, flow, gas, burner stage, cascade bytes, bus health.",
                     refresh="1m", price=False)


DASHBOARDS = {"overview": overview, "hot-water": hot_water, "gas-cost": gas_cost,
              "recirculation": recirculation, "cascade": cascade, "detail": detail}


def render() -> dict[str, str]:
    return {f"navien-{name}.json": json.dumps(fn(), indent=2, ensure_ascii=False) + "\n"
            for name, fn in DASHBOARDS.items()}


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, content in render().items():
        (OUT / name).write_text(content)
        print(OUT / name)
