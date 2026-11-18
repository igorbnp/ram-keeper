/* ram-keeper — pure data shaping.
 *
 * Everything here is pure so the panel never has to think about parsing,
 * ordering, or formatting. QML delegates; this file decides.
 */

// ---------------------------------------------------------------------------
// Units and formatting
// ---------------------------------------------------------------------------

function safeNonNeg(value) {
  if (value === null || value === undefined || typeof value === "boolean"
      || typeof value === "object") {
    return 0;
  }
  var n = Number(value);
  return (isFinite(n) && n > 0) ? n : 0;
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || typeof bytes === "boolean"
      || Array.isArray(bytes)
      || (typeof bytes === "string" && bytes.trim() === "")) {
    return "0 MB";
  }
  var n = Number(bytes);
  if (!isFinite(n) || n < 0) return "0 MB";
  if (n < 1024) {
    var b = Math.round(n);
    return b < 1024 ? b + " B" : "1 KB";
  }
  var kb = n / 1024;
  if (Math.round(kb) < 1024) return Math.round(kb) + " KB";
  var mb = n / 1048576;
  if (Math.round(mb) < 1024) {
    var mb1 = Number(mb.toFixed(1));
    return (mb1 < 10 ? mb.toFixed(1) : Math.round(mb)) + " MB";
  }
  var gb = mb / 1024;
  var gb1 = Number(gb.toFixed(1));
  if (gb1 >= 100) return Math.round(gb) + " GB";
  var gb2 = Number(gb.toFixed(2));
  return (gb2 < 10 ? gb.toFixed(2) : gb.toFixed(1)) + " GB";
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
  if (!snapshot || typeof snapshot !== "object"
      || snapshot.state === "unknown"
      || snapshot.memory_reading_valid === false
      || snapshot.memory_breakdown_valid === false) {
    return null;
  }
  var total = Number(snapshot.mem_total);
  if (!isFinite(total) || total <= 0) return null;
  var used = Math.min(total, safeNonNeg(snapshot.mem_used));
  var cached = Math.min(total, safeNonNeg(snapshot.cached));
  var apps = Number(snapshot.anon_and_kernel);
  if (!isFinite(apps) || apps < 0 || snapshot.anon_and_kernel === null
      || typeof snapshot.anon_and_kernel === "boolean") {
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
    case "unknown": return "󰋗";   // question, not the critical slash
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
  if (!forecast || typeof forecast !== "object" || Array.isArray(forecast)) return null;
  var rate = Number(forecast.rate_bytes_per_sec);
  if (!isFinite(rate)) rate = 0;
  var horizon = safeNonNeg(forecast.horizon_seconds);
  var conf = safeNonNeg(forecast.confidence);
  var urgency = typeof forecast.urgency === "string" && forecast.urgency ? forecast.urgency : "none";
  var culprit = typeof forecast.culprit === "string" ? forecast.culprit : "";
  var willCross = forecast.will_cross === true;

  var text;
  if (urgency === "none" || !willCross) {
    if (Math.abs(rate) < 4 * 1024 * 1024) {
      text = "Steady — nothing is trending";
    } else {
      text = rate < 0
        ? "Falling " + formatBytes(Math.abs(rate)) + "/s"
        : "Recovering +" + formatBytes(rate) + "/s";
    }
  } else if (urgency === "imminent") {
    text = "Will hit the floor in " + horizonLabel(horizon);
  } else if (urgency === "soon") {
    text = "Heading down — floor in " + horizonLabel(horizon);
  } else {
    text = "Slowly trending down — floor in " + horizonLabel(horizon);
  }

  if (culprit && rate < 0) text += " · " + culprit + " is growing";

  // Only flag as a warning when the forecast actually predicts crossing the
  // threshold. A "Steady" or "Recovering" forecast with will_cross:false must
  // never paint orange/red — the user would see urgency colour on a calm machine.
  var warn = Boolean(willCross && (urgency === "soon" || urgency === "imminent"));

  return {
    urgency: urgency,
    horizonSeconds: horizon,
    rateBytesPerSec: rate,
    confidence: conf,
    culprit: culprit,
    text: text,
    isWarning: warn
  };
}

function forecastForSnapshot(snapshot) {
  if (!snapshot || snapshot.state === "unknown" || snapshot.memory_reading_valid === false) return null;
  return forecastRow(snapshot.forecast);
}

// Humanise a horizon. Seconds matter when they are small — "in 45s" is
// actionable, "in 0.01 minutes" is noise.
function horizonLabel(seconds) {
  var s = Number(seconds);
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
function rowAction(group) {
  if (!group || group.inviolable === true) return "";
  if (group.protected === true) return "release";
  return "protect";
}

function reclaimableGroups(snapshot, limit) {
  if (!snapshot || !Array.isArray(snapshot.groups)) return [];
  var rows = [];
  for (var i = 0; i < snapshot.groups.length; i++) {
    var g = snapshot.groups[i];
    if (!g || typeof g !== "object") continue;
    if (g.protected) continue;
    // swapable:false means MemorySwapMax=0 — the kernel will not move anon
    // pages to zram, so only file/cache is recoverable.
    var canSwap = g.swapable !== false;
    var recl = safeNonNeg(g.reclaimable);
    var anon = safeNonNeg(g.anon);
    var give = recl + (canSwap ? anon : 0);
    if (give <= 64 * 1024 * 1024) continue;
    var thrashing = g.thrashing === true;
    var giveLbl = formatBytes(give);
    rows.push({
      key: String(g.name || ""),
      label: String(g.label || g.name || "process"),
      bytes: safeNonNeg(g.current),
      anon: anon,
      cache: recl,
      swap: safeNonNeg(g.swap),
      swapable: canSwap,
      give: give,
      giveLabel: giveLbl,
      thrashing: thrashing,
      subLabel: "can free " + giveLbl + (thrashing ? " · thrashing" : ""),
      protected: g.protected === true,
      inviolable: g.inviolable === true,
      action: rowAction(g),
      kind: String(g.kind || "")
    });
  }
  // Ordered by what this pass could RECOVER, which is not the same as size: a
  // 2 GB process holding 150 MB of giveable memory matters less than a 500 MB
  // process that can give 500 MB. The section is titled "available to reclaim",
  // so the first row has to be the one that gives the most.
  rows.sort(function (a, b) { return (b.give - a.give) || (b.bytes - a.bytes); });
  return typeof limit === "number" ? rows.slice(0, limit) : rows;
}

function protectedGroups(snapshot, limit) {
  if (!snapshot || !Array.isArray(snapshot.groups)) return [];
  var rows = snapshot.groups
    .filter(function (g) { return g && typeof g === "object" && g.protected === true; })
    .map(function (g) {
      return {
        key: String(g.name || ""),
        label: String(g.label || g.name || "protected"),
        bytes: safeNonNeg(g.current),
        anon: safeNonNeg(g.anon),
        cache: safeNonNeg(g.reclaimable),
        swap: safeNonNeg(g.swap),
        protected: true,
        inviolable: g.inviolable === true,
        action: rowAction(g),
        kind: String(g.kind || "")
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
  if (!snapshot || !snapshot.zram || !Array.isArray(snapshot.zram.devices)) return [];
  return snapshot.zram.devices
    .filter(function (d) { return d && typeof d === "object"; })
    .map(function (d) {
      // Prefer the daemon's own ram_backed flag; fall back to case-insensitive
      // name heuristic only when the flag is absent (older daemon versions).
      var isZram = typeof d.ram_backed === "boolean"
        ? d.ram_backed
        : String(d.name || "").toLowerCase().indexOf("zram") >= 0;
      var size = safeNonNeg(d.size);
      var used = safeNonNeg(d.used);
      var fraction = size > 0 ? Math.min(1, used / size) : 0;
      if (!isFinite(fraction) || fraction < 0) fraction = 0;
      var isConcern = !isZram && fraction > 0.25;
      var rawName = String(d.name || "swap");
      return {
        key: rawName,
        label: isZram ? "zram (RAM)" : (isConcern ? rawName + " (slow disk)" : rawName),
        shortLabel: isZram ? "zram" : "disk",
        badge: isZram ? "RAM" : (isConcern ? "SLOW DISK" : "disk"),
        bytes: used,
        size: size,
        fraction: fraction,
        // Only disk swap degrades performance; zram swap is nearly free.
        isConcern: isConcern,
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
  var full60 = safeNonNeg(psi.full_avg60);
  var stall = Math.max(psi.some_avg60, full60);
  var severity = "ok";
  if (stall >= 20) severity = "critical";
  else if (stall >= 6) severity = "warning";
  else if (stall >= 1) severity = "notice";
  return { stallPercent: stall, severity: severity, available: true };
}

function psiLabel(row) {
  if (!row || row.severity === "unavailable" || row.available === false) return "Unavailable";
  if (row.severity === "ok") return "None";
  var pct = safeNonNeg(row.stallPercent);
  return pct.toFixed(1) + "%";
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
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  // A fresh daemon snapshot with invalid memory metrics is still useful: it
  // tells the panel the sensor is unreadable. Do not reject it and keep an old
  // healthy reading on screen.
  if (data.state === "unknown" && data.memory_reading_valid === false) return data;
  if (data.mem_total === null || typeof data.mem_total === "boolean") return null;
  var mt = Number(data.mem_total);
  if (!isFinite(mt) || mt <= 0) return null;
  return data;
}

function parseResult(raw) {
  var text = String(raw == null ? "" : raw).trim();
  if (!text) return null;
  var data = null;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return null;
  }
  return (data && typeof data === "object" && !Array.isArray(data)) ? data : null;
}

// The helper emits one JSON object; anything else means a broken run.
function parseJsonLine(line) {
  var text = String(line || "").trim();
  if (!text || text.charAt(0) !== "{") return null;
  return parseSnapshot(text);
}

function summaryLine(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return "";
  if (result.skipped_anon === "no-zram-room" && !result.acted) {
    return "App memory was not moved to disk swap";
  }
  if (result.skipped_anon === "psi-full-thrash" && !result.acted) {
    return "Skipped swap — system is thrashing";
  }
  if (!result.acted) return "Nothing to do — memory is comfortable";
  var parts = [];
  var freed = safeNonNeg(result.freed);
  var swapped = safeNonNeg(result.swapped_out);
  if (freed > 0) parts.push("freed " + formatBytes(freed));
  if (swapped > 0) parts.push("moved " + formatBytes(swapped) + " to zram");
  if (result.skipped_anon === "no-zram-room") parts.push("app memory was not moved to disk swap");
  if (result.skipped_anon === "psi-full-thrash") parts.push("skipped swap due to thrashing");
  if (parts.length === 0) parts.push("no change");
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// F2 — Pause/snooze state helpers
// ---------------------------------------------------------------------------

function parsePauseState(snapshot, nowSeconds) {
  if (!snapshot || typeof snapshot !== "object") {
    return { paused: false, label: "", remaining: 0 };
  }
  if (snapshot.paused !== true) {
    return { paused: false, label: "", remaining: 0 };
  }
  var untilRaw = snapshot.paused_until;
  var hasUntil = untilRaw !== null && untilRaw !== undefined
    && typeof untilRaw !== "boolean" && typeof untilRaw !== "object"
    && isFinite(Number(untilRaw)) && Number(untilRaw) > 0;
  var untilSec = hasUntil ? Number(untilRaw) : 0;
  var nowSec = (nowSeconds !== undefined && nowSeconds !== null
    && typeof nowSeconds !== "boolean" && typeof nowSeconds !== "object"
    && isFinite(Number(nowSeconds)) && Number(nowSeconds) > 0)
    ? Number(nowSeconds) : 0;

  var remaining = 0;
  if (hasUntil && nowSec > 0) {
    remaining = Math.max(0, Math.round(untilSec - nowSec));
  } else {
    remaining = Math.round(safeNonNeg(snapshot.paused_remaining_s));
    if (remaining === 0 && hasUntil) {
      var refNow = safeNonNeg(snapshot.checked_at);
      if (refNow > 0 && untilSec > refNow) {
        remaining = Math.max(0, Math.round(untilSec - refNow));
      }
    }
  }

  var label = remaining > 0
    ? ("Snoozed · resumes in " + horizonLabel(remaining))
    : (hasUntil ? "Snoozed · resuming soon" : "Paused indefinitely");
  return { paused: true, label: label, remaining: remaining };
}

function watchLine(snapshot, daemonUp, nowSeconds) {
  var ps = parsePauseState(snapshot, nowSeconds);
  if (ps.paused) {
    if (ps.remaining > 0) {
      return "Automatic protection snoozed · resumes in " + horizonLabel(ps.remaining);
    }
    if (ps.label === "Snoozed · resuming soon") {
      return "Automatic protection snoozed · resuming soon";
    }
    return "Automatic protection is paused";
  }
  if (!daemonUp) return "Automatic protection is paused";
  if (snapshot && (snapshot.state === "unknown" || snapshot.memory_reading_valid === false)) {
    return "Memory reading unavailable — cannot read /proc/meminfo";
  }
  var held = snapshot && Array.isArray(snapshot.held_by)
    ? snapshot.held_by.filter(function (h) { return typeof h === "string" && h.length > 0; })
    : [];
  if (held.length) {
    return "Memory is critically low. Protected apps are holding it: " + held.join(", ");
  }
  var relief = snapshot && snapshot.relief;
  if (relief && relief.skipped_anon === "no-zram-room") {
    if (relief.acted) return "Dropped cache. App memory was not moved to disk swap.";
    return "Memory is tight. App memory was not moved to disk swap.";
  }
  if (relief && relief.skipped_anon === "psi-full-thrash") {
    return "Memory is stalling · skipped swap because the system is thrashing.";
  }
  return "Watching automatically · nothing needed from you";
}

// ---------------------------------------------------------------------------
// Free-memory gauge target
// ---------------------------------------------------------------------------

function freePercentLabel(snapshot) {
  if (!snapshot || snapshot.state === "unknown" || snapshot.memory_reading_valid === false) return "--%";
  if (snapshot.free_percent === null || typeof snapshot.free_percent === "boolean") return "--%";
  var percent = Number(snapshot.free_percent);
  if (!isFinite(percent)) return "--%";
  return Math.round(percent) + "%";
}

function freeFraction(snapshot) {
  if (!snapshot || safeNonNeg(snapshot.mem_total) <= 0) return 1;
  if (snapshot.free_percent === null || typeof snapshot.free_percent === "boolean") return 1;
  var pct = Number(snapshot.free_percent);
  if (!isFinite(pct)) return 1;
  return Math.max(0, Math.min(1, pct / 100));
}

function usedFraction(snapshot) {
  return 1 - freeFraction(snapshot);
}

// ---------------------------------------------------------------------------
// F3 — Trend arrow, rich tooltip/hero, and session impact
// ---------------------------------------------------------------------------

// A subtle directional arrow for the bar label: "38% ↓" when memory is falling
// fast enough to notice, "38% ↑" when it is recovering, nothing when steady.
function trendArrow(snapshot) {
  if (!snapshot || snapshot.state === "unknown" || snapshot.memory_reading_valid === false) return "";
  var fc = snapshot.forecast;
  if (!fc || typeof fc !== "object") return "";
  var rate = Number(fc.rate_bytes_per_sec);
  if (!isFinite(rate)) return "";
  if (fc.will_cross === true && (fc.urgency === "imminent" || fc.urgency === "soon") && rate < 0) {
    return " ↓";
  }
  if (Math.abs(rate) < 4 * 1024 * 1024) return "";
  if (fc.confidence !== undefined && fc.confidence !== null) {
    var conf = Number(fc.confidence);
    if (!isFinite(conf) || conf < 0.35) return "";
  }
  return rate < 0 ? " ↓" : " ↑";
}

// Enhanced bar label: percentage + trend arrow (when relevant).
function barLabel(snapshot) {
  if (!snapshot || snapshot.state === "unknown" || snapshot.memory_reading_valid === false) return "--";
  var pct = freePercentLabel(snapshot);
  if (pct === "--%") return "--";
  return pct + trendArrow(snapshot);
}

// Rich tooltip with ETA and culprit when available.
function barTooltip(snapshot, nowSeconds) {
  if (!snapshot || typeof snapshot !== "object") return "Measuring memory…";
  if (snapshot.state === "unknown" || snapshot.memory_reading_valid === false) {
    return "Memory reading unavailable";
  }
  var pct = freePercentLabel(snapshot);
  var label = stateLabel(snapshot.state);
  var tip = pct + " memory free — " + label;
  var ps = parsePauseState(snapshot, nowSeconds);
  if (ps.paused) return tip + " · " + ps.label;
  var fc = forecastForSnapshot(snapshot);
  if (fc && fc.isWarning && fc.horizonSeconds > 0) {
    tip += " · floor in " + horizonLabel(fc.horizonSeconds);
    if (fc.culprit) tip += " (" + fc.culprit + ")";
  }
  return tip;
}

// PanelHero subtitle with available/total plus live ETA or snooze countdown.
function heroMeta(snapshot, nowSeconds) {
  if (!snapshot || typeof snapshot !== "object") return "";
  if (snapshot.state === "unknown" || snapshot.memory_reading_valid === false) {
    return "The daemon could not read memory";
  }
  var base = formatBytes(snapshot.mem_available) + " available of "
    + formatBytes(snapshot.mem_total);
  var ps = parsePauseState(snapshot, nowSeconds);
  if (ps.paused) return base + " · " + ps.label;
  var fc = forecastForSnapshot(snapshot);
  if (fc && fc.isWarning && fc.horizonSeconds > 0) {
    base += " · floor in " + horizonLabel(fc.horizonSeconds);
    if (fc.culprit) base += " · " + fc.culprit;
  }
  return base;
}

// Session impact summary: "Saved 1.00 GB · 5 actions · 512 MB freed 5 min ago"
function sessionImpactRow(snapshot, nowSeconds) {
  if (!snapshot || typeof snapshot !== "object" || !snapshot.session_stats
      || typeof snapshot.session_stats !== "object") {
    return null;
  }
  var ss = snapshot.session_stats;
  var freed = safeNonNeg(ss.bytes_freed);
  var actions = Math.round(safeNonNeg(ss.actions));
  if (actions === 0 && freed === 0) return null;

  var parts = [];
  if (freed > 0) parts.push("Saved " + formatBytes(freed));
  if (actions > 0) parts.push(actions + (actions === 1 ? " action" : " actions"));

  var lastLabel = "";
  var lastTs = safeNonNeg(ss.last_relief_ts);
  var nowSec = Number(nowSeconds);
  if (lastTs > 0 && isFinite(nowSec) && nowSec >= lastTs) {
    var ago = nowSec - lastTs;
    var agoStr = ago < 90 ? Math.max(1, Math.round(ago)) + "s ago"
      : ago < 5400 ? Math.round(ago / 60) + " min ago"
      : (ago / 3600).toFixed(1) + " h ago";
    var lastBytes = safeNonNeg(ss.last_relief_bytes);
    var who = typeof ss.last_relief_label === "string" && ss.last_relief_label.trim() !== ""
      ? " (" + ss.last_relief_label.trim() + ")" : "";
    if (lastBytes > 0) {
      lastLabel = formatBytes(lastBytes) + who + " freed " + agoStr;
    } else {
      lastLabel = "last relief" + who + " " + agoStr;
    }
  }

  return {
    text: parts.join(" · ") + (lastLabel ? " · " + lastLabel : ""),
    bytesSaved: freed,
    actions: actions
  };
}

// The shell loads this file as a plain QML import; the CommonJS export below
// exists so the exact same logic can be unit-tested under node.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    formatBytes: formatBytes,
    formatRate: formatRate,
    memoryParts: memoryParts,
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
    rowAction: rowAction,
    reclaimableGroups: reclaimableGroups,
    protectedGroups: protectedGroups,
    swapRows: swapRows,
    psiRow: psiRow,
    psiLabel: psiLabel,
    parseSnapshot: parseSnapshot,
    parseResult: parseResult,
    parseJsonLine: parseJsonLine,
    summaryLine: summaryLine,
    parsePauseState: parsePauseState,
    watchLine: watchLine,
    freePercentLabel: freePercentLabel,
    freeFraction: freeFraction,
    usedFraction: usedFraction,
    trendArrow: trendArrow,
    barLabel: barLabel,
    barTooltip: barTooltip,
    heroMeta: heroMeta,
    sessionImpactRow: sessionImpactRow
  };
}
