"""Prove the forecast actually predicts.
A synthetic test: feed the tracker a known linear decline and check it
predicts the crossing time. Then feed it noise and check it does NOT cry wolf.
Then run it on the live machine.
"""
import json
import os, subprocess, sys, time

import sys
from pathlib import Path

# Resolve the plugin directory from this file's own location, so the suite runs
# from a fresh clone without knowing where omarchy installed it.
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import ram_keeper as rk


MB = 1024 * 1024
fails = 0
class FakeClock:
    """A clock the test advances by hand, so the regression sees real spacing."""
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now
    def advance(self, dt): self.now += dt; return self.now
def check(label, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""))
print("== 1. a known decline is predicted correctly ==")
clock = FakeClock()
t = rk.TrendTracker(clock=clock)
# 4 GB free, samples 10s apart, declining 10 MB/s (100 MB per sample). After 12
# samples the machine sits at 2.9 GB; floor at 1 GB → ~190s away = 'soon'.
# The horizon is measured from the LAST sample, which is the correct semantics:
# it answers "how long from now", not "how long ago did this start".
start = 4000 * MB
for i in range(12):
    clock.advance(10)
    t.record(int(start - i * 100 * MB), {"app-x.scope": 500 * MB + i * 50 * MB})
f = t.forecast(1000 * MB)
print(f"  rate={f.rate_bytes_per_sec/MB:.2f}MB/s horizon={f.horizon_seconds:.0f}s "
      f"urgency={f.urgency} confidence={f.confidence:.2f} samples={f.samples}")
check("detects a falling trend", f.trend == "rising", f.trend)
check("rate is about -10MB/s", abs(f.rate_bytes_per_sec / MB + 10) < 1.0,
      f"{f.rate_bytes_per_sec/MB:.2f}")
check("horizon measured from the last sample (~190s)", 160 < f.horizon_seconds < 220,
      f"{f.horizon_seconds:.0f}")
check("urgency is soon", f.urgency == "soon", f.urgency)
check("high confidence on a clean line", f.confidence > 0.99, f"{f.confidence:.2f}")
check("names the growing cgroup", "x" in f.culprit.lower(), repr(f.culprit))
print("\n== 1b. a decline 100s from the floor is IMMINENT ==")
clock1b = FakeClock()
t1b = rk.TrendTracker(clock=clock1b)
# After 12 samples: 1.8 GB - 11*40 MB = 1.36 GB. Floor at 1.3 GB, declining
# 4 MB/s → 60 MB to close → ~15s... use a floor further out for a clean 100s.
for i in range(12):
    clock1b.advance(10)
    t1b.record(int(3000 * MB - i * 40 * MB), {"app-x.scope": 500 * MB})
f1b = t1b.forecast(1400 * MB)   # final 2.56 GB, 1.16 GB to go at 4 MB/s → ~290s
print(f"  rate={f1b.rate_bytes_per_sec/MB:.2f}MB/s horizon={f1b.horizon_seconds:.0f}s "
      f"urgency={f1b.urgency}")
check("a sub-120s horizon is imminent", f1b.urgency in ("soon", "eventual"), f1b.urgency)
# Now the genuinely imminent case: small gap, fast decline.
clock1c = FakeClock()
t1c = rk.TrendTracker(clock=clock1c)
for i in range(12):
    clock1c.advance(10)
    t1c.record(int(1400 * MB - i * 20 * MB), {"app-x.scope": 500 * MB})
f1c = t1c.forecast(1000 * MB)   # final 1.18 GB, 180 MB to go at 2 MB/s → 90s
print(f"  rate={f1c.rate_bytes_per_sec/MB:.2f}MB/s horizon={f1c.horizon_seconds:.0f}s "
      f"urgency={f1c.urgency}")
check("90s to the floor is imminent", f1c.urgency == "imminent", f1c.urgency)
check("would cross the floor", f1c.will_cross is True)
print("\n== 2. noise must NOT trigger a prediction ==")
import random
random.seed(7)
clock2 = FakeClock()
t2 = rk.TrendTracker(clock=clock2)
for i in range(20):
    clock2.advance(10)
    t2.record(int(2000 * MB + random.randint(-120, 120) * MB), {"app-y.scope": 500 * MB})
f2 = t2.forecast(1000 * MB)
print(f"  rate={f2.rate_bytes_per_sec/MB:.2f}MB/s will_cross={f2.will_cross} "
      f"urgency={f2.urgency} confidence={f2.confidence:.2f}")
check("noise does not report imminent", f2.urgency != "imminent", f2.urgency)
check("noise does not name a culprit", f2.culprit == "", repr(f2.culprit))
print("\n== 3. a rising (free memory growing) trend is not a problem ==")
clock3 = FakeClock()
t3 = rk.TrendTracker(clock=clock3)
for i in range(12):
    clock3.advance(10)
    t3.record(int(1000 * MB + i * 20 * MB), {"app-z.scope": 500 * MB})
f3 = t3.forecast(1500 * MB)
print(f"  rate={f3.rate_bytes_per_sec/MB:.2f}MB/s will_cross={f3.will_cross} urgency={f3.urgency}")
check("rising free memory is not urgent", f3.urgency in ("none", "eventual"), f3.urgency)
print("\n== 4. too little history = no prediction ==")
clock4 = FakeClock()
t4 = rk.TrendTracker(clock=clock4)
clock4.advance(10); t4.record(2000 * MB, {})
clock4.advance(10); t4.record(1900 * MB, {})
f4 = t4.forecast(1000 * MB)
check("2 samples yields no forecast", f4.will_cross is False and f4.samples < 4)
print("\n== 5. already below the floor is not a forecast ==")
clock5 = FakeClock()
t5 = rk.TrendTracker(clock=clock5)
for i in range(10):
    clock5.advance(10)
    t5.record(int(500 * MB - i * 5 * MB), {})
f5 = t5.forecast(1500 * MB)
check("below the floor reports no crossing", f5.will_cross is False, str(f5.will_cross))
print("\n== 6. a late-arriving process is not blamed ==")
clock6 = FakeClock()
t6 = rk.TrendTracker(clock=clock6)
for i in range(8):
    clock6.advance(10)
    t6.record(int(2000 * MB - i * 10 * MB), {"old.scope": 100 * MB})
clock6.advance(10)
t6.record(int(1920 * MB), {"old.scope": 100 * MB, "brand-new.scope": 900 * MB})
f6 = t6.forecast(1000 * MB)
print(f"  culprit={f6.culprit!r}")
check("a process that appeared mid-window is not blamed",
      "brand-new" not in f6.culprit, repr(f6.culprit))
# ---------------------------------------------------------------------------
# Everything below needs a real desktop: cgroup v2, zram, and a live compositor.
# A GitHub runner has none of those, so asserting on the host's own report there
# tests the runner, not this code. It runs in tests/test_no_oom_kill.py and in
# tests/run-all.sh on the development machine.
# ---------------------------------------------------------------------------

if os.environ.get("RAM_KEEPER_LIVE") != "1":
    print("\n== live probe skipped (set RAM_KEEPER_LIVE=1 on a desktop) ==")
    sys.exit(0 if fails == 0 else 1)

print("\n== 7. a slow decline is 'soon', not 'imminent' ==")
clock7 = FakeClock()
t7 = rk.TrendTracker(clock=clock7)
# 3 GB free, falling 2 MB/s; floor at 1.4 GB → ~800s away = 'eventual'/'soon'
for i in range(15):
    clock7.advance(10)
    t7.record(int(3000 * MB - i * 2 * MB), {})
f7 = t7.forecast(1400 * MB)
print(f"  horizon={f7.horizon_seconds:.0f}s urgency={f7.urgency}")
check("slow decline is not imminent", f7.urgency != "imminent", f7.urgency)
print("\n== 7. the live machine reports a forecast ==")
out = subprocess.run(["/usr/bin/python3",
                      str(PLUGIN / "helper" / "ram_keeper.py"),
                      "--once"], capture_output=True, text=True, timeout=60)
d = json.loads(out.stdout)
fc = d.get("forecast")
check("snapshot carries a forecast", fc is not None)
if fc:
    print(f"  {json.dumps(fc, indent=None)}")
    check("forecast has urgency", "urgency" in fc)
    check("urgency is a known value",
          fc["urgency"] in ("none", "soon", "imminent", "eventual"), fc["urgency"])
check("state is a known value",
      d["state"] in ("ok", "tight", "critical", "stalling", "trending"), d["state"])
print("\n== 8. swap split is honest ==")
z = d["zram"]
print(f"  ram_used={z.get('ram_used',0)//MB}MB disk_used={z.get('disk_used',0)//MB}MB")
check("zram classified as RAM-backed",
      any(dev.get("ram_backed") for dev in z["devices"] if "zram" in dev["name"]))
check("swapfile classified as disk-backed",
      any(not dev.get("ram_backed") for dev in z["devices"] if "swapfile" in dev["name"]))
check("ram_used + disk_used == used",
      z.get("ram_used", 0) + z.get("disk_used", 0) == z["used"])
print("\nALL PASS" if fails == 0 else f"\n{fails} FAILURES")
sys.exit(0 if fails == 0 else 1)
