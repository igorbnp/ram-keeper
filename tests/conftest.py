"""Collection rules for pytest and isolation for every test.

The suite has two shapes, deliberately:

* **pytest modules** — a shared fixture model, one assertion per behaviour
  (test_ram_keeper_core, test_reclaim_policy, test_config_attacks,
  test_unit_hardening, test_integration, test_process_guardian,
  test_bugfixes). Run them with `python3 -m pytest tests/`.
* **plain scripts** — self-contained, print PASS/FAIL lines and exit with a
  status (test_forecast, test_pass_budget, test_config_hostile,
  test_inviolable, test_resilience, test_no_oom_kill, test_critical_metrics,
  test_qml_structure). They run their whole file on import, so pytest cannot
  import them: it used to die with an INTERNALERROR from their module-level
  `sys.exit()`. They are listed here and executed by tests/run-all.sh and CI.

A file must be in exactly one of those two groups.
"""

import pytest

collect_ignore = [
    "test_pass_budget.py",
    "test_forecast.py",
    "test_config_hostile.py",
    "test_inviolable.py",
    "test_resilience.py",
    "test_no_oom_kill.py",
    "test_critical_metrics.py",
    "test_qml_structure.py",
]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """No test may read or write the real config file.

    The path is a fresh file under tmp_path. Tests that need a specific
    document write that file, or point RAM_KEEPER_CONFIG somewhere else.
    """
    cfg = tmp_path / "ram-keeper" / "config.json"
    monkeypatch.setenv("RAM_KEEPER_CONFIG", str(cfg))


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Every test writes state and logs into its own directory.

    Without this, a test that builds a real Steward with a synthetic snapshot
    appends fabricated "reclaim" events to ~/.local/state/ram-keeper/log.jsonl
    — the production audit trail. It happened: 45 events claiming critical
    pressure on a machine that never went below ~38% free.
    """
    monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path / "rk-state"))

