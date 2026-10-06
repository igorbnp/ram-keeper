#!/usr/bin/env bash
# ram-keeper installer. Idempotent: safe to run again after an update.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$PLUGIN_DIR/units/ram-keeper.service"
NOTIFY_SRC="$PLUGIN_DIR/helper/ram_keeper_notify"
UNIT_DST="$HOME/.config/systemd/user/ram-keeper.service"
STATE_DIR="$HOME/.local/state/ram-keeper"

say() { printf '\033[1;36m▸\033[0m %s\n' "$1"; }
ok()  { printf '\033[1;32m✓\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$1"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------

[[ -f "$PLUGIN_DIR/helper/ram_keeper.py" ]] || die "helper not found at $PLUGIN_DIR/helper/ram_keeper.py"
[[ -f "$UNIT_SRC" ]] || die "unit not found at $UNIT_SRC"

command -v systemctl >/dev/null || die "systemctl not found"
[[ -d /sys/fs/cgroup ]] || die "/sys/fs/cgroup missing — this needs cgroup v2"

# cgroup v2 unified hierarchy, not a v1 hybrid.
stat -fc %T /sys/fs/cgroup 2>/dev/null | grep -q cgroup2fs \
  || die "cgroup v2 is required (found: $(stat -fc %T /sys/fs/cgroup 2>/dev/null))"

PYTHON=""
for candidate in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  fi
done
[[ -n "$PYTHON" ]] || die "no python3 >= 3.9 found"

say "python: $PYTHON ($("$PYTHON" -V 2>&1))"

# The unit names the interpreter explicitly. If /usr/bin/python3 is not it
# (a Nix store, a venv), render that path instead — into the DESTINATION, not
# into the template. An earlier revision used `sed -i` on the template, which
# left the tracked file dirty and made every later run depend on that mutation.
PYTHON_OVERRIDE="$PYTHON"
if [[ "$PYTHON" != "/usr/bin/python3" ]]; then
  warn "python3 is not at /usr/bin/python3 ($PYTHON); the unit will use it"
fi

# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------

mkdir -p "$STATE_DIR" "$STATE_DIR/flags" "$HOME/.config/systemd/user"
# Render @PLUGIN_DIR@ rather than hardcoding the install path. `omarchy plugin
# add` clones into $PLUGINS_DIR/<manifest id>, so the directory name is chosen by
# the id (which the marketplace requires to be namespaced) — a hardcoded path
# breaks the moment either changes.
render_unit() {
  # Render the template with `envsubst`-style expansion done by PYTHON, not sed
  # and not bash parameter substitution.
  #
  # Both naive approaches are wrong on real home directories:
  #   sed "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" — a path containing `|` is parsed as
  #     more sed options. It failed, and because the redirect had already
  #     truncated the destination, left an EMPTY unit file: a daemon that never
  #     starts, with no error shown.
  #   "${content//@PLUGIN_DIR@/$PLUGIN_DIR}" — `&` is special in the PATTERN
  #     (it matches the matched text), so a directory named "sub dir&x" expands
  #     the placeholder to itself and the placeholder survives into the unit.
  # Python's str.replace is literal in both the needle and the haystack, so no
  # character in the path can mean anything.
  local src="$1" dst="$2"
  "$PYTHON" - "$src" "$dst" "$PLUGIN_DIR" "$PYTHON_OVERRIDE" <<'PYEOF'
import sys
src, dst, plugin_dir, python = sys.argv[1:5]
with open(src, encoding="utf-8") as handle:
    content = handle.read()
rendered = content.replace("@PLUGIN_DIR@", plugin_dir)
rendered = rendered.replace("/usr/bin/python3", python)
if not rendered.strip():
    sys.exit(f"rendered unit is empty: {dst}")
if "@PLUGIN_DIR@" in rendered:
    sys.exit(f"template placeholder survived in {dst}")
with open(dst, "w", encoding="utf-8") as handle:
    handle.write(rendered)
PYEOF
  [[ $? -eq 0 ]] || die "could not render $src"
  [[ -s "$dst" ]] || die "rendered unit is empty: $dst"
}

render_unit "$UNIT_SRC" "$UNIT_DST"
ok "unit installed at $UNIT_DST"

chmod +x "$PLUGIN_DIR/helper/ram_keeper.py"

# --------------------------------------------------------------------------
# Notifier: the AI integration
#
# Optional, and deliberately event-driven. An LLM never watches a number; it is
# only woken by a click on a toast that already carries evidence. That is the
# pattern omarchy-crash-watch uses, and it is the reason this feature is cheap
# instead of a recurring token bill.
# --------------------------------------------------------------------------

if [[ -f "$NOTIFY_SRC" ]]; then
  chmod +x "$NOTIFY_SRC"
  NOTIFY_UNIT="$HOME/.config/systemd/user/ram-keeper-notify.service"
  if [[ -f "$PLUGIN_DIR/units/ram-keeper-notify.service.in" ]]; then
    render_unit "$PLUGIN_DIR/units/ram-keeper-notify.service.in" "$NOTIFY_UNIT"
    ok "notifier unit installed"
  else
    warn "notifier unit template missing; skipping the AI notifications"
  fi
else
  warn "notifier script not found; skipping the AI notifications"
fi

# --------------------------------------------------------------------------
# Kernel/omarchy preconditions we depend on — report, never silently assume
# --------------------------------------------------------------------------

ZRAM_ALGO="$(cat /sys/block/zram0/comp_algorithm 2>/dev/null | tr -d '\n' | grep -o '\[[a-z0-9-]*\]' | tr -d '[]' || true)"
if [[ -n "$ZRAM_ALGO" ]]; then
  ok "zram0 present, compression: $ZRAM_ALGO"
else
  warn "no zram0 found — anon memory will go to disk swap, which is much slower"
fi

if [[ -e /sys/block/zram0 ]]; then
  ZRAM_PRIO="$(awk '$1=="/dev/zram0"{print $5}' /proc/swaps 2>/dev/null || true)"
  [[ -n "$ZRAM_PRIO" ]] && ok "zram swap priority: $ZRAM_PRIO (higher than disk = correct)"
fi

if systemctl is-active --quiet systemd-oomd; then
  ok "systemd-oomd is the safety net (it decides what gets killed)"
else
  warn "systemd-oomd is not active — nothing will rescue the desktop if memory runs out"
fi

SWAPPINESS="$(cat /proc/sys/vm/swappiness 2>/dev/null || echo '?')"
if [[ "$SWAPPINESS" != "?" && "$SWAPPINESS" -lt 60 ]] 2>/dev/null; then
  warn "vm.swappiness=$SWAPPINESS is low for a zram machine; 150 keeps swap traffic in RAM"
fi

# --------------------------------------------------------------------------
# Enable
# --------------------------------------------------------------------------

systemctl --user daemon-reload
# try-restart, NOT `enable --now`. `enable --now` only STARTS a unit that is
# inactive; on a unit that is already running it is a no-op, so every code fix
# after the first install would leave the OLD daemon resident — the file on
# disk changes, the running process does not, and the user is protected by
# code that no longer exists. That failure is invisible: the unit is active,
# the installer reports success, and the bug being fixed stays fixed nowhere.
systemctl --user enable ram-keeper.service >/dev/null 2>&1 \
  || die "could not enable ram-keeper.service"
systemctl --user try-restart ram-keeper.service >/dev/null 2>&1 \
  || systemctl --user start ram-keeper.service

sleep 2
if systemctl --user is-active --quiet ram-keeper.service; then
  # Active is not the same as correct. A unit whose ExecStart points at a path
  # that no longer exists also ends up "activating" forever under Restart=always,
  # so verify the process is actually running THIS file.
  running="$(systemctl --user show ram-keeper.service -p ExecMainStartTimestamp --value 2>/dev/null)"
  if [[ -n "$running" ]] && systemctl --user show ram-keeper.service \
       -p NRestarts --value 2>/dev/null | grep -qv "^0$"; then
    warn "daemon is restarting repeatedly; check: journalctl --user -u ram-keeper -n 30"
  fi
  ok "ram-keeper.service is running"
else
  warn "service did not come up; check: journalctl --user -u ram-keeper -n 40"
  warn "ExecStart resolves to: $(systemctl --user show ram-keeper.service -p ExecStart --value 2>/dev/null)"
fi

# The notifier is optional: a machine with no notifications still gets full
# protection, it just does not get told about it.
if [[ -f "$HOME/.config/systemd/user/ram-keeper-notify.service" ]]; then
  systemctl --user enable ram-keeper-notify.service >/dev/null 2>&1 || true
  systemctl --user try-restart ram-keeper-notify.service >/dev/null 2>&1 || true
  sleep 1
  if systemctl --user is-active --quiet ram-keeper-notify.service; then
    ok "ram-keeper-notify.service is running (alerts on, one-click AI)"
  else
    warn "notifier did not start (alerts off, protection unaffected)"
  fi
  if [[ -x "$NOTIFY_SRC" ]]; then
    bash -n "$NOTIFY_SRC" && ok "notifier script parses" \
      || warn "notifier script has a syntax error"
  fi
fi

# --------------------------------------------------------------------------
# Prove it can actually read the machine
# --------------------------------------------------------------------------

if "$PYTHON" "$PLUGIN_DIR/helper/ram_keeper.py" --once >/dev/null 2>&1; then
  SNAP="$("$PYTHON" "$PLUGIN_DIR/helper/ram_keeper.py" --once 2>/dev/null)"
  FREE="$(printf '%s' "$SNAP" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["free_percent"])' 2>/dev/null || echo '?')"
  STATE="$(printf '%s' "$SNAP" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])' 2>/dev/null || echo '?')"
  ok "memory readable: ${FREE}% free, state=$STATE"
else
  warn "the helper could not produce a snapshot — run: $PYTHON $PLUGIN_DIR/helper/ram_keeper.py --once"
fi

say "plugin folder: $PLUGIN_DIR"
ok "done. The bar widget appears after 'omarchy restart shell'."