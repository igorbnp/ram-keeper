#!/usr/bin/env bash
# ram-keeper installer. Idempotent: safe to run again after an update.
#
# Usage:
#   ./install.sh          Install and start the plugin
#   ./install.sh --check  Post-install health check (no changes)
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
# --check: post-install health verification (F4)
#
# Invokes the daemon's --doctor mode plus checks the units, notifier script,
# state directory and journal warnings. Makes no changes to the system.
# --------------------------------------------------------------------------
if [[ $# -gt 0 && "${1:-}" != "--check" ]]; then
  die "unknown option: $1 (usage: ./install.sh [--check])"
fi

if [[ "${1:-}" == "--check" ]]; then
  checks_failed=0
  check_ok()   { printf '\033[1;32m✓\033[0m %s\n' "$1"; }
  check_warn() { printf '\033[1;33m!\033[0m %s\n' "$1"; checks_failed=$((checks_failed + 1)); }

  say "running post-install health check (read-only)"

  # 1. Find a python >= 3.9 interpreter.
  CHECK_PY=""
  for c in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    if [[ -n "$c" && -x "$c" ]] \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      CHECK_PY="$c"
      break
    fi
  done

  # 2. Run the daemon's built-in --doctor (with graceful fallback to --once).
  if [[ -n "$CHECK_PY" && -f "$PLUGIN_DIR/helper/ram_keeper.py" ]]; then
    doc_rc=0
    doc_out="$("$CHECK_PY" "$PLUGIN_DIR/helper/ram_keeper.py" --doctor 2>&1)" || doc_rc=$?
    if (( doc_rc == 0 )); then
      [[ -n "$doc_out" ]] && printf '%s\n' "$doc_out"
      check_ok "ram_keeper.py --doctor passed"
    elif (( doc_rc == 2 )) && [[ "$doc_out" == *"unrecognized arguments: --doctor"* ]]; then
      if "$CHECK_PY" "$PLUGIN_DIR/helper/ram_keeper.py" --once >/dev/null 2>&1; then
        check_ok "ram_keeper.py --once snapshot readable"
      else
        check_warn "ram_keeper.py --once failed"
      fi
    else
      [[ -n "$doc_out" ]] && printf '%s\n' "$doc_out"
      check_warn "ram_keeper.py --doctor reported issues (exit $doc_rc)"
    fi
  else
    check_warn "cannot run --doctor: python3 >= 3.9 or helper not found"
  fi

  # 3. Units installed, enabled and active.
  if systemctl --user is-enabled ram-keeper.service >/dev/null 2>&1; then
    check_ok "ram-keeper.service is enabled"
  else
    check_warn "ram-keeper.service is not enabled (fix: bash $PLUGIN_DIR/install.sh)"
  fi

  if systemctl --user is-active --quiet ram-keeper.service; then
    check_ok "ram-keeper.service is running"
    restarts="$(systemctl --user show ram-keeper.service -p NRestarts --value 2>/dev/null || echo 0)"
    if [[ "$restarts" != "0" ]]; then
      check_warn "daemon has restarted $restarts time(s); check: journalctl --user -u ram-keeper -n 30"
    fi
  else
    check_warn "ram-keeper.service is not running (fix: systemctl --user restart ram-keeper.service)"
  fi

  if [[ -f "$HOME/.config/systemd/user/ram-keeper-notify.service" ]]; then
    if systemctl --user is-active --quiet ram-keeper-notify.service; then
      check_ok "ram-keeper-notify.service is running"
    elif command -v omarchy-notification-send >/dev/null 2>&1; then
      check_warn "ram-keeper-notify.service is not running (fix: systemctl --user start ram-keeper-notify.service)"
    else
      say "omarchy-notification-send not on PATH; notifier intentionally idle"
    fi
  fi

  if [[ -f "$NOTIFY_SRC" ]]; then
    if bash -n "$NOTIFY_SRC" 2>/dev/null; then
      check_ok "notifier script parses"
    else
      check_warn "notifier script has a syntax error"
    fi
  fi

  # 4. State directory writable and snapshot fresh.
  if [[ -d "$STATE_DIR/flags" && -w "$STATE_DIR/flags" ]]; then
    check_ok "state directory writable: $STATE_DIR/flags"
  else
    check_warn "state directory missing or read-only: $STATE_DIR/flags (fix: bash $PLUGIN_DIR/install.sh)"
  fi

  if [[ -f "$STATE_DIR/state.json" ]]; then
    age=$(( $(date +%s) - $(stat -c %Y "$STATE_DIR/state.json" 2>/dev/null || echo 0) ))
    if (( age <= 180 )); then
      check_ok "state.json is fresh (${age}s old)"
    else
      check_warn "state.json is stale (${age}s old)"
    fi
  fi

  # 5. Kernel / Omarchy environment & AI diagnosis skill.
  if [[ -e /sys/block/zram0 ]]; then
    check_ok "zram0 swap present"
  else
    check_warn "zram0 not present (anonymous memory will not swap to compressed RAM)"
  fi

  if systemctl is-active --quiet systemd-oomd 2>/dev/null; then
    check_ok "systemd-oomd is active"
  else
    check_warn "systemd-oomd is not active"
  fi

  HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}"
  if [[ -f "$HERMES_ROOT/skills/ram-memory-diagnosis/SKILL.md" ]]; then
    check_ok "Hermes diagnosis skill present"
  else
    say "Hermes skill optional: $HERMES_ROOT/skills/ram-memory-diagnosis/SKILL.md not installed"
  fi

  # 6. Journal warnings in the last hour.
  if command -v journalctl >/dev/null 2>&1; then
    warns="$(journalctl --user -u ram-keeper -u ram-keeper-notify --since '1 hour ago' -p warning --no-pager -q 2>/dev/null | wc -l | tr -d ' ')"
    if (( warns > 0 )); then
      check_warn "$warns journal warning(s) in the last hour; run: journalctl --user -u ram-keeper -u ram-keeper-notify -p warning --since '1 hour ago'"
    else
      check_ok "no journal warnings in the last hour"
    fi
  fi

  echo
  if (( checks_failed == 0 )); then
    ok "all health checks passed"
  else
    warn "$checks_failed check(s) need attention"
  fi
  exit $(( checks_failed > 0 ? 1 : 0 ))
fi

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

mkdir -p "$STATE_DIR" "$STATE_DIR/flags" "$HOME/.config/ram-keeper" \
         "$HOME/.config/systemd/user" "$HOME/.config/user-tmpfiles.d"
cat >"$HOME/.config/user-tmpfiles.d/ram-keeper.conf" <<'TMPFILES'
d %h/.local/state/ram-keeper 0700 - - -
d %h/.local/state/ram-keeper/flags 0700 - - -
d %h/.config/ram-keeper 0700 - - -
TMPFILES
if command -v systemd-tmpfiles >/dev/null 2>&1; then
  systemd-tmpfiles --user --create >/dev/null 2>&1 || true
fi
# Render @PLUGIN_DIR@ rather than hardcoding the install path. `omarchy plugin
# add` clones into $PLUGINS_DIR/<manifest id>, so the directory name is chosen by
# the id (which the marketplace requires to be namespaced) — a hardcoded path
# breaks the moment either changes.
render_unit() {
  # One implementation, in helper/render_unit.py, also used by the hostile-path
  # test. `sed` broke on a path containing `|` (and left an empty unit behind
  # because the redirect had already truncated it); bash's ${var//pat/rep}
  # treated `&` as the matched text. Python's string ops are literal, and the
  # renderer writes to a temp file then renames, so a failure cannot leave a
  # truncated unit for systemd to load.
  local src="$1" dst="$2"
  "$PYTHON" "$PLUGIN_DIR/helper/render_unit.py" \
      "$src" "$dst" "$PLUGIN_DIR" "$PYTHON_OVERRIDE" \
    || die "could not render $src (see the error above)"
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
# `enable --now` only STARTS a unit that is inactive; on a unit that is already
# running it is a no-op, so every code fix after the first install would leave
# the OLD daemon resident. `restart` is required: it reloads changed code when
# active AND starts the service on a fresh install. Fall back to `start` if the
# restart fails for a transient reason.
systemctl --user enable ram-keeper.service >/dev/null 2>&1 \
  || die "could not enable ram-keeper.service"
systemctl --user restart ram-keeper.service >/dev/null 2>&1 \
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
  # `try-restart` is a no-op on an inactive unit, which means a fresh install
  # never starts the notifier. Mirror the daemon's pattern: restart if running,
  # otherwise start.
  systemctl --user restart ram-keeper-notify.service >/dev/null 2>&1 \
    || systemctl --user start ram-keeper-notify.service >/dev/null 2>&1 || true
  sleep 1
  if systemctl --user is-active --quiet ram-keeper-notify.service; then
    ok "ram-keeper-notify.service is running (alerts on, one-click AI)"
  else
    warn "notifier did not start (alerts off, protection unaffected)"
  fi
  if [[ -x "$NOTIFY_SRC" ]]; then
    if bash -n "$NOTIFY_SRC"; then
      ok "notifier script parses"
    else
      warn "notifier script has a syntax error"
    fi
  fi
fi

# --------------------------------------------------------------------------
# Prove it can actually read the machine
# --------------------------------------------------------------------------

if "$PYTHON" "$PLUGIN_DIR/helper/ram_keeper.py" --once >/dev/null 2>&1; then
  SNAP="$("$PYTHON" "$PLUGIN_DIR/helper/ram_keeper.py" --once 2>/dev/null)"
  FREE="$(printf '%s' "$SNAP" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["free_percent"])' 2>/dev/null || echo '?')"
  STATE="$(printf '%s' "$SNAP" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["state"])' 2>/dev/null || echo '?')"
  ok "memory readable: ${FREE}% free, state=$STATE"
else
  warn "the helper could not produce a snapshot — run: $PYTHON $PLUGIN_DIR/helper/ram_keeper.py --once"
fi

say "plugin folder: $PLUGIN_DIR"
ok "done. The bar widget appears after 'omarchy restart shell'."