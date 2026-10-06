// The `unknown` state must never render as "Healthy" — an unreadable signal
// claiming all-clear is worse than no signal.
const path = require("path");
// Resolve from this file's own location so the suite runs from a fresh clone.
const PLUGIN = path.join(__dirname, "..");
const M_PATH = path.join(PLUGIN, "qml", "Model.js");

const M = require(M_PATH);
let fail = 0;
const check = (n, ok, d) => { if (!ok) { fail++; console.log(`  FAIL ${n} — ${d||""}`); }
                             else console.log(`  ok   ${n}`); };

check("unknown is NOT 'Healthy'", M.stateLabel("unknown") !== "Healthy",
      `got "${M.stateLabel("unknown")}"`);
check("unknown reads as a problem", /no reading/i.test(M.stateLabel("unknown")),
      M.stateLabel("unknown"));
check("isUnknown('unknown')", M.isUnknown("unknown") === true);
check("isUnknown('ok') is false", M.isUnknown("ok") === false);
check("unknown counts as a warning", M.isWarning("unknown") === true);
check("unknown glyph differs from ok", M.stateGlyph("unknown") !== M.stateGlyph("ok"));

// With an unknown snapshot the numbers are zero, so every section must be empty
// rather than showing a misleading empty bar.
const unknownSnap = { mem_total: 0, mem_available: 0, free_percent: 0,
                      cached: 0, groups: [], zram: { devices: [] }, psi: {} };
check("no memory bars for unknown", M.memoryPercentages(unknownSnap).length === 0);
check("no legend for unknown", M.memorySegments(unknownSnap).length === 0);
check("no swap rows for unknown", M.swapRows(unknownSnap).length === 0);
check("no victims for unknown", M.reclaimableGroups(unknownSnap).length === 0);

// A healthy snapshot must still work (regression guard: the guard above must
// not have broken the normal case).
const healthy = { mem_total: 8026562560, mem_available: 3000000000,
                  mem_used: 5000000000, anon_and_kernel: 1200000000,
                  cached: 3800000000, free_percent: 37,
                  groups: [{ name: "app-x.scope", label: "x", current: 500*1048576,
                             anon: 400*1048576, reclaimable: 50*1048576,
                             swap: 0, swapable: true, protected: false }],
                  zram: { devices: [{ name: "/dev/zram0", used: 1e9, size: 7e9, ram_backed: true }] },
                  psi: { some_avg60: 0, full_avg60: 0 }, forecast: {} };
check("healthy still shows bars", M.memoryPercentages(healthy).length === 3,
      String(M.memoryPercentages(healthy).length));
check("healthy still shows victims", M.reclaimableGroups(healthy).length === 1);

console.log(fail === 0 ? "\nALL PASS" : `\n${fail} FAILURES`);
process.exit(fail ? 1 : 0);
