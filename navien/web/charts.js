// Small SVG time-series charts for the dashboard (no third-party library).
// Specs from the dataviz guidance: 2px round lines, hairline solid gridlines, one y-axis per
// chart, legend for >= 2 series plus sparse end labels, a crosshair synced across charts with
// one tooltip listing every series, keyboard + touch scrubbing, and a table view per chart.

import { fmtNumber } from "./units.js";

const SVG = "http://www.w3.org/2000/svg";
const M = { top: 10, right: 64, bottom: 22, left: 44 };
const BREAK_S = 3.5;                  // a gap longer than this between cycles breaks the line

// One hover time shared by every chart, so the crosshair lines up across them.
const sync = { charts: new Set(), t: null };
function setHover(t) {
  sync.t = t;
  for (const c of sync.charts) c.drawHover();
}

function svg(tag, attrs = {}, parent = null) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

function html(tag, cls, parent, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  if (parent) parent.appendChild(el);
  return el;
}

function niceStep(span, count) {
  const raw = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}

export function niceDomain(min, max, count = 4, zero = false) {
  if (zero) min = Math.min(0, min);
  if (!(max > min)) { max = min + 1; min = zero ? 0 : min - 1; }
  const step = niceStep(max - min, count);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
  return { lo, hi, ticks, step };
}

function compact(v, step) {
  if (Math.abs(v) >= 1000) return `${fmtNumber(v / 1000, step >= 1000 ? 0 : 1)}K`;
  return fmtNumber(v, step < 1 ? (step < 0.1 ? 2 : 1) : 0);
}

function timeTicks(t0, t1, width) {
  const steps = [15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  // At most 8 ticks: wide charts would otherwise pick 30 s steps, which an HH:MM label
  // can't tell apart. Sub-minute steps (short windows only) get seconds in their labels.
  const maxTicks = Math.min(8, Math.max(2, Math.floor(width / 70)));
  const step = steps.find((s) => (t1 - t0) / s <= maxTicks) || 3600;
  const out = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
  return { ticks: out, seconds: step < 60 };
}

const clock = (t, seconds = false) => new Date(t * 1000).toLocaleTimeString(undefined,
  { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}), hour12: false });

function nearestIndex(times, t) {
  let lo = 0, hi = times.length - 1;
  if (hi < 0) return -1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < t) lo = mid; else hi = mid;
  }
  return Math.abs(times[lo] - t) <= Math.abs(times[hi] - t) ? lo : hi;
}

// ---------------------------------------------------------------------------- base

class Chart {
  constructor(root, { title, height }) {
    this.root = root;
    root.classList.add("chart");
    const head = html("div", "chart-head", root);
    html("h3", "chart-title", head, title);
    this.legend = html("div", "chart-legend", head);
    this.tableBtn = html("button", "small chart-table-btn", head, "Table");
    this.tableBtn.type = "button";
    this.tableBtn.setAttribute("aria-pressed", "false");
    this.tableBtn.addEventListener("click", () => this.toggleTable());
    this.plot = html("div", "chart-plot", root);
    this.height = height;
    this.svg = svg("svg", { role: "img", "aria-label": title, tabindex: "0" }, this.plot);
    this.tip = html("div", "chart-tip", this.plot);
    this.tip.hidden = true;
    this.tableWrap = html("div", "chart-table", root);
    this.tableWrap.hidden = true;
    this.times = [];
    this.hoverEl = null;
    sync.charts.add(this);

    const toT = (e) => {
      const r = this.svg.getBoundingClientRect();
      const x = e.clientX - r.left;
      return this.t0 + ((x - M.left) / this.plotW) * (this.t1 - this.t0);
    };
    this.svg.addEventListener("pointermove", (e) => this.times.length && setHover(toT(e)));
    this.svg.addEventListener("pointerdown", (e) => this.times.length && setHover(toT(e)));
    this.svg.addEventListener("pointerleave", (e) => e.pointerType === "mouse" && setHover(null));
    this.svg.addEventListener("blur", () => setHover(null));
    this.svg.addEventListener("keydown", (e) => this.onKey(e));
  }

  onKey(e) {
    if (!this.times.length) return;
    let i = sync.t === null ? this.times.length - 1 : nearestIndex(this.times, sync.t);
    if (e.key === "ArrowLeft") i -= e.shiftKey ? 10 : 1;
    else if (e.key === "ArrowRight") i += e.shiftKey ? 10 : 1;
    else if (e.key === "Home") i = 0;
    else if (e.key === "End") i = this.times.length - 1;
    else if (e.key === "Escape") { setHover(null); return; }
    else return;
    e.preventDefault();
    setHover(this.times[Math.max(0, Math.min(this.times.length - 1, i))]);
  }

  layout(t1, windowS) {
    this.width = Math.max(240, this.plot.clientWidth || 300);
    this.plotW = this.width - M.left - M.right;
    this.plotH = this.height;
    this.t1 = t1;
    this.t0 = t1 - windowS;
    this.svg.setAttribute("viewBox", `0 0 ${this.width} ${this.plotH + M.top + M.bottom}`);
    this.svg.setAttribute("width", this.width);
    this.svg.setAttribute("height", this.plotH + M.top + M.bottom);
    this.svg.replaceChildren();
    this.x = (t) => M.left + ((t - this.t0) / (this.t1 - this.t0)) * this.plotW;
    // x axis: baseline + clock ticks
    const g = svg("g", { class: "axis" }, this.svg);
    const tt = timeTicks(this.t0, this.t1, this.plotW);
    for (const t of tt.ticks) {
      const x = this.x(t);
      svg("text", { x, y: M.top + this.plotH + 16, "text-anchor": "middle" }, g).textContent = clock(t, tt.seconds);
    }
    this.hoverEl = svg("g", { class: "hover" }, this.svg);
  }

  destroy() {
    sync.charts.delete(this);
    this.root.replaceChildren();
  }

  toggleTable() {
    const open = this.tableWrap.hidden;
    this.tableWrap.hidden = !open;
    this.tableBtn.setAttribute("aria-pressed", String(open));
    if (open) this.renderTable();
  }

  positionTip(xPx) {
    this.tip.hidden = false;
    const w = this.tip.offsetWidth;
    const left = xPx + 12 + w > this.width ? xPx - 12 - w : xPx + 12;
    this.tip.style.left = `${Math.max(0, left)}px`;
    this.tip.style.top = `${M.top}px`;
  }
}

// ---------------------------------------------------------------------------- line chart

export class LineChart extends Chart {
  /**
   * series: [{label, color, dash?, get(snapshot) -> metric value | null}]
   * ref: optional {label, get} drawn as a muted dashed reference line (e.g. setpoint)
   * conv(prefs) -> {fn, label}: metric -> display units
   */
  constructor(root, { title, series, ref = null, conv, zero = false, height = 150 }) {
    super(root, { title, height });
    this.series = series;
    this.ref = ref;
    this.conv = conv;
    this.zero = zero;
    for (const s of [...series, ...(ref ? [{ ...ref, ref: true }] : [])]) {
      const item = html("span", "legend-item", this.legend);
      const key = html("span", `line-key${s.dash || s.ref ? " dashed" : ""}${s.ref ? " ref" : ""}`, item);
      key.style.setProperty("--key", s.ref ? "var(--muted)" : s.color);
      html("span", "", item, s.label);
    }
    if (series.length < 2 && !ref) this.legend.hidden = true;   // one series: the title names it
  }

  update(cycles, prefs, windowS) {
    const { fn, label } = this.conv(prefs);
    this.unit = label;
    const t1 = cycles.length ? cycles[cycles.length - 1].ts : Date.now() / 1000;
    this.times = cycles.map((c) => c.ts);
    const val = (get) => cycles.map((c) => { const v = get(c); return v === null || v === undefined ? null : fn(v); });
    this.values = this.series.map((s) => val(s.get));
    this.refValues = this.ref ? val(this.ref.get) : null;
    this.render(t1, windowS);
  }

  render(t1, windowS) {
    this.layout(t1, windowS);
    const visible = this.times.map((t) => t >= this.t0);
    const all = [...this.values, ...(this.refValues ? [this.refValues] : [])]
      .flatMap((vs) => vs.filter((v, i) => v !== null && visible[i]));
    const d = niceDomain(all.length ? Math.min(...all) : 0, all.length ? Math.max(...all) : 1, 4, this.zero);
    this.domain = d;
    const y = (v) => M.top + this.plotH - ((v - d.lo) / (d.hi - d.lo)) * this.plotH;
    this.y = y;

    const grid = svg("g", { class: "grid" }, this.svg);
    const axis = svg("g", { class: "axis" }, this.svg);
    for (const v of d.ticks) {
      const yy = y(v);
      svg("line", { x1: M.left, x2: M.left + this.plotW, y1: yy, y2: yy, class: v === d.lo ? "baseline" : "" }, grid);
      svg("text", { x: M.left - 6, y: yy + 4, "text-anchor": "end" }, axis).textContent = compact(v, d.step);
    }
    const paths = (vs) => {
      let dStr = "", pen = false;
      for (let i = 0; i < vs.length; i++) {
        if (!visible[i] || vs[i] === null || (i > 0 && this.times[i] - this.times[i - 1] > BREAK_S)) { pen = false; }
        if (!visible[i] || vs[i] === null) continue;
        dStr += `${pen ? "L" : "M"}${this.x(this.times[i]).toFixed(1)},${y(vs[i]).toFixed(1)}`;
        pen = true;
      }
      return dStr;
    };
    if (this.refValues) svg("path", { d: paths(this.refValues), class: "ref-line" }, this.svg);
    this.series.forEach((s, k) => {
      svg("path", { d: paths(this.values[k]), class: `series${s.dash ? " dashed" : ""}`, style: `stroke:${s.color}` }, this.svg);
    });

    // end labels: value at the line end; dropped if any two would collide
    const ends = this.series.map((s, k) => {
      const vs = this.values[k];
      for (let i = vs.length - 1; i >= 0; i--) if (vs[i] !== null) return { s, v: vs[i], y: y(vs[i]), t: this.times[i] };
      return null;
    }).filter(Boolean);
    const sorted = [...ends].sort((a, b) => a.y - b.y);
    const collide = sorted.some((e, i) => i && e.y - sorted[i - 1].y < 14);
    if (!collide) {
      const lab = svg("g", { class: "end-labels" }, this.svg);
      for (const e of ends) {
        const x = M.left + this.plotW + 6;
        svg("line", { x1: x, x2: x + 10, y1: e.y, y2: e.y, style: `stroke:${e.s.color}`, class: `key${e.s.dash ? " dashed" : ""}` }, lab);
        svg("text", { x: x + 14, y: e.y + 4 }, lab).textContent = compact(e.v, d.step / 2);
      }
    }
    this.hoverEl.remove();
    this.svg.appendChild(this.hoverEl);
    this.drawHover();
    if (!this.tableWrap.hidden) this.renderTable();
  }

  drawHover() {
    if (!this.hoverEl) return;
    this.hoverEl.replaceChildren();
    if (sync.t === null || !this.times.length || sync.t < this.t0 || sync.t > this.t1 + 1) {
      this.tip.hidden = true;
      return;
    }
    const i = nearestIndex(this.times, sync.t);
    const x = this.x(this.times[i]);
    svg("line", { x1: x, x2: x, y1: M.top, y2: M.top + this.plotH, class: "crosshair" }, this.hoverEl);
    this.tip.replaceChildren();
    html("div", "tip-time", this.tip, clock(this.times[i], true));
    const rows = this.series.map((s, k) => ({ s, v: this.values[k][i] }));
    if (this.refValues) rows.push({ s: { ...this.ref, color: "var(--muted)", dash: true }, v: this.refValues[i] });
    for (const { s, v } of rows) {
      if (v !== null && !s.ref && s.label !== this.ref?.label) {
        svg("circle", { cx: x, cy: this.y(v), r: 4, class: "dot", style: `fill:${s.color}` }, this.hoverEl);
      }
      const row = html("div", "tip-row", this.tip);
      const key = html("span", `line-key${s.dash ? " dashed" : ""}`, row);
      key.style.setProperty("--key", s.color);
      html("strong", "", row, v === null ? "–" : `${compact(v, this.domain.step / 2)} ${this.unit}`);
      html("span", "muted", row, s.label);
    }
    this.positionTip(x);
  }

  renderTable() {
    const table = document.createElement("table");
    const head = html("tr", "", html("thead", "", table));
    html("th", "", head, "Time");
    const cols = [...this.series, ...(this.ref ? [this.ref] : [])];
    for (const s of cols) html("th", "", head, `${s.label} (${this.unit})`);
    const body = html("tbody", "", table);
    for (let i = this.times.length - 1; i >= 0; i--) {
      if (this.times[i] < this.t0) break;
      const tr = html("tr", "", body);
      html("td", "", tr, clock(this.times[i], true));
      const vals = [...this.values.map((vs) => vs[i]), ...(this.refValues ? [this.refValues[i]] : [])];
      for (const v of vals) html("td", "", tr, v === null ? "–" : compact(v, this.domain.step / 2));
    }
    this.tableWrap.replaceChildren(table);
  }
}

// ---------------------------------------------------------------------------- state band

export class StateBand extends Chart {
  /** rows: [{label, color, get(snapshot) -> {state, text} | null}]; states: [{key, label, mix}] */
  constructor(root, { title, rows, states }) {
    super(root, { title, height: rows.length * 22 - 6 });
    this.rows = rows;
    this.states = states;
    for (const st of states) {
      const item = html("span", "legend-item", this.legend);
      const key = html("span", "box-key", item);
      key.style.setProperty("--mix", `${st.mix}%`);
      html("span", "", item, st.label);
    }
  }

  update(cycles, prefs, windowS) {
    this.times = cycles.map((c) => c.ts);
    this.cells = this.rows.map((r) => cycles.map((c) => r.get(c)));
    this.render(this.times.length ? this.times[this.times.length - 1] : Date.now() / 1000, windowS);
  }

  render(t1, windowS) {
    this.layout(t1, windowS);
    const g = svg("g", { class: "band" }, this.svg);
    const mix = Object.fromEntries(this.states.map((s) => [s.key, s.mix]));
    this.rows.forEach((r, k) => {
      const top = M.top + k * 22;
      svg("text", { x: M.left - 6, y: top + 12, "text-anchor": "end", class: "axis-label" }, g).textContent = r.label;
      svg("line", { x1: M.left, x2: M.left + this.plotW, y1: top + 8, y2: top + 8, class: "track" }, g);
      // run-length segments; a 2px surface gap separates touching segments
      const cells = this.cells[k];
      let i = 0;
      while (i < cells.length) {
        const st = cells[i] && cells[i].state;
        let j = i + 1;
        while (j < cells.length && (cells[j] && cells[j].state) === st && this.times[j] - this.times[j - 1] <= BREAK_S) j++;
        if (st && mix[st] && this.times[j - 1] >= this.t0) {
          const xa = Math.max(M.left, this.x(this.times[i] - 0.5));
          const xb = Math.min(M.left + this.plotW, this.x(this.times[j - 1] + 0.5));
          if (xb - xa > 2) {
            svg("rect", { x: xa + 1, y: top, width: xb - xa - 2, height: 16, rx: 3,
                          style: `fill:color-mix(in oklab, ${r.color} ${mix[st]}%, var(--surface))` }, g);
          }
        }
        i = j;
      }
    });
    this.hoverEl.remove();
    this.svg.appendChild(this.hoverEl);
    this.drawHover();
    if (!this.tableWrap.hidden) this.renderTable();
  }

  drawHover() {
    if (!this.hoverEl) return;
    this.hoverEl.replaceChildren();
    if (sync.t === null || !this.times.length || sync.t < this.t0 || sync.t > this.t1 + 1) {
      this.tip.hidden = true;
      return;
    }
    const i = nearestIndex(this.times, sync.t);
    const x = this.x(this.times[i]);
    svg("line", { x1: x, x2: x, y1: M.top - 4, y2: M.top + this.plotH + 4, class: "crosshair" }, this.hoverEl);
    this.tip.replaceChildren();
    html("div", "tip-time", this.tip, clock(this.times[i], true));
    this.rows.forEach((r, k) => {
      const row = html("div", "tip-row", this.tip);
      const key = html("span", "line-key", row);
      key.style.setProperty("--key", r.color);
      html("strong", "", row, this.cells[k][i] ? this.cells[k][i].text : "–");
      html("span", "muted", row, r.label);
    });
    this.positionTip(x);
  }

  renderTable() {
    const table = document.createElement("table");
    const head = html("tr", "", html("thead", "", table));
    html("th", "", head, "Time");
    for (const r of this.rows) html("th", "", head, r.label);
    const body = html("tbody", "", table);
    for (let i = this.times.length - 1; i >= 0; i--) {
      if (this.times[i] < this.t0) break;
      const tr = html("tr", "", body);
      html("td", "", tr, clock(this.times[i], true));
      for (const cells of this.cells) html("td", "", tr, cells[i] ? cells[i].text : "–");
    }
    this.tableWrap.replaceChildren(table);
  }
}
