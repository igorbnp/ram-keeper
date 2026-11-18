"""Integration tests for ram_keeper end-to-end flows.

These tests exercise the full pipeline: snapshot → relieve → publish,
config → scan → reclaim, and the new CLI commands (--doctor, --history,
--pause, --resume).  They are hermetic: every external path is redirected
to tmp_path.
"""

from __future__ import annotations

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

    def test_instance_numeric_not_alias_but_base_expands(self):
        """@12345 is NOT an alias, yet app-org.chromium.Chromium@12345.service matches chromium."""
        aliases = rk.unit_aliases("app-org.chromium.Chromium@12345.service")
        assert "12345" not in aliases
        assert "chromium" in aliases
        assert rk.names_match("app-org.chromium.Chromium@12345.service", ["chromium"])

    def test_uwsm_compositor_scopes_match_app(self):
        """uwsm scopes (Hyprland/sway/niri/uwsm) expose the launched app alias."""
        assert rk.names_match("app-Hyprland-chromium-ea8ee5ca.scope", ["chromium"])
        assert rk.names_match("app-sway-firefox-9912.scope", ["firefox"])
        assert rk.names_match("app-niri-org.telegram.desktop-abcd1234.scope", ["telegram"])
        assert not rk.names_match("app-wayland\\x2dwm@sway.service", ["wayland-wm@hyprland.desktop"])
        assert not rk.names_match("syncthing.service", ["syncthing-server"])

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


# ---------------------------------------------------------------------------
# Round 2 adversarial regression coverage
# ---------------------------------------------------------------------------

class TestCliPauseStateContract:
    """Contract tests for --pause/--resume/--protected-*/state.json/paused-by-user."""

    @staticmethod
    def _run_cli(args, cfg_path: Path, st_dir: Path):
        import subprocess
        env = os.environ.copy()
        env["RAM_KEEPER_CONFIG"] = str(cfg_path)
        env["RAM_KEEPER_STATE_DIR"] = str(st_dir)
        return subprocess.run(
            [sys.executable, str(PLUGIN / "helper" / "ram_keeper.py"), *args],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def test_pause_cli_variants_and_marker_and_state_sync(self, tmp_path):
        cfg_path = tmp_path / "cfg" / "config.json"
        st_dir = tmp_path / "state"
        st_dir.mkdir()
        (st_dir / "state.json").write_text(json.dumps({
            "state": "ok", "mem_total": 8_000_000_000, "mem_available": 3_000_000_000,
            "free_percent": 37.5, "checked_at": 1700000000.0,
            "forecast": {"urgency": "soon", "will_cross": True, "horizon_seconds": 180.0},
            "session_stats": {"started_at": 1699999000.0, "actions": 3, "bytes_freed": 500_000_000},
            "relief": {"acted": False, "reason": "no pressure", "skipped_anon": ""},
            "paused": False, "paused_until": None, "paused_remaining_s": None,
            "groups": [{"name": "app-org.mozilla.firefox-123.scope", "label": "Firefox",
                        "protected": False, "inviolable": False, "current": 400_000_000,
                        "anon": 300_000_000, "reclaimable": 50_000_000, "swapable": True}],
        }), encoding="utf-8")

        # 1. --pause indefinite
        r = self._run_cli(["--pause", "indefinite"], cfg_path, st_dir)
        assert r.returncode == 0, r.stderr
        assert (st_dir / "paused-by-user").exists()
        st = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        assert st["paused"] is True
        assert st["paused_until"] is None
        assert st["checked_at"] == 1700000000.0
        assert st["forecast"]["urgency"] == "soon"
        assert st["session_stats"]["actions"] == 3

        # 2. --pause 30 + clamp checks + invalid NaN/inf rejection
        r30 = self._run_cli(["--pause", "30"], cfg_path, st_dir)
        assert r30.returncode == 0
        st30 = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        assert st30["paused"] is True and st30["paused_until"] > time.time()

        for bad in ("nan", "inf", "-inf", "-5", "bogus"):
            rbad = self._run_cli(["--pause", bad], cfg_path, st_dir)
            assert rbad.returncode == 2
            assert rbad.stderr.strip() != ""
        # pause.json remains valid finite JSON
        pj = json.loads((st_dir / "pause.json").read_text(encoding="utf-8"))
        assert pj["paused_until"] > time.time()

        # 3. --protected-add & --protected-remove recalculate groups in state.json
        r_add = self._run_cli(["--protected-add", "firefox"], cfg_path, st_dir)
        assert r_add.returncode == 0
        st_add = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        assert st_add["groups"][0]["protected"] is True
        assert st_add["paused"] is True
        assert st_add["forecast"]["urgency"] == "soon"

        r_rem = self._run_cli(["--protected-remove", "firefox"], cfg_path, st_dir)
        assert r_rem.returncode == 0
        st_rem = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        assert st_rem["groups"][0]["protected"] is False

        # 4. --resume removes pause.json + paused-by-user and updates state.json
        r_res = self._run_cli(["--resume"], cfg_path, st_dir)
        assert r_res.returncode == 0
        assert not (st_dir / "pause.json").exists()
        assert not (st_dir / "paused-by-user").exists()
        st_res = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        assert st_res["paused"] is False
        assert st_res["paused_until"] is None

    def test_expired_pause_removes_marker_and_relieve_does_not_restore_stale_pause(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("RAM_KEEPER_CONFIG", str(tmp_path / "cfg.json"))
        # Write an already-expired pause.json + paused-by-user + stale state.json
        (tmp_path / "pause.json").write_text(
            json.dumps({"set_at": time.time() - 120, "paused_until": time.time() - 5}),
            encoding="utf-8",
        )
        (tmp_path / "paused-by-user").write_text("", encoding="utf-8")
        (tmp_path / "state.json").write_text(json.dumps({
            "state": "ok", "mem_total": 8_000_000_000, "mem_available": 4_000_000_000,
            "free_percent": 50.0, "checked_at": time.time() - 10,
            "paused": True, "paused_until": time.time() - 5, "paused_remaining_s": 0,
            "forecast": {"urgency": "none"}, "session_stats": {"actions": 2},
        }), encoding="utf-8")

        s = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        s._check_pause()
        assert s._paused is False
        assert not (tmp_path / "pause.json").exists()
        assert not (tmp_path / "paused-by-user").exists()

        # Running CLI --sweep-cache must not copy stale paused=True back from state.json
        r = self._run_cli(["--sweep-cache"], tmp_path / "cfg.json", tmp_path)
        assert r.returncode == 0
        st = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert st["paused"] is False
        assert st["session_stats"]["actions"] == 2

    def test_sleep_wakes_on_pause_deletion_and_seen_config_change(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        cfg_file = tmp_path / "cfg.json"
        monkeypatch.setenv("RAM_KEEPER_CONFIG", str(cfg_file))
        pause_file = tmp_path / "pause.json"
        pause_file.write_text('{"paused_until": null}', encoding="utf-8")
        s = rk.Steward(config=dict(rk.DEFAULT_CONFIG))

        # 1. Deleting pause.json mid-sleep wakes immediately (0 sleep steps)
        slept = []

        def unlink_on_first_sleep(dt):
            slept.append(dt)
            if pause_file.exists():
                pause_file.unlink()

        with patch("time.sleep", side_effect=unlink_on_first_sleep):
            s._sleep(20.0)
        assert len(slept) == 1

        # 2. Passing stale seen_config (edited during supervise_once) wakes before sleeping
        cfg_file.write_text('{"target_free_percent": 20.0}', encoding="utf-8")
        slept2 = []
        with patch("time.sleep", side_effect=lambda dt: slept2.append(dt)):
            s._sleep(20.0, seen_config=None)
        assert slept2 == []

    def test_history_zero_and_psi_full_thrash_reason_and_post_suspend_forecast(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path))
        (tmp_path / "log.jsonl").write_text(
            json.dumps({"event": "reclaim", "ts": 1000, "freed": 1024, "swapped_out": 0}) + "\n",
            encoding="utf-8",
        )
        h0 = json.loads(rk.history(count=0, as_json=True))
        assert h0["total_events"] == 1
        assert h0["shown"] == []
        assert "no reclaim events" in rk.history(count=0, as_json=False)

        # PSI full thrash reason must mention PSI, not missing zram
        s = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        res = s.relieve({
            "state": "tight", "free_percent": 12.0,
            "mem_total": 8_000_000_000, "mem_available": 960_000_000,
            "psi": {"available": True, "some_avg10": 15.0, "some_avg60": 12.0,
                    "full_avg10": 18.0, "full_avg60": 12.0},
            "zram": {"devices": [{"name": "/dev/zram0", "size": 8_000_000_000,
                                  "used": 100_000_000, "ram_backed": True}]},
            "groups": [{"path": "/nonexistent", "name": "app-x-1.scope", "kind": "scope",
                        "label": "X", "current": 900_000_000, "anon": 800_000_000,
                        "file": 0, "reclaimable_file": 0, "swap": 0, "reclaimable": 0,
                        "protected": False, "inviolable": False, "swapable": True, "pids": 1}],
        })
        assert res["skipped_anon"] == "psi-full-thrash"
        assert "PSI full" in res["reason"]

        # TrendTracker must not bridge a >180s suspend gap or pull pre-window samples
        now = [1000.0]
        tr = rk.TrendTracker(clock=lambda: now[0])
        for i in range(5):
            now[0] += 10.0
            tr.record(4_000_000_000 - i * 100_000_000, {})
        # Suspend for 300s (inside 600s window, >180s gap) + 2 post-wake samples
        now[0] += 300.0
        tr.record(3_500_000_000, {})
        now[0] += 10.0
        tr.record(3_400_000_000, {})
        fc = tr.forecast(1_000_000_000)
        assert fc.will_cross is False
        assert fc.samples == 2

    def test_protected_add_preserves_config_symlink(self, tmp_path, monkeypatch):
        real_cfg = tmp_path / "dotfiles" / "rk-config.json"
        real_cfg.parent.mkdir(parents=True)
        real_cfg.write_text('{"target_free_percent": 22.0}\n', encoding="utf-8")
        link_cfg = tmp_path / "link-config.json"
        try:
            link_cfg.symlink_to(real_cfg)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("symlinks require privilege on Windows")
            raise
        monkeypatch.setenv("RAM_KEEPER_CONFIG", str(link_cfg))
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path / "state"))
        rk.add_never_reclaim("firefox")
        assert link_cfg.is_symlink()
        data = json.loads(real_cfg.read_text(encoding="utf-8"))
        assert "firefox" in data["never_reclaim"]
        assert data["target_free_percent"] == 22.0
