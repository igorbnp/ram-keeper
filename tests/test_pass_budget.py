"""Pass budget: the daemon must never reclaim more than its budget allows.

One reclaim pass is bounded in four places, and every assertion here drives the
real `Steward.relieve()` with a stubbed `reclaim_from`, so what is checked is
what the code would have asked the kernel for:

  * max_reclaim_per_pass   — a hard ceiling on the whole pass
  * max_victim_slice       — the most ONE cgroup can be asked, across both passes
  * the available total    — a pass cannot ask for more than exists to give
  * the per-class floors   — cache is asked first, anon only when it helps

Earlier revisions of this file recomputed the budget formula in the test and
checked the result of that arithmetic, plus grepped the source for a string that
looked like the fix. Both passed while the anon pass was unreachable.
"""
import os
import sys
import tempfile
from pathlib import Path

# Never write into the real ~/.local/state/ram-keeper: this suite builds real
# Steward objects, and a pass that "frees" memory would append a fabricated
# reclaim event to the production log.
os.environ.setdefault("RAM_KEEPER_STATE_DIR", tempfile.mkdtemp(prefix="rk-test-state-"))
os.environ.setdefault("RAM_KEEPER_CONFIG", str(Path(tempfile.mkdtemp(prefix="rk-config-")) / "config.json"))

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


def group(name, current, anon=0, reclaimable=0, swapable=True, protected=False):
    return {
        "name": name, "kind": "scope", "label": name, "current": current,
        "anon": anon, "file": reclaimable, "reclaimable_file": reclaimable,
        "swap": 0, "reclaimable": reclaimable, "protected": protected,
        "swapable": swapable, "pids": 1,
        "path": f"/sys/fs/cgroup/fake/{name}",
    }


def snapshot(groups, state="critical", free_percent=4.0, total=8192 * MB,
             available=300 * MB, forecast=None):
    return {"state": state, "memory_reading_valid": True,
            "free_percent": free_percent, "mem_total": total,
            "mem_available": available, "mem_used": total - available,
            "groups": groups, "forecast": forecast,
            "zram": {"devices": [{"name": "/dev/zram0", "ram_backed": True,
                                  "size": 8192 * MB, "used": 0}]}}


class Recorder:
    def __init__(self):
        self.asks = []

    def __call__(self, victim, amount, cache_only=False):
        self.asks.append((victim.name, amount))
        return rk.Action(label=victim.label, requested=amount, freed=amount,
                         swapped_out=0, outcome="ok")

    def total(self, name=None):
        return sum(a for n, a in self.asks if name is None or n == name)


def steward(**overrides):
    cfg = dict(rk.DEFAULT_CONFIG)
    cfg["reclaim_sleep"] = 0
    cfg.update(overrides)
    st = rk.Steward(config=cfg)
    rec = Recorder()
    st.reclaim_from = rec
    return st, rec


# ---------------------------------------------------------------------------
print("== 1. max_reclaim_per_pass is a hard ceiling ==")
st, rec = steward(max_reclaim_per_pass=512 * MB, max_victim_fraction=60)
victims = [group(f"app-hog-{i}.scope", 4096 * MB, anon=3800 * MB,
                 reclaimable=200 * MB) for i in range(3)]
st.relieve(snapshot(victims))
check("no pass asks for more than the ceiling",
      rec.total() <= 512 * MB, f"asked {rec.total() // MB}MB")

# ---------------------------------------------------------------------------
print("\n== 2. one cgroup never exceeds its slice in a single pass ==")
st, rec = steward(max_reclaim_per_pass=4096 * MB, max_victim_fraction=25)
victims = [group("app-mixed-1.scope", 1024 * MB, anon=800 * MB, reclaimable=200 * MB)]
st.relieve(snapshot(victims))
slice_bytes = st.max_victim_slice(1024 * MB)
check("cache and anon together stay within the victim's slice",
      rec.total("app-mixed-1.scope") <= slice_bytes,
      f"asked {rec.total('app-mixed-1.scope') // MB}MB of {slice_bytes // MB}MB")

# ---------------------------------------------------------------------------
print("\n== 3. cache is exhausted before anon is touched ==")
st, rec = steward(max_reclaim_per_pass=4096 * MB, max_victim_fraction=60)
# The budget must outlive the cache for the ordering to be observable at all:
# 100MB of droppable cache against a ~400MB pass leaves room for pass 2.
st.relieve(snapshot([group("app-cache-1.scope", 2048 * MB, anon=1500 * MB,
                           reclaimable=100 * MB)]))
asks = [a for n, a in rec.asks if n == "app-cache-1.scope"]
check("the first request is bounded by droppable cache",
      asks and asks[0] <= 100 * MB, f"first ask {asks[0] // MB}MB")
check("more than one request was made (cache alone did not spend the budget)",
      len(asks) > 1, f"asks={[a // MB for a in asks]}")

# ---------------------------------------------------------------------------
print("\n== 4. a non-swappable victim is never asked for anonymous memory ==")
st, rec = steward(max_reclaim_per_pass=4096 * MB, max_victim_fraction=60)
st.relieve(snapshot([
    group("app-noswap-1.scope", 2048 * MB, anon=1800 * MB,
          reclaimable=100 * MB, swapable=False),
    group("app-swappy-1.scope", 2048 * MB, anon=900 * MB, reclaimable=900 * MB),
]))
check("non-swappable victim only gave cache",
      rec.total("app-noswap-1.scope") <= 100 * MB,
      f"asked {rec.total('app-noswap-1.scope') // MB}MB")
check("the swappable victim was used instead",
      rec.total("app-swappy-1.scope") > 0)

# ---------------------------------------------------------------------------
print("\n== 5. the pass is bounded by what actually exists to give ==")
st, rec = steward(max_reclaim_per_pass=4096 * MB, max_victim_fraction=60)
st.relieve(snapshot([group("app-small-1.scope", 120 * MB, anon=100 * MB,
                           reclaimable=10 * MB)], available=100 * MB))
ceiling = int((100 * MB) * 0.25)
check("a tiny pool cannot fund a large pass",
      rec.total() <= max(ceiling, 64 * MB),
      f"asked {rec.total() // MB}MB for a {ceiling // MB}MB reachable pool")

# ---------------------------------------------------------------------------
print("\n== 6. an empty machine produces no requests at all ==")
st, rec = steward()
st.relieve(snapshot([group("app-below-floor.scope", 20 * MB,
                           anon=10 * MB, reclaimable=5 * MB)]))
check("nothing above the floor is asked for anything", rec.asks == [], str(rec.asks))

# ---------------------------------------------------------------------------
print("\n== 7. protected and inviolable cgroups are unreachable ==")
st, rec = steward(max_reclaim_per_pass=4096 * MB, max_victim_fraction=60)
st.relieve(snapshot([
    group("wayland-wm@hyprland.desktop.service", 2048 * MB, anon=1800 * MB,
          reclaimable=100 * MB, protected=True),
    group("init.scope", 1024 * MB, anon=900 * MB, reclaimable=50 * MB),
    group("app-ok-1.scope", 1024 * MB, anon=900 * MB, reclaimable=100 * MB),
]))
check("the compositor was never asked", rec.total("wayland-wm@hyprland.desktop.service") == 0)
check("the user manager was never asked", rec.total("init.scope") == 0)
check("an ordinary app was still reclaimed", rec.total("app-ok-1.scope") > 0)

print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURES'}")
sys.exit(1 if fails else 0)
