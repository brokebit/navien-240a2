# InfluxDB and Grafana setup

navien-dash writes to **InfluxDB 1.x** over HTTP (InfluxQL, not Flux). These are the
one-time steps on the Influx and Grafana side. The schema itself is in
[`PLAN.md` §1.10](../PLAN.md) and, as code, `SCHEMA` in [`navien/influx.py`](../navien/influx.py).

## 1. Database and retention policies

Two retention policies: per-second samples for 90 days, everything else (counters, bus
stats, events and hourly totals) forever.

```sql
CREATE DATABASE "navien" WITH DURATION INF REPLICATION 1 NAME "forever"
CREATE RETENTION POLICY "samples_90d" ON "navien" DURATION 90d REPLICATION 1 SHARD DURATION 1d
```

The first statement creates the database with `forever` as its default policy. Run them
with the `influx` shell:

```sh
influx -host iot.dynowski.com -port 8086 -ssl -execute 'CREATE DATABASE "navien" WITH DURATION INF REPLICATION 1 NAME "forever"'
influx -host iot.dynowski.com -port 8086 -ssl -execute 'CREATE RETENTION POLICY "samples_90d" ON "navien" DURATION 90d REPLICATION 1 SHARD DURATION 1d'
influx -host iot.dynowski.com -port 8086 -ssl -execute 'SHOW RETENTION POLICIES ON "navien"'
```

or with curl:

```sh
curl -XPOST https://iot.dynowski.com:8086/query --data-urlencode 'q=CREATE DATABASE "navien" WITH DURATION INF REPLICATION 1 NAME "forever"'
curl -XPOST https://iot.dynowski.com:8086/query --data-urlencode 'q=CREATE RETENTION POLICY "samples_90d" ON "navien" DURATION 90d REPLICATION 1 SHARD DURATION 1d'
curl -G https://iot.dynowski.com:8086/query --data-urlencode 'q=SHOW RETENTION POLICIES ON "navien"'
```

`SHOW RETENTION POLICIES` should list `forever` (duration `0s`, default `true`) and
`samples_90d` (`2160h0m0s`). With authentication enabled, add `-username`/`-password`
(or `-u user:pass` for curl) and give the service a user with `GRANT WRITE ON "navien"`;
Grafana needs `GRANT READ`.

Volume: about 260 000 sample points a day. Most fields are constant most of the time and
compress well; expect tens of MB a day in `samples_90d`.

## 2. Enable the writer

In `/etc/navien-dash/navien.toml`:

```toml
[influx]
enabled = true
url = "https://iot.dynowski.com:8086"
database = "navien"
retention_policy = "samples_90d"
rollup_retention_policy = "forever"
```

then `sudo systemctl restart navien-dash`. `http://<pi>:8000/api/health` shows the
writer under `influx`: points written, queued, dropped, rejected and the last error. If
Influx is unreachable, the writer keeps up to `max_queue` points (about 4 hours) and
retries with backoff; `--replay` never writes to Influx.

## 3. Grafana data source

Add an **InfluxDB** data source: query language **InfluxQL**, URL
`https://iot.dynowski.com:8086`, database `navien`, HTTP method GET, min time interval
`1s`. Leave the retention policy empty and name it in queries (`"samples_90d"."navien_unit"`)
because the measurements live in two policies.

Any name works: each dashboard has a **Data source** dropdown at the top. An example
provisioning file is in `provisioning/datasources/navien.yaml`.

## 4. Measurements

All values metric; tags `unit` (`0` master, `1` slave) and `role`.

| measurement | policy | rate | what |
|---|---|---|---|
| `navien_unit` | `samples_90d` | 1/s per unit | temperatures, flow, capacity, gas rate, stage and heating-mode codes, flame, errors, and mode-split `flow_dhw_lpm flow_recirc_lpm gas_dhw_kcalh gas_recirc_kcalh` |
| `navien_cascade` | `samples_90d` | 1/s | units in demand, total flow, mean capacity/temperatures, status bits, `x26..x28` |
| `navien_counters` | `forever` | 1/min per unit | the gas-frame counters (unverified) and version strings |
| `navien_bus` | `forever` | 1/min | frame and error counters since service start, writer queue/drops |
| `navien_event` | `forever` | per event | tags `type` (`draw recirc burn stage2 error`, `draw_unit`) and `unit` (`0 1 all`); timestamped at the event start, written at its end (PLAN.md §1.10) |
| `navien_hourly` | `forever` | 1/h per unit + `all` | litres and kcal by purpose (`water_dhw_l water_recirc_l gas_dhw_kcal gas_recirc_kcal gas_other_kcal`), counts (`draws recirc_runs burns ignitions short_burns failed_ignitions`), `flame_s stage2_s peak_flow_lpm min_supply_c coverage_s` |

Codes: `mode_code` 0 idle, 8 recirculation, 32 hot water demand. `stage_code` 20 standby,
21 demand, 32 startup, 40/41 pre-purge, 42 pre-ignition, 43 ignition, 44 flame on,
45 ramp up, 51 combustion, 52 water adjustment, 60 flame off, 70/71 post-purge,
72 shutting down, 73 DHW wait (decimal; hex in `PROTOCOL_NOTES.md` §5).

## 5. Example queries

`INTEGRAL(field, 1m)` of a L/min field gives litres; of a kcal/h field with `1h`, kcal.
Use the mode-split fields for "how much went to X": filtering on `mode_code` and then
integrating would interpolate across the gaps.

Hot water per day, gallons, both units:

```sql
SELECT INTEGRAL("flow_dhw_lpm", 1m) / 3.78541 FROM "samples_90d"."navien_unit"
WHERE $timeFilter GROUP BY time(1d), "unit" tz('America/Chicago')
```

Gas per day by purpose, therms (1 therm = 25 200 kcal):

```sql
SELECT INTEGRAL("gas_dhw_kcalh", 1h) / 25200 AS "hot water",
       INTEGRAL("gas_recirc_kcalh", 1h) / 25200 AS "recirculation",
       INTEGRAL("gas_kcalh", 1h) / 25200 AS "total"
FROM "samples_90d"."navien_unit" WHERE $timeFilter GROUP BY time(1d) tz('America/Chicago')
```

Outlet temperature, °F:

```sql
SELECT MEAN("outlet_c") * 9 / 5 + 32 FROM "samples_90d"."navien_unit"
WHERE $timeFilter GROUP BY time($__interval), "unit" fill(null)
```

Both units in demand (slave called in), minutes per day:

```sql
SELECT COUNT("units_in_demand") / 60 FROM "samples_90d"."navien_cascade"
WHERE "units_in_demand" = 2 AND $timeFilter GROUP BY time(1d) tz('America/Chicago')
```

Checksum failures per hour (counters reset when the service restarts):

```sql
SELECT NON_NEGATIVE_DIFFERENCE(LAST("checksum_fail")) FROM "forever"."navien_bus"
WHERE $timeFilter GROUP BY time(1h)
```

Samples are kept 90 days. For longer ranges use the hourly totals, which are kept
forever. Sum them per day in local time:

```sql
SELECT SUM("water_dhw_l") / 3.78541 AS "gallons",
       SUM("gas_recirc_kcal") / (SUM("gas_dhw_kcal") + SUM("gas_recirc_kcal")) * 100 AS "recirc %"
FROM "forever"."navien_hourly" WHERE "unit" = 'all' AND $timeFilter
GROUP BY time(1d) tz('America/Chicago')
```

Draws, newest first (one row per draw; filter on `duration_s` or `volume_l` to tell a
hand-wash from a shower):

```sql
SELECT "duration_s", "volume_l" / 3.78541 AS "gal", "peak_flow_lpm" / 3.78541 AS "gpm",
       "time_to_hot_s", "units_max"
FROM "forever"."navien_event" WHERE "type" = 'draw' AND $timeFilter ORDER BY time DESC
```

Recirculation runs per day and unit:

```sql
SELECT COUNT("duration_s") FROM "forever"."navien_event"
WHERE "type" = 'recirc' AND $timeFilter GROUP BY time(1d), "unit" tz('America/Chicago')
```

An hour with `coverage_s` well under 3600 had missing bus data (service stopped).

## 6. Backfill from the raw logs

The service keeps every byte it reads (`/var/lib/navien-dash/raw`, 60 days by default).
`backfill` runs those logs through the same decoder and event detection as the live
service and writes the result with the original timestamps:

```sh
navien-dash backfill --dry-run                 # events and point counts, writes nothing
navien-dash backfill                           # every raw log
navien-dash backfill --from 2026-10-28 --to 2026-10-28    # one local day
```

It reads `/etc/navien-dash/navien.toml` (or `--config`), needs no serial port and can run
while the service does. Points with the same series and timestamp overwrite each other in
Influx, so re-running it changes nothing, and running it after an upgrade that fixes the
decoder or the event logic rewrites history with the fix. It takes about 3.5 s per hour of
log on a Pi 4. Samples older than 90 days are refused by `samples_90d` and counted as
rejected; events and hourly totals still go in.

## 7. Dashboards

Six dashboards in `dashboards/` (also attached to each GitHub release), tagged `navien`
and linked to each other from the top bar:

| file | what |
|---|---|
| `navien-overview.json` | today and last-7-days tiles (gallons, therms, cost, recirculation share, draws, showers, slave call-ins), gallons and therms per day, last 24 h outlet temperature and flow |
| `navien-hot-water.json` | gallons per hour and by hour of day, draws and showers per day, duration and volume histograms, time to hot, a table of every draw |
| `navien-gas-cost.json` | therms and cost per day and week by purpose, gas per gallon, delivered heat ÷ reported gas, cold-supply temperature |
| `navien-recirculation.json` | runs per day, cost per day and per run, interval between runs, duration, loop temperatures, a table of runs |
| `navien-cascade.json` | burner hours, burns, short burns and failed starts, time to ignition, peak combined flow, outlet − setpoint, slave call-ins, errors, the (unverified) gas counter |
| `navien-detail.json` | per-second samples: temperatures, flow, gas rate, burner stage and heating mode timelines, capacity, cascade bytes, bus and writer health |

**Import:** Dashboards → New → Import → upload the JSON file. The **Data source** dropdown
at the top preselects `influxdb-navien` (falls back to the first InfluxDB source if there is
none of that name); set **Gas $/therm** (default 0.65, this installation's all-in rate),
then save the dashboard (tick "Save current variable values as dashboard default") so they
stick. Re-importing a newer version with the same uid replaces it.

**Upload from the command line:** `scripts/grafana-push.sh` posts all six through Grafana's
HTTP API into a "Navien" folder, replacing earlier copies. It needs a service account token
with the Editor role (Administration → Users and access → Service accounts):

```sh
GRAFANA_URL=https://grafana.example:3000 GRAFANA_TOKEN=glsa_... scripts/grafana-push.sh
```

The token can live in `~/.config/navien-dash/grafana-token` (mode 600) instead of the
environment. Pass a directory to upload other copies, e.g. the JSON files of a release.
Dashboards arrive with their file defaults, so a price you saved in Grafana is reset.

**Provisioning** instead: `provisioning/dashboards/navien.yaml` loads every JSON file in a
directory on the Grafana host into a "Navien" folder.

Notes:
- Units are US (°F, gallons, GPM, therms, BTU/h), converted in the queries; Influx holds metric.
- Daily and weekly figures are sums of the hourly totals, grouped in `America/Chicago`
  (the hidden `tz` variable; change it under Dashboard settings → Variables). Hourly totals
  are written when an hour is complete, so "today" tiles run up to an hour behind; the
  per-second panels are live.
- "Showers" are draws of at least **Shower ≥ s** seconds (default 240), a variable on the
  Overview and Hot water dashboards; nothing is classified at write time.
- InfluxQL has no calendar months, so the longest bucket is a week (epoch weeks, starting
  Thursday).
- "Hot water by hour of day" uses the Format time transformation (Grafana 10.4 or newer).
- The dashboards are generated by `build.py` (edit that, not the JSON, then run
  `venv/bin/python grafana/build.py`); a test checks every query against the writer's schema.
