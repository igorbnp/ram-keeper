"""Adversarial tests for the reclaim policy itself.

These target the accounting and ordering decisions that decide how much memory
one pass may ask for, from whom, and what the pass is allowed to report
afterwards. They are deliberately behavioural: every assertion drives the real
`Steward.relieve()` / `Steward.reclaim_from()` and inspects what was actually
asked of the kernel, instead of grepping the source for a string that looks
like the fix.

Everything here runs against a temporary directory and a stubbed
`reclaim_from`, so it is safe on a CI runner and on a live desktop.
"""

import json
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import ram_keeper as rk  # noqa: E402

MB = 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_group(name, current, anon=0, reclaimable=0, protected=False,
               swapable=True, pids=1):
    """One entry shaped exactly like a published snapshot's group dict."""
    return {
        "name": name,
        "kind": "scope",
        "label": name,
        "current": current,
        "anon": anon,
        "file": reclaimable,
        "reclaimable_file": reclaimable,
        "swap": 0,
        "reclaimable": reclaimable,
        "protected": protected,
        "swapable": swapable,
        "pids": pids,
        "path": f"/sys/fs/cgroup/fake/{name}",
    }


def _zram_room():
    return {"devices": [{"name": "/dev/zram0", "ram_backed": True,
                         "size": 8192 * MB, "used": 0}]}


def make_snapshot(groups, state="critical", free_percent=5.0, total=8192 * MB,
                  available=400 * MB, forecast=None, zram=None):
    return {
        "state": state,
        "memory_reading_valid": True,
        "free_percent": free_percent,
        "mem_total": total,
        "mem_available": available,
        "mem_used": total - available,
        "groups": groups,
        "forecast": forecast,
        "zram": _zram_room() if zram is None else zram,
    }


class Recorder:
    """Replaces Steward.reclaim_from so no real cgroup is touched."""

    def __init__(self):
        self.asks = []          # (group name, bytes asked)
        self.cache_only = []
        self.budget_left = []

    def __call__(self, group, amount, cache_only=False):
        self.asks.append((group.name, amount))
        self.cache_only.append(bool(cache_only))
        return rk.Action(label=group.label, requested=amount,
                         freed=amount, swapped_out=0, outcome="ok")


def steward_with(**overrides):
    cfg = dict(rk.DEFAULT_CONFIG)
    cfg["reclaim_sleep"] = 0
    cfg.update(overrides)
    return rk.Steward(config=cfg)


@pytest.fixture
def recorder():
    return Recorder()


# ---------------------------------------------------------------------------
# 1. The cache pass must not eat the anon pass
# ---------------------------------------------------------------------------
# max_victim_slice() bounds a REQUEST. It was implemented by skipping a victim
# for the rest of the pass once it had been asked anything, which is stricter
# than the documented ceiling: every cgroup with >=16MB of droppable cache is
# asked in pass 1, and is therefore invisible to pass 2. On a desktop where the
# only thing that can really give memory back is anonymous pages, the anon pass
# never runs — the daemon reports "acted" and the machine stays under pressure.

def test_anon_pass_reaches_a_victim_that_already_gave_cache(monkeypatch, recorder):
    steward = steward_with(max_reclaim_per_pass=1024 * MB)
    monkeypatch.setattr(steward, "reclaim_from", recorder)

    # 50MB of droppable cache and 700MB of anon: the cache pass cannot possibly
    # exhaust this victim's per-pass slice, so anon must be reachable.
    snap = make_snapshot([make_group("app-big-1.scope", current=1024 * MB,
                                     anon=700 * MB, reclaimable=50 * MB)])
    steward.relieve(snap)

    cache_asks = [a for a in recorder.asks if a[0] == "app-big-1.scope"]
    assert cache_asks, "the cache pass asked for nothing at all"
    assert len(cache_asks) >= 2, (
        "the cache pass asked this victim once and the anon pass then skipped "
        f"it: asks={cache_asks}")
    assert sum(amount for _, amount in cache_asks) > 50 * MB, (
        "the pass stopped after the cache was drained, leaving anonymous memory "
        f"untouched: asks={cache_asks}")


def test_total_asked_per_victim_never_exceeds_its_slice(monkeypatch, recorder):
    steward = steward_with(max_victim_fraction=25, max_reclaim_per_pass=4096 * MB)
    monkeypatch.setattr(steward, "reclaim_from", recorder)

    current = 2048 * MB
    snap = make_snapshot(
        [make_group("app-big-2.scope", current=current,
                    anon=1900 * MB, reclaimable=140 * MB)],
        free_percent=1.0, available=100 * MB)
    steward.relieve(snap)

    slice_bytes = steward.max_victim_slice(current)
    asked = sum(amount for name, amount in recorder.asks if name == "app-big-2.scope")
    assert asked <= slice_bytes, (
        f"one victim was asked {asked // MB}MB in a single pass, "
        f"above its {slice_bytes // MB}MB slice")


def test_cache_is_asked_before_anon(monkeypatch, recorder):
    """Ordering is the policy: cheap and invisible pages go first."""
    steward = steward_with(max_reclaim_per_pass=4096 * MB)
    monkeypatch.setattr(steward, "reclaim_from", recorder)
    snap = make_snapshot([make_group("app-order-1.scope", current=1024 * MB,
                                     anon=800 * MB, reclaimable=200 * MB)])
    steward.relieve(snap)
    # With one victim and both kinds of memory, the first request must be the
    # whole droppable cache, i.e. the cache pass ran first.
    assert recorder.asks, "nothing was asked"
    assert recorder.asks[0][1] <= 200 * MB


# ---------------------------------------------------------------------------
# 2. A cgroup that vanished must not be reported as memory freed
# ---------------------------------------------------------------------------

def _fake_cgroup(tmp_path, current=100 * MB, with_reclaim_file=False):
    path = tmp_path / "app-gone-1.scope"
    path.mkdir()
    (path / "memory.current").write_text(str(current))
    (path / "memory.swap.current").write_text("0")
    if with_reclaim_file:
        (path / "memory.reclaim").write_text("")
    group = rk.Cgroup(path=path, name="app-gone-1.scope", kind="scope",
                      label="app-gone-1", current=current, anon=current,
                      reclaimable=0, swapable=True, pids=1)
    return path, group


def test_vanished_cgroup_reports_zero_freed(tmp_path):
    """The cgroup disappeared between scan() and the write.

    This is the normal lifecycle of a desktop app, not an error path. The group
    object still holds a path; every read of it now fails. Reporting that as
    "the whole footprint was freed" invents memory, and a pass can stop early
    believing its budget was spent.
    """
    import shutil
    steward = steward_with()
    path, group = _fake_cgroup(tmp_path, current=100 * MB, with_reclaim_file=True)
    shutil.rmtree(path)          # the app exited
    action = steward.reclaim_from(group, 50 * MB)
    assert action.freed == 0, (
        f"a vanished cgroup reported {action.freed // MB}MB freed")
    assert action.cgroup_freed == 0
    assert action.outcome == "gone", (
        f"a vanished cgroup was reported as {action.outcome!r}")


def test_trending_inside_the_margin_does_not_ask_for_the_floor():
    steward = steward_with(max_reclaim_per_pass=4096 * MB)
    recorder = Recorder()
    steward.reclaim_from = recorder
    total = 8192 * MB
    floor = int(total * 18 / 100)
    group = make_group("app-near.scope", current=800 * MB, anon=700 * MB,
                       reclaimable=100 * MB)
    close = make_snapshot(
        [group], state="trending", free_percent=18.9, total=total,
        available=floor + 76 * MB,
        forecast={"urgency": "imminent", "horizon_seconds": 40})
    result = steward.relieve(close)
    assert recorder.asks == []
    assert result["reason"] == "trending within floor margin"
    assert result["acted"] is False

    recorder.asks.clear()
    wide = make_snapshot(
        [group], state="trending", free_percent=22.0, total=total,
        available=floor + 300 * MB,
        forecast={"urgency": "imminent", "horizon_seconds": 40})
    steward.relieve(wide)
    asked = sum(amount for _, amount in recorder.asks)
    assert asked > 0
    assert asked < 512 * MB


def test_stalling_drops_cache_and_leaves_anonymous_memory():
    steward = steward_with(anon_reclaim_cooldown=0, max_reclaim_per_pass=4096 * MB,
                           max_victim_fraction=60)
    recorder = Recorder()
    steward.reclaim_from = recorder
    group = make_group("app-stall.scope", current=800 * MB, anon=700 * MB,
                       reclaimable=100 * MB)
    result = steward.relieve(make_snapshot([group], state="stalling", free_percent=20.0))
    assert recorder.asks
    assert recorder.cache_only == [True]
    assert result["actions"]


def test_stalling_with_imminent_forecast_becomes_trending(monkeypatch):
    steward = steward_with()
    total = 8000 * MB
    monkeypatch.setattr(rk, "meminfo", lambda: {
        "MemTotal": total, "MemAvailable": 2000 * MB, "Cached": 100 * MB})
    monkeypatch.setattr(rk, "psi", lambda resource="memory": {
        "available": True, "some_avg60": 8.0})
    monkeypatch.setattr(rk, "zram_stats", lambda: {"devices": []})
    monkeypatch.setattr(rk, "user_root", lambda: Path("/no-such-cgroup"))
    monkeypatch.setattr(rk, "scan", lambda *args, **kwargs: [])

    class _Imminent:
        urgency = "imminent"

        def as_dict(self):
            return {"urgency": "imminent", "horizon_seconds": 30}

    monkeypatch.setattr(steward.tracker, "forecast", lambda target: _Imminent())
    assert steward.snapshot()["state"] == "trending"


def test_gone_or_denied_does_not_spend_the_budget():
    class _Reply:
        def __init__(self):
            self.names = []

        def __call__(self, group, amount, cache_only=False):
            self.names.append(group.name)
            outcome = "gone" if "gone" in group.name else "denied" if "denied" in group.name else "ok"
            freed = 0 if outcome in ("gone", "denied") else amount
            return rk.Action(label=group.label, requested=amount, freed=freed,
                             swapped_out=0, outcome=outcome)

    steward = steward_with(max_reclaim_per_pass=200 * MB, max_victim_fraction=100,
                           anon_reclaim_cooldown=0, cache_first=False)
    reply = _Reply()
    steward.reclaim_from = reply
    groups = [
        make_group("app-gone.scope", current=800 * MB, anon=800 * MB, reclaimable=0),
        make_group("app-denied.scope", current=500 * MB, anon=500 * MB, reclaimable=0),
        make_group("app-live.scope", current=400 * MB, anon=400 * MB, reclaimable=0),
    ]
    steward.relieve(make_snapshot(groups))
    assert reply.names == ["app-gone.scope", "app-denied.scope", "app-live.scope"]


def test_cache_write_uses_swappiness_zero_and_falls_back(tmp_path, monkeypatch):
    import errno
    steward = steward_with()
    path, group = _fake_cgroup(tmp_path, current=100 * MB, with_reclaim_file=True)
    writes = []
    real_open = open

    def fake_open(file, mode="r", *args, **kwargs):
        if Path(file) == path / "memory.reclaim" and "w" in mode:
            class _Handle:
                def write(self, data):
                    writes.append(data)
                    if "swappiness=0" in data:
                        raise OSError(errno.EINVAL, "old kernel")
                    return len(data)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False
            return _Handle()
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    action = steward.reclaim_from(group, 32 * MB, cache_only=True)
    assert writes == [f"{32 * MB} swappiness=0", str(32 * MB)]
    assert action.outcome == "ok"

    writes.clear()
    action = steward.reclaim_from(group, 32 * MB, cache_only=False)
    assert writes == [str(32 * MB)]
    assert action.outcome == "ok"


def test_unreadable_after_value_is_not_treated_as_zero(tmp_path, monkeypatch):
    """A real 0 after a reclaim is meaningful; an unreadable file is not."""
    steward = steward_with()
    path, group = _fake_cgroup(tmp_path, current=100 * MB, with_reclaim_file=True)

    real_read_int = rk._read_int
    calls = {"n": 0}

    def flaky_read_int(p, default=0):
        if Path(p) == path / "memory.current":
            calls["n"] += 1
            if calls["n"] == 2:          # the after-read
                return default
        return real_read_int(p, default)

    monkeypatch.setattr(rk, "_read_int", flaky_read_int)
    action = steward.reclaim_from(group, 10 * MB)
    assert action.freed == 0, (
        "an unreadable memory.current after the write was reported as if the "
        f"cgroup had emptied: {action.freed // MB}MB")


# ---------------------------------------------------------------------------
# 3. Who may never be a victim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "init.scope",              # systemd --user itself
    "session-2.scope",         # the compositor's session
    "wayland-wm@hyprland.desktop.service",
    "ram-keeper.service",
    "quickshell",
])
def test_never_a_victim(name):
    assert rk.is_inviolable(name), f"{name} can be handed to a reclaim sweep"


def test_scan_marks_init_scope_protected(tmp_path):
    root = tmp_path / "user.slice" / "user-1000.slice"
    init = root / "init.scope"
    init.mkdir(parents=True)
    (init / "memory.current").write_text(str(200 * MB))
    (init / "memory.stat").write_text("anon 0\nfile 0\ninactive_file 0\n")
    (init / "pids.current").write_text("1\n")
    (init / "memory.swap.max").write_text("max\n")

    found = rk.scan(root, set(), 16 * MB)
    assert found, "scan found no groups at all"
    assert all(g.protected for g in found if g.name == "init.scope"), (
        "the user manager (init.scope) is offered as a reclaim victim")


# ---------------------------------------------------------------------------
# 4. Names in the config must actually match real cgroup names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cgroup,wanted", [
    ("app-Spotify-1043.scope", "spotify"),
    ("app-spotify-1043.scope", "spotify"),
    ("app-org.chromium.Chromium-991.scope", "chromium"),
    ("app-Hyprland-gtk\\x2dlaunch-1aa92790.scope", "hyprland-gtk-launch"),
    ("syncthing.service", "syncthing"),
    ("ollama", "ollama.service"),
])
def test_config_names_match_real_cgroups(cgroup, wanted):
    assert rk.names_match(cgroup, {wanted}), (
        f"a user protecting {wanted!r} would not protect {cgroup!r}")


@pytest.mark.parametrize("cgroup,wanted", [
    ("app-firefox-1.scope", "spotify"),
    ("syncthing.service", "syncthing-server"),
    ("syncthing-server.service", "syncthing"),
    ("pipewire-pulse.service", "pipewire.service"),
    ("hermes-gateway.service", "hermes"),
    ("siamese.service", "sia"),
    ("asia.service", "sia"),
])
def test_config_names_do_not_over_match(cgroup, wanted):
    assert not rk.names_match(cgroup, {wanted})


@pytest.mark.parametrize("cgroup,wanted", [
    ("pipewire.service", "pipewire.service"),
    ("syncthing.service", "syncthing.service"),
    ("ollama.service", "ollama.service"),
    ("hermes.service", "hermes"),
    ("app-hermes-1043.scope", "hermes"),
    ("hermes-gateway.service", "hermes-gateway"),
    ("sia.service", "sia"),
    ("app-sia-1043.scope", "sia"),
])
def test_documented_units_match_the_real_cgroup(cgroup, wanted):
    assert rk.names_match(cgroup, {wanted})


def test_defaults_protect_what_the_docs_name():
    protected = set(rk.DEFAULT_CONFIG["protected"])
    for name in ("pipewire.service", "syncthing.service", "ollama.service",
                 "hermes", "hermes-gateway", "sia"):
        assert name in protected
    for cgroup in ("pipewire.service", "syncthing.service", "ollama.service",
                   "hermes.service", "hermes-gateway.service", "sia.service"):
        assert rk.names_match(cgroup, protected)
    for stranger in ("pipewire-pulse.service", "syncthing-server.service",
                     "siamese.service"):
        assert not rk.names_match(stranger, protected)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_anon_cooldown_holds_while_an_untouchable_culprit_keeps_pressure():
    """Several passes, free memory stays critical, the hog cannot be touched."""
    clock = _Clock()
    steward = steward_with(anon_reclaim_cooldown=90, max_reclaim_per_pass=4096 * MB,
                           max_victim_fraction=60)
    steward._clock = clock
    recorder = Recorder()
    steward.reclaim_from = recorder
    hog = make_group("untouchable.scope", current=6800 * MB, anon=6800 * MB,
                     reclaimable=0, protected=True)
    browser = make_group("app-browser.scope", current=800 * MB, anon=700 * MB,
                         reclaimable=0)
    snap = make_snapshot([hog, browser], state="critical", free_percent=5.0)

    def asked(name):
        return sum(amount for victim, amount in recorder.asks if victim == name)

    steward.relieve(snap)
    assert asked("untouchable.scope") == 0
    assert asked("app-browser.scope") > 0
    first = asked("app-browser.scope")

    clock.now = 5
    steward.relieve(snap)
    assert asked("app-browser.scope") == first, (
        "a second pass inside the cooldown drained the same app again")

    clock.now = 10
    steward.relieve(snap)
    assert asked("app-browser.scope") == first

    clock.now = 90
    steward.relieve(snap)
    assert asked("app-browser.scope") > first, (
        "the pause never expired")

    after_wait = asked("app-browser.scope")
    clock.now = 95
    steward.relieve(snap, force=True)
    assert asked("app-browser.scope") > after_wait, (
        "the manual button was blocked by the pause")


def test_anon_pass_requires_free_zram():
    steward = steward_with(max_reclaim_per_pass=4096 * MB, max_victim_fraction=60,
                           anon_reclaim_cooldown=0)
    recorder = Recorder()
    steward.reclaim_from = recorder
    group = make_group("app-disk.scope", current=800 * MB, anon=700 * MB,
                       reclaimable=100 * MB)
    full = {"devices": [{"name": "/dev/zram0", "ram_backed": True,
                         "size": 200 * MB, "used": 200 * MB}]}
    disk = {"devices": [{"name": "/swap/swapfile", "ram_backed": False,
                         "size": 8192 * MB, "used": 0}]}
    steward.relieve(make_snapshot([group], zram=full))
    assert [amount for _, amount in recorder.asks] == [100 * MB], recorder.asks
    recorder.asks.clear()
    steward.relieve(make_snapshot([group], zram=disk))
    assert [amount for _, amount in recorder.asks] == [100 * MB], recorder.asks
    recorder.asks.clear()
    steward.relieve(make_snapshot([group], zram={"devices": []}))
    assert [amount for _, amount in recorder.asks] == [100 * MB]
    recorder.asks.clear()
    steward.relieve(make_snapshot([group]))
    assert [name for name, _ in recorder.asks] == ["app-disk.scope", "app-disk.scope"]
    assert recorder.asks[0][1] == 100 * MB
    assert recorder.asks[1][1] >= 16 * MB


def test_scan_honours_protection_by_short_name(tmp_path):
    root = tmp_path / "user.slice" / "user-1000.slice"
    app = root / "app-Spotify-1043.scope"
    app.mkdir(parents=True)
    (app / "memory.current").write_text(str(300 * MB))
    (app / "memory.stat").write_text("anon 100\nfile 200\ninactive_file 150\n")
    (app / "pids.current").write_text("12\n")
    (app / "memory.swap.max").write_text("max\n")

    found = rk.scan(root, {"spotify"}, 16 * MB)
    assert found and found[0].protected, (
        "protected: ['spotify'] did not protect app-Spotify-1043.scope")


def test_never_reclaim_is_also_matched_by_short_name(tmp_path, monkeypatch):
    steward = steward_with(never_reclaim=["spotify"], max_reclaim_per_pass=1024 * MB)
    rec = Recorder()
    monkeypatch.setattr(steward, "reclaim_from", rec)
    snap = make_snapshot([make_group("app-Spotify-1043.scope", current=512 * MB,
                                     anon=400 * MB, reclaimable=100 * MB)])
    steward.relieve(snap)
    assert not rec.asks, "a never_reclaim entry was reclaimed anyway"


# ---------------------------------------------------------------------------
# 5. Documented knobs must exist in the config
# ---------------------------------------------------------------------------

def test_reclaim_sleep_is_a_real_setting(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"reclaim_sleep": 0.05}))
    monkeypatch.setattr(rk, "config_path", lambda: cfg_file)
    cfg = rk.load_config()
    assert cfg.get("reclaim_sleep") == 0.05, (
        "reclaim_sleep is read by reclaim_from() but rejected by load_config() "
        "as an unknown setting, so the documented knob never takes effect")
    assert not any("reclaim_sleep" in w and "unknown" in w
                   for w in rk.config_warnings())


def test_reclaim_sleep_is_clamped(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"reclaim_sleep": 999}))
    monkeypatch.setattr(rk, "config_path", lambda: cfg_file)
    cfg = rk.load_config()
    assert 0 <= cfg["reclaim_sleep"] <= 5.0


def test_reclaim_from_uses_the_configured_sleep(tmp_path, monkeypatch):
    steward = steward_with(reclaim_sleep=0.123)
    _path, group = _fake_cgroup(tmp_path, current=100 * MB, with_reclaim_file=True)
    slept = []
    monkeypatch.setattr(rk.time, "sleep", lambda s: slept.append(s))
    steward.reclaim_from(group, 10 * MB)
    assert slept == [0.123], f"reclaim_from slept {slept}"


# ---------------------------------------------------------------------------
# 6. The published snapshot must not lie about what it can touch
# ---------------------------------------------------------------------------

def test_never_reclaim_is_excluded_from_the_published_victim_total(tmp_path, monkeypatch):
    """reclaimable_total must describe what a pass may actually touch."""
    steward = steward_with(never_reclaim=["localsearch-3.service"])
    groups = [
        rk.Cgroup(path=tmp_path / "a", name="app-A-1.scope", kind="scope",
                  label="A", current=500 * MB, anon=0, reclaimable=120 * MB),
        rk.Cgroup(path=tmp_path / "b", name="localsearch-3.service", kind="service",
                  label="ls", current=500 * MB, anon=0, reclaimable=300 * MB),
    ]
    monkeypatch.setattr(rk, "user_root", lambda: tmp_path)
    monkeypatch.setattr(rk, "scan", lambda root, protected, min_bytes: groups)
    monkeypatch.setattr(rk, "meminfo", lambda: {"MemTotal": 8192 * MB,
                                                "MemAvailable": 4096 * MB,
                                                "Cached": 100 * MB})
    snap = steward.snapshot()
    assert snap["reclaimable_total"] == 120 * MB, (
        "a never_reclaim cgroup was counted as reclaimable: "
        f"{snap['reclaimable_total'] // MB}MB")


# ---------------------------------------------------------------------------
# 7. Reported "freed" must be RAM, not the cgroup's drop
# ---------------------------------------------------------------------------
# A cgroup that moves 100MB of anon to zram drops 100MB of `memory.current`, but
# zram then occupies 100/ratio MB of real RAM. The correction was multiplied by
# the ratio instead of divided by it, which made every swap-heavy reclaim report
# zero (clamped from a negative number): the panel said "nothing to do" after a
# pass that genuinely returned memory. Measured live: 150MB reclaimed, 0MB
# reported.

def _settle_into(path, after_current, after_swap):
    def settle(self):
        (path / "memory.current").write_text(str(after_current))
        (path / "memory.swap.current").write_text(str(after_swap))
    return settle


def test_anon_reclaim_reports_ram_net_of_compression(tmp_path, monkeypatch):
    steward = steward_with()
    path, group = _fake_cgroup(tmp_path, current=300 * MB, with_reclaim_file=True)
    monkeypatch.setattr(rk.Steward, "zram_ratio", staticmethod(lambda: 4.0))
    monkeypatch.setattr(rk.Steward, "_settle",
                        _settle_into(path, 200 * MB, 100 * MB))
    action = steward.reclaim_from(group, 100 * MB)
    assert action.cgroup_freed == 100 * MB
    assert action.swapped_out == 100 * MB
    assert action.freed == 75 * MB, (
        "expected 100MB moved minus 25MB held by zram, got "
        f"{action.freed // MB}MB")
    assert action.freed > 0, "a pass that moved 100MB must not report zero"


def test_anon_reclaim_with_no_compression_reports_zero(tmp_path, monkeypatch):
    """ratio 1.0 means zram costs as much RAM as it returned: net zero, honestly."""
    steward = steward_with()
    path, group = _fake_cgroup(tmp_path, current=300 * MB, with_reclaim_file=True)
    monkeypatch.setattr(rk.Steward, "zram_ratio", staticmethod(lambda: 1.0))
    monkeypatch.setattr(rk.Steward, "_settle",
                        _settle_into(path, 200 * MB, 100 * MB))
    assert steward.reclaim_from(group, 100 * MB).freed == 0


def test_cache_only_reclaim_reports_the_full_amount(tmp_path, monkeypatch):
    steward = steward_with()
    path, group = _fake_cgroup(tmp_path, current=300 * MB, with_reclaim_file=True)
    monkeypatch.setattr(rk.Steward, "zram_ratio", staticmethod(lambda: 4.0))
    monkeypatch.setattr(rk.Steward, "_settle",
                        _settle_into(path, 220 * MB, 0))
    action = steward.reclaim_from(group, 80 * MB)
    assert action.swapped_out == 0
    assert action.freed == 80 * MB, f"{action.freed // MB}MB"
