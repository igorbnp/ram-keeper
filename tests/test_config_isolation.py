"""The suite must not read or write the real config file.

A previous revision of the live scripts copied ~/.config/ram-keeper/config.json
aside, overwrote it with hostile JSON, and restored it afterwards. If the
daemon reloaded during that window it acted on the hostile file. The override
is RAM_KEEPER_CONFIG, and this file fails if a test goes back to the real path.
"""
import os
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import ram_keeper as rk  # noqa: E402

TESTS = Path(__file__).resolve().parent
REAL_MARKERS = (
    ".config/ram-keeper/config.json",
    ".config\\ram-keeper\\config.json",
)
# Scripts pytest does not collect. Each one imports the helper or runs it,
# so each one has to point RAM_KEEPER_CONFIG at a throwaway file itself.
SCRIPT_SUITES = (
    "test_pass_budget.py",
    "test_forecast.py",
    "test_config_hostile.py",
    "test_inviolable.py",
    "test_resilience.py",
    "test_no_oom_kill.py",
    "test_critical_metrics.py",
    "test_hostile_install_path.sh",
    "run-all.sh",
)


def test_config_path_follows_the_environment(monkeypatch, tmp_path):
    target = tmp_path / "ram-keeper" / "config.json"
    monkeypatch.setenv("RAM_KEEPER_CONFIG", str(target))
    assert rk.config_path() == target


def test_config_path_is_the_home_file_when_unset(monkeypatch):
    monkeypatch.delenv("RAM_KEEPER_CONFIG", raising=False)
    assert rk.config_path() == Path.home() / ".config" / "ram-keeper" / "config.json"


def test_load_config_reads_the_override_not_the_home_file(monkeypatch, tmp_path):
    target = tmp_path / "ram-keeper" / "config.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"target_free_percent": 22.0}\n', encoding="utf-8")
    monkeypatch.setenv("RAM_KEEPER_CONFIG", str(target))
    loaded = rk.load_config()
    assert loaded["target_free_percent"] == 22.0
    assert rk.config_path() == target


def test_no_test_names_the_real_config_path():
    bad = []
    for path in list(TESTS.glob("*.py")) + list(TESTS.glob("*.sh")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in REAL_MARKERS:
            if marker in text:
                bad.append(f"{path.name} contains {marker}")
    assert bad == [], bad


def test_script_suites_set_a_private_config_path():
    missing = []
    for name in SCRIPT_SUITES:
        text = (TESTS / name).read_text(encoding="utf-8")
        if "RAM_KEEPER_CONFIG" not in text:
            missing.append(name)
    assert missing == [], missing


def test_override_is_active_for_this_process():
    """conftest points every pytest at a temp file before the test body."""
    assert os.environ.get("RAM_KEEPER_CONFIG")
    assert rk.config_path() != Path.home() / ".config" / "ram-keeper" / "config.json"
