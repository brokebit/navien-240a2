// Navien cascade dashboard. Plain ES modules, no build step; talks to /ws (navien/ws.py).

import { DEFAULT_PREFS, UNITS, fmt, fmtAge, fmtNumber, humanize, unitLabel } from "./units.js";
import { Debug } from "./debug.js";
import { fakeInflux, influxStatus } from "./influx.js";
import { LineChart, StateBand } from "./charts.js";

const STALE_S = 5;                  // no cycle for this long -> data shown as stale
const HEALTH_EVERY_MS = 5000;
const UNIT_NAMES = { 0: "Master", 1: "Slave" };

// ------------------------------------------------------------------ preferences

function loadPrefs() {
  try {
    return { ...DEFAULT_PREFS, debug: false, ...JSON.parse(localStorage.getItem("navien.prefs") || "{}") };
  } catch {
    return { ...DEFAULT_PREFS, debug: false };
  }
}

function savePrefs() {
  try {
    localStorage.setItem("navien.prefs", JSON.stringify(prefs));
  } catch { /* private mode etc.: prefs just don't persist */ }
}

const prefs = loadPrefs();
const app = {
  hello: null,
  cycles: [],                       // recent snapshots, oldest first (charts use this in 3b)
  latest: null,
  lastCycleAt: 0,                   // client clock: when the last cycle arrived
  health: null,
  healthAt: 0,                      // client clock: when health arrived
  influxFake: false,                // ?influx=… dev override in effect
  ws: null,
  wsOpen: false,
};

const $ = (id) => document.getElementById(id);
const debug = new Debug({ send: (msg) => send(msg), getLatest: () => app.latest });

// ------------------------------------------------------------------ charts

const UNIT_COLORS = ["var(--unit-0)", "var(--unit-1)"];
const unitSeries = (key) => [0, 1].map((i) => ({
  label: UNIT_NAMES[i], color: UNIT_COLORS[i],
  get: (c) => (c.units[String(i)] ? c.units[String(i)][key] : null),
}));
const conv = (q) => (p) => ({ fn: UNITS[q][p[q]].conv, label: UNITS[q][p[q]].label });

// Burner sequence states for the band: firing, the ignition/purge sequence around it, idle.
const SEQUENCE = new Set(["startup", "demand", "pre_purge_1", "pre_purge_2", "pre_ignition", "ignition",
  "flame_on", "ramp_up", "water_adjustment", "flame_off", "post_purge_1", "post_purge_2", "shutting_down"]);
const burnerState = (u) => (u.flame ? "firing" : SEQUENCE.has(u.stage) ? "sequence" : "idle");

const charts = [
  new LineChart($("chart-outlet"), { title: "Outlet temperature", series: unitSeries("outlet_c"),
    ref: { label: "Setpoint", get: (c) => (c.units["0"] ? c.units["0"].set_c : null) }, conv: conv("temp") }),
  new LineChart($("chart-inlet"), { title: "Inlet temperature", series: unitSeries("inlet_c"), conv: conv("temp") }),
  new LineChart($("chart-flow"), { title: "Flow", series: unitSeries("flow_lpm"), conv: conv("flow"), zero: true }),
  new LineChart($("chart-gas"), { title: "Gas rate", series: unitSeries("gas_kcalh"), conv: conv("gas"), zero: true }),
  new StateBand($("chart-burner"), {
    title: "Burner",
    rows: [0, 1].map((i) => ({
      label: UNIT_NAMES[i], color: UNIT_COLORS[i],
      get: (c) => {
        const u = c.units[String(i)];
        return u ? { state: burnerState(u), text: u.flame ? `Firing · ${humanize(u.stage)}` : humanize(u.stage) } : null;
      },
    })),
    states: [{ key: "firing", label: "Firing", mix: 100 }, { key: "sequence", label: "Ignition / purge", mix: 40 }],
  }),
];

function windowSeconds() {
  return app.hello ? app.hello.history_seconds : 300;
}

let chartsQueued = false;
function renderCharts() {
  if (chartsQueued) return;            // coalesce bursts (history + resize) into one frame
  chartsQueued = true;
  requestAnimationFrame(() => {
    chartsQueued = false;
    for (const c of charts) c.update(app.cycles, prefs, windowSeconds());
  });
}
new ResizeObserver(renderCharts).observe(document.querySelector(".chart-grid"));

// ------------------------------------------------------------------ connection

let backoff = 1000;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  app.ws = ws;
  ws.onopen = () => {
    app.wsOpen = true;
    backoff = 1000;
    if (prefs.debug) send({ subscribe: ["frames"] });
    renderStatus();
  };
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = () => {
    app.wsOpen = false;
    renderStatus();
    setTimeout(connect, backoff + Math.random() * 500);
    backoff = Math.min(backoff * 2, 30000);
  };
}

function send(msg) {
  if (app.ws && app.ws.readyState === WebSocket.OPEN) app.ws.send(JSON.stringify(msg));
}

function onMessage(msg) {
  switch (msg.type) {
    case "hello":
      app.hello = msg;
      $("foot-source").textContent = msg.source;
      if (msg.version) $("foot-version").textContent = `navien-dash ${msg.version}`;
      $("chart-window").textContent = fmtAge(msg.history_seconds);
      break;
    case "history":
      app.cycles = msg.cycles;
      if (app.cycles.length) onCycle(app.cycles[app.cycles.length - 1], false);
      renderCharts();
      break;
    case "cycle":
      onCycle(msg, true);
      break;
    case "frame":
      debug.onFrame(msg);
      break;
    case "gap":
      send({ get: "history" });      // re-sync after dropped messages
      break;
    case "error":
      console.warn("server:", msg.message);
      break;
  }
}

function onCycle(snap, append) {
  if (append) {
    app.cycles.push(snap);
    const keep = app.hello ? app.hello.history_seconds : 300;
    const cutoff = snap.ts - keep;
    while (app.cycles.length && app.cycles[0].ts < cutoff) app.cycles.shift();
  }
  app.latest = snap;
  app.lastCycleAt = Date.now();
  render(snap);
  if (append) renderCharts();
  debug.onCycle(snap);
}

async function pollHealth() {
  try {
    const r = await fetch("/api/health", { cache: "no-store" });
    app.health = await r.json();
    app.healthAt = Date.now();
    const { fake, h } = fakeInflux(app.health.influx);
    app.health.influx = h;
    app.influxFake = fake;
    debug.onHealth(app.health, app.healthAt);
  } catch {
    app.health = null;
  }
  renderStatus();
}

// ------------------------------------------------------------------ status

function renderStatus() {
  const age = app.lastCycleAt ? (Date.now() - app.lastCycleAt) / 1000 : null;
  let cls, label, detail = "";
  if (!app.wsOpen) {
    cls = "warning"; label = "Reconnecting…";
    detail = "Lost the connection to the monitor service; retrying.";
  } else if (app.health && !app.health.bus_alive) {
    cls = "critical"; label = "No data from heaters";
    const r = app.health.reader || {};
    if (r.last_error) {
      detail = `RS-485 adapter: ${r.last_error}`;
    } else if (r.last_stall_reopen_at) {
      const ago = Math.max(0, Date.now() / 1000 - r.last_stall_reopen_at);
      detail = `The RS-485 adapter delivers no bytes; the monitor reopens the port while it stays ` +
        `silent (${fmtNumber(r.stall_reopens)} reopens, last ${fmtAge(ago)} ago).`;
    } else {
      detail = "The monitor is running but receives no frames from the bus.";
    }
  } else if (age === null || age > STALE_S) {
    cls = "warning"; label = "Waiting for data";
  } else {
    cls = "good"; label = "Live";
  }
  const pill = $("conn");
  pill.className = `pill status-${cls}`;
  pill.querySelector(".label").textContent = label;
  const d = $("conn-detail");
  d.hidden = !detail;
  d.textContent = detail;
  $("foot-age").textContent = age === null ? "never" : `${fmtAge(age)} ago`;
  document.body.classList.toggle("is-stale", cls !== "good");
  renderInflux();
}

// Shown only when the service writes to InfluxDB (never under --replay).
function renderInflux() {
  const h = app.health && app.health.influx;
  const pill = $("influx"), detail = $("influx-detail"), foot = $("foot-influx");
  pill.hidden = foot.hidden = !h;
  if (!h) {
    detail.hidden = true;
    return;
  }
  const s = influxStatus(h, app.healthAt);
  const sim = app.influxFake ? " (simulated)" : "";
  pill.className = `pill status-${s.cls}`;
  pill.querySelector(".label").textContent = s.label + sim;
  // a CSS tooltip, not title=: title is reset every second here, so the browser never shows it
  pill.dataset.tip = s.footer;
  detail.hidden = !s.detail;
  detail.className = `conn-detail ${s.cls}`;
  detail.textContent = s.detail + (sim && s.detail ? " [Simulated: remove ?influx= from the address.]" : "");
  foot.querySelector(".text").textContent = s.footer + sim;
}

// ------------------------------------------------------------------ household view

const cards = new Map();            // unit index -> {root, fields}

function cardFor(idx, role) {
  if (cards.has(idx)) return cards.get(idx);
  const root = $("unit-card").content.firstElementChild.cloneNode(true);
  root.dataset.unit = idx;
  root.querySelector(".role").textContent = UNIT_NAMES[idx] || humanize(role);
  root.querySelector(".idx").textContent = `unit ${idx}`;
  const fields = {};
  for (const el of root.querySelectorAll("[data-f]")) fields[el.dataset.f] = el;
  const card = { root, fields, stage: root.querySelector(".stage"), err: root.querySelector(".err"),
                 stale: root.querySelector(".stale"), meter: root.querySelector(".meter-fill") };
  cards.set(idx, card);
  // keep cards in unit order
  const container = $("units");
  const after = [...container.children].find((c) => Number(c.dataset.unit) > idx);
  container.insertBefore(root, after || null);
  applyUnitLabels(root);
  return card;
}

function recircText(u) {
  const mode = u.recirc_int_scheduled ? "internal schedule"
    : u.recirc_ext_scheduled ? "external schedule"
    : u.hotbutton_enabled ? "HotButton" : null;
  if (u.recirc_running) return mode ? `Running (${mode})` : "Running";
  if (!mode) return "Off";
  return u.recirc_scheduled_enabled || u.hotbutton_enabled ? `Ready (${mode})` : `Disabled (${mode})`;
}

function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

function renderUnit(idx, u) {
  const c = cardFor(idx, u.role);
  const f = c.fields;
  setText(f.outlet_c, fmt("temp", u.outlet_c, prefs));
  setText(f.inlet_c, fmt("temp", u.inlet_c, prefs));
  setText(f.set_c, fmt("temp", u.set_c, prefs));
  setText(f.flow_lpm, fmt("flow", u.flow_lpm, prefs));
  setText(f.gas_kcalh, fmt("gas", u.gas_kcalh, prefs));
  setText(f.heat_capacity_pct, fmtNumber(u.heat_capacity_pct, 1));
  c.meter.style.width = `${Math.min(100, Math.max(0, u.heat_capacity_pct || 0))}%`;

  c.root.classList.toggle("firing", !!u.flame);
  setText(c.stage.querySelector(".label"), u.flame ? `Firing · ${humanize(u.stage)}` : humanize(u.stage));

  const hasErr = !!u.error_code;
  c.err.hidden = !hasErr;
  if (hasErr) setText(c.err.querySelector(".label"), `Error ${u.error_code} (level ${u.error_level})`);

  setText(f.heating_mode, humanize(u.heating_mode));
  setText(f.recirc, recircText(u));
  setText(f.power_on, u.power_on === undefined ? "–" : u.power_on ? "On" : "Off");
  setText(f.capacity_pct, `${fmtNumber(u.capacity_pct, 1)} %`);
  setText(f.device_type, u.device_type || "–");
  setText(f.versions, u.controller_version ? `${u.controller_version} / ${u.panel_version}` : "–");
  setText(f.display, u.display_f === undefined ? "–" : u.display_f ? "°F" : "°C");
  setText(f.error, u.error_code === undefined ? "–" : `${u.error_code} / ${u.error_level}`);
  setText(f.gas_total_m3, u.gas_total_m3 === undefined ? "–" : `${fmtNumber(u.gas_total_m3, 1)} m³`);
  setText(f.days_since_install, fmtNumber(u.days_since_install));
  setText(f.dhw_uses, fmtNumber(u.dhw_uses));
  setText(f.operating_hours, u.operating_hours === undefined ? "–" : `${fmtNumber(u.operating_hours)} h`);

  const ages = u.age_s || {};
  setText(f.age, Object.entries(ages).map(([k, v]) => `${k} ${fmtNumber(v, 1)} s`).join(", ") || "–");
  const oldest = Math.max(0, ...Object.values(ages));
  c.stale.hidden = oldest <= STALE_S;
  c.root.classList.toggle("stale-data", oldest > STALE_S);
  if (oldest > STALE_S) setText(c.stale, `No fresh data from this unit for ${fmtAge(oldest)}.`);
}

function render(snap) {
  const units = Object.entries(snap.units).map(([k, v]) => [Number(k), v]);
  for (const [idx, u] of units) renderUnit(idx, u);

  const firing = units.filter(([, u]) => u.flame).length;
  setText($("sys-firing"), `${firing} of ${units.length}`);
  const sum = (key) => units.reduce((a, [, u]) => a + (u[key] || 0), 0);
  setText($("sys-flow"), fmt("flow", sum("flow_lpm"), prefs));
  setText($("sys-gas"), fmt("gas", sum("gas_kcalh"), prefs));
  const master = snap.units["0"];
  setText($("sys-set"), master ? fmt("temp", master.set_c, prefs) : "–");
  renderStatus();
}

// ------------------------------------------------------------------ controls

function applyUnitLabels(root = document) {
  for (const el of root.querySelectorAll(".unit[data-unit]")) {
    el.textContent = unitLabel(el.dataset.unit, prefs);
  }
}

function initControls() {
  for (const seg of document.querySelectorAll(".seg[data-pref]")) {
    const key = seg.dataset.pref;
    const sync = () => {
      for (const b of seg.querySelectorAll("button")) {
        b.setAttribute("aria-pressed", String(b.dataset.value === prefs[key]));
      }
    };
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      prefs[key] = b.dataset.value;
      savePrefs();
      sync();
      applyUnitLabels();
      if (app.latest) render(app.latest);
      renderCharts();
    });
    sync();
  }
  const dbg = $("debug-toggle");
  dbg.checked = !!prefs.debug;
  const applyDebug = () => {
    prefs.debug = dbg.checked;
    savePrefs();
    debug.setEnabled(prefs.debug);
  };
  dbg.addEventListener("change", applyDebug);
  debug.setEnabled(prefs.debug);
  applyUnitLabels();
}

initControls();
connect();
pollHealth();
setInterval(pollHealth, HEALTH_EVERY_MS);
setInterval(renderStatus, 1000);
