# Navien NPE-240A2 cascade — RS-485 protocol notes

Status as of 2026-10-28. Everything marked **measured** comes from
`captures/navien_capture_20261028_123030.jsonl` (60 s, 360 frames, all checksums valid,
no resyncs). Everything marked *community* comes from the single-unit reverse-engineering
projects listed at the end and has not been verified here unless stated.

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

Cycle period 998.6 ms mean (991.7–1007.9, σ 7.9 ms). Six frames per cycle, fixed order:

Offsets are measured from the start of the cycle's `0x41` frame (re-measured 2026-10-28 from
the timestamps in all three captures; an earlier version of this table gave gaps between
frames instead of offsets for the master frames). Timestamps are quantised to 16 ms
(FTDI default latency timer), so treat these as ±16 ms.

| offset (min–max, median) | src→dst/dir | len | type byte | what |
|---|---|---|---|---|
| 0 ms | 50→51 / 10 | 34 | `41` | master → slave (§4.4) |
| 48–64 ms, 64 | 51→50 / 90 | 34 | `42` | slave water |
| 48–128 ms, 112 | 51→0F / 90 | 42 | `45` | slave gas (the early ones are cycles where it replaces the water frame) |
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

| payload | frame | master | slave | meaning |
|---|---|---|---|---|
| 0 | 6 | `42` | `42` | packet type |
| 1 | 7 | `00` | `20` | **role marker** (differs master/slave) |
| 2 | 8 | `00`/`20` | `00` | flags: `0x20` consumption (set exactly while flow > 0); `0x08` recirc running (*community*, not seen) |
| 3 | 9 | `25` | `25` (once `35`) | power: low nibble 5 = on (*community*, consistent); high nibble unknown |
| 4 | 10 | VAR | `14` | **burner stage** — see §5 |
| 5 | 11 | `67` | `67` | DHW setpoint °C×2 → 51.5 °C (≈125 °F display) |
| 6 | 12 | VAR 44–53.5 °C | 40.5/41 °C | outlet temp °C×2 |
| 7 | 13 | VAR 29.5–42 °C | 40.5 °C | inlet temp °C×2 |
| 8–10 | 14–16 | `00` | `00` | |
| 11 | 17 | VAR 0–24 | `00` | **operating capacity, %×2** — see §5 |
| 12 | 18 | VAR 0–48 | `00` | flow L/min×10 |
| 13 | 19 | `00` | `00` | |
| 14–15 | 20–21 | `A0 BE` | `A0 BE` | unknown constant (single-unit example shows `88 C2` here) |
| 16 | 22 | `00` | `00` | |
| 17 | 23 | `20` | `20` | unknown |
| 18 | 24 | `02` | `02` | status bits: `0x08` metric display (clear → imperial, matches display); `0x02` = recirc mode bit (*community*) |
| 19 | 25 | `00` | `00`/`04` | **toggling bit** on slave, ~13 s period, in lockstep with `0x41`[19] (§4.4) |
| 20 | 26 | `01` | `00` | unknown (differs master/slave) |
| 21 | 27 | `00`/`01` | `00` | **flame on** — 1 exactly while stage = `0x33` |
| 22–23 | 28–29 | `00` | `00` | |
| 24 | 30 | `02` | `03` | **role/index marker** (differs master/slave) |
| 25–26 | 31–32 | `00` | `00` | |
| 27 | 33 | `02` | `02` | bit `0x02` = recirculation enabled (*community*) |
| 28–33 | 34–39 | `00` | `00` | |

Community fields not yet located here: `operation_time` (hi/lo), `system_active`.

### 4.2 Gas frame — `src unit → dst 0x0F`, dir 0x90, len 42, type `0x45`

| payload | frame | master | slave | meaning |
|---|---|---|---|---|
| 0 | 6 | `45` | `45` | packet type |
| 1 | 7 | `00` | `00` | |
| 2–6 | 8–12 | `0C 02 0C 07 1B` | same | probably controller/panel version bytes (*community* puts versions at the start) — check against display info menu |
| 7 | 13 | `00` | `00` | |
| 8 | 14 | `67` | `67` | setpoint °C×2 |
| 9 | 15 | VAR | `51`/`52` | outlet °C×2 (often ±0.5 °C from the water-frame value) |
| 10 | 16 | VAR | `53` | inlet °C×2 |
| 11–13 | 17–19 | `00` | `00` | |
| 14 | 20 | VAR 0–0x5E | `00` | **coarse burner power** ≈ kcal ÷ 256; reads 0 during stages 2C/2D (ignition) and lags the kcal field |
| 15 | 21 | `01` | `01` | |
| 16–17 | 22–23 | VAR | `00 00` | **current gas use, kcal/h, little-endian**: 12 075 fixed during ignition, 23 647 peak on light-off, modulating 6 500–8 300 in steady combustion |
| 18 | 24 | `1E` | `32` | *community*: lifetime gas ×0.1 m³. Reads 3.0 / 5.0 m³ — too low to be lifetime; **treat as unverified** |
| 19–21 | 25–27 | `00` | `00` | (if [18] is 32-bit LE, these are its upper bytes) |
| 22 | 28 | `01` | `01` | |
| 23 | 29 | `00` | `00` | |
| 24 | 30 | `04` | `04` | |
| 25–35 | 31–41 | `00` | `00` | |
| 36–37 | 42–43 | `AA 48` | `AA 48` | unknown constant, same on both units |
| 38–39 | 44–45 | `00` | `00` | |
| 40 | 46 | `01` | `00` | **role marker** |
| 41 | 47 | `00` | `00` | |

Community fields not located here: cumulative water usage, total operating time, days since
install, DHW use count. Candidates: the zero runs at 25–35, or [18..21] if it is not gas.

### 4.3 Checksum/role summary

Three bytes distinguish master from slave in the status frames: water[1] (`00`/`20`),
water[24] (`02`/`03`), gas[40] (`01`/`00`). None of them is simply the unit index.

### 4.4 Master → slave frame — `50→51`, dir 0x10, len 34, type `0x41` (measured)

The ESPHome project (PR #65) treats frames with this shape as the master relaying the
*sub-unit's* data. On this installation the content is the **master's own state from the
previous cycle**, apparently pushed to the slave for cascade coordination:

| payload | frame | value | meaning |
|---|---|---|---|
| 0 | 6 | `41` | packet type |
| 1 | 7 | `00` | |
| 2 | 8 | `00`/`20` | consumption flag — mirrors master water[2], one cycle late |
| 3 | 9 | `01`/`05`/`07` | status: `01` idle, `05` demand (stages 20, 29, 2C, 2D, 3C, 46), `07` combustion (stage 33). Bits: `0x04` demand, `0x02` flame |
| 4 | 10 | `00` | |
| 5 | 11 | `67` | setpoint °C×2 |
| 6 | 12 | VAR | outlet °C×2 — equals master water[6] while flowing; **holds the last draw's value when idle** (52.5 °C held while live outlet read 44.5 °C) |
| 7 | 13 | VAR | inlet °C×2 — same behaviour |
| 8–9 | 14–15 | `00` | |
| 10 | 16 | `00`/`01` | 1 while consumption, one cycle late |
| 11 | 17 | VAR 0–24 | capacity %×2 — same values as master water[11] |
| 12 | 18 | VAR 0–48 | flow L/min×10 — same as master water[12] |
| 13 | 19 | `00` | |
| 14 | 20 | `01` | |
| 15–16 | 21–22 | `00` | |
| 17 | 23 | `20` | |
| 18 | 24 | `02` | |
| 19 | 25 | `00`/`04` | **toggling bit**, ~13 s period, flips in the same cycle as slave water[19] |
| 20 | 26 | VAR 61→93 | **unknown A** — rose almost monotonically over the minute (dip at ignition) |
| 21 | 27 | VAR 30→42 | **unknown B** — same shape |
| 22 | 28 | VAR 101→133 | **unknown C** — always exactly A + 40 |
| 23 | 29 | `0A` | |
| 24 | 30 | `0E` | |
| 25 | 31 | `00` | |
| 26 | 32 | `06` | |
| 27 | 33 | `02` | |
| 28–30 | 34–36 | `00` | |
| 31 | 37 | `C2` | |
| 32–33 | 38–39 | `00` | |

Hypothesis for A/B/C: cascade load or demand figures the master uses to decide when to
stage the slave in. They rose as inlet temperature fell (i.e. as required ΔT grew) and kept
rising through post-purge, so they are not simply burner output. B ≈ 72 − inlet °C loosely
fits but not tightly. A capture in which the slave is brought in is needed; look for a
threshold crossing in A or C in the cycle before the slave's stage leaves `0x14`.

### 4.5 Master `0x44` frame — `50→50`, dir 0x10, len 34 (measured)

Payload constant: `44 00 14 1E 0A 00 00 … 00`. Values 20, 30, 10. Possibly cascade or
timing configuration (e.g. seconds), possibly a poll slot for an absent device. Unknown.

## 5. Burn cycle as observed (master, one tap ≈ 4.3 L/min)

Cycle-by-cycle from the capture (times are seconds into the capture):

| t | stage | flame | cap % | kcal/h | coarse | notes |
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

Stage values match the NavienManager enumeration (dacarson) exactly:
`0x14` standby, `0x20` demand, `0x29` pre-purge, `0x2B` ignition, `0x2C` flame on,
`0x2D` ramp-up, `0x33` active combustion, `0x34` water adjustment, `0x3C` flame off,
`0x46`/`0x47` post-purge 1/2, `0x49` DHW wait. `0x2B`, `0x34`, `0x47`, `0x49` not seen yet.

Capacity sanity check: 4.2 L/min × ΔT 9.5 K ≈ 2 400 kcal/h delivered to water ≈ 5 % of the
unit's 50 400 kcal/h (199 900 BTU/h) rating, vs. 5.5 % reported — consistent with %×2
encoding, allowing for heat-exchanger warm-up.

The slave never left `0x14` and reported 0 kcal throughout.

## 6. Open questions, ranked

1. `0x41`[20..22] (A/B/C) — likely the cascade hand-off logic. Needs a slave-firing capture.
2. The `0x04` toggling bit (slave water[19] / `0x41`[19]) — ~13 s period, not stage-linked.
   Recirc pump? Heartbeat? Capture a recirculation event and a long idle period.
3. Gas[18] — lifetime gas or something else? Compare with the installer menu. Also find
   where cumulative water, operating hours and DHW-use count live (all zero-ish runs are
   candidates; a long capture may show one of them tick).
4. Gas[2..6] version bytes vs. the display's version screen.
5. Slave's duplicate-gas / missing-water cycles — pattern or noise?
6. The `0x44` frame — does it ever change (power cycle, installer-menu change, error)?
7. Error codes — nowhere decoded in the community either. A deliberate error (e.g. closing
   the gas valve briefly to induce an ignition failure) would reveal them, if the user is
   willing.
8. Water[14..15] `A0 BE` and gas[36..37] `AA 48` constants — model/rating codes? Temperature limits?

## 7. Writing to the bus (*community*, untested here)

Control frames: `F7 sys dst=0x50 src=0x0F dir=0x10 len=0x0C 4F … cs`, seed `0x62`.
Known fields (frame index): `[8]` power `0x0A` on / `0x0B` off; `[9]` DHW setpoint °C×2;
`[10]` SH setpoint (combi only); `[12]` token echoed in the ack; recirculation/HotButton flags
later in the payload (HotButton = "on" twice then "off"). NaviLink repeats each command ~6×.
Heater needs NaviLink enabled in the installer menu and sends a periodic "NaviLink present"
announce: `F7 sys 0F 50 10 03 4A 00 01 cs`. On the original NPE-240A power/setpoint commands
are ignored; on the NPE-240A2 they are reported to work (htumanyan/navien issue #97).
Transmit only after ≥20 ms of bus silence (PR #82); the ~150 ms gaps in §3 are the slots.
Whether a slave can be addressed directly (`dst 0x51`) is unknown; all community
implementations target `0x50`.

## 8. References

- htumanyan/navien — ESPHome component, protocol docs (`doc/rs485.md`, `doc/payload.md`,
  `doc/protocol_data_flow.md`), PRs #65 (cascade frames), #81 (SH setpoint), #82 (quiet-bus TX),
  #91 (sys_type), issue #97 (NPE-240A vs A2 commands). https://github.com/htumanyan/navien
- dacarson/NavienManager — ESP32 HomeKit; `Navien.cpp` has the most complete field list and
  the stage enumeration. https://github.com/dacarson/NavienManager
- evanjarrett/ESPHome-Navien — original byte-offset decoding of water/gas/control packets.
  https://github.com/evanjarrett/ESPHome-Navien
- Home Assistant thread "Navien, ESP32 Navilink interface":
  https://community.home-assistant.io/t/navien-esp32-navilink-interface/720567
- Waveshare USB TO RS485 (C): https://docs.waveshare.com/USB_TO_RS485_C
