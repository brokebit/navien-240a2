# Navien NPE-240A2 cascade — RS-485 bus decoding

## What this project is

Reverse-engineering and monitoring the RS-485 bus of two Navien NPE-240A2 tankless
water heaters running in cascade (unit 0 = master, unit 1 = slave). The goal is a
passive monitor first (log every field from both units), then possibly control
(setpoint, power, recirculation) later. Work so far was done in a claude.ai chat;
this file plus `PROTOCOL_NOTES.md` is the handoff. Read `PROTOCOL_NOTES.md` before
touching the decoder.

The current build effort is a live dashboard service (FastAPI, WebSockets, raw logging,
later InfluxDB 1.x). Its architecture, decisions and phased development plan are in
`PLAN.md`; read it before writing service or dashboard code.

## Hardware

- Two NPE-240A2, cascaded over a 2-wire A/B cable between their RS-485 ports.
  The port also exposes GND and ~13 V (NaviLink accessory power); 13 V is never used.
- Sniffer: Waveshare "USB TO RS485 (C)" (FT232RNL, galvanically isolated, hardware
  auto-direction, fixed onboard 120 Ω termination). Wired A+→A, B−→B, GND→heater GND.
- Host: Raspberry Pi 4, Ubuntu 26.04 (aarch64), Python 3.14 in `venv/`. Adapter shows up as
  `/dev/ttyUSB0` (`ftdi_sio`); the package's udev rule adds a stable `/dev/navien`.
  Powered by a Mean Well RS-25-5.
- FTDI default `latency_timer` is 16 ms, so the existing captures' timestamps are quantised
  to 16 ms. Set it to 1 ms for timing work.
- No NaviLink device is on the bus. NaviLink must be enabled in the installer menu
  for the heater to accept commands; this has not been needed for passive capture.

## Bus facts (measured on this installation, 2026-10-28 capture)

- 19200 baud, 8N1. Frame: `F7 | sys_type | src | dst | dir | len | payload[len] | checksum`.
- **sys_type is `0x02` on these units** (single NPEs in the community repos report
  `0x05`, NHB-H `0x04`). Anything we ever transmit must use `0x02`.
- Checksum: seed by *source* — every frame from `0x50` uses seed `0x4B`, everything
  else (`0x51` slave, `0x0F` controller) uses `0x62`. Algorithm in `navien-capture.py`
  (`navien_checksum`), verified against all 360 frames of the capture and the
  published single-unit examples.
- Addresses: `0x50` master, `0x51` slave, `0x0F` controller/NaviLink, `0x01` panel.
  `dst=0x50` = "water" status, `dst=0x0F` = "gas" status. `dir 0x90` status, `0x10` control.
- Fixed 1.000 s cycle, seven frames, always this order:

  | t (ms) | frame | payload type byte | notes |
  |---|---|---|---|
  | 0 | `50→51` dir 10, len 34 | `0x41` | master → slave: cascade totals (units in demand, total flow, mean temps), one cycle late |
  | ~60 | `51→50` dir 90, len 34 | `0x42` | slave water |
  | ~110 | `51→0F` dir 90, len 42 | `0x45` | slave gas (occasionally sent twice, water skipped) |
  | ~210 | `50→52` dir 10, len 3 | `0x40` | **sys_type `0x13`**, constant `40 00 04`; poll of absent unit 2? (PROTOCOL_NOTES §4.6). The capture script drops it |
  | ~400 | `50→50` dir 90, len 34 | `0x42` | master water |
  | ~600 | `50→0F` dir 90, len 42 | `0x45` | master gas |
  | ~800 | `50→50` dir 10, len 34 | `0x44` | master, constant `44 00 14 1E 0A 00…`, unknown |

  The slave only speaks in the ~150 ms after the master's `0x41` poll. The ~150 ms
  quiet gaps are where a controller transmits (community rule: wait ≥20 ms of silence).

## Byte map (frame index, 0 = the F7 byte; payload index = frame index − 6)

Confirmed on this installation unless marked.

Cross-checked against htumanyan/navien on 2026-10-28 (PROTOCOL_NOTES.md §9).

**Water frame (`dst 0x50`, len 34):**
`[8]` heating mode enum: `00` idle, `08` DHW recirc, `20` DHW demand · `[9]` system power
(bit `0x01` on; `0x20` = no HotButton fitted) · `[10]` operating state (14 standby, 20 startup,
29 pre-purge 2, 2C flame-on, 2D ramp, 33 combustion, 3C flame-off, 46 post-purge; full enum in
PROTOCOL_NOTES §5) · `[11]` setpoint °C×2 · `[12]` outlet °C×2 · `[13]` inlet °C×2 ·
`[14..15]` error code LE · `[16]` error level · `[17]` capacity %×2 · `[18]` flow L/min×10 ·
`[24]` status bits (`0x08` display °C, `0x01`/`0x02` int/ext scheduled recirc) · `[27]` flame on ·
`[33]` recirc flags (`0x01` HotButton active, `0x02` scheduled recirc enabled).
Role marker: `[7]` 00 master / 20 slave. (`[30]` is *not* a role marker; it's an unknown counter.)

**Gas frame (`dst 0x0F`, len 42):**
`[8]` device type (`0C` = CAS_NPE2) · `[10]` controller version (`0C` → 1.2) · `[12]` panel
version (`1B` → 2.7) · `[14]` setpoint · `[15]` outlet · `[16]` inlet (all °C×2) ·
`[20]` heat capacity %×2 (verified = kcal/h ÷ 50 400 rating) · `[21]` bit `0x01` display °F ·
`[22..23]` current gas, kcal/h, LE (upstream claims ×10 — our data says no) ·
`[24..25]` cumulative gas ×0.1 m³ · `[28..29]` days since install · `[30..31]` DHW use count ·
`[36..37]` operating hours — the counters read implausibly low, **unverified** · `[46]` 01 master / 00 slave.

**Master→slave `0x41` frame (len 34):** cascade-wide values, one cycle late (PROTOCOL_NOTES §4.4.1):
`[8]` mode, `[11]` setpoint · `[12]`/`[13]` mean outlet/inlet and `[17]` mean capacity of the units in
DHW demand (freeze when idle) · `[16]` **number of units in DHW demand** (0 during recirc) ·
`[18]` **total** flow · `[9]` bits 01 power / 02 a burner lit / 04 DHW demand / 08 recirc ·
`[20]` bit `0x02` slave called in · `[25]` bit `0x04` toggles ~every 13 s (unknown) ·
`[26]` = max(60, 153 − `[13]`) at setpoint 51.5 °C, `[28] == [26] + 40`, `[27]` a function of `[26]`
whose rule depends on `[16]` — derivations known, **meaning unknown**.

## Files

- `PLAN.md` — dashboard service architecture, decisions, phased development plan.
- `navien/` — the service package. Phase 0: protocol, decode, state, rawlog, source.
  Phase 1: `service.py` (wiring, reader thread → event loop), `hub.py` (fan-out),
  `api.py` (FastAPI REST, OpenAPI at `/docs`, JSON index at `/api`). Phase 2: `ws.py`
  (`/ws` push protocol, documented in its docstring). Phase 3a/3b: `web/` — the dashboard served
  at `/` (charts are our own SVG in `web/charts.js`) (plain ES modules, no build step, no CDN). No browser or JS engine on the Pi, so
  frontend changes can only be checked by the user (dev server: `--replay … --loop
  --http-port 8765`, then http://192.168.100.181:8765).
  `venv/bin/python -m navien` serves http://<pi>:8000 from the live bus (config `./navien.toml`,
  gitignored; copy of `navien.example.toml`). `--headless` prints cycles instead;
  `--replay <file.raw[.gz]> [--speed 0|N] [--loop]` replaces the serial port (no raw logging).
  Field tables in `navien/decode.py` are the code's byte map; keep them in step with PROTOCOL_NOTES.md.
- `tests/` — `venv/bin/python -m pytest`. Runs against `captures/`, which is gitignored
  (local to the Pi), so the capture-based tests only run here.
- `raw/` (gitignored) — the service's rotating raw log, `navien_<UTC period>.raw` + `.idx`
  (frame-start timestamps). Re-parse these with `navien.source.load_chunks`.
- `packaging/` + `scripts/build-deb.sh` — the Debian package (PLAN.md §1.11): systemd unit,
  udev rule (`/dev/navien`, FTDI latency 1 ms), sysusers, default `/etc` config, maintainer
  scripts. Nothing is installed by hand any more.
- `requirements.txt` (top level), `requirements.lock` (exact runtime set bundled in the .deb),
  `requirements-dev.txt` (+ pytest, httpx2). Change a version → update the lock, run tests.

## Releases

1. Tests pass; docs (PLAN.md changelog, PROTOCOL_NOTES.md) updated; commit.
2. Tag `vX.Y.Z` (Phase N → `v0.N.0`; fixes bump the patch) and, at the end of a phase, `phase-N`.
3. `scripts/build-deb.sh` → `dist/navien-dash_X.Y.Z_arm64.deb` (refuses a dirty tree;
   `--dev` builds `…+git….dirty` test packages).
4. The user installs: `sudo apt install ./dist/navien-dash_X.Y.Z_arm64.deb`. Never run sudo
   or install packages on the user's behalf.

Installed layout: code `/opt/navien-dash`, config `/etc/navien-dash/navien.toml`, raw logs
`/var/lib/navien-dash/raw`, `journalctl -u navien-dash`, CLI `navien-dash` (same flags as
`python -m navien`). Stop the service (`sudo systemctl stop navien-dash`) before running
the dev tree or `navien-capture.py` against the port.
- `navien-capture.py` — standalone, receive-only proof-of-concept sniffer.
  `python3 navien-capture.py --port /dev/ttyUSB0 --seconds 90`.
  Writes `<prefix>.raw` (every byte) and `<prefix>.jsonl` (one record per frame: timestamp,
  header fields, hex payload, checksum/seed, `decode{}` of known fields). Prints a cadence
  summary per `src>dst/dir` stream at the end. Needs `pyserial`. It is **not** imported by
  the service and is not being refactored; the service's `navien/` package (PLAN.md) ports its
  parser and becomes the source of truth for the byte map. It cannot run while the service
  holds the serial port.
- `captures/navien_capture_20261028_123030.{raw,jsonl}` — 60 s, 360 frames, 0 checksum
  failures. Master did two burn cycles (one tap running, ~4.3 L/min); slave idle throughout.
  This is the capture `PROTOCOL_NOTES.md` is based on.
- `captures/navien_capture_20261028_125418.*` (266 frames) and `…_125728.*` (360 frames) —
  later captures, 0 checksum failures, same schedule; contents not yet written up.
  None of the `captures/` files have an `.idx`; replay synthesises timing from the schedule.
- `PROTOCOL_NOTES.md` — full findings, per-byte tables for every stream, open questions,
  references to the community repos this builds on.

## Working rules

- **Never transmit on the bus unless the user explicitly asks for that step.** The script is
  receive-only by design. When TX is eventually added: sys_type `0x02`, src `0x0F`, seed `0x62`,
  send only after ≥20 ms bus silence, target `dst 0x50`, and verify effect by watching the
  next status frames (the heater sends no ack).
- Keep raw captures; re-parse from `.raw`, never from memory of what a byte "should" be.
  The service keeps a rotated raw log permanently (PLAN.md §1.5).
- Exactly one process may own the serial port, and only its reader thread touches it.
  Run the service as a single uvicorn worker.
- **The packaged service normally owns the port.** Before any dev run against the live bus,
  check `systemctl is-active navien-dash`; if active, ask the user to
  `sudo systemctl stop navien-dash` (and to start it again afterwards). Prefer not needing
  the port at all: `--replay` a log from `/var/lib/navien-dash/raw/` (readable by all), or
  read the running service's API on :8000. Since v0.1.1 the service takes an exclusive lock
  on the port, so a second navien process fails to open it; `navien-capture.py` takes no
  lock and would still collide.
- Use frame indices (0 = F7) in code and notes; say "payload index" explicitly if used.
- Treat community decodes (single-unit NPE/NCB/NHB) as hypotheses to verify on this cascade,
  not facts. Several already differ (sys_type `0x02` vs their `0x05` templates, the current-gas
  scale, the `0x41` frame's meaning and checksum seed).

## Next steps

Build track: follow the phases in `PLAN.md` (0 foundations → 1 REST → 2 WebSocket →
3a text dashboard → 3b charts → 4 InfluxDB). Each phase is reviewed with the user before
the next starts.

Reverse-engineering track (runs alongside; the dashboard's debug view and raw log support it):

1. ~~Slave firing~~ — done from the service log (2026-10-28 18:23, PROTOCOL_NOTES §4.4.1).
   Still open: what triggers staging, and the meaning of `0x41`[26..28]; a setpoint change
   would test `[26]` = setpoint×2 − inlet×2 + 50.
2. ~~Recirculation~~ — water `[8]` = `08` confirmed (both units, ~30 min cycle). Still open:
   `[33]`, `[25]`.
3. Compare gas `[24]` and the version bytes against the installer-menu display values.
4. Only then, and only when the user asks: NaviLink-present announce packet
   (`F7 02 0F 50 10 03 4A 00 01 + cs`) as a first TX experiment. Out of scope for PLAN.md.
