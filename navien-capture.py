#!/usr/bin/env python3
"""
navien_capture.py - passive RS-485 frame capture for Navien NPE/NCB/NHB units.

Listens on a USB-RS485 adapter (19200 8N1), splits the byte stream into
Navien frames (0xF7 marker + 6-byte header + payload + checksum), validates
the checksum, and records everything for later analysis.

It NEVER transmits.

Outputs (prefix defaults to navien_capture_<timestamp>):
  <prefix>.raw    every byte received, exactly as it arrived (re-parse later)
  <prefix>.jsonl  one JSON object per frame with timestamp, header fields,
                  hex payload, checksum result and a light decode of the
                  well-known fields
  console         one line per frame while running, then a summary of
                  frame counts and inter-frame cadence per (src,dst,dir)

Usage:
  pip install pyserial
  python navien_capture.py --port /dev/ttyUSB0 --seconds 60
  python navien_capture.py --list            # show serial ports
"""

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

MARKER = 0xF7
HDR_SIZE = 6
MAX_PAYLOAD = 64          # anything larger is treated as a framing error
SEEDS = (0x4B, 0x62)      # 0x4B: status from 0x50, 0x62: control / other

# --------------------------------------------------------------------------- #
# Protocol helpers
# --------------------------------------------------------------------------- #

def navien_checksum(data: bytes, seed: int) -> int:
    """Checksum over header+payload (excluding the checksum byte itself)."""
    r = 0xFF
    for b in data:
        r <<= 1
        if r > 0xFF:
            r = (r & 0xFF) ^ seed
        r = (r & 0xFF) ^ b
    return r & 0xFF


def classify(src: int, dst: int, direction: int, payload: bytes) -> str:
    if direction == 0x90:
        if dst == 0x50:
            return "water"
        if dst == 0x0F:
            return "gas"
        return "status?"
    if direction == 0x10:
        if payload and payload[0] == 0x4A:
            return "announce"
        if payload and payload[0] == 0x4F:
            return "command"
        if 0x50 <= src <= 0x5F and 0x50 <= dst <= 0x5F:
            return "cascade"
        return "control?"
    return "unknown"


def t2c(b: int) -> float:
    return b / 2.0


# Burner stage byte (water frame[10]); names from the NavienManager project,
# sequence confirmed on an NPE-240A2 cascade capture.
STAGES = {
    0x14: "standby", 0x20: "demand", 0x29: "pre_purge", 0x2B: "ignition",
    0x2C: "flame_on", 0x2D: "ramp_up", 0x33: "combustion", 0x34: "water_adjust",
    0x3C: "flame_off", 0x46: "post_purge_1", 0x47: "post_purge_2", 0x49: "dhw_wait",
}


def stage_name(b: int) -> str:
    return STAGES.get(b, "0x%02X" % b)


def light_decode(kind: str, frame: bytes) -> dict:
    """Decode the fields whose offsets are established.
    Offsets are whole-frame indices (0 = 0xF7)."""
    d = {}
    try:
        if kind == "water" and len(frame) >= 41:
            d["unit"] = frame[2] - 0x50
            d["power_on"] = bool(frame[9] & 0x05)
            d["stage"] = stage_name(frame[10])
            d["set_c"] = t2c(frame[11])
            d["outlet_c"] = t2c(frame[12])
            d["inlet_c"] = t2c(frame[13])
            d["capacity_pct"] = frame[17] / 2.0
            d["flow_lpm"] = frame[18] / 10.0
            d["flame_on"] = bool(frame[27])
            d["consumption"] = bool(frame[8] & 0x20)
            d["recirc_running"] = bool(frame[8] & 0x08)
            d["metric_display"] = bool(frame[24] & 0x08)
            d["recirc_enabled"] = bool(frame[33] & 0x02)
        elif kind == "gas" and len(frame) >= 49:
            d["unit"] = frame[2] - 0x50
            d["set_c"] = t2c(frame[14])
            d["outlet_c"] = t2c(frame[15])
            d["inlet_c"] = t2c(frame[16])
            d["gas_kcal"] = frame[22] | (frame[23] << 8)
            d["gas_coarse"] = frame[20]          # ~ gas_kcal >> 8, 0 during ignition
        elif kind == "cascade" and len(frame) >= 41 and frame[6] == 0x41:
            # Master -> slave frame: carries the master's own state, one cycle late.
            d["from_unit"] = frame[2] - 0x50
            d["to_unit"] = frame[3] - 0x50
            st = frame[9]                        # 01 idle, 05 demand, 07 flame
            d["demand"] = bool(st & 0x04)
            d["flame_on"] = bool(st & 0x02)
            d["consumption"] = bool(frame[8] & 0x20)
            d["set_c"] = t2c(frame[11])
            d["outlet_c"] = t2c(frame[12])       # match the water frame while flowing;
            d["inlet_c"] = t2c(frame[13])        # hold the last draw's values in standby
            d["capacity_pct"] = frame[17] / 2.0
            d["flow_lpm"] = frame[18] / 10.0
            d["x26"] = frame[26]                 # three slowly varying bytes,
            d["x27"] = frame[27]                 # meaning not yet known;
            d["x28"] = frame[28]                 # x28 == x26 + 40 so far
        elif kind == "command" and len(frame) >= 19:
            d["power"] = {0x0A: "on", 0x0B: "off"}.get(frame[8])
            d["set_c"] = t2c(frame[9]) if frame[9] else None
            d["token"] = frame[12]
    except IndexError:
        pass
    return d


# --------------------------------------------------------------------------- #
# Stream parser
# --------------------------------------------------------------------------- #

class FrameParser:
    """Incremental parser: feed() bytes, get back complete frames."""

    def __init__(self):
        self.buf = bytearray()
        self.buf_ts = []          # arrival timestamp per byte in buf
        self.resyncs = 0

    def feed(self, data: bytes, ts: float):
        self.buf.extend(data)
        self.buf_ts.extend([ts] * len(data))
        out = []
        while True:
            # find marker
            try:
                i = self.buf.index(MARKER)
            except ValueError:
                self._drop(len(self.buf))
                break
            if i:
                self.resyncs += 1
                self._drop(i)
            if len(self.buf) < HDR_SIZE:
                break
            sys_type, plen = self.buf[1], self.buf[5]
            if not (1 <= sys_type <= 9) or plen == 0xFF or plen > MAX_PAYLOAD:
                # not a real header (firmware itself requires sys_type in 1..9)
                self.resyncs += 1
                self._drop(1)
                continue
            total = HDR_SIZE + plen + 1
            if len(self.buf) < total:
                break
            frame = bytes(self.buf[:total])
            cs_ok = any(navien_checksum(frame[:-1], s) == frame[-1] for s in SEEDS)
            if not cs_ok:
                # Could be a corrupted frame or a false marker inside a real
                # frame. Accept it as a bad frame only if it is followed by
                # another marker (i.e. the boundary looks right); otherwise
                # slide one byte and try again.
                if len(self.buf) <= total:
                    break                       # need the next byte to decide
                if self.buf[total] != MARKER:
                    self.resyncs += 1
                    self._drop(1)
                    continue
            out.append((self.buf_ts[0], frame))
            self._drop(total)
        return out

    def _drop(self, n):
        del self.buf[:n]
        del self.buf_ts[:n]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def list_ports():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found.")
    for p in ports:
        print(f"{p.device:16s} {p.description}")
    return ports


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (auto-detected if only one exists)")
    ap.add_argument("--seconds", type=float, default=60, help="capture duration (default 60)")
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--out", help="output file prefix")
    ap.add_argument("--quiet", action="store_true", help="don't print every frame")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    args = ap.parse_args()

    if args.list:
        list_ports()
        return

    port = args.port
    if not port:
        ports = list(serial.tools.list_ports.comports())
        if len(ports) == 1:
            port = ports[0].device
            print(f"Auto-selected port {port} ({ports[0].description})")
        else:
            print("Specify --port. Available ports:")
            list_ports()
            sys.exit(1)

    prefix = args.out or datetime.now().strftime("navien_capture_%Y%m%d_%H%M%S")
    raw_path, jsonl_path = prefix + ".raw", prefix + ".jsonl"

    ser = serial.Serial(port, args.baud, bytesize=8, parity="N", stopbits=1, timeout=0.05)
    ser.reset_input_buffer()

    parser = FrameParser()
    counts = defaultdict(int)
    cs_fail = defaultdict(int)
    last_ts = {}                 # per-stream last timestamp
    gaps = defaultdict(list)     # per-stream inter-frame gaps (ms)
    prev_any = None
    first_frame_ts = None
    nbytes = 0
    nframes = 0

    print(f"Listening on {port} @ {args.baud} 8N1 for {args.seconds:.0f}s  (Ctrl-C to stop early)")
    print(f"Writing {raw_path} and {jsonl_path}")
    print("-" * 100)

    t_start = time.time()
    try:
        with open(raw_path, "wb") as raw_f, open(jsonl_path, "w") as js_f:
            while time.time() - t_start < args.seconds:
                data = ser.read(ser.in_waiting or 1)
                if not data:
                    if nbytes == 0 and time.time() - t_start > 5:
                        print("No bytes in 5 s. Check: NaviLink enabled in installer menu (P.18), "
                              "A/B wiring, correct port.", file=sys.stderr)
                        nbytes = -1   # only warn once
                    continue
                ts = time.time()
                nbytes = max(nbytes, 0) + len(data)
                raw_f.write(data)

                for fts, frame in parser.feed(data, ts):
                    nframes += 1
                    if first_frame_ts is None:
                        first_frame_ts = fts
                    sys_type, src, dst, direction, plen = frame[1], frame[2], frame[3], frame[4], frame[5]
                    payload = frame[HDR_SIZE:-1]
                    cs_rx = frame[-1]
                    seed_ok = next((s for s in SEEDS if navien_checksum(frame[:-1], s) == cs_rx), None)
                    kind = classify(src, dst, direction, payload)
                    stream = f"{src:02X}>{dst:02X}/{direction:02X}"

                    counts[stream] += 1
                    if seed_ok is None:
                        cs_fail[stream] += 1
                    if stream in last_ts:
                        gaps[stream].append((fts - last_ts[stream]) * 1000)
                    last_ts[stream] = fts
                    dt_any = (fts - prev_any) * 1000 if prev_any else 0.0
                    prev_any = fts

                    rec = {
                        "ts": round(fts, 4),
                        "t_rel": round(fts - first_frame_ts, 4),
                        "dt_ms": round(dt_any, 1),
                        "kind": kind,
                        "sys_type": sys_type,
                        "src": src, "dst": dst, "dir": direction, "len": plen,
                        "payload": payload.hex(" "),
                        "checksum": cs_rx,
                        "checksum_ok": seed_ok is not None,
                        "seed": seed_ok,
                        "decode": light_decode(kind, frame),
                        "raw": frame.hex(" "),
                    }
                    js_f.write(json.dumps(rec) + "\n")

                    if not args.quiet:
                        flag = "" if seed_ok is not None else "  ** CHECKSUM FAIL"
                        dec = rec["decode"]
                        brief = ""
                        if kind in ("water", "gas"):
                            brief = (f" u{dec.get('unit')} set={dec.get('set_c')} out={dec.get('outlet_c')} "
                                     f"in={dec.get('inlet_c')}")
                            if kind == "water":
                                brief += (f" flow={dec.get('flow_lpm')} cap={dec.get('capacity_pct')}%"
                                          f" {dec.get('stage')}{' FLAME' if dec.get('flame_on') else ''}")
                            else:
                                brief += f" kcal={dec.get('gas_kcal')}"
                        elif kind == "cascade" and "capacity_pct" in dec:
                            brief = (f" u{dec.get('from_unit')}>u{dec.get('to_unit')} flow={dec.get('flow_lpm')}"
                                     f" cap={dec.get('capacity_pct')}% x={dec.get('x26')}/{dec.get('x27')}/{dec.get('x28')}"
                                     f"{' FLAME' if dec.get('flame_on') else ''}")
                        print(f"{rec['t_rel']:8.3f}s +{dt_any:6.1f}ms {kind:9s} {stream:11s} "
                              f"len={plen:2d}{brief}{flag}")
    except KeyboardInterrupt:
        print("\nStopped early.")
    finally:
        ser.close()

    # ----------------------------------------------------------------------- #
    # Summary
    # ----------------------------------------------------------------------- #
    elapsed = time.time() - t_start
    print("-" * 100)
    print(f"Captured {nframes} frames / {max(nbytes, 0)} bytes in {elapsed:.1f}s; "
          f"resyncs={parser.resyncs}, leftover bytes={len(parser.buf)}")
    if not counts:
        return
    print(f"\n{'stream':12s} {'kind':9s} {'frames':>7s} {'cs_fail':>7s} "
          f"{'mean_ms':>8s} {'min_ms':>7s} {'max_ms':>7s} {'per_s':>6s}")
    for stream in sorted(counts):
        src, rest = stream.split(">")
        dst, direction = rest.split("/")
        kind = classify(int(src, 16), int(dst, 16), int(direction, 16), b"")
        g = gaps[stream]
        if g:
            mean, mn, mx = statistics.mean(g), min(g), max(g)
            rate = 1000.0 / mean if mean else 0
            print(f"{stream:12s} {kind:9s} {counts[stream]:7d} {cs_fail[stream]:7d} "
                  f"{mean:8.1f} {mn:7.1f} {mx:7.1f} {rate:6.2f}")
        else:
            print(f"{stream:12s} {kind:9s} {counts[stream]:7d} {cs_fail[stream]:7d}       -       -       -      -")
    print(f"\nFiles: {raw_path}  {jsonl_path}")


if __name__ == "__main__":
    main()
