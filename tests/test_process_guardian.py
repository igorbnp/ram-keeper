"""Tests for process_guardian: process detection, description lookup, protection decision."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Resolve the plugin directory from this file's own location
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import process_guardian as pg  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_processes():
    """A set of sample PIDs for testing."""
    return {1, 2, 3, 100, 200}


@pytest.fixture
def mock_process_info():
    """Mock ProcessInfo objects for testing."""
    return [
        pg.ProcessInfo(pid=100, name="firefox", cmdline="/usr/lib/firefox/firefox", cgroup="/user.slice/user-1000.slice/app-firefox.scope"),
        pg.ProcessInfo(pid=200, name="hyprland", cmdline="/usr/bin/Hyprland", cgroup="/user.slice/user-1000.slice/session-2.scope"),
        pg.ProcessInfo(pid=300, name="unknown-binary", cmdline="/opt/unknown/binary", cgroup="/user.slice/user-1000.slice/app-unknown.scope"),
    ]


@pytest.fixture
def guardian_config():
    """A basic guardian config."""
    return pg.GuardianConfig(
        protected_names={"firefox", "my-app"},
        protected_descriptions={"Web Browser"},
        scan_interval=10.0,
        max_processes=100,
    )


# ---------------------------------------------------------------------------
# Tests: list_current_pids
# ---------------------------------------------------------------------------

class TestListCurrentPids:
    """Tests for list_current_pids."""

    def test_returns_set_of_ints(self):
        result = pg.list_current_pids()
        assert isinstance(result, set)
        assert all(isinstance(pid, int) for pid in result)

    def test_includes_init(self):
        """PID 1 should always be present on a Linux system."""
        result = pg.list_current_pids()
        assert 1 in result

    def test_handles_oserror_gracefully(self):
        """Should return empty set if /proc is unreadable."""
        with patch("os.listdir", side_effect=OSError("permission denied")):
            result = pg.list_current_pids()
            assert result == set()

    def test_filters_non_numeric_entries(self):
        """Should only include numeric entries (actual PIDs)."""
        with patch("os.listdir", return_value=["1", "2", "not-a-pid", "3", "self", "thread-self"]):
            result = pg.list_current_pids()
            assert result == {1, 2, 3}


# ---------------------------------------------------------------------------
# Tests: read_process_info
# ---------------------------------------------------------------------------

class TestReadProcessInfo:
    """Tests for read_process_info."""

    def test_returns_none_for_vanished_process(self):
        """Should return None if the process doesn't exist."""
        result = pg.read_process_info(999999)
        assert result is None

    def test_reads_own_process(self):
        """Should successfully read info about the current process."""
        import os
        pid = os.getpid()
        result = pg.read_process_info(pid)
        assert result is not None
        assert result.pid == pid
        assert result.name  # should have a name

    def test_handles_oserror_gracefully(self):
        """Should return None on any OSError."""
        with patch("pathlib.Path.read_text", side_effect=OSError):
            result = pg.read_process_info(999999)
            assert result is None


# ---------------------------------------------------------------------------
# Tests: detect_new_processes
# ---------------------------------------------------------------------------

class TestDetectNewProcesses:
    """Tests for detect_new_processes."""

    def test_detects_new_pids(self, sample_processes):
        """Should return only PIDs that are new."""
        previous = {1, 2, 3}
        current = {1, 2, 3, 100, 200}
        with patch("process_guardian.read_process_info") as mock_read:
            mock_read.side_effect = lambda pid: pg.ProcessInfo(pid=pid, name=f"proc-{pid}")
            result = pg.detect_new_processes(previous, current)
        assert len(result) == 2
        pids = {p.pid for p in result}
        assert pids == {100, 200}

    def test_returns_empty_when_no_new(self, sample_processes):
        """Should return empty list when nothing new."""
        with patch("process_guardian.read_process_info") as mock_read:
            result = pg.detect_new_processes(sample_processes, sample_processes)
        assert result == []
        mock_read.assert_not_called()

    def test_sorts_by_pid(self):
        """Results should be sorted by PID."""
        previous = set()
        current = {300, 100, 200}
        with patch("process_guardian.read_process_info") as mock_read:
            mock_read.side_effect = lambda pid: pg.ProcessInfo(pid=pid, name=f"proc-{pid}")
            result = pg.detect_new_processes(previous, current)
        pids = [p.pid for p in result]
        assert pids == [100, 200, 300]

    def test_skips_vanished_processes(self):
        """Processes that vanish during detection should be skipped."""
        previous = set()
        current = {100, 200, 300}

        def side_effect(pid):
            if pid == 200:
                return None  # vanished
            return pg.ProcessInfo(pid=pid, name=f"proc-{pid}")

        with patch("process_guardian.read_process_info", side_effect=side_effect):
            result = pg.detect_new_processes(previous, current)
        assert len(result) == 2
        assert 200 not in {p.pid for p in result}

    def test_none_current_pids_calls_list(self):
        """When current_pids is None, should call list_current_pids."""
        previous = {1, 2}
        current = {1, 2, 3}
        with patch("process_guardian.list_current_pids", return_value=current):
            with patch("process_guardian.read_process_info") as mock_read:
                mock_read.return_value = pg.ProcessInfo(pid=3, name="new-proc")
                result = pg.detect_new_processes(previous, None)
        assert len(result) == 1
        assert result[0].pid == 3


# ---------------------------------------------------------------------------
# Tests: lookup_desktop_file
# ---------------------------------------------------------------------------

class TestLookupDesktopFile:
    """Tests for lookup_desktop_file."""

    def test_returns_empty_string_when_not_found(self):
        """Should return empty string for unknown application."""
        result = pg.lookup_desktop_file("definitely-not-a-real-app-12345")
        assert result == ""

    def test_finds_system_app(self):
        """Should find a common system app if it exists."""
        # This test is lenient — it only checks the function doesn't crash
        # and returns a string (may be empty on minimal systems)
        result = pg.lookup_desktop_file("firefox")
        assert isinstance(result, str)

    def test_handles_missing_directories(self):
        """Should not crash if application directories don't exist."""
        with patch("pathlib.Path.is_dir", return_value=False):
            result = pg.lookup_desktop_file("anything")
            assert result == ""

    def test_parses_desktop_file_correctly(self, tmp_path):
        """Should correctly parse the Name= field from a .desktop file."""
        desktop_file = tmp_path / "test-app.desktop"
        desktop_file.write_text(
            "[Desktop Entry]\n"
            "Name=Test Application\n"
            "Exec=/usr/bin/test-app\n"
            "Type=Application\n"
        )
        # We need to patch the search paths to include our temp directory
        with patch.object(pg, "lookup_desktop_file", wraps=pg.lookup_desktop_file):
            # Manually test the parsing function
            result = pg._parse_desktop_name(desktop_file)
        assert result == "Test Application"


# ---------------------------------------------------------------------------
# Tests: lookup_unit_description
# ---------------------------------------------------------------------------

class TestLookupUnitDescription:
    """Tests for lookup_unit_description."""

    def test_returns_empty_for_nonexistent_unit(self):
        """Should return empty string for a unit that doesn't exist."""
        result = pg.lookup_unit_description("definitely-not-real-12345.service")
        assert result == ""

    def test_handles_subprocess_error(self):
        """Should return empty string on subprocess failure."""
        with patch("subprocess.run", side_effect=OSError("command not found")):
            result = pg.lookup_unit_description("anything.service")
            assert result == ""

    def test_handles_timeout(self):
        """Should return empty string on timeout."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            result = pg.lookup_unit_description("anything.service")
            assert result == ""


# ---------------------------------------------------------------------------
# Tests: get_process_description
# ---------------------------------------------------------------------------

class TestGetProcessDescription:
    """Tests for get_process_description."""

    def test_returns_desktop_file_name_first(self):
        """Should prefer .desktop file name over other sources."""
        proc = pg.ProcessInfo(pid=1, name="firefox")
        with patch("process_guardian.lookup_desktop_file", return_value="Firefox Web Browser"):
            with patch("process_guardian.lookup_unit_description", return_value="Firefox"):
                result = pg.get_process_description(proc)
        assert result == "Firefox Web Browser"

    def test_falls_back_to_unit_description(self):
        """Should use unit description if no .desktop file found."""
        proc = pg.ProcessInfo(pid=1, name="my-daemon")
        with patch("process_guardian.lookup_desktop_file", return_value=""):
            with patch("process_guardian.lookup_unit_description", return_value="My Daemon Service"):
                result = pg.get_process_description(proc)
        assert result == "My Daemon Service"

    def test_falls_back_to_process_name(self):
        """Should use process name if nothing else found."""
        proc = pg.ProcessInfo(pid=1, name="my-binary")
        with patch("process_guardian.lookup_desktop_file", return_value=""):
            with patch("process_guardian.lookup_unit_description", return_value=""):
                result = pg.get_process_description(proc)
        assert result == "my-binary"


# ---------------------------------------------------------------------------
# Tests: should_protect
# ---------------------------------------------------------------------------

class TestShouldProtect:
    """Tests for should_protect decision logic."""

    def test_protects_by_name(self, guardian_config):
        """Should protect processes whose name is in the protected list."""
        proc = pg.ProcessInfo(pid=1, name="firefox")
        protected, reason = pg.should_protect(proc, guardian_config)
        assert protected is True
        assert "name" in reason.lower() or "protected" in reason.lower()

    def test_protects_by_description(self, guardian_config):
        """Should protect processes whose description matches."""
        # Use a name NOT in protected_names, but with a protected description
        proc = pg.ProcessInfo(pid=1, name="my-browser", description="Web Browser")
        protected, reason = pg.should_protect(proc, guardian_config)
        assert protected is True
        assert "description" in reason.lower()

    def test_protects_critical_processes(self, guardian_config):
        """Should protect known critical system processes."""
        proc = pg.ProcessInfo(pid=1, name="hyprland", cgroup="/user.slice/session-2.scope")
        protected, reason = pg.should_protect(proc, guardian_config)
        assert protected is True
        assert "critical" in reason.lower()

    def test_protects_session_scope(self, guardian_config):
        """Should protect processes in session scope."""
        proc = pg.ProcessInfo(pid=1, name="some-app", cgroup="/user.slice/user-1000.slice/session-5.scope")
        protected, reason = pg.should_protect(proc, guardian_config)
        assert protected is True
        assert "session" in reason.lower()

    def test_does_not_protect_unknown(self, guardian_config):
        """Should NOT protect unknown processes by default."""
        proc = pg.ProcessInfo(pid=1, name="random-binary", cgroup="/user.slice/user-1000.slice/app-random.scope")
        protected, reason = pg.should_protect(proc, guardian_config)
        assert protected is False
        assert reason == ""

    def test_empty_config_protects_nothing_extra(self):
        """With empty config, only critical processes are protected."""
        config = pg.GuardianConfig()
        proc = pg.ProcessInfo(pid=1, name="unknown-app")
        protected, _ = pg.should_protect(proc, config)
        assert protected is False

    def test_compositor_protection(self, guardian_config):
        """Compositor should always be protected."""
        proc = pg.ProcessInfo(pid=1, name="Hyprland")
        protected, reason = pg.should_protect(proc, guardian_config)
        assert protected is True


# ---------------------------------------------------------------------------
# Tests: _is_critical_process
# ---------------------------------------------------------------------------

class TestIsCriticalProcess:
    """Tests for _is_critical_process."""

    @pytest.mark.parametrize("name", [
        "hyprland", "Hyprland", "sway", "gnome-shell", "kwin_x11", "kwin_wayland",
    ])
    def test_known_compositors(self, name):
        """All known compositor names should be critical."""
        proc = pg.ProcessInfo(pid=1, name=name)
        result, reason = pg._is_critical_process(proc)
        assert result is True

    def test_session_scope_is_critical(self):
        """Session scopes should be critical."""
        proc = pg.ProcessInfo(pid=1, name="anything", cgroup="/user.slice/session-2.scope")
        result, _ = pg._is_critical_process(proc)
        assert result is True

    def test_regular_app_is_not_critical(self):
        """Regular apps should not be critical."""
        proc = pg.ProcessInfo(pid=1, name="firefox", cgroup="/user.slice/app-firefox.scope")
        result, _ = pg._is_critical_process(proc)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: evaluate_new_processes
# ---------------------------------------------------------------------------

class TestEvaluateNewProcesses:
    """Tests for the full evaluation pipeline."""

    def test_full_pipeline(self, guardian_config):
        """Should detect, enrich, and classify new processes."""
        previous = {1, 2}
        current = {1, 2, 100, 200}

        def mock_read(pid):
            names = {100: "firefox", 200: "hyprland"}
            return pg.ProcessInfo(pid=pid, name=names.get(pid, f"proc-{pid}"))

        with patch("process_guardian.read_process_info", side_effect=mock_read):
            with patch("process_guardian.get_process_description", side_effect=lambda p: p.name):
                result = pg.evaluate_new_processes(previous, guardian_config, current)

        assert len(result) == 2
        firefox = next(p for p in result if p.name == "firefox")
        hyprland = next(p for p in result if p.name == "hyprland")
        assert firefox.is_protected is True
        assert hyprland.is_protected is True

    def test_empty_when_no_new_processes(self, guardian_config):
        """Should return empty list when no new processes."""
        pids = {1, 2, 3}
        result = pg.evaluate_new_processes(pids, guardian_config, pids)
        assert result == []

    def test_uses_default_config(self):
        """Should use default config when none provided."""
        previous = set()
        current = {100}
        with patch("process_guardian.read_process_info") as mock_read:
            mock_read.return_value = pg.ProcessInfo(pid=100, name="unknown")
            with patch("process_guardian.get_process_description", return_value="unknown"):
                result = pg.evaluate_new_processes(previous, None, current)
        assert len(result) == 1
        assert result[0].is_protected is False


# ---------------------------------------------------------------------------
# Tests: get_protected_cgroup_names
# ---------------------------------------------------------------------------

class TestGetProtectedCgroupNames:
    """Tests for get_protected_cgroup_names."""

    def test_extracts_cgroup_names(self):
        """Should extract cgroup names from protected processes."""
        processes = [
            pg.ProcessInfo(pid=1, name="firefox", cgroup="/user.slice/user-1000.slice/app-firefox.scope", is_protected=True),
            pg.ProcessInfo(pid=2, name="random", cgroup="/user.slice/user-1000.slice/app-random.scope", is_protected=False),
        ]
        result = pg.get_protected_cgroup_names(processes)
        assert "app-firefox.scope" in result
        assert "app-random.scope" not in result

    def test_empty_when_no_protected(self):
        """Should return empty set when no protected processes."""
        processes = [
            pg.ProcessInfo(pid=1, name="app1", cgroup="/user.slice/app1.scope", is_protected=False),
        ]
        result = pg.get_protected_cgroup_names(processes)
        assert result == set()

    def test_handles_empty_cgroup(self):
        """Should handle processes with empty cgroup gracefully."""
        processes = [
            pg.ProcessInfo(pid=1, name="app1", cgroup="", is_protected=True),
        ]
        result = pg.get_protected_cgroup_names(processes)
        assert result == set()

    def test_empty_list(self):
        """Should return empty set for empty list."""
        result = pg.get_protected_cgroup_names([])
        assert result == set()
