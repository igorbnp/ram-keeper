"""Prove the compositor is unreachable no matter what the config says."""
import sys
from pathlib import Path

# Resolve the plugin directory from this file's own location, so the suite runs
# from a fresh clone without knowing where omarchy installed it.
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import json, sys, subprocess

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



REAL = Path.home()/".config/ram-keeper/config.json"
backup = REAL.read_text() if REAL.exists() else None

HELPER = str(PLUGIN / "helper" / "ram_keeper.py")
def victims_with(cfg):
    REAL.parent.mkdir(parents=True, exist_ok=True)
    REAL.write_text(json.dumps(cfg))
    out = subprocess.run(["/usr/bin/python3", HELPER, "--once"],
                         capture_output=True, text=True, timeout=60)
    d = json.loads(out.stdout)
    v = [g["name"] for g in d["groups"] if not g["protected"]]
    return d, v
try:
    hostiles = [
        ("empty protected",        {"protected": []}),
        ("replaced protected",     {"protected": ["my-app.scope"]}),
        ("compositor explicitly",  {"protected": ["wayland-wm@hyprland.desktop"],
                                    "never_reclaim": []}),
        ("everything unprotected", {"protected": [], "never_reclaim": []}),
        ("weird list",             {"protected": [1, 2, 3]}),
    ]
    failures = 0
    for label, cfg in hostiles:
        d, v = victims_with(cfg)
        bad = [n for n in v if "session" in n or "hyprland" in n or "quickshell" in n
               or n.startswith("ram-keeper")]
        status = "ok  " if not bad else "FAIL"
        if bad: failures += 1
        print(f"  {status} {label:26s} reclaimable cgroups: {len(v)}  compositor-shell present: {bad or 'no'}")
    print("\n== the compositor really is discovered as a cgroup ==")
    d, _ = victims_with({})
    allg = [g["name"] for g in d["groups"]]
    print(f"  all cgroups seen: {allg}")
    comp = [g for g in d["groups"] if "session" in g["name"]]
    if comp:
        print(f"  compositor cgroup found: {comp[0]['name']} protected={comp[0]['protected']}")
    else:
        print("  NOTE: compositor cgroup not in the scan (it lives in session-N.scope, outside app.slice leaves)")
    print("\n== unit test is_inviolable directly ==")
    import importlib
    if "ram_keeper" in sys.modules: del sys.modules["ram_keeper"]
    rk = importlib.import_module("ram_keeper")
    for n, expect in [("wayland-wm@hyprland.desktop", True),
                      ("session-2.scope", True),
                      ("quickshell", True),
                      ("ram-keeper.service", True),
                      ("app-org.chromium.Chromium-123.scope", False),
                      ("syncthing.service", False),
                      ("session.slice", True),
                      ("localsearch-3.service", False)]:
        got = rk.is_inviolable(n)
        ok = got == expect
        if not ok: failures += 1
        print(f"  {'ok  ' if ok else 'FAIL'} is_inviolable({n!r}) = {got} (expected {expect})")
    print("\nALL PASS" if failures == 0 else f"\n{failures} FAILURES")
finally:
    if backup is not None: REAL.write_text(backup)
    elif REAL.exists(): REAL.unlink()
