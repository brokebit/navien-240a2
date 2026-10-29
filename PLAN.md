# Navien cascade live dashboard — plan

Status: **draft for review**, 2026-10-28. No code has been written against this plan yet.

Goal: a long-running service on the Pi that passively reads the RS-485 bus of the two
NPE-240A2 units, decodes every frame, keeps a short live history, always keeps a rotated
raw log, serves a live web dashboard (household view + debug view) to desktop and phone
browsers on the LAN, exposes a JSON API for other clients, and later writes to InfluxDB 1.x.

Decisions taken in review (2026-10-28):

| Topic | Decision |
|---|---|
| Web framework | FastAPI + uvicorn (native WebSockets, asyncio fan-out, free OpenAPI docs for API clients) |
| Home Assistant / MQTT | Not used, not planned |
| Clients | Desktop + phone browsers, future API clients. LAN only, **no auth** |
| Dashboard | Household view, with a toggle into a debug (raw-bytes) view |
| Units | User toggle on the client (°C/°F, L/min/GPM, kcal/h / BTU/h). Server always speaks metric |
| In-memory history | 5 min default, configurable |
| Raw logging | Always on, rotated, with retention |
| InfluxDB | 1.x (HTTP line protocol) |
| `navien-capture.py` | Stays a standalone proof-of-concept tool; not integrated, not refactored |
| Analyzer (`--analyze`) | Dropped |
| TX / control | Out of scope. The architecture leaves one place where it would go (§1.7) and nothing more |
| Branch | Work on `master` for now |

---

## 1. Architecture

### 1.1 Overview

```
            ┌───────────────────────── one process: uvicorn, ONE worker ─────────────────────────┐
            │                                                                                      │
 /dev/ttyUSB0 ──► Reader thread ──bytes──► RawLog thread ──► raw/YYYYMMDD-HH.raw(+.idx), rotate/gzip
 (or .raw      │   (blocking pyserial,    (queue.Queue)                                            │
  replay)      │    owns the port)                                                                 │
            │     │ FrameParser → decode                                                           │
            │     │ loop.call_soon_threadsafe(...)                                                 │
            │     ▼                                                                                │
            │   asyncio event loop                                                                 │
            │     StateModel ──(cycle complete)──► Hub ──┬─► WebSocket client queues (bounded,     │
            │       latest per unit/stream               │     drop-oldest) ──► /ws                 │
            │       history ring (5 min)                 ├─► frame ring (debug)                     │
            │       counters/health                      └─► Influx writer queue (bounded, batched) │
            │                                                   └─► POST /write (Phase 4)          │
            │   FastAPI: /api/state /api/history /api/frames /api/health /ws  + static dashboard   │
            └──────────────────────────────────────────────────────────────────────────────────────┘
```

Why this shape:

- **The reader never waits on a consumer.** It does three non-blocking things per read:
  `put_nowait` the raw bytes to the RawLog queue, parse, and hand complete frames to the
  event loop with `call_soon_threadsafe`. Disk, network and browsers can stall without
  affecting reception or frame timestamps (which matter for reverse-engineering).
- **Blocking serial I/O in a dedicated thread**, not `pyserial-asyncio`: simpler, mature,
  and isolates the reader from any event-loop hiccup.
- **One queue per consumer (fan-out), not one shared queue.** A `Queue` item goes to
  exactly one getter, so a shared queue cannot feed both the dashboard and Influx.
  Each consumer gets its own bounded queue with a drop-oldest policy, so a slow phone
  or an unreachable Influx server loses only its own data, and only the oldest.
- **Raw bytes are logged before parsing**, so a parser bug can never lose data. This
  satisfies the "keep raw, re-parse from `.raw`" rule permanently, not just during captures.
- **Exactly one process owns the serial port.** uvicorn must run with a single worker.
  `navien-capture.py` and the service cannot run at the same time
  (`systemctl stop navien-dash` first).

Load is trivial (~6 frames/s, ~270 B/s), so none of this is about throughput; it is about
isolation and timing fidelity.

### 1.2 Package layout

```
navien/
  __init__.py
  __main__.py      CLI: python -m navien [--config navien.toml] [--replay FILE.raw] [--headless]
  config.py        TOML config (stdlib tomllib) + CLI overrides, dataclasses with defaults
  protocol.py      checksum, FrameParser, Frame dataclass, stream/kind classification
  decode.py        field tables (frame indices) + decode functions per frame kind
  source.py        SerialSource and ReplaySource (both run in the reader thread)
  rawlog.py        rotating raw logger thread (+ .idx timing sidecar), retention
  state.py         StateModel: per-unit merge, cycle coalescing, history ring, counters
  hub.py           fan-out to bounded per-subscriber asyncio queues
  api.py           FastAPI app, lifespan wiring, REST + WebSocket routes
  influx.py        Influx 1.x schema + line-protocol writer (Phase 4a)
  events.py        Recorder: events + hourly rollups from cycles (Phase 4b)
  backfill.py      `backfill` subcommand: raw logs -> Influx; `prime` at service start (Phase 4b)
  web/             static dashboard: index.html, app.js, style.css, vendor/uplot.*
tests/
  test_protocol.py test_decode.py test_state.py test_hub.py test_rawlog.py test_api.py test_influx.py
  test_events.py   data/ (raw log excerpt for the event tests)
navien.example.toml
requirements.txt   fastapi, uvicorn[standard], pyserial, httpx; dev: pytest
packaging/              files installed by the .deb (§1.11)
  navien-dash.service   systemd unit (user navien, hardened)
  99-navien-dash.rules  udev rule: /dev/navien for any FT232R, latency_timer=1
  navien-dash.sysusers  service user `navien` in group dialout
  navien.toml           /etc/navien-dash/navien.toml (conffile)
  DEBIAN/               control.in, postinst, prerm, postrm, conffiles
scripts/build-deb.sh    builds dist/navien-dash_<version>_arm64.deb
scripts/release.sh      GitHub release with the .deb attached
grafana/                dashboards/*.json, provisioning/ examples, README (Phase 4c)
```

`protocol.py`/`decode.py` start as a **port** of the parser, checksum and `light_decode`
from `navien-capture.py` (the script is left untouched). From then on the package is the
source of truth for the byte map; fixes do not flow back into the script.

### 1.3 Data sources

- **SerialSource** — pyserial, 19200 8N1, `timeout=0.05`, reads `in_waiting or 1`.
  Reconnects with backoff if the adapter disappears (USB unplug), and reports
  `bus_alive=false` meanwhile instead of crashing.
- **ReplaySource** — reads a `.raw` file (plus `.idx` if present) and feeds it at real-time
  pace (or `--speed N`, looping optional). Without `.idx`, timing is synthesised from
  frame boundaries at the known 1 s cycle. This allows developing and testing the whole
  stack on a laptop with no heater attached, and is what the API tests use.

Timestamp precision: the existing captures show frame times quantised to 16 ms — that is
the FTDI default `latency_timer`. The udev rule sets it to 1 ms.

### 1.4 Decoding and the state model

- `decode.py` keeps field definitions as data: name, frame index (or index range), scale,
  unit, kind, and a `confidence` tag (`measured` / `community` / `unknown`). The debug view
  and the docs both use this table, so "what we know" lives in one place.
- Field coverage follows `PROTOCOL_NOTES.md` §4 after the htumanyan/navien audit (§9 there):
  heating mode, operating state (upstream stage names, `0x20` = startup), error code/level,
  device type, controller/panel versions, heat capacity, display units, recirculation flags,
  and the gas-frame counters (tagged `unverified` until checked against the installer menu).
  Current gas is kcal/h (not upstream's ×10).
- Unknown bytes are not decoded into named fields; they stay visible in the debug view and
  `/api/frames`. The `0x41` bytes `[26..28]` are the exception (exposed as `x26..x28`).
- Frames with a bad checksum are counted and shown in the debug stream but **never
  update state**.
- **Cycle coalescing:** the master's `0x41` frame (t = 0 of every cycle) closes the previous
  cycle. On close, the StateModel emits one `cycle` snapshot containing the latest decoded
  values of every stream seen in that cycle. Missing frames (e.g. the slave's
  duplicate-gas / no-water cycles) keep the previous value and are marked with their
  `age_s`, not zeroed.
- Snapshot shape (metric, SI-ish units, stable field names):

  ```json
  {"type": "cycle", "ts": 1793197830.012, "seq": 4711,
   "units": {
     "0": {"role": "master", "stage": "active_combustion", "stage_code": 51, "flame": true,
           "heating_mode": "dhw_demand", "set_c": 51.5, "outlet_c": 49.0, "inlet_c": 31.5,
           "flow_lpm": 4.3, "capacity_pct": 5.5, "heat_capacity_pct": 13.0, "gas_kcalh": 6640,
           "error_code": 0, "error_level": 0, "power_on": true,
           "recirc_running": false, "recirc_scheduled_enabled": true, "display_f": true,
           "device_type": "CAS_NPE2", "controller_version": "1.2", "panel_version": "2.7",
           "counters": {"gas_m3": 3.0, "days_since_install": 1, "dhw_uses": 4,
                        "operating_hours": 0},
           "age_s": {"water": 0.6, "gas": 0.4}},
     "1": {"role": "slave", "...": "..."}},
   "cascade": {"demand": true, "flame": true, "x26": 93, "x27": 42, "x28": 133, "toggle_25": false},
   "bus": {"frames": 360, "checksum_fail": 0, "resyncs": 0, "cycle_ms": 998}}
  ```

- **History ring:** `deque(maxlen = history_seconds)` of cycle snapshots (1 Hz → 300 entries
  for 5 min). Not persisted across restarts (Influx covers long-term history).
- **Frame ring:** last N raw frames (default 600 ≈ 100 cycles) with timestamp, stream,
  checksum result and hex, for the debug view and `/api/frames`.

### 1.5 Raw logging

- Always on. Writes every received byte, unmodified, to `raw/navien_YYYYMMDDTHHZ.raw`
  (hourly rotation, **UTC** so a DST change never repeats an hour). A sidecar `.idx` records
  `(byte_offset, ts)` at the start of each burst (a chunk ≥ 5 ms after the previous one, i.e.
  every frame start), so timing survives without changing the `.raw` format the capture
  script and ReplaySource understand.
- Closed files are gzip-compressed in the background (~23 MB/day uncompressed).
- Retention by **both** age and total size (defaults: 60 days, 2 GB), oldest deleted first.
- Writes are buffered and flushed every few seconds (SD-card friendly); fsync on rotation and
  shutdown.
- Fed directly by the reader thread through its own `queue.Queue`. If the disk stalls the
  queue fills and the reader drops (and counts) raw chunks rather than blocking.

### 1.6 API surface

| Route | Purpose |
|---|---|
| `GET /` | Dashboard (static files) |
| `GET /api/state` | Latest cycle snapshot |
| `GET /api/history?seconds=300` | History ring (≤ configured window) |
| `GET /api/frames?limit=100&stream=50>51/10` | Recent raw frames (debug) |
| `GET /api/fields` | Field table from `decode.py` (names, indices, units, confidence) |
| `GET /api/health` | bus_alive, last frame age, counters, ws clients, rawlog file, influx queue/last error |
| `WS /ws` | Push channel (below) |
| `GET /docs` | FastAPI's generated OpenAPI docs, for future API clients |

WebSocket protocol (JSON text messages; implemented in `navien/ws.py`, whose docstring is
the reference):

| direction | message | when |
|---|---|---|
| → client | `{"type":"hello","api_version","source","history_seconds","topics"}` | once, on connect |
| → client | `{"type":"history","seconds","cycles":[…]}` | on connect, and on request |
| → client | `{"type":"cycle",…}` (same shape as `GET /api/state`) | every bus cycle |
| → client | `{"type":"frame",…}` (same shape as `/api/frames` items) | every frame, if subscribed |
| → client | `{"type":"gap","dropped":n}` | before the next message after a drop |
| → client | `{"type":"error","message"}` | unparseable / unknown client message |
| → server | `{"subscribe":["frames"]}`, `{"unsubscribe":["frames"]}` | debug view only |
| → server | `{"get":"history","seconds":N}` | after a gap, to re-sync |

- Per client: one Hub subscription (`http.ws_queue`, default 256 messages ≈ 4 min of cycles
  or ~35 s with frames) with drop-oldest, a writer task (the only sender) and a reader task.
- Dead or stalled clients: uvicorn's keepalive ping (20 s interval, 20 s timeout) closes
  them, measured at ~40 s; an app-level send timeout (`http.ws_send_timeout_s`, 30 s) is
  the second line of defence.
- Clients reconnect with exponential backoff and re-sync from the `history` message.

### 1.7 Where TX would go (not built)

The only component allowed to touch the port is the reader thread. If control is ever
added, it becomes a TX queue drained by that thread after ≥ 20 ms of silence following the
master's frames, per the working rules in `CLAUDE.md`. Nothing in this plan implements or
stubs it; the point is only that no other design choice here prevents it.

### 1.8 Configuration

`navien.toml` (copy of `navien.example.toml`), every key optional:

```toml
serial = { port = "/dev/navien", baud = 19200 }
http   = { host = "0.0.0.0", port = 8000 }
state  = { history_seconds = 300, frame_buffer = 600, bus_timeout_s = 3 }
rawlog = { dir = "raw", rotate = "hourly", compress = true, keep_days = 60, max_total_mb = 2048 }

[influx]
enabled = false
url = "http://influx.lan:8086"
database = "navien"
username = ""
password = ""
retention_policy = ""          # samples (navien_unit, navien_cascade), e.g. a 90-day RP
rollup_retention_policy = ""   # events, hourly, counters, bus, e.g. an infinite RP
batch_seconds = 10
max_queue = 50000              # points
timeout_s = 10
sample_every_s = 1

[events]
debounce_s = 3
short_burn_s = 30
hot_band_c = 1.0
```

CLI flags override (`--replay`, `--speed`, `--port`, `--http-port`, `--headless`).

### 1.9 Frontend

- Plain HTML + CSS + JS modules, **no build step**, all assets served by the Pi
  (no CDN), responsive down to phone width, light/dark following the OS.
- **Household view:** one card per unit (stage, flame, outlet/inlet/setpoint, flow,
  capacity, gas rate, recirculation, error code highlighted when non-zero), a cascade strip (demand, which units are firing),
  a status bar (connected, bus alive, last update age, checksum errors).
- **Debug view** (toggle, remembered): per-stream hex rows with bytes that changed in the
  last N cycles highlighted, decoded-field overlay from `/api/fields`, unknown bytes
  marked, counters and cadence per stream.
- **Units toggle:** °C/°F, L/min/GPM, kcal/h / BTU/h, stored in `localStorage`. Conversion
  is client-side only.
- **Charts (Phase 3b):** own small SVG components in `web/charts.js` (changed from the
  original uPlot plan on 2026-10-28: no third-party code to fetch and vendor, ≤ 300 points per
  series, and exact control over the dataviz specs, hover sync and touch). Temperatures, flow, gas rate, capacity per unit, a stage/flame band, and in
  debug mode "plot any byte" (pick stream + frame index) — useful for the `[26..28]` mystery.

### 1.10 InfluxDB 1.x, events and Grafana (Phase 4)

Revised 2026-10-28 after the first service-log analysis (PROTOCOL_NOTES §4.4.1): beyond
raw samples, the useful questions are about *events* (a shower, a recirculation run, a
burner start) and *totals* (gallons, therms, cost, the recirculation share of gas).
InfluxQL is good at windowed sums and means but cannot find runs of a state, so the
service detects events itself and writes them as points. Everything is derived from the
same cycle snapshots the dashboard uses, so live writes and a backfill from raw logs
produce identical points.

```
Reader → StateModel ─ cycle ─▶ Hub ─▶ WebSocket clients
                                  └─▶ Recorder (events.py) ─ samples, events, hourly ─▶ InfluxWriter ─▶ /write
raw logs ─ backfill: parse → StateModel → Recorder (same code) ─────────────────────────▶ InfluxWriter
```

**Writer (`navien/influx.py`).** A Hub subscriber task. Bounded queue in points
(`max_queue`, default 50 000 ≈ 4 h of samples), batches every `batch_seconds` or 5 000
points, `POST /write?db=…&rp=…&precision=ms` via `httpx`, exponential backoff (1 s → 60 s)
on failure. On overflow it drops the oldest samples first and keeps events/hourly points
as long as possible; drops are counted. `/api/health` reports queue depth, points written
and dropped, last success and last error. Network errors, timeouts, 5xx, 429 and 401/403/404
(auth, database or RP missing: fixable on the server) keep the batch and retry; any other
4xx (bad line, field type conflict, points beyond the RP) drops it as `rejected`.

**Measurements.** Metric units throughout; Grafana converts (§ Grafana below). Tags are
few and low-cardinality.

| measurement | tags | rate | fields |
|---|---|---|---|
| `navien_unit` | `unit` 0/1, `role` master/slave | each cycle | `set_c outlet_c inlet_c flow_lpm capacity_pct gas_kcalh heat_capacity_pct`, `stage_code mode_code` (int), `flame power_on recirc_running` (bool), `error_code error_level`, plus **mode-split** `flow_dhw_lpm flow_recirc_lpm gas_dhw_kcalh gas_recirc_kcalh` (0 outside that mode) |
| `navien_cascade` | — | each cycle | `units_in_demand flow_lpm capacity_pct set_c inlet_c outlet_c status stage2 demand flame recirc x26 x27 x28 toggle_25` |
| `navien_counters` | `unit` | each minute | gas[24..37] counters (unverified), version bytes |
| `navien_bus` | — | each minute | frames, checksum failures, resyncs, cycles, timeouts, rawlog drops, influx drops |
| `navien_event` | `type`, `unit` (0/1/`all`) | at event end, timestamped at its start | see below |
| `navien_hourly` | `unit` (0/1/`all`) | at each UTC hour end | see below |

The mode-split fields exist because `INTEGRAL()` over a `WHERE mode_code = …` filter
interpolates across the gaps and over-counts; integrating a field that is 0 outside its
mode is exact.

**Events (`navien/events.py`).** A pure state machine over cycle snapshots; no I/O, no
clock of its own (uses cycle timestamps), so it is tested by replaying captures and raw
logs. Transitions are debounced (`events.debounce_s`, default 3 s) so a one-cycle blip
neither splits nor creates an event: a run must hold that long to count and be off that
long to end (burns start on their first cycle; only their end is debounced). An event is
written when it ends, timestamped at its start. Every event has `duration_s` (seconds the
condition held) and `truncated`: a data gap (> 10 s between cycles, or one unit silent that
long) ends open events at their last cycle with `truncated=true`, and after a gap a
condition that is already on is ignored until it goes off, since its start is unknown.
Missing data is "unknown", never "off".

| `type` | unit | starts / ends | fields |
|---|---|---|---|
| `draw` | `all` | any unit in DHW demand (water[8] = `20`; the same as `0x41`[16] > 0, a cycle sooner) / none | `duration_s volume_l gas_kcal peak_flow_lpm mean_outlet_c min_inlet_c time_to_hot_s units_max` (2 = slave was called in) |
| `draw_unit` | 0/1 | that unit's water[8] = `20` | same, per unit |
| `recirc` | 0/1 | that unit's water[8] = `08` | `duration_s volume_l gas_kcal start_inlet_c end_outlet_c since_last_s` |
| `burn` | 0/1 | stage enters startup…combustion (water[10] `20`–`34`) / leaves it (flame off, post-purge, wait, standby) | `duration_s flame_s ignition_s` (start→first flame) `ignitions` (flame rising edges; a re-light inside the debounce stays one burn) `mode_code` (mode with most flame time) `short` (flame < `events.short_burn_s`, default 30 s) `failed` (no flame at all: an aborted start) |
| `stage2` | `all` | `0x41`[20] bit `0x02` set / cleared | `duration_s trigger_flow_lpm trigger_capacity_pct` (master's, at the set cycle) |
| `error` | 0/1 | water[14..15] ≠ 0 / = 0 | `code level duration_s` |

`time_to_hot_s`: from draw start until outlet ≥ setpoint − `events.hot_band_c` (default
1 °C); empty if never reached. Draw kinds (hand-wash, shower, tub) are not classified in
the service: dashboards filter on `duration_s`, `volume_l` and `peak_flow_lpm`, so the
thresholds can change without rewriting history.

**Hourly rollups.** Written by the Recorder `debounce_s` + 5 s after each UTC hour ends (so
events ending in its last seconds still count), and for the open hour at shutdown: per unit
and `all`: `water_dhw_l water_recirc_l gas_dhw_kcal gas_recirc_kcal gas_other_kcal draws
recirc_runs burns ignitions short_burns failed_ignitions flame_s stage2_s peak_flow_lpm
min_supply_c coverage_s`, timestamped at the hour start. Integrals land in the hour of each
cycle; event counts in the hour the event ended. `all` sums the units (`flame_s` is
burner-seconds), except `peak_flow_lpm` (peak total DHW flow) and `stage2_s` (`all` only).
`coverage_s` is the seconds of bus data seen in that hour. A restarted service first replays
the current hour's raw log into its Recorder (`backfill.prime`, ~2 s for a full hour), so
a restart does not leave a partial hour behind, and one shorter than 10 s does not even
split an event; a later backfill of the full hour also overwrites a partial one (same
series, same timestamp). Daily, weekly and monthly figures are `SUM()` over hourly with
`GROUP BY time(1d) tz('America/Chicago')`, so no continuous queries are needed and nothing
has to be re-run after a backfill. `min_supply_c` = lowest inlet during draws longer than
2 min, the cold-supply estimate.

**Backfill.** `navien-dash backfill [--from WHEN] [--to WHEN] [--dry-run] [PATH…]`
(default `rawlog.dir`; config `./navien.toml`, else `/etc/navien-dash/navien.toml`): parse
the raw logs in time order as one stream with their `.idx` timing, run the same StateModel →
Recorder → writer at full speed with original timestamps. Idempotent (Influx overwrites
identical series + timestamp), so it can be re-run after a decoder or event-logic fix.
Needs no serial port and can run beside the service. `navien_bus` is not written (it
describes the live process). Never drops points: it waits for Influx and gives up after 8
failed attempts. `--dry-run` detects events and counts points without writing. The raw logs
since v0.1.0 become history the day Influx is enabled. Speed: ~3.5 s per hour of log on the
Pi 4.

**This installation (decided 2026-10-28).** InfluxDB OSS 1.13.1 at
`https://iot.dynowski.com:8086` (LAN, 192.168.100.12), authentication off, database
`navien`. The user creates the database and RPs; Grafana's datasource is added by the user,
who supplies its name; dashboards are JSON files the user imports. Gas price default
$1.50/therm (rough US residential average) until the user's tariff is known.

**Retention.** Two retention policies, created by the operator (statements in
`grafana/README.md`): samples (`navien_unit`, `navien_cascade`) in `samples_90d`
(`retention_policy`), everything else in `forever`, the database default
(`rollup_retention_policy`). A backfill point older than its RP is rejected by Influx
("points beyond retention policy"); the writer counts those as dropped, not as errors.
Volume: ~260 000 sample points/day (≈ 5 M field values); TSM compression makes the many
constant fields cheap, so expect tens of MB/day rather than hundreds.
`influx.sample_every_s` (default 1) can thin samples if that is too much; events and
rollups are always computed from every cycle.

**Grafana.** Dashboards as JSON in `grafana/dashboards/`, with provisioning examples
(`grafana/provisioning/` datasource + dashboard provider) and setup notes. Datasource
InfluxDB, query language InfluxQL, referenced by a `${DS_NAVIEN}` input so the files also
import through the UI. Stored data is metric; panels convert in the query (°F, gallons,
GPM, therms, BTU/h), matching the web dashboard's defaults. A `price_per_therm` dashboard
variable drives cost panels. Time zone: browser. Planned dashboards:

1. **Overview** — today and 7-day stat tiles (gallons, therms, cost, recirc share of gas,
   draws, showers, slave call-ins), daily stacked bars of water and gas (DHW / recirc),
   last 24 h temperatures and flow.
2. **Hot water** — usage by hour of day × day (heatmap), draw table, duration and volume
   histograms, time-to-hot trend, showers per day.
3. **Gas & cost** — therms and cost per day/month by purpose, gas per gallon, the
   delivered-heat vs reported-gas ratio (flow × ΔT ÷ gas; a consistency check, not a
   measured efficiency), cold-supply temperature trend.
4. **Recirculation** — runs/day, duration, interval between runs (loop heat-loss proxy),
   gas and cost per run and per day, per unit.
5. **Cascade & equipment** — runtime, burns and short burns per unit, ignition time trend,
   failed ignitions, stage-2 events with their trigger flow/capacity, peak combined demand,
   errors, outlet stability (outlet − setpoint during long draws).
6. **Detail** — per-cycle samples over hours: temperatures, flow, gas, burner stage as a
   state timeline, the `0x41` unknowns, bus health.

Dashboards are checked by a test that parses every JSON file and verifies each query only
references measurements, fields and tags the writer produces (from one schema table in
`influx.py`), so a rename cannot silently blank a panel. No Grafana runs on the Pi; the
visual check is the user's.

### 1.11 Deployment: a Debian package per release

Decided 2026-10-28: no manual installs; every release is a `.deb`.

- **Versioning:** semver git tags `vX.Y.Z` (Phase 1 = `v0.1.0`, each later phase a minor
  bump, fixes a patch bump); `phase-N` tags kept alongside. The build derives the package
  version from `git describe`: `v0.1.0` → `0.1.0`; commits past a tag → `0.1.0+git3.gabc1234`;
  uncommitted changes (only with `--dev`) → `…dirty`. Dev builds sort after their release.
- **Build:** `scripts/build-deb.sh [--dev]` on the Pi (arm64, system Python 3.14) →
  `dist/navien-dash_<version>_arm64.deb`. Plain `dpkg-deb`, no debhelper. Release builds
  refuse a dirty tree. The build smoke-tests the staged venv (imports + a capture replay).
- **Dependencies:** a bundled virtualenv in `/opt/navien-dash/venv`, installed `--no-deps`
  from `requirements.lock` (exact versions we test with) and checked with `pip check`.
  The package `Depends: python3 (>= 3.14), python3 (<< 3.15)` because the venv links to the
  system interpreter; a Python minor upgrade needs a rebuild.
- **Layout:** code `/opt/navien-dash/app` (on the venv's path via a `.pth`), config
  `/etc/navien-dash/navien.toml` (conffile, survives upgrades), state and raw logs
  `/var/lib/navien-dash` (systemd `StateDirectory`), CLI wrapper `/usr/bin/navien-dash`,
  unit `/usr/lib/systemd/system/navien-dash.service`, udev rule
  `/usr/lib/udev/rules.d/99-navien-dash.rules`, user via `/usr/lib/sysusers.d`.
- **Service:** runs as system user `navien` (group `dialout`), `ProtectSystem=strict` and
  related hardening, `Restart=always`, logs to journald.
- **Maintainer scripts:** postinst creates the user, reloads/triggers udev, enables the unit
  on first install and restarts it on upgrade (unless disabled); prerm stops it; purge keeps
  `/var/lib/navien-dash` (raw logs are irreplaceable) and says so.
- **Install / upgrade:** `sudo apt install ./dist/navien-dash_<version>_arm64.deb`.
- Development still uses the repo `venv/` (`pip install -r requirements-dev.txt`) and
  `python -m navien`; stop the packaged service first, since only one process may own the port.

---

## 2. Development plan

Each phase ends with passing tests, a short note in this file's changelog (§4), and a
review with the user before the next phase starts. All phases are receive-only.

### Phase 0 — Foundations: receive and parse (headless)

1. Repo hygiene: `.gitignore` (`venv/`, `__pycache__/`, `raw/`, `navien.toml`), `requirements.txt`.
   **First task: `pip install` FastAPI/uvicorn/httpx into the venv** to confirm wheels exist
   for Python 3.14 on aarch64 (pydantic-core, uvloop, httptools). If not, fall back to
   `uvicorn` without `[standard]` extras, or a Python 3.12/3.13 venv.
2. `navien/protocol.py`, `navien/decode.py` ported from the capture script; field tables as data.
3. `navien/source.py` (Serial + Replay), `navien/rawlog.py`, `navien/config.py`.
4. `navien/state.py`: cycle coalescing, history ring, counters (pure, no asyncio, easy to test).
5. `python -m navien --headless` prints one line per cycle (both units) from serial or replay.
6. Tests: parser on all three `captures/*.raw` (frame counts and 0 checksum failures match
   their `.jsonl`), decoder golden values from `PROTOCOL_NOTES.md` §5, state model handling of
   the slave duplicate-gas cycles, rawlog rotation/retention on a temp dir.
7. udev rule + latency timer (now shipped in the package, §1.11).

**Done when:** headless mode runs on the Pi against the live bus for 1 h with 0 unexplained
resyncs, and the raw log rotates and replays back to identical frames.

### Phase 1 — Service core and REST

1. `navien/hub.py`, `navien/api.py` with FastAPI lifespan starting the reader and RawLog threads.
2. `/api/state`, `/api/history`, `/api/frames`, `/api/fields`, `/api/health`, `/docs`.
3. Bus-loss handling (adapter unplug → reconnect, `bus_alive=false`).
4. systemd unit, shipped in the first `.deb` (§1.11).
5. Tests: `TestClient` against ReplaySource.

**Done when:** `curl` from another LAN machine returns live state; service survives
adapter unplug/replug and a restart via systemd.

### Phase 2 — WebSocket push

1. `/ws` with hello → history → cycle stream, optional `frames` subscription.
2. Bounded per-client queues, drop-oldest + `gap` marker, ping/timeout cleanup of dead clients.
3. Tests: WS client via TestClient; slow-consumer test proves the reader and other clients
   are unaffected.

**Done when:** several browser tabs plus a deliberately stalled client run for an hour with
no growth in memory and no missed cycles on healthy clients.

### Phase 3a — Text dashboard

1. Household view, status bar, reconnect logic.
2. Units toggle and debug toggle (`localStorage`).
3. Debug view: hex rows, change highlighting, field overlay, per-stream counters.
4. Manual test on desktop and phone.

**Done when:** a tap opened in the house is visible on a phone within ~1 s, and the debug
view shows the `0x41` `[26..28]` bytes changing live.

### Phase 3b — Charts

1. Vendor uPlot; charts fed from `history` on load then appended per cycle.
2. Temperature, flow, gas, capacity, stage/flame band; window = history window.
3. Debug "plot any byte".

**Done when:** charts on a phone stay smooth over a 5-minute window and recover correctly
after a reconnect.

### Phase 4 — InfluxDB 1.x, events, Grafana (§1.10)

Three sub-phases, each reviewed and released like 3a/3b.

**4a — Writer and samples (v0.5.0).**
1. `navien/influx.py`: line protocol, schema table, batching writer, backoff, drop policy,
   health in `/api/health`; `[influx]` config additions (`rollup_retention_policy`,
   `timeout_s`, `sample_every_s`).
2. `navien_unit` (with mode-split fields), `navien_cascade`, `navien_counters`, `navien_bus`.
3. Tests: line-protocol escaping and types (int `i`, float, bool, string), batching, backoff,
   4xx vs 5xx handling, overflow drop order, all against a fake HTTP server.
4. `grafana/README.md`: database and retention-policy statements, example InfluxQL.

**Done when:** a 24 h run lands one sample per cycle per series; a 10-minute Influx outage
loses nothing and leaves the web dashboard unaffected; a longer one drops oldest samples
only, counted in `/api/health`.

**4b — Events, hourly rollups, backfill (v0.6.0).**
1. `navien/events.py` (Recorder): the event types and hourly fields of §1.10, `[events]`
   config (`debounce_s`, `short_burn_s`, `hot_band_c`).
2. `navien-dash backfill` (and `python -m navien backfill`).
3. Tests against `captures/` and a committed excerpt of the 2026-10-28 service log (the
   18:23 two-unit draw and a recirc run; small, so it can live in `tests/data/`): expected
   events, volumes and gas within tolerance, debounce, partial-hour overwrite, backfill
   idempotence.

**Done when:** a backfill of the raw logs since v0.1.0 gives event counts and daily
totals that match a direct analysis of the same logs (as in the 2026-10-28 analysis:
17 recirc runs (corrected from 24, see the changelog), 47 % recirc share for that
afternoon), and re-running it changes nothing.

**4c — Grafana dashboards (v0.7.0, tag `phase-4`).**
1. The six dashboards of §1.10 as JSON, provisioning examples, setup notes in
   `grafana/README.md`; release assets include the dashboard files.
2. Test: dashboards reference only schema fields.

**Done when:** the user imports the dashboards into their Grafana and checks each against
the web dashboard and a hand calculation for one day.

### Parallel reverse-engineering track (not phase-gated)

The dashboard's debug view and raw log are meant to make these easier; they stay in
`CLAUDE.md` "Next steps": slave-firing capture, recirculation event, installer-menu
comparison. Findings update `PROTOCOL_NOTES.md` and the field table in `decode.py` together.

---

## 3. Out of scope

TX / control of any kind · authentication · HA / MQTT · persistence of the in-memory
history · running or administering the Influx and Grafana servers (we ship dashboard
files and setup statements, not server config) · changes to `navien-capture.py` ·
an offline analyzer tool.

## 4. Changelog

- 2026-10-28 — draft written after review Q&A.
- 2026-10-28 — field coverage updated after the htumanyan/navien audit; plan approved, Phase 0 started.
- 2026-10-28 — Phase 0 implemented: `navien/{protocol,decode,state,config,source,rawlog,__main__}.py`,
  35 tests passing, ~3 min live headless run (0 checksum failures, 0 resyncs, clean SIGTERM
  shutdown). Changes from the plan: raw-log periods are UTC; `.idx` marks bursts, not every
  read (it was 2.5× the raw size); the parser accepts any sys_type, which exposed a seventh
  frame per cycle (`50→52`, sys_type `0x13`, PROTOCOL_NOTES §4.6). Outstanding for "done":
  install the udev rule (sudo) and a 1 h live run.
- 2026-10-28 — Phase 0 committed and tagged `phase-0`. udev rule installed; its latency line
  only matched `add` events, fixed to `ACTION!="remove"` (needs re-copying).
- 2026-10-28 — Phase 1 implemented: `service.py`, `hub.py`, `api.py`, headless mode rebuilt on
  the same Service, `deploy/navien-dash.service`; 45 tests passing; live run over the LAN OK.
  Changes from the plan: `/api/health` always returns 200 with `status` = `ok`|`no_data`
  (not 503), so the dashboard can read it; access logging off (journald noise).
  Outstanding for "done": install the systemd unit (sudo), unplug/replug test.
- 2026-10-28 — Deployment changed to a Debian package per release (§1.11): bundled venv,
  `dpkg-deb` build script, `vX.Y.Z` tags, generic FT232R udev rule. Manual `deploy/` files
  moved to `packaging/`. Requirements split into `requirements.txt` (top level),
  `requirements.lock` (full pinned runtime set) and `requirements-dev.txt` (+ pytest, httpx2).
  Phase 1 "done" now means: `v0.1.0` .deb installs, service runs, survives unplug/replug.
- 2026-10-28 — `v0.1.0` installed and verified by the user. `v0.1.1`: the serial port is
  opened with an exclusive lock, so a dev run cannot silently share the port with the
  installed service (two readers split the byte stream). Dev runs use `--replay` or ask
  the user to stop the service first.
- 2026-10-28 — Phase 2 implemented: `navien/ws.py` + `/ws` route, `http.ws_queue` and
  `http.ws_send_timeout_s` config. Protocol as in §1.6 (adds `unsubscribe`, `get history`,
  `error`). 51 tests. Soak on replay at 10× for 7 min: 5 clients × 4 129 cycles, 0 missing,
  0 gaps; one also took 28 919 frames; a stalled client was closed by uvicorn's ping after ~40 s.
- 2026-10-28 — Phase 3a implemented: `navien/web/` (index.html, app.js, units.js, debug.js,
  style.css), served at `/` with `Cache-Control: no-cache`; JSON index moved to `/api`.
  Household view (system tiles, per-unit cards with stage/flame, temps, flow, gas, burner
  output meter, recirculation, error pill, unit info with the unverified counters), status
  pill + adapter error detail from `/api/health`, reconnect with backoff and history re-sync
  on `gap`, unit toggles defaulting to °F/GPM/BTU/h, debug view (hex rows in cycle order,
  change highlighting at <3/15/60 s, field overlay by confidence, tap-a-byte detail with
  value range, bus counters, pausable frame log). Colours from the dataviz reference palette
  (unit identity = categorical slots 1/2; status colours only for health, always with icon +
  label); live values use tabular figures so they don't jitter. No JS engine or browser on
  the Pi: the JS was checked statically only, so the user's desktop/phone check is the real test.
- 2026-10-28 — Phase 3b implemented: `web/charts.js` (LineChart, StateBand; no uPlot, see
  §1.9). Charts for outlet temperature (with setpoint reference), inlet temperature, flow,
  gas rate, and a burner band (firing / ignition-purge per unit); window = `history_seconds`,
  filled from the `history` message and re-filled after reconnect or `gap`. One y-axis per
  chart, unit colours as identity, legend + end labels (dropped on collision), crosshair
  synced across charts with a tooltip for every series, keyboard (arrows/Home/End/Esc) and
  touch scrubbing, a table view per chart. Debug: plot of the selected byte over the last
  600 frames of its stream. Gas rate and burner output % are the same quantity (heat
  capacity = kcal/h ÷ 504), so only gas rate is charted; operating capacity (water[17]) is
  not charted until its meaning is clearer. JS checked statically only.

- 2026-10-28 — v0.4.1: README (with dashboard screenshot in `docs/`), MIT licence, example
  config comment fixed. Charts: time axes capped at 8 ticks, so wide charts (the burner
  band) no longer pick 30 s steps whose HH:MM labels repeat; sub-minute steps show seconds.
  Decoder: the `0x41` frame is cascade-wide (PROTOCOL_NOTES §4.4.1, from the first
  service-log slave-firing and recirculation events); field `active` (wrong once two units
  draw) replaced by `units_in_demand`, plus `recirc` and `stage2`.
- 2026-10-28 — Phase 4 expanded (§1.10, §2 Phase 4): besides per-cycle samples, the
  service detects events (draws, recirc runs, burns, stage-2, errors) and writes hourly
  rollups, so day/week/month totals are plain `SUM()`s and need no continuous queries;
  mode-split gas/flow fields make integrals exact; a backfill command loads the raw logs
  through the same code; Grafana dashboards (six) ship as JSON with a schema-consistency
  test. Split into 4a (writer, v0.5.0), 4b (events/backfill, v0.6.0), 4c (Grafana, v0.7.0).
- 2026-10-28 — Phase 4a implemented: `navien/influx.py` (`SCHEMA`, line protocol with type
  coercion, `Points` snapshot→lines, `InfluxWriter` with per-RP queues, batching, backoff,
  samples-first overflow drop, health under `/api/health` → `influx`). Writes
  `navien_unit`/`navien_cascade` every cycle to `samples_90d`, `navien_counters`/`navien_bus`
  each minute to `forever`; disabled under `--replay`. Water `heating_mode_code` added to the
  decoder for `mode_code`. `grafana/README.md`: database/RP statements, data source, example
  queries. Checked against the real server (1.13.1): a write to a missing database gets a 404
  and is retried, not dropped.
- 2026-10-28 — Phase 4b implemented: `navien/events.py` (Recorder: debounced trackers for
  draw, draw_unit, recirc, burn, stage2, error; hourly buckets), `navien_event` and
  `navien_hourly` in `SCHEMA`, `[events]` config, `navien/backfill.py` (`navien-dash
  backfill`, and `prime` at service start). Deviations from the plan above: draws key on the
  units' water[8] rather than the lagged `0x41`[16]; burns are startup→end of combustion and
  carry `ignitions`; event counts go to the hour the event ends; `truncated` on every event.
  Checked against the 2026-10-28 15:43–19:10 service log: 149 L DHW, 9 094 kcal, recirc
  share 46.7 % (frame-level analysis: 149 L, 9 098 kcal, 47 %); 17 recirc runs (10 master,
  7 slave) and 23 ignitions, equal to the raw mode-`08` runs and flame-on entries. The
  earlier "24 runs (16 + 8)" was a miscount. One aborted start found (master 18:18:29:
  startup → flame off → post-purge, lit 10 s later). Tests use a 15-minute excerpt of that
  log in `tests/data/` (54 kB).
