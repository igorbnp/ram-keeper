"""Adversarial config tests: every one of these used to be able to kill or
disable the daemon. Each must now degrade to a working configuration.
"""
import json, subprocess, sys, tempfile, os


import sys
from pathlib import Path

# Resolve the plugin directory from this file's own location, so the suite runs
# from a fresh clone without knowing where omarchy installed it.
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

HELPER = str(PLUGIN / "helper" / "ram_keeper.py")
REAL = Path.home() / ".config/ram-keeper/config.json"
def run_with(cfg_text, label):
    """Run --once with a specific config file, restore the real one after."""
    backup = REAL.read_text() if REAL.exists() else None
    try:
        REAL.parent.mkdir(parents=True, exist_ok=True)
        REAL.write_text(cfg_text)
        p = subprocess.run(["/usr/bin/python3", HELPER, "--once"],
                           capture_output=True, text=True, timeout=60)
        ok_exit = p.returncode == 0
        cfg = {}
        if ok_exit:
            cfg = json.loads(p.stdout)
        print(f"  {'ok  ' if ok_exit else 'FAIL'} {label}")
        if not ok_exit:
            print(f"       exit={p.returncode} stderr={p.stderr.strip()[:200]}")
        else:
            warn = [l for l in p.stderr.splitlines() if "config:" in l]
            if warn:
                print(f"       warned: {warn[0].strip()[:110]}")
            if cfg:
                print(f"       target={cfg.get('state')} free={cfg.get('free_percent')}%")
        return ok_exit, cfg, p.stderr
    finally:
        if backup is not None:
            REAL.write_text(backup)
        elif REAL.exists():
            REAL.unlink()
cases = [
    ("valid override",            json.dumps({"target_free_percent": 22.0})),
    ("negative interval",          json.dumps({"interval_idle": -5.0})),
    ("zero interval_pressure",     json.dumps({"interval_pressure": 0})),
    ("string where number",        json.dumps({"interval_idle": "abc"})),
    ("null value",                 json.dumps({"target_free_percent": None})),
    ("bool where number",          json.dumps({"interval_idle": True})),
    ("inverted thresholds",        json.dumps({"target_free_percent": 5.0,
                                               "critical_free_percent": 40.0})),
    ("equal thresholds",           json.dumps({"target_free_percent": 18.0,
                                               "critical_free_percent": 18.0})),
    ("pressure slower than idle",  json.dumps({"interval_idle": 5.0,
                                               "interval_pressure": 60.0})),
    ("zero max reclaim",           json.dumps({"max_reclaim_per_pass": 0})),
    ("absurd percentages",         json.dumps({"target_free_percent": 1e9,
                                               "critical_free_percent": -1e9})),
    ("huge min_target",            json.dumps({"min_target_bytes": 10**18})),
    ("protected as bare string",   json.dumps({"never_reclaim": "my-app.scope"})),
    ("protected as number",        json.dumps({"protected": 42})),
    ("protected empty list",       json.dumps({"protected": [], "never_reclaim": []})),
    ("cache_first as string",      json.dumps({"cache_first": "yes"})),
    ("cache_first false",          json.dumps({"cache_first": False})),
    ("unknown keys",               json.dumps({"bogus_key": 1, "wat": [1,2]})),
    ("NaN literal",                '{"target_free_percent": NaN}'),
    ("Infinity literal",           '{"psi_some_percent": Infinity}'),
    ("truncated JSON",             '{"target_free_percent": '),
    ("JSON array not object",      '[1, 2, 3]'),
    ("JSON string not object",     '"hello"'),
    ("completely empty file",      ''),
    ("null document",              'null'),
]
print("== every bad config must still produce a working snapshot ==")
failures = 0
for label, text in cases:
    ok, cfg, err = run_with(text, label)
    if not ok:
        failures += 1
print(f"\n{len(cases) - failures}/{len(cases)} survived")
# Now prove the values are actually sane, not just non-crashing.
print("\n== inverted thresholds must be repaired, not accepted ==")
ok, cfg, err = run_with(json.dumps({"target_free_percent": 5.0,
                                    "critical_free_percent": 40.0}), "invert")
import subprocess as sp
# read the repaired config directly from the module
for mod in [m for m in list(sys.modules) if m == "ram_keeper"]:
    del sys.modules[mod]
REAL.parent.mkdir(parents=True, exist_ok=True)
backup = REAL.read_text() if REAL.exists() else None
try:
    REAL.write_text(json.dumps({"target_free_percent": 5.0, "critical_free_percent": 40.0,
                                "interval_idle": 5.0, "interval_pressure": 60.0,
                                "max_reclaim_per_pass": 0,
                                "never_reclaim": "solo.scope",
                                "target_free_percent": 5.0}))
    import importlib
    rk = importlib.import_module("ram_keeper")
    c = rk.load_config()
    print(f"  critical={c['critical_free_percent']} target={c['target_free_percent']} "
          f"-> critical < target: {c['critical_free_percent'] < c['target_free_percent']}")
    print(f"  interval_idle={c['interval_idle']} pressure={c['interval_pressure']} "
          f"-> pressure < idle: {c['interval_pressure'] < c['interval_idle']}")
    print(f"  max_reclaim={c['max_reclaim_per_pass']} -> > 0: {c['max_reclaim_per_pass'] > 0}")
    print(f"  never_reclaim={c['never_reclaim']} -> is a list of str: "
          f"{isinstance(c['never_reclaim'], list) and all(isinstance(x, str) for x in c['never_reclaim'])}")
    ok2 = (c["critical_free_percent"] < c["target_free_percent"]
           and c["interval_pressure"] < c["interval_idle"]
           and c["max_reclaim_per_pass"] > 0)
    print(f"  ALL RELATIONSHIPS HOLD: {ok2}")
    if not ok2:
        failures += 1
finally:
    if backup is not None:
        REAL.write_text(backup)
    elif REAL.exists():
        REAL.unlink()
print("\nALL PASS" if failures == 0 else f"\n{failures} FAILURES")
sys.exit(0 if failures == 0 else 1)
