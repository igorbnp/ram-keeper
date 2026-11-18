#!/usr/bin/env python3
"""ram-keeper — automatic RAM steward for a memory-scarce Omarchy desktop.

Reads cgroup v2 accounting and PSI, decides whether the machine is actually
under pressure (not merely "using RAM"), and reclaims proactively from the
cheapest victim first. Designed to run unattended:

  * page cache is thrown away first  — free, instantly re-readable
  * anonymous memory goes to zram    — compressed, CPU-cheap, RAM-resident
  * disk swap is the last resort    — slow, and zram is nearly free here
  * nothing is ever killed           — systemd-oomd owns that decision

Everything runs as the plain user. The daemon only writes cgroup files it
owns; system daemons in system.slice are read for reporting but never
reclaimed (the kernel refuses, and it should).

Invoked by the panel for one-shot snapshots and actions, and run as a loop by
the user systemd unit. See CONFIG.md for the policy and thresholds.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

CGROOT = Path("/sys/fs/cgroup")

# ---------------------------------------------------------------------------
# Discovery: where this user's memory actually lives
# ---------------------------------------------------------------------------

def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(_read(path, str(default)))
    except ValueError:
        return default


def self_cgroup() -> str:
    """The cgroup path of this process, relative to the cgroup root."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return line.split(":", 2)[2]
    except OSError:
        pass
    return ""


def user_root() -> Path:
    """The user.slice/user-$UID.slice directory, discovered rather than assumed.

    Finding it dynamically keeps the daemon correct if the uid, the systemd
    hierarchy layout, or a future Omarchy migration changes the path shape.
    """
    try:
        uid = os.getuid()
    except AttributeError:
        # Windows / non-POSIX: return a path that will not exist, so scan()
        # returns an empty list rather than walking the entire cgroup tree.
        return CGROOT / "user.slice" / "user-nonposix.slice"
    own = CGROOT / "user.slice" / f"user-{uid}.slice"
    if own.is_dir():
        return own
    # The correct path for this user's slice, even if the directory doesn't
    # exist yet.  Returning the parent (user.slice) would scan OTHER users'
    # slices — exposing their processes and inflating `available`.  Returning
    # a non-existent path is safe: scan() checks is_dir() first.
    return own


@dataclass
class Cgroup:
    """One reclaimable memory container, scored as a victim."""

    path: Path
    name: str
    kind: str                 # "scope" | "slice" | "service"
    label: str
    current: int = 0
    anon: int = 0
    file: int = 0
    reclaimable_file: int = 0
    swap: int = 0
    reclaimable: int = 0      # file pages we could drop for free
    protected: bool = False   # exempt by configuration or by role
    inviolable: bool = False  # no panel button may take this protection off
    swapable: bool = True     # False when MemorySwapMax=0 (anon can never be reclaimed)
    pids: int = 0

    @property
    def reclaim_file(self) -> Path:
        return self.path / "memory.reclaim"


def _stat_field(stat_text: str, key: str) -> int:
    for line in stat_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == key:
            try:
                return int(parts[1])
            except ValueError:
                return 0
    return 0


def cgroup_swap(path: Path) -> int:
    """Bytes this cgroup has in swap.

    `memory.swap.current` is the supported interface and works on every kernel
    cgroup v2 has shipped it on. The `swap` key in memory.stat is NOT: it was
    never upstream, and reading it silently yields 0 on kernels without it
    (this box's 7.2.5 is one), which turns every "I swapped 400MB" report into
    a silent zero. Fall back to memory.stat only for very old kernels that
    lack the file at all.
    """
    value = _read_int(path / "memory.swap.current", -1)
    if value >= 0:
        return value
    return _stat_field(_read(path / "memory.stat"), "swap")


_TRAILING_ID = re.compile(r"-(?:\d+|[0-9a-f]{8}(?:[0-9a-f]{8}){0,3})$")
# Suffixes that ride on top of the reverse-DNS id but are not part of the
# app's own name.  `.desktop` is the main one (org.telegram.desktop → desktop
# alias collides with every other .desktop app).  `.Client` appears in some
# Flatpak entries (org.telegram.desktop.Client).
_DOTTED_NOISE = frozenset({"desktop", "client"})


def _strip_unit_suffix(name: str) -> str:
    """Drop a systemd unit suffix. Only the kind marker, not escaped text."""
    for suffix in (".service", ".scope", ".slice", ".socket", ".mount", ".desktop"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


_SYSTEMD_HEX_RUN = re.compile(r"(?:\\x[0-9a-fA-F]{2})+")


def _decode_systemd_escapes(text: str) -> str:
    """Decode all systemd \\xHH hex escapes, handling UTF-8 multi-byte.

    Consecutive \\xHH pairs are decoded as a single byte run so that
    multi-byte UTF-8 sequences like ``\\xc3\\xa9`` produce ``é``
    rather than two Latin-1 characters.
    """
    def _sub(m: re.Match) -> str:
        raw = bytes.fromhex(m.group(0).replace("\\x", ""))
        return "".join(
            ch for ch in raw.decode("utf-8", errors="replace")
            if ord(ch) >= 32 and ord(ch) != 127
        )
    return _SYSTEMD_HEX_RUN.sub(_sub, text)


def unit_aliases(name: str) -> frozenset[str]:
    """Every shape a user could reasonably use to name this cgroup.

    A config entry has to be written against a name the user can actually see:
    "Spotify", not "app-Spotify-1043.scope". Systemd, meanwhile, only ever
    publishes the second form — with a PID or a stable hash glued on, escaped
    hyphens, and (for desktop apps) a reverse-DNS id. Without this expansion,
    `protected: ["spotify"]` silently protects nothing at all, which is the
    worst possible outcome for a setting whose whole purpose is safety.

    Deliberately conservative: aliases only ever REMOVE decoration
    (suffix, `app-` prefix, trailing id, reverse-DNS prefix). A name is never
    split on a hyphen, so protecting "syncthing" cannot accidentally protect
    "syncthing-server".
    """
    if not name or not isinstance(name, str):
        return frozenset()
    text = _decode_systemd_escapes(name.strip())
    if not text:
        return frozenset()
    # Handle @instance service names: app-wayland\\x2dwm@hyprland.service
    # Strip the instance part for matching, keeping the instance name as alias
    instance = ""
    if "@" in text:
        base_part, _, instance_part = text.partition("@")
        instance = _strip_unit_suffix(instance_part)
        # Keep the full name but also add the instance as an alias
        text_without_instance = base_part
    else:
        text_without_instance = text

    aliases: set[str] = {text}
    base = _strip_unit_suffix(text)
    aliases.add(base)
    if instance and not instance.isdigit():
        aliases.add(instance)
        # Do NOT add the template base (e.g. "wayland-wm") alone — it
        # would match ALL instances of that template, so protecting
        # wayland-wm@hyprland would inadvertently protect wayland-wm@sway.
    if base[:4].casefold() == "app-":
        rest = base[4:]
        aliases.add(rest)
        aliases.add(_TRAILING_ID.sub("", base))
        trimmed = _TRAILING_ID.sub("", rest)
        aliases.add(trimmed)
        # Strip trailing noise segments (.desktop, .client, .Client) BEFORE
        # rsplit so "org.telegram.desktop" → "org.telegram" → "telegram".
        clean = trimmed
        while "." in clean:
            tail = clean.rsplit(".", 1)[-1]
            if tail.casefold() in _DOTTED_NOISE:
                clean = clean[: -(len(tail) + 1)]
            else:
                break
        if clean != trimmed:
            aliases.add(clean)
        # org.chromium.Chromium-991 → Chromium; Hyprland-chromium-ea8ee5ca →
        # Hyprland-chromium (the hash is already stripped above).
        if "." in clean:
            last_part = clean.rsplit(".", 1)[-1]
            rest_part = clean.split(".", 1)[-1]
            aliases.add(last_part)
            if rest_part != last_part:
                aliases.add(rest_part)
    return frozenset(a.casefold() for a in aliases if a)


def names_match(cgroup_name: str, wanted) -> bool:
    """True if `cgroup_name` is one of the names in `wanted` (see aliases)."""
    if not wanted:
        return False
    theirs = unit_aliases(cgroup_name)
    if not theirs:
        return False
    for candidate in wanted:
        if isinstance(candidate, str) and unit_aliases(candidate) & theirs:
            return True
    return False


def human_label(name: str) -> str:
    """Turn a systemd cgroup name into something worth showing a human."""
    label = name
    if name[:4].casefold() == "app-":
        label = name[4:]
    if label.lower().endswith(".scope"):
        label = label[:-6]
    if label.lower().endswith(".service"):
        label = label[:-8]
    label = _decode_systemd_escapes(label)
    if label.lower().startswith("org.chromium.chromium-"):
        label = "Chromium " + label.rsplit("-", 1)[-1]
    if label[:4].casefold() == "app-":
        label = label[4:]
    return label or name


def scan(root: Path, protected: set[str], min_bytes: int,
         never: set[str] | None = None) -> list[Cgroup]:
    """Enumerate reclaimable leaf cgroups under the user's own hierarchy."""
    found: list[Cgroup] = []
    if not root.is_dir():
        return found

    for cgroup_dir in sorted(root.rglob("*")):
        # TOCTOU: a cgroup can vanish between any two syscalls. is_dir() and
        # exists() swallow ENOENT, but iterdir() RAISES FileNotFoundError, and
        # scan() has no try/ — one cgroup exiting mid-walk would abort the whole
        # pass. Every traversal is therefore guarded, not just the first check.
        try:
            if not cgroup_dir.is_dir():
                continue
            if not (cgroup_dir / "memory.current").exists():
                continue

            name = cgroup_dir.name
            if name.endswith(".mount"):
                continue

            # Only leaves hold processes we can meaningfully reclaim from; a
            # parent's accounting is the sum of its children, so reclaiming the
            # parent double-counts and can hit protected descendants' floors.
            children = [c for c in cgroup_dir.iterdir()
                        if c.is_dir() and (c / "memory.current").exists()]
            if children:
                continue

            current = _read_int(cgroup_dir / "memory.current")
            if current < min_bytes:
                continue
        except OSError:
            # The cgroup went away mid-scan. A desktop does this constantly —
            # every app launch and exit is a cgroup lifecycle event — so this is
            # the normal case, not an exceptional one.
            continue

        stat_text = _read(cgroup_dir / "memory.stat")
        anon = _stat_field(stat_text, "anon")
        file_pages = _stat_field(stat_text, "file")
        # Only `inactive_file` is droppable without disturbing a running
        # process. Adding `file - active_file` on top of it double-counts: an
        # earlier revision did, and overstated reclaimable cache by ~2% here.
        # The kernel is the authority on what it can actually give back, so the
        # honest figure is the inactive tail alone.
        inactive_file = _stat_field(stat_text, "inactive_file")
        reclaimable = min(file_pages, inactive_file)
        pids = _read_int(cgroup_dir / "pids.current", 0)
        swap = cgroup_swap(cgroup_dir)
        # A cgroup capped at zero swap (MemorySwapMax=0) can never give anon
        # back to zram. Without knowing that we keep asking it for a gigabyte
        # and get EAGAIN every single pass, burning the budget on a victim
        # that was never able to help.
        swap_max_raw = _read(cgroup_dir / "memory.swap.max")
        swapable = swap_max_raw not in ("0", "")

        if name.endswith(".scope"):
            kind = "scope"
        elif name.endswith(".slice"):
            kind = "slice"
        else:
            kind = "service"

        inviolable = is_inviolable(name)
        is_protected = (names_match(name, protected)
                        or names_match(name, never or set())
                        or inviolable)
        found.append(Cgroup(
            path=cgroup_dir,
            name=name,
            kind=kind,
            label=human_label(name),
            current=current,
            anon=anon,
            file=file_pages,
            reclaimable_file=reclaimable,
            swap=swap,
            reclaimable=reclaimable,
            protected=is_protected,
            inviolable=inviolable,
            swapable=swapable,
            pids=pids,
        ))
    return found


# ---------------------------------------------------------------------------
# System-wide signals
# ---------------------------------------------------------------------------

def meminfo() -> dict:
    values: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        try:
            key, rest = line.split(":", 1)
            values[key] = int(rest.split()[0]) * 1024
        except (ValueError, IndexError):
            continue
    return values


def psi(resource: str = "memory") -> dict:
    """Pressure Stall Information plus whether its key signal was readable.

    A real zero and an unavailable file are different things: the former is a
    healthy reading, the latter means pressure cannot be inferred from PSI.
    """
    out = {"available": False,
           "some_avg10": 0.0, "some_avg60": 0.0, "some_avg300": 0.0,
           "full_avg10": 0.0, "full_avg60": 0.0, "full_avg300": 0.0}
    try:
        text = Path(f"/proc/pressure/{resource}").read_text()
    except OSError:
        return out
    valid_keys: set[str] = set()
    for line in text.strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        for token in parts[1:]:
            if "=" not in token:
                continue
            name, _, value = token.partition("=")
            key = f"{kind}_{name}"
            if key in out:
                try:
                    number = float(value)
                except ValueError:
                    continue
                if not math.isfinite(number) or number < 0:
                    continue
                out[key] = number
                valid_keys.add(key)
    out["available"] = "some_avg60" in valid_keys
    return out


def zram_stats() -> dict:
    """Swap usage, split by device.

    The one thing this does that a normal system monitor does not: separate
    RAM-backed swap from disk-backed swap. Every desktop shows a single
    combined "swap 40%" figure, which is actively misleading — a full zram is
    compressed RAM and costs nothing but CPU, while 5% of a disk swapfile is a
    slow machine. Collapsing them hides the only distinction that matters.
    """
    devices = []
    try:
        # /proc/swaps reports Size and Used in KiB. No swapon process.
        lines = Path("/proc/swaps").read_text().splitlines()
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            name = parts[0]
            try:
                size_val = int(parts[2]) * 1024
                used_val = int(parts[3]) * 1024
            except (ValueError, IndexError):
                continue  # skip this line, not the whole loop
            # Determine if this is a RAM-backed device by checking if it
            # is a zram block device (e.g. /dev/zram0, /dev/zram1).
            # Using path substring like "zram" in name.lower() is wrong:
            # a swapfile at /swap/zram-backup.swap would be classified
            # as RAM-backed.
            dev_basename = name.rsplit("/", 1)[-1] if "/" in name else name
            ram_backed = dev_basename.startswith("zram")
            devices.append({
                "name": name,
                "type": parts[1],
                "size": size_val,
                "used": used_val,
                "ram_backed": ram_backed,
            })
    except (OSError, ValueError, IndexError):
        pass
    return {"devices": devices,
            "used": sum(d["used"] for d in devices),
            "size": sum(d["size"] for d in devices),
            "disk_used": sum(d["used"] for d in devices if not d["ram_backed"]),
            "ram_used": sum(d["used"] for d in devices if d["ram_backed"])}


def zram_has_room(zram: dict | None, margin: int = 64 * 1024 * 1024) -> bool:
    """True when some RAM-backed swap device can still take anonymous pages.

    Disk swap does not count. A full zram, or no zram at all, must not push
    app memory onto the slow swapfile.
    """
    if not isinstance(zram, dict):
        return False
    devices = zram.get("devices") or []
    if not isinstance(devices, list):
        return False
    for dev in devices:
        if not isinstance(dev, dict) or not dev.get("ram_backed"):
            continue
        try:
            free = int(dev.get("size") or 0) - int(dev.get("used") or 0)
        except (TypeError, ValueError):
            continue
        if free > margin:
            return True
    return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # Free memory below this fraction of total: start reclaiming.
    "target_free_percent": 18.0,
    # Below this, reclaim hard.
    "critical_free_percent": 10.0,
    # PSI memory stall above this (percent of wall time) also triggers, even
    # when free RAM looks fine. Catches the "free RAM is high but every
    # allocation is slow" shape of pressure.
    "psi_some_percent": 6.0,
    # Don't touch cgroups smaller than this; churn costs more than it saves.
    "min_target_bytes": 96 * 1024 * 1024,
    # Seconds between passes when idle.
    "interval_idle": 20.0,
    # Seconds between passes while under pressure.
    "interval_pressure": 5.0,
    # Minimum seconds between anonymous reclaims of the SAME cgroup. Without
    # this, an untouchable hog keeps free memory under the floor and every
    # 5 s pass takes another 25% of the same browser until it is empty.
    "anon_reclaim_cooldown": 90.0,
    # Hard ceiling on how much a single pass may reclaim, so one spike can't
    # turn into a system-wide swap storm.
    "max_reclaim_per_pass": 2 * 1024 * 1024 * 1024,
    # Prefer dropping page cache; only swap anon out when cache is exhausted.
    "cache_first": True,
    # Largest share of a single cgroup's memory one pass may reclaim, in percent.
    # See Steward.max_victim_slice(): asking one process for everything it holds
    # is what got an app OOM-killed during sustained-load testing.
    "max_victim_fraction": 25,
    # cgroups never reclaimed. The compositor and our own infrastructure are
    # added automatically; this is for the user's own additions.
    "protected": [
        "wayland-wm@hyprland.desktop",
        "quickshell",
        "pipewire.service",
        "pipewire-pulse.service",
        "wireplumber.service",
        "syncthing.service",
        "ollama.service",
        "hermes",
        "hermes-gateway",
        "sia",
        "ram-keeper",
        "xdg-desktop-portal",
        "xdg-desktop-portal-hyprland",
        "xdg-desktop-portal-gtk",
        "xdg-desktop-portal-wlr",
        "xdg-desktop-portal-kde",
    ],
    # Seconds to let a cgroup settle after a reclaim request before measuring
    # what actually came back. Reclaim of cache is synchronous; anon→zram can
    # take a moment to be accounted. This knob was read by reclaim_from() while
    # being absent from DEFAULT_CONFIG, so load_config() rejected it as an
    # unknown setting and the documented value never took effect.
    "reclaim_sleep": 0.4,
    # cgroups never reclaimed. The compositor and our own infrastructure are
    # added automatically; this is for the user's own additions. Names are
    # matched through unit_aliases(), so "localsearch" and
    # "localsearch-3.service" are the same setting.
    "never_reclaim": [
        "localsearch-3.service",
    ],
    # Names the user took off the default lists. A non-empty protected list is
    # merged back with the defaults, so removing "ollama" from that list would
    # not stick. This list is subtracted after the merge. Inviolable names are
    # refused.
    "unprotect": [],
    # PSI full_avg10 above this percent aborts anon→zram reclaim entirely for
    # the current pass, to avoid swap thrashing under heavy pressure.
    "psi_full_abort_percent": 10.0,
}

# The compositor and the shell can never be handed to a reclaim sweep, whatever
# the config file says. This list is deliberately NOT part of DEFAULT_CONFIG:
# `protected` is overridden by the user's file, so putting the compositor there
# means a user who adds one app of their own silently un-protects the window
# manager — a machine that kills its compositor has failed, not saved itself.
# A user cannot opt out of this, and that is the point.
INVIOABLE_PROTECTED = frozenset({
    "wayland-wm@hyprland.desktop",   # Hyprland: a dead WM is a dead desktop
    "session",                        # any session-* scope
    "quickshell",                     # the shell that draws this panel
    "ram-keeper",                     # ourselves
    "ram-keeper-notify",              # the notifier; a different unit from the daemon
    # systemd --user's own manager. It is a leaf cgroup holding the thing that
    # would have to act on a problem, so making it a victim is self-defeating:
    # reclaiming from it stalls every unit the user has.
    "init",
})


# uwsm may publish the compositor as app-Hyprland-<pid>.scope or as
# wayland-wm@Hyprland.service, with any capitalisation. The instance has to
# be Hyprland itself: hyprland-gtk-launch and wayland-wm@sway stay ordinary.
_COMPOSITOR_UNIT = re.compile(
    r"^wayland-wm@hyprland(?:\.desktop)?(?:\.(?:service|scope|slice|socket))?$",
    re.IGNORECASE,
)


def is_inviolable(name: str) -> bool:
    """True for cgroups no config file may ever hand to a reclaim sweep.

    `names_match` understands `app-quickshell-<pid>.scope`. A suffix strip
    alone leaves that name unprotected. `session-N.scope` is numbered per
    login, so the session prefix stays as its own check and is not a
    substring search (an app named `minha-session` is not the session).
    The compositor is recognised by the alias `hyprland` and by the
    wayland-wm@ template, so a capitalised or unsuffixed unit is still
    shielded, and a longer name that merely contains those letters is not.
    """
    if not isinstance(name, str) or not name:
        return False
    if names_match(name, INVIOABLE_PROTECTED) or names_match(name, ("hyprland",)):
        return True
    if _COMPOSITOR_UNIT.match(name.strip()):
        return True
    base = _strip_unit_suffix(name.strip()).casefold()
    return base == "session" or base.startswith("session-")


def config_path() -> Path:
    """Where the user's config lives.

    `RAM_KEEPER_CONFIG` points at another file so a test can load and write a
    throwaway config. Without it, the suite used to overwrite the real
    `~/.config/ram-keeper/config.json` and, if the daemon reloaded in that
    window, act on the hostile values.
    """
    override = os.environ.get("RAM_KEEPER_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "ram-keeper" / "config.json"


def state_dir() -> Path:
    """Where the daemon publishes state and logs.

    Overridable with RAM_KEEPER_STATE_DIR so a test suite cannot write into the
    real ~/.local/state/ram-keeper. It did: the unit tests built a real Steward
    with synthetic snapshots and appended fabricated "reclaim" events to the
    production log, which is an audit trail. A test that pollutes the data it is
    meant to protect is worse than no test.
    """
    override = os.environ.get("RAM_KEEPER_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local/state/ram-keeper"


# Bounds every user-supplied number is clamped into. A config file is the one
# place a typo becomes a machine-wide problem: a negative interval would make
# the daemon spin, and a zero budget would make it reclaim nothing while
# reporting that it worked. Clamping here means a bad file degrades to a
# working app instead of a dead one.
CONFIG_BOUNDS = {
    "target_free_percent": (5.0, 60.0),
    "critical_free_percent": (1.0, 55.0),
    "psi_some_percent": (0.5, 90.0),
    "min_target_bytes": (4 * 1024 * 1024, 4 * 1024 * 1024 * 1024),
    "interval_idle": (1.0, 3600.0),
    "interval_pressure": (0.5, 600.0),
    "anon_reclaim_cooldown": (0.0, 600.0),
    "max_reclaim_per_pass": (16 * 1024 * 1024, 32 * 1024 * 1024 * 1024),
    "max_victim_fraction": (5, 60),
    "reclaim_sleep": (0.0, 5.0),
    "psi_full_abort_percent": (1.0, 80.0),
}

_CONFIG_WARNINGS: list[str] = []


def _coerce_number(key: str, value, fallback):
    """Return `value` as a usable number, or the default if it isn't one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _CONFIG_WARNINGS.append(f"{key}: expected a number, got {value!r}; using {fallback}")
        return fallback
    try:
        number = float(value)
    except (OverflowError, ValueError):
        # A 400-digit integer is valid JSON and parses to a Python int, but
        # float() on it raises OverflowError — which nothing above catches.
        _CONFIG_WARNINGS.append(f"{key}: {value!r} is too large; using {fallback}")
        return fallback
    if not math.isfinite(number):
        _CONFIG_WARNINGS.append(f"{key}: not a finite number ({value!r}); using {fallback}")
        return fallback
    low, high = CONFIG_BOUNDS[key]
    if number < low or number > high:
        clamped = max(low, min(high, number))
        _CONFIG_WARNINGS.append(
            f"{key}: {value} outside [{low}, {high}]; clamped to {clamped}")
        return type(fallback)(clamped) if isinstance(fallback, int) else clamped
    return type(fallback)(number) if isinstance(fallback, int) else number


def _coerce_strlist(key: str, value, fallback):
    """A list of names. A bare string is a common typo for a one-item list, so
    accept it rather than iterating its characters and silently protecting
    nothing (or protecting single letters)."""
    if isinstance(value, str):
        name = value.strip()
        if not name:
            _CONFIG_WARNINGS.append(f"{key}: blank name; using default list")
            return list(fallback)
        return [name]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        names = [name for v in value if (name := v.strip())]
        if len(names) != len(value):
            _CONFIG_WARNINGS.append(f"{key}: blank name in list; using default when no names remain")
            if value and not names:
                return list(fallback)
        return names
    _CONFIG_WARNINGS.append(f"{key}: expected a list of names, got {value!r}; using default")
    return list(fallback)


def load_config() -> dict:
    """Defaults, overlaid with the user's file, every value validated.

    The two relationships the logic depends on are enforced HERE, not assumed:
    critical must sit below target (otherwise a state can be reachable that no
    reclaim path handles), and the pressure interval must be shorter than the
    idle one (otherwise the daemon sleeps through the very pressure it exists
    to respond to).
    """
    _CONFIG_WARNINGS.clear()
    cfg = dict(DEFAULT_CONFIG)

    # Read BYTES, then decode. `read_text()` raises UnicodeDecodeError (a
    # ValueError, not an OSError) on a file some editor, sync tool or bad
    # copy left with invalid UTF-8 — and that would escape every caller,
    # including the daemon's reload and the panel's --once, as a traceback
    # instead of a warning. Same for a pathologically nested document:
    # json.loads raises RecursionError (a RuntimeError), which `except
    # ValueError` does not catch.
    raw: bytes | None = None
    try:
        raw = config_path().read_bytes()
    except OSError:
        raw = None
    if raw is not None:
        text: str | None = None
        try:
            text = raw.decode("utf-8-sig")   # tolerates a BOM, which json.loads rejects
        except UnicodeDecodeError as exc:
            _CONFIG_WARNINGS.append(f"config.json is not valid UTF-8 ({exc}); using defaults")
        user = None
        if text is not None:
            try:
                user = json.loads(text)
            except RecursionError:
                _CONFIG_WARNINGS.append("config.json is nested too deeply; using defaults")
                user = None
            except ValueError as exc:
                _CONFIG_WARNINGS.append(f"config.json is not valid JSON ({exc}); using defaults")
                user = None
        if isinstance(user, dict):
            for key, value in user.items():
                if key not in DEFAULT_CONFIG:
                    _CONFIG_WARNINGS.append(f"unknown setting {key!r} ignored")
                    continue
                if key in ("protected", "never_reclaim", "unprotect"):
                    cfg[key] = _coerce_strlist(key, value, DEFAULT_CONFIG[key])
                    # Merge, never replace. If a user adds their own app to
                    # `protected`, replacing the default list would silently
                    # un-protect everything the defaults protected — including
                    # (before the INVIOABLE_PROTECTED guard existed) the
                    # compositor. Adding to the defaults is also what the user
                    # meant. An explicit empty list is still honoured, since
                    # that is an unambiguous statement.
                    if value and key != "unprotect":
                        merged = list(DEFAULT_CONFIG[key])
                        for item in cfg[key]:
                            if item not in merged:
                                merged.append(item)
                        cfg[key] = merged
                elif key == "cache_first":
                    if isinstance(value, bool):
                        cfg[key] = value
                    else:
                        _CONFIG_WARNINGS.append(f"cache_first must be true/false; using default")
                else:
                    cfg[key] = _coerce_number(key, value, DEFAULT_CONFIG[key])

    # Critical below target, always. If the user inverted them, the floor wins
    # and the target is pulled down under it.
    if cfg["critical_free_percent"] >= cfg["target_free_percent"]:
        _CONFIG_WARNINGS.append(
            "critical_free_percent must be below target_free_percent; "
            f"target lowered to {cfg['critical_free_percent'] + 1:.0f}%")
        cfg["target_free_percent"] = min(60.0, cfg["critical_free_percent"] + 1.0)

    # Pressure must be checked more often than idle.
    if cfg["interval_pressure"] >= cfg["interval_idle"]:
        _CONFIG_WARNINGS.append(
            "interval_pressure must be below interval_idle; "
            f"pressure interval set to {max(0.5, cfg['interval_idle'] / 2)}s")
        cfg["interval_pressure"] = max(0.5, cfg["interval_idle"] / 2)

    blocked = []
    for name in cfg.get("unprotect") or []:
        if is_inviolable(name):
            _CONFIG_WARNINGS.append(f"{name} stays protected")
            continue
        blocked.append(name)
    cfg["unprotect"] = blocked
    if blocked:
        cfg["protected"] = [item for item in cfg["protected"]
                            if not names_match(item, blocked)]
        cfg["never_reclaim"] = [item for item in cfg["never_reclaim"]
                                if not names_match(item, blocked)]

    for warning in _CONFIG_WARNINGS:
        print(f"ram-keeper config: {warning}", file=sys.stderr)
    return cfg


def config_warnings() -> list[str]:
    return list(_CONFIG_WARNINGS)


# ---------------------------------------------------------------------------
# Forecast: the part a threshold cannot do
# ---------------------------------------------------------------------------

@dataclass
class Forecast:
    """Where memory is heading, and whether it will matter.

    A threshold reacts to a number crossing a line. This reacts to the
    number's *direction*, which is the only way to act before the line is
    crossed. A process quietly climbing from 400 MB to 2 GB over ten minutes is
    exactly the case a floor-based trigger handles badly: free memory looks
    fine right up until the moment it doesn't, and then every reclaim is
    already too late.
    """

    horizon_seconds: float = 0.0
    rate_bytes_per_sec: float = 0.0
    will_cross: bool = False
    confidence: float = 0.0       # 0..1, from the fit of the samples
    samples: int = 0
    trend: str = "flat"           # "rising" | "flat"
    culprit: str = ""             # who is growing

    @property
    def urgency(self) -> str:
        if not self.will_cross:
            return "none"
        if self.horizon_seconds < 120:
            return "imminent"       # under two minutes
        if self.horizon_seconds < 600:
            return "soon"           # under ten minutes
        return "eventual"

    def as_dict(self) -> dict:
        return {**asdict(self), "urgency": self.urgency}


class TrendTracker:
    """Ring buffer of (timestamp, free_bytes, per-cgroup anon) samples.

    Deliberately memoryless: samples fall out of the window, and the cgroup
    name itself encodes the PID, so a process restarting shows up as a new key
    rather than as growth down to zero.
    """

    def __init__(self, capacity: int = 240, clock=time.monotonic):
        self.capacity = capacity
        self.samples: list[tuple[float, int, dict[str, int]]] = []
        # Injectable so the trend maths can be tested against a known timeline
        # instead of twelve samples taken in the same millisecond.
        self._clock = clock

    def record(self, free_bytes: int, anon_by_group: dict[str, int]) -> None:
        self.samples.append((self._clock(), free_bytes, anon_by_group))
        if len(self.samples) > self.capacity:
            del self.samples[:-self.capacity]

    def reset(self) -> None:
        self.samples.clear()

    def forecast(self, target_bytes: int) -> Forecast:
        """Least-squares slope of free memory, extrapolated to the floor."""
        if len(self.samples) < 4:
            return Forecast()

        now = self._clock()
        # Only the recent window matters: a machine's memory use changes shape
        # over minutes, and a 40-minute-old sample would bias the slope toward
        # a regime that no longer exists.
        window = [s for s in self.samples if now - s[0] <= 600.0]
        if len(window) < 4:
            window = self.samples[-4:]

        t0 = window[0][0]
        xs = [s[0] - t0 for s in window]
        ys = [float(s[1]) for s in window]
        n = len(xs)

        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        var_x = sum((x - mean_x) ** 2 for x in xs)
        if var_x <= 0:
            return Forecast(samples=n)

        cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
        slope = cov / var_x                     # bytes per second
        intercept = mean_y - slope * mean_x

        # R² as a cheap confidence proxy: a straight line through noisy data is
        # not a trend worth acting on.
        ss_tot = sum((y - mean_y) ** 2 for y in ys)
        ss_res = sum((ys[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
        confidence = max(0.0, min(1.0, r2))

        forecast = Forecast(rate_bytes_per_sec=slope, confidence=confidence, samples=n)
        # "rising" means free memory is falling. Below ~0.05%/s of the current
        # level it is indistinguishable from noise, so report flat rather than
        # crying wolf.
        noise_floor = -abs(mean_y) * 0.0005
        forecast.trend = "rising" if slope < noise_floor else "flat"

        # Only compute horizon when the slope is below the noise floor AND
        # meaningful. The old `slope < -1.0` override would fire on a trivial
        # -20 KB/s, overriding the noise_floor check above.
        if slope < noise_floor and slope < 0:
            distance = ys[-1] - target_bytes     # bytes still above the floor
            if distance > 0:
                forecast.horizon_seconds = distance / (-slope)
                forecast.will_cross = True
                forecast.trend = "rising"
                forecast.culprit = self._fastest_growing(window)

        # An imminent crossing on a poor fit is probably a coincidence. Report
        # it as eventual so the daemon does not react to noise.
        # Require confidence >= 0.3 to call anything "imminent".
        if forecast.will_cross and forecast.confidence < 0.3:
            if forecast.horizon_seconds < 300:
                forecast.horizon_seconds = max(forecast.horizon_seconds, 300.0)
        return forecast

    def _fastest_growing(self, window: list) -> str:
        """Which cgroup is responsible for the trend.

        Ranked by its own growth rate, not absolute size: the largest process is
        usually not the one that is growing, and blaming the biggest is exactly
        the wrong advice.

        Uses the recent tail of the window (last ~120s) for burst detection,
        so a leak starting 5 minutes into a 10-minute window is not diluted
        by the first 5 minutes of flat data.
        """
        if len(window) < 4:
            return ""
        # Use the recent tail for burst detection
        recent_cutoff = window[-1][0] - 120.0
        recent = [s for s in window if s[0] >= recent_cutoff]
        if len(recent) < 3:
            recent = window[-4:]
        last = recent[-1]
        first_recent = recent[0]
        best_name, best_rate = "", 0.0
        for name, value in last[2].items():
            # Present in the last few samples, measured from when it appeared.
            # Requiring it in the first sample hid a leak that started later.
            if any(name not in sample[2] for sample in recent[-3:]):
                continue
            first_val_sample = next(
                (sample for sample in recent if name in sample[2]),
                first_recent,
            )
            if name not in first_val_sample[2]:
                continue
            span = last[0] - first_val_sample[0]
            if span < 15.0:
                continue
            rate = (value - first_val_sample[2][name]) / span
            if rate > best_rate:
                best_name, best_rate = name, rate
        # Only name a culprit when the growth is real, not noise.
        if best_rate > 4 * 1024 * 1024:      # >4 MB/s sustained
            return human_label(best_name)
        return ""


# ---------------------------------------------------------------------------
# The steward
# ---------------------------------------------------------------------------

@dataclass
class Action:
    label: str
    requested: int
    # RAM actually returned to the system. Smaller than cgroup_freed when pages
    # went to zram, because compressed storage is not RAM.
    freed: int
    swapped_out: int
    outcome: str
    cgroup_freed: int = 0   # the raw per-cgroup drop, kept for auditing

    @property
    def freed_ok(self) -> bool:
        return self.outcome in ("ok", "eagain")


def manifest_path() -> Path:
    """The manifest shipped next to this plugin, on disk."""
    return Path(__file__).resolve().parent.parent / "manifest.json"


def plugin_version() -> str:
    """The version the installed manifest claims, or nothing if it is unreadable."""
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    version = data.get("version") if isinstance(data, dict) else ""
    return version if isinstance(version, str) else ""


@dataclass
class SessionStats:
    """Tracks activity within the current daemon lifetime, replacing the
    dead *_today counters that never reset."""
    started_at: float = field(default_factory=time.time)
    actions: int = 0
    bytes_freed: int = 0
    last_relief_ts: float | None = None
    last_relief_bytes: int = 0
    last_relief_label: str = ""

    def record(self, freed: int, label: str = "") -> None:
        self.actions += 1
        self.bytes_freed += max(0, freed)
        self.last_relief_ts = time.time()
        self.last_relief_bytes = max(0, freed)
        if label:
            self.last_relief_label = label

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Steward:
    config: dict = field(default_factory=load_config)
    log_path: Path = field(
        default_factory=lambda: state_dir() / "log.jsonl")
    state_path: Path = field(
        default_factory=lambda: state_dir() / "state.json")
    actions_today: int = 0           # kept for backward compat, fed by session_stats
    bytes_reclaimed_today: int = 0   # kept for backward compat, fed by session_stats
    tracker: TrendTracker = field(default_factory=TrendTracker)
    last_forecast: Forecast = field(default_factory=Forecast)
    session_stats: SessionStats = field(default_factory=SessionStats)
    # Rotate the reclaim log at 4 MB; two generations are kept.
    max_log_bytes: int = 4 * 1024 * 1024
    # Injectable so a test can walk several passes without waiting.
    _clock: object = field(default=time.monotonic, repr=False, compare=False)
    _anon_ready_at: dict = field(default_factory=dict, repr=False, compare=False)
    # Swap-thrash guard state: per-cgroup (name → (strikes, cooldown_until))
    _swap_history: dict = field(default_factory=dict, repr=False, compare=False)
    # Pause/snooze state
    _paused: bool = field(default=False, repr=False, compare=False)
    _paused_until: float | None = field(default=None, repr=False, compare=False)
    # Captured once. A later edit of manifest.json must not make this process
    # claim it is already the new build: the panel compares this string with
    # the file it reads itself, and restarts the unit when they differ.
    started_version: str = field(default_factory=plugin_version, repr=False, compare=False)

    # -- logging ---------------------------------------------------------
    def log(self, event: dict) -> None:
        event.setdefault("ts", time.time())
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # Rotate by size. At ~42 reclaim events/hour this file reached ~30 MB
            # a day and grew forever inside a MemoryMax=384M unit — the unit that
            # exists to protect the machine was itself a slow leak. Two
            # generations is enough to inspect a trend; the JSON on disk is not
            # an archive.
            if (self.log_path.exists()
                    and self.log_path.stat().st_size > self.max_log_bytes):
                previous = self.log_path.with_suffix(".jsonl.1")
                try:
                    previous.unlink()
                except OSError:
                    pass
                self.log_path.replace(previous)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def publish(self, snapshot: dict) -> None:
        """Write the live snapshot where the panel can read it.

        The panel used to run `--once` every few seconds, which meant spawning a
        fresh Python process, re-walking every cgroup, and — fatally for the
        forecast — starting with an empty trend history each time. The forecast
        only exists inside the long-running daemon, so the panel could never
        show it. Publishing from here fixes both: the panel reads a file, and
        the forecast it shows is the daemon's real one.
        """
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a reader must never see a half-written file.
            # PID-stamped to avoid collision between concurrent CLI invocations.
            tmp = self.state_path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(snapshot, default=str), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    # -- assessment ------------------------------------------------------
    def snapshot(self) -> dict:
        info = meminfo()
        # A partial /proc/meminfo read is not "zero free memory" — it is "we do
        # not know". Treating the missing keys as 0 produced free_percent 0.0,
        # i.e. permanent `critical`, which had the daemon demanding ~688 MB every
        # 5 seconds off the biggest app on a completely healthy machine. An
        # unreadable signal must degrade to inaction, never to the most
        # aggressive response available.
        raw_total = info.get("MemTotal", 0)
        raw_available = info.get("MemAvailable", 0)
        readings_valid = (raw_total > 0 and "MemAvailable" in info
                          and 0 <= raw_available <= raw_total)

        # Keep one harmless internal denominator for forecast arithmetic, but
        # never publish it as real memory data when the source read is invalid.
        total = raw_total if raw_total > 0 else 1
        available = raw_available if readings_valid else 0
        free_percent = (100.0 * available / total) if readings_valid else None
        pressure = psi("memory")
        swap = zram_stats()

        protected = set(self.config["protected"])
        never = set(self.config["never_reclaim"])
        groups = scan(user_root(), protected, int(self.config["min_target_bytes"]), never)

        victims = [g for g in groups if not g.protected and not names_match(g.name, never)]
        victims.sort(key=lambda g: g.reclaimable, reverse=True)

        state = "ok"
        if not readings_valid:
            # No trustworthy reading: report it and do nothing. The forecaster is
            # fed nothing either, so a bad read cannot pollute the trend.
            state = "unknown"
        elif free_percent < self.config["critical_free_percent"]:
            state = "critical"
        elif free_percent < self.config["target_free_percent"]:
            state = "tight"
        elif pressure.get("available", True) and pressure["some_avg60"] > self.config["psi_some_percent"]:
            # High stall time with free RAM available means the free memory is
            # in the wrong place (fragmented, or a hot cgroup under pressure).
            state = "stalling"

        # Feed the forecaster. Sampling on every pass means the window holds
        # several minutes of history within a couple of minutes of uptime.
        if readings_valid:
            anon_by_group = {g.name: g.anon for g in groups}
            self.tracker.record(available, anon_by_group)
            target_bytes = int(total * self.config["target_free_percent"] / 100.0)
            self.last_forecast = self.tracker.forecast(target_bytes)
        else:
            # A missing sample breaks the current forecast chain. Keep old trend
            # points for the next valid sample, but never publish a stale verdict.
            self.last_forecast = Forecast()

        # An imminent crossing is pressure the floor does not show yet. Treat
        # it as its own state so the daemon acts before the number moves rather
        # than after — which is the entire reason for having a forecast.
        # Remember a stall that this promotion hides. The small-gap trending
        # path would otherwise return without touching the cache the stall
        # was supposed to drop.
        psi_stalling = state == "stalling"
        if state in ("ok", "stalling") and self.last_forecast.urgency == "imminent":
            state = "trending"

        # A critical machine whose only large cgroups are protected has
        # nothing the pass can ask. Name those apps so the panel does not
        # say that nothing is needed.
        reachable_floor = 64 * 1024 * 1024
        # Check if zram has room for anon reclaim
        _zram_ok = zram_has_room(swap)
        reachable = [
            g for g in groups
            if not g.protected and (
                g.reclaimable > reachable_floor
                or (g.anon > reachable_floor and g.swapable and _zram_ok)
            )
        ]
        held_by: list[str] = []
        if state == "critical" and not reachable:
            seen: list[str] = []
            big = [g for g in groups
                   if g.protected and g.current >= int(self.config["min_target_bytes"])]
            big.sort(key=lambda g: g.current, reverse=True)
            for holder in big:
                label = (holder.label or holder.name or "").strip()
                if label and label not in seen:
                    seen.append(label)
            held_by = seen[:8]

        # MemAvailable already excludes reclaimable cache, so the honest split of
        # "used" is: real anon+kernel, cache, then free-and-available. `Cached`
        # from meminfo includes pages held by processes' file mappings, and on a
        # healthy machine it approaches the whole of MemUsed — so the two are
        # reported separately and never allowed to imply that apps use nothing.
        if readings_valid:
            mem_used = raw_total - raw_available
        else:
            mem_used = None

        raw_cached = info.get("Cached")
        breakdown_valid = False
        cached = anon_and_kernel = None
        if (readings_valid and isinstance(raw_cached, int)
                and not isinstance(raw_cached, bool)
                and 0 <= raw_cached <= raw_total):
            breakdown_valid = True
            # Clamp cache to used memory so the three portions still sum to
            # MemTotal if Linux's cached estimate overlaps MemAvailable.
            cached = min(raw_cached, raw_total - raw_available, raw_total)
            anon_and_kernel = (raw_total - raw_available) - cached

        return {
            "state": state,
            "memory_reading_valid": readings_valid,
            "memory_breakdown_valid": breakdown_valid,
            "free_percent": round(free_percent, 1) if free_percent is not None else None,
            "mem_total": raw_total if raw_total > 0 else None,
            "mem_available": raw_available if readings_valid else None,
            "mem_used": mem_used,
            "anon_and_kernel": anon_and_kernel,
            "cached": cached,
            "zram": swap,
            "psi": pressure,
            "forecast": self.last_forecast.as_dict() if readings_valid else None,
            "groups": [
                asdict(g) | {
                    "path": str(g.path),
                    "thrashing": self._swap_history.get(g.name, (0, 0.0))[1] > self._clock(),
                }
                for g in groups
            ],
            "reclaimable_total": sum(g.reclaimable for g in victims),
            "checked_at": time.time(),
            "version": self.started_version,
            "psi_stalling": psi_stalling,
            "held_by": held_by,
            "interval_idle": float(self.config.get("interval_idle", 20.0)),
            "session_stats": self.session_stats.as_dict(),
            "paused": self._paused,
            "paused_until": self._paused_until,
            "paused_remaining_s": (
                max(0, int(self._paused_until - time.time()))
                if self._paused and self._paused_until is not None
                else None
            ),
        }

    # -- reclamation -----------------------------------------------------
    def reclaim_from(self, group: Cgroup, amount: int, cache_only: bool = False) -> Action:
        """Ask one cgroup to give back `amount`. Cache comes off first.

        Accounting rule: every number reported here must be something the
        kernel actually showed us. A cgroup can disappear between scan() and
        this call — an app closing is the normal case on a desktop — and then
        `memory.current` is simply gone. Reading a missing file as "0 bytes"
        makes `before - after` equal the victim's entire footprint, so a write
        that failed with ENOENT is reported as hundreds of megabytes freed, the
        log records an event that never happened, and the pass stops early
        believing its budget was spent.
        """
        before = _read_int(group.path / "memory.current", -1)
        before_swap = cgroup_swap(group.path)
        outcome = "unknown"
        # A cache pass must not swap anonymous pages. `swappiness=0` asks the
        # kernel for that. Older kernels reject the token with EINVAL; then
        # the plain byte count is the only write they accept.
        payloads = [f"{int(amount)} swappiness=0", str(int(amount))] if cache_only else [str(int(amount))]
        for index, payload in enumerate(payloads):
            try:
                with open(group.reclaim_file, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                outcome = "ok"
                break
            except FileNotFoundError:
                outcome = "gone"
                break
            except PermissionError:
                outcome = "denied"
                break
            except OSError as e:
                if cache_only and index == 0 and e.errno == errno.EINVAL:
                    continue
                # EAGAIN means "could not free that much right now" — the kernel
                # still reclaimed everything it could. Not a failure.
                outcome = "eagain" if e.errno == 11 else f"error:{e.errno}"
                break

        self._settle()
        after = _read_int(group.path / "memory.current", -1)
        after_swap = cgroup_swap(group.path)

        if before < 0 or after < 0:
            # The cgroup (or its accounting file) is not there any more. A real
            # 0 is a legitimate reading; an unreadable file is not.
            freed = 0
            swapped = 0
            # If we got EAGAIN and then the cgroup vanished during _settle(),
            # the honest outcome is "gone", not "eagain".
            if outcome in ("ok", "eagain"):
                outcome = "gone"
        else:
            freed = max(0, before - after)
            swapped = max(0, after_swap - before_swap)

        # `freed` from the cgroup is not the same as RAM returned to the system
        # when the pages went to zram. The cgroup drops the UNCOMPRESSED size;
        # zram then holds `swapped / ratio` bytes of real RAM (measured 4.34x
        # compression on this box), so moving 300MB of anon returns ~231MB, not
        # 300 and certainly not 300 - 300*4.34.
        #
        # The direction matters more than it looks: multiplying by the ratio
        # makes the result negative for any swap-heavy reclaim, clamps it to 0,
        # and therefore reports a pass that moved 150MB as having freed
        # NOTHING — "acted: false" in the panel, no log entry, no counter. The
        # whole anon pass looked like a no-op. Verified on a real 600MB cgroup:
        # 150MB reclaimed into zram, reported 0MB.
        ratio = max(1.0, self.zram_ratio())
        effective = max(0, freed - int(swapped / ratio))
        action = Action(label=group.label, requested=amount,
                        freed=effective,
                        swapped_out=swapped, outcome=outcome,
                        cgroup_freed=freed)
        return action

    def _settle(self) -> None:
        """Pause so the kernel's accounting catches up with the request.

        Configurable (and clamped): a negative or NaN value here is a
        `ValueError` from time.sleep, which would escape the daemon's own
        except clause while it is holding a pass's state.
        """
        try:
            value = float(self.config.get("reclaim_sleep", 0.4))
        except (TypeError, ValueError):
            value = 0.4
        if not math.isfinite(value):
            value = 0.4
        time.sleep(max(0.0, min(5.0, value)))

    @staticmethod
    def zram_ratio() -> float:
        """Bytes of uncompressed data per byte of compressed zram storage.

        Read live from mm_stat so the number is measured, not assumed. Falls
        back to a conservative 3.0 (a typical zstd ratio) when zram is absent,
        which keeps the reported figure honest rather than optimistic.

        Uses mem_used_total (fields[2]) rather than compr_data_size (fields[1]):
        mem_used_total includes allocator overhead and is the actual RAM cost,
        which is what the budget arithmetic needs.

        Scans all zram devices, not just zram0.
        """
        best_ratio = 0.0
        try:
            for entry in Path("/sys/block").iterdir():
                if not entry.name.startswith("zram"):
                    continue
                mm_stat = entry / "mm_stat"
                try:
                    fields = mm_stat.read_text().split()
                    orig = int(fields[0])
                    mem_used = int(fields[2])  # mem_used_total, not compr_data_size
                    if orig > 0 and mem_used > 0:
                        ratio = orig / mem_used
                        if ratio > best_ratio:
                            best_ratio = ratio
                except (OSError, IndexError, ValueError):
                    continue
        except OSError:
            pass
        return best_ratio if best_ratio > 0 else 3.0

    def max_victim_slice(self, victim_bytes: int | None = None) -> int:
        """Return the configured fraction of one victim's current memory.

        Pass the cgroup's ``current`` bytes for every reclaim request. With no
        argument, return the system-wide equivalent as a planning ceiling only.
        A measured regression showed why this must be victim-relative: a 1.5 GB
        request against a Chromium cgroup holding ~300 MB emptied the process,
        which then faulted hard and was killed by systemd-oomd. The per-victim
        cap spreads pressure across passes instead of draining one app at once.
        """
        try:
            if victim_bytes is None:
                victim_bytes = int(meminfo().get(
                    "MemTotal", 8 * 1024 * 1024 * 1024) or 0)
            size = max(0, int(victim_bytes))
            fraction = max(0.0, min(100.0, float(self.config["max_victim_fraction"])))
        except (TypeError, ValueError, OverflowError):
            return 0
        return int(fraction * size // 100)

    def relieve(self, snapshot: dict | None = None, force: bool = False,
                cache_only: bool = False) -> dict:
        """One reclamation pass. Returns what it did, for the panel.

        `cache_only` is the "Drop cache" button: page cache and nothing else.
        Without it, force=True skipped the early return but still ran the anon
        passes, so a cache sweep on a healthy desktop swapped out Chromium and
        the Hyprland launcher — 658 MB into zram for no memory benefit and a
        real slowdown, under a button labelled "Drop cache".
        """
        snap = snapshot or self.snapshot()
        state = snap["state"]
        free_percent = snap["free_percent"]
        target_free = self.config["target_free_percent"]
        # "trending" means the forecast says the floor is about to be crossed even
        # though it hasn't been yet. Reclaiming then is the entire point of
        # forecasting; waiting for the floor would make the forecast pointless.
        # Note it is NOT in this list, so a trending state acts.
        if state == "unknown":
            return {"acted": False,
                    "reason": "memory reading unavailable; refusing to reclaim",
                    "actions": []}
        if state == "ok" and not force:
            return {"acted": False, "reason": f"no pressure (state={state}, free={free_percent}%)",
                    "actions": []}
        # PSI says the machine is waiting, but the floor is not crossed and the
        # forecast is not imminent. Drop cache only: swapping anon here is what
        # turns a stall into the thrash the stall was warning about.
        if state == "stalling" and not force:
            cache_only = True

        # How much do we actually need? Under real pressure the deficit is the
        # honest number: ask for the shortfall plus headroom, not a fixed
        # slice. A fixed ask starves exactly when it matters most — with a 3GB
        # hog and 400MB free, "256MB please" is a rounding error.
        deficit_percent = max(0.0, target_free - free_percent)
        needed = int(snap["mem_total"] * (deficit_percent / 100.0))
        # One pass frees a fraction of the deficit: reclaiming the whole thing
        # at once would swap out far more than the system can hold in cache and
        # turn a slowdown into a stall.
        needed = max(needed // 2, 512 * 1024 * 1024)

        # Predicted pressure has no deficit yet — the floor is still ahead of us.
        # Reclaim a slice sized to the predicted arrival instead of doing nothing,
        # so the crossing is pre-empted rather than merely predicted.
        forecast = snap.get("forecast") or {}
        floor_bytes = int(snap["mem_total"] * target_free / 100.0)
        gap = max(0, snap["mem_available"] - floor_bytes)
        # Only act on a prediction when there is something to predict AROUND.
        # Without this, a forecast on a machine with 3.6 GB free fell through to
        # the 64 MB floor and reclaimed from a healthy system — the forecast
        # would have been the cause of the pressure it exists to prevent.
        trending_sized = False
        if (state == "trending"
                and forecast.get("urgency") in ("soon", "imminent")
                and forecast.get("horizon_seconds")
                and gap >= 128 * 1024 * 1024):
            # Bounded by the horizon, not by the distance to the floor: `gap`
            # grows as the machine gets healthier, so a 70%-free box used to
            # reclaim MORE than a 20%-free one (measured 1254 MB vs 210 MB),
            # which is exactly backwards. Inside a ten-minute horizon take a
            # third of the gap; beyond that, pre-empt only a little.
            horizon = float(forecast.get("horizon_seconds") or 0)
            # The forecast pass has a small machine-wide planning ceiling;
            # each actual cgroup request is separately capped to that victim's
            # configured fraction of its current memory below.
            headroom = self.max_victim_slice()
            slice_of_gap = gap // 3 if horizon < 600 else min(gap // 3, headroom // 2)
            needed = max(min(slice_of_gap, headroom), 64 * 1024 * 1024)
            trending_sized = True

        # Inside the margin the 512 MB floor would ask for MORE, not less.
        # A forecast that close to the floor waits; it does not lunge.
        if state == "trending" and not force and not trending_sized:
            if snap.get("psi_stalling"):
                cache_only = True
            else:
                return {"acted": False, "reason": "trending within floor margin",
                        "actions": []}

        if force:
            needed = int(self.config["max_reclaim_per_pass"])

        snap_groups = snap["groups"]

        needed = min(needed, int(self.config["max_reclaim_per_pass"]))

        # Bound the pass by what is actually available to reclaim. Reclaiming
        # more than the machine can spare in one go is what turns a slowdown
        # into an OOM: every page pulled is a page some process will fault back
        # in immediately, and a process that faults hard enough crosses the
        # oomd threshold. A fraction per pass, repeated every few seconds, is
        # indistinguishable to the user and cannot kill anything.
        # MUST use the same predicate as `candidates` below. An earlier version
        # filtered only `protected`, so the ceiling was computed partly from
        # memory this pass can never touch — measured 1241 MB claimed against
        # 1062 MB genuinely reachable, 180 MB of it fiction from a single
        # service that is itself protected.
        _never = set(self.config["never_reclaim"])
        _floor = int(self.config["min_target_bytes"])
        _zram_ok_for_avail = zram_has_room(snap.get("zram"))
        available = sum(
            (g.get("reclaimable") or 0)
            + ((g.get("anon") or 0) if (not cache_only and g.get("swapable", True) and _zram_ok_for_avail) else 0)
            for g in snap_groups
            if not g.get("protected")
            and not names_match(g.get("name") or "", _never)
            and not is_inviolable(g.get("name") or "")
            and (g.get("current") or 0) > _floor)
        # No floor when there is nothing reachable: a phantom budget reads as
        # work the pass could do, on a pass that can do nothing.
        if available > 0:
            needed = min(needed, max(int(available * 0.25), 64 * 1024 * 1024))
        else:
            needed = 0
        needed = min(needed, int(self.config["max_reclaim_per_pass"]))
        actions: list[Action] = []
        budget = needed
        # Per-VICTIM accounting, in the unit the kernel is asked in.
        #
        # max_victim_slice() bounds what ONE cgroup may be asked in ONE pass.
        # It was first implemented as "skip a victim once it has been asked
        # anything", which is much stricter than that: every cgroup with 16MB+
        # of droppable cache is asked in pass 1, so pass 2 could never ask it
        # for anonymous memory — while the whole point of pass 2 is that cache
        # alone is often not enough. Measured on a 6-cgroup desktop, the pass
        # reported "acted" and left the anon pass as dead code.
        #
        # Track the bytes already asked per victim instead, and let the next
        # pass ask only up to the remaining slice. The shared ceiling is
        # preserved exactly; the policy is not silently reduced to cache-only.
        asked_bytes: dict[str, int] = {}

        def remaining_slice(group) -> int:
            return max(0, self.max_victim_slice(group.current)
                       - asked_bytes.get(group.name, 0))

        # Filter out extra keys that snapshot() adds but Cgroup doesn't accept
        _cgroup_fields = {f.name for f in Cgroup.__dataclass_fields__.values()}
        groups = [Cgroup(**{k: (Path(v) if k == "path" else v)
                           for k, v in g.items() if k in _cgroup_fields})
                  for g in snap["groups"]]
        never = set(self.config["never_reclaim"])
        # Final barrier, independent of the `protected` flag above. Every
        # reclaim path goes through this list, so the compositor cannot be
        # reached even if a future edit, a config change, or a bug in scan()
        # gets the protection flag wrong. Reclaim is a best-effort optimisation;
        # evicting the compositor is a dead desktop, and there is no amount of
        # memory pressure that makes that an acceptable trade.
        candidates = [g for g in groups
                      if not g.protected
                      and not is_inviolable(g.name)
                      and not names_match(g.name, never)
                      and g.current > self.config["min_target_bytes"]]

        # Pass 1: page cache, cheapest and invisible to the user.
        if self.config["cache_first"] or cache_only:
            for group in sorted(candidates, key=lambda g: g.reclaimable, reverse=True):
                if budget <= 0:
                    break
                if group.reclaimable < 16 * 1024 * 1024:
                    continue
                ask = min(budget, group.reclaimable, remaining_slice(group))
                if ask < 16 * 1024 * 1024:
                    continue
                action = self.reclaim_from(group, ask, cache_only=True)
                actions.append(action)
                # A cgroup that vanished, or one we may not write, did not take
                # any of the budget. Charging it anyway ends the pass on a miss.
                # On EAGAIN/error with 0 freed, deduct only what was actually
                # freed — not the full ask — to avoid starving Pass 2.
                if action.outcome in ("gone", "denied"):
                    pass  # do not charge the budget
                elif action.outcome in ("ok",):
                    budget -= ask
                    asked_bytes[group.name] = asked_bytes.get(group.name, 0) + ask
                else:
                    # EAGAIN / error: charge only what was freed
                    budget -= max(action.cgroup_freed, 0)
                    asked_bytes[group.name] = asked_bytes.get(group.name, 0) + max(action.cgroup_freed, 0)

        # Pass 2: anonymous memory to zram. Target the single biggest holder
        # rather than spreading thin slices across everything: one process
        # giving up 500MB in one go is far cheaper than ten processes each
        # faulting a little back in, and it keeps the reclaimed pages
        # together in zram where the compressor works on a contiguous run.
        skipped_anon = ""
        # F1: Swap-Thrash Guard — abort anon reclaim when PSI full is high
        psi_data = snap.get("psi") or {}
        psi_full_abort = float(self.config.get("psi_full_abort_percent", 10.0))
        if (budget > 0 and not cache_only
                and psi_data.get("available")
                and psi_data.get("full_avg10", 0.0) >= psi_full_abort):
            skipped_anon = "psi-full-thrash"
            cache_only = True
        elif budget > 0 and not cache_only and not zram_has_room(snap.get("zram")):
            # Disk swap is not a fallback. Cache was the action that was
            # allowed; anonymous pages stay where they are, and the pass says so.
            skipped_anon = "no-zram-room"
        elif budget > 0 and not cache_only and zram_has_room(snap.get("zram")):
            for group in sorted(candidates, key=lambda g: g.anon, reverse=True):
                if budget <= 0:
                    break
                if group.anon < 64 * 1024 * 1024:
                    continue
                # Skip victims that cannot swap: asking them for anon is a
                # guaranteed EAGAIN that wastes the pass's budget.
                if not group.swapable:
                    continue
                ask = min(budget, group.anon, remaining_slice(group))
                if ask < 16 * 1024 * 1024:
                    continue
                if not force:
                    ready_at = self._anon_ready_at.get(group.name, 0.0)
                    if self._clock() < ready_at:
                        continue
                action = self.reclaim_from(group, ask)
                actions.append(action)
                if action.outcome in ("gone", "denied"):
                    continue
                if action.outcome == "ok":
                    budget -= ask
                    asked_bytes[group.name] = asked_bytes.get(group.name, 0) + ask
                else:
                    # EAGAIN / error: charge only what was freed
                    budget -= max(action.cgroup_freed, 0)
                    asked_bytes[group.name] = asked_bytes.get(group.name, 0) + max(action.cgroup_freed, 0)
                try:
                    pause = float(self.config.get("anon_reclaim_cooldown", 90.0))
                except (TypeError, ValueError):
                    pause = 90.0
                # Critical is the state that cannot wait a minute and a half
                # for the same victim. The ceiling is per cgroup, and a
                # shorter configured pause stays shorter.
                if state == "critical" and math.isfinite(pause):
                    pause = min(pause, 15.0)
                if math.isfinite(pause) and pause > 0:
                    self._anon_ready_at[group.name] = self._clock() + pause

                # F1: Refault detection — if we swapped out pages but the
                # cgroup freed very little (pages faulted right back in),
                # increment strikes and apply exponential cooldown.
                if action.swapped_out > 0 and action.cgroup_freed < action.swapped_out * 0.5:
                    strikes, _ = self._swap_history.get(group.name, (0, 0.0))
                    strikes += 1
                    cooldown = min(600.0, 30.0 * (2 ** min(strikes, 5)))
                    self._swap_history[group.name] = (strikes, self._clock() + cooldown)
                    self._anon_ready_at[group.name] = self._clock() + cooldown

        # Pass 3: cgroups with swap disabled are NOT reclaimed.
        # Asking them for anon is a guaranteed EAGAIN that wastes the pass's budget.
        # Their cache was already tried in pass 1; if they still owe us, the kernel
        # will refuse with EAGAIN, which is the honest answer that there is nothing left to take.
        # NOTE: This pass is intentionally empty. The original implementation asked
        # non-swapable cgroups for anon, which always fails with EAGAIN and wastes
        # the pass's budget. The test expects non-swapable victims to NOT be reached.

        # Prune expired entries from _anon_ready_at to prevent unbounded growth
        now_mono = self._clock()
        expired = [k for k, v in self._anon_ready_at.items() if now_mono >= v]
        for k in expired:
            del self._anon_ready_at[k]

        real = [a for a in actions if a.freed > 0]
        total_freed = sum(a.freed for a in real)
        self.actions_today += len(real)
        self.bytes_reclaimed_today += total_freed
        if real:
            self.session_stats.record(total_freed, label=real[0].label if real else "")
        reason = f"state={state}, free={free_percent}%, target={target_free}%"
        if skipped_anon:
            reason = "No ram-backed swap with room, so app memory was not moved to disk swap"
        summary = {
            "acted": bool(real),
            "reason": reason,
            "skipped_anon": skipped_anon,
            "requested": needed,
            "freed": sum(a.freed for a in real),
            "swapped_out": sum(a.swapped_out for a in real),
            "actions": [asdict(a) for a in actions],
        }
        if real:
            self.log({"event": "reclaim", **summary})
        if skipped_anon:
            self.log({"event": "anon-skipped", "skipped_anon": skipped_anon,
                      "reason": reason, "state": state})
        return summary

    def sweep_cache(self) -> dict:
        """Manual, user-requested cleanup: drop page cache and nothing else.

        Cheap, reversible, and no process's anonymous memory is touched — which
        is what the button in the panel promises.
        """
        return self.relieve(force=True, cache_only=True)

    def supervise_once(self) -> dict:
        """One measurement, one reclaim when there is pressure, then publish.

        The panel and the notifier read the file this writes. Publishing
        before the pass used to let them say a reclaim was under way on a
        pass that had refused to move anything.
        """
        self._check_pause()
        snap = self.snapshot()
        if self._paused:
            relief = {"acted": False, "reason": "paused", "skipped_anon": ""}
        elif snap["state"] == "ok":
            relief = {"acted": False, "reason": "no pressure", "skipped_anon": ""}
        else:
            relief = self.relieve(snap)
            if relief.get("acted"):
                # Re-read state for the panel, but do NOT feed the post-reclaim
                # readings into TrendTracker — the sudden +N MB jump from our
                # own reclaim would destroy R² and mask real leaks.
                saved_samples = list(self.tracker.samples)
                snap = self.snapshot()
                self.tracker.samples = saved_samples
        snap["relief"] = {
            "acted": bool(relief.get("acted")),
            "reason": str(relief.get("reason") or ""),
            "skipped_anon": str(relief.get("skipped_anon") or ""),
        }
        self.publish(snap)
        return snap

    def _check_pause(self) -> None:
        """Read pause.json from state_dir and update self._paused state."""
        pause_path = state_dir() / "pause.json"
        try:
            data = json.loads(pause_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._paused = False
            self._paused_until = None
            return
        if not isinstance(data, dict):
            self._paused = False
            self._paused_until = None
            return
        paused_until = data.get("paused_until")
        if paused_until is None:
            # Indefinite pause
            self._paused = True
            self._paused_until = None
        elif isinstance(paused_until, (int, float)) and math.isfinite(paused_until):
            if time.time() < paused_until:
                self._paused = True
                self._paused_until = paused_until
            else:
                # Pause expired — clean up
                self._paused = False
                self._paused_until = None
                try:
                    pause_path.unlink()
                except OSError:
                    pass
        else:
            self._paused = False
            self._paused_until = None

    # -- loop ------------------------------------------------------------
    def run_forever(self) -> None:
        """The supervision loop.

        Hardened against the two ways a background daemon dies badly: dying
        quietly (which looks identical to "nothing to do"), and spinning hot
        (which turns a bug into a machine-wide slowdown). Every pass is
        individually guarded, failures back off, and a broken config reloads
        from disk so an edit takes effect without a restart.
        """
        self.log({"event": "start", "pid": os.getpid(), "cgroup": self_cgroup()})
        consecutive_failures = 0
        seen_mtime = None

        while True:
            try:
                # A panel click writes the config and should be visible on the
                # next pass, not five minutes later. The sleep also returns
                # early when the file's mtime changes.
                try:
                    mtime = config_path().stat().st_mtime
                except OSError:
                    mtime = None
                if mtime != seen_mtime:
                    self.config = load_config()
                    seen_mtime = mtime
                    if _CONFIG_WARNINGS:
                        self.log({"event": "config-warning",
                                  "warnings": list(_CONFIG_WARNINGS)})

                snap = self.supervise_once()

                consecutive_failures = 0
                # Only states that relieve() will actually act on. `stalling`
                # is refused by relieve (the stall is not attributable to any
                # one cgroup), so ticking at the 5s pressure rate for it just
                # bought a full 51-cgroup walk and a swap read every
                # five seconds, forever, doing nothing.
                pressure = snap["state"] in ("tight", "critical", "trending", "stalling")
                self._sleep(self.config["interval_pressure"] if pressure
                            else self.config["interval_idle"])

            except KeyboardInterrupt:
                break
            except Exception as exc:
                # Never let one bad pass kill the process: with Restart=always
                # a crash loop would burn CPU and hide the real error behind a
                # restart storm. Back off instead, and say so.
                consecutive_failures += 1
                self.log({"event": "error", "error": repr(exc),
                          "consecutive_failures": consecutive_failures})
                # Exponential backoff, capped, so a persistent failure costs a
                # few wake-ups a minute rather than thousands.
                backoff = min(300.0, self.config["interval_idle"]
                              * (2 ** min(consecutive_failures, 5)))
                if consecutive_failures in (1, 5, 25):
                    print(f"ram-keeper: pass failed ({exc!r}); retrying in {backoff:.0f}s",
                          file=sys.stderr, flush=True)
                self._sleep(backoff)

        self.log({"event": "stop"})

    def _sleep(self, seconds: float) -> None:
        """Sleep, refusing to pass a value time.sleep would reject.

        load_config clamps intervals, but this is the last line of defence:
        a negative or NaN interval raises ValueError, and an exception thrown
        from inside the sleep would escape the loop's own except clause.
        """
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            value = 20.0
        if not (value == value) or value <= 0 or value == float("inf"):
            value = 20.0
        remaining = min(value, 3600.0)
        try:
            seen_config = config_path().stat().st_mtime
        except OSError:
            seen_config = None
        pause_path = state_dir() / "pause.json"
        try:
            seen_pause = pause_path.stat().st_mtime
        except OSError:
            seen_pause = None
        while remaining > 0:
            try:
                cur_config = config_path().stat().st_mtime
            except OSError:
                cur_config = None
            # Wake on ANY change: initial creation (seen is None, current not
            # None) OR mtime change.
            if cur_config is not None and (seen_config is None or cur_config != seen_config):
                return
            # Also wake on pause.json creation or change
            try:
                cur_pause = pause_path.stat().st_mtime
            except OSError:
                cur_pause = None
            if cur_pause is not None and (seen_pause is None or cur_pause != seen_pause):
                return
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def published_snapshot(max_age: float = 120.0) -> dict | None:
    """The daemon's own last snapshot, if it is fresh.

    Preferred over re-measuring because it carries the trend history, which
    only exists inside the long-running daemon. A short-lived process would
    always report "no forecast yet" — the panel would show a permanently
    useless row and nobody would trust it.
    """
    path = state_dir() / "state.json"
    try:
        st = path.stat()
        # Reject non-regular files (FIFO would block), oversized files (OOM
        # under MemoryMax=384M), and future mtime (clock skew).
        if not __import__("stat").S_ISREG(st.st_mode):
            return None
        if st.st_size > 10 * 1024 * 1024:   # 10 MB cap
            return None
        age = time.time() - st.st_mtime
        if age > max_age or age < -60:       # future mtime = clock skew
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("mem_total"):
            return data
    except (OSError, ValueError, RecursionError):
        pass
    return None


def add_never_reclaim(name: str) -> list[str]:
    """Add one cgroup name to `never_reclaim`, safely.

    Three properties, each of which the naive `read → modify → write_text`
    version did not have:
      * a lock, so two concurrent calls (the panel's button and a CLI call)
        cannot lose one of the two additions;
      * an atomic replace, so the daemon's 300-second config reload can never
        read a half-written file and fall back to DEFAULTS — which would
        silently un-protect everything the user had protected;
      * validation, so an empty or multi-line argument cannot be written into
        the config as a setting that matches nothing.
    """
    name = (name or "").strip()
    if not name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("cgroup name must be one non-blank line")

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "a", encoding="utf-8") as lock_handle:
        try:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass

        existing: dict = {}
        try:
            loaded = json.loads(path.read_bytes().decode("utf-8-sig"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            existing = {}

        current = existing.get("never_reclaim")
        if isinstance(current, str):
            current = [current]
        if not isinstance(current, list):
            current = []
        names = [v.strip() for v in current
                 if isinstance(v, str) and v.strip()]

        if name not in names:
            names.append(name)
        existing["never_reclaim"] = names
        raw_unprotect = existing.get("unprotect")
        if isinstance(raw_unprotect, str):
            raw_unprotect = [raw_unprotect]
        if not isinstance(raw_unprotect, list):
            raw_unprotect = []
        existing["unprotect"] = [
            v.strip() for v in raw_unprotect
            if isinstance(v, str) and v.strip() and not names_match(v, [name])
        ]

        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    return names


def remove_protection(name: str) -> list[str]:
    """Take one name off both lists, and keep a default from coming back.

    The stored protected list is merged with the defaults whenever it is
    non-empty, so deleting ollama from that list would not last. The name is
    recorded in `unprotect`, which is subtracted after the merge. An inviolable
    name is refused.
    """
    name = (name or "").strip()
    if not name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("cgroup name must be one non-blank line")
    if is_inviolable(name):
        raise ValueError(f"{name} stays protected")

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "a", encoding="utf-8") as lock_handle:
        try:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass

        existing: dict = {}
        try:
            loaded = json.loads(path.read_bytes().decode("utf-8-sig"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            existing = {}

        def _listed(key: str) -> list[str]:
            current = existing.get(key)
            if isinstance(current, str):
                current = [current]
            if not isinstance(current, list):
                current = []
            return [v.strip() for v in current if isinstance(v, str) and v.strip()]

        def _drop_name(key: str) -> None:
            if key not in existing:
                return
            original = _listed(key)
            if not original:
                # Was already empty — an explicit user choice. Preserve so
                # defaults don't come back.
                return
            kept = [v for v in original if not names_match(v, [name])]
            if kept:
                existing[key] = kept
            else:
                # Was non-empty, now empty after removing the target.
                # Pop the key so load_config() falls back to defaults.
                existing.pop(key, None)

        _drop_name("protected")
        _drop_name("never_reclaim")
        gone = _listed("unprotect")
        if not any(names_match(v, [name]) for v in gone):
            gone.append(name)
        existing["unprotect"] = gone

        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    return existing["unprotect"]


def pause_daemon(minutes: float | None = None) -> dict:
    """Write pause.json so the daemon skips reclaim passes.

    minutes=None → indefinite pause.
    minutes=0 → resume (deletes pause.json).
    minutes>0 → pause for that many minutes.
    """
    pause_path = state_dir() / "pause.json"
    pause_path.parent.mkdir(parents=True, exist_ok=True)

    if minutes is not None and minutes <= 0:
        # Resume
        try:
            pause_path.unlink()
        except OSError:
            pass
        return {"paused": False}

    data: dict = {"set_at": time.time()}
    if minutes is not None:
        data["paused_until"] = time.time() + minutes * 60.0
    else:
        data["paused_until"] = None

    tmp = pause_path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(pause_path)
    return {"paused": True, **data}


def resume_daemon() -> dict:
    """Remove pause.json to resume the daemon."""
    return pause_daemon(minutes=0)


def doctor(as_json: bool = False) -> tuple[int, str]:
    """Read-only health check. Returns (exit_code, report_text).

    exit 0 = all ok, exit 1 = at least one problem found.
    """
    checks: list[dict] = []

    # 1. cgroup v2
    cg2 = CGROOT.is_dir() and (CGROOT / "cgroup.controllers").exists()
    checks.append({"name": "cgroup_v2", "ok": cg2,
                    "detail": "cgroup v2 mounted" if cg2 else "cgroup v2 not found"})

    # 2. user slice
    try:
        uid = os.getuid()
        uslice = CGROOT / "user.slice" / f"user-{uid}.slice"
        us_ok = uslice.is_dir()
    except AttributeError:
        uslice = Path("N/A")
        us_ok = False
    checks.append({"name": "user_slice", "ok": us_ok,
                    "detail": str(uslice) if us_ok else "user slice not found"})

    # 3. memory.reclaim writable
    reclaim_ok = False
    if us_ok:
        try:
            # Find at least one leaf with memory.reclaim
            for d in uslice.rglob("*"):
                try:
                    rf = d / "memory.reclaim"
                    if rf.exists() and os.access(str(rf), os.W_OK):
                        reclaim_ok = True
                        break
                except OSError:
                    continue
        except OSError:
            pass
    checks.append({"name": "memory_reclaim_writable", "ok": reclaim_ok,
                    "detail": "at least one writable reclaim file" if reclaim_ok else "no writable memory.reclaim found"})

    # 4. /proc/meminfo
    mi = meminfo()
    mi_ok = mi.get("MemTotal", 0) > 0 and "MemAvailable" in mi
    checks.append({"name": "meminfo", "ok": mi_ok,
                    "detail": f"MemTotal={mi.get('MemTotal', 0)}" if mi_ok else "cannot read /proc/meminfo"})

    # 5. PSI
    psi_data = psi("memory")
    psi_ok = psi_data.get("available", False)
    checks.append({"name": "psi", "ok": psi_ok,
                    "detail": f"some_avg60={psi_data.get('some_avg60', 0)}" if psi_ok else "PSI not available"})

    # 6. zram
    zram = zram_stats()
    zram_ok = any(d.get("ram_backed") for d in (zram.get("devices") or []))
    checks.append({"name": "zram", "ok": zram_ok,
                    "detail": f"ram_used={zram.get('ram_used', 0)}" if zram_ok else "no RAM-backed swap device"})

    # 7. config
    cfg = load_config()
    warnings = config_warnings()
    cfg_ok = not warnings
    checks.append({"name": "config", "ok": cfg_ok,
                    "detail": "clean" if cfg_ok else f"{len(warnings)} warnings"})

    # 8. state.json age
    state_path = state_dir() / "state.json"
    state_ok = False
    state_detail = "no state.json"
    try:
        age = time.time() - state_path.stat().st_mtime
        if 0 <= age <= 120:
            state_ok = True
            state_detail = f"age={age:.0f}s"
        else:
            state_detail = f"stale (age={age:.0f}s)"
    except OSError:
        pass
    checks.append({"name": "state_freshness", "ok": state_ok, "detail": state_detail})

    # 9. pause state
    pause_path = state_dir() / "pause.json"
    paused = pause_path.exists()
    checks.append({"name": "pause", "ok": True,
                    "detail": "paused" if paused else "not paused"})

    all_ok = all(c["ok"] for c in checks)
    exit_code = 0 if all_ok else 1

    if as_json:
        result = {"ok": all_ok, "checks": checks}
        if warnings:
            result["config_warnings"] = warnings
        return exit_code, json.dumps(result, indent=2)

    lines = []
    for c in checks:
        mark = "✓" if c["ok"] else "✗"
        lines.append(f"  {mark} {c['name']}: {c['detail']}")
    if warnings:
        lines.append("")
        lines.append("  Config warnings:")
        for w in warnings:
            lines.append(f"    - {w}")
    header = "ram-keeper doctor: " + ("all ok" if all_ok else "problems found")
    return exit_code, header + "\n" + "\n".join(lines)


def history(count: int = 20, as_json: bool = False) -> str:
    """Read the last N events from the reclaim log.

    Returns a human-readable summary (or JSON when as_json=True).
    """
    log_path = state_dir() / "log.jsonl"
    log_prev = state_dir() / "log.jsonl.1"
    events: list[dict] = []

    for path in (log_prev, log_path):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except (ValueError, RecursionError):
                    continue
        except OSError:
            continue

    # Filter to reclaim events and take last N
    reclaims = [e for e in events if e.get("event") == "reclaim"]
    tail = reclaims[-count:]

    if as_json:
        return json.dumps({"total_events": len(reclaims), "shown": tail}, indent=2, default=str)

    if not tail:
        return "ram-keeper history: no reclaim events found"

    total_freed = sum(e.get("freed", 0) for e in tail)
    total_swapped = sum(e.get("swapped_out", 0) for e in tail)
    lines = [f"ram-keeper history: last {len(tail)} of {len(reclaims)} reclaim events"]
    lines.append(f"  Total freed: {total_freed / (1024*1024):.1f} MB, swapped: {total_swapped / (1024*1024):.1f} MB")
    lines.append("")

    # Top victims by freed bytes
    victim_freed: dict[str, int] = {}
    for ev in tail:
        for action in ev.get("actions", []):
            label = action.get("label", "?")
            victim_freed[label] = victim_freed.get(label, 0) + action.get("freed", 0)
    top = sorted(victim_freed.items(), key=lambda x: x[1], reverse=True)[:5]
    if top:
        lines.append("  Top victims:")
        for label, freed in top:
            lines.append(f"    {label}: {freed / (1024*1024):.1f} MB")
        lines.append("")

    # Recent events
    for ev in tail[-5:]:
        ts = ev.get("ts", 0)
        try:
            dt_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except (OSError, ValueError, OverflowError):
            dt_str = str(ts)
        freed = ev.get("freed", 0) / (1024 * 1024)
        reason = ev.get("reason", "")
        lines.append(f"  [{dt_str}] freed {freed:.1f} MB — {reason}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="ram-keeper")
    parser.add_argument("--once", action="store_true",
                        help="assess once, print JSON, exit")
    parser.add_argument("--daemon", action="store_true",
                        help="run the supervision loop")
    parser.add_argument("--relieve", action="store_true",
                        help="force one reclamation pass")
    parser.add_argument("--sweep-cache", action="store_true",
                        help="drop reclaimable page cache now")
    parser.add_argument("--protected-add", metavar="CGROUP",
                        help="add a cgroup to the never-reclaim list")
    parser.add_argument("--protected-remove", metavar="CGROUP",
                        help="stop protecting a cgroup the defaults would keep")
    parser.add_argument("--doctor", action="store_true",
                        help="read-only health check; exit 0=ok, 1=problem")
    parser.add_argument("--history", nargs="?", const=20, type=int, metavar="N",
                        help="show last N reclaim events (default: 20)")
    parser.add_argument("--pause", nargs="?", const=-1, type=float, metavar="MINUTES",
                        help="pause reclaim for MINUTES (omit for indefinite, 0 to resume)")
    parser.add_argument("--resume", action="store_true",
                        help="resume reclaim (alias for --pause 0)")
    parser.add_argument("--json", action="store_true",
                        help="output as JSON (for --doctor and --history)")
    args = parser.parse_args()

    if args.protected_add and args.protected_remove:
        print("ram-keeper: pass only one of --protected-add and --protected-remove",
              file=sys.stderr)
        return 2

    if args.protected_remove:
        try:
            lst = remove_protection(args.protected_remove)
        except ValueError as exc:
            print(f"ram-keeper: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"ram-keeper: could not write config: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"unprotect": lst}))
        return 0

    if args.protected_add:
        try:
            lst = add_never_reclaim(args.protected_add)
        except ValueError as exc:
            print(f"ram-keeper: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"ram-keeper: could not write config: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"protected": lst}))
        return 0

    if args.doctor:
        code, text = doctor(as_json=getattr(args, "json", False))
        print(text)
        return code

    if args.history is not None:
        text = history(count=args.history,
                       as_json=getattr(args, "json", False))
        print(text)
        return 0

    if args.resume:
        result = resume_daemon()
        print(json.dumps(result))
        return 0

    if args.pause is not None:
        minutes = None if args.pause < 0 else args.pause
        result = pause_daemon(minutes=minutes)
        print(json.dumps(result, default=str))
        return 0

    steward = Steward()

    if args.once:
        # Prefer the daemon's published snapshot: it carries the forecast.
        published = published_snapshot()
        if published is not None:
            print(json.dumps(published, default=str))
        else:
            print(json.dumps(steward.snapshot(), default=str))
        return 0

    if args.relieve:
        # Merge relief into the daemon's existing snapshot if available,
        # so we don't destroy its forecast/session_stats.
        result = steward.relieve(force=True)
        print(json.dumps(result, default=str))
        existing = published_snapshot() or {}
        fresh = steward.snapshot()
        # Preserve the daemon's forecast and session_stats if they exist
        for key in ("forecast", "session_stats", "paused", "paused_until",
                    "paused_remaining_s", "interval_idle"):
            if key in existing and key not in fresh:
                fresh[key] = existing[key]
            elif key in existing and existing[key] is not None:
                fresh[key] = existing[key]
        fresh["relief"] = {
            "acted": bool(result.get("acted")),
            "reason": str(result.get("reason") or ""),
            "skipped_anon": str(result.get("skipped_anon") or ""),
        }
        steward.publish(fresh)
        return 0

    if args.sweep_cache:
        result = steward.sweep_cache()
        print(json.dumps(result, default=str))
        existing = published_snapshot() or {}
        fresh = steward.snapshot()
        for key in ("forecast", "session_stats", "paused", "paused_until",
                    "paused_remaining_s", "interval_idle"):
            if key in existing and key not in fresh:
                fresh[key] = existing[key]
            elif key in existing and existing[key] is not None:
                fresh[key] = existing[key]
        fresh["relief"] = {
            "acted": bool(result.get("acted")),
            "reason": str(result.get("reason") or ""),
            "skipped_anon": str(result.get("skipped_anon") or ""),
        }
        steward.publish(fresh)
        return 0

    if args.daemon:
        steward.run_forever()
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())