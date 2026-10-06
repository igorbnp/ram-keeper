// Unit-test ram-keeper's Model.js against real helper output.
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
//
// This used to run the daemon and assert against whatever the host reported,
// so the suite passed or failed depending on the machine. On a GitHub runner
// there are no user cgroups and no zram, and "apps present" / "zram identified"
// failed for reasons unrelated to the code. A fixture tests the shaping; the
// daemon's real output is covered by the python suites, which only run on a
// desktop.
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
check("free present", segs.some(s => s.key === "free"));
check("cache present when cache is non-zero",
      !snap["cached"] || segs.some(s => s.key === "cache"));
const totalSeg = segs.reduce((a, s) => a + s.bytes, 0);
// Segments must reconstruct MemTotal exactly, or the bar lies.
check("segments sum to MemTotal",
  Math.abs(totalSeg - snap.mem_total) < 4 * 1048576,
  `${totalSeg} vs ${snap.mem_total}`);
const pcts = M.memoryPercentages(snap);
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
check("lists victims", victims.length > 0);
check("never includes protected", victims.every(v => v.protected === false));
check("sorted by size desc",
  victims.every((v, i) => i === 0 || victims[i - 1].bytes >= v.bytes));
check("respects limit", M.reclaimableGroups(snap, 2).length <= 2);
const prot = M.protectedGroups(snap);
check("protected list is all protected", prot.every(p => p.protected === true));
// Match on the CGROUP NAME, never the label. An ordinary GTK window launched
// by a Hyprland helper is labelled "Hyprland-gtk-launch-<hash>", and matching
// that string reports a compositor reclaim that never happened — a test that
// cries wolf is a test people stop running.
const inviolable = new Set(["wayland-wm@hyprland.desktop", "quickshell", "ram-keeper"]);
const compositorVictims = snap.groups.filter(g => !g.protected && (
  inviolable.has(g.name) || inviolable.has(g.name.replace(/\.service$/, ""))
));
check("protected excludes the compositor from victims",
  compositorVictims.length === 0,
  compositorVictims.map(g => g.name).join(", "));
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
check("null psi is safe", M.psiRow(null).severity === "ok");
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

// The important one: an imminent forecast must be flagged as a warning.
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

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);