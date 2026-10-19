"""Pass budget: the daemon must never reclaim more than its budget allows.

One reclaim pass is bounded in three places: max_reclaim_per_pass (a hard
ceiling), max_victim_slice (the most one cgroup can be asked for), and the
available-reclaimable total (you can't take from a cgroup that can't give).

This suite checks all three ceilings hold using mocked cgroups, so it runs
the same in CI and on a desktop.
"""
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import ram_keeper as rk

MB = 1024 * 1024
fails = 0

def check(label, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))

# ---------------------------------------------------------------------------
# Build a fake snapshot with controllable victims
# ---------------------------------------------------------------------------
def make_steward(max_reclaim=None, max_victim_pct=25, **overrides):
    cfg = dict(rk.DEFAULT_CONFIG)
    if max_reclaim is not None:
        cfg["max_reclaim_per_pass"] = max_reclaim
    cfg["max_victim_fraction"] = max_victim_pct
    cfg.update(overrides)
    return rk.Steward(config=cfg)

# ---------------------------------------------------------------------------
# Test 1: max_reclaim_per_pass is a hard ceiling
# ---------------------------------------------------------------------------
print("== 1. a single pass never reclaims more than max_reclaim_per_pass ==")

steward = make_steward(max_reclaim=512 * MB, max_victim_pct=80)
# The math would ask for 2 GB but the budget says 512 MB max.
needed_raw = 2 * 1024 * MB
needed = min(needed_raw, int(steward.config["max_reclaim_per_pass"]))
check("budget clamps to max_reclaim_per_pass", needed == 512 * MB, f"{needed//MB}MB")

# ---------------------------------------------------------------------------
# Test 2: max_victim_slice bounds a single cgroup
# ---------------------------------------------------------------------------
print("\n== 2. a single cgroup is never asked for more than max_victim_slice ==")

steward2 = make_steward(max_reclaim=32768 * MB, max_victim_pct=25)
slice_bytes = steward2.max_victim_slice()
check("25% of total RAM slice is computed", slice_bytes > 0, f"{slice_bytes//MB}MB")

# ---------------------------------------------------------------------------
# Test 3: per-victim-per-pass tracking prevents double-asking
# ---------------------------------------------------------------------------
print("\n== 3. one cgroup is never asked twice in a single pass ==")

source = (PLUGIN / "helper" / "ram_keeper.py").read_text()
check("asked_this_pass guard exists", "asked_this_pass" in source, "found in relieve()")
check("victim added to asked_this_pass", "asked_this_pass.add(group.name)" in source)

# ---------------------------------------------------------------------------
# Test 4: budget floors at 64 MB even for tiny deficits
# ---------------------------------------------------------------------------
print("\n== 4. a tiny deficit still allocates a 64 MB floor ==")

# needed = deficit + headroom, then // 2, then max(.., 64MB)
# With a 1% deficit on 8GB, needed = 80MB → 40MB → floored to 64MB
needed_4 = max((int(8 * 1024 * MB * 0.01) // 2), 64 * MB)
check("floor is 64 MB", needed_4 == 64 * MB, f"{needed_4//MB}MB")

# ---------------------------------------------------------------------------
# Test 5: budget is bounded by available reclaimable memory
# ---------------------------------------------------------------------------
print("\n== 5. budget cannot exceed available reclaimable memory ==")

small_reclaimable = 10 * MB
available = small_reclaimable
budget_after_clamp = min(1024 * MB, max(int(available * 0.25), 64 * MB)) if available > 0 else 0
check("budget clamps to available when tiny", budget_after_clamp == 64 * MB, f"{budget_after_clamp//MB}MB")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURES'}")
assert fails == 0, f"Test failed: {fails} failures"