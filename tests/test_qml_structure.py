"""Static checks on the plugin's QML, run BEFORE restarting the shell.

Quickshell parses QML silently: a broken file loads nothing and answers
"Target not found", which looks like a dead plugin rather than a syntax error.
These checks catch the failures that are invisible until runtime:
stray `#` comments left by a bad edit, unbalanced braces, references to
undeclared ids, `text()` called where `text` is a property, and private-use
glyphs no installed icon font actually carries.
"""
import os
import re
import sys
from pathlib import Path

QML_DIR = Path(__file__).resolve().parent.parent / "qml"
fails = 0


def _cmap_of(path):
    """Codepoints a font file covers. stdlib only: the test must run bare."""
    import struct
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if data[:4] == b"ttcf":
            data = data[struct.unpack(">I", data[12:16])[0]:]
        ntab = struct.unpack(">H", data[4:6])[0]
        tables = {}
        for i in range(ntab):
            rec = data[12 + 16 * i:28 + 16 * i]
            tables[rec[:4].decode("latin1")] = struct.unpack(">II", rec[8:16])
        base = tables["cmap"][0]
        nsub = struct.unpack(">H", data[base + 2:base + 4])[0]
        cps = set()
        for i in range(nsub):
            _, _, off = struct.unpack(">HHI",
                                      data[base + 4 + 8 * i:base + 12 + 8 * i])
            off += base
            fmt = struct.unpack(">H", data[off:off + 2])[0]
            if fmt == 4:
                sx = struct.unpack(">H", data[off + 6:off + 8])[0]
                seg = sx // 2
                ends = struct.unpack(">%dH" % seg, data[off + 14:off + 14 + sx])
                sp = off + 16 + sx
                starts = struct.unpack(">%dH" % seg, data[sp:sp + sx])
                for st, en in zip(starts, ends):
                    if en != 0xFFFF and en - st < 70000:
                        cps.update(range(st, en + 1))
            elif fmt == 12:
                ng = struct.unpack(">I", data[off + 12:off + 16])[0]
                for g in range(ng):
                    st, en, _ = struct.unpack(">III",
                                              data[off + 16 + 12 * g:off + 28 + 12 * g])
                    if en - st < 70000:
                        cps.update(range(st, en + 1))
        return cps
    except Exception:
        return set()


def _font_coverage():
    import glob
    covered = set()
    for pattern in ("/usr/share/fonts/**/*.ttf", "/usr/share/fonts/**/*.otf",
                    "/usr/share/fonts/**/*.ttc",
                    os.path.expanduser("~/.local/share/fonts/**/*.ttf")):
        for path in glob.glob(pattern, recursive=True):
            covered |= _cmap_of(path)
    return covered


# The font the shell's icon text actually renders with.
SHELL_FONT = "JetBrainsMono Nerd Font"


def _shell_font_coverage():
    """Codepoints carried by the shell's icon font, or None if it is absent."""
    import subprocess
    listing = subprocess.run(["fc-list", ":family=" + SHELL_FONT, "file"],
                             capture_output=True, text=True).stdout
    covered = set()
    for line in listing.splitlines():
        line = line.strip()
        if not line:
            continue
        # `fc-list :family=X file` prints "<path>:" with an empty tail — the
        # filename is BEFORE the colon. Splitting from the right returned the
        # empty string, so this helper silently reported "font not installed"
        # and every glyph check quietly fell back to any-font.
        path = line.split(":")[0].strip()
        if path:
            covered |= _cmap_of(path)
    return covered or None


def strip_comments(src):
    """Remove // and /* */ comments, respecting string literals.

    Comments are stripped BEFORE strings on purpose. Stripping in the other
    order mis-parses an apostrophe inside a comment (there are several in these
    files) and reports a phantom imbalance in a file that is perfectly valid.
    """
    out = []
    i, n = 0, len(src)
    in_str = False
    while i < n:
        c = src[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


for path in sorted(QML_DIR.glob("*.qml")):
    src = path.read_text(encoding="utf-8")
    problems = []

    # 1. a stray shell/python comment. This is exactly how the file broke: a
    #    `//` prefix was lost during an edit and the QML parser reported
    #    "Unexpected token" three lines later, pointing nowhere near the cause.
    for i, line in enumerate(src.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            problems.append("line %d: stray shell/python comment: %s" % (i, stripped[:60]))

    # 2. unterminated block comment
    if src.count("/*") != src.count("*/"):
        problems.append("unbalanced /* */")

    src = re.sub(r"/(?![/*])(?:\\.|[^/\\\n])+/[gimsuy]*", " RE ", src)
    code = strip_comments(src)
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', code)

    # 3. brace balance
    opens, closes = code.count("{"), code.count("}")
    if opens != closes:
        problems.append("brace imbalance: %d open, %d close" % (opens, closes))

    # 4. every id a handler references must be declared somewhere in the file
    ids = set(re.findall(r"(?m)^\s*id:\s*(\w+)", src))
    for ref in set(re.findall(r"(?<![\w.])(\w+)\.(running|command)\b", code)):
        if ref[0] not in ids:
            problems.append("references id '%s' that is not declared" % ref[0])

    # 4b. `text` on StdioCollector is a PROPERTY. Calling text() parses fine,
    #     loads the plugin, and then throws a TypeError the first time it runs —
    #     a dead panel with no visible error. Catch it here.
    for i, line in enumerate(src.split("\n"), 1):
        if re.search(r"\btext\s*\(\s*\)", line) and "StdioCollector" not in src[:0]:
            problems.append("line %d: text() called as a method; StdioCollector.text is a property" % i)

    # 4c. Private-use glyphs only render if a font actually carries them. A
    #     Material Symbols codepoint (U+E0xx/U+E2xx) is tofu on this machine,
    #     because the only icon font installed is JetBrainsMono Nerd Font whose
    #     Material range starts at U+F0xxx. A plugin author picks the glyph from
    #     a web font they saw elsewhere and never checks the local font.
    glyphs = set()
    for gm in re.finditer(r'(?:iconText|text|return)\s*[:=]?\s*"([^"]*)"', src):
        raw = gm.group(1)
        # QML accepts \uXXXX escapes as well as literal characters, and an
        # editor may store either. Resolve both, or the check silently passes on
        # every escaped glyph — which is every glyph written by a careful author.
        raw = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw)
        raw = re.sub(r"\\x([0-9a-fA-F]{2})",
                     lambda m: chr(int(m.group(1), 16)), raw)
        for ch in raw:
            if 0xE000 <= ord(ch) <= 0xF8FF:
                glyphs.add(ord(ch))
    if glyphs:
        # The icon font the shell requests, not "any font". Noto Sans Symbols
        # carries 49k codepoints including the private-use plane, so an
        # any-font check happily passes on a glyph that still draws a box in
        # this panel. Only coverage by the requested family counts.
        cov = _shell_font_coverage()
        scope = SHELL_FONT
        if cov is None:
            cov, scope = _font_coverage(), "any installed font"
        missing = sorted(cp for cp in glyphs if cp not in cov)
        for cp in missing:
            problems.append(
                "U+%05X is private-use and absent from %s "
                "(renders as a tofu box)" % (cp, scope))

    if problems:
        fails += 1
        print("  FAIL %s" % path.name)
        for p in problems:
            print("       %s" % p)
    else:
        print("  ok   %s" % path.name)

print("\n" + ("QML structure ok" if fails == 0 else "%d file(s) suspect" % fails))
sys.exit(0 if fails == 0 else 1)