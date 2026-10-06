"""Resilience: the daemon must survive every weird runtime condition.
Each scenario runs against the real helper. Success = the daemon still
produces a correct verdict and never raises.
"""
import sys
from pathlib import Path

# Resolve the plugin directory from this file's own location, so the suite runs
# from a fresh clone without knowing where omarchy installed it.
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import json, os, subprocess, sys, time

# ---------------------------------------------------------------------------
# This suite exercises the live machine: it runs the daemon, reads real
# cgroups, spawns a victim and checks the compositor survived. A GitHub runner
# has no user cgroups and no compositor, so running it there tests the runner,
# not the code. It reports as skipped rather than failing.
# ---------------------------------------------------------------------------
import os as _os
if not _os.path.exists("/sys/fs/cgroup/user.slice") or not _os.environ.get("CI"):
    pass
elif _os.environ.get("RAM_KEEPER_LIVE") != "1":
    print("  --   live suite skipped (no RAM_KEEPER_LIVE on a CI runner)")
    raise SystemExit(0)



HELPER = str(PLUGIN / "helper" / "ram_keeper.py")
fails = 0
def check(label, ok, detail=""):
    global fails
    if not ok:
        fails += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail and not ok else ""))
def once(timeout=60):
    p = subprocess.run(["/usr/bin/python3", HELPER, "--once"],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr
print("== 1. normal operation ==")
rc, out, err = once()
check("--once exits 0", rc == 0, f"rc={rc} err={err[:200]}")
try:
    d = json.loads(out)
    check("snapshot is well formed", d["mem_total"] > 0 and "state" in d)
    check("every group has the fields the panel reads",
          all(all(k in g for k in ("name", "label", "current", "anon",
                                   "reclaimable", "swap", "swapable", "protected"))
              for g in d["groups"]))
except Exception as e:
    check("snapshot parses", False, repr(e))
print("\n== 2. cgroup disappears mid-flight (the classic race) ==")
# Start a hog, kill it while a scan is walking the tree. A scan that assumed
# cgroups are stable would raise FileNotFoundError here.
hog = "b=[bytearray(1024*1024) for _ in range(400)]\nimport time\ntime.sleep(45)"
subprocess.run(["systemd-run", "--user", "--unit=ramtest-race", "/usr/bin/python3", "-c", hog],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)
victim = "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/ramtest-race.service"
check("victim cgroup exists", Path(victim).is_dir())
subprocess.run(["systemctl", "--user", "stop", "ramtest-race"], capture_output=True)
# Immediately scan: the cgroup is gone or mid-teardown.
rc, out, err = once()
check("scan survives a vanished cgroup", rc == 0, f"rc={rc} err={err[:300]}")
print("\n== 3. hostile reclaim target (a path that cannot be written) ==")
# Point the daemon at a cgroup it cannot reclaim: system.slice is EACCES.
out = subprocess.run(["/usr/bin/python3", "-c", f"""
import sys
sys.path.insert(0, {str(PLUGIN / "helper")!r})
import ram_keeper as rk
from pathlib import Path
g = rk.Cgroup(path=Path('/sys/fs/cgroup/system.slice'), name='system.slice',
              kind='slice', label='system', current=999, anon=999, file=0,
              reclaimable_file=0, swap=0, reclaimable=0)
s = rk.Steward()
a = s.reclaim_from(g, 64*1024*1024)
print('  outcome:', a.outcome)
"""], capture_output=True, text=True, timeout=60)
check("unwritable target returns denied, not a traceback",
      "outcome: denied" in out.stdout, out.stdout[-200:] + out.stderr[-200:])
check("no traceback leaked", "Traceback" not in out.stderr, out.stderr[:200])

print("\n== 4. reclaim of a cgroup that vanishes between read and write ==")
out = subprocess.run(["/usr/bin/python3", "-c", f"""
import sys
sys.path.insert(0, {str(PLUGIN / "helper")!r})
import ram_keeper as rk
from pathlib import Path
g = rk.Cgroup(path=Path('/sys/fs/cgroup/does/not/exist'), name='gone',
              kind='scope', label='gone', current=10**9, anon=10**9)
s = rk.Steward()
a = s.reclaim_from(g, 1024)
print('  outcome:', a.outcome)
"""], capture_output=True, text=True, timeout=60)
check("missing path is handled", "outcome:" in out.stdout and "Traceback" not in out.stderr,
      out.stderr[:300])

print("\n== 5. arithmetic guards ==")
# Patch time.sleep so the guard is exercised without actually waiting: the point
# is that _sleep normalises the value and calls sleep exactly once with
# something time.sleep would accept.
out = subprocess.run(["/usr/bin/python3", "-c", f"""
import sys
sys.path.insert(0, {str(PLUGIN / "helper")!r})
import ram_keeper as rk
calls = []
rk.time.sleep = lambda v: calls.append(v)
s = rk.Steward()
for bad in (0, -1, float('nan'), float('inf'), 'abc', None, -0.0, 1e300, -1e300):
    s._sleep(bad)
print('  all positive:', all(isinstance(c,(int,float)) and 0 < c <= 3600 for c in calls))
"""], capture_output=True, text=True, timeout=90)
check("_sleep normalises every bad value", "all positive: True" in out.stdout,
      out.stdout[-300:] + out.stderr[-300:])

out = subprocess.run(["/usr/bin/python3", "-c", f"""
import sys
sys.path.insert(0, {str(PLUGIN / "helper")!r})
import ram_keeper as rk
s = rk.Steward()
snap = s.snapshot()
# degenerate snapshot: zero total, zero free, no groups
snap['mem_total'] = 0
snap['free_percent'] = 0
snap['groups'] = []
r = s.relieve(snap)
print('  acted =', r['acted'])
"""], capture_output=True, text=True, timeout=90)
check("relieve handles a zero-total snapshot", "acted =" in out.stdout and "Traceback" not in out.stderr,
      out.stdout[-200:] + out.stderr[-400:])

print("\n== 6. forced relieves actually free memory and never kill anything ==")
before = subprocess.run(["/usr/bin/python3", HELPER, "--once"],
                        capture_output=True, text=True).stdout
b = json.loads(before)
r = subprocess.run(["/usr/bin/python3", HELPER, "--relieve"],
                   capture_output=True, text=True, timeout=90)
res = json.loads(r.stdout)
check("relieve returns a well formed result", "acted" in res and "actions" in res)
for name, cmd in (("Hyprland", ["pgrep", "-x", "Hyprland"]),
                  ("quickshell", ["pgrep", "-x", "quickshell"]),
                  ("hermes", ["pgrep", "-x", "hermes"])):
    check(f"{name} survived the forced relieve",
          subprocess.run(cmd, capture_output=True).returncode == 0)
# Match on the CGROUP NAME, not the human label: a label like
# "Hyprland-gtk-launch-1d9d67c9" is an ordinary GTK app window that merely
# mentions Hyprland, and matching on it reports a compositor reclaim that never
# happened. The compositor's real cgroup names are covered by is_inviolable.
from ram_keeper import is_inviolable
import json as _json
_snap = _json.loads(subprocess.run(
    ["/usr/bin/python3", HELPER, "--once"], capture_output=True, text=True).stdout)
_check = _json.loads(r.stdout)
touched = []
for a in _check["actions"]:
    for g in _snap["groups"]:
        if g["label"] == a["label"] and is_inviolable(g["name"]):
            touched.append((a["label"], g["name"]))
check("no inviolable cgroup was ever a target", not touched, str(touched))
check("compositor cgroup is protected in the scan",
      any(is_inviolable(g["name"]) for g in _snap["groups"] if "hyprland" in g["name"]),
      "compositor not found among scanned groups")
print("\nALL PASS" if fails == 0 else f"\n{fails} FAILURES")
sys.exit(0 if fails == 0 else 1)
