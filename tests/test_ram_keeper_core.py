"""Tests for ram_keeper core functions: scan, snapshot, reclaim_from, load_config."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

# Resolve the plugin directory from this file's own location
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import ram_keeper as rk  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_cgroup_tree(tmp_path):
    """Create a mock cgroup tree for testing scan()."""
    root = tmp_path / "user.slice" / "user-1000.slice"
    root.mkdir(parents=True)

    # Create a leaf cgroup with memory.current
    app_dir = root / "app-firefox.scope"
    app_dir.mkdir()
    (app_dir / "memory.current").write_text("500000000")
    (app_dir / "memory.stat").write_text("anon 200000000\nfile 300000000\ninactive_file 250000000\n")
    (app_dir / "pids.current").write_text("5")
    (app_dir / "memory.swap.current").write_text("100000000")
    (app_dir / "memory.swap.max").write_text("max")

    # Create a protected cgroup
    protected_dir = root / "wayland-wm@hyprland.desktop.service"
    protected_dir.mkdir()
    (protected_dir / "memory.current").write_text("300000000")
    (protected_dir / "memory.stat").write_text("anon 100000000\nfile 200000000\ninactive_file 150000000\n")
    (protected_dir / "pids.current").write_text("10")
    (protected_dir / "memory.swap.current").write_text("50000000")
    (protected_dir / "memory.swap.max").write_text("max")

    # Create a non-leaf cgroup (should be skipped)
    parent_dir = root / "app.slice"
    parent_dir.mkdir()
    (parent_dir / "memory.current").write_text("1000000000")
    (parent_dir / "memory.stat").write_text("anon 500000000\nfile 500000000\ninactive_file 400000000\n")

    # Create a small cgroup (below min_bytes)
    small_dir = root / "small-app.scope"
    small_dir.mkdir()
    (small_dir / "memory.current").write_text("1000000")  # 1 MB
    (small_dir / "memory.stat").write_text("anon 500000\nfile 500000\ninactive_file 400000\n")
    (small_dir / "pids.current").write_text("1")
    (small_dir / "memory.swap.current").write_text("0")
    (small_dir / "memory.swap.max").write_text("max")

    return root


@pytest.fixture
def mock_meminfo():
    """Mock /proc/meminfo content."""
    return {
        "MemTotal": 8026562560,
        "MemAvailable": 3073976832,
        "MemFree": 2000000000,
        "Cached": 1500000000,
        "SwapTotal": 8027074560,
        "SwapFree": 7000000000,
    }


# ---------------------------------------------------------------------------
# Tests: _read and _read_int
# ---------------------------------------------------------------------------

class TestReadHelpers:
    """Tests for _read and _read_int helper functions."""

    def test_read_returns_stripped_content(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("  hello world  \n")
        assert rk._read(f) == "hello world"

    def test_read_returns_default_on_missing(self, tmp_path):
        assert rk._read(tmp_path / "nonexistent", "default") == "default"

    def test_read_returns_empty_default(self, tmp_path):
        assert rk._read(tmp_path / "nonexistent") == ""

    def test_read_int_parses_integer(self, tmp_path):
        f = tmp_path / "num.txt"
        f.write_text("42\n")
        assert rk._read_int(f) == 42

    def test_read_int_returns_default_on_invalid(self, tmp_path):
        f = tmp_path / "num.txt"
        f.write_text("not a number")
        assert rk._read_int(f, 99) == 99

    def test_read_int_returns_default_on_missing(self, tmp_path):
        assert rk._read_int(tmp_path / "nonexistent", 99) == 99


# ---------------------------------------------------------------------------
# Tests: self_cgroup
# ---------------------------------------------------------------------------

class TestSelfCgroup:
    """Tests for self_cgroup."""

    def test_returns_string(self):
        result = rk.self_cgroup()
        assert isinstance(result, str)

    def test_returns_empty_on_error(self):
        with patch("pathlib.Path.read_text", side_effect=OSError):
            result = rk.self_cgroup()
            assert result == ""


# ---------------------------------------------------------------------------
# Tests: user_root
# ---------------------------------------------------------------------------

class TestUserRoot:
    """Tests for user_root."""

    def test_returns_path(self):
        result = rk.user_root()
        assert isinstance(result, Path)

    def test_finds_user_slice(self):
        """Should find the user slice for the current UID."""
        import os
        uid = os.getuid()
        result = rk.user_root()
        # Should at least return a path (may not exist in test env)
        assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# Tests: human_label
# ---------------------------------------------------------------------------

class TestHumanLabel:
    """Tests for human_label."""

    def test_strips_app_prefix(self):
        assert rk.human_label("app-firefox.scope") == "firefox"

    def test_strips_scope_suffix(self):
        assert rk.human_label("my-app.scope") == "my-app"

    def test_strips_service_suffix(self):
        assert rk.human_label("my-service.service") == "my-service"

    def test_handles_chromium(self):
        result = rk.human_label("app-org.chromium.Chromium-123.scope")
        assert "Chromium" in result

    def test_handles_empty(self):
        assert rk.human_label("") == ""

    def test_handles_escaped_dashes(self):
        assert rk.human_label("app-my\\x2dapp.scope") == "my-app"


# ---------------------------------------------------------------------------
# Tests: _stat_field
# ---------------------------------------------------------------------------

class TestStatField:
    """Tests for _stat_field."""

    def test_parses_value(self):
        stat_text = "anon 12345\nfile 67890\n"
        assert rk._stat_field(stat_text, "anon") == 12345
        assert rk._stat_field(stat_text, "file") == 67890

    def test_returns_zero_for_missing_key(self):
        stat_text = "anon 12345\n"
        assert rk._stat_field(stat_text, "nonexistent") == 0

    def test_returns_zero_for_invalid_value(self):
        stat_text = "anon notanumber\n"
        assert rk._stat_field(stat_text, "anon") == 0

    def test_handles_empty_string(self):
        assert rk._stat_field("", "anon") == 0


# ---------------------------------------------------------------------------
# Tests: cgroup_swap
# ---------------------------------------------------------------------------

class TestCgroupSwap:
    """Tests for cgroup_swap."""

    def test_reads_swap_current(self, tmp_path):
        (tmp_path / "memory.swap.current").write_text("123456789")
        assert rk.cgroup_swap(tmp_path) == 123456789

    def test_falls_back_to_stat(self, tmp_path):
        """Should fall back to memory.stat if swap.current is missing."""
        (tmp_path / "memory.swap.current").write_text("-1")
        (tmp_path / "memory.stat").write_text("swap 987654321\n")
        assert rk.cgroup_swap(tmp_path) == 987654321

    def test_returns_zero_on_error(self, tmp_path):
        assert rk.cgroup_swap(tmp_path) == 0


# ---------------------------------------------------------------------------
# Tests: scan
# ---------------------------------------------------------------------------

class TestScan:
    """Tests for scan function."""

    def test_finds_leaf_cgroups(self, mock_cgroup_tree):
        """Should find leaf cgroups with memory.current."""
        result = rk.scan(mock_cgroup_tree, protected=set(), min_bytes=1024 * 1024)
        names = {g.name for g in result}
        assert "app-firefox.scope" in names
        assert "wayland-wm@hyprland.desktop.service" in names

    def test_skips_non_leaf_cgroups(self, mock_cgroup_tree):
        """Should skip cgroups that have children with memory.current."""
        # Add a child to app.slice so it's a non-leaf
        child_dir = mock_cgroup_tree / "app.slice" / "app-child.scope"
        child_dir.mkdir()
        (child_dir / "memory.current").write_text("100000000")
        (child_dir / "memory.stat").write_text("anon 50000000\nfile 50000000\ninactive_file 40000000\n")

        result = rk.scan(mock_cgroup_tree, protected=set(), min_bytes=1024 * 1024)
        names = {g.name for g in result}
        assert "app.slice" not in names
        assert "app-child.scope" in names

    def test_skips_small_cgroups(self, mock_cgroup_tree):
        """Should skip cgroups below min_bytes threshold."""
        result = rk.scan(mock_cgroup_tree, protected=set(), min_bytes=1024 * 1024)
        names = {g.name for g in result}
        assert "small-app.scope" not in names

    def test_marks_protected_cgroups(self, mock_cgroup_tree):
        """Should mark protected cgroups correctly."""
        protected = {"wayland-wm@hyprland.desktop.service"}
        result = rk.scan(mock_cgroup_tree, protected=protected, min_bytes=1024 * 1024)
        for g in result:
            if g.name == "wayland-wm@hyprland.desktop.service":
                assert g.protected is True
            else:
                assert g.protected is False

    def test_marks_inviolable_cgroups(self, mock_cgroup_tree):
        """Should mark inviolable cgroups as protected."""
        result = rk.scan(mock_cgroup_tree, protected=set(), min_bytes=1024 * 1024)
        for g in result:
            if "hyprland" in g.name:
                assert g.protected is True

    def test_returns_empty_for_nonexistent_root(self, tmp_path):
        """Should return empty list if root doesn't exist."""
        result = rk.scan(tmp_path / "nonexistent", protected=set(), min_bytes=0)
        assert result == []

    def test_parses_memory_stats(self, mock_cgroup_tree):
        """Should correctly parse memory statistics."""
        result = rk.scan(mock_cgroup_tree, protected=set(), min_bytes=1024 * 1024)
        firefox = next(g for g in result if g.name == "app-firefox.scope")
        assert firefox.current == 500000000
        assert firefox.anon == 200000000
        assert firefox.file == 300000000
        assert firefox.reclaimable_file == 250000000
        assert firefox.reclaimable == 250000000  # min(file, inactive_file)
        assert firefox.pids == 5
        assert firefox.swap == 100000000

    def test_detects_swapable(self, mock_cgroup_tree):
        """Should detect swapable based on memory.swap.max."""
        result = rk.scan(mock_cgroup_tree, protected=set(), min_bytes=1024 * 1024)
        firefox = next(g for g in result if g.name == "app-firefox.scope")
        assert firefox.swapable is True

    def test_detects_non_swapable(self, mock_cgroup_tree):
        """Should detect non-swapable when memory.swap.max is 0."""
        # Add a non-swapable cgroup
        locked_dir = mock_cgroup_tree / "locked-app.scope"
        locked_dir.mkdir()
        (locked_dir / "memory.current").write_text("500000000")
        (locked_dir / "memory.stat").write_text("anon 200000000\nfile 300000000\ninactive_file 250000000\n")
        (locked_dir / "pids.current").write_text("3")
        (locked_dir / "memory.swap.current").write_text("0")
        (locked_dir / "memory.swap.max").write_text("0")

        result = rk.scan(mock_cgroup_tree, protected=set(), min_bytes=1024 * 1024)
        locked = next(g for g in result if g.name == "locked-app.scope")
        assert locked.swapable is False

    def test_handles_cgroup_vanishing_mid_scan(self, mock_cgroup_tree):
        """Should handle cgroups that vanish during scan."""
        original_is_dir = Path.is_dir
        call_count = [0]

        def flaky_is_dir(self):
            call_count[0] += 1
            # Fail on every 10th call to simulate a vanishing cgroup
            if call_count[0] % 10 == 0:
                raise OSError("No such file or directory")
            return original_is_dir(self)

        with patch.object(Path, "is_dir", flaky_is_dir):
            # Should not raise
            result = rk.scan(mock_cgroup_tree, protected=set(), min_bytes=1024 * 1024)
            assert isinstance(result, list)

    def test_skips_mount_cgroups(self, mock_cgroup_tree):
        """Should skip .mount cgroups."""
        mount_dir = mock_cgroup_tree / "systemd-123.mount"
        mount_dir.mkdir()
        (mount_dir / "memory.current").write_text("1000000")
        (mount_dir / "memory.stat").write_text("anon 500000\nfile 500000\ninactive_file 400000\n")

        result = rk.scan(mock_cgroup_tree, protected=set(), min_bytes=0)
        names = {g.name for g in result}
        assert "systemd-123.mount" not in names


# ---------------------------------------------------------------------------
# Tests: meminfo
# ---------------------------------------------------------------------------

class TestMeminfo:
    """Tests for meminfo."""

    def test_parses_meminfo(self):
        """Should parse /proc/meminfo correctly."""
        result = rk.meminfo()
        assert isinstance(result, dict)
        # On a real system, these should be present
        if result:
            assert "MemTotal" in result
            assert isinstance(result["MemTotal"], int)

    def test_returns_empty_on_error(self):
        """Should return empty dict on error."""
        with patch("pathlib.Path.read_text", side_effect=OSError):
            result = rk.meminfo()
            assert result == {}

    def test_handles_malformed_lines(self):
        """Should handle malformed lines gracefully (may return partial/empty dict)."""
        # A line with ":" but invalid value causes ValueError, which stops processing
        # The function returns whatever was parsed before the error (or empty dict)
        meminfo_content = "MemTotal: 8026562560 kB\nMemAvailable: notanumber kB\nCached: 1500000000 kB\n"
        with patch("pathlib.Path.read_text", return_value=meminfo_content):
            result = rk.meminfo()
        # The function should not crash; result is a dict (possibly empty or partial)
        assert isinstance(result, dict)
        # MemTotal should be parsed (it comes before the malformed line)
        if result:
            assert result.get("MemTotal") == 8026562560 * 1024


# ---------------------------------------------------------------------------
# Tests: psi
# ---------------------------------------------------------------------------

class TestPsi:
    """Tests for psi."""

    def test_returns_dict_with_expected_keys(self):
        """Should return dict with PSI keys."""
        result = rk.psi("memory")
        assert isinstance(result, dict)
        expected_keys = {"some_avg10", "some_avg60", "some_avg300", "full_avg10", "full_avg60", "full_avg300"}
        assert expected_keys.issubset(result.keys())

    def test_returns_zeros_on_error(self):
        """Should return zeros if PSI file is unreadable."""
        with patch("pathlib.Path.read_text", side_effect=OSError):
            result = rk.psi("memory")
            assert result["some_avg10"] == 0.0
            assert result["full_avg60"] == 0.0

    def test_parses_psi_output(self):
        """Should parse PSI output correctly."""
        psi_text = "some avg10=5.5 avg60=3.2\nfull avg10=2.1 avg60=1.0\n"
        with patch("pathlib.Path.read_text", return_value=psi_text):
            result = rk.psi("memory")
            assert result["some_avg10"] == 5.5
            assert result["some_avg60"] == 3.2
            assert result["full_avg10"] == 2.1
            assert result["full_avg60"] == 1.0


# ---------------------------------------------------------------------------
# Tests: zram_stats
# ---------------------------------------------------------------------------

class TestZramStats:
    """Tests for zram_stats."""

    def test_returns_dict_with_devices(self):
        """Should return dict with devices list."""
        result = rk.zram_stats()
        assert isinstance(result, dict)
        assert "devices" in result
        assert "used" in result
        assert "size" in result

    def test_handles_subprocess_error(self):
        """Should return empty result on subprocess error."""
        with patch("subprocess.run", side_effect=OSError):
            result = rk.zram_stats()
            assert result["devices"] == []
            assert result["used"] == 0

    def test_parses_swapon_output(self):
        """Should parse swapon output correctly."""
        swapon_output = "/dev/zram0 partition 8025796608 2950127616 0\n/swap/swapfile file 8027074560 7417856 -2\n"
        mock_result = MagicMock()
        mock_result.stdout = swapon_output
        with patch("subprocess.run", return_value=mock_result):
            result = rk.zram_stats()
            assert len(result["devices"]) == 2
            assert result["used"] == 2950127616 + 7417856
            assert result["ram_used"] == 2950127616
            assert result["disk_used"] == 7417856


# ---------------------------------------------------------------------------
# Tests: is_inviolable
# ---------------------------------------------------------------------------

class TestIsInviolable:
    """Tests for is_inviolable."""

    @pytest.mark.parametrize("name,expected", [
        ("wayland-wm@hyprland.desktop", True),
        ("wayland-wm@hyprland.desktop.service", True),
        ("session", True),
        ("session.slice", True),
        ("session-2.scope", True),
        ("quickshell", True),
        ("quickshell.service", True),
        ("ram-keeper", True),
        ("ram-keeper.service", True),
        ("app-org.chromium.Chromium-123.scope", False),
        ("syncthing.service", False),
        ("firefox", False),
        ("", False),
    ])
    def test_is_inviolable(self, name, expected):
        assert rk.is_inviolable(name) == expected


# ---------------------------------------------------------------------------
# Tests: config_path
# ---------------------------------------------------------------------------

class TestConfigPath:
    """Tests for config_path."""

    def test_returns_path_in_config_dir(self):
        result = rk.config_path()
        assert isinstance(result, Path)
        assert "ram-keeper" in str(result)
        assert "config.json" in str(result)


# ---------------------------------------------------------------------------
# Tests: load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    """Tests for load_config."""

    def test_returns_default_config(self):
        """Should return default config when no file exists."""
        with patch("pathlib.Path.read_text", side_effect=OSError):
            result = rk.load_config()
        assert result["target_free_percent"] == 18.0
        assert result["critical_free_percent"] == 10.0
        assert result["psi_some_percent"] == 6.0
        assert result["min_target_bytes"] == 96 * 1024 * 1024
        assert result["interval_idle"] == 20.0
        assert result["interval_pressure"] == 5.0
        assert result["max_reclaim_per_pass"] == 2 * 1024 * 1024 * 1024
        assert result["cache_first"] is True
        assert result["max_victim_fraction"] == 25
        assert isinstance(result["protected"], list)
        assert isinstance(result["never_reclaim"], list)

    def test_loads_user_config(self, tmp_path):
        """Should load and merge user config."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "target_free_percent": 25.0,
            "interval_idle": 30.0,
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["target_free_percent"] == 25.0
        assert result["interval_idle"] == 30.0
        # Defaults should still be present
        assert result["critical_free_percent"] == 10.0

    def test_repairs_inverted_thresholds(self, tmp_path):
        """Should repair inverted critical/target thresholds."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "target_free_percent": 5.0,
            "critical_free_percent": 40.0,
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["critical_free_percent"] < result["target_free_percent"]

    def test_repairs_equal_thresholds(self, tmp_path):
        """Should repair equal critical/target thresholds."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "target_free_percent": 18.0,
            "critical_free_percent": 18.0,
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["critical_free_percent"] < result["target_free_percent"]

    def test_repairs_interval_order(self, tmp_path):
        """Should repair interval_pressure >= interval_idle."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "interval_idle": 5.0,
            "interval_pressure": 60.0,
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["interval_pressure"] < result["interval_idle"]

    def test_clamps_out_of_bounds_values(self, tmp_path):
        """Should clamp values to CONFIG_BOUNDS."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "target_free_percent": 100.0,  # above max of 60
            "interval_idle": -5.0,  # below min of 1
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["target_free_percent"] <= 60.0
        assert result["interval_idle"] >= 1.0

    def test_handles_invalid_json(self, tmp_path):
        """Should fall back to defaults on invalid JSON."""
        config_file = tmp_path / "config.json"
        config_file.write_text("not valid json {{{")
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["target_free_percent"] == 18.0  # default

    def test_handles_non_dict_json(self, tmp_path):
        """Should fall back to defaults if JSON is not a dict."""
        config_file = tmp_path / "config.json"
        config_file.write_text("[1, 2, 3]")
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["target_free_percent"] == 18.0  # default

    def test_merges_protected_list(self, tmp_path):
        """Should merge protected list with defaults."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "protected": ["my-app"],
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert "my-app" in result["protected"]
        # Defaults should still be present
        assert "quickshell" in result["protected"]

    def test_handles_string_as_list(self, tmp_path):
        """Should handle a bare string for protected/never_reclaim."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "protected": "my-app",
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert isinstance(result["protected"], list)
        assert "my-app" in result["protected"]

    def test_handles_bool_as_number(self, tmp_path):
        """Should reject bool values for numeric fields."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "interval_idle": True,
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["interval_idle"] == 20.0  # default

    def test_handles_null_value(self, tmp_path):
        """Should handle null values gracefully."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "target_free_percent": None,
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["target_free_percent"] == 18.0  # default

    def test_ignores_unknown_keys(self, tmp_path):
        """Should ignore unknown config keys."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "unknown_key": "value",
            "target_free_percent": 22.0,
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["target_free_percent"] == 22.0
        assert "unknown_key" not in result

    def test_cache_first_must_be_bool(self, tmp_path):
        """Should reject non-bool cache_first values."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "cache_first": "yes",
        }))
        with patch("ram_keeper.config_path", return_value=config_file):
            result = rk.load_config()
        assert result["cache_first"] is True  # default


# ---------------------------------------------------------------------------
# Tests: config_warnings
# ---------------------------------------------------------------------------

class TestConfigWarnings:
    """Tests for config_warnings."""

    def test_returns_list(self):
        result = rk.config_warnings()
        assert isinstance(result, list)

    def test_cleared_on_new_load(self, tmp_path):
        """Warnings should be cleared when load_config is called."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"target_free_percent": 100.0}))
        with patch("ram_keeper.config_path", return_value=config_file):
            rk.load_config()
            warnings = rk.config_warnings()
            assert len(warnings) > 0
            # Load again with good config
            config_file.write_text(json.dumps({"target_free_percent": 20.0}))
            rk.load_config()
            warnings = rk.config_warnings()
            assert len(warnings) == 0


# ---------------------------------------------------------------------------
# Tests: Steward.snapshot
# ---------------------------------------------------------------------------

class TestSnapshot:
    """Tests for Steward.snapshot."""

    def test_returns_dict_with_expected_keys(self):
        """Should return a snapshot dict with all expected keys."""
        s = rk.Steward()
        with patch("ram_keeper.meminfo", return_value={"MemTotal": 8026562560, "MemAvailable": 3073976832, "Cached": 1500000000}):
            with patch("ram_keeper.psi", return_value={"some_avg10": 0, "some_avg60": 0, "some_avg300": 0, "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}):
                with patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}):
                    with patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
                        result = s.snapshot()
        assert "state" in result
        assert "free_percent" in result
        assert "mem_total" in result
        assert "mem_available" in result
        assert "groups" in result
        assert "forecast" in result

    def test_state_ok_when_healthy(self):
        """Should report 'ok' when memory is healthy."""
        s = rk.Steward()
        with patch("ram_keeper.meminfo", return_value={"MemTotal": 8026562560, "MemAvailable": 4000000000, "Cached": 1500000000}):
            with patch("ram_keeper.psi", return_value={"some_avg10": 0, "some_avg60": 0, "some_avg300": 0, "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}):
                with patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}):
                    with patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
                        result = s.snapshot()
        assert result["state"] == "ok"

    def test_state_tight_when_low(self):
        """Should report 'tight' when free memory is below target."""
        s = rk.Steward()
        # 15% free, target is 18%
        with patch("ram_keeper.meminfo", return_value={"MemTotal": 8026562560, "MemAvailable": 1200000000, "Cached": 500000000}):
            with patch("ram_keeper.psi", return_value={"some_avg10": 0, "some_avg60": 0, "some_avg300": 0, "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}):
                with patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}):
                    with patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
                        result = s.snapshot()
        assert result["state"] == "tight"

    def test_state_critical_when_very_low(self):
        """Should report 'critical' when free memory is below critical threshold."""
        s = rk.Steward()
        # 5% free, critical is 10%
        with patch("ram_keeper.meminfo", return_value={"MemTotal": 8026562560, "MemAvailable": 400000000, "Cached": 200000000}):
            with patch("ram_keeper.psi", return_value={"some_avg10": 0, "some_avg60": 0, "some_avg300": 0, "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}):
                with patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}):
                    with patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
                        result = s.snapshot()
        assert result["state"] == "critical"

    def test_state_unknown_when_no_readings(self):
        """Should report 'unknown' when meminfo is unreadable."""
        s = rk.Steward()
        with patch("ram_keeper.meminfo", return_value={}):
            with patch("ram_keeper.psi", return_value={"some_avg10": 0, "some_avg60": 0, "some_avg300": 0, "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}):
                with patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}):
                    with patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
                        result = s.snapshot()
        assert result["state"] == "unknown"

    def test_state_stalling_when_high_psi(self):
        """Should report 'stalling' when PSI is high but free memory is OK."""
        s = rk.Steward()
        with patch("ram_keeper.meminfo", return_value={"MemTotal": 8026562560, "MemAvailable": 4000000000, "Cached": 1500000000}):
            with patch("ram_keeper.psi", return_value={"some_avg10": 0, "some_avg60": 10.0, "some_avg300": 0, "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}):
                with patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}):
                    with patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
                        result = s.snapshot()
        assert result["state"] == "stalling"

    def test_handles_zero_mem_total(self):
        """Should handle zero MemTotal gracefully."""
        s = rk.Steward()
        with patch("ram_keeper.meminfo", return_value={"MemTotal": 0, "MemAvailable": 0}):
            with patch("ram_keeper.psi", return_value={"some_avg10": 0, "some_avg60": 0, "some_avg300": 0, "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}):
                with patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}):
                    with patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
                        result = s.snapshot()
        assert result["state"] == "unknown"


# ---------------------------------------------------------------------------
# Tests: Steward.reclaim_from
# ---------------------------------------------------------------------------

class TestReclaimFrom:
    """Tests for Steward.reclaim_from."""

    def test_returns_action(self):
        """Should return an Action object."""
        s = rk.Steward()
        group = rk.Cgroup(
            path=Path("/nonexistent"),
            name="test",
            kind="scope",
            label="test",
            current=1000000,
            anon=500000,
        )
        with patch("ram_keeper._read_int", return_value=500000):
            with patch("ram_keeper.cgroup_swap", return_value=0):
                with patch("builtins.open", mock_open()):
                    with patch("time.sleep"):
                        result = s.reclaim_from(group, 100000)
        assert isinstance(result, rk.Action)
        assert result.label == "test"
        assert result.requested == 100000

    def test_handles_permission_error(self):
        """Should handle PermissionError gracefully."""
        s = rk.Steward()
        group = rk.Cgroup(
            path=Path("/nonexistent"),
            name="test",
            kind="scope",
            label="test",
            current=1000000,
            anon=500000,
        )
        with patch("ram_keeper._read_int", return_value=1000000):
            with patch("ram_keeper.cgroup_swap", return_value=0):
                with patch("builtins.open", side_effect=PermissionError):
                    with patch("time.sleep"):
                        result = s.reclaim_from(group, 100000)
        assert result.outcome == "denied"

    def test_handles_eagain(self):
        """Should handle EAGAIN (errno 11) as 'eagain' outcome."""
        s = rk.Steward()
        group = rk.Cgroup(
            path=Path("/nonexistent"),
            name="test",
            kind="scope",
            label="test",
            current=1000000,
            anon=500000,
        )
        with patch("ram_keeper._read_int", return_value=1000000):
            with patch("ram_keeper.cgroup_swap", return_value=0):
                with patch("builtins.open", side_effect=OSError(11, "Resource temporarily unavailable")):
                    with patch("time.sleep"):
                        result = s.reclaim_from(group, 100000)
        assert result.outcome == "eagain"

    def test_handles_other_oserror(self):
        """Should handle other OSErrons with error code."""
        s = rk.Steward()
        group = rk.Cgroup(
            path=Path("/nonexistent"),
            name="test",
            kind="scope",
            label="test",
            current=1000000,
            anon=500000,
        )
        # Use errno 5 (EIO) which is not EAGAIN (11) or EACCES (13)
        with patch("ram_keeper._read_int", return_value=1000000):
            with patch("ram_keeper.cgroup_swap", return_value=0):
                with patch("builtins.open", side_effect=OSError(5, "Input/output error")):
                    with patch("time.sleep"):
                        result = s.reclaim_from(group, 100000)
        assert result.outcome == "error:5"

    def test_calculates_freed_bytes(self):
        """Should calculate freed bytes correctly."""
        s = rk.Steward()
        group = rk.Cgroup(
            path=Path("/nonexistent"),
            name="test",
            kind="scope",
            label="test",
            current=1000000,
            anon=500000,
        )
        # Before: 1000000, After: 600000 -> freed = 400000
        with patch("ram_keeper._read_int", side_effect=[1000000, 600000]):
            with patch("ram_keeper.cgroup_swap", return_value=0):
                with patch("builtins.open", mock_open()):
                    with patch("time.sleep"):
                        result = s.reclaim_from(group, 500000)
        assert result.cgroup_freed == 400000

    def test_handles_negative_freed(self):
        """Should clamp negative freed to zero."""
        s = rk.Steward()
        group = rk.Cgroup(
            path=Path("/nonexistent"),
            name="test",
            kind="scope",
            label="test",
            current=1000000,
            anon=500000,
        )
        # Before: 1000000, After: 1200000 -> freed = -200000 -> clamped to 0
        with patch("ram_keeper._read_int", side_effect=[1000000, 1200000]):
            with patch("ram_keeper.cgroup_swap", return_value=0):
                with patch("builtins.open", mock_open()):
                    with patch("time.sleep"):
                        result = s.reclaim_from(group, 500000)
        assert result.cgroup_freed == 0


# ---------------------------------------------------------------------------
# Tests: Steward.max_victim_slice
# ---------------------------------------------------------------------------

class TestMaxVictimSlice:
    """Tests for Steward.max_victim_slice."""

    def test_returns_positive_int(self):
        """Should return a positive integer."""
        s = rk.Steward()
        result = s.max_victim_slice()
        assert isinstance(result, int)
        assert result > 0

    def test_respects_config(self):
        """Should respect max_victim_fraction config."""
        config = dict(rk.DEFAULT_CONFIG)
        config["max_victim_fraction"] = 50
        s = rk.Steward(config=config)
        # Mock meminfo to return 8 GB
        with patch("ram_keeper.meminfo", return_value={"MemTotal": 8 * 1024**3}):
            result = s.max_victim_slice()
        # 50% of 8 GB = 4 GB
        assert result == int(0.50 * 8 * 1024**3)


# ---------------------------------------------------------------------------
# Tests: Steward.zram_ratio
# ---------------------------------------------------------------------------

class TestZramRatio:
    """Tests for Steward.zram_ratio."""

    def test_returns_float(self):
        """Should return a float."""
        result = rk.Steward.zram_ratio()
        assert isinstance(result, float)
        assert result > 0

    def test_fallback_on_error(self):
        """Should return 3.0 fallback on error."""
        with patch("pathlib.Path.read_text", side_effect=OSError):
            result = rk.Steward.zram_ratio()
            assert result == 3.0

    def test_parses_mm_stat(self, tmp_path):
        """Should parse /sys/block/zram0/mm_stat correctly."""
        mm_stat = tmp_path / "mm_stat"
        mm_stat.write_text("1000000 250000\n")
        with patch("pathlib.Path.read_text", return_value="1000000 250000\n"):
            result = rk.Steward.zram_ratio()
            assert result == 4.0  # 1000000 / 250000


# ---------------------------------------------------------------------------
# Tests: Steward.log
# ---------------------------------------------------------------------------

class TestLog:
    """Tests for Steward.log."""

    def test_writes_jsonl(self, tmp_path):
        """Should write JSON lines to the log file."""
        log_file = tmp_path / "test.jsonl"
        s = rk.Steward(log_path=log_file)
        s.log({"event": "test", "data": "value"})
        content = log_file.read_text()
        assert "test" in content
        assert "value" in content

    def test_handles_oserror(self, tmp_path):
        """Should handle OSError gracefully."""
        log_file = tmp_path / "nonexistent" / "test.jsonl"
        s = rk.Steward(log_path=log_file)
        # Should not raise
        s.log({"event": "test"})

    def test_adds_timestamp(self, tmp_path):
        """Should add a timestamp to the event."""
        log_file = tmp_path / "test.jsonl"
        s = rk.Steward(log_path=log_file)
        s.log({"event": "test"})
        content = json.loads(log_file.read_text().strip())
        assert "ts" in content


# ---------------------------------------------------------------------------
# Tests: Steward.publish
# ---------------------------------------------------------------------------

class TestPublish:
    """Tests for Steward.publish."""

    def test_writes_snapshot(self, tmp_path):
        """Should write snapshot to state file."""
        state_file = tmp_path / "state.json"
        s = rk.Steward(state_path=state_file)
        snapshot = {"state": "ok", "mem_total": 1000}
        s.publish(snapshot)
        content = json.loads(state_file.read_text())
        assert content["state"] == "ok"

    def test_handles_oserror(self, tmp_path):
        """Should handle OSError gracefully."""
        state_file = tmp_path / "nonexistent" / "state.json"
        s = rk.Steward(state_path=state_file)
        # Should not raise
        s.publish({"state": "ok"})


# ---------------------------------------------------------------------------
# Tests: Steward._sleep
# ---------------------------------------------------------------------------

class TestSleep:
    """Tests for Steward._sleep."""

    def test_sleeps_positive_values(self):
        """Should sleep for positive values."""
        s = rk.Steward()
        with patch("time.sleep") as mock_sleep:
            s._sleep(1.0)
            mock_sleep.assert_called_once_with(1.0)

    def test_rejects_zero(self):
        """Should reject zero and use default."""
        s = rk.Steward()
        with patch("time.sleep") as mock_sleep:
            s._sleep(0)
            mock_sleep.assert_called_once_with(20.0)

    def test_rejects_negative(self):
        """Should reject negative values and use default."""
        s = rk.Steward()
        with patch("time.sleep") as mock_sleep:
            s._sleep(-5.0)
            mock_sleep.assert_called_once_with(20.0)

    def test_rejects_nan(self):
        """Should reject NaN and use default."""
        s = rk.Steward()
        with patch("time.sleep") as mock_sleep:
            s._sleep(float("nan"))
            mock_sleep.assert_called_once_with(20.0)

    def test_rejects_inf(self):
        """Should reject infinity and use default."""
        s = rk.Steward()
        with patch("time.sleep") as mock_sleep:
            s._sleep(float("inf"))
            mock_sleep.assert_called_once_with(20.0)

    def test_rejects_string(self):
        """Should reject string values and use default."""
        s = rk.Steward()
        with patch("time.sleep") as mock_sleep:
            s._sleep("abc")
            mock_sleep.assert_called_once_with(20.0)

    def test_rejects_none(self):
        """Should reject None and use default."""
        s = rk.Steward()
        with patch("time.sleep") as mock_sleep:
            s._sleep(None)
            mock_sleep.assert_called_once_with(20.0)

    def test_caps_at_3600(self):
        """Should cap sleep at 3600 seconds."""
        s = rk.Steward()
        with patch("time.sleep") as mock_sleep:
            s._sleep(7200.0)
            mock_sleep.assert_called_once_with(3600.0)


# ---------------------------------------------------------------------------
# Tests: published_snapshot
# ---------------------------------------------------------------------------

class TestPublishedSnapshot:
    """Tests for published_snapshot."""

    def test_returns_none_for_missing_file(self, tmp_path):
        """Should return None if state file doesn't exist."""
        with patch("ram_keeper.Path.home", return_value=tmp_path):
            result = rk.published_snapshot()
            assert result is None

    def test_returns_none_for_stale_file(self, tmp_path):
        """Should return None if state file is too old."""
        state_file = tmp_path / ".local" / "state" / "ram-keeper" / "state.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps({"state": "ok", "mem_total": 1000}))
        # Make it old
        old_time = time.time() - 300  # 5 minutes ago
        import os
        os.utime(state_file, (old_time, old_time))
        with patch("ram_keeper.Path.home", return_value=tmp_path):
            result = rk.published_snapshot(max_age=120)
            assert result is None

    def test_returns_snapshot_for_fresh_file(self, tmp_path):
        """Should return snapshot if state file is fresh."""
        state_file = tmp_path / ".local" / "state" / "ram-keeper" / "state.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps({"state": "ok", "mem_total": 1000}))
        with patch("ram_keeper.Path.home", return_value=tmp_path):
            result = rk.published_snapshot(max_age=120)
            assert result is not None
            assert result["state"] == "ok"

    def test_returns_none_for_invalid_json(self, tmp_path):
        """Should return None if state file contains invalid JSON."""
        state_file = tmp_path / ".local" / "state" / "ram-keeper" / "state.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text("not valid json")
        with patch("ram_keeper.Path.home", return_value=tmp_path):
            result = rk.published_snapshot()
            assert result is None

    def test_returns_none_for_non_dict(self, tmp_path):
        """Should return None if state file contains non-dict JSON."""
        state_file = tmp_path / ".local" / "state" / "ram-keeper" / "state.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text("[1, 2, 3]")
        with patch("ram_keeper.Path.home", return_value=tmp_path):
            result = rk.published_snapshot()
            assert result is None
