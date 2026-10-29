# Navien NPE-240A2 cascade — RS-485 protocol notes

Status as of 2026-10-28. Everything marked **measured** comes from
`captures/navien_capture_20261028_123030.raw` (60 s, 420 frames, all checksums valid; the
capture script counted only 360 because it dropped the §4.6 frames as resyncs), unless
another source is named. Everything marked *community* or *upstream* comes from the reverse-engineering
projects listed in §8 and has not been verified here unless stated. §9 records an audit
against htumanyan/navien (commit `b49bdfc`) and what it changed in these notes.

Byte offsets in the §4 tables are listed as **payload index** (0 = first byte after the
6-byte header) *and* frame index; prose elsewhere says which one it means. Frame index =
payload index + 6. Code (`navien/decode.py`, see `PLAN.md`) uses frame indices only.

When the field table in `navien/decode.py` exists, it and this file must be updated together.

---

## 1. Link layer

- 19200 8N1, no parity. No request/response discipline on the single-unit bus (*community*);
  on this cascade the master imposes a strict schedule (§3).
- Frame: `F7 sys_type src dst dir len payload… checksum`.
- `sys_type`: **measured `0x02`** on both units. (*community*: `0x05` NPE single units,
  `0x04` NHB-110H, `0x06` NCB-H.) Controllers must echo the unit's own sys_type or commands
  are ignored (htumanyan/navien PR #91).
- `len` is the payload length; total frame = 6 + len + 1.
- Checksum (verified against all 360 frames and the published single-unit examples):

  ```python
  def navien_checksum(data, seed):      # data = header + payload, without checksum
      r = 0xFF
      for b in data:
          r <<= 1
          if r > 0xFF:
              r = (r & 0xFF) ^ seed
          r = (r & 0xFF) ^ b
      return r & 0xFF
  ```

  Seed is chosen by **source address**: `src 0x50 → 0x4B` (240/240 frames incl. the master's
  dir-0x10 frames), `src 0x51 → 0x62` (120/120 frames). Controller (`src 0x0F`) frames use
  `0x62` (*community*, consistent with the rule).

## 2. Addressing

| Address | Role |
|---|---|
| `0x50` | Unit 0 / master. Also the "water" status destination. |
| `0x51`…`0x5F` | Cascade units 1–15 (`0x50 + index`). Only `0x51` exists here. |
| `0x0F` | Controller / NaviLink / us. Also the "gas" status destination. |
| `0x01` | Panel/peer endpoint used in handshake traffic (*community*). |

Direction byte: `0x90` status, `0x10` control/cascade.

## 3. Schedule (measured)

Cycle period 998.6 ms mean (991.7–1007.9, σ 7.9 ms). **Seven** frames per cycle, fixed order
(the seventh, `50→52`, was missed until 2026-10-28 because the capture script's parser
rejected its sys_type; see §4.6):

Offsets are measured from the start of the cycle's `0x41` frame (re-measured 2026-10-28 from
the timestamps in all three captures; an earlier version of this table gave gaps between
frames instead of offsets for the master frames). Timestamps are quantised to 16 ms
(FTDI default latency timer), so treat these as ±16 ms.

| offset (min–max, median) | src→dst/dir | len | type byte | what |
|---|---|---|---|---|
| 0 ms | 50→51 / 10 | 34 | `41` | master → slave (§4.4) |
| 48–64 ms, 64 | 51→50 / 90 | 34 | `42` | slave water |
| 48–128 ms, 112 | 51→0F / 90 | 42 | `45` | slave gas (the early ones are cycles where it replaces the water frame) |
| 192–224 ms, 208 | 50→52 / 10 | 3 | `40` | master → `0x52`, **sys_type `0x13`** (§4.6). Timed from the first live raw log (`raw/navien_20261028T20Z`, 150 cycles) |
| 384–416 ms, 400 | 50→50 / 90 | 34 | `42` | master water |
| 592–624 ms, 592 | 50→0F / 90 | 42 | `45` | master gas |
| 784–816 ms, 800 | 50→50 / 10 | 34 | `44` | master (§4.5) |

Frame on-wire time at 19200 8N1 is ~21 ms (len 34) / ~26 ms (len 42).

Slave anomaly: in 4 of 60 cycles the slave sent its gas frame twice and no water frame
(64 gas / 56 water). The two later captures show it too (45/43 and 61/59). Cause unknown;
the decoder and state model must tolerate it.

## 4. Frame contents

Notation for the tables: `const XX` = identical in every frame of the capture; `VAR` = varied.

### 4.1 Water frame — `src unit → dst 0x50`, dir 0x90, len 34, type `0x42`

"upstream" = htumanyan/navien `WATER_DATA` struct (§9), whose field names are frame indices.
"upstream cascade vectors" = the two 240A2 slave water frames in its `src/checksum.cpp`.

| payload | frame | master | slave | meaning |
|---|---|---|---|---|
| 0 | 6 | `42` | `42` | packet type |
| 1 | 7 | `00` | `20` | **role marker** (differs master/slave; upstream cascade vectors also show `20` on the slave) |
| 2 | 8 | `00`/`20` | `00` | **heating mode** (upstream enum, not a bit field): `00` idle, `08` DHW recirculating, `10` space heating, `20` DHW demand, `40` indirect tank. Measured: `20` only while flow > 0, but 5 frames with 0.4–1.3 L/min (short blips, onset) still read `00` — a flow threshold, not "any flow" |
| 3 | 9 | `25` | `25` (once `35`) | **system power** (upstream): bit `0x01` = power on; bit `0x20` set when *no* HotButton is fitted (`25`/`05` on, `20`/`00` off). `0x10` (slave, once) unknown |
| 4 | 10 | VAR | `14` | **operating state / burner stage** — see §5 |
| 5 | 11 | `67` | `67` | DHW setpoint °C×2 → 51.5 °C (≈125 °F display) |
| 6 | 12 | VAR 44–53.5 °C | 40.5/41 °C | outlet temp °C×2 |
| 7 | 13 | VAR 29.5–42 °C | 40.5 °C | inlet temp °C×2 |
| 8–9 | 14–15 | `00 00` | `00 00` | **error code**, 16-bit LE (upstream, PR #58). 0 = no error. Not yet seen non-zero here |
| 10 | 16 | `00` | `00` | **error level** (upstream) |
| 11 | 17 | VAR 0–24 | `00` | **operating capacity, %×2** — see §5 (upstream agrees) |
| 12 | 18 | VAR 0–48 | `00` | flow L/min×10 (upstream agrees) |
| 13 | 19 | `00` | `00` | |
| 14–15 | 20–21 | `A0 BE` | `A0 BE` | unknown constant. Same on the upstream 240A2 cascade; `88 C2` on single NPE, `58 B1` on NCB-H → probably a model/rating code |
| 16 | 22 | `00` | `00` | |
| 17 | 23 | `20` | `20` | unknown (`20` on all 240A/A2 examples, `89` NCB-H) |
| 18 | 24 | `02` | `02` | **system status** bits (upstream): `0x08` display °C (clear → °F, matches our display); `0x01` recirc *internal* scheduled; `0x02` recirc *external* scheduled (i.e. schedule ceded to a NaviLink-type controller). Neither set → recirc off or HotButton mode |
| 19 | 25 | `00` | `00`/`04` | **toggling bit** on slave, ~13 s period, in lockstep with `0x41`[19] (§4.4). Upstream: unknown; reads `0C` on their 240A2 slave |
| 20 | 26 | `01` | `00` | unknown. Upstream cascade vectors: `00` and `01` on the *same* slave, so **not** a role marker |
| 21 | 27 | `00`/`01` | `00` | **flame on** — 1 exactly while stage = `0x33`. Upstream: `boiler_active`, bit `0x01` |
| 22–23 | 28–29 | `00 00` | `00 00` | upstream: "counter A" 16-bit LE, meaning unknown (`06`, `04` on their slave) |
| 24–25 | 30–31 | `02 00` | `03 00` | upstream: "counter B" 16-bit LE, meaning unknown (`12`, `0A` on their slave). **Not a role marker** — the earlier reading here was an artefact of short captures |
| 26 | 32 | `00` | `00` | |
| 27 | 33 | `02` | `02` | **recirculation flags** (upstream): `0x01` HotButton recirc triggered/active; `0x02` scheduled recirc enabled |
| 28–33 | 34–39 | `00` | `00` | |

Upstream derives *recirc running* on NPE units as `heating_mode == 0x08` **or**
`[33] & 0x01`, and *recirc mode* from `[24]` bits 0/1 plus gas `[21]` bit 2.

### 4.2 Gas frame — `src unit → dst 0x0F`, dir 0x90, len 42, type `0x45`

Field names from upstream `GAS_DATA`; the third upstream column is their 240A2 cascade slave
gas vector (`TEST_VEC_240A2_SRC_51_3`), which is the closest available comparison.

| payload | frame | master | slave | upstream 240A2 slave | meaning |
|---|---|---|---|---|---|
| 0 | 6 | `45` | `45` | `45` | packet type |
| 1 | 7 | `00` | `00` | `00` | |
| 2 | 8 | `0C` | `0C` | `0C` | **device type** (upstream enum): `0x0C` = 12 = **CAS_NPE2** (cascaded NPE-A2) |
| 3 | 9 | `02` | `02` | `02` | unknown (`01` on NCB-H) |
| 4 | 10 | `0C` | `0C` | `0C` | **controller version**, decimal digits → "1.2" |
| 5 | 11 | `07` | `07` | `07` | unknown |
| 6 | 12 | `1B` | `1B` | `1B` | **panel version**, decimal digits → "2.7" |
| 7 | 13 | `00` | `00` | `00` | space-heat setpoint °C×2 (combi only) |
| 8 | 14 | `67` | `67` | `5C` | DHW setpoint °C×2 |
| 9 | 15 | VAR | `51`/`52` | `5B` | outlet °C×2 (often ±0.5 °C from the water-frame value) |
| 10 | 16 | VAR | `53` | `44` | inlet °C×2 |
| 11 | 17 | `00` | `00` | `00` | SH outlet °C×2 (combi only) |
| 12 | 18 | `00` | `00` | `00` | SH return °C×2 (combi only) |
| 13 | 19 | `00` | `00` | `00` | outdoor temp, sign-magnitude whole °C; upstream says `9E` = no probe. **Reads `00` here and on the upstream 240A2** — treat `00` as "no probe" on this model |
| 14 | 20 | VAR 0–0x5E | `00` | `00` | **heat capacity, %×2** (upstream). Verified here: `[20]/2` = current gas ÷ 504 (i.e. % of the 50 400 kcal/h rating) within −0.6…+0.1 points over 61 frames. Reads 0 during ignition (stages 2C/2D) |
| 15 | 21 | `01` | `01` | `01` | **system status 2** (upstream): bit `0x01` display °F (consistent with water[24] `0x08` clear); bit `0x04` HotButton mode enabled (clear here) |
| 16–17 | 22–23 | VAR | `00 00` | `00 00` | **current gas use, kcal/h, 16-bit LE**. See note below on the upstream ×10 claim |
| 18–19 | 24–25 | `1E 00` | `32 00` | `30 00` | **cumulative gas**, 16-bit LE, ×0.1 m³ (upstream) → 3.0 / 5.0 m³ |
| 20–21 | 26–27 | `00 00` | `00 00` | `00 00` | unknown |
| 22–23 | 28–29 | `01 00` | `01 00` | `08 00` | **days since install** (upstream) → 1 |
| 24–25 | 30–31 | `04 00` | `04 00` | `16 00` | **DHW use count** (upstream; "in 10-use increments" per a comment, the decoder publishes it raw) |
| 26 | 32 | `00` | `00` | `7D` | unknown |
| 27–28 | 33–34 | `00 00` | `00 00` | `2F 00` | upstream: "counter C", unknown |
| 29 | 35 | `00` | `00` | `00` | |
| 30–31 | 36–37 | `00 00` | `00 00` | `04 00` | **total operating time, hours** (upstream) |
| 32–33 | 38–39 | `00 00` | `00 00` | `00 00` | **cumulative DHW usage hours** (upstream) |
| 34–35 | 40–41 | `00 00` | `00 00` | `00 00` | cumulative SH usage hours (combi only) |
| 36–37 | 42–43 | `AA 48` | `AA 48` | `AA 48` | unknown constant; identical on the upstream 240A2, `A6 49` single NPE, `78 3C` NCB-H |
| 38–39 | 44–45 | `00` | `00` | `00` | |
| 40 | 46 | `01` | `00` | `00` | **role marker** (upstream: `01` on single NPE, `00` NCB-H — i.e. "1 = master/standalone") |
| 41 | 47 | `00` | `00` | `00` | |

**Counters [24..37] are suspiciously small** on both installations (here: 1 day, 3–5 m³,
0 operating hours). Either both cascades are newly installed, or these counters mean
something else on CAS_NPE2. Compare with the installer-menu values (open question 3).

**Current-gas scale disagreement.** Upstream's YAML says the raw value is "10× kcal/h" and
multiplies by 10 for its kW sensor. Our data contradicts that for this model: raw values
peak at 23 647 on light-off, which is 47 % of the 50 400 kcal/h (199 900 BTU/h) rating —
exactly what gas[20] reports (`0x5E`/2 = 47 %). ×10 would be 4.7× the rated input. We keep
**raw = kcal/h**.

### 4.3 Checksum/role summary

Two bytes reliably distinguish master from slave in the status frames: water[7] (`00`/`20`)
and gas[46] (`01`/`00`). Water[26] and water[30] were listed here before; upstream cascade
vectors show both varying on a slave, so they are not role markers. Use the source address
(`0x50` + index) to identify the unit.

### 4.4 Master → slave frame — `50→51`, dir 0x10, len 34, type `0x41` (measured)

The ESPHome project (PR #65) treats frames with this shape as the master relaying the
*sub-unit's* data. On this installation it carries **cascade-wide values**: the number of
units in DHW demand, their total flow and their mean capacity, inlet and outlet, one cycle
late. With only the master drawing, as in the first capture, these equal the master's own
values, which is why this section first read it as "the master's state from the previous
cycle". The 2026-10-28 service logs (§4.4.1) show the difference once the slave joins.

Upstream's current code ignores every dir-0x10 frame not from `0x0F` (commit `0add04d`
"…observed in cascade setup for 240A2 between navien units"), so it decodes none of this.
Its `checksum.cpp` does contain one such frame from another 240A2 cascade
(`TEST_VEC_240BE_SRC_51`, labelled seed `0x62` with a "TODO: verify"). Recomputing it: the
stored checksum `BE` matches seed **`0x4B`**, not `0x62` — consistent with our seed-by-source
rule. That frame has the same layout as ours: `[9]=01` idle, `[20]=01`, `[29..37]` =
`0A 0E 00 06 02 00 00 00 C2` identical, `[25]=0C`, and A/B/C = 67/32/107, so **C = A + 40
holds on a second installation**.

| payload | frame | value | meaning |
|---|---|---|---|
| 0 | 6 | `41` | packet type |
| 1 | 7 | `00` | |
| 2 | 8 | `00`/`20` | consumption flag — mirrors master water[2], one cycle late |
| 3 | 9 | `01`/`03`/`05`/`07`/`0B` | status bits: `0x01` power, `0x02` a burner is lit (also set, as `03`, while only the slave burns), `0x04` DHW demand, `0x08` recirculation (`0B` = recirc + flame) |
| 4 | 10 | `00` | |
| 5 | 11 | `67` | setpoint °C×2 |
| 6 | 12 | VAR | outlet °C×2 — mean over the units in DHW demand (= master's when only it draws); **holds the last draw's value when idle** (52.5 °C held while live outlet read 44.5 °C) |
| 7 | 13 | VAR | inlet °C×2 — same behaviour |
| 8–9 | 14–15 | `00` | |
| 10 | 16 | `00`/`01`/`02` | **number of units in DHW demand** (water[8] = `20`), one cycle late. `00` during recirculation even with burners lit |
| 11 | 17 | VAR | capacity %×2 — mean over the units in demand |
| 12 | 18 | VAR | flow L/min×10 — **total** over the units in demand |
| 13 | 19 | `00` | |
| 14 | 20 | `01`/`03` | bit `0x02` = **slave called in**: set 2 s before the slave's flow started (18:23:23), cleared ~1 min after both units stopped. One event so far |
| 15–16 | 21–22 | `00` | |
| 17 | 23 | `20` | |
| 18 | 24 | `02` | |
| 19 | 25 | `00`/`04` | **toggling bit**, ~13 s period, flips in the same cycle as slave water[19] |
| 20 | 26 | VAR 60–110 | **A** = 153 − [13], floored at 60 (§4.4.1). Only setpoint 51.5 °C (`67`) seen, so 153 may be setpoint×2 + 50 |
| 21 | 27 | VAR 30–48 | **B** — a function of A whose rule depends on [16] (§4.4.1) |
| 22 | 28 | VAR 100–150 | **C** — always exactly A + 40 |
| 23 | 29 | `0A` | |
| 24 | 30 | `0E` | |
| 25 | 31 | `00` | |
| 26 | 32 | `06` | |
| 27 | 33 | `02` | |
| 28–30 | 34–36 | `00` | |
| 31 | 37 | `C2` | |
| 32–33 | 38–39 | `00` | |

#### 4.4.1 Slave firing and recirculation (service raw logs, 2026-10-28 15:43–19:10 CDT)

Source: `/var/lib/navien-dash/raw/navien_20261028T20Z` … `navien_20261029T00Z`, 12 377
`0x41` frames, 0 checksum failures. All statements below hold for every frame except the
one-cycle lag at transitions.

**Two units in DHW demand (18:23:05–18:30:13).** Master demand started at 18:23:05 and its
flow rose to 16.8 L/min (capacity 56 %). At 18:23:23 `0x41`[20] went `01`→`03`; at 18:23:25
the slave reported flow (stage `14`, 2.8 L/min) and the master's flow fell. Within ~5 s the
flow was split evenly (≈9.5 / 9.5 L/min). Slave stages: `14` → `20` → `2C` → `2D` → `33`, the
same sequence as the master's. From then on `0x41`[16] = 2, [18] = sum of both flows,
[17]/[12]/[13] = mean of both units' capacity/outlet/inlet. At the end the master stopped one
cycle before the slave, and both went straight from `3C` into a recirculation run (18:30:14).

**A, B, C.** Across all frames, A = `max(60, 153 − [13])` and C = A + 40. A is therefore not a
hidden load figure but derived from the (mixed) inlet temperature; with the setpoint fixed at
51.5 °C (`67` = 103) every frame fits both "153 − inlet" and "setpoint×2 − inlet×2 + 50", i.e.
ΔT×2 + 50. A setpoint change would separate the two. B follows A with one of two rules:
- [16] ≤ 1: `B = (3A + 4) // 8 + 7` (≈ 0.375·A + 7.5)
- [16] = 2: `B = A // 2 − 10`

The rule switched in the same cycle as [16] (18:23:25 and 18:30:13). Meaning still unknown;
possibly a per-unit firing target the slave reads.

**Recirculation (water[8] = `08`).** 10 runs on the master and 7 on the slave (15:43–19:10;
an earlier count of 16 + 8 was wrong), each 1–3 min,
at a fixed flow per unit (master ≈15.2 L/min, slave ≈13.5 L/min), with inlet reading warm
loop water (37–46 °C). The slave's runs repeat every ~31.5 min (15:53, 16:24, 16:56, 17:28,
18:00, …); the master's mostly start right after a DHW draw ends. This confirms the upstream
enum value `08` = DHW recirculating.

**Recirculation trigger: a 30-minute timer, not temperature** (Influx events,
2026-10-28 15:45 – 10-29 07:18, 71 runs). Each unit keeps its own timer:
- A run starts 1798–1799 s after that unit's previous run *ended*, every time (all 52 timer
  runs). The start-to-start interval therefore varies with run length (e.g. 117.8 s run →
  next start 1916 s later, 73.9 s run → 1872 s). Start inlet varies 39–43.5 °C. A
  temperature threshold would not give a constant off time to ±1 s, independent of the
  night's ambient.
- Every DHW draw on a unit (17 of 17) goes straight into a recirc run (water[8] `20` → `08`
  within 0–1 s, inlet still cold, 21–41 °C). That run restarts the 30-min timer. A timer
  expiring during a draw waits for the draw to end (master 10-29 06:16:40).
- The two units drifted into step overnight (starts within 1–10 s) only because their
  timers happened to align; nothing links them.
- Runs end with the outlet at the setpoint (51–52.5 °C); what ends a run is not yet
  analysed. One cluster of four short slave runs 5 s apart (19:32–19:33) is unexplained.
- Cost over the window at $0.65/therm: timer runs ≈0.82 therm/day ($0.53), post-draw runs
  ≈0.23 therm/day ($0.15).
- water[24] = `02` and [33] = `02` throughout; on upstream's reading that is "scheduled
  recirc enabled", which fits a firmware schedule/interval setting. The installer-menu
  setting behind the 30 min is unverified. During recirc `0x41`[16] = 0 and [9] = `0B`. The
screenshot of the dashboard at 19:00–19:02 shows one of these, not a tap draw.

### 4.5 Master `0x44` frame — `50→50`, dir 0x10, len 34 (measured)

Payload constant: `44 00 14 1E 0A 00 00 … 00`. Values 20, 30, 10. Possibly cascade or
timing configuration (e.g. seconds), possibly a poll slot for an absent device. Unknown.

### 4.6 Master → `0x52` frame, sys_type `0x13` (measured, found 2026-10-28)

`F7 13 50 52 10 03 40 00 04 A6` — byte-identical once per cycle in all three captures and
the live log. Checksum `A6` validates with seed `0x4B` (src `0x50`), so it is a real frame,
not noise. `navien-capture.py` (and upstream, whose firmware-derived check requires
sys_type 1..9) discard it, which is why the capture summaries showed 2 "resyncs" per cycle
and only six streams. `navien/protocol.py` accepts any sys_type.

It sits right after the slave's gas frame, in the slot where a unit 2 would answer.
Hypothesis: the master polls the next cascade address (`0x52`) to discover added units,
using a different sys_type for that discovery traffic. Nobody answers here. Payload
`40 00 04`: type `0x40`, meaning of `00 04` unknown.

## 5. Burn cycle as observed (master, one tap ≈ 4.3 L/min)

Cycle-by-cycle from the capture (times are seconds into the capture):

| t | stage | flame | cap % | kcal/h | gas[20] | notes |
|---|---|---|---|---|---|---|
| 0–10 | `14` standby | 0 | 0 | 0 | 0 | brief 0.4–0.5 L/min blips at 3 s and 8 s, capacity 1–2 % |
| 11 | `14` | 0 | 4 | 0 | 0 | flow 3.1 L/min, consumption flag set |
| 12 | `20` demand | 0 | 5.5 | 0 | 0 | |
| 13 | `29` pre-purge | 0 | 5.5 | 0 | 0 | |
| 14–15 | `2C` flame-on | 0 | 6 | 12 075 | 0 | fixed ignition gas rate |
| 16 | `2D` ramp | 0 | 5.5 | 23 647 | 0x5E | max fire |
| 17–29 | `33` combustion | **1** | 5.5 | 11 671 → 6 640 | tracks | modulating down; outlet 47 → 49 °C |
| 30–31 | `3C` flame-off | 0 | 5.5 | 0 | 0 | outlet overshoots to 50 °C |
| 32–39 | `46` post-purge | 0 | 6–8 | 0 | 0 | outlet 50.5 → 52.5 °C; inlet now 37 °C |
| 40 | `29` | 0 | 8 | 0 | 0 | re-light; inlet 36 °C |
| 41–42 | `2C` | 0 | 8 | 12 024 | 0 | |
| 43–59 | `33` | **1** | 8 → 12 | 13 382 → 6 590 | tracks | inlet falls to 29.5 °C, capacity rises to 12 % |

Stage enumeration. Upstream (`OPERATING_STATE`, from ificator/navien_ha) is a superset of
the NavienManager list this note was first written from, and **disagrees on `0x20`**:

| code | upstream name | earlier name here | seen here |
|---|---|---|---|
| `14` | standby | standby | yes |
| `15` | demand | — | no |
| `20` | **startup** | demand | yes (1 cycle, right after flow starts) |
| `28` | pre-purge 1 | — | no |
| `29` | pre-purge 2 | pre-purge | yes |
| `2A` | pre-ignition | — | no |
| `2B` | ignition | ignition | yes (service log) |
| `2C` | flame on | flame on | yes |
| `2D` | ramp up | ramp-up | yes |
| `33` | active combustion | active combustion | yes |
| `34` | water adjustment valve operation | water adjustment | yes (service log; alternates with `33` in long burns) |
| `3C` | flame off | flame off | yes |
| `46` | post-purge 1 | post-purge 1 | yes |
| `47` | post-purge 2 | post-purge 2 | yes (service log) |
| `48` | shutting down | — | no |
| `49` | DHW wait / set-point match | DHW wait | yes (service log; ~80 s after each burn, then `14`) |

Use the upstream names; `0x20` → "startup" fits what we see (first cycle after flow begins).

Burn sequence in the 2026-10-28 service log (18 burns): `14` → `20` → `29` (→ `2B`) → `2C` →
`2D` → `33`/`34` … → `3C` → `46` → `47` → `49` (~80 s) → `14`. A new demand during post-purge
or DHW wait re-ignites from there (`46`/`49` → `29`/`2B`) without passing standby. One
aborted start: master 18:18:29, `20` → `3C` after 1 s with no flame, post-purge, then
`29` → `2C` 10 s later. `events.py` counts `20`–`34` as a burn and flags a burn without
flame as `failed`.

`0x49` (DHW wait / set-point match) first seen 2026-10-28 15:24 local on the **slave**: no
flow, heating mode idle, outlet 49 °C / inlet 45 °C, up from 41.5 / 41 °C at 15:13. Nothing
was logging in between, so the slave probably fired unobserved (`raw/navien_20261028T20Z.raw`).

Capacity sanity check: 4.2 L/min × ΔT 9.5 K ≈ 2 400 kcal/h delivered to water ≈ 5 % of the
unit's 50 400 kcal/h (199 900 BTU/h) rating, vs. 5.5 % reported — consistent with %×2
encoding, allowing for heat-exchanger warm-up.

The slave never left `0x14` and reported 0 kcal throughout.

## 6. Open questions, ranked

1. `0x41`[26..28] (A/B/C) — A and C are now explained by inlet temperature (§4.4.1), B by A
   plus the number of units in demand; what they *mean* is open. A setpoint change would
   test whether A = setpoint×2 − inlet×2 + 50. What triggers `0x41`[20] / slave staging
   (master capacity 56 % and 16.8 L/min at the one event seen) needs more two-unit draws.
2. The `0x04` toggling bit (slave water[19] / `0x41`[19]) — ~13 s period, not stage-linked.
   Recirc pump? Heartbeat? Capture a recirculation event and a long idle period.
3. Gas counters (frame [24..37]: cumulative gas, days since install, DHW use count,
   operating hours) — upstream gives a full map, but values here are tiny (1 day, 3–5 m³,
   0 h). When were the units installed? Compare with the installer-menu / info screens, and
   watch whether "days since install" ticks over at midnight in a long capture.
4. Version bytes: gas[10] = `0C` → controller "1.2", gas[12] = `1B` → panel "2.7" per
   upstream. Confirm against the display's version screen.
5. Slave's duplicate-gas / missing-water cycles — pattern or noise?
6. The `0x44` frame — does it ever change (power cycle, installer-menu change, error)?
7. Error codes — upstream now decodes water[14..15] (code, LE) and [16] (level). Not yet seen
   non-zero here. Verify the next time a unit shows an error on its display; a deliberate
   error (e.g. briefly closing the gas valve) only if the user is willing.
8. Water[20..21] `A0 BE` and gas[42..43] `AA 48` — identical on the upstream 240A2 cascade,
   different on other models, so probably model/rating codes.
9. Water[28..31] "counters A/B" — meaning unknown upstream too. Ours read `00 00` / `02 00`
   (master) and `00 00` / `03 00` (slave); a long capture may show them change.
10. Gas[19] outdoor temp reads `00`, not upstream's `9E` "no probe" value. Probably model-specific.
11. The sys_type `0x13` `50→52` frame (§4.6) — discovery poll? Does its payload change when
    the slave is powered off, or at master power-up?

## 7. Writing to the bus (*community*, untested here)

Control frames: `F7 sys src=0x0F dst=0x50 dir=0x10 len=0x0C 4F … cs`, seed `0x62`
(header order is src then dst). Upstream hard-codes sys `0x05` in its command templates; we
must use `0x02`. Fields (frame index), all others `00`:

| byte | meaning (upstream `navien_proto.h` / `navien_link.cpp`) |
|---|---|
| `[8]` | power: `0A` on, `0B` off, `00` no change |
| `[9]` | DHW setpoint °C×2 (`round(t*2)`), `00` = no change |
| `[10]` | SH setpoint °C×2 (combi only; verified on NCB-H), `00` = no change |
| `[11]` | recirculation: `01` HotButton press (followed by one frame with `00` = release), `08` scheduled-recirc on, `10` scheduled-recirc off |
| `[12]` | `D9` in upstream's legacy `RECIRC_ON_CMD`, `00` in the newer scheduled-recirc commands — meaning unknown |

The earlier claim here that `[12]` is a "token echoed in the ack" does **not** come from
upstream; source unknown, treat as unverified.

Upstream sending behaviour (current code): every command is queued **2×** (not ~6×), and
one queued frame is written **immediately after each complete frame received**. When no
command is pending it sends the NaviLink-present announce `F7 sys 0F 50 10 03 4A 00 01 cs`
after every received frame, and stops announcing if it sees another device's announce (then
it only transmits right after that device's announce). There is no explicit ≥20 ms
silence wait in the current code; on our bus the master's schedule (§3) leaves ~150 ms gaps
after each frame, so "right after a frame" and "after ≥20 ms silence" both land in a gap.
Heater needs NaviLink enabled in the installer menu. On the original NPE-240A power/setpoint
commands are ignored; on the NPE-240A2 they are reported to work (issue #97).
Whether a slave can be addressed directly (`dst 0x51`) is unknown; all community
implementations target `0x50`.

## 8. References

- htumanyan/navien — ESPHome component, protocol docs (`doc/rs485.md`, `doc/payload.md`,
  `doc/protocol_data_flow.md`), PRs #65 (cascade frames), #81 (SH setpoint), #82 (quiet-bus TX),
  #91 (sys_type), issue #97 (NPE-240A vs A2 commands). https://github.com/htumanyan/navien
  **Audited 2026-10-28 at commit `b49bdfc` (2026-10-22)** — see §9. Most useful files:
  `esphome/components/navien/navien_proto.h` (field structs, frame-index names),
  `navien.cpp` (scaling, enums), `navien_link.cpp` (checksum seed choice, TX behaviour),
  `src/checksum.cpp` (test vectors, including four from a 240A2 cascade).
- dacarson/NavienManager — ESP32 HomeKit; `Navien.cpp` has the most complete field list and
  the stage enumeration. https://github.com/dacarson/NavienManager
- evanjarrett/ESPHome-Navien — original byte-offset decoding of water/gas/control packets.
  https://github.com/evanjarrett/ESPHome-Navien
- Home Assistant thread "Navien, ESP32 Navilink interface":
  https://community.home-assistant.io/t/navien-esp32-navilink-interface/720567
- Waveshare USB TO RS485 (C): https://docs.waveshare.com/USB_TO_RS485_C

## 9. Audit against htumanyan/navien (2026-10-28, commit `b49bdfc`)

Consistent with our findings:
- 19200 8N1, `F7` marker, `len` = payload length, checksum algorithm.
- Seed selection: upstream picks `0x4B` iff `src == 0x50`, else `0x62` — the same
  seed-by-source rule (so the slave's `0x62` seed is *not* a disagreement with upstream).
- sys_type `0x02`: upstream's own 240A2 cascade test vectors also carry `0x02`. Its docs
  still say "always 0x05" and its command templates hard-code `0x05`.
- Cascade addressing: units are `0x50 + index`; upstream filters status frames by source.
- Water [11]–[13], [17], [18] scales; gas [14]–[16], [22..23] positions; water [24] `0x08`
  display-units bit; water [33] recirc bits; water [27] flame.
- Version bytes `0C 02 0C 07 1B`, water `A0 BE`, gas `AA 48` and the whole `0x41` layout are
  byte-identical on upstream's 240A2 cascade vectors.

New from upstream (folded into §4/§5/§7 above):
- Water [8] is a heating-mode enum; [9] power bit 0 + "no HotButton" bit 5; [14..16] error
  code/level; [24] recirc-schedule bits; [28..31] two unknown counters.
- Gas [8] device type (`0C` = CAS_NPE2), [10]/[12] controller/panel versions, [13], [17..19]
  combi/outdoor temps, [20] heat capacity %×2, [21] system status 2, [24..41] counters.
- Stage enum has more states, and names `0x20` "startup", not "demand".
- Command byte [11] values for HotButton and scheduled recirculation.

Corrections to our own notes:
- Water [26] and [30] are not role markers (they vary on upstream's slave).
- Gas [20] is heat capacity %×2, not "kcal ÷ 256" (that was a coincidence of scale).
- Gas [24] cumulative gas is 16-bit LE `[24..25]`, not a possible 32-bit at `[24..27]`.

Where we disagree with upstream, and keep our reading:
- Current gas `[22..23]` is kcal/h on the NPE-240A2, not 10× kcal/h (§4.2 note).
- Upstream labels its `50→51` cascade test vector as seed `0x62`; its checksum actually
  matches `0x4B` (§4.4).
- Upstream says inter-unit dir-0x10 frames "have some other checksum algorithm"; they use
  the normal algorithm with the source's seed (all 120 such frames per capture validate).

Not covered by upstream at all: the `0x41` and `0x44` frame contents, the sys_type `0x13`
`50→52` frame (upstream's sys_type 1..9 check would drop it), the bus schedule
(upstream describes the bus as unscheduled full duplex, which is not true on a cascade),
and the slave duplicate-gas cycles.
