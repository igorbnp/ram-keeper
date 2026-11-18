// Unit-test ram-keeper's Model.js against real helper output and edge cases.
const path = require("path");
// Resolve from this file's own location so the suite runs from a fresh clone.
const PLUGIN = path.join(__dirname, "..");
const M_PATH = path.join(PLUGIN, "qml", "Model.js");

const M = require(M_PATH);

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log(`  ok   ${name}`); }
  else { console.log(`  FAIL ${name}${detail ? " — " + detail : ""}`); failures++; }
}

// A fixed snapshot, shaped like real daemon output.
const snap = M.parseSnapshot(JSON.stringify({
  state: "ok", free_percent: 38.3,
  mem_total: 8026562560, mem_available: 3073976832,
  mem_used: 4952585728, anon_and_kernel: 1665888256, cached: 2347696128,
  zram: {
    devices: [
      { name: "/swap/swapfile", type: "file", size: 8027074560,
        used: 7417856, ram_backed: false },
      { name: "/dev/zram0", type: "partition", size: 8025796608,
        used: 2950127616, ram_backed: true }
    ],
    used: 2957545472, size: 16052871168,
    disk_used: 7417856, ram_used: 2950127616
  },
  psi: { some_avg10: 0, some_avg60: 0.05, some_avg300: 0.31,
         full_avg10: 0, full_avg60: 0.04, full_avg300: 0.3 },
  forecast: { horizon_seconds: 962.9, rate_bytes_per_sec: -2666973,
              will_cross: true, confidence: 0.43, samples: 5,
              trend: "rising", culprit: "", urgency: "eventual" },
  groups: [
    { name: "app-org.chromium.Chromium-733668.scope",
      label: "Chromium 733668", kind: "scope", current: 442421248,
      anon: 211144704, file: 213536768, reclaimable_file: 208338944,
      swap: 473661440, reclaimable: 208338944, protected: false,
      swapable: true, pids: 152 },
    { name: "app-org.mozilla.firefox-88412.scope",
      label: "Firefox 88412", kind: "scope", current: 610000000,
      anon: 90000000, file: 40000000, reclaimable_file: 30000000,
      swap: 0, reclaimable: 30000000, protected: false,
      swapable: true, pids: 64 },
    { name: "wayland-wm@hyprland.desktop.service",
      label: "wayland-wm@hyprland", kind: "service", current: 1221656576,
      anon: 366366720, file: 680165376, reclaimable_file: 410349568,
      swap: 601264128, reclaimable: 410349568, protected: true,
      swapable: true, pids: 1919 }
  ],
  reclaimable_total: 782864384,
  checked_at: 1791253790.008
}));

console.log("== parsing ==");
check("parses real helper output", snap !== null);
check("has mem_total", snap && snap.mem_total > 0);
check("rejects garbage", M.parseSnapshot("not json") === null);
check("rejects empty", M.parseSnapshot("") === null);
check("rejects a non-snapshot object", M.parseSnapshot('{"hello":1}') === null);
check("parseJsonLine skips non-JSON lines", M.parseJsonLine("some log noise") === null);

console.log("\n== formatting ==");
check("0 B", M.formatBytes(0) === "0 B", M.formatBytes(0));
check("bytes", M.formatBytes(512) === "512 B", M.formatBytes(512));
check("MB", M.formatBytes(1572864) === "1.5 MB", M.formatBytes(1572864));
check("big MB rounds", M.formatBytes(157 * 1048576) === "157 MB", M.formatBytes(157 * 1048576));
check("GB", M.formatBytes(3.5 * 1073741824) === "3.50 GB", M.formatBytes(3.5 * 1073741824));
check("negative is safe", M.formatBytes(-5) === "0 MB");
check("NaN is safe", M.formatBytes(NaN) === "0 MB");
check("undefined is safe", M.formatBytes(undefined) === "0 MB");

console.log("\n== memory segments ==");
const segs = M.memorySegments(snap);
check("has segments", segs.length > 0);
check("apps present", segs.some(s => s.key === "apps"));
check("apps use the apps colour", segs.find(s => s.key === "apps").tone === "apps",
      segs.find(s => s.key === "apps").tone);
check("free present", segs.some(s => s.key === "free"));
check("cache present when cache is non-zero",
      !snap["cached"] || segs.some(s => s.key === "cache"));
const totalSeg = segs.reduce((a, s) => a + s.bytes, 0);
check("segments sum to MemTotal",
  Math.abs(totalSeg - snap.mem_total) < 4 * 1048576,
  `${totalSeg} vs ${snap.mem_total}`);
const pcts = M.memoryPercentages(snap);
check("percentage bar uses the apps colour",
      pcts.find(s => s.key === "apps").tone === "apps",
      pcts.find(s => s.key === "apps").tone);
const sumPct = pcts.reduce((a, s) => a + s.fraction, 0);
check("percentages sum to ~1", Math.abs(sumPct - 1) < 0.02, String(sumPct));
check("empty snapshot is safe", M.memorySegments({}).length === 0);
check("null snapshot is safe", M.memorySegments(null).length === 0);

console.log("\n== state ==");
check("ok -> Healthy", M.stateLabel("ok") === "Healthy");
check("tight -> Low", M.stateLabel("tight") === "Low");
check("critical -> Critical", M.stateLabel("critical") === "Critical");
check("stalling -> Stalling", M.stateLabel("stalling") === "Stalling");
check("unknown state is safe", M.stateLabel(undefined) === "Healthy");
check("only critical is urgent", M.isUrgent("critical") && !M.isUrgent("tight") && !M.isUrgent("ok"));
check("tight and stalling warn", M.isWarning("tight") && M.isWarning("stalling") && !M.isWarning("ok"));
check("glyphs differ per state",
  new Set(["ok", "tight", "critical", "stalling"].map(M.stateGlyph)).size === 4);

console.log("\n== victims ==");
const victims = M.reclaimableGroups(snap);
check("lists multiple victims", victims.length === 2, String(victims.length));
check("never includes protected", victims.every(v => v.protected === false));
// D-23: sort invariant is descending `give` (recoverable bytes), not `bytes`
// (Firefox above has larger `current` 610 MB than Chromium 442 MB, but smaller `give`).
check("sorted by give desc",
  victims.every((v, i) => i === 0 || victims[i - 1].give >= v.give),
  victims.map(v => `${v.key}:give=${v.give},bytes=${v.bytes}`).join(" | "));
check("respects limit", M.reclaimableGroups(snap, 1).length === 1);
const prot = M.protectedGroups(snap);
check("protected list is all protected", prot.every(p => p.protected === true));
const inviolable = new Set(["wayland-wm@hyprland.desktop", "quickshell", "ram-keeper"]);
const compositorVictims = snap.groups.filter(g => !g.protected && (
  inviolable.has(g.name) || inviolable.has(g.name.replace(/\.service$/, ""))
));
check("protected excludes the compositor from victims",
  compositorVictims.length === 0,
  compositorVictims.map(g => g.name).join(", "));

const actionRows = M.protectedGroups({
  groups: [
    { name: "session-2.scope", label: "session", protected: true, inviolable: true, current: 400 * 1024 * 1024 },
    { name: "ollama.service", label: "ollama", protected: true, inviolable: false, current: 800 * 1024 * 1024 }
  ]
});
check("an inviolable row has no button",
  actionRows.find(r => r.key === "session-2.scope").action === "");
check("a protected row can be released",
  actionRows.find(r => r.key === "ollama.service").action === "release");
const freeRows = M.reclaimableGroups({
  groups: [
    { name: "app-Firefox-1.scope", label: "Firefox", protected: false, inviolable: false,
      current: 900 * 1024 * 1024, anon: 800 * 1024 * 1024, reclaimable: 100 * 1024 * 1024 }
  ]
});
check("a reclaimable row can be protected", freeRows[0] && freeRows[0].action === "protect");
const blocked = {
  state: "critical",
  relief: { acted: false, skipped_anon: "no-zram-room" }
};
check("a blocked swap is not called reclaiming",
  M.watchLine(blocked, true).indexOf("disk swap") >= 0
  && M.watchLine(blocked, true).indexOf("reclaiming now") < 0);
check("a manual result does not call a blocked swap comfortable",
  M.summaryLine({ acted: false, skipped_anon: "no-zram-room" }).indexOf("disk swap") >= 0);
const heldLine = M.watchLine({
  state: "critical",
  held_by: ["ollama", "hermes"],
  groups: []
}, true);
check("critical without a victim names the protected apps",
  heldLine.indexOf("ollama") >= 0 && heldLine.indexOf("hermes") >= 0
  && heldLine.indexOf("nothing needed") < 0);
const quickshellVictim = snap.groups.filter(g => !g.protected && /quickshell/i.test(g.name));
check("protected excludes quickshell from victims", quickshellVictim.length === 0,
  quickshellVictim.map(g => g.name).join(", "));

console.log("\n== swap ==");
const swap = M.swapRows(snap);
check("has swap rows", swap.length > 0);
const zramRow = swap.find(r => r.isZram);
check("zram identified", !!zramRow);
check("zram is NOT flagged as concern even when mostly full",
  zramRow && !zramRow.isConcern, zramRow && String(zramRow.fraction));
check("disk swap row labelled", swap.some(r => !r.isZram && /swap/i.test(r.label)));

console.log("\n== PSI ==");
const psi = M.psiRow(snap.psi);
check("reads real psi", psi && psi.stallPercent >= 0);
check("zero psi is ok severity", M.psiRow({ some_avg60: 0, full_avg60: 0 }).severity === "ok");
check("25% stall is critical", M.psiRow({ some_avg60: 25, full_avg60: 0 }).severity === "critical");
check("10% stall is warning", M.psiRow({ some_avg60: 10, full_avg60: 0 }).severity === "warning");
check("null psi is explicitly unavailable", M.psiRow(null).severity === "unavailable");
check("missing psi average is unavailable", M.psiRow({}).severity === "unavailable");
check("unavailable psi label", M.psiLabel(M.psiRow(null)) === "Unavailable");
check("label for none", M.psiLabel({ stallPercent: 0, severity: "ok" }) === "None");

console.log("\n== gauges ==");
check("free fraction", Math.abs(M.freeFraction(snap) - snap.free_percent / 100) < 0.001);
check("used complements free",
  Math.abs(M.usedFraction(snap) + M.freeFraction(snap) - 1) < 0.001);
check("clamped above 1", M.freeFraction({ mem_total: 100, free_percent: 150 }) === 1);
check("clamped below 0", M.freeFraction({ mem_total: 100, free_percent: -5 }) === 0);

console.log("\n== forecast ==");
const fc = M.forecastRow(snap.forecast);
check("forecastRow handles a real snapshot", fc !== null);
if (fc) {
  console.log(`  "${fc.text}"  urgency=${fc.urgency} conf=${fc.confidence.toFixed(2)}`);
  check("forecast text is non-empty", typeof fc.text === "string" && fc.text.length > 0);
  check("urgency is a known value",
        ["none", "soon", "imminent", "eventual"].includes(fc.urgency), fc.urgency);
}
check("forecastRow(null) is safe", M.forecastRow(null) === null);
check("forecastRow({}) is safe",
      M.forecastRow({}) && M.forecastRow({}).urgency === "none");

const imminent = M.forecastRow({
  horizon_seconds: 45, rate_bytes_per_sec: -8e6, will_cross: true,
  confidence: 0.9, culprit: "Chromium 123", urgency: "imminent"
});
console.log(`  imminent -> "${imminent.text}"`);
check("imminent is flagged as a warning", imminent.isWarning === true);
check("imminent names the culprit", /Chromium/.test(imminent.text), imminent.text);
check("imminent horizon is in seconds", /45s/.test(imminent.text), imminent.text);

const steady = M.forecastRow({ horizon_seconds: 0, rate_bytes_per_sec: -1e6,
  will_cross: false, confidence: 0.1, urgency: "none" });
console.log(`  steady -> "${steady.text}"`);
check("a quiet machine says so", /Steady/.test(steady.text), steady.text);

console.log("\n== horizon labels ==");
check("45s", M.horizonLabel(45) === "45s", M.horizonLabel(45));
check("5 min", M.horizonLabel(300) === "5 min", M.horizonLabel(300));
check("2.0 h", M.horizonLabel(7200) === "2.0 h", M.horizonLabel(7200));
check("zero is safe", typeof M.horizonLabel(0) === "string");
check("NaN is safe", typeof M.horizonLabel(NaN) === "string");
check("negative is safe", typeof M.horizonLabel(-5) === "string");

console.log("\n== trending is a known state ==");
check("trending -> Watching", M.stateLabel("trending") === "Watching", M.stateLabel("trending"));
check("trending glyph differs from ok", M.stateGlyph("trending") !== M.stateGlyph("ok"));
check("trending is a warning", M.isWarning("trending") === true);

console.log("\n== result summary ==");
check("no-action result reads as nothing to do",
  M.summaryLine({ acted: false }) === "Nothing to do — memory is comfortable");
check("action result reports freed and swapped",
  M.summaryLine({ acted: true, freed: 1073741824, swapped_out: 536870912 })
    === "freed 1.00 GB · moved 512 MB to zram",
  M.summaryLine({ acted: true, freed: 1073741824, swapped_out: 536870912 }));
check("null result is safe", M.summaryLine(null) === "");
check("empty action output is not a comfortable result", M.parseResult("") === null);
check("blank action output is not a comfortable result", M.parseResult("   ") === null);

console.log("\n== candidates are ordered by what we would actually recover ==");
const MB = 1024 * 1024;
const orderSnap = {
  mem_total: 8000 * MB, groups: [
    { name: "app-big-idle-1.scope", label: "big-idle", current: 2000 * MB,
      anon: 100 * MB, reclaimable: 50 * MB, protected: false, swapable: true },
    { name: "app-small-hungry-1.scope", label: "small-hungry", current: 500 * MB,
      anon: 400 * MB, reclaimable: 100 * MB, protected: false, swapable: true }
  ]
};
const ordered = M.reclaimableGroups(orderSnap, 8);
check("the hungrier process leads, not the biggest one",
  ordered.length === 2 && ordered[0].key === "app-small-hungry-1.scope",
  ordered.map(function (r) { return r.key + "=" + r.give; }).join(", "));

console.log("\n== D-19: non-array and null-item guards ==");
check("reclaimableGroups({groups: {}})", M.reclaimableGroups({ groups: {} }).length === 0);
check("reclaimableGroups({groups: 'oops'})", M.reclaimableGroups({ groups: "oops" }).length === 0);
check("reclaimableGroups({groups: [null]})", M.reclaimableGroups({ groups: [null] }).length === 0);
check("protectedGroups({groups: {}})", M.protectedGroups({ groups: {} }).length === 0);
check("protectedGroups({groups: [null]})", M.protectedGroups({ groups: [null] }).length === 0);
check("swapRows({zram: {devices: {}}})", M.swapRows({ zram: { devices: {} } }).length === 0);
check("swapRows({zram: {devices: [null]}})", M.swapRows({ zram: { devices: [null] } }).length === 0);

console.log("\n== D-21: numeric sanitization and swap/reclaim contracts ==");
check("swapable=false with 0 cache is excluded from reclaimableGroups",
  M.reclaimableGroups({
    mem_total: 8e9,
    groups: [{ name: "locked.scope", label: "LockedSwap0", current: 900e6,
               anon: 850e6, reclaimable: 0, swapable: false, protected: false }]
  }).length === 0);
const cacheOnlyVictim = M.reclaimableGroups({
  mem_total: 8e9,
  groups: [{ name: "nswap.scope", label: "NoSwap", current: 500 * MB,
             anon: 400 * MB, reclaimable: 100 * MB, swapable: false, protected: false }]
});
check("swapable=false counts only reclaimable cache in give",
  cacheOnlyVictim.length === 1 && cacheOnlyVictim[0].give === 100 * MB,
  String(cacheOnlyVictim[0] && cacheOnlyVictim[0].give));
check("two 10KB numeric strings do not concatenate above 64MB",
  M.reclaimableGroups({
    mem_total: 8e9,
    groups: [{ name: "tiny.scope", label: "Tiny10KB", current: 20000,
               anon: "10000", reclaimable: "10000", swapable: true, protected: false }]
  }).length === 0);
const coercedVictim = M.reclaimableGroups({
  groups: [{ name: "str.scope", label: "str", current: "500000000",
             anon: "400000000", reclaimable: "100000000", swapable: true, protected: false }]
});
check("numeric strings in groups coerce to numbers",
  coercedVictim.length === 1 && coercedVictim[0].give === 500000000);

const trickySwaps = M.swapRows({
  zram: {
    devices: [
      { name: "/swap/no-zram-disk.swap", size: 8e9, used: 6e9, ram_backed: false },
      { name: "/dev/ZRAM0", size: 8e9, used: 6e9, ram_backed: true },
      { name: "/swap/swapfile", size: 8e9, used: undefined, ram_backed: false }
    ]
  }
});
check("ram_backed=false overrides 'zram' substring in path",
  trickySwaps[0].isZram === false && trickySwaps[0].isConcern === true
  && /slow disk/.test(trickySwaps[0].label),
  JSON.stringify(trickySwaps[0]));
check("ram_backed=true identifies /dev/ZRAM0 as zram without concern",
  trickySwaps[1].isZram === true && trickySwaps[1].isConcern === false);
check("swapRows handles undefined used without NaN fraction",
  trickySwaps[2].fraction === 0 && Number.isFinite(trickySwaps[2].fraction));
check("swapRows fallback without ram_backed is case-insensitive",
  M.swapRows({ zram: { devices: [{ name: "/dev/ZRAM0", size: 1000, used: 500 }] } })[0].isZram === true);

const infPsi = M.psiRow({ some_avg60: 1, full_avg60: Infinity });
check("psiRow ignores Infinity full_avg60",
  infPsi.stallPercent === 1 && M.psiLabel(infPsi) === "1.0%",
  M.psiLabel(infPsi));
check("parseSnapshot rejects negative mem_total", M.parseSnapshot('{"mem_total":-100}') === null);
check("parseSnapshot rejects non-numeric mem_total", M.parseSnapshot('{"mem_total":"bad"}') === null);
check("parseSnapshot rejects JSON array", M.parseSnapshot("[]") === null);
check("parseResult rejects JSON array", M.parseResult("[]") === null);
check("freeFraction with bad free_percent is finite",
  M.freeFraction({ mem_total: 8e9, free_percent: "bad" }) === 1
  && M.usedFraction({ mem_total: 8e9, free_percent: "bad" }) === 0);
const infSegs = M.memorySegments({ mem_total: 8000, mem_used: Infinity, cached: Infinity, anon_and_kernel: -1 });
check("memorySegments handles Infinity mem_used/cached without NaN",
  infSegs.length > 0 && infSegs.every(s => Number.isFinite(s.bytes) && s.bytes >= 0));
const calmImminent = M.forecastRow({ urgency: "imminent", will_cross: false, rate_bytes_per_sec: 0 });
check("will_cross=false never flags Steady as isWarning",
  calmImminent.isWarning === false && /Steady/.test(calmImminent.text));
const negInfFc = M.forecastRow({ urgency: "none", will_cross: false, rate_bytes_per_sec: -Infinity });
check("rate=-Infinity sanitizes to Steady instead of Falling 0 MB/s",
  /Steady/.test(negInfFc.text), negInfFc.text);

console.log("\n== G-26: formatBytes boundaries and Recovering forecast ==");
check("formatBytes(null) is 0 MB", M.formatBytes(null) === "0 MB", M.formatBytes(null));
check("formatBytes('') is 0 MB", M.formatBytes("") === "0 MB", M.formatBytes(""));
check("formatBytes(4096) is 4 KB", M.formatBytes(4096) === "4 KB", M.formatBytes(4096));
check("formatBytes(1023.9 MB) rolls over to 1.00 GB",
  M.formatBytes(1023.9 * 1048576) === "1.00 GB", M.formatBytes(1023.9 * 1048576));
check("formatBytes(9.96 MB) rounds cleanly to 10 MB",
  M.formatBytes(9.96 * 1048576) === "10 MB", M.formatBytes(9.96 * 1048576));
check("formatBytes(9.996 GB) rolls over to 10.0 GB",
  M.formatBytes(9.996 * 1073741824) === "10.0 GB", M.formatBytes(9.996 * 1073741824));
const recovering = M.forecastRow({ urgency: "none", will_cross: false, rate_bytes_per_sec: 6 * MB });
check("positive rate renders 'Recovering +6.0 MB/s'",
  recovering.text === "Recovering +6.0 MB/s", recovering.text);

console.log("\n== F2 & F3: snooze, trend arrow, hero/tooltip ETA, session stats, thrashing ==");
const psTimed = M.parsePauseState({ paused: true, paused_remaining_s: 1500 });
check("timed snooze state parses countdown",
  psTimed.paused === true && psTimed.remaining === 1500 && /25 min/.test(psTimed.label),
  psTimed.label);
const psIndef = M.parsePauseState({ paused: true, paused_remaining_s: null });
check("indefinite pause state label",
  psIndef.paused === true && psIndef.label === "Paused indefinitely", psIndef.label);
check("watchLine reflects timed snooze",
  /snoozed.*25 min/i.test(M.watchLine({ paused: true, paused_remaining_s: 1500 }, false)));
check("watchLine reflects psi-full-thrash",
  /thrashing/i.test(M.watchLine({ state: "stalling", relief: { skipped_anon: "psi-full-thrash" } }, true)));
check("summaryLine reflects psi-full-thrash",
  /thrashing/i.test(M.summaryLine({ acted: false, skipped_anon: "psi-full-thrash" })));

check("trendArrow down on confident drop",
  M.trendArrow({ state: "ok", forecast: { rate_bytes_per_sec: -8 * MB, confidence: 0.6 } }) === " ↓");
check("trendArrow suppressed on noisy low-confidence jitter",
  M.trendArrow({ state: "ok", forecast: { rate_bytes_per_sec: -8 * MB, confidence: 0.1, will_cross: false } }) === "");
check("trendArrow up on recovery",
  M.trendArrow({ state: "ok", forecast: { rate_bytes_per_sec: 8 * MB, confidence: 0.5 } }) === " ↑");
check("barLabel combines percent and arrow",
  M.barLabel({ state: "trending", free_percent: 22.4,
               forecast: { rate_bytes_per_sec: -9 * MB, confidence: 0.8, will_cross: true, urgency: "soon" } }) === "22% ↓");
const warnSnap = {
  state: "trending", mem_total: 8 * 1073741824, mem_available: 1.8 * 1073741824, free_percent: 22.5,
  forecast: { horizon_seconds: 45, rate_bytes_per_sec: -9 * MB, will_cross: true,
              confidence: 0.85, culprit: "Chromium", urgency: "imminent" }
};
check("barTooltip surfaces ETA and culprit",
  /floor in 45s/.test(M.barTooltip(warnSnap)) && /Chromium/.test(M.barTooltip(warnSnap)),
  M.barTooltip(warnSnap));
check("heroMeta surfaces ETA and culprit",
  /floor in 45s/.test(M.heroMeta(warnSnap)) && /Chromium/.test(M.heroMeta(warnSnap)),
  M.heroMeta(warnSnap));

check("sessionImpactRow hides when absent or zero",
  M.sessionImpactRow(snap, 1800000000) === null
  && M.sessionImpactRow({ session_stats: { actions: 0, bytes_freed: 0 } }, 1800000000) === null);
const impact = M.sessionImpactRow({
  session_stats: {
    started_at: 1799990000, actions: 6, bytes_freed: 1500 * MB,
    last_relief_ts: 1799999820, last_relief_bytes: 210 * MB, last_relief_label: "Chromium"
  }
}, 1800000000);
check("sessionImpactRow formats saved total, actions, culprit, and age",
  impact !== null && /Saved 1\.46 GB/.test(impact.text) && /6 actions/.test(impact.text)
  && /210 MB \(Chromium\) freed 3 min ago/.test(impact.text),
  impact && impact.text);

const thrashVictim = M.reclaimableGroups({
  groups: [{ name: "app-heavy.scope", label: "Heavy", current: 600 * MB,
             anon: 300 * MB, reclaimable: 100 * MB, swapable: true, protected: false, thrashing: true }]
});
check("reclaimableGroups surfaces giveLabel and thrashing badge",
  thrashVictim.length === 1 && thrashVictim[0].thrashing === true
  && thrashVictim[0].subLabel === "can free 400 MB · thrashing",
  thrashVictim[0] && thrashVictim[0].subLabel);

console.log("\n== hostile snapshots must never throw ==");
let seed = 1234567;
function rand() { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; }
const weird = [null, undefined, "", "x", 0, -1, 1e308, NaN, Infinity, [], {}, true,
               [{ name: null }], { groups: null }, { groups: {} }, { groups: [null, {}] },
               { zram: { devices: {} } }, { zram: { devices: [null, { size: 0 }] } },
               { mem_total: 0, groups: [] }, { mem_total: "large" },
               { mem_total: 8000, anon_and_kernel: -5, cached: 1e9 },
               { mem_total: 8000, mem_used: Infinity, cached: Infinity, anon_and_kernel: -1 },
               { mem_total: 8000, anon_and_kernel: "NaN", cached: null, free_percent: "bad" }];
let thrown = null;
try {
  for (let i = 0; i < 500; i++) {
    const s = (i < weird.length) ? weird[i] : {
      mem_total: rand() * 1e10,
      mem_used: rand() * 1e10,
      anon_and_kernel: rand() * 1e10 - 5e9,
      cached: rand() * 1e10,
      free_percent: rand() * 200 - 50,
      memory_breakdown_valid: rand() > 0.5,
      memory_reading_valid: rand() > 0.5,
      paused: rand() > 0.7,
      paused_remaining_s: rand() > 0.5 ? rand() * 3600 : null,
      state: ["ok", "tight", "critical", "trending", "unknown", "bogus"][Math.floor(rand() * 6)],
      forecast: { urgency: ["none", "eventual", "soon", "imminent"][Math.floor(rand() * 4)],
                  horizon_seconds: rand() * 1e5, rate_bytes_per_sec: rand() * 1e7 - 5e6,
                  confidence: rand(), will_cross: rand() > 0.5, culprit: rand() > 0.7 ? "X" : "" },
      psi: { some_avg60: rand() * 100, full_avg60: rand() > 0.9 ? Infinity : rand() * 100, available: rand() > 0.2 },
      zram: { devices: [{ name: "zram0", size: rand() * 1e10, used: rand() * 1e10, ram_backed: rand() > 0.5 }] },
      session_stats: rand() > 0.5 ? { actions: rand() * 20, bytes_freed: rand() * 1e9,
                                      last_relief_ts: 1000, last_relief_bytes: rand() * 1e8,
                                      last_relief_label: "App" } : null,
      groups: [{ name: "g" + i, label: null, current: rand() * 1e10,
                 anon: rand() * 1e10, reclaimable: rand() * 1e10,
                 protected: rand() > 0.5, swapable: rand() > 0.5, thrashing: rand() > 0.8 }]
    };
    M.memorySegments(s); M.memoryPercentages(s); M.reclaimableGroups(s, 8);
    M.protectedGroups(s, 4); M.swapRows(s); M.psiRow(s && s.psi);
    M.psiLabel(M.psiRow(s && s.psi)); M.forecastRow(s && s.forecast);
    M.forecastForSnapshot(s); M.freePercentLabel(s); M.freeFraction(s);
    M.watchLine(s, rand() > 0.5); M.usedFraction(s);
    M.stateLabel(s && s.state); M.stateGlyph(s && s.state);
    M.parseJsonLine(JSON.stringify(s || {})); M.formatBytes(s && s.mem_total);
    M.parsePauseState(s); M.trendArrow(s); M.barLabel(s);
    M.barTooltip(s); M.heroMeta(s); M.sessionImpactRow(s, 2000);

    const segs = M.memorySegments(s);
    if (!Array.isArray(segs)) throw new Error("memorySegments did not return an array");
    segs.forEach(function (seg) {
      if (!isFinite(seg.bytes) || seg.bytes < 0) throw new Error("segment " + seg.key + " invalid: " + seg.bytes);
    });
    const pcts = M.memoryPercentages(s);
    pcts.forEach(function (seg) {
      if (!isFinite(seg.fraction) || seg.fraction < 0 || seg.fraction > 1.001) {
        throw new Error("fraction out of range: " + seg.key + "=" + seg.fraction);
      }
    });
    const ff = M.freeFraction(s);
    const uf = M.usedFraction(s);
    if (!isFinite(ff) || !isFinite(uf)) throw new Error("non-finite gauge fraction");
    for (const str of [M.formatBytes(s && s.mem_total), M.barLabel(s), M.barTooltip(s),
                       M.heroMeta(s), M.watchLine(s, true), M.psiLabel(M.psiRow(s && s.psi))]) {
      if (typeof str !== "string" || /NaN|undefined|Infinity/.test(str)) {
        throw new Error("bad string output: " + str);
      }
    }
  }
} catch (e) { thrown = e; }
check("500 hostile snapshots produce no exception or NaN/Infinity text",
  thrown === null, thrown && thrown.message);

console.log("\n== percentages never overflow the bar ===");
const over = M.memoryPercentages({ mem_total: 1000, mem_used: 2000,
  anon_and_kernel: 1500, cached: 500, memory_breakdown_valid: true });
const sum = over.reduce(function (a, s) { return a + s.fraction; }, 0);
check("clamped total <= 1", sum <= 1.001, "sum=" + sum);

console.log("\n== Service.qml behavioral harness (B-06, B-07, D-22, F2, F3) ==");
{
  const fs = require("fs");
  const vm = require("vm");
  const svcSrc = fs.readFileSync(path.join(PLUGIN, "qml", "Service.qml"), "utf8");

  function extractFn(src, sigStart) {
    const start = src.indexOf(sigStart);
    if (start < 0) throw new Error("missing in Service.qml: " + sigStart);
    const braceStart = src.indexOf("{", start);
    let depth = 0;
    for (let i = braceStart; i < src.length; i++) {
      if (src[i] === "{") depth++;
      else if (src[i] === "}") {
        depth--;
        if (depth === 0) {
          return src.slice(start, i + 1)
            .replace(/\(([^)]*)\)\s*:\s*\w+/, (_, args) => "(" + args.replace(/:\s*\w+/g, "") + ")");
        }
      }
    }
    throw new Error("unclosed function: " + sigStart);
  }

  function extractProcessOnExited(src, procId) {
    const idx = src.indexOf("id: " + procId);
    if (idx < 0) throw new Error("missing Process id: " + procId);
    const exIdx = src.indexOf("onExited:", idx);
    const fnIdx = src.indexOf("function", exIdx);
    const braceStart = src.indexOf("{", fnIdx);
    let depth = 0;
    for (let i = braceStart; i < src.length; i++) {
      if (src[i] === "{") depth++;
      else if (src[i] === "}") {
        depth--;
        if (depth === 0) return "(" + src.slice(fnIdx, i + 1) + ")";
      }
    }
    throw new Error("unclosed onExited for " + procId);
  }

  function makeServiceHarness() {
    const makeProc = () => ({ running: false, command: [], spawns: 0, onExited: null });
    const makeTimer = () => ({ restarted: 0, restart() { this.restarted++; } });
    const ctx = {
      Model: M,
      Date, Math, Number, String, JSON, Boolean, parseInt, isFinite, NaN,
      manifestReader: makeProc(),
      stateReader: makeProc(),
      protectProc: makeProc(),
      actionProc: makeProc(),
      daemonCtlProc: makeProc(),
      pauseRead: makeProc(),
      versionRestart: makeProc(),
      actionDoneTimer: makeTimer(),
      daemonCheckTimer: makeTimer(),
      postStartRefreshTimer: makeTimer(),
      protectRefreshTimer: makeTimer(),
      startDaemonTimer: makeTimer(),
      root: {
        manifestPath: "/plugin/manifest.json",
        diskVersion: "",
        helperPath: "/plugin/helper/ram_keeper.py",
        unitName: "ram-keeper.service",
        stateDir: "/tmp/rk-state",
        statePath: "/tmp/rk-state/state.json",
        pauseFlagPath: "/tmp/rk-state/paused-by-user",
        snapshot: {},
        loaded: false,
        lastError: "",
        busy: false,
        activeAction: "",
        lastResult: "",
        daemonUp: false,
        pausedByUser: false,
        pauseKnown: true,
        versionRestarted: false,
        lastHeartbeat: 0,
        pollTicks: 1,
        daemonCtlBusy: false,
        ctlStep: "",
        ctlPauseArg: "indefinite",
        queuedCtl: null,
        startFailed: false,
        stoppedAt: 0,
        lastCtlAt: 0,
        refreshPending: false,
        snoozed: false,
        snoozeRemaining: 0,
        pendingProtectKey: "",
        listVerb: "release"
      }
    };
    for (const k of ["manifestReader", "stateReader", "protectProc", "daemonCtlProc", "versionRestart"]) {
      let running = false;
      Object.defineProperty(ctx[k], "running", {
        get() { return running; },
        set(v) { running = Boolean(v); if (running) this.spawns++; }
      });
    }
    vm.createContext(ctx);
    const fnNames = [
      "function refresh()", "function applySnapshot(", "function considerVersionRestart()",
      "function refreshManifest()", "function applyManifest(", "function protect(",
      "function unprotect(", "function _parseMinutesArg(", "function _requestCtl(",
      "function _beginCtl(", "function snoozeDaemon(", "function pauseDaemon()",
      "function resumeDaemon()", "function startDaemon()", "function stopDaemon()",
      "function daemonActive()", "function noteHeartbeat()", "function status()"
    ];
    for (const sig of fnNames) {
      const code = extractFn(svcSrc, sig);
      const name = sig.match(/function\s+(\w+)/)[1];
      vm.runInContext(code + `; root.${name} = ${name};`, ctx);
    }
    ctx.stateReader.onExited = vm.runInContext(extractProcessOnExited(svcSrc, "stateReader"), ctx);
    ctx.daemonCtlProc.onExited = vm.runInContext(extractProcessOnExited(svcSrc, "daemonCtlProc"), ctx);
    ctx.versionRestart.onExited = vm.runInContext(extractProcessOnExited(svcSrc, "versionRestart"), ctx);
    ctx.protectProc.onExited = vm.runInContext(extractProcessOnExited(svcSrc, "protectProc"), ctx);
    return ctx;
  }

  // D-22: IpcHandler.status() before load never omits fields or reports 57-year age
  const h1 = makeServiceHarness();
  const stUnloaded = JSON.parse(h1.root.status());
  check("status() before first snapshot has state='unknown', free=null, ageSeconds=null",
    stUnloaded.loaded === false && stUnloaded.state === "unknown"
    && stUnloaded.free === null && stUnloaded.ageSeconds === null,
    JSON.stringify(stUnloaded));

  // D-22: refresh() while stateReader is running queues refreshPending
  h1.stateReader.running = true;
  h1.root.refresh();
  check("refresh() while stateReader running sets refreshPending",
    h1.root.refreshPending === true && h1.stateReader.spawns === 1);
  h1.stateReader.running = false;
  h1.stateReader.onExited(0);
  check("stateReader.onExited drains refreshPending with an immediate re-read",
    h1.root.refreshPending === false && h1.stateReader.spawns === 2 && h1.stateReader.running === true);
  h1.stateReader.running = false;

  // D-22: daemonActive() rejects future timestamps and honours interval_idle
  const nowSec = Date.now() / 1000;
  h1.root.lastHeartbeat = nowSec + 3600;
  h1.root.daemonActive();
  check("daemonActive() rejects checked_at far in the future", h1.root.daemonUp === false);
  h1.root.snapshot = { mem_total: 8e9, interval_idle: 120 };
  h1.root.lastHeartbeat = nowSec - 250;
  h1.root.daemonActive();
  check("daemonActive() honours interval_idle * 3 > 180s", h1.root.daemonUp === true);

  // B-06a & B-06b: rapid stopDaemon() -> startDaemon() serializes without race
  const h2 = makeServiceHarness();
  h2.root.stopDaemon();
  check("stopDaemon() starts pause-cli step",
    h2.daemonCtlProc.spawns === 1 && h2.root.ctlStep === "pause-cli" && h2.root.pausedByUser === true);
  h2.root.startDaemon();
  check("rapid startDaemon() queues resume without spawning concurrent process",
    h2.daemonCtlProc.spawns === 1 && h2.root.queuedCtl !== null && h2.root.pausedByUser === false);
  h2.daemonCtlProc.running = false;
  h2.daemonCtlProc.onExited(0); // finishes pause-cli, immediately dequeues resume-cli
  check("finishing pause-cli immediately dequeues resume-cli",
    h2.daemonCtlProc.spawns === 2 && h2.root.ctlStep === "resume-cli" && h2.root.queuedCtl === null);
  h2.daemonCtlProc.running = false;
  h2.daemonCtlProc.onExited(0); // finishes resume-cli -> starts systemctl start
  h2.daemonCtlProc.running = false;
  h2.daemonCtlProc.onExited(0); // finishes resume-start -> triggers immediate refresh()
  check("resume-start triggers immediate stateReader refresh",
    h2.root.daemonCtlBusy === false && h2.stateReader.spawns === 1 && h2.root.lastError === "");

  // B-06c: failed systemctl start within 180s does NOT lie daemonUp=true on stale state.json
  const h3 = makeServiceHarness();
  const staleNow = Date.now() / 1000;
  h3.root.stopDaemon();
  h3.daemonCtlProc.running = false;
  h3.daemonCtlProc.onExited(0);
  h3.stateReader.running = false;
  h3.root.startDaemon();
  h3.daemonCtlProc.running = false;
  h3.daemonCtlProc.onExited(0); // resume-cli ok -> resume-start
  h3.daemonCtlProc.running = false;
  h3.daemonCtlProc.onExited(1); // systemctl start FAILED!
  h3.root.applySnapshot(JSON.stringify({ state: "ok", mem_total: 8e9, free_percent: 40, checked_at: staleNow - 10 }));
  check("failed start keeps daemonUp=false despite stale <180s state.json (B-06c)",
    h3.root.startFailed === true && h3.root.daemonUp === false
    && /Could not start/.test(h3.root.lastError));

  // B-07: versionRestart clears lastError on success and refreshManifest caches diskVersion
  const h4 = makeServiceHarness();
  h4.root.applyManifest(JSON.stringify({ version: "1.2.0" }));
  h4.root.refreshManifest();
  h4.root.refreshManifest();
  check("refreshManifest() caches diskVersion instead of polling every 5s (B-07)",
    h4.manifestReader.spawns === 0 && h4.root.diskVersion === "1.2.0");
  h4.root.applySnapshot(JSON.stringify({ state: "ok", mem_total: 8e9, free_percent: 45,
                                         checked_at: Date.now() / 1000, version: "1.1.0" }));
  check("version mismatch triggers one restart", h4.versionRestart.spawns === 1 && h4.root.lastError !== "");
  h4.versionRestart.running = false;
  h4.versionRestart.onExited(0);
  check("successful versionRestart clears lastError (B-07)", h4.root.lastError === "");

  // F2: timed snooze expiry in state.json automatically unpauses UI
  const h5 = makeServiceHarness();
  const tNow = Date.now() / 1000;
  h5.root.applySnapshot(JSON.stringify({ state: "ok", mem_total: 8e9, free_percent: 40,
                                         checked_at: tNow - 60, paused: true, paused_remaining_s: 60 }));
  check("daemon-published snooze marks UI paused", h5.root.pausedByUser === true && h5.root.daemonUp === false);
  h5.root.applySnapshot(JSON.stringify({ state: "ok", mem_total: 8e9, free_percent: 40,
                                         checked_at: tNow, paused: false, paused_remaining_s: null }));
  check("expired daemon snooze automatically restores daemonUp=true (F2)",
    h5.root.pausedByUser === false && h5.root.snoozed === false && h5.root.daemonUp === true);
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
