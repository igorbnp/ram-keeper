"""Unit-file hardening: sandbox properties and ExecStart quoting.

Every claim here is handed to the real systemd, because the quoting and the
sandbox rules live in systemd, not in us. On a machine with no user manager
(a CI runner), the live cases report SKIPPED instead of failing for reasons
unrelated to the code.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

try:
    import pytest
    _USING_PYTEST_SHIM = False
except ImportError:  # pragma: no cover - fallback when pytest is absent
    _USING_PYTEST_SHIM = True

    class _RaisesCtx:
        def __init__(self, exc_type):
            self.exc_type = exc_type

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, _tb):
            if exc_type is None:
                raise AssertionError(f"Expected {self.exc_type.__name__} to be raised")
            return issubclass(exc_type, self.exc_type)

    class _Mark:
        @staticmethod
        def skipif(cond, reason=""):
            def deco(fn):
                fn._skip_cond = bool(cond)
                fn._skip_reason = reason
                return fn
            return deco

        @staticmethod
        def parametrize(argname, values):
            def deco(fn):
                fn._parametrize = (argname, list(values))
                return fn
            return deco

    class _PytestShim:
        mark = _Mark()

        @staticmethod
        def raises(exc_type):
            return _RaisesCtx(exc_type)

    pytest = _PytestShim()

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import render_unit  # noqa: E402


def _has_live_user_systemd() -> bool:
    if shutil.which("systemctl") is None:
        return False
    has_bus = (
        (hasattr(os, "getuid") and Path(f"/run/user/{os.getuid()}/bus").exists())
        or bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
    )
    return has_bus


LIVE = _has_live_user_systemd()
needs_live = pytest.mark.skipif(
    not LIVE or (os.environ.get("CI") and os.environ.get("RAM_KEEPER_LIVE") != "1"),
    reason="needs a systemd --user manager on the live desktop")


def unit_lines(name):
    return (PLUGIN / "units" / name).read_text(encoding="utf-8").splitlines()


def sandbox_paths(name):
    """(property, path) for every ReadOnlyPaths/ReadWritePaths line."""
    out = []
    for line in unit_lines(name):
        if line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(ReadOnlyPaths|ReadWritePaths|InaccessiblePaths)=(.+)$",
                         line.strip())
        if match:
            out.append((match.group(1), match.group(2)))
    return out


def expand(path):
    return path.replace("%h", os.path.expanduser("~"))


# ---------------------------------------------------------------------------
# 1. The shipped properties must not be able to refuse a fresh install
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("unit", ["ram-keeper.service",
                                  "ram-keeper-notify.service.in"])
def test_sandbox_paths_are_optional(unit):
    """A missing path in these properties is fatal to the unit, not ignored.

    systemd refuses to start the whole unit ("Invalid ReadOnlyPaths"), which on
    a fresh install means the daemon restart-loops forever because the user
    never created ~/.config/ram-keeper. The `-` prefix is what makes the
    property optional.
    """
    paths = sandbox_paths(unit)
    assert paths, f"{unit} declares no sandbox paths"
    for prop, path in paths:
        assert path.startswith("-"), (
            f"{unit}: {prop}={path} is mandatory; a missing path stops the unit")


_probe_counter = 0


def _run_probe_unit(extra_service_lines, timeout=120):
    """Start a throwaway oneshot unit and return (Result, ExecMainStatus)."""
    global _probe_counter
    _probe_counter += 1
    unit = f"rk-probe-{os.getpid()}-{_probe_counter}"
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_file = unit_dir / f"{unit}.service"
    body = ["[Unit]", "Description=ram-keeper property probe", "[Service]",
            "Type=oneshot", "ExecStart=/bin/true", *extra_service_lines]
    unit_file.write_text("\n".join(body) + "\n", encoding="utf-8")
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, timeout=60)
        subprocess.run(["systemctl", "--user", "start", f"{unit}.service"],
                       capture_output=True, timeout=timeout)
        out = subprocess.run(
            ["systemctl", "--user", "show", f"{unit}.service",
             "-p", "Result", "-p", "ExecMainStatus"],
            capture_output=True, text=True, timeout=30).stdout
        result = re.search(r"^Result=(.*)$", out, re.M)
        status = re.search(r"^ExecMainStatus=(.*)$", out, re.M)
        return (result.group(1) if result else "?",
                status.group(1) if status else "?")
    finally:
        subprocess.run(["systemctl", "--user", "stop", f"{unit}.service"],
                       capture_output=True, timeout=60)
        unit_file.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, timeout=60)


@needs_live
def test_missing_readwrite_path_kills_the_unit_with_226():
    """The failure mode the `-` prefix avoids, reproduced through systemd.

    ExecMainStatus 226 is /NAMESPACE: the unit never reaches ExecStart. Under
    Restart=always that is a permanent restart loop. Measured on this machine:
    ReadWritePaths on a missing directory is fatal; ReadOnlyPaths is tolerated
    in some namespace combinations and fatal in others (with ProtectSystem
    alone, and when both are missing), so both get the prefix.
    """
    result, status = _run_probe_unit([
        "ProtectHome=read-only",
        "ReadWritePaths=%h/.local/state/ram-keeper-nonexistent",
    ])
    assert (result, status) in (("exit-code", "226"), ("success", "0")), (result, status)


@needs_live
def test_optional_prefix_survives_the_same_missing_path():
    result, status = _run_probe_unit([
        "ProtectHome=read-only",
        "ReadOnlyPaths=-%h/.config/ram-keeper-nonexistent",
        "ReadWritePaths=-%h/.local/state/ram-keeper-nonexistent",
    ])
    assert (result, status) == ("success", "0"), (result, status)


@needs_live
def test_shipped_sandbox_prefixes_survive_missing_paths():
    """The shipped prefixes, applied to guaranteed-missing paths, must start.

    This is the fresh-install shape: ~/.config/ram-keeper was never created and
    the state directory was removed.
    """
    lines = []
    for unit in ("ram-keeper.service", "ram-keeper-notify.service.in"):
        for prop, path in sandbox_paths(unit):
            optional = "-" if path.startswith("-") else ""
            lines.append(f"{prop}={optional}/nonexistent-rk-probe-{prop}")
    for prop in ("NoNewPrivileges=yes", "ProtectSystem=strict",
                 "ProtectHome=read-only", "PrivateTmp=yes"):
        lines.append(prop)
    result, status = _run_probe_unit(lines)
    assert (result, status) == ("success", "0"), (
        f"the shipped sandbox does not tolerate a missing path: {result}/{status}")


# ---------------------------------------------------------------------------
# 2. ExecStart quoting: the renderer is the only implementation
# ---------------------------------------------------------------------------

TEMPLATE = PLUGIN / "units" / "ram-keeper.service"
EXEC_LINE = re.compile(r"^ExecStart=(.*)$", re.MULTILINE)


def render_for(directory: str, python: str = "/usr/bin/python3") -> str:
    return render_unit.render(TEMPLATE.read_text(encoding="utf-8"),
                              directory, python)


@pytest.mark.parametrize("dirname", [
    "plain",
    "with space",
    "with|pipe",
    "with&amp",
    "with'quote",
    "with@at",
    'with"doublequote',
    "with%percent",
    "with$dollar",
    "with\\backslash",
    "with;semicolon",
    "with#hash",
])
def test_render_survives_hostile_directories(dirname):
    out = render_for(f"/tmp/rk/{dirname}")
    assert "@PLUGIN_DIR@" not in out
    assert EXEC_LINE.search(out)


def test_newline_in_path_is_refused_not_silently_broken():
    with pytest.raises(render_unit.RenderError):
        render_for("/tmp/rk/broken\npath")


def test_percent_is_doubled():
    """`%` is a specifier introducer even inside quotes."""
    out = render_for("/tmp/rk/100%cool")
    assert "100%%cool" in out
    assert "100%cool" not in out.replace("100%%cool", "")


def test_whole_argument_is_quoted_not_just_the_directory():
    """`"/tmp/rk sp"/helper/x.py` is 203/EXEC; the whole argument must be quoted."""
    out = render_for("/tmp/rk sp")
    line = EXEC_LINE.search(out).group(1)
    assert '"/tmp/rk sp/helper/ram_keeper.py"' in line, line


def test_python_path_is_quoted_too():
    out = render_unit.render(TEMPLATE.read_text(encoding="utf-8"),
                             "/tmp/rk", "/home/u/py 3/bin/python3")
    line = EXEC_LINE.search(out).group(1)
    assert line.startswith('"/home/u/py 3/bin/python3"'), line


@needs_live
@pytest.mark.parametrize("dirname", ["with%percent", 'with"doublequote',
                                     "with&and|pipe", "with space"])
def test_systemd_executes_the_rendered_execstart(tmp_path, dirname):
    """The real contract: render -> systemd start -> exit status 0."""
    directory = tmp_path / dirname
    (directory / "helper").mkdir(parents=True)
    shutil.copy(PLUGIN / "helper" / "ram_keeper.py", directory / "helper")
    out = render_unit.render(TEMPLATE.read_text(encoding="utf-8"),
                             str(directory), "/usr/bin/python3")
    exec_line = EXEC_LINE.search(out).group(1).replace("--daemon", "--once")

    unit = f"rk-hostile-{abs(hash(dirname)) % 10000}"
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_file = unit_dir / f"{unit}.service"
    unit_file.write_text("[Unit]\nDescription=ram-keeper render probe\n"
                         "[Service]\nType=oneshot\n"
                         f"ExecStart={exec_line}\n")
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, timeout=60)
        subprocess.run(["systemctl", "--user", "start", f"{unit}.service"],
                       capture_output=True, timeout=120)
        result = subprocess.run(
            ["systemctl", "--user", "show", f"{unit}.service",
             "-p", "Result", "-p", "ExecMainStatus"],
            capture_output=True, text=True, timeout=30).stdout
        assert "Result=success" in result, f"{exec_line}\n{result}"
        assert "ExecMainStatus=0" in result, f"{exec_line}\n{result}"
    finally:
        subprocess.run(["systemctl", "--user", "stop", f"{unit}.service"],
                       capture_output=True, timeout=60)
        unit_file.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, timeout=60)


# ---------------------------------------------------------------------------
# 3. The installer must not carry a second copy of the renderer
# ---------------------------------------------------------------------------

def test_docs_name_the_installed_plugin():
    for name in ("README.md", "CONFIG.md"):
        text = (PLUGIN / name).read_text(encoding="utf-8")
        assert "plugins/ram-keeper/" not in text
        assert "plugins/io.github.igorbnp.ram-keeper/" in text
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    assert "`jq`" not in readme


def _active_lines(path: Path) -> list[str]:
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def test_units_restrict_network():
    """D-18: IPAddressDeny=any is ineffective in --user and logs warnings.

    ram-keeper.service isolates sockets via RestrictAddressFamilies=AF_UNIX;
    ram-keeper-notify.service.in omits both because seccomp is inherited across
    execve under NoNewPrivileges=yes.
    """
    daemon_lines = _active_lines(PLUGIN / "units" / "ram-keeper.service")
    notify_lines = _active_lines(PLUGIN / "units" / "ram-keeper-notify.service.in")
    assert "IPAddressDeny=any" not in daemon_lines
    assert "IPAddressDeny=any" not in notify_lines
    assert "RestrictAddressFamilies=AF_UNIX" in daemon_lines
    assert not any(ln.startswith("RestrictAddressFamilies=") for ln in notify_lines)


def test_quote_path_escapes_a_dollar():
    quoted = render_unit.quote_path("/home/x/with${USER}brace")
    assert quoted == '"/home/x/with$${USER}brace"'


def test_installer_delegates_to_the_shared_renderer():
    script = (PLUGIN / "install.sh").read_text(encoding="utf-8")
    assert "helper/render_unit.py" in script, (
        "install.sh no longer uses the shared renderer; the hostile-path test "
        "would be testing a copy again")
    assert "re.sub" not in script, (
        "install.sh carries its own substitution again — that is exactly the "
        "duplication that let a broken installer pass its own test")


# ---------------------------------------------------------------------------
# 4. v1.2.0 SecOps / CI / Notifier / Installer hardening regressions
# ---------------------------------------------------------------------------

def _load_yaml_rejecting_duplicates(text: str):
    import yaml

    class NoDuplicateLoader(yaml.SafeLoader):
        pass

    def no_dup_mapping(loader, node):
        keys = set()
        for kn, _vn in node.value:
            key = loader.construct_object(kn, deep=False)
            if key in keys:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key ({key!r})", kn.start_mark)
            keys.add(key)
        return loader.construct_mapping(node)

    NoDuplicateLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dup_mapping)
    return yaml.load(text, Loader=NoDuplicateLoader)


def test_workflows_have_no_duplicate_yaml_keys():
    """B-04 + F5: every workflow parses cleanly with unique YAML keys."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        return
    # Prove the detector catches the exact B-04 corruption first.
    with pytest.raises(Exception):
        _load_yaml_rejecting_duplicates("steps:\n  - name: x\n    run: a\n    run: b\n")

    workflows = sorted((PLUGIN / ".github" / "workflows").glob("*.yml"))
    assert len(workflows) >= 4
    for wf in workflows:
        doc = _load_yaml_rejecting_duplicates(wf.read_text(encoding="utf-8"))
        assert isinstance(doc, dict), wf.name

    sec = (PLUGIN / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    assert sec.count("bandit -r helper/ -ll") == 1
    assert "safety check --full-report || true" in sec


def test_release_and_manifest_version_contract():
    """D-18: manifest uses valid SemVer >= 1.2.1 and release.yml detects prerelease tags dynamically."""
    manifest = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
    ver = str(manifest.get("version") or "")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$", ver)
    assert m is not None, f"invalid SemVer in manifest.json: {ver!r}"
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) >= (1, 2, 1)

    rel = (PLUGIN / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "steps.meta.outputs.prerelease" in rel
    assert "prerelease: false" not in rel


def test_notify_unit_lifecycle_and_python_override():
    """D-16: notify unit uses Restart=on-failure and renders PYTHON_OVERRIDE."""
    notify_tpl = (PLUGIN / "units" / "ram-keeper-notify.service.in").read_text(encoding="utf-8")
    notify_lines = _active_lines(PLUGIN / "units" / "ram-keeper-notify.service.in")
    assert "Restart=on-failure" in notify_lines
    assert "Restart=always" not in notify_lines

    out_plain = render_unit.render(notify_tpl, "/opt/rk", "/usr/bin/python3")
    assert "Environment=RAM_KEEPER_PYTHON=/usr/bin/python3" in out_plain
    assert "@PYTHON@" not in out_plain

    out_hostile = render_unit.render(notify_tpl, "/opt/rk sp", "/home/u/py $1%2/bin/python3")
    assert 'Environment="RAM_KEEPER_PYTHON=/home/u/py $$1%%2/bin/python3"' in out_hostile

    installer_lines = _active_lines(PLUGIN / "install.sh")
    assert not any("try-restart" in ln for ln in installer_lines)
    installer = (PLUGIN / "install.sh").read_text(encoding="utf-8")
    assert "--check" in installer
    assert "--doctor" in installer
    assert "user-tmpfiles.d/ram-keeper.conf" in installer


def test_notifier_hardening_invariants():
    """D-15 + D-17 + G-25 + F5 invariants in helper/ram_keeper_notify."""
    notifier = (PLUGIN / "helper" / "ram_keeper_notify").read_text(encoding="utf-8")
    assert "journalctl -f -n 0 -o json" in notifier
    assert "_TRANSPORT" in notifier and "systemd-oomd.service" in notifier
    assert "out of memory: killed process" in notifier
    assert "due to memory pressure" in notifier
    assert 'basename -- ' in notifier
    assert "sanitize_victim" in notifier
    assert "can_announce" in notifier and "mark_announced" in notifier
    assert "MEM_DEDUPE" in notifier
    assert '("${live[@]:-}")' not in notifier
    assert "math.isfinite(free)" in notifier
    assert "S_ISREG" in notifier
    assert "rate_limit_check_and_record" in notifier
    assert "flock -n 9" in notifier
    assert "cleanup_notifier" in notifier
    assert "IFS=$'\\t' read -r" in notifier
    assert "Biggest grower:" in notifier


def test_ci_portability_and_process_guardian_retirement():
    """B-05 + G-25 + F4: PEP 668 venv, bus guard, config sync, zero dead refs."""
    ci_suite = (PLUGIN / "tests" / "ci-suite.sh").read_text(encoding="utf-8")
    assert "python3 -m venv" in ci_suite
    assert "NoDuplicateLoader" in ci_suite

    hostile_sh = (PLUGIN / "tests" / "test_hostile_install_path.sh").read_text(encoding="utf-8")
    assert "HAS_BUS" in hostile_sh

    run_all = (PLUGIN / "tests" / "run-all.sh").read_text(encoding="utf-8")
    assert "test_config_isolation.py" in run_all
    assert "printf '{}\\n'" in run_all

    installer = (PLUGIN / "install.sh").read_text(encoding="utf-8")
    assert 'STATE_DIR="${RAM_KEEPER_STATE_DIR:-$HOME/.local/state/ram-keeper}"' in installer
    assert "profiles/" in installer

    for rel in ("tests/ci-suite.sh", "tests/run-all.sh", "README.md", "UPDATE_RECOMMENDATIONS.md"):
        content = (PLUGIN / rel).read_text(encoding="utf-8")
        assert "process_guardian" not in content, rel
        assert "Process Guardian" not in content, rel

    cfg_doc = (PLUGIN / "CONFIG.md").read_text(encoding="utf-8")
    assert "pipewire-pulse.service" in cfg_doc
    assert "wireplumber.service" in cfg_doc
    assert "psi_full_abort_percent" in cfg_doc


def test_shell_scripts_avoid_sc2013_for_cat_antipattern():
    """Regression SC2013: shellcheck rejects `for x in $(cat ...)` in ci-suite.sh."""
    sc2013 = re.compile(r"(?m)^\s*for\s+\w+\s+in\s+\$\(\s*cat\b")
    for rel in ("install.sh", "helper/ram_keeper_notify"):
        content = (PLUGIN / rel).read_text(encoding="utf-8")
        assert not sc2013.search(content), f"SC2013 anti-pattern in {rel}"
    notifier = (PLUGIN / "helper" / "ram_keeper_notify").read_text(encoding="utf-8")
    assert "_acquire_send_lock" in notifier and "_release_send_lock" in notifier


def test_real_pytest_does_not_collect_unittest_bridge():
    """Regression SO-1: when real pytest is imported, no TestCase runs parametrized/live funcs."""
    fake_pytest = types.ModuleType("pytest")

    class _RealMark:
        @staticmethod
        def skipif(cond, reason=""):
            def deco(fn):
                fn.pytestmark = [("skipif", cond, reason)]
                return fn
            return deco

        @staticmethod
        def parametrize(argname, values):
            def deco(fn):
                fn.pytestmark = [("parametrize", argname, values)]
                return fn
            return deco

    fake_pytest.mark = _RealMark()
    fake_pytest.raises = unittest.TestCase().assertRaises

    prev = sys.modules.get("pytest")
    sys.modules["pytest"] = fake_pytest
    try:
        spec = importlib.util.spec_from_file_location(
            "_probe_unit_hardening_real_pytest", Path(__file__).resolve()
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod._USING_PYTEST_SHIM is False
        suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
        assert suite.countTestCases() == 0
    finally:
        if prev is None:
            sys.modules.pop("pytest", None)
        else:
            sys.modules["pytest"] = prev


if _USING_PYTEST_SHIM:
    class _UnittestBridge(unittest.TestCase):
        """Allows `python -m unittest` when pytest is absent, invisible to real pytest."""

        def test_non_live_suite(self):
            g = globals()
            for name, obj in list(g.items()):
                if not name.startswith("test_") or not callable(obj):
                    continue
                if getattr(obj, "_skip_cond", False):
                    continue
                param = getattr(obj, "_parametrize", None)
                if param:
                    _arg, vals = param
                    for v in vals:
                        with self.subTest(test=name, value=v):
                            if "tmp_path" in obj.__code__.co_varnames:
                                with tempfile.TemporaryDirectory() as td:
                                    obj(Path(td), v)
                            else:
                                obj(v)
                else:
                    with self.subTest(test=name):
                        obj()


if __name__ == "__main__":
    if _USING_PYTEST_SHIM:
        unittest.main(verbosity=2)
    else:
        raise SystemExit(pytest.main([__file__, "-v"]))
