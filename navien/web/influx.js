// InfluxDB writer status from /api/health's "influx" block (navien/influx.py InfluxWriter.health).
// Used by the top-bar pill, the footer and the debug view.

import { fmtAge, fmtNumber } from "./units.js";

const LOSS_RECENT_S = 600;          // a drop or rejection this recent keeps the pill red

const clock = (epoch) => new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/**
 * health: the influx block, receivedAt: Date.now() when it arrived. Ages are taken on the
 * server's clock (health.now) and advanced by the time since the poll, so a browser whose
 * clock is off still shows the right ages.
 */
export function influxStatus(h, receivedAt) {
  const now = h.now + Math.max(0, (Date.now() - receivedAt) / 1000);
  const age = (t) => (t === null || t === undefined ? null : now - t);
  const queued = (h.queued.samples || 0) + (h.queued.rollup || 0);
  const okWithin = 3 * h.batch_seconds + 10;
  const s = {
    queued, okAge: age(h.last_ok), errorAge: age(h.last_error_at),
    dropAge: age(h.last_drop_at), rejectAge: age(h.last_rejected_at),
    failingAge: age(h.failing_since), fill: h.max_queue ? queued / h.max_queue : 0,
  };
  const failing = h.failing_since !== null && h.failing_since !== undefined;
  const reason = failing ? ` since ${clock(h.failing_since)} (${h.last_error})` : "";
  const queue = `${fmtNumber(queued)} points queued (${fmtNumber(s.fill * 100)} % of the buffer)`;
  const retry = h.retry_in_s > 0 ? `, retrying in ${fmtAge(h.retry_in_s)}` : "";

  if (s.dropAge !== null && s.dropAge < LOSS_RECENT_S) {
    s.cls = "critical"; s.label = "Influx: dropping data";
    s.detail = failing
      ? `InfluxDB unreachable${reason}. The buffer is full (${fmtNumber(h.max_queue)} points), so the ` +
        `oldest samples are being discarded: ${fmtNumber(h.dropped)} dropped since the service ` +
        `started${retry}. Events and hourly totals are dropped last.`
      : `InfluxDB is writing again, but ${fmtNumber(h.dropped)} points were dropped (last ` +
        `${fmtAge(s.dropAge)} ago) while it was unreachable. Re-run navien-dash backfill for that period.`;
  } else if (s.rejectAge !== null && s.rejectAge < LOSS_RECENT_S) {
    s.cls = "critical"; s.label = "Influx: points rejected";
    s.detail = `InfluxDB rejected points ${fmtAge(s.rejectAge)} ago (${h.last_error}); ` +
      `${fmtNumber(h.rejected)} rejected since the service started. Rejected points are not retried: ` +
      `check the database and retention policies.`;
  } else if (failing) {
    s.cls = "warning"; s.label = `Influx: ${fmtNumber(queued)} queued`;
    s.detail = `InfluxDB unreachable${reason}. ${queue}${retry}. ` +
      (h.dropped ? `${fmtNumber(h.dropped)} points were dropped earlier.` : "Nothing lost yet.");
  } else if (s.okAge === null) {
    s.cls = "warning"; s.label = "Influx: connecting";
    s.detail = queued > 0 ? `No write to InfluxDB yet. ${queue}.` : "";
  } else if (s.okAge > okWithin) {
    s.cls = "warning"; s.label = `Influx: ${fmtNumber(queued)} queued`;
    s.detail = `No successful write to InfluxDB for ${fmtAge(s.okAge)} (writes are due every ` +
      `${fmtAge(h.batch_seconds)}). ${queue}.`;
  } else {
    s.cls = "good"; s.label = "Influx"; s.detail = "";
  }
  s.footer = s.okAge === null ? `Influx: no write yet, ${fmtNumber(queued)} queued`
    : `Influx: last write ${fmtAge(s.okAge)} ago, ${fmtNumber(queued)} queued`;
  return s;
}

/** Rows for the debug view's InfluxDB block. */
export function influxRows(h, receivedAt) {
  const s = influxStatus(h, receivedAt);
  const ago = (a) => (a === null ? "never" : `${fmtAge(a)} ago`);
  const events = Object.entries(h.events || {}).map(([k, v]) => `${k} ${fmtNumber(v)}`).join(" · ");
  return [
    ["Server", `${h.url} · db ${h.database}`],
    ["State", s.label],
    ["Written", `${fmtNumber(h.written)} points in ${fmtNumber(h.batches)} batches`],
    ["Last write", ago(s.okAge)],
    ["Queued", `${fmtNumber(h.queued.samples)} samples · ${fmtNumber(h.queued.rollup)} rollup ` +
      `(of ${fmtNumber(h.max_queue)})`],
    ["Dropped (buffer full)", h.dropped ? `${fmtNumber(h.dropped)}, last ${ago(s.dropAge)}` : "0"],
    ["Rejected", h.rejected ? `${fmtNumber(h.rejected)}, last ${ago(s.rejectAge)}` : "0"],
    ["Missed cycles", fmtNumber(h.subscription_dropped)],
    ["Last error", h.last_error ? `${h.last_error} (${ago(s.errorAge)})` : "none"],
    ["Retry in", h.retry_in_s > 0 ? fmtAge(h.retry_in_s) : "–"],
    ["Events written", events || "none yet"],
  ];
}

/**
 * ?influx=ok|retry|drop|reject|off replaces the health block with a made-up one, to look at
 * each state without breaking the real connection. Dev aid only; the page says so.
 */
export function fakeInflux(real) {
  const mode = new URLSearchParams(location.search).get("influx");
  if (!mode) return { fake: false, h: real };
  if (mode === "off") return { fake: true, h: null };
  const now = real ? real.now : Date.now() / 1000;
  const h = {
    url: real ? real.url : "https://influx.example:8086", database: real ? real.database : "navien",
    batch_seconds: 10, max_queue: 50000, queued: { samples: 28, rollup: 3 },
    written: 1834221, batches: 6290, dropped: 0, rejected: 0,
    events: { draw: 13, draw_unit: 17, recirc: 71, burn: 88, stage2: 2 },
    subscription_dropped: 0, last_ok: now - 4, last_error: null, last_error_at: null,
    failing_since: null, last_drop_at: null, last_rejected_at: null, retry_in_s: 0, now,
  };
  const refused = "ConnectError: [Errno 111] Connection refused";
  if (mode === "retry") Object.assign(h, {
    queued: { samples: 3380, rollup: 32 }, last_ok: now - 760, failing_since: now - 754,
    last_error: refused, last_error_at: now - 3, retry_in_s: 8,
  });
  if (mode === "drop") Object.assign(h, {
    queued: { samples: 49820, rollup: 180 }, last_ok: now - 6 * 3600, failing_since: now - 6 * 3600 + 6,
    last_error: refused, last_error_at: now - 20, retry_in_s: 40, dropped: 12840, last_drop_at: now - 1,
  });
  if (mode === "reject") Object.assign(h, {
    rejected: 214, last_rejected_at: now - 90, last_error_at: now - 90,
    last_error: 'HTTP 400: {"error":"partial write: points beyond retention policy dropped=214"}',
  });
  return { fake: true, h };
}
