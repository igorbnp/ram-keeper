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
import json
import os
import socket
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
    uid = os.getuid()
    slices = sorted(CGROOT.glob(f"user.slice/user-{uid}.slice"))
    if slices:
        return slices[0]
    # Fall back to the first user slice present (containers, unusual setups).
    for candidate in sorted(CGROOT.glob("user.slice/user-*.slice")):
        return candidate
    return CGROOT / "user.slice"


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


def human_label(name: str) -> str:
    """Turn a systemd cgroup name into something worth showing a human."""
    label = name
    if name.startswith("app-"):
        label = name[4:]
    if label.endswith(".scope"):
        label = label[:-6]
    if label.endswith(".service"):
        label = label[:-8]
    label = label.replace("\\x2d", "-")
    if label.startswith("org.chromium.Chromium-"):
        label = "Chromium " + label.rsplit("-", 1)[-1]
    if label.startswith("app-"):
        label = label[4:]
    return label or name


def scan(root: Path, protected: set[str], min_bytes: int) -> list[Cgroup]:
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

        is_protected = (name in protected
                        or name.removesuffix(".service") in protected
                        or is_inviolable(name))
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
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, rest = line.split(":", 1)
            values[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return values


def psi(resource: str = "memory") -> dict:
    """Pressure Stall Information: how long the system waited on memory."""
    out = {"some_avg10": 0.0, "some_avg60": 0.0, "some_avg300": 0.0,
           "full_avg10": 0.0, "full_avg60": 0.0, "full_avg300": 0.0}
    try:
        text = Path(f"/proc/pressure/{resource}").read_text()
    except OSError:
        return out
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
                    out[key] = float(value)
                except ValueError:
                    pass
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
        out = subprocess.run(["swapon", "--show=NAME,TYPE,SIZE,USED,PRIO",
                              "--noheadings", "--bytes"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                name = parts[0]
                devices.append({
                    "name": name,
                    "type": parts[1],
                    "size": int(parts[2]) if parts[2].isdigit() else 0,
                    "used": int(parts[3]) if parts[3].isdigit() else 0,
                    "ram_backed": "zram" in name.lower(),
                })
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {"devices": devices,
            "used": sum(d["used"] for d in devices),
            "size": sum(d["size"] for d in devices),
            "disk_used": sum(d["used"] for d in devices if not d["ram_backed"]),
            "ram_used": sum(d["used"] for d in devices if d["ram_backed"])}


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
        "ram-keeper",
    ],
    # Never reclaim these even if large (background indexers etc. rebuild
    # caches slowly and are cheap to leave alone).
    "never_reclaim": [
        "localsearch-3.service",
    ],
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
})


def is_inviolable(name: str) -> bool:
    """True for cgroups no config file may ever hand to a reclaim sweep."""
    if name in INVIOABLE_PROTECTED:
        return True
    # Strip every systemd unit suffix, not just two: session.slice has to
    # reduce to "session", which .removesuffix(".scope") alone does not do.
    base = name
    for suffix in (".service", ".scope", ".slice"):
        base = base.removesuffix(suffix)
    if base in INVIOABLE_PROTECTED:
        return True
    # session.slice, and the compositor's own session-N.scope which is numbered
    # per login, so the exact name cannot be hard-coded.
    return base == "session" or base.startswith("session-")


def config_path() -> Path:
    return Path.home() / ".config" / "ram-keeper" / "config.json"


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
    "max_reclaim_per_pass": (16 * 1024 * 1024, 32 * 1024 * 1024 * 1024),
    "max_victim_fraction": (5, 60),
}

_CONFIG_WARNINGS: list[str] = []


def _coerce_number(key: str, value, fallback):
    """Return `value` as a usable number, or the default if it isn't one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _CONFIG_WARNINGS.append(f"{key}: expected a number, got {value!r}; using {fallback}")
        return fallback
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
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
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
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

    try:
        raw = config_path().read_text()
    except OSError:
        raw = None
    if raw is not None:
        try:
            user = json.loads(raw)
        except ValueError as exc:
            _CONFIG_WARNINGS.append(f"config.json is not valid JSON ({exc}); using defaults")
            user = None
        if isinstance(user, dict):
            for key, value in user.items():
                if key not in DEFAULT_CONFIG:
                    _CONFIG_WARNINGS.append(f"unknown setting {key!r} ignored")
                    continue
                if key in ("protected", "never_reclaim"):
                    cfg[key] = _coerce_strlist(key, value, DEFAULT_CONFIG[key])
                    # Merge, never replace. If a user adds their own app to
                    # `protected`, replacing the default list would silently
                    # un-protect everything the defaults protected — including
                    # (before the INVIOABLE_PROTECTED guard existed) the
                    # compositor. Adding to the defaults is also what the user
                    # meant. An explicit empty list is still honoured, since
                    # that is an unambiguous statement.
                    if value:
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

        if slope < -1.0:
            distance = ys[-1] - target_bytes     # bytes still above the floor
            if distance > 0:
                forecast.horizon_seconds = distance / (-slope)
                forecast.will_cross = True
                forecast.trend = "rising"
                forecast.culprit = self._fastest_growing(window)

        # An imminent crossing on a poor fit is probably a coincidence. Report
        # it as eventual so the daemon does not react to noise.
        if forecast.will_cross and forecast.confidence < 0.3 \
                and forecast.horizon_seconds < 300:
            forecast.horizon_seconds *= 10
        return forecast

    def _fastest_growing(self, window: list) -> str:
        """Which cgroup is responsible for the trend.

        Ranked by its own growth rate, not absolute size: the largest process is
        usually not the one that is growing, and blaming the biggest is exactly
        the wrong advice.
        """
        if len(window) < 4:
            return ""
        first, last = window[0], window[-1]
        span = last[0] - first[0]
        if span <= 1.0:
            return ""
        best_name, best_rate = "", 0.0
        for name, value in last[2].items():
            start = first[2].get(name)
            if start is None:
                continue           # process appeared mid-window: not a trend
            rate = (value - start) / span
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


@dataclass
class Steward:
    config: dict = field(default_factory=load_config)
    log_path: Path = field(
        default_factory=lambda: Path.home() / ".local/state/ram-keeper/log.jsonl")
    state_path: Path = field(
        default_factory=lambda: Path.home() / ".local/state/ram-keeper/state.json")
    actions_today: int = 0
    bytes_reclaimed_today: int = 0
    tracker: TrendTracker = field(default_factory=TrendTracker)
    last_forecast: Forecast = field(default_factory=Forecast)
    # Rotate the reclaim log at 4 MB; two generations are kept.
    max_log_bytes: int = 4 * 1024 * 1024

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
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, default=str))
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
        readings_valid = raw_total > 0 and raw_available > 0

        total = raw_total or 1
        available = raw_available if readings_valid else 0
        free_percent = (100.0 * available / total) if readings_valid else 100.0
        pressure = psi("memory")
        swap = zram_stats()

        protected = set(self.config["protected"])
        never = set(self.config["never_reclaim"])
        groups = scan(user_root(), protected, int(self.config["min_target_bytes"]))

        victims = [g for g in groups if not g.protected and g.name not in never]
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
        elif pressure["some_avg60"] > self.config["psi_some_percent"]:
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
            target_bytes = int(total * self.config["target_free_percent"] / 100.0)

        # An imminent crossing is pressure the floor does not show yet. Treat
        # it as its own state so the daemon acts before the number moves rather
        # than after — which is the entire reason for having a forecast.
        if state == "ok" and self.last_forecast.urgency == "imminent":
            state = "trending"

        # MemAvailable already excludes reclaimable cache, so the honest split of
        # "used" is: real anon+kernel, cache, then free-and-available. `Cached`
        # from meminfo includes pages held by processes' file mappings, and on a
        # healthy machine it approaches the whole of MemUsed — so the two are
        # reported separately and never allowed to imply that apps use nothing.
        mem_used = info.get("MemTotal", 0) - info.get("MemAvailable", 0)
        cached = info.get("Cached", 0)
        # Clamp: cache can never exceed what is counted as used, and the two
        # together can never exceed the machine.
        cached = min(cached, mem_used, total)
        anon_and_kernel = mem_used - cached

        return {
            "state": state,
            "free_percent": round(free_percent, 1),
            "mem_total": total,
            "mem_available": available,
            "mem_used": mem_used,
            "anon_and_kernel": anon_and_kernel,
            "cached": cached,
            "zram": swap,
            "psi": pressure,
            "forecast": self.last_forecast.as_dict(),
            "groups": [asdict(g) | {"path": str(g.path)} for g in groups],
            "reclaimable_total": sum(g.reclaimable for g in victims),
            "checked_at": time.time(),
        }

    # -- reclamation -----------------------------------------------------
    def reclaim_from(self, group: Cgroup, amount: int) -> Action:
        """Ask one cgroup to give back `amount`. Cache comes off first."""
        before = _read_int(group.path / "memory.current")
        before_swap = cgroup_swap(group.path)
        outcome = "unknown"
        try:
            with open(group.reclaim_file, "w", encoding="utf-8") as handle:
                handle.write(str(amount))
            outcome = "ok"
        except PermissionError:
            outcome = "denied"
        except OSError as e:
            # EAGAIN means "could not free that much right now" — the kernel
            # still reclaimed everything it could. Not a failure.
            outcome = "eagain" if e.errno == 11 else f"error:{e.errno}"

        time.sleep(0.4)
        after = _read_int(group.path / "memory.current")
        after_swap = cgroup_swap(group.path)
        freed = max(0, before - after)
        swapped = max(0, after_swap - before_swap)

        # `freed` from the cgroup is not the same as RAM returned to the system
        # when the pages went to zram: measured 4.34x compression on this box,
        # so moving 300 MB of anon out of a cgroup returns ~69 MB of real RAM,
        # not 300. Reporting the cgroup number as "memory freed" overstates the
        # benefit roughly fourfold whenever the anon pass runs — which is the
        # pass that matters most. The honest figure is the RAM actually
        # returned, with the raw cgroup number kept for auditing.
        effective = max(0, freed - int(swapped * self.zram_ratio()))
        action = Action(label=group.label, requested=amount,
                        freed=effective,
                        swapped_out=swapped, outcome=outcome,
                        cgroup_freed=freed)
        self.bytes_reclaimed_today += max(0, effective)
        return action

    @staticmethod
    def zram_ratio() -> float:
        """Bytes of uncompressed data per byte of compressed zram storage.

        Read live from mm_stat so the number is measured, not assumed. Falls
        back to a conservative 3.0 (a typical zstd ratio) when zram is absent,
        which keeps the reported figure honest rather than optimistic.
        """
        try:
            fields = Path("/sys/block/zram0/mm_stat").read_text().split()
            orig, compr = int(fields[0]), int(fields[1])
            if orig > 0 and compr > 0:
                return orig / compr
        except (OSError, IndexError, ValueError):
            pass
        return 3.0

    def max_victim_slice(self) -> int:
        """The most the daemon will ever ask of ONE cgroup in ONE pass.

        This cap exists because of a measured failure, not a hypothetical one.
        Under sustained load the anon pass computed a 1.5 GB ask for a single
        Chromium cgroup holding ~300 MB. The kernel honoured it as far as it
        could, but that emptied the process: it ended up with almost no resident
        pages, faulted hard, and `systemd-oomd` killed it. Reclaim meant to save
        the machine had become the thing that killed an app.

        The fix is to be a small, predictable fraction of what the victim
        actually holds. Several passes over several seconds take 500 MB out of a
        2 GB process without ever leaving it unable to run; one pass asking for
        all of it does not. Sized relative to the victim rather than fixed, so a
        small process is never asked for more than it has.
        """
        return int(self.config["max_victim_fraction"] * 1024 * 1024 * 1024 // 100)

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
        if state in ("ok", "stalling", "unknown") and not force:
            return {"acted": False, "reason": f"no pressure (state={state}, free={free_percent}%)",
                    "actions": []}

        # How much do we actually need? Under real pressure the deficit is the
        # honest number: ask for the shortfall plus headroom, not a fixed
        # slice. A fixed ask starves exactly when it matters most — with a 3GB
        # hog and 400MB free, "256MB please" is a rounding error.
        deficit_percent = max(0.0, target_free - free_percent)
        needed = int(snap["mem_total"] * (deficit_percent / 100.0))
        # One pass frees a fraction of the deficit: reclaiming the whole thing
        # at once would swap out far more than the system can hold in cache and
        # turn a slowdown into a stall.
        needed = max(needed // 2, 64 * 1024 * 1024)

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
            # A pre-emptive pass is a precaution, not a rescue: it is bounded by
            # the per-victim slice like everything else, so a healthy machine can
            # never be drained more than one slice in a single pass.
            headroom = self.max_victim_slice()
            slice_of_gap = gap // 3 if horizon < 600 else min(gap // 3, headroom // 2)
            needed = max(min(slice_of_gap, headroom), 64 * 1024 * 1024)

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
        available = sum((g.get("reclaimable") or 0) + (g.get("anon") or 0)
                        for g in snap_groups
                        if not g.get("protected")
                        and g.get("name") not in _never
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
        # Per-VICTIM-per-pass accounting. max_victim_slice() bounds each REQUEST,
        # not each victim: pass 1 (cache) and pass 2 (anon) both reach the same
        # group, so one cgroup was asked 2x the documented ceiling in a single
        # pass. Each group gets at most one slice per pass, across all passes.
        asked_this_pass: set[str] = set()

        groups = [Cgroup(**{k: v for k, v in g.items() if k != "path"} | {"path": Path(g["path"])})
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
                      and g.name not in never
                      and g.current > self.config["min_target_bytes"]]

        # Pass 1: page cache, cheapest and invisible to the user.
        if self.config["cache_first"]:
            for group in sorted(candidates, key=lambda g: g.reclaimable, reverse=True):
                if budget <= 0:
                    break
                if group.reclaimable < 16 * 1024 * 1024:
                    continue
                # One slice per victim per PASS, across every pass. The slice
                # bounds a single request; without this the cache pass and the
                # anon pass both reached the same group and asked 2x the
                # documented ceiling from one cgroup in one pass.
                if group.name in asked_this_pass:
                    continue
                ask = min(budget, group.reclaimable, self.max_victim_slice())
                action = self.reclaim_from(group, ask)
                actions.append(action)
                budget -= ask      # kernel-visible bytes: the ceiling is what we ASK for
                asked_this_pass.add(group.name)

        # Pass 2: anonymous memory to zram. Target the single biggest holder
        # rather than spreading thin slices across everything: one process
        # giving up 500MB in one go is far cheaper than ten processes each
        # faulting a little back in, and it keeps the reclaimed pages
        # together in zram where the compressor works on a contiguous run.
        if budget > 0 and not cache_only:
            for group in sorted(candidates, key=lambda g: g.anon, reverse=True):
                if budget <= 0:
                    break
                if group.anon < 64 * 1024 * 1024:
                    continue
                # Skip victims that cannot swap: asking them for anon is a
                # guaranteed EAGAIN that wastes the pass's budget.
                if not group.swapable:
                    continue
                if group.name in asked_this_pass:
                    continue
                ask = min(budget, group.anon, self.max_victim_slice())
                action = self.reclaim_from(group, ask)
                actions.append(action)
                budget -= ask          # kernel-visible bytes: the ceiling is on what we ASK for
                asked_this_pass.add(group.name)

        # Pass 3: last resort for cgroups with swap disabled. Their cache was
        # already tried in pass 1; if they still owe us, take what anon the
        # kernel will give without swap (it will refuse with EAGAIN, which is
        # the honest answer that there is nothing left to take).
        if budget > 0 and not cache_only:
            for group in sorted((g for g in candidates if not g.swapable),
                                key=lambda g: g.anon, reverse=True):
                if budget <= 0:
                    break
                if group.anon < 64 * 1024 * 1024:
                    continue
                ask = min(budget, group.anon, self.max_victim_slice())
                action = self.reclaim_from(group, ask)
                actions.append(action)
                budget -= ask          # kernel-visible bytes: the ceiling is on what we ASK for

        real = [a for a in actions if a.freed > 0]
        self.actions_today += len(real)
        summary = {
            "acted": bool(real),
            "reason": f"state={state}, free={free_percent}%, target={target_free}%",
            "requested": needed,
            "freed": sum(a.freed for a in real),
            "swapped_out": sum(a.swapped_out for a in real),
            "actions": [asdict(a) for a in actions],
        }
        if real:
            self.log({"event": "reclaim", **summary})
        return summary

    def sweep_cache(self) -> dict:
        """Manual, user-requested cleanup: drop page cache and nothing else.

        Cheap, reversible, and no process's anonymous memory is touched — which
        is what the button in the panel promises.
        """
        return self.relieve(force=True, cache_only=True)

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
        last_reload = 0.0
        reload_interval = 300.0

        while True:
            try:
                # Pick up an edited config occasionally. Without this the only
                # way a threshold change takes effect is a restart, and a user
                # who tuned it would reasonably assume it was live.
                now = time.monotonic()
                if now - last_reload > reload_interval:
                    self.config = load_config()
                    last_reload = now
                    if _CONFIG_WARNINGS:
                        self.log({"event": "config-warning",
                                  "warnings": list(_CONFIG_WARNINGS)})

                snap = self.snapshot()
                self.publish(snap)
                if snap["state"] != "ok":
                    self.relieve(snap)

                consecutive_failures = 0
                # Only states that relieve() will actually act on. `stalling`
                # is refused by relieve (the stall is not attributable to any
                # one cgroup), so ticking at the 5s pressure rate for it just
                # bought a full 51-cgroup walk and a `swapon` subprocess every
                # five seconds, forever, doing nothing.
                pressure = snap["state"] in ("tight", "critical", "trending")
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
        time.sleep(min(value, 3600.0))


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
    path = Path.home() / ".local/state/ram-keeper/state.json"
    try:
        if time.time() - path.stat().st_mtime > max_age:
            return None
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.get("mem_total"):
            return data
    except (OSError, ValueError):
        pass
    return None


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
    args = parser.parse_args()

    steward = Steward()

    if args.protected_add:
        cfg = load_config()
        lst = list(cfg["never_reclaim"])
        if args.protected_add not in lst:
            lst.append(args.protected_add)
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["never_reclaim"] = lst
        path.write_text(json.dumps(existing, indent=2))
        print(json.dumps({"protected": lst}))
        return 0

    if args.once:
        # Prefer the daemon's published snapshot: it carries the forecast.
        published = published_snapshot()
        if published is not None:
            print(json.dumps(published, default=str))
        else:
            print(json.dumps(steward.snapshot(), default=str))
        return 0

    if args.relieve:
        print(json.dumps(steward.relieve(force=True), default=str))
        return 0

    if args.sweep_cache:
        print(json.dumps(steward.sweep_cache(), default=str))
        return 0

    if args.daemon:
        steward.run_forever()
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())