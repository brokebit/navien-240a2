// Debug view: one hex row per bus stream with change highlighting and the decoded-field
// overlay from /api/fields. Subscribes to the WebSocket "frames" topic only while shown.

import { fmtAge, fmtNumber } from "./units.js";

// Order of the streams within a bus cycle (PROTOCOL_NOTES.md §3); unknown streams go last.
const STREAM_ORDER = ["50>51/10", "51>50/90", "51>0F/90", "50>52/10", "50>50/90", "50>0F/90", "50>50/10"];
const HEADER_LEN = 6;
const CHANGE_LEVELS = [[3, "chg-1"], [15, "chg-2"], [60, "chg-3"]];   // seconds since change -> class
const FRAMELOG_LINES = 60;

const hex2 = (b) => b.toString(16).padStart(2, "0");

export class Debug {
  constructor({ send, getLatest }) {
    this.send = send;
    this.getLatest = getLatest;
    this.enabled = false;
    this.fields = null;               // kind -> Map(frameIndex -> [field, ...])
    this.streams = new Map();         // stream -> state
    this.selected = null;             // {stream, index}
    this.paused = false;
    this.framelog = [];
    this.root = document.getElementById("debug");
    this.list = document.getElementById("streams");
    this.detail = document.getElementById("byte-detail");
    this.counters = document.getElementById("bus-counters");
    this.logEl = document.getElementById("framelog");
    this.health = null;

    this.list.addEventListener("click", (e) => {
      const b = e.target.closest(".b[data-i]");
      if (!b) return;
      const row = b.closest("[data-stream]");
      this.selected = { stream: row.dataset.stream, index: Number(b.dataset.i) };
      this.renderDetail();
      this.markSelected();
    });
    const pause = document.getElementById("framelog-pause");
    pause.addEventListener("click", () => {
      this.paused = !this.paused;
      pause.textContent = this.paused ? "Resume" : "Pause";
    });
    setInterval(() => this.enabled && this.refreshAges(), 1000);
  }

  // -------------------------------------------------------------- lifecycle

  async setEnabled(on) {
    this.enabled = on;
    this.root.hidden = !on;
    if (!on) {
      this.send({ unsubscribe: ["frames"] });
      return;
    }
    this.send({ subscribe: ["frames"] });
    if (!this.fields) await this.loadFields();
    if (this.streams.size === 0) {
      try {                                   // fill rows at once from the server's frame ring
        const r = await fetch("/api/frames?limit=600", { cache: "no-store" });
        for (const f of await r.json()) this.onFrame(f, true);
      } catch { /* the live stream will fill them */ }
    }
  }

  async loadFields() {
    try {
      const r = await fetch("/api/fields", { cache: "no-store" });
      const rows = await r.json();
      this.fields = {};
      for (const f of rows) {
        const m = (this.fields[f.kind] ||= new Map());
        const size = f.type === "u16" ? 2 : 1;
        for (let i = f.index; i < f.index + size; i++) {
          if (!m.has(i)) m.set(i, []);
          m.get(i).push(f);
        }
      }
    } catch {
      this.fields = {};
    }
  }

  // -------------------------------------------------------------- input

  onFrame(f, backfill = false) {
    const now = backfill ? f.ts * 1000 : Date.now();
    const bytes = f.hex.split(" ").map((h) => parseInt(h, 16));
    let s = this.streams.get(f.stream);
    if (!s) {
      s = { stream: f.stream, kind: f.kind, count: 0, fails: 0, last: null, ts: [], changedAt: [],
            seen: [], row: null, spans: [] };
      this.streams.set(f.stream, s);
    }
    s.count += 1;
    if (!f.checksum_ok) s.fails += 1;
    s.kind = f.kind;
    s.ts.push(f.ts);
    if (s.ts.length > 20) s.ts.shift();
    for (let i = 0; i < bytes.length; i++) {
      if (s.last && s.last[i] !== bytes[i]) s.changedAt[i] = now;
      const seen = (s.seen[i] ||= { min: bytes[i], max: bytes[i], values: new Set() });
      seen.min = Math.min(seen.min, bytes[i]);
      seen.max = Math.max(seen.max, bytes[i]);
      if (seen.values.size <= 32) seen.values.add(bytes[i]);
    }
    s.last = bytes;
    s.lastTs = f.ts;
    s.lastRecv = now;

    if (!this.enabled) return;
    this.renderRow(s);
    if (this.selected && this.selected.stream === f.stream) this.renderDetail();
    if (!backfill && !this.paused) this.logFrame(f);
  }

  onCycle(snap) {
    if (!this.enabled) return;
    const b = snap.bus;
    const h = this.health;
    const rows = [
      ["Frames", fmtNumber(b.frames)],
      ["Checksum failures", fmtNumber(b.checksum_fail)],
      ["Unexpected seeds", fmtNumber(b.seed_unexpected)],
      ["Cycle", b.cycle_ms === null ? "–" : `${b.cycle_ms} ms`],
      ["Parser resyncs", h ? fmtNumber(h.reader.resyncs) : "–"],
      ["Raw log", h && h.rawlog ? `${h.rawlog.file} (${fmtNumber(h.rawlog.dropped_chunks)} dropped)` : "–"],
      ["Clients", h ? h.hub.subscribers.map((s) => s.name).join(", ") : "–"],
    ];
    this.counters.replaceChildren(...rows.flatMap(([k, v]) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      return [dt, dd];
    }));
  }

  onHealth(h) {
    this.health = h;
  }

  // -------------------------------------------------------------- rendering

  rowFor(s) {
    if (s.row) return s.row;
    const row = document.createElement("div");
    row.className = "stream";
    row.dataset.stream = s.stream;
    row.innerHTML = `<div class="stream-head"><code class="name"></code> <span class="kind"></span>
      <span class="stats muted"></span></div><div class="bytes" role="group"></div>`;
    s.row = row;
    const order = (x) => { const i = STREAM_ORDER.indexOf(x); return i < 0 ? 99 : i; };
    const after = [...this.list.children].find((r) => order(r.dataset.stream) > order(s.stream));
    this.list.insertBefore(row, after || null);
    return row;
  }

  renderRow(s) {
    const row = this.rowFor(s);
    row.querySelector(".name").textContent = s.stream;
    row.querySelector(".kind").textContent = s.kind;
    const gaps = s.ts.slice(1).map((t, i) => t - s.ts[i]);
    const cadence = gaps.length ? gaps.reduce((a, b) => a + b, 0) / gaps.length : null;
    row.querySelector(".stats").textContent =
      `${fmtNumber(s.count)} frames${s.fails ? ` · ${s.fails} bad` : ""}` +
      (cadence ? ` · every ${fmtNumber(cadence * 1000)} ms` : "");

    const box = row.querySelector(".bytes");
    if (s.spans.length !== s.last.length) {
      box.replaceChildren();
      s.spans = s.last.map((_, i) => {
        const b = document.createElement("span");
        b.dataset.i = i;
        b.tabIndex = 0;
        box.appendChild(b);
        return b;
      });
    }
    const known = (this.fields && this.fields[s.kind]) || new Map();
    const now = Date.now();
    s.last.forEach((v, i) => {
      const span = s.spans[i];
      const text = hex2(v);
      if (span.textContent !== text) span.textContent = text;
      span.className = this.byteClass(s, i, known, now);
    });
    this.markSelected();
  }

  byteClass(s, i, known, now) {
    const cls = ["b"];
    if (i < HEADER_LEN) cls.push("hdr");
    else if (i === s.last.length - 1) cls.push("cs");
    const f = known.get(i);
    if (f) cls.push(`k-${f[0].confidence}`);
    const t = s.changedAt[i];
    if (t) {
      const age = (now - t) / 1000;
      const lvl = CHANGE_LEVELS.find(([lim]) => age < lim);
      if (lvl) cls.push(lvl[1]);
    }
    if (this.selected && this.selected.stream === s.stream && this.selected.index === i) cls.push("sel");
    return cls.join(" ");
  }

  refreshAges() {
    // change highlights fade even when a stream is quiet
    const now = Date.now();
    for (const s of this.streams.values()) {
      if (!s.row) continue;
      const known = (this.fields && this.fields[s.kind]) || new Map();
      s.spans.forEach((span, i) => { span.className = this.byteClass(s, i, known, now); });
    }
    if (this.selected) this.renderDetail();
  }

  markSelected() {
    for (const el of this.list.querySelectorAll(".b.sel")) {
      const row = el.closest("[data-stream]");
      if (!this.selected || row.dataset.stream !== this.selected.stream || Number(el.dataset.i) !== this.selected.index) {
        el.classList.remove("sel");
      }
    }
    if (!this.selected) return;
    const s = this.streams.get(this.selected.stream);
    if (s && s.spans[this.selected.index]) s.spans[this.selected.index].classList.add("sel");
  }

  renderDetail() {
    const sel = this.selected;
    const s = sel && this.streams.get(sel.stream);
    if (!s || !s.last || sel.index >= s.last.length) {
      this.detail.hidden = true;
      return;
    }
    const i = sel.index;
    const v = s.last[i];
    const seen = s.seen[i];
    const where = i < HEADER_LEN ? "header" : i === s.last.length - 1 ? "checksum" : `payload[${i - HEADER_LEN}]`;
    const changed = s.changedAt[i] ? `${fmtAge((Date.now() - s.changedAt[i]) / 1000)} ago` : "not since opened";
    const fields = ((this.fields && this.fields[s.kind]) || new Map()).get(i) || [];
    const fieldText = fields.length
      ? fields.map((f) => `${f.name} (${f.confidence}${f.unit ? ", " + f.unit : ""}${f.note ? ": " + f.note : ""})`).join("; ")
      : "unknown";
    const values = [...seen.values].sort((a, b) => a - b);
    const lines = [
      `${s.stream} ${s.kind} · frame[${i}] = ${where}`,
      `now 0x${hex2(v).toUpperCase()} = ${v} = 0b${v.toString(2).padStart(8, "0")}`,
      `seen ${values.length > 32 ? ">32" : values.length} value(s), min ${seen.min}, max ${seen.max}` +
        (values.length <= 12 ? `: ${values.map((x) => hex2(x)).join(" ")}` : ""),
      `last change ${changed}`,
      `field: ${fieldText}`,
    ];
    this.detail.hidden = false;
    this.detail.textContent = lines.join("\n");
  }

  logFrame(f) {
    const t = new Date(f.ts * 1000);
    const stamp = t.toLocaleTimeString(undefined, { hour12: false }) + "." + String(t.getMilliseconds()).padStart(3, "0");
    this.framelog.push(`${stamp} ${f.stream.padEnd(9)} ${f.checksum_ok ? " " : "!"} ${f.hex}`);
    if (this.framelog.length > FRAMELOG_LINES) this.framelog.shift();
    this.logEl.textContent = this.framelog.join("\n");
  }
}
