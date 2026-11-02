"""Adversarial config-file tests.

The config is the one file a user (or a sync tool, or an editor crash) can hand
the daemon in any shape at all. Every case here is something a real machine
produced or could produce; none of them may crash the daemon, and none may
silently turn protection off.

These run against a temporary config path, never the live one.

The CI matrix starts at Python 3.9, where `bytes | str` in an annotation is
evaluated at import time and raises TypeError; PEP 563 fixes that.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import ram_keeper as rk  # noqa: E402

HELPER = str(PLUGIN / "helper" / "ram_keeper.py")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(rk, "config_path", lambda: path)
    return path


def load(cfg, payload: bytes | str):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    cfg.write_bytes(payload)
    return rk.load_config()


# ---------------------------------------------------------------------------
# 1. Hostile bytes: the file is not what json.loads expects
# ---------------------------------------------------------------------------

def test_invalid_utf8_does_not_raise(cfg):
    """read_text() raises UnicodeDecodeError — a ValueError, not an OSError.

    A config half-written by an editor, or copied from a FAT volume, used to
    escape load_config() as a traceback: the daemon's hot reload would raise,
    the panel's --once would print a stack trace instead of JSON, and the user
    would see "no reading" with no explanation.
    """
    cfg_ok = load(cfg, b'{"target_free_percent": 25.0, "never_reclaim": ["a\xffb"]}')
    assert cfg_ok["target_free_percent"] == rk.DEFAULT_CONFIG["target_free_percent"], (
        "a config with invalid UTF-8 did not fall back to defaults")
    assert rk.config_warnings(), "the fallback was silent"


def test_bom_is_tolerated(cfg):
    """Windows editors prepend a BOM; json.loads rejects it as 'Unexpected BOM'."""
    cfg_ok = load(cfg, b"\xef\xbb\xbf" + b'{"target_free_percent": 25.0}')
    assert cfg_ok["target_free_percent"] == 25.0, (
        "a BOM made a perfectly readable config fall back to defaults")


def test_deeply_nested_document_does_not_raise(cfg):
    """5000 levels deep is valid JSON; it must not take the daemon with it."""
    nested = "[" * 5000 + "]" * 5000
    cfg_ok = load(cfg, nested)
    assert cfg_ok["target_free_percent"] == rk.DEFAULT_CONFIG["target_free_percent"]


def test_recursion_error_from_json_is_contained(cfg, monkeypatch):
    """On Pythons whose C scanner recurses, json.loads raises RecursionError.

    That is a RuntimeError, so `except ValueError` around json.loads misses it
    and the daemon's reload dies instead of falling back to defaults.
    """
    cfg.write_text("{}")

    def boom(*_args, **_kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(rk.json, "loads", boom)
    loaded = rk.load_config()
    assert loaded["target_free_percent"] == rk.DEFAULT_CONFIG["target_free_percent"]
    assert any("nested" in w for w in rk.config_warnings()), rk.config_warnings()


def test_absurd_integer_does_not_raise(cfg):
    """float() on a 400-digit integer raises OverflowError."""
    cfg_ok = load(cfg, '{"target_free_percent": ' + "9" * 400 + "}")
    assert cfg_ok["target_free_percent"] <= 60.0


def test_config_path_is_a_directory(cfg):
    cfg.mkdir()
    loaded = rk.load_config()
    assert loaded["target_free_percent"] == rk.DEFAULT_CONFIG["target_free_percent"]


def test_config_is_a_dangling_symlink(cfg):
    cfg.symlink_to(cfg.parent / "missing-target.json")
    loaded = rk.load_config()
    assert loaded["target_free_percent"] == rk.DEFAULT_CONFIG["target_free_percent"]


def test_duplicate_keys_last_wins_without_crashing(cfg):
    cfg_ok = load(cfg, '{"target_free_percent": 25.0, "target_free_percent": 30.0}')
    assert cfg_ok["target_free_percent"] == 30.0


def test_large_junk_file_degrades(cfg):
    load(cfg, "x" * (2 * 1024 * 1024))
    loaded = rk.load_config()
    assert loaded["interval_idle"] == rk.DEFAULT_CONFIG["interval_idle"]


def test_valid_config_produces_no_warnings(cfg):
    load(cfg, json.dumps({"target_free_percent": 21.0, "never_reclaim": ["spotify"]}))
    assert rk.config_warnings() == [], rk.config_warnings()


# ---------------------------------------------------------------------------
# 2. --protected-add: the CLI write path
# ---------------------------------------------------------------------------

def run_add(tmp_path, name, home):
    return subprocess.run(
        [sys.executable, HELPER, "--protected-add", name],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "HOME": str(home)},
    )


def test_protected_add_is_concurrent_safe(tmp_path):
    """Two clicks (or a panel button plus a CLI call) must not lose one.

    The old implementation read, modified and rewrote the file with no lock:
    one of two concurrent additions was silently overwritten. The same write
    was also non-atomic, so a daemon reload landing mid-write read a partial
    file, fell back to DEFAULTS, and un-protected everything.
    """
    home = tmp_path
    names = [f"app-Parallel-{i}.scope" for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda n: run_add(tmp_path, n, home), names))
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]

    cfg_file = home / ".config" / "ram-keeper" / "config.json"
    data = json.loads(cfg_file.read_text())
    stored = data["never_reclaim"]
    missing = [n for n in names if n not in stored]
    assert not missing, f"lost {len(missing)} concurrent addition(s): {missing}"
    assert not list(cfg_file.parent.glob("*.tmp")), "a temp file was left behind"


def test_protected_add_rejects_blank_name(tmp_path):
    result = run_add(tmp_path, "   ", tmp_path)
    assert result.returncode != 0, "a blank name was accepted into the config"
    cfg_file = tmp_path / ".config" / "ram-keeper" / "config.json"
    assert not cfg_file.exists() or "never_reclaim" not in cfg_file.read_text()


def test_protected_add_rejects_multiline_name(tmp_path):
    result = run_add(tmp_path, "evil\nname", tmp_path)
    assert result.returncode != 0
    cfg_file = tmp_path / ".config" / "ram-keeper" / "config.json"
    if cfg_file.exists():
        assert "evil\\nname" not in cfg_file.read_text()


def test_protected_add_dedupes_identical_names(tmp_path):
    first = run_add(tmp_path, "app-Foo-123.scope", tmp_path)
    second = run_add(tmp_path, "app-Foo-123.scope", tmp_path)
    assert first.returncode == 0 and second.returncode == 0
    stored = json.loads((tmp_path / ".config" / "ram-keeper" / "config.json").read_text())
    assert stored["never_reclaim"] == ["app-Foo-123.scope"], (
        "the same name was stored twice")


def test_distinct_instances_of_one_app_are_not_collapsed(tmp_path):
    """app-Foo-1 and app-Foo-2 are different cgroups and must both be kept."""
    for name in ("app-Parallel-1.scope", "app-Parallel-2.scope"):
        assert run_add(tmp_path, name, tmp_path).returncode == 0
    stored = json.loads((tmp_path / ".config" / "ram-keeper" / "config.json").read_text())
    assert sorted(stored["never_reclaim"]) == ["app-Parallel-1.scope",
                                              "app-Parallel-2.scope"]


def test_protected_add_preserves_other_settings(tmp_path):
    cfg_file = tmp_path / ".config" / "ram-keeper" / "config.json"
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text(json.dumps({"target_free_percent": 25.0}))
    result = run_add(tmp_path, "app-Keep-1.scope", tmp_path)
    assert result.returncode == 0
    data = json.loads(cfg_file.read_text())
    assert data["target_free_percent"] == 25.0
    assert data["never_reclaim"] == ["app-Keep-1.scope"]


# ---------------------------------------------------------------------------
# 3. Numeric coercion must not overflow or lie
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [10**400, -(10**400), 1e308 * 10])
def test_coerce_number_survives_overflow(value):
    rk._CONFIG_WARNINGS.clear()
    out = rk._coerce_number("target_free_percent", value, 18.0)
    assert 5.0 <= out <= 60.0
