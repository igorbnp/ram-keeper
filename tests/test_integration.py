"""Integration tests: process_guardian + ram_keeper working together."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Resolve the plugin directory from this file's own location
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import ram_keeper as rk  # noqa: E402
import process_guardian as pg  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def integrated_config():
    """A config that both modules can use."""
    rk_config = dict(rk.DEFAULT_CONFIG)
    rk_config.update({
        "protected": ["wayland-wm@hyprland.desktop", "quickshell", "ram-keeper"],
        "never_reclaim": ["localsearch-3.service"],
    })
    return {
        "guardian": pg.GuardianConfig(
            protected_names={"firefox", "my-critical-app"},
            protected_descriptions={"Web Browser", "Critical Service"},
            scan_interval=10.0,
        ),
        "ram_keeper": rk_config,
    }


@pytest.fixture
def mock_system_state():
    """A mock system state for integration testing."""
    return {
        "pids": {1, 2, 3, 100, 200, 300},
        "processes": {
            100: {"name": "firefox", "cgroup": "/user.slice/user-1000.slice/app-firefox.scope"},
            200: {"name": "hyprland", "cgroup": "/user.slice/user-1000.slice/session-2.scope"},
            300: {"name": "unknown-app", "cgroup": "/user.slice/user-1000.slice/app-unknown.scope"},
        },
        "meminfo": {"MemTotal": 8026562560, "MemAvailable": 3073976832, "Cached": 1500000000},
        "psi": {"some_avg10": 0, "some_avg60": 0, "some_avg300": 0, "full_avg10": 0, "full_avg60": 0, "full_avg300": 0},
        "zram": {"devices": [], "used": 0, "size": 0},
    }


# ---------------------------------------------------------------------------
# Integration: process_guardian feeds ram_keeper
# ---------------------------------------------------------------------------

class TestGuardianToRamKeeper:
    """Tests for process_guardian providing protection info to ram_keeper."""

    def test_protected_cgroups_flow_to_ram_keeper(self, integrated_config):
        """Protected processes from guardian should be respected by ram_keeper."""
        # Simulate guardian finding protected processes
        processes = [
            pg.ProcessInfo(pid=100, name="firefox", cgroup="/user.slice/user-1000.slice/app-firefox.scope", is_protected=True),
            pg.ProcessInfo(pid=200, name="hyprland", cgroup="/user.slice/user-1000.slice/session-2.scope", is_protected=True),
            pg.ProcessInfo(pid=300, name="unknown-app", cgroup="/user.slice/user-1000.slice/app-unknown.scope", is_protected=False),
        ]

        # Get protected cgroup names from guardian
        protected_cgroups = pg.get_protected_cgroup_names(processes)

        # These should be respected by ram_keeper's scan
        assert "app-firefox.scope" in protected_cgroups
        assert "session-2.scope" in protected_cgroups
        assert "app-unknown.scope" not in protected_cgroups

    def test_guardian_protection_survives_config_override(self, integrated_config):
        """Even if user clears protected list, inviolable processes stay protected."""
        # User config with empty protected list
        user_config = {
            "protected": [],
            "never_reclaim": [],
        }

        # But guardian still detects compositor
        processes = [
            pg.ProcessInfo(pid=200, name="hyprland", cgroup="/user.slice/user-1000.slice/session-2.scope", is_protected=True),
        ]

        protected_cgroups = pg.get_protected_cgroup_names(processes)

        # ram_keeper's is_inviolable should still protect it
        for cgroup in protected_cgroups:
            assert rk.is_inviolable(cgroup) or cgroup in integrated_config["ram_keeper"]["protected"]

    def test_new_protected_process_added_to_config(self, integrated_config, tmp_path):
        """A newly detected protected process can be added to ram_keeper config."""
        # Simulate guardian detecting a new critical process
        new_process = pg.ProcessInfo(
            pid=500,
            name="my-critical-app",
            cgroup="/user.slice/user-1000.slice/app-my-critical-app.scope",
            is_protected=True,
        )

        # Get the cgroup name
        protected_cgroups = pg.get_protected_cgroup_names([new_process])
        assert "app-my-critical-app.scope" in protected_cgroups

        # This could be written to config
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"protected": list(protected_cgroups)}))

        # Verify it loads correctly — patch config_path AND Path.home to isolate
        with patch("ram_keeper.config_path", return_value=config_file), \
             patch("ram_keeper.Path.home", return_value=tmp_path):
            config = rk.load_config()
        assert "app-my-critical-app.scope" in config["protected"]


# ---------------------------------------------------------------------------
# Integration: ram_keeper scan + process_guardian detection
# ---------------------------------------------------------------------------

class TestScanWithGuardian:
    """Tests for ram_keeper scan combined with process_guardian detection."""

    def test_scan_respects_guardian_protection(self, integrated_config, mock_system_state):
        """ram_keeper scan should respect protection from guardian."""
        # Create a mock cgroup tree
        tmp_path = Path("/tmp/test_cgroup")
        tmp_path.mkdir(exist_ok=True)

        # Create cgroups
        for pid, info in mock_system_state["processes"].items():
            cgroup_name = Path(info["cgroup"]).name
            cgroup_dir = tmp_path / cgroup_name
            cgroup_dir.mkdir(exist_ok=True)
            (cgroup_dir / "memory.current").write_text("500000000")
            (cgroup_dir / "memory.stat").write_text("anon 200000000\nfile 300000000\ninactive_file 250000000\n")
            (cgroup_dir / "pids.current").write_text("5")
            (cgroup_dir / "memory.swap.current").write_text("0")
            (cgroup_dir / "memory.swap.max").write_text("max")

        # Scan with guardian's protected set
        protected = {"app-firefox.scope", "session-2.scope"}
        result = rk.scan(tmp_path, protected=protected, min_bytes=1024 * 1024)

        # Check protection flags
        for group in result:
            if group.name in protected:
                assert group.protected is True
            else:
                assert group.protected is False

        # Cleanup
        import shutil
        shutil.rmtree(tmp_path, ignore_errors=True)

    def test_snapshot_includes_guardian_protected_groups(self, integrated_config, mock_system_state):
        """Snapshot should include groups that guardian marked as protected."""
        s = rk.Steward(config=integrated_config["ram_keeper"])

        # Mock the scan to return guardian-protected groups
        mock_groups = [
            rk.Cgroup(
                path=Path("/nonexistent/firefox"),
                name="app-firefox.scope",
                kind="scope",
                label="firefox",
                current=500000000,
                anon=200000000,
                file=300000000,
                reclaimable_file=250000000,
                swap=0,
                reclaimable=250000000,
                protected=True,  # Protected by guardian
                swapable=True,
                pids=5,
            ),
            rk.Cgroup(
                path=Path("/nonexistent/unknown"),
                name="app-unknown.scope",
                kind="scope",
                label="unknown",
                current=300000000,
                anon=100000000,
                file=200000000,
                reclaimable_file=150000000,
                swap=0,
                reclaimable=150000000,
                protected=False,
                swapable=True,
                pids=3,
            ),
        ]

        with patch("ram_keeper.meminfo", return_value=mock_system_state["meminfo"]), \
             patch("ram_keeper.psi", return_value=mock_system_state["psi"]), \
             patch("ram_keeper.zram_stats", return_value=mock_system_state["zram"]), \
             patch("ram_keeper.user_root", return_value=Path("/nonexistent")), \
             patch("ram_keeper.scan", return_value=mock_groups):
            result = s.snapshot()

        # Check that protected groups are in the snapshot
        group_names = [g["name"] for g in result["groups"]]
        assert "app-firefox.scope" in group_names
        assert "app-unknown.scope" in group_names

        # Check that protected flag is preserved
        firefox = next(g for g in result["groups"] if g["name"] == "app-firefox.scope")
        assert firefox["protected"] is True

    def test_relieve_skips_guardian_protected(self, integrated_config, mock_system_state):
        """relieve() should skip groups protected by guardian."""
        s = rk.Steward(config=integrated_config["ram_keeper"])

        # Create a snapshot with a protected group
        snapshot = {
            "state": "critical",
            "free_percent": 5.0,
            "mem_total": 8026562560,
            "mem_available": 400000000,
            "cached": 0,
            "forecast": {"urgency": "none"},
            "groups": [
                {
                    "name": "app-firefox.scope",
                    "label": "firefox",
                    "kind": "scope",
                    "current": 500000000,
                    "anon": 200000000,
                    "file": 300000000,
                    "reclaimable_file": 250000000,
                    "swap": 0,
                    "reclaimable": 250000000,
                    "protected": True,  # Protected by guardian
                    "swapable": True,
                    "pids": 5,
                    "path": "/nonexistent",
                },
                {
                    "name": "app-unknown.scope",
                    "label": "unknown",
                    "kind": "scope",
                    "current": 300000000,
                    "anon": 100000000,
                    "file": 200000000,
                    "reclaimable_file": 150000000,
                    "swap": 0,
                    "reclaimable": 150000000,
                    "protected": False,
                    "swapable": True,
                    "pids": 3,
                    "path": "/nonexistent",
                },
            ],
        }

        with patch.object(s, "reclaim_from") as mock_reclaim:
            mock_reclaim.return_value = rk.Action(
                label="test", requested=1000, freed=1000, swapped_out=0, outcome="ok"
            )
            result = s.relieve(snapshot)

        # Should only reclaim from unprotected group
        reclaimed_names = [call[0][0].name for call in mock_reclaim.call_args_list]
        assert "app-unknown.scope" in reclaimed_names
        assert "app-firefox.scope" not in reclaimed_names


# ---------------------------------------------------------------------------
# Integration: Full pipeline
# ---------------------------------------------------------------------------

class TestFullPipeline:
    """Tests for the full integrated pipeline."""

    def test_detect_protect_reclaim_pipeline(self, integrated_config, mock_system_state):
        """Full pipeline: detect new processes, protect them, reclaim from others."""
        guardian_config = integrated_config["guardian"]

        # Step 1: Detect new processes
        previous_pids = {1, 2, 3}
        current_pids = mock_system_state["pids"]

        def mock_read(pid):
            info = mock_system_state["processes"].get(pid)
            if info:
                return pg.ProcessInfo(pid=pid, name=info["name"], cgroup=info["cgroup"])
            return None

        with patch("process_guardian.read_process_info", side_effect=mock_read):
            with patch("process_guardian.get_process_description", side_effect=lambda p: p.name):
                new_processes = pg.evaluate_new_processes(previous_pids, guardian_config, current_pids)

        # Step 2: Get protected cgroups
        protected_cgroups = pg.get_protected_cgroup_names(new_processes)

        # Step 3: Verify protection is consistent
        # Protected cgroups should be either inviolable or in the protected list
        # OR explicitly marked as protected by the guardian
        for proc in new_processes:
            if proc.is_protected:
                # If marked protected by guardian, it should have a reason
                assert proc.protection_reason != ""
            else:
                # If not protected, it should not be in protected cgroups
                if proc.cgroup:
                    cgroup_name = Path(proc.cgroup).name
                    assert cgroup_name not in protected_cgroups

    def test_guardian_updates_ram_keeper_config(self, integrated_config, tmp_path):
        """Guardian can update ram_keeper's config with new protected processes."""
        # Simulate guardian detecting new protected processes
        new_protected = [
            pg.ProcessInfo(pid=500, name="new-critical", cgroup="/user.slice/user-1000.slice/app-new-critical.scope", is_protected=True),
        ]

        protected_cgroups = pg.get_protected_cgroup_names(new_protected)

        # Write to config
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "protected": list(protected_cgroups),
        }))

        # Load and verify — patch config_path AND Path.home to isolate
        with patch("ram_keeper.config_path", return_value=config_file), \
             patch("ram_keeper.Path.home", return_value=tmp_path):
            config = rk.load_config()

        assert "app-new-critical.scope" in config["protected"]

    def test_end_to_end_with_mocks(self, integrated_config, mock_system_state):
        """End-to-end test with all external calls mocked."""
        guardian_config = integrated_config["guardian"]

        # Mock all external calls
        with patch("process_guardian.read_process_info") as mock_read:
            with patch("process_guardian.get_process_description", side_effect=lambda p: p.name):
                with patch("ram_keeper.meminfo", return_value=mock_system_state["meminfo"]):
                    with patch("ram_keeper.psi", return_value=mock_system_state["psi"]):
                        with patch("ram_keeper.zram_stats", return_value=mock_system_state["zram"]):
                            with patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
                                # Step 1: Guardian detects new processes
                                def read_side_effect(pid):
                                    info = mock_system_state["processes"].get(pid)
                                    if info:
                                        return pg.ProcessInfo(pid=pid, name=info["name"], cgroup=info["cgroup"])
                                    return None

                                mock_read.side_effect = read_side_effect

                                new_processes = pg.evaluate_new_processes(
                                    {1, 2, 3},
                                    guardian_config,
                                    mock_system_state["pids"],
                                )

                                # Step 2: Get protected cgroups
                                protected_cgroups = pg.get_protected_cgroup_names(new_processes)

                                # Step 3: Create ram_keeper steward with merged config
                                merged_config = dict(integrated_config["ram_keeper"])
                                merged_config["protected"] = list(set(
                                    merged_config["protected"] + list(protected_cgroups)
                                ))

                                s = rk.Steward(config=merged_config)

                                # Step 4: Take snapshot
                                snapshot = s.snapshot()

                                # Verify
                                assert snapshot["state"] in ("ok", "tight", "critical", "stalling", "unknown")
                                assert isinstance(snapshot["groups"], list)


# ---------------------------------------------------------------------------
# Integration: Error handling across modules
# ---------------------------------------------------------------------------

class TestErrorHandlingIntegration:
    """Tests for error handling when modules interact."""

    def test_guardian_handles_ram_keeper_errors(self, integrated_config):
        """Guardian should handle errors from ram_keeper gracefully."""
        # If ram_keeper's is_inviolable raises, guardian should still work
        with patch("ram_keeper.is_inviolable", side_effect=Exception("unexpected")):
            processes = [
                pg.ProcessInfo(pid=1, name="test", cgroup="/user.slice/test.scope", is_protected=True),
            ]
            # Should not raise
            result = pg.get_protected_cgroup_names(processes)
            assert isinstance(result, set)

    def test_ram_keeper_handles_guardian_errors(self, integrated_config):
        """ram_keeper should handle errors from guardian gracefully."""
        # If process detection fails, ram_keeper should still work
        with patch("process_guardian.detect_new_processes", side_effect=Exception("unexpected")):
            s = rk.Steward(config=integrated_config["ram_keeper"])
            with patch("ram_keeper.meminfo", return_value=integrated_config["ram_keeper"]):
                with patch("ram_keeper.psi", return_value={"some_avg10": 0, "some_avg60": 0, "some_avg300": 0, "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}):
                    with patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}):
                        with patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
                            # Should not raise
                            result = s.snapshot()
                            assert "state" in result

    def test_both_modules_handle_empty_inputs(self, integrated_config):
        """Both modules should handle empty inputs gracefully."""
        # Empty process list
        assert pg.get_protected_cgroup_names([]) == set()

        # Empty PID set
        result = pg.detect_new_processes(set(), set())
        assert result == []

        # Explicit default config (avoid reading user's file)
        s = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        assert s.config["target_free_percent"] == 18.0


# ---------------------------------------------------------------------------
# Integration: Config consistency
# ---------------------------------------------------------------------------

class TestConfigConsistency:
    """Tests for config consistency between modules."""

    def test_protected_lists_are_compatible(self, integrated_config):
        """Protected lists from both modules should be compatible."""
        guardian_protected = integrated_config["guardian"].protected_names
        ram_keeper_protected = set(integrated_config["ram_keeper"]["protected"])

        # Both should be sets of strings
        assert all(isinstance(x, str) for x in guardian_protected)
        assert all(isinstance(x, str) for x in ram_keeper_protected)

    def test_merged_config_is_valid(self, integrated_config):
        """Merging configs from both modules should produce valid config."""
        # Merge protected lists
        merged_protected = list(set(
            integrated_config["guardian"].protected_names
            | set(integrated_config["ram_keeper"]["protected"])
        ))

        # Create merged config
        merged_config = dict(integrated_config["ram_keeper"])
        merged_config["protected"] = merged_protected

        # Should be loadable
        s = rk.Steward(config=merged_config)
        assert isinstance(s.config["protected"], list)
        assert len(s.config["protected"]) > 0

    def test_guardian_config_in_ram_keeper_format(self, integrated_config):
        """Guardian config can be converted to ram_keeper format."""
        guardian_config = integrated_config["guardian"]

        # Convert to ram_keeper format
        rk_config = {
            "protected": list(guardian_config.protected_names),
            "never_reclaim": [],
        }

        # Should be valid
        assert isinstance(rk_config["protected"], list)
        assert all(isinstance(x, str) for x in rk_config["protected"])
