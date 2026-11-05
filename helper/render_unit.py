#!/usr/bin/env python3
"""Render a systemd unit template. The ONLY implementation of this step.

Why this is a separate file: the hostile-path test used to carry its own copy of
the substitution, so it passed while the shipped installer failed on a directory
containing a space. A test that reimplements the thing it is testing proves
nothing. Both the installer and the test now execute this script.

Rules, each one learned from a real failure:

* Substitution is `str.replace`/regex over LITERAL text, never `sed` (a path
  containing `|` is parsed as extra sed options, and the redirect has already
  truncated the destination by then) and never `${var//pat/rep}` (`&` is
  special in the pattern).
* An argument is quoted unless every character is safe. systemd splits
  ExecStart on whitespace and honours double quotes only around a WHOLE
  argument.
* Inside double quotes, `\\` and `\\"` are escapes, so both must be escaped.
* `%` is a specifier introducer even inside quotes: `%h`, `%i`, `%c`. A
  directory named `100%cool` renders as `%%` or systemd either expands the
  wrong thing or refuses the unit outright.
* A newline cannot be represented in a unit file at all. Refuse it instead of
  writing a unit that parses as two lines and fails at the second.

Usage:
    render_unit.py SRC DST PLUGIN_DIR PYTHON
    render_unit.py --stdout SRC PLUGIN_DIR PYTHON
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "/._+-:@,=~"
)


class RenderError(Exception):
    pass


def _check(arg: str, what: str) -> None:
    if "\n" in arg or "\r" in arg or "\0" in arg:
        raise RenderError(
            f"{what} contains a newline or NUL byte, which a unit file cannot "
            f"represent: {arg!r}")


def quote_path(arg: str, what: str = "path") -> str:
    """One systemd argument: literal, escaped and quoted when it needs to be."""
    _check(arg, what)
    escaped = (arg.replace("\\", "\\\\").replace('"', '\\"')
               .replace("%", "%%").replace("$", "$$"))
    if all(ch in SAFE_CHARS for ch in arg):
        return escaped                      # nothing to quote, nothing to escape
    return '"' + escaped + '"'


def render(content: str, plugin_dir: str, python: str) -> str:
    _check(plugin_dir, "plugin directory")
    _check(python, "python path")
    if not content.strip():
        raise RenderError("template is empty")

    # @PLUGIN_DIR@ plus whatever subpath follows it in the template, quoted as
    # ONE argument: quoting only the directory gives `"/tmp/rk sp"/helper/x.py`,
    # which systemd reads as one argument containing literal quotes -> 203/EXEC.
    rendered = re.sub(
        r"@PLUGIN_DIR@(\S*)",
        lambda m: quote_path(plugin_dir + m.group(1), "plugin path"),
        content,
    )

    # The interpreter is a path argument too. Only the ExecStart position is
    # rewritten, so a comment mentioning the default path keeps saying what it
    # says.
    rendered = re.sub(
        r"(?m)^(ExecStart=)/usr/bin/python3(?=\s|$)",
        lambda m: m.group(1) + quote_path(python, "python path"),
        rendered,
    )

    if "@PLUGIN_DIR@" in rendered:
        raise RenderError("placeholder survived rendering")
    return rendered


def main(argv: list[str]) -> int:
    to_stdout = "--stdout" in argv
    args = [a for a in argv[1:] if a != "--stdout"]
    if to_stdout:
        if len(args) != 3:
            print(__doc__, file=sys.stderr)
            return 2
        src, plugin_dir, python = args
        dst = None
    else:
        if len(args) != 4:
            print(__doc__, file=sys.stderr)
            return 2
        src, dst, plugin_dir, python = args

    try:
        content = Path(src).read_text(encoding="utf-8")
        rendered = render(content, plugin_dir, python)
    except (OSError, RenderError) as exc:
        print(f"render_unit: {exc}", file=sys.stderr)
        return 1

    if dst is None:
        sys.stdout.write(rendered)
        return 0

    # Write-then-rename: a failed render must never leave a truncated unit
    # behind, because systemd would happily load the empty file.
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent),
        prefix=target.name + ".", suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(rendered)
        os.replace(handle.name, target)
    except OSError as exc:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        print(f"render_unit: could not write {dst}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
