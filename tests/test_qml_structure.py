"""Static checks on the plugin's QML and JS, run BEFORE restarting the shell.

Quickshell parses QML silently: a broken file loads nothing and answers
"Target not found", which looks like a dead plugin rather than a syntax error.
These checks catch the failures that are invisible until runtime:
stray `#` comments left by a bad edit, unbalanced braces, untyped parameters
in typed QML functions, references to undeclared ids, `text()` called where
`text` is a property, missing `Text.PlainText`, and private-use glyphs no
installed icon font actually carries.
"""
import os
import re
from pathlib import Path

QML_DIR = Path(__file__).resolve().parent.parent / "qml"
fails = 0


def check(name, ok, detail=""):
    global fails
    if ok:
        print("  ok   %s" % name)
    else:
        fails += 1
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


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
    """Codepoints carried by the shell's icon font, or None if unavailable."""
    import subprocess
    try:
        listing = subprocess.run(["fc-list", ":family=" + SHELL_FONT, "file"],
                                 capture_output=True, text=True, check=False).stdout
    except (FileNotFoundError, OSError):
        return None
    covered = set()
    for line in listing.splitlines():
        line = line.strip()
        if not line:
            continue
        path = line.split(":")[0].strip()
        if path:
            covered |= _cmap_of(path)
    return covered or None


_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%^~<>")
_REGEX_KEYWORDS = {"return", "typeof", "case", "throw", "delete", "void", "in", "of"}


def strip_comments_and_literals(src):
    """Single-pass JS/QML lexer that strips comments, strings, and regex literals.

    Avoids two classic false-positives (D-23):
      1. Treating two division operators `(a / 2) + ({k: 1}.k / 2)` as a regex
         literal `/.../` and eating the braces between them.
      2. Ignoring single-quoted strings `'{'` or apostrophes inside `//` comments.
    """
    out = []
    i, n = 0, len(src)
    prev_sig = ""       # last non-whitespace char outside comments/literals
    prev_word = ""      # last identifier word if prev_sig is identifier char

    while i < n:
        c = src[i]

        # Line comment
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            i += 2
            while i < n and src[i] != "\n":
                i += 1
            continue

        # Block comment
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue

        # String literal ("...", '...', `...`)
        if c in ('"', "'", "`"):
            quote = c
            i += 1
            while i < n:
                sc = src[i]
                if sc == "\\" and i + 1 < n:
                    i += 2
                    continue
                if sc == "\n":
                    out.append("\n")
                if sc == quote:
                    i += 1
                    break
                i += 1
            out.append('""')
            prev_sig = '"'
            prev_word = ""
            continue

        # Regex literal vs division operator
        if c == "/":
            can_start_regex = (
                prev_sig == ""
                or prev_sig in _REGEX_PRECEDERS
                or prev_word in _REGEX_KEYWORDS
            )
            if can_start_regex:
                j = i + 1
                in_class = False
                closed = False
                while j < n and src[j] != "\n":
                    rc = src[j]
                    if rc == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if rc == "[":
                        in_class = True
                    elif rc == "]":
                        in_class = False
                    elif rc == "/" and not in_class:
                        j += 1
                        while j < n and src[j].isalpha():
                            j += 1
                        closed = True
                        break
                    j += 1
                if closed:
                    out.append(" RE ")
                    prev_sig = "R"
                    prev_word = ""
                    i = j
                    continue

        out.append(c)
        if not c.isspace():
            if c.isalnum() or c in ("_", "$"):
                prev_word = (prev_word + c) if (prev_sig.isalnum() or prev_sig in ("_", "$")) else c
            else:
                prev_word = ""
            prev_sig = c
        i += 1

    return "".join(out)


def _extract_pua_glyphs(src):
    """Extract BMP + Supplementary PUA codepoints from string literals."""
    glyphs = set()
    for gm in re.finditer(r'(?:iconText|text|return)\s*[:=]?\s*"((?:[^"\\]|\\.)*)"', src):
        raw = gm.group(1)
        raw = re.sub(r"\\u\{([0-9a-fA-F]+)\}", lambda m: chr(int(m.group(1), 16)), raw)
        raw = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw)
        raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), raw)
        for ch in raw:
            cp = ord(ch)
            if (0xE000 <= cp <= 0xF8FF) or (0xF0000 <= cp <= 0x10FFFD):
                glyphs.add(cp)
    return glyphs


def _missing_pua_glyphs(glyphs, shell_cov, fallback_cov):
    """Return `(missing_codepoints, scope)` for PUA glyphs against installed fonts.

    Headless CI runners install DejaVu/Liberation (non-empty `fallback_cov` for
    ASCII/Latin) without `JetBrainsMono Nerd Font` (`shell_cov is None`, zero
    `>= 0xF0000` glyphs). Do not blame QML for a runner lacking Nerd Fonts,
    while still rejecting uncovered BMP PUA (`0xE000..0xF8FF`, e.g. Material
    Symbols web-font codepoints) and enforcing full coverage whenever `SHELL_FONT`
    or another Supplementary-PUA icon font is installed.
    """
    if shell_cov is not None:
        return sorted(cp for cp in glyphs if cp not in shell_cov), SHELL_FONT
    if not fallback_cov:
        return [], "any installed font"
    has_supp_pua_font = any(cp >= 0xF0000 for cp in fallback_cov)
    missing = sorted(
        cp for cp in glyphs
        if cp not in fallback_cov and (cp <= 0xF8FF or has_supp_pua_font)
    )
    return missing, "any installed font"


# D-23 self-checks on the lexer and PUA extractor before scanning real files.
_div_sample = strip_comments_and_literals("Item {\n  property real x: (a / 2) + ({k: 1}.k / 2)\n}\n")
check("lexer preserves braces between division operators",
      _div_sample.count("{") == 2 and _div_sample.count("}") == 2,
      repr(_div_sample))
_sq_sample = strip_comments_and_literals("Item {\n  property string s: '{'\n}\n")
check("lexer strips braces inside single-quoted strings",
      _sq_sample.count("{") == 1 and _sq_sample.count("}") == 1,
      repr(_sq_sample))
check("PUA extractor catches Supplementary PUA-A (U+F0432)",
      0xF0432 in _extract_pua_glyphs('iconText: "\U000f0432"'))
_ascii_only = set(range(0x20, 0x7F))
check("PUA font check tolerates headless runner without Nerd Fonts while catching BMP PUA",
      _missing_pua_glyphs({0xF0432, 0xE88A}, None, _ascii_only)[0] == [0xE88A]
      and _missing_pua_glyphs({0xF0432}, {0xF0001}, _ascii_only)[0] == [0xF0432])

scan_files = sorted(QML_DIR.glob("*.qml")) + sorted(QML_DIR.glob("*.js"))

for path in scan_files:
    src = path.read_text(encoding="utf-8")
    problems = []

    # 1. stray shell/python `#` comment
    for i, line in enumerate(src.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            problems.append("line %d: stray shell/python comment: %s" % (i, stripped[:60]))

    # 2. unterminated block comment
    if src.count("/*") != src.count("*/"):
        problems.append("unbalanced /* */")

    code = strip_comments_and_literals(src)

    # 3. brace and paren balance
    opens, closes = code.count("{"), code.count("}")
    if opens != closes:
        problems.append("brace imbalance: %d open, %d close" % (opens, closes))
    popens, pcloses = code.count("("), code.count(")")
    if popens != pcloses:
        problems.append("paren imbalance: %d open, %d close" % (popens, pcloses))

    if path.suffix == ".qml":
        # 4. every id a handler references for .running/.command must exist
        ids = set(re.findall(r"(?m)^\s*id:\s*(\w+)", src))
        for ref in set(re.findall(r"(?<![.\w])(\w+)\.(running|command)\b", code)):
            if ref[0] not in ids:
                problems.append("references id '%s' that is not declared" % ref[0])

        # 4a. Qt6 QML rejects untyped parameters when a function declares a
        #     return type (`function snooze(minutes): void` fails to parse).
        for fm in re.finditer(r"\bfunction\s+(\w+)\s*\(([^)]*)\)\s*:\s*\w+", code):
            fn_name, params = fm.group(1), fm.group(2).strip()
            if params:
                for p in params.split(","):
                    if ":" not in p:
                        problems.append(
                            "typed QML function '%s' has untyped parameter '%s'"
                            % (fn_name, p.strip()))

    # 4b. `text` on StdioCollector is a property, never `text()`
    for i, line in enumerate(code.split("\n"), 1):
        if re.search(r"\btext\s*\(\s*\)", line):
            problems.append("line %d: text() called as a method; StdioCollector.text is a property" % i)

    # 4c. Private-use glyphs (BMP + Supplementary PUA) must exist in the icon font
    glyphs = _extract_pua_glyphs(src)
    if glyphs:
        shell_cov = _shell_font_coverage()
        fallback_cov = set() if shell_cov is not None else _font_coverage()
        missing, scope = _missing_pua_glyphs(glyphs, shell_cov, fallback_cov)
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

model = (QML_DIR / "Model.js").read_text(encoding="utf-8")
service = (QML_DIR / "Service.qml").read_text(encoding="utf-8")
panel = (QML_DIR / "Panel.qml").read_text(encoding="utf-8")

critical = re.search(r'case "critical": return "([^"]*)"', model)
unknown = re.search(r'case "unknown": return "([^"]*)"', model)
check("Model.js unknown glyph differs from critical",
      bool(critical and unknown and critical.group(1) != unknown.group(1)))
check("Model.js scans all 6 state glyphs as PUA",
      len(_extract_pua_glyphs(model)) >= 6,
      str(len(_extract_pua_glyphs(model))))

apply = service.split("function applySnapshot", 1)[1].split("function considerVersionRestart", 1)[0]
check("a snapshot poll keeps the action error", 'lastError = ""' not in apply)
check("each action button has its own Working label",
      'activeAction === "relieve"' in panel and 'activeAction === "sweep"' in panel)
check("the panel reads manifest.json itself",
      not re.search(r"root\.manifest\b", service) and "manifest.json" in service)
check("the pause is remembered across a shell restart",
      "paused-by-user" in service and "pauseKnown" in service)
check("each row has its own protect or release button",
      'modelData.action === "protect"' in panel and 'modelData.action === "release"' in panel)
check("the footer uses the snapshot's own sentence", "Model.watchLine" in panel)
check("the panel can ask to protect an app", "function protect(" in service)
check("the off button uses the warning colour at rest",
      "root.daemonUp ? root.foreground : root.urgentColor" in panel)

# BarIconButton contract (and VETO check: slotSize + openPanelIndicatorWidth intact)
if "BarIconButton {" not in panel:
    check("Panel.qml has BarIconButton", False)
else:
    bar_btn = panel.split("BarIconButton {", 1)[1].split("KeyboardPanel {", 1)[0]
    check("BarIconButton separates icon text and percentage labelText",
          "labelText:" in bar_btn and "percentText" not in bar_btn.split("text:", 1)[1].split("\n", 1)[0])
    check("BarIconButton preserves slotSize and openPanelIndicatorWidth",
          "slotSize: root.barWidth" in bar_btn and "openPanelIndicatorWidth" in panel)

check("Panel content uses a bounded Flickable scroll container",
      "Flickable {" in panel and "boundsBehavior: Flickable.StopAtBounds" in panel and "clip: true" in panel)

# G-26: ScrollBar.vertical + clamped keyboard scroll
check("Flickable has ScrollBar.vertical (G-26)", "ScrollBar.vertical:" in panel)
check("Panel has clamped keyboard scroll (G-26)",
      "Key_PageUp" in panel and "Key_PageDown" in panel
      and "Math.max(0, flickable.contentHeight - flickable.height)" in panel)

# D-19: EVERY Text block in Panel.qml must set textFormat: Text.PlainText
# Count strictly on comment/literal-stripped `panel_clean` AND verify per block.
panel_clean = strip_comments_and_literals(panel)
text_starts = [m.start() for m in re.finditer(r"(?<![A-Za-z0-9_])Text\s*\{", panel_clean)]
plain_text_count = len(re.findall(r"\btextFormat\s*:\s*Text\.PlainText\b", panel_clean))
missing_plain_blocks = 0
for ts in text_starts:
    bstart = panel_clean.index("{", ts)
    depth = 0
    for bi in range(bstart, len(panel_clean)):
        if panel_clean[bi] == "{":
            depth += 1
        elif panel_clean[bi] == "}":
            depth -= 1
            if depth == 0:
                block_body = panel_clean[bstart:bi + 1]
                if not re.search(r"\btextFormat\s*:\s*Text\.PlainText\b", block_body):
                    missing_plain_blocks += 1
                break
check("every Text element in Panel.qml uses Text.PlainText (D-19)",
      len(text_starts) > 0 and len(text_starts) == plain_text_count and missing_plain_blocks == 0,
      "%d Text blocks, %d PlainText, %d missing" % (len(text_starts), plain_text_count, missing_plain_blocks))

# B-06: serialized daemonCtlProc + queuedCtl + startFailed + failedMinHeartbeat, no sh -c
check("daemon control serialized with queue and failedMinHeartbeat guard (B-06)",
      "id: daemonCtlProc" in service and "queuedCtl" in service
      and "startFailed" in service and "failedMinHeartbeat" in service
      and "stateReadStartedAt" in service
      and "id: pauseWrite" not in service and "id: pauseClear" not in service)
check("Service.qml never shells out via sh -c",
      '"sh"' not in service and '"bash"' not in service)
action_section = service.split("id: actionProc", 1)[1].split("id: daemonCtlProc", 1)[0]
check("actionProc does not claim Done on empty/failed stdout",
      '"Done"' not in action_section)

# B-07: versionRestart clears lastError on success and refreshManifest caches diskVersion
vr_section = service.split("id: versionRestart", 1)[1].split("Timer {", 1)[0]
rm_section = service.split("function refreshManifest()", 1)[1].split("function applyManifest", 1)[0]
check("versionRestart clears lastError on success (B-07)", 'lastError = ""' in vr_section)
check("refreshManifest stops polling once diskVersion is cached (B-07)",
      'root.diskVersion !== ""' in rm_section and "return" in rm_section)

# D-20: dynamic anchors (no 0.55 collision) + unknown guards
check("Panel eliminates 0.55 label width collision (D-20)",
      "parent.width * 0.55" not in panel
      and "protectBtn.visible ? protectBtn.left : reclaimSize.left" in panel
      and "releaseBtn.visible ? releaseBtn.left : sizeText.left" in panel)
nr_idx = panel.index('text: "NEVER RECLAIMED"')
nr_header = panel[panel.rfind("PanelSectionHeader", 0, nr_idx):nr_idx]
check("NEVER RECLAIMED header guarded against unknown state (D-20)",
      "!Model.isUnknown(root.state)" in nr_header)

# D-22: refreshPending, future heartbeat guard, interval_idle, RAM_KEEPER_STATE_DIR, safe status()
check("Service.qml handles refreshPending, future timestamps, interval_idle, and stateDir (D-22)",
      "refreshPending" in service and "age < -5" in service
      and "interval_idle" in service and "RAM_KEEPER_STATE_DIR" in service)

# F2 & F3: snooze controls + IPC verbs + immediate protect feedback + swap mini-bar + session impact
check("IPC handler exposes pause, typed snooze, and resume verbs (F2)",
      "function pause(): void" in service
      and "function snooze(minutes: string): void" in service
      and "function resume(): void" in service)
check("Panel exposes Snooze 30m / 1h / Start-Pause-Resume controls and liveNow (F2)",
      '"Snooze 30m"' in panel and '"Snooze 1h"' in panel
      and '"Resume"' in panel and '"Start"' in panel
      and "root.daemonOff" in panel and "root.liveNow" in panel)
check("Panel shows immediate Protecting…/Releasing… feedback and swap mini-bar (F3)",
      '"Protecting…"' in panel and '"Releasing…"' in panel
      and "pendingProtectKey" in service and "swapBarBg" in panel
      and "sessionImpact" in panel and "Model.barLabel" in panel)

print("\n" + ("QML structure ok" if fails == 0 else "%d check(s) failed" % fails))
assert fails == 0, f"Test failed: {fails} failures"
