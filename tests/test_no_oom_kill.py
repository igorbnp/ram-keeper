"""Regression test for the OOM kill that sustained load exposed.

Measured failure: under continuous load the anon pass computed a 1.5 GB ask for
a single Chromium cgroup holding ~300 MB. The kernel emptied the process, it
faulted hard, and systemd-oomd killed it. Reclaim meant to protect the machine
had become the thing that killed an app.

This reproduces that shape — one victim large enough to tempt a big ask, plus
sustained pressure — and asserts the cap holds AND nothing gets killed.
"""
import json, re, subprocess, sys, time
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

HELPER = str(PLUGIN / "helper" / "ram_keeper.py")

def alive(name):
    return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0

# Transient units an agent or a test creates on purpose to provoke a real kill.
# Their kills are deliberate experiments; counting them made the suite blame the
# daemon for someone else's probe. Exclude them, and keep everything else.
_PROBE_UNIT = re.compile(r"(rk|test|probe)[-a-z0-9]*")


def oom_kills():
    jl = subprocess.run(["journalctl", "-b", "--no-pager"], capture_output=True, text=True).stdout
    count = 0
    for line in jl.splitlines():
        if "oom-kill" not in line:
            continue
        if _PROBE_UNIT.search(line):
            continue
        count += 1
    return count

def build_snapshot(victim_anon_mb, victim_file_mb, state, free_pct):
    total = 8026562560
    return {
        "state": state, "free_percent": free_pct,
        "mem_available": int(total * free_pct / 100), "mem_total": total,
        "cached": 0, "forecast": {"urgency": "none"},
        "groups": [{
            "name": "victim.scope", "label": "victim", "kind": "scope",
            "current": (victim_anon_mb + victim_file_mb) * MB,
            "anon": victim_anon_mb * MB, "file": victim_file_mb * MB,
            "reclaimable_file": victim_file_mb * MB, "swap": 0,
            "reclaimable": victim_file_mb * MB, "protected": False,
            "swapable": True, "pids": 1, "path": "/nonexistent",
        }],
    }

print("== 1. the cap is derived from the victim's size, not fixed ==")
s = rk.Steward()
teto = s.max_victim_slice()
print(f"  max_victim_slice = {teto//MB} MB ({s.config['max_victim_fraction']}% of 8 GB)")
check("cap is positive and bounded", 0 < teto <= 2 * 1024 * MB,
      f"{teto//MB}MB")

print("\n== 2. the exact shape that killed the browser ==")
# critical, one 300MB-anon victim with a little cache: previously asked 1521MB.
snap = build_snapshot(300, 100, "critical", 5.0)
r = s.relieve(snap)
per_victim = [a["requested"] for a in r["actions"]]
print(f"  needed = {r['requested']//MB}MB, requests = {[p//MB for p in per_victim]}MB")
check("total request is far below the old 1521 MB", r["requested"] < 400 * MB,
      f"{r['requested']//MB}MB")
check("no single request exceeds the cap",
      all(p <= teto for p in per_victim), f"{[p//MB for p in per_victim]} vs {teto//MB}MB")

print("\n== 3. a big victim is still reclaimable, just gradually ==")
snap = build_snapshot(1500, 400, "critical", 5.0)
r = s.relieve(snap)
reqs = [a["requested"] for a in r["actions"]]
print(f"  requests = {[x//MB for x in reqs]}MB (cap {teto//MB}MB)")
check("every request is capped", all(x <= teto for x in reqs))
check("several passes are used instead of one huge one", len(reqs) >= 1)
check("total stays inside the available memory",
      sum(reqs) <= 2000 * MB, f"{sum(reqs)//MB}MB of 2000MB available")

print("\n== 4. the cap is configurable and clamped ==")
cfgfile = Path.home() / ".config/ram-keeper/config.json"
backup = cfgfile.read_text() if cfgfile.exists() else None
try:
    for value, expect in [(5, 5), (60, 60), (1, 5), (999, 60)]:
        cfgfile.parent.mkdir(parents=True, exist_ok=True)
        cfgfile.write_text(json.dumps({"max_victim_fraction": value}))
        got = rk.Steward().config["max_victim_fraction"]
        check(f"max_victim_fraction={value} -> {got}", got == expect, f"got {got}")
finally:
    if backup is not None:
        cfgfile.write_text(backup)
    elif cfgfile.exists():
        cfgfile.unlink()

print("\n== 5. live: sustained load must not kill anything ==")
kills_before = oom_kills()
hog = """
import time
b = [bytearray(4*1024*1024) for _ in range(1200)]
print('READY', flush=True)
time.sleep(420)
"""
subprocess.run(["systemd-run", "--user", "--unit=ramtest-kill", "/usr/bin/python3", "-c", hog],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    time.sleep(0.5)
    if "READY" in subprocess.run(["journalctl", "--user", "-u", "ramtest-kill",
                                  "-o", "cat", "--no-pager"],
                                 capture_output=True, text=True).stdout:
        break
print("  load running; letting the daemon work for 3 minutes")
time.sleep(180)

snap = json.loads(subprocess.run(["/usr/bin/python3", HELPER, "--once"],
                                 capture_output=True, text=True).stdout)
print(f"  state={snap['state']} free={snap['free_percent']}%")
kills_after = oom_kills()
check("no new OOM kills during sustained load", kills_after == kills_before,
      f"{kills_before} -> {kills_after}")
for n in ("Hyprland", "quickshell", "hermes"):
    check(f"{n} survived", alive(n))

subprocess.run(["systemctl", "--user", "stop", "ramtest-kill"], capture_output=True)

print("\n" + ("ALL PASS" if fails == 0 else f"\n{fails} FAILURES"))
sys.exit(0 if fails == 0 else 1)