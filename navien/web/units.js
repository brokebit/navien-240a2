// Unit conversion and formatting. The server always speaks metric (°C, L/min, kcal/h);
// everything the user sees goes through here.

export const UNITS = {
  temp: {
    F: { label: "°F", conv: (c) => c * 9 / 5 + 32, dp: 0 },
    C: { label: "°C", conv: (c) => c, dp: 1 },
  },
  flow: {
    gpm: { label: "GPM", conv: (lpm) => lpm / 3.78541, dp: 2 },
    lpm: { label: "L/min", conv: (lpm) => lpm, dp: 1 },
  },
  gas: {
    btu: { label: "BTU/h", conv: (kcal) => kcal * 3.96832, dp: 0 },
    kcal: { label: "kcal/h", conv: (kcal) => kcal, dp: 0 },
  },
};

export const DEFAULT_PREFS = { temp: "F", flow: "gpm", gas: "btu" };

const nf = new Map();
function numberFormat(dp) {
  if (!nf.has(dp)) {
    nf.set(dp, new Intl.NumberFormat(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp }));
  }
  return nf.get(dp);
}

/** Format a metric value of the given quantity in the user's chosen unit (number only). */
export function fmt(quantity, value, prefs) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  const u = UNITS[quantity][prefs[quantity]];
  return numberFormat(u.dp).format(u.conv(value));
}

export function unitLabel(quantity, prefs) {
  return UNITS[quantity][prefs[quantity]].label;
}

export function fmtNumber(value, dp = 0) {
  if (value === null || value === undefined) return "–";
  return numberFormat(dp).format(value);
}

/** "active_combustion" -> "Active combustion"; unknown codes ("0x4A") pass through. */
export function humanize(name) {
  if (!name) return "–";
  if (name.startsWith("0x")) return `Unknown (${name})`;
  const s = name.replaceAll("_", " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Seconds -> "4 s", "3 min", "2 h". */
export function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}
