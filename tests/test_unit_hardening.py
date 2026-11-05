"""Unit-file hardening: sandbox properties and ExecStart quoting.

Every claim here is handed to the real systemd, because the quoting and the
sandbox rules live in systemd, not in us. On a machine with no user manager
(a CI runner), the live cases report SKIPPED instead of failing for reasons
unrelated to the code.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))

import render_unit  # noqa: E402

LIVE = (Path(f"/run/user/{os.getuid()}/bus").exists()
        or bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")))
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
    assert (result, status) == ("exit-code", "226"), (result, status)


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
