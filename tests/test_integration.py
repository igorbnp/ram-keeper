"""Integration tests for ram_keeper end-to-end flows.

These tests exercise the full pipeline: snapshot → relieve → publish,
config → scan → reclaim, and the new CLI commands (--doctor, --history,
--pause, --resume).  They are hermetic: every external path is redirected
to tmp_path.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import ram_keeper as rk  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_steward(tmp_path, monkeypatch):
    """A Steward wired to tmp_path for config and state."""
    cfg_path = tmp_path / "config.json"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("RAM_KEEPER_CONFIG", str(cfg_path))
    monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(state))
    s = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
    s.log_path = state / "log.jsonl"
    s.state_path = state / "state.json"
    return s


@pytest.fixture
def mock_meminfo():
    return {"MemTotal": 8_000_000_000, "MemAvailable": 1_200_000_000,
            "Cached": 500_000_000}


@pytest.fixture
def mock_groups(tmp_path):
    """Create a minimal in-memory cgroup tree with two groups."""
    groups = [
        rk.Cgroup(
            path=tmp_path / "g1",
            name="app-bigapp-123.scope",
            kind="scope",
            label="bigapp",
            current=600_000_000,
            anon=300_000_000,
            file=300_000_000,
            reclaimable_file=200_000_000,
            swap=0,
            reclaimable=200_000_000,
            protected=False,
            swapable=True,
            pids=5,
        ),
        rk.Cgroup(
            path=tmp_path / "g2",
            name="app-smallapp-456.scope",
            kind="scope",
            label="smallapp",
            current=200_000_000,
            anon=100_000_000,
            file=100_000_000,
            reclaimable_file=80_000_000,
            swap=0,
            reclaimable=80_000_000,
            protected=False,
            swapable=True,
            pids=2,
        ),
    ]
    return groups


# ---------------------------------------------------------------------------
# Snapshot → Relieve pipeline
# ---------------------------------------------------------------------------

class TestSnapshotRelievePipeline:
    """Full snapshot-then-relieve with mocked kernel interfaces."""

    def test_snapshot_carries_session_stats(self, isolated_steward):
        """snapshot() must include session_stats, paused, interval_idle."""
        with patch("ram_keeper.meminfo", return_value={
                "MemTotal": 8_000_000_000, "MemAvailable": 6_000_000_000,
                "Cached": 2_000_000_000}), \
             patch("ram_keeper.psi", return_value={
                "available": True, "some_avg10": 0, "some_avg60": 0,
                "some_avg300": 0, "full_avg10": 0, "full_avg60": 0,
                "full_avg300": 0}), \
             patch("ram_keeper.zram_stats", return_value={
                "devices": [], "used": 0, "size": 0,
                "disk_used": 0, "ram_used": 0}), \
             patch("ram_keeper.user_root", return_value=Path("/nonexistent")), \
             patch("ram_keeper.scan", return_value=[]):
            snap = isolated_steward.snapshot()

        assert "session_stats" in snap
        assert "paused" in snap
        assert snap["paused"] is False
        assert "interval_idle" in snap

    def test_relieve_records_session_stats(self, isolated_steward, mock_groups):
        """After a real relieve, session_stats should be updated."""
        snap = {
            "state": "critical",
            "free_percent": 5.0,
            "mem_total": 8_000_000_000,
            "mem_available": 400_000_000,
            "cached": 0,
            "forecast": {"urgency": "none"},
            "psi": {"available": False},
            "zram": {"devices": [], "used": 0, "size": 0,
                     "disk_used": 0, "ram_used": 0},
            "groups": [
                {**{k: v for k, v in g.__dict__.items() if k != "path"} |
                 {"path": str(g.path)}}
                for g in mock_groups
            ],
        }
        with patch.object(isolated_steward, "reclaim_from") as mock_reclaim:
            mock_reclaim.return_value = rk.Action(
                label="bigapp", requested=100_000_000,
                freed=80_000_000, swapped_out=0,
                outcome="ok", cgroup_freed=80_000_000,
            )
            isolated_steward.relieve(snap)

        assert isolated_steward.session_stats.actions >= 1
        assert isolated_steward.session_stats.bytes_freed >= 80_000_000


# ---------------------------------------------------------------------------
# Config → scan → reclaim flow
# ---------------------------------------------------------------------------

class TestConfigScanReclaim:
    """Config changes propagate correctly through scan and reclaim."""

    def test_protected_groups_are_skipped(self, isolated_steward, mock_groups):
        """A group in the protected list must not be reclaimed."""
        # Mark first group as protected
        mock_groups[0].protected = True

        snap = {
            "state": "critical",
            "free_percent": 5.0,
            "mem_total": 8_000_000_000,
            "mem_available": 400_000_000,
            "cached": 0,
            "forecast": {"urgency": "none"},
            "psi": {"available": False},
            "zram": {"devices": [], "used": 0, "size": 0,
                     "disk_used": 0, "ram_used": 0},
            "groups": [
                {**{k: v for k, v in g.__dict__.items() if k != "path"} |
                 {"path": str(g.path)}}
                for g in mock_groups
            ],
        }
        with patch.object(isolated_steward, "reclaim_from") as mock_reclaim:
            mock_reclaim.return_value = rk.Action(
                label="smallapp", requested=50_000_000,
                freed=40_000_000, swapped_out=0,
                outcome="ok", cgroup_freed=40_000_000,
            )
            result = isolated_steward.relieve(snap)

        # Only the unprotected group should have been reclaimed
        reclaimed = [c[0][0].name for c in mock_reclaim.call_args_list]
        assert "app-bigapp-123.scope" not in reclaimed

    def test_xdg_desktop_portal_variants_are_protected(self):
        """All xdg-desktop-portal-* variants must be in DEFAULT_CONFIG."""
        defaults = rk.DEFAULT_CONFIG["protected"]
        for variant in ("xdg-desktop-portal", "xdg-desktop-portal-hyprland",
                        "xdg-desktop-portal-gtk", "xdg-desktop-portal-wlr",
                        "xdg-desktop-portal-kde"):
            assert variant in defaults, f"{variant} missing from default protected"


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------

class TestPauseResume:
    """Tests for --pause / --resume CLI and daemon-side pause checking."""

    def test_pause_indefinite(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        result = rk.pause_daemon(minutes=None)
        assert result["paused"] is True
        assert result["paused_until"] is None
        data = json.loads((tmp_path / "pause.json").read_text(encoding="utf-8"))
        assert data["paused_until"] is None

    def test_pause_timed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        result = rk.pause_daemon(minutes=5)
        assert result["paused"] is True
        assert result["paused_until"] is not None
        data = json.loads((tmp_path / "pause.json").read_text(encoding="utf-8"))
        assert data["paused_until"] > time.time()

    def test_resume_removes_pause(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        rk.pause_daemon(minutes=None)
        assert (tmp_path / "pause.json").exists()
        result = rk.resume_daemon()
        assert result["paused"] is False
        assert not (tmp_path / "pause.json").exists()

    def test_pause_zero_is_resume(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        rk.pause_daemon(minutes=10)
        rk.pause_daemon(minutes=0)
        assert not (tmp_path / "pause.json").exists()

    def test_check_pause_reads_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("RAM_KEEPER_CONFIG", str(tmp_path / "cfg.json"))
        s = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        # No pause file → not paused
        s._check_pause()
        assert s._paused is False
        # Write a pause
        rk.pause_daemon(minutes=60)
        s._check_pause()
        assert s._paused is True

    def test_supervise_once_skips_relieve_when_paused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("RAM_KEEPER_CONFIG", str(tmp_path / "cfg.json"))
        rk.pause_daemon(minutes=60)
        s = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        s.state_path = tmp_path / "state.json"
        s.log_path = tmp_path / "log.jsonl"

        with patch("ram_keeper.meminfo", return_value={
                "MemTotal": 8_000_000_000, "MemAvailable": 400_000_000,
                "Cached": 100_000_000}), \
             patch("ram_keeper.psi", return_value={
                "available": True, "some_avg10": 0, "some_avg60": 0,
                "some_avg300": 0, "full_avg10": 0, "full_avg60": 0,
                "full_avg300": 0}), \
             patch("ram_keeper.zram_stats", return_value={
                "devices": [], "used": 0, "size": 0,
                "disk_used": 0, "ram_used": 0}), \
             patch("ram_keeper.user_root", return_value=Path("/nonexistent")), \
             patch("ram_keeper.scan", return_value=[]):
            snap = s.supervise_once()

        # Even under pressure, paused daemon must NOT reclaim
        assert snap["relief"]["acted"] is False
        assert snap["relief"]["reason"] == "paused"
        # But snapshot and TrendTracker still run
        assert "state" in snap


# ---------------------------------------------------------------------------
# Doctor / History
# ---------------------------------------------------------------------------

class TestDoctorHistory:
    """Tests for --doctor and --history CLI commands."""

    def test_doctor_returns_json(self):
        """doctor(as_json=True) must return valid JSON."""
        code, text = rk.doctor(as_json=True)
        data = json.loads(text)
        assert "ok" in data
        assert "checks" in data

    def test_doctor_returns_text(self):
        """doctor(as_json=False) must return readable text."""
        code, text = rk.doctor(as_json=False)
        assert "ram-keeper doctor:" in text

    def test_history_empty(self, tmp_path, monkeypatch):
        """history with no log returns a 'no events' message."""
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        text = rk.history(count=10)
        assert "no reclaim events" in text

    def test_history_with_events(self, tmp_path, monkeypatch):
        """history should parse and summarize reclaim events."""
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        log = tmp_path / "log.jsonl"
        events = [
            {"event": "reclaim", "ts": time.time() - 60,
             "freed": 100_000_000, "swapped_out": 0,
             "actions": [{"label": "bigapp", "freed": 100_000_000}]},
            {"event": "reclaim", "ts": time.time() - 30,
             "freed": 50_000_000, "swapped_out": 20_000_000,
             "actions": [{"label": "smallapp", "freed": 50_000_000}]},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                       encoding="utf-8")
        text = rk.history(count=10)
        assert "last 2" in text
        assert "bigapp" in text

    def test_history_json(self, tmp_path, monkeypatch):
        """history(as_json=True) must return valid JSON."""
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        log = tmp_path / "log.jsonl"
        log.write_text(json.dumps({"event": "reclaim", "ts": 1,
                                   "freed": 10, "swapped_out": 0,
                                   "actions": []}) + "\n",
                       encoding="utf-8")
        text = rk.history(count=5, as_json=True)
        data = json.loads(text)
        assert "total_events" in data


# ---------------------------------------------------------------------------
# Unit aliases — new behaviour
# ---------------------------------------------------------------------------

class TestUnitAliasesIntegration:

    def test_telegram_desktop_produces_telegram(self):
        """app-org.telegram.desktop-1234.scope → 'telegram' alias."""
        aliases = rk.unit_aliases("app-org.telegram.desktop-1234.scope")
        assert "telegram" in aliases

    def test_chromium_produces_chromium(self):
        """app-org.chromium.Chromium-991.scope → 'chromium' alias."""
        aliases = rk.unit_aliases("app-org.chromium.Chromium-991.scope")
        assert "chromium" in aliases

    def test_instance_numeric_not_alias(self):
        """@12345 (numeric PID) should NOT appear as alias."""
        aliases = rk.unit_aliases("app-org.chromium.Chromium@12345.service")
        assert "12345" not in aliases

    def test_instance_named_is_alias(self):
        """@hyprland should appear as alias."""
        aliases = rk.unit_aliases("wayland-wm@hyprland.service")
        assert "hyprland" in aliases

    def test_systemd_escapes_utf8(self):
        """Multi-byte UTF-8 escapes must decode correctly."""
        # \xc3\xa9 = é in UTF-8
        result = rk._decode_systemd_escapes("caf\\xc3\\xa9")
        assert result == "café"


# ---------------------------------------------------------------------------
# Session stats
# ---------------------------------------------------------------------------

class TestSessionStatsIntegration:

    def test_session_stats_record(self):
        ss = rk.SessionStats()
        ss.record(1_000_000, label="test")
        assert ss.actions == 1
        assert ss.bytes_freed == 1_000_000
        assert ss.last_relief_label == "test"
        d = ss.as_dict()
        assert d["actions"] == 1
