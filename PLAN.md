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
  influx.py        Influx 1.x line-protocol writer (Phase 4)
  web/             static dashboard: index.html, app.js, style.css, vendor/uplot.*
tests/
  test_protocol.py test_decode.py test_state.py test_hub.py test_rawlog.py test_api.py test_influx.py
navien.example.toml
requirements.txt   fastapi, uvicorn[standard], pyserial, httpx; dev: pytest
packaging/              files installed by the .deb (§1.11)
  navien-dash.service   systemd unit (user navien, hardened)
  99-navien-dash.rules  udev rule: /dev/navien for any FT232R, latency_timer=1
  navien-dash.sysusers  service user `navien` in group dialout
  navien.toml           /etc/navien-dash/navien.toml (conffile)
  DEBIAN/               control.in, postinst, prerm, postrm, conffiles
scripts/build-deb.sh    builds dist/navien-dash_<version>_arm64.deb
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

WebSocket protocol (JSON text messages):

- On connect the server sends `{"type":"hello", "config":{history_seconds,…}}`, then
  `{"type":"history", "cycles":[…]}` so charts fill immediately, then `cycle` messages at 1 Hz.
- The client may send `{"subscribe":["frames"]}` to also receive every raw frame
  (`{"type":"frame",…}`); only the debug view does this, so normal clients stay light.
- Per-client queue bounded (e.g. 120 messages); on overflow the oldest is dropped and the
  client gets a `{"type":"gap"}` marker so it can re-request history.
- Clients reconnect with exponential backoff and re-sync from `history`.

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
retention_policy = ""
batch_seconds = 10
max_queue = 3600
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
- **Charts (Phase 3b):** uPlot (small, built for streaming time series), vendored into
  `web/vendor/`. Temperatures, flow, gas rate, capacity per unit, a stage/flame band, and in
  debug mode "plot any byte" (pick stream + frame index) — useful for the `[26..28]` mystery.

### 1.10 InfluxDB 1.x (Phase 4)

- Writer is another Hub subscriber: async task, bounded queue (default 3600 points per
  measurement ≈ 1 h at 1 Hz), batches every `batch_seconds`, `POST /write?db=…&precision=ms`
  via `httpx`. Exponential backoff on failure; on overflow drops oldest and counts.
- Measurements:
  - `navien_unit`, tags `unit=0|1`, `role=master|slave`; fields: the decoded numeric/bool
    values plus `stage_code` (int) and `stage` (string).
  - `navien_cascade`: `demand`, `flame`, `x26`, `x27`, `x28`, `toggle_25`.
  - `navien_bus`: frame/checksum/resync counters, cycle time, raw-log drops.
- 1 point per measurement per cycle (~260 k points/day total). Downsampling and retention
  policies are configured on the Influx side; the plan documents example CQs but does not
  manage the server.

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

### Phase 4 — InfluxDB 1.x

1. `navien/influx.py`, config section, health reporting.
2. Tests: line-protocol formatting (escaping, ints vs floats vs bools), batching, backoff,
   overflow drop-oldest using a fake HTTP server.
3. Example InfluxQL queries and continuous queries documented here.

**Done when:** a 24 h run lands ~86 400 points per series in Influx, and an Influx outage of
10 minutes loses nothing (queue absorbs it) while the dashboard is unaffected.

### Parallel reverse-engineering track (not phase-gated)

The dashboard's debug view and raw log are meant to make these easier; they stay in
`CLAUDE.md` "Next steps": slave-firing capture, recirculation event, installer-menu
comparison. Findings update `PROTOCOL_NOTES.md` and the field table in `decode.py` together.

---

## 3. Out of scope

TX / control of any kind · authentication · HA / MQTT · persistence of the in-memory
history · managing the Influx server or Grafana · changes to `navien-capture.py` ·
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
