# InfluxDB and Grafana setup

navien-dash writes to **InfluxDB 1.x** over HTTP (InfluxQL, not Flux). These are the
one-time steps on the Influx and Grafana side. The schema itself is in
[`PLAN.md` §1.10](../PLAN.md) and, as code, `SCHEMA` in [`navien/influx.py`](../navien/influx.py).

## 1. Database and retention policies

Two retention policies: per-second samples for 90 days, everything else (counters, bus
stats, and from v0.6.0 events and hourly totals) forever.

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

The dashboards (Phase 4c) ask for this data source when imported.

## 4. Measurements

All values metric; tags `unit` (`0` master, `1` slave) and `role`.

| measurement | policy | rate | what |
|---|---|---|---|
| `navien_unit` | `samples_90d` | 1/s per unit | temperatures, flow, capacity, gas rate, stage and heating-mode codes, flame, errors, and mode-split `flow_dhw_lpm flow_recirc_lpm gas_dhw_kcalh gas_recirc_kcalh` |
| `navien_cascade` | `samples_90d` | 1/s | units in demand, total flow, mean capacity/temperatures, status bits, `x26..x28` |
| `navien_counters` | `forever` | 1/min per unit | the gas-frame counters (unverified) and version strings |
| `navien_bus` | `forever` | 1/min | frame and error counters since service start, writer queue/drops |

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

Samples are kept 90 days. Longer-range totals come from the hourly rollups added in
v0.6.0, which are kept forever.
