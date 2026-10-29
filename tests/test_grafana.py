"""The Grafana dashboards: generated files up to date, queries consistent with SCHEMA."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from navien.events import Recorder
from navien.influx import SCHEMA

ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS = ROOT / "grafana" / "dashboards"
FILES = sorted(DASHBOARDS.glob("*.json"))
RPS = {"forever", "samples_90d"}
EVENT_TYPES = {tr.type for tr in Recorder().trackers}


def load_build():
    spec = importlib.util.spec_from_file_location("grafana_build", ROOT / "grafana" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def targets(d):
    for p in d["panels"]:
        for t in p.get("targets", []):
            yield p, t


def test_six_dashboards_and_files_up_to_date():
    rendered = load_build().render()
    assert len(rendered) == 6
    assert sorted(rendered) == [f.name for f in FILES], "run grafana/build.py"
    for f in FILES:
        assert f.read_text() == rendered[f.name], f"{f.name} is stale: run grafana/build.py"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_queries_use_only_schema_names(path):
    d = json.loads(path.read_text())
    n = 0
    for p, t in targets(d):
        query = t["query"]
        n += 1
        measurements = re.findall(r'FROM "(\w+)"\."(\w+)"', query)
        assert measurements, (p["title"], query)
        allowed = set(RPS)
        for rp, m in measurements:
            assert m in SCHEMA, (p["title"], m)
            assert rp == ("samples_90d" if m in ("navien_unit", "navien_cascade") else "forever"), \
                (p["title"], rp, m)
            tags, fields = SCHEMA[m]
            allowed |= {m, *tags, *fields}
        allowed |= set(re.findall(r'AS "(\w+)"', query))          # subquery / column aliases
        for name in re.findall(r'"(\w+)"', query):
            assert name in allowed, (path.name, p["title"], name)
        for value in re.findall(r'"type" = \'(\w+)\'', query):
            assert value in EVENT_TYPES, (p["title"], value)
        for value in re.findall(r'"unit" = \'(\w+)\'', query):
            assert value in {"0", "1", "all"}, (p["title"], value)
        if "navien_hourly" in query or "navien_event" in query:
            # grouping by day must happen in local time, and tz() must be last
            if "time(1d)" in query or "time(1w)" in query:
                assert query.rstrip().endswith("tz('$tz')"), query
    assert n > 0


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_dashboard_structure(path):
    d = json.loads(path.read_text())
    variables = {v["name"] for v in d["templating"]["list"]}
    assert "datasource" in variables and d["uid"].startswith("navien-")
    builtin = {"timeFilter", "__interval"}
    ids = set()
    for p in d["panels"]:
        assert p["id"] not in ids
        ids.add(p["id"])
        g = p["gridPos"]
        assert 0 <= g["x"] and g["x"] + g["w"] <= 24
        if p["type"] in ("row", "text"):
            continue
        assert p["datasource"] == {"type": "influxdb", "uid": "${datasource}"}
        for t in p["targets"]:
            assert t["datasource"]["uid"] == "${datasource}" and t["rawQuery"]
            for var in re.findall(r"\$(\w+)", t["query"]):
                assert var in variables | builtin, (p["title"], var)
        for var in re.findall(r"\$(\w+)", p.get("description", "")):
            assert var in variables, (p["title"], var)
