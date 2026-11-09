#!/usr/bin/env python3
"""Experimental process-discovery helpers; not active in the production daemon.

The daemon and QML panel do not import or call this module. It provides isolated
functions for a possible future Process Guardian, but new-process notifications
and dynamic user protection are not currently part of the running plugin. The
production daemon uses its config plus the built-in inviolable cgroups.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ProcessInfo:
    """One process discovered on the system."""

    pid: int
    name: str
    cmdline: str = ""
    cgroup: str = ""
    description: str = ""
    is_protected: bool = False
    protection_reason: str = ""


@dataclass
class GuardianConfig:
    """Configuration for the process guardian."""

    protected_names: set[str] = field(default_factory=set)
    protected_descriptions: set[str] = field(default_factory=set)
    scan_interval: float = 30.0
    max_processes: int = 500


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

def list_current_pids() -> set[int]:
    """Return the set of all current PIDs on the system."""
    pids: set[int] = set()
    try:
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                pids.add(int(entry))
    except OSError:
        pass
    return pids


def read_process_info(pid: int) -> ProcessInfo | None:
    """Read basic info about a process. Returns None if it vanished."""
    try:
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        cmdline = cmdline_path.read_text(errors="replace").replace("\x00", " ").strip()

        # Read cgroup
        cgroup = ""
        try:
            cgroup_text = Path(f"/proc/{pid}/cgroup").read_text(errors="replace")
            for line in cgroup_text.splitlines():
                if line.startswith("0::"):
                    cgroup = line.split(":", 2)[2]
                    break
        except OSError:
            pass

        # Derive name from cmdline
        name = cmdline.split()[0] if cmdline else Path(f"/proc/{pid}/comm").read_text(errors="replace").strip()
        name = Path(name).name  # strip path

        return ProcessInfo(pid=pid, name=name, cmdline=cmdline, cgroup=cgroup)
    except (OSError, IndexError):
        return None


def detect_new_processes(
    previous_pids: set[int],
    current_pids: set[int] | None = None,
) -> list[ProcessInfo]:
    """Find processes that appeared since the last scan.

    Returns a list of ProcessInfo for new PIDs, sorted by PID.
    """
    if current_pids is None:
        current_pids = list_current_pids()

    new_pids = current_pids - previous_pids
    processes: list[ProcessInfo] = []
    for pid in sorted(new_pids):
        info = read_process_info(pid)
        if info is not None:
            processes.append(info)
    return processes


# ---------------------------------------------------------------------------
# Description lookup
# ---------------------------------------------------------------------------

def lookup_desktop_file(name: str) -> str:
    """Search for a .desktop file matching the process name.

    Returns the Name= field value, or empty string if not found.
    """
    search_paths = [
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path.home() / ".local/share/applications",
    ]

    for base in search_paths:
        if not base.is_dir():
            continue
        # Try exact match first
        direct = base / f"{name}.desktop"
        if direct.is_file():
            return _parse_desktop_name(direct)
        # Try case-insensitive match
        try:
            for entry in base.iterdir():
                if entry.suffix == ".desktop" and entry.stem.lower() == name.lower():
                    return _parse_desktop_name(entry)
        except OSError:
            continue
    return ""


def _parse_desktop_name(path: Path) -> str:
    """Extract the Name= field from a .desktop file."""
    try:
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("Name="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def lookup_unit_description(name: str) -> str:
    """Look up a systemd unit description."""
    if not name.endswith(".service"):
        name = f"{name}.service"
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "--", name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return ""
        for line in result.stdout.splitlines():
            if line.startswith("Description="):
                value = line.split("=", 1)[1].strip()
                # systemctl returns the unit name as value when the unit doesn't exist
                if value and value != name:
                    return value
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def get_process_description(process: ProcessInfo) -> str:
    """Get the best available human-readable description for a process.

    Tries, in order:
    1. .desktop file lookup
    2. systemd unit description
    3. The process name itself
    """
    desc = lookup_desktop_file(process.name)
    if desc:
        return desc
    desc = lookup_unit_description(process.name)
    if desc:
        return desc
    return process.name


# ---------------------------------------------------------------------------
# Protection decision
# ---------------------------------------------------------------------------

def should_protect(process: ProcessInfo, config: GuardianConfig) -> tuple[bool, str]:
    """Decide whether a process should be protected from reclamation.

    Returns (True, reason) if protected, (False, "") otherwise.

    Protection rules:
    1. Process name is in the protected list
    2. Process description matches a protected description
    3. Process is a known system-critical service (compositor, shell, etc.)
    """
    # Rule 1: explicit name match
    if process.name in config.protected_names:
        return True, "explicitly protected by name"

    # Rule 2: description match
    if process.description in config.protected_descriptions:
        return True, "protected by description"

    # Rule 3: known critical processes
    critical = _is_critical_process(process)
    if critical[0]:
        return critical

    return False, ""


def _is_critical_process(process: ProcessInfo) -> tuple[bool, str]:
    """Check if a process is a known critical system component."""
    critical_names = {
        "hyprland", "sway", "wayland-compositor",
        "quickshell", "ram-keeper",
        "gnome-shell", "kwin_x11", "kwin_wayland",
        "Xorg", "Xwayland",
    }
    if process.name.lower() in {n.lower() for n in critical_names}:
        return True, "critical system component"

    # A real session scope is session-<digits>.scope. A name that merely
    # contains both words (app-minha-session.scope) is an ordinary app.
    parts = [p for p in process.cgroup.split("/") if p]
    if any(re.fullmatch(r"session-\d+\.scope", part) for part in parts):
        return True, "session scope"

    return False, ""


def evaluate_new_processes(
    previous_pids: set[int],
    config: GuardianConfig | None = None,
    current_pids: set[int] | None = None,
) -> list[ProcessInfo]:
    """Full pipeline: detect new processes, enrich with descriptions, decide protection.

    Returns a list of ProcessInfo with is_protected and protection_reason set.
    """
    if config is None:
        config = GuardianConfig()

    new_procs = detect_new_processes(previous_pids, current_pids)
    result: list[ProcessInfo] = []

    for proc in new_procs:
        proc.description = get_process_description(proc)
        protected, reason = should_protect(proc, config)
        proc.is_protected = protected
        proc.protection_reason = reason
        result.append(proc)

    return result[: config.max_processes]


# ---------------------------------------------------------------------------
# Integration with ram-keeper
# ---------------------------------------------------------------------------

def get_protected_cgroup_names(processes: list[ProcessInfo]) -> set[str]:
    """Extract cgroup names that should be protected from a list of processes.

    This bridges process_guardian and ram_keeper: the output can be merged
    into ram_keeper's protected set.
    """
    protected: set[str] = set()
    for proc in processes:
        if proc.is_protected and proc.cgroup:
            # Extract the cgroup name (last component)
            name = Path(proc.cgroup).name
            if name:
                protected.add(name)
    return protected
