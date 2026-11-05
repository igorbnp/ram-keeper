/* ram-keeper — pure data shaping.
 *
 * Everything here is pure so the panel never has to think about parsing,
 * ordering, or formatting. QML delegates; this file decides.
 */

// ---------------------------------------------------------------------------
// Units and formatting
// ---------------------------------------------------------------------------

function formatBytes(bytes) {
  var n = Number(bytes);
  if (!isFinite(n) || n < 0) return "0 MB";
  if (n < 1024) return Math.round(n) + " B";
  var mb = n / 1048576;
  if (mb < 1024) return (mb < 10 ? mb.toFixed(1) : Math.round(mb)) + " MB";
  var gb = mb / 1024;
  if (gb < 100) return (gb < 10 ? gb.toFixed(2) : gb.toFixed(1)) + " GB";
  return Math.round(gb) + " GB";
}

function formatRate(bytes) {
  return formatBytes(bytes) + "/s";
}

// ---------------------------------------------------------------------------
// Memory bar segments: what is actually consuming the RAM
// ---------------------------------------------------------------------------

// Order matters — the bar reads left to right like a sentence:
// apps, then cache, then what is genuinely free and available.
// ---[ memory parts ]-------------------------------------------------------
// One normalisation for both the bar and the legend.
//
// The helper already publishes values that are supposed to sum to MemTotal, but
// a snapshot is data from another process: a race between two /proc reads, an
// older helper, or a hand-edited state.json can put `anon_and_kernel + cached`
// above `mem_total`. Painting fractions straight from that produced a bar 200%
// wide (clipped, so it looks full) and a legend claiming more memory than the
// machine has. The total is the authority; the parts are clamped into it.
function memoryParts(snapshot) {
  if (!snapshot || snapshot.memory_breakdown_valid === false) return null;
  var total = Number(snapshot.mem_total);
  if (!isFinite(total) || total <= 0) return null;
  var used = Math.max(0, Number(snapshot.mem_used) || 0);
  var cached = Math.max(0, Number(snapshot.cached) || 0);
  var apps = Number(snapshot.anon_and_kernel);
  if (!isFinite(apps) || apps < 0) {
    apps = Math.max(0, Math.min(used - cached, used));
  }
  apps = Math.max(0, Math.min(total, apps));
  var cache = Math.max(0, Math.min(total - apps, cached));
  return { total: total, apps: apps, cache: cache, free: total - apps - cache };
}

function memorySegments(snapshot) {
  var parts = memoryParts(snapshot);
  if (!parts) return [];
  return [
    { key: "apps", label: "Apps", bytes: parts.apps, tone: "apps" },
    { key: "cache", label: "Cache", bytes: parts.cache, tone: "cache" },
    { key: "free", label: "Free", bytes: parts.free, tone: "free" }
  ].filter(function (s) { return s.bytes > 0; });
}

function memoryPercentages(snapshot) {
  var parts = memoryParts(snapshot);
  if (!parts) return [];
  return [
    { key: "apps", label: "Apps", fraction: parts.apps / parts.total, tone: "apps" },
    { key: "cache", label: "Cache", fraction: parts.cache / parts.total, tone: "cache" },
    { key: "free", label: "Free", fraction: parts.free / parts.total, tone: "free" }
  ].filter(function (s) { return s.fraction > 0.001; });
}

// ---------------------------------------------------------------------------
// Pressure verdict
// ---------------------------------------------------------------------------

function stateLabel(state) {
  switch (state) {
    case "critical": return "Critical";
    case "tight": return "Low";
    case "stalling": return "Stalling";
    case "trending": return "Watching";
    case "unknown": return "No reading";
    default: return "Healthy";
  }
}

// "unknown" is the daemon saying it could not read memory. It must never render
// as "Healthy": an unreadable signal that claims all-clear is worse than no
// signal at all, because the user stops looking.
function isUnknown(state) {
  return state === "unknown";
}

function stateGlyph(state) {
  switch (state) {
    case "critical": return "󰅚";   // circle-slash
    case "tight": return "󰗚";      // gauge
    case "stalling": return "󰅜";   // history / clock
    case "trending": return "󰔡";   // trending down
    case "unknown": return "󰅚";   // question — deliberately not the calm glyph
    default: return "󰍛";           // memory
  }
}

function isUrgent(state) {
  return state === "critical";
}

function isWarning(state) {
  return state === "tight" || state === "stalling" || state === "trending"
    || state === "unknown";
}

// ---------------------------------------------------------------------------
// Forecast: where memory is heading
// ---------------------------------------------------------------------------

// The forecast is the part a threshold cannot do. A floor reacts to a number
// crossing a line; this reacts to its direction, which is the only way to act
// before the line is crossed.
function forecastRow(forecast) {
  if (!forecast) return null;
  var rate = Number(forecast.rate_bytes_per_sec) || 0;
  var horizon = Number(forecast.horizon_seconds) || 0;
  var urgency = forecast.urgency || "none";
  var culprit = forecast.culprit || "";

  var text;
  if (urgency === "none" || !forecast.will_cross) {
    if (Math.abs(rate) < 4 * 1024 * 1024) {
      text = "Steady — nothing is trending";
    } else {
      text = rate < 0
        ? "Falling " + formatBytes(Math.abs(rate)) + "/s"
        : "Rising " + formatBytes(rate) + "/s";
    }
  } else if (urgency === "imminent") {
    text = "Will hit the floor in " + horizonLabel(horizon);
  } else if (urgency === "soon") {
    text = "Heading down — floor in " + horizonLabel(horizon);
  } else {
    text = "Slowly trending down — floor in " + horizonLabel(horizon);
  }

  if (culprit && rate < 0) text += " · " + culprit + " is growing";

  return {
    urgency: urgency,
    horizonSeconds: horizon,
    rateBytesPerSec: rate,
    confidence: Number(forecast.confidence) || 0,
    culprit: culprit,
    text: text,
    isWarning: urgency === "soon" || urgency === "imminent"
  };
}

function forecastForSnapshot(snapshot) {
  if (!snapshot || snapshot.state === "unknown" || snapshot.memory_reading_valid === false) return null;
  return forecastRow(snapshot.forecast);
}

// Humanise a horizon. Seconds matter when they are small — "in 45s" is
// actionable, "in 0.01 minutes" is noise.
function horizonLabel(seconds) {
  var s = Number(seconds) || 0;
  if (!isFinite(s) || s <= 0) return "under a minute";
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + " min";
  return (s / 3600).toFixed(1) + " h";
}

// ---------------------------------------------------------------------------
// Victims: which processes could give memory back right now
// ---------------------------------------------------------------------------

// Only things that can actually give something back: unprotected, above the
// noise floor, and not already empty. Sorted by what we'd recover, so the
// panel leads with the process that matters.
function reclaimableGroups(snapshot, limit) {
  if (!snapshot || !snapshot.groups) return [];
  var rows = snapshot.groups
    .filter(function (g) {
      if (g.protected) return false;
      if (!g.reclaimable && !g.anon) return false;
      return (g.reclaimable + g.anon) > 64 * 1024 * 1024;
    })
    .map(function (g) {
      var give = (g.reclaimable || 0) + (g.anon || 0);
      return {
        key: g.name,
        label: g.label || g.name,
        bytes: g.current || 0,
        anon: g.anon || 0,
        cache: g.reclaimable || 0,
        swap: g.swap || 0,
        swapable: g.swapable !== false,
        give: give,
        protected: g.protected === true,
        kind: g.kind || ""
      };
    });
  // Ordered by what this pass could RECOVER, which is not the same as size: a
  // 2 GB process holding 150 MB of giveable memory matters less than a 500 MB
  // process that can give 500 MB. The section is titled "available to reclaim",
  // so the first row has to be the one that gives the most.
  rows.sort(function (a, b) { return (b.give - a.give) || (b.bytes - a.bytes); });
  return typeof limit === "number" ? rows.slice(0, limit) : rows;
}

function protectedGroups(snapshot, limit) {
  if (!snapshot || !snapshot.groups) return [];
  var rows = snapshot.groups
    .filter(function (g) { return g.protected === true; })
    .map(function (g) {
      return {
        key: g.name,
        label: g.label || g.name,
        bytes: g.current || 0,
        anon: g.anon || 0,
        cache: g.reclaimable || 0,
        swap: g.swap || 0,
        protected: true,
        kind: g.kind || ""
      };
    });
  rows.sort(function (a, b) { return b.bytes - a.bytes; });
  return typeof limit === "number" ? rows.slice(0, limit) : rows;
}

// ---------------------------------------------------------------------------
// zram, the healthy shape of swap
// ---------------------------------------------------------------------------

// zram at 90% is NOT a problem: its "full" state is compressed RAM, not a
// disk stall. Only flag the disk swapfile, which is the slow one.
function swapRows(snapshot) {
  if (!snapshot || !snapshot.zram || !snapshot.zram.devices) return [];
  return snapshot.zram.devices.map(function (d) {
    var isZram = String(d.name || "").indexOf("zram") >= 0;
    var fraction = d.size > 0 ? d.used / d.size : 0;
    return {
      key: d.name,
      label: isZram ? "zram (RAM)" : String(d.name || "swap"),
      shortLabel: isZram ? "zram" : "disk",
      bytes: d.used || 0,
      size: d.size || 0,
      fraction: fraction,
      // Only disk swap degrades performance; zram swap is nearly free.
      isConcern: !isZram && fraction > 0.25,
      isZram: isZram
    };
  });
}

// ---------------------------------------------------------------------------
// PSI: the honest pressure signal
// ---------------------------------------------------------------------------

function psiRow(psi) {
  if (!psi || psi.available === false || typeof psi.some_avg60 !== "number"
      || !isFinite(psi.some_avg60) || psi.some_avg60 < 0) {
    return { stallPercent: 0, severity: "unavailable", available: false };
  }
  var stall = Math.max(psi.some_avg60, Number(psi.full_avg60) || 0);
  var severity = "ok";
  if (stall >= 20) severity = "critical";
  else if (stall >= 6) severity = "warning";
  else if (stall >= 1) severity = "notice";
  return { stallPercent: stall, severity: severity, available: true };
}

function psiLabel(row) {
  if (!row || row.severity === "unavailable" || row.available === false) return "Unavailable";
  if (row.severity === "ok") return "None";
  return row.stallPercent.toFixed(1) + "%";
}

// ---------------------------------------------------------------------------
// Parsing the helper's JSON
// ---------------------------------------------------------------------------

function parseSnapshot(raw) {
  var data = null;
  try {
    data = JSON.parse(raw || "{}");
  } catch (e) {
    return null;
  }
  if (!data || typeof data !== "object") return null;
  // A fresh daemon snapshot with invalid memory metrics is still useful: it
  // tells the panel the sensor is unreadable. Do not reject it and keep an old
  // healthy reading on screen.
  if (data.state === "unknown" && data.memory_reading_valid === false) return data;
  if (!data.mem_total) return null;
  return data;
}

function parseResult(raw) {
  var data = null;
  try {
    data = JSON.parse(raw || "{}");
  } catch (e) {
    return null;
  }
  return (data && typeof data === "object") ? data : null;
}

// The helper emits one JSON object; anything else means a broken run.
function parseJsonLine(line) {
  var text = String(line || "").trim();
  if (!text || text.charAt(0) !== "{") return null;
  return parseSnapshot(text);
}

function summaryLine(result) {
  if (!result) return "";
  if (!result.acted) return "Nothing to do — memory is comfortable";
  var parts = [];
  if (result.freed > 0) parts.push("freed " + formatBytes(result.freed));
  if (result.swapped_out > 0) parts.push("moved " + formatBytes(result.swapped_out) + " to zram");
  if (parts.length === 0) parts.push("no change");
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// Free-memory gauge target
// ---------------------------------------------------------------------------

function freePercentLabel(snapshot) {
  if (!snapshot || snapshot.state === "unknown" || snapshot.memory_reading_valid === false) return "--%";
  var percent = Number(snapshot.free_percent);
  if (!isFinite(percent)) return "--%";
  return Math.round(percent) + "%";
}

function freeFraction(snapshot) {
  if (!snapshot || !snapshot.mem_total) return 1;
  return Math.max(0, Math.min(1, Number(snapshot.free_percent || 0) / 100));
}

function usedFraction(snapshot) {
  return 1 - freeFraction(snapshot);
}

// The shell loads this file as a plain QML import; the CommonJS export below
// exists so the exact same logic can be unit-tested under node.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    formatBytes: formatBytes,
    formatRate: formatRate,
    memorySegments: memorySegments,
    memoryPercentages: memoryPercentages,
    stateLabel: stateLabel,
    stateGlyph: stateGlyph,
    isUnknown: isUnknown,
    isUrgent: isUrgent,
    isWarning: isWarning,
    forecastRow: forecastRow,
    forecastForSnapshot: forecastForSnapshot,
    horizonLabel: horizonLabel,
    reclaimableGroups: reclaimableGroups,
    protectedGroups: protectedGroups,
    swapRows: swapRows,
    psiRow: psiRow,
    psiLabel: psiLabel,
    parseSnapshot: parseSnapshot,
    parseResult: parseResult,
    parseJsonLine: parseJsonLine,
    summaryLine: summaryLine,
    freePercentLabel: freePercentLabel,
    freeFraction: freeFraction,
    usedFraction: usedFraction
  };
}