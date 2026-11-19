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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pytest_shim import pytest, install_unittest_bridge  # noqa: E402

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
    def _run_cli(args, cfg_path: Path, st_dir: Path, cg_root=None):
        import subprocess
        env = os.environ.copy()
        env["RAM_KEEPER_CONFIG"] = str(cfg_path)
        env["RAM_KEEPER_STATE_DIR"] = str(st_dir)
        env["RAM_KEEPER_CGROUP_ROOT"] = str(
            cg_root if cg_root is not None else (st_dir / "empty-cgroup")
        )
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

    def test_meminfo_breakdown_keeps_apps_nonzero_when_cached_exceeds_mem_used(self, monkeypatch):
        """Real Linux /proc/meminfo includes reclaimable cache inside MemAvailable."""
        steward = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        # Real shape measured on Omarchy: MemTotal=7.83GB, MemFree=448MB,
        # MemAvailable=3.83GB, Cached=4.13GB (> MemTotal - MemAvailable = 4.00GB).
        monkeypatch.setattr(rk, "meminfo", lambda: {
            "MemTotal": 8_000_000_000,
            "MemFree": 450_000_000,
            "MemAvailable": 3_850_000_000,
            "Buffers": 28_000,
            "Cached": 4_150_000_000,
            "Shmem": 400_000_000,
            "SReclaimable": 250_000_000,
        })
        monkeypatch.setattr(rk, "psi", lambda resource="memory": {
            "available": True, "some_avg10": 0.0, "some_avg60": 0.0,
            "some_avg300": 0.0, "full_avg10": 0.0, "full_avg60": 0.0, "full_avg300": 0.0,
        })
        monkeypatch.setattr(rk, "zram_stats", lambda: {"devices": [], "used": 0, "size": 0})
        monkeypatch.setattr(rk, "user_root", lambda: Path("/nonexistent-cgroup"))

        snap = steward.snapshot()
        assert snap["memory_breakdown_valid"] is True
        expected_cache = 4_150_000_000 + 28_000 + 250_000_000 - 400_000_000
        expected_apps = 8_000_000_000 - 450_000_000 - expected_cache
        assert snap["cached"] == expected_cache
        assert snap["anon_and_kernel"] == expected_apps
        assert snap["anon_and_kernel"] > 3_000_000_000
        assert snap["anon_and_kernel"] + snap["cached"] + 450_000_000 == 8_000_000_000

    def test_protected_cli_syncs_live_cgroup_tree_via_env_root(self, tmp_path):
        """When RAM_KEEPER_CGROUP_ROOT has live user cgroups, --protected-add updates them."""
        if not hasattr(os, "getuid"):
            pytest.skip("POSIX uid required for user-$UID.slice layout")
        cfg_path = tmp_path / "cfg" / "config.json"
        st_dir = tmp_path / "state"
        st_dir.mkdir()
        cg_root = tmp_path / "cgroup"
        app_cg = cg_root / "user.slice" / f"user-{os.getuid()}.slice" / "app-org.mozilla.firefox-999.scope"
        app_cg.mkdir(parents=True)
        (app_cg / "memory.current").write_text(str(500 * 1024 * 1024) + "\n", encoding="utf-8")
        (app_cg / "memory.stat").write_text(
            f"anon {300 * 1024 * 1024}\nfile {200 * 1024 * 1024}\ninactive_file {150 * 1024 * 1024}\n",
            encoding="utf-8",
        )
        (app_cg / "memory.swap.current").write_text("0\n", encoding="utf-8")
        (app_cg / "cgroup.procs").write_text("999\n", encoding="utf-8")

        r_once = self._run_cli(["--once"], cfg_path, st_dir, cg_root=cg_root)
        assert r_once.returncode == 0, r_once.stderr
        (st_dir / "state.json").write_text(r_once.stdout, encoding="utf-8")

        r_add = self._run_cli(["--protected-add", "firefox"], cfg_path, st_dir, cg_root=cg_root)
        assert r_add.returncode == 0, r_add.stderr
        st_add = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        ff = [g for g in st_add["groups"] if "firefox" in g["name"]]
        assert len(ff) == 1 and ff[0]["protected"] is True

    def test_manual_sweep_and_relieve_persist_session_stats_across_daemon_passes(self, tmp_path, monkeypatch):
        """Manual CLI --sweep-cache increments session_stats and survives daemon supervise_once()."""
        cfg_path = tmp_path / "cfg" / "config.json"
        st_dir = tmp_path / "state"
        st_dir.mkdir()
        monkeypatch.setenv("RAM_KEEPER_CONFIG", str(cfg_path))
        monkeypatch.setenv("RAM_KEEPER_STATE_DIR", str(st_dir))

        cg_root = tmp_path / "cgroup"
        uid_part = f"user-{os.getuid()}.slice" if hasattr(os, "getuid") else "user-nonposix.slice"
        app_cg = cg_root / "user.slice" / uid_part / "app-heavy-cache-100.scope"
        app_cg.mkdir(parents=True)
        cur_file = app_cg / "memory.current"
        stat_file = app_cg / "memory.stat"
        reclaim_file = app_cg / "memory.reclaim"
        cur_file.write_text(str(600 * 1024 * 1024) + "\n", encoding="utf-8")
        stat_file.write_text(
            f"anon {200 * 1024 * 1024}\nfile {400 * 1024 * 1024}\ninactive_file {300 * 1024 * 1024}\n",
            encoding="utf-8",
        )
        (app_cg / "memory.swap.current").write_text("0\n", encoding="utf-8")
        (app_cg / "cgroup.procs").write_text("100\n", encoding="utf-8")
        reclaim_file.write_text("", encoding="utf-8")

        monkeypatch.setattr(rk, "CGROOT", cg_root)
        monkeypatch.setattr(rk, "meminfo", lambda: {
            "MemTotal": 8_000_000_000, "MemFree": 1_000_000_000,
            "MemAvailable": 4_000_000_000, "Cached": 3_000_000_000,
        })
        monkeypatch.setattr(rk, "psi", lambda resource="memory": {
            "available": True, "some_avg10": 0.0, "some_avg60": 0.0,
            "some_avg300": 0.0, "full_avg10": 0.0, "full_avg60": 0.0, "full_avg300": 0.0,
        })
        monkeypatch.setattr(rk, "zram_stats", lambda: {"devices": [], "used": 0, "size": 0})

        # 1. Live daemon starts and publishes initial calm snapshot (actions=0).
        daemon = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        daemon._init_session_stats_store()
        snap0 = daemon.supervise_once()
        assert snap0["session_stats"]["actions"] == 0

        # 2. Separate process (simulating Panel 'Drop cache' CLI call) frees 120 MB of cache.
        def fake_reclaim_from(self_st, group, amount, cache_only=False):
            freed = 120 * 1024 * 1024
            return rk.Action(
                label=group.label, requested=amount,
                freed=freed, swapped_out=0, outcome="ok", cgroup_freed=freed,
            )
        monkeypatch.setattr(rk.Steward, "reclaim_from", fake_reclaim_from)
        monkeypatch.setattr(sys, "argv", ["ram_keeper.py", "--sweep-cache"])
        assert rk.main() == 0

        st_after_cli = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        assert st_after_cli["session_stats"]["actions"] == 1
        assert st_after_cli["session_stats"]["bytes_freed"] == 120 * 1024 * 1024

        # 3. Daemon wakes on its next tick and MUST keep the manual session_stats!
        snap1 = daemon.supervise_once()
        assert snap1["session_stats"]["actions"] == 1
        assert snap1["session_stats"]["bytes_freed"] == 120 * 1024 * 1024

        # 4. Corrupt / hostile session_stats.json or 400-digit ints are safely rejected
        #    WITHOUT losing accumulated stats on subsequent daemon or CLI reclaims!
        huge_int = int("1" + "0" * 400)
        for bad in (None, [], "x", {"started_at": True}, {"started_at": -1},
                    {"started_at": huge_int, "actions": 1},
                    {"started_at": 100.0, "actions": huge_int},
                    {"started_at": 100.0, "bytes_freed": huge_int},
                    {"started_at": 100.0, "last_relief_bytes": huge_int},
                    {"started_at": 100.0, "last_relief_ts": huge_int},
                    {"started_at": 100.0, "actions": -5}, {"started_at": 100.0, "bytes_freed": float("nan")},
                    {"started_at": 100.0, "last_relief_ts": float("inf")}):
            assert rk.SessionStats.from_dict(bad) is None

        # Corrupt session_stats.json followed by daemon reclaim preserves in-memory history (1 -> 2).
        (st_dir / "session_stats.json").write_text("{corrupt", encoding="utf-8")
        daemon._record_session_stats(50 * 1024 * 1024, label="App")
        assert daemon.session_stats.actions == 2
        assert daemon.session_stats.bytes_freed == 170 * 1024 * 1024

        # Corrupt session_stats.json followed by manual CLI reclaim recovers from state.json (2 -> 3).
        daemon.supervise_once()
        (st_dir / "session_stats.json").write_text("{corrupt", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["ram_keeper.py", "--sweep-cache"])
        assert rk.main() == 0
        st_recovered = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        assert st_recovered["session_stats"]["actions"] == 3
        assert st_recovered["session_stats"]["bytes_freed"] == 290 * 1024 * 1024

        # Sync daemon to actions=3, then test 400-digit int in session_stats.json.
        daemon.supervise_once()
        assert daemon.session_stats.actions == 3
        (st_dir / "session_stats.json").write_text(
            '{"started_at": ' + ("1" + "0" * 400) + ', "actions": 1}', encoding="utf-8"
        )
        r_pause = self._run_cli(["--pause", "10"], cfg_path, st_dir, cg_root=cg_root)
        assert r_pause.returncode == 0, r_pause.stderr

        # Fresh session_stats.json wins over stale state.json during _sync_state_after_ctl().
        daemon._record_session_stats(10 * 1024 * 1024, label="App")  # store now has actions=4
        r_resume = self._run_cli(["--resume"], cfg_path, st_dir, cg_root=cg_root)
        assert r_resume.returncode == 0, r_resume.stderr
        st_synced = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        assert st_synced["session_stats"]["actions"] == 4

    def test_protected_cli_refuses_corrupt_config_without_wiping_it(self, tmp_path):
        """B2: syntax error in config.json must never be silently overwritten."""
        cfg_path = tmp_path / "config.json"
        st_dir = tmp_path / "state"
        st_dir.mkdir()
        broken = '{\n  "target_free_percent": 25.0,\n  "protected": ["my-db"],\n}\n'
        cfg_path.write_text(broken, encoding="utf-8")

        r_add = self._run_cli(["--protected-add", "firefox"], cfg_path, st_dir)
        assert r_add.returncode == 2
        assert "invalid" in r_add.stderr.lower()
        assert cfg_path.read_text(encoding="utf-8") == broken

        r_rem = self._run_cli(["--protected-remove", "ollama"], cfg_path, st_dir)
        assert r_rem.returncode == 2
        assert cfg_path.read_text(encoding="utf-8") == broken

        # Broken symlink must be refused without materializing target directories/files
        broken_dir = tmp_path / "broken-target-dir"
        broken_target = broken_dir / "nonexistent.json"
        link_cfg = tmp_path / "broken-link.json"
        try:
            link_cfg.symlink_to(broken_target)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                return
            raise
        for flag in (["--protected-add", "firefox"], ["--protected-remove", "ollama"]):
            r_lnk = self._run_cli(flag, link_cfg, st_dir)
            assert r_lnk.returncode == 2
            assert link_cfg.is_symlink()
            assert not broken_target.exists()
            assert not broken_dir.exists()

    def test_sweep_cache_and_relieve_do_not_forge_daemon_heartbeat(self, tmp_path):
        """B3: manual CLI actions must preserve existing checked_at (or 0 when absent)."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}\n", encoding="utf-8")
        st_dir = tmp_path / "state"
        st_dir.mkdir()

        # Case 1: daemon dead for 2 hours -> checked_at stays old, doctor flags stale
        old_ts = time.time() - 7200
        (st_dir / "state.json").write_text(json.dumps({
            "state": "ok", "mem_total": 8_000_000_000, "mem_available": 4_000_000_000,
            "free_percent": 50.0, "checked_at": old_ts, "groups": [],
        }), encoding="utf-8")
        r = self._run_cli(["--sweep-cache"], cfg_path, st_dir)
        assert r.returncode == 0, r.stderr
        st = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
        assert abs(st["checked_at"] - old_ts) < 1.0

        r_doc = self._run_cli(["--doctor", "--json"], cfg_path, st_dir)
        doc = json.loads(r_doc.stdout)
        sf = next(c for c in doc["checks"] if c["name"] == "state_freshness")
        assert sf["ok"] is False

        # Case 2: no prior state.json -> checked_at is 0 (no spoofed heartbeat)
        (st_dir / "state.json").unlink()
        r2 = self._run_cli(["--sweep-cache"], cfg_path, st_dir)
        assert r2.returncode == 0, r2.stderr
        if (st_dir / "state.json").exists():
            st2 = json.loads((st_dir / "state.json").read_text(encoding="utf-8"))
            assert st2["checked_at"] == 0

    def test_resume_handles_oserror_without_traceback(self, tmp_path):
        """D4: --resume reports OSError cleanly with rc=1, matching --pause."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("file", encoding="utf-8")
        r = self._run_cli(["--resume"], tmp_path / "cfg.json", blocker / "sub")
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "ram-keeper:" in r.stderr

    def test_history_survives_null_and_malformed_reclaim_entries(self, tmp_path):
        """D5: --history must not crash on null freed/swapped_out/actions."""
        st_dir = tmp_path / "state"
        st_dir.mkdir()
        (st_dir / "log.jsonl").write_text("\n".join([
            json.dumps({"event": "reclaim", "freed": None, "swapped_out": None,
                        "actions": None, "ts": None, "reason": None}),
            json.dumps({"event": "reclaim", "freed": 10 * 1024 * 1024, "swapped_out": "bad",
                        "actions": ["bad", {"label": "app", "freed": None}], "ts": 1700000000}),
        ]) + "\n", encoding="utf-8")
        r = self._run_cli(["--history", "5"], tmp_path / "cfg.json", st_dir)
        assert r.returncode == 0, r.stderr
        assert "Traceback" not in r.stderr
        assert "10.0 MB" in r.stdout

    def test_doctor_scales_freshness_with_interval_idle_and_allows_small_skew(self, tmp_path):
        """D6: interval_idle=300s with 150s age and -0.2s NTP skew are both healthy."""
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"interval_idle": 300.0}), encoding="utf-8")
        st_dir = tmp_path / "state"
        st_dir.mkdir()
        sf_path = st_dir / "state.json"

        sf_path.write_text(json.dumps({
            "state": "ok", "mem_total": 8_000_000_000, "checked_at": time.time() - 150.0,
        }), encoding="utf-8")
        r1 = self._run_cli(["--doctor", "--json"], cfg_path, st_dir)
        chk1 = next(c for c in json.loads(r1.stdout)["checks"] if c["name"] == "state_freshness")
        assert chk1["ok"] is True, chk1

        sf_path.write_text(json.dumps({
            "state": "ok", "mem_total": 8_000_000_000, "checked_at": time.time() + 0.2,
        }), encoding="utf-8")
        r2 = self._run_cli(["--doctor", "--json"], cfg_path, st_dir)
        chk2 = next(c for c in json.loads(r2.stdout)["checks"] if c["name"] == "state_freshness")
        assert chk2["ok"] is True, chk2

        # Corrupt JSON or non-object root must NOT pass as fresh even with recent mtime
        for bad_state in ("{broken", "[]", "null"):
            sf_path.write_text(bad_state, encoding="utf-8")
            r_bad = self._run_cli(["--doctor", "--json"], cfg_path, st_dir)
            chk_bad = next(c for c in json.loads(r_bad.stdout)["checks"] if c["name"] == "state_freshness")
            assert chk_bad["ok"] is False, (bad_state, chk_bad)

    def test_swap_history_pruned_after_cooldown_expires(self):
        """D8: expired _swap_history entries are pruned alongside _anon_ready_at."""
        now_t = [1000.0]
        steward = rk.Steward(config=dict(rk.DEFAULT_CONFIG), _clock=lambda: now_t[0])
        steward._swap_history["app-old-1.scope"] = (2, 1120.0)
        steward._swap_history["app-active-2.scope"] = (1, 6000.0)
        steward._anon_ready_at["app-old-1.scope"] = 1120.0
        now_t[0] = 5000.0
        steward.relieve(snapshot={
            "state": "tight", "free_percent": 15.0, "mem_total": 8 * 1024**3,
            "mem_available": int(1.2 * 1024**3), "groups": [],
            "zram": {"devices": []}, "psi": {"available": False},
        })
        assert "app-old-1.scope" not in steward._swap_history
        assert "app-old-1.scope" not in steward._anon_ready_at
        assert "app-active-2.scope" in steward._swap_history


install_unittest_bridge(globals())
