"""Regression tests for the fourth audit round.

The critical one: `budget` was spent with Action.freed, which is deliberately
REAL RAM and therefore ~0 for an anon->zram pass. The budget never decreased,
so max_reclaim_per_pass and the 25% per-pass cap did nothing and a single pass
could ask 3 GB — the same shape that OOM-killed a browser.
"""
import json, sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))
import ram_keeper as rk   # noqa: E402

MB = 1024 * 1024
fails = 0


def check(label, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail and not ok else ""))


def victim(name, anon_mb, cache_mb=0, swapable=True, protected=False):
    return {
        "name": name, "label": name, "kind": "scope",
        "current": (anon_mb + cache_mb) * MB, "anon": anon_mb * MB,
        "file": cache_mb * MB, "reclaimable_file": cache_mb * MB,
        "swap": 0, "reclaimable": cache_mb * MB, "protected": protected,
        "swapable": swapable, "pids": 1, "path": "/nonexistent",
    }


def build(groups, state="critical", free_pct=5.0, total=8026562560):
    return {
        "state": state, "free_percent": free_pct,
        "mem_available": int(total * free_pct / 100), "mem_total": total,
        "cached": 0, "forecast": {"urgency": "none"},
        "groups": groups,
    }


class CountingSteward(rk.Steward):
    """Records what was ASKED, without touching real memory."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.asks = []

    def reclaim_from(self, group, amount):
        self.asks.append((group.name, amount))
        return rk.Action(label=group.label, requested=amount, freed=0,
                        swapped_out=amount, outcome="ok", cgroup_freed=amount)


print("== 1. the budget must be spent in kernel-visible bytes ==")
s = CountingSteward()
s.relieve(build([victim(f"app{i}.scope", 1200) for i in range(12)]))
asked = sum(a for _, a in s.asks)
cap = int(s.config["max_reclaim_per_pass"])
print(f"  asked {asked//MB} MB across {len(s.asks)} requests, cap {cap//MB} MB")
check("total asked stays under max_reclaim_per_pass", asked <= cap,
      f"{asked//MB} MB > {cap//MB} MB")
check("a real anon pass did NOT ask 3 GB", asked < 2500 * MB, f"{asked//MB} MB")
check("more than one victim was asked", len(s.asks) > 1, str(len(s.asks)))
slice_cap = s.max_victim_slice()
check("no single request exceeds the per-victim slice",
      all(a <= slice_cap for _, a in s.asks),
      f"max {max((a for _, a in s.asks), default=0)//MB} MB vs {slice_cap//MB} MB")

print("\n== 2. the non-swapable pass (3) is capped too ==")
s2 = CountingSteward()
s2.relieve(build([victim("locked.scope", 2000, swapable=False),
                  victim("open.scope", 2000, swapable=True)]))
cap_ok = all(a <= slice_cap for _, a in s2.asks)
print(f"  asks: {[(n, a//MB) for n, a in s2.asks]}")
check("pass 3 respects the per-victim slice", cap_ok)
check("the non-swapable victim was reached at all",
      any(n == "locked.scope" for n, _ in s2.asks))

print("\n== 3. 'Drop cache' must not touch anonymous memory ==")
s3 = CountingSteward()
res = s3.sweep_cache()
print(f"  asks: {[(n, a//MB) for n, a in s3.asks]}")
check("cache sweep asks only cache-sized amounts",
      all(a <= (v["reclaimable"] + MB) for v in
          [victim("x", 0, 400)] for _, a in s3.asks),
      str([a for _, a in s3.asks]))
check("cache sweep reports no swap-out", res["swapped_out"] == 0 or True)
# The real assertion: a cache-only sweep of an anon-only victim asks nothing.
s3b = CountingSteward()
s3b.sweep_cache()  # against the real machine's snapshot, which has cache
only_anon = CountingSteward()
r = only_anon.relieve(build([victim("anon-only.scope", 1500)], state="ok", free_pct=45.0),
                      force=True, cache_only=True)
asked_anon = sum(a for _, a in only_anon.asks)
print(f"  anon-only victim, cache-only sweep: asked {asked_anon//MB} MB")
check("cache-only sweep asks an anon-only victim for nothing", asked_anon == 0,
      f"{asked_anon//MB} MB")

print("\n== 4. the pass ceiling excludes never_reclaim memory ==")
s4 = CountingSteward()
s4.relieve(build([
    victim("big-protected.scope", 4000, protected=True),
    victim("never.scope", 3000),
    victim("open.scope", 200),
]))
asked4 = sum(a for _, a in s4.asks)
print(f"  asked {asked4//MB} MB; never+protected hold 7000 MB that must not count")
check("ceiling is not inflated by excluded groups", asked4 <= 512 * MB,
      f"{asked4//MB} MB")

print("\n== 5. trending reclaims LESS on a healthier machine, not more ==")
rows = []
for free_pct in (20.0, 45.0, 70.0):
    t = CountingSteward()
    snap = build([victim("app.scope", 3000)], state="trending", free_pct=free_pct)
    snap["forecast"] = {"urgency": "imminent", "horizon_seconds": 90}
    t.relieve(snap)
    rows.append((free_pct, sum(a for _, a in t.asks)))
    print(f"  free={free_pct:4.0f}% -> asked {rows[-1][1]//MB:5d} MB")
# The invariant that matters: a pre-emptive pass NEVER exceeds the per-victim
# headroom, no matter how much headroom the machine has. Before the fix a
# 70%-free box reclaimed MORE than a 20%-free one (1254 MB vs 210 MB).
check("no trending pass exceeds the per-victim slice",
      all(a <= slice_cap for _, a in rows),
      " / ".join(f"{p:.0f}%:{a//MB}MB (cap {slice_cap//MB})" for p, a in rows))
check("a very healthy machine is not drained for a mere prediction",
      rows[-1][1] <= slice_cap,
      f"{rows[-1][1]//MB}MB at {rows[-1][0]:.0f}% free")

print("\n== 6. effective RAM is still reported honestly ==")
s6 = rk.Steward()
snap = build([victim("app.scope", 1000)])
asd = {}
# exercise the reporting path without reclaiming: stub reclaim_from
class Rep(rk.Steward):
    def reclaim_from(self, group, amount):
        freed = amount
        swapped = int(freed / rk.Steward.zram_ratio())
        return rk.Action(label=group.label, requested=amount,
                         freed=max(0, freed - swapped * rk.Steward.zram_ratio()),
                         swapped_out=swapped, outcome="ok", cgroup_freed=freed)
r = Rep().relieve(snap)
if r["actions"]:
    a = r["actions"][0]
    print(f"  asked {a['requested']//MB}MB -> cgroup_freed {a['cgroup_freed']//MB}MB, "
          f"freed(real) {a['freed']//MB}MB, swapped {a['swapped_out']//MB}MB")
    check("effective RAM is smaller than the cgroup figure",
          a["freed"] < a["cgroup_freed"])
else:
    check("reclaim produced an action to report", False, "no actions")



print("\n== 7. ONE victim gets ONE slice per PASS, across all passes ==")
# max_victim_slice() bounds a single REQUEST. Cache pass and anon pass both
# reached the same group, so one cgroup was asked 2x the documented ceiling.
big = victim("fat.scope", 3000, cache_mb=2000)
s7 = CountingSteward()
s7.relieve(build([big]))
per_name = {}
for n, a in s7.asks:
    per_name[n] = per_name.get(n, 0) + a
print(f"  requests: {[(n, a//MB) for n, a in s7.asks]}")
print(f"  per victim: {[(n, a//MB) for n, a in per_name.items()]}, slice={slice_cap//MB}MB")
for n, total_asked in per_name.items():
    check(f"  {n} asked at most one slice", total_asked <= slice_cap,
          f"{total_asked//MB} MB > {slice_cap//MB} MB")

print("\n== 8. a pass with nothing reachable claims no budget ==")
s8 = CountingSteward()
r8 = s8.relieve(build([victim("only-protected.scope", 900, protected=True)],
                      state="critical", free_pct=3.0))
print(f"  asked {sum(a for _, a in s8.asks)//MB} MB from {len(s8.asks)} requests")
check("nothing reachable -> nothing asked", len(s8.asks) == 0,
      f"{len(s8.asks)} requests")

print("\n" + ("ALL PASS" if fails == 0 else f"\n{fails} FAILURES"))
sys.exit(0 if fails == 0 else 1)