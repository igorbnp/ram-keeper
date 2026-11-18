#!/usr/bin/env bash
# A successful systemctl try-restart is a no-op when a unit is inactive.
# The installer must start BOTH ram-keeper.service and ram-keeper-notify.service
# on a fresh installation, render PYTHON_OVERRIDE into the notify unit, and
# support read-only `./install.sh --check`.
set -uo pipefail

PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/home"
export HOME="$TMP/home"
export SYSTEMCTL_LOG="$TMP/systemctl.log"
export SYSTEMCTL_ACTIVE="$TMP/ram-keeper-active"
export NOTIFY_ACTIVE="$TMP/ram-keeper-notify-active"
export PATH="$TMP/bin:$PATH"

cat >"$TMP/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$SYSTEMCTL_LOG"
case "$*" in
  "--user is-active --quiet ram-keeper.service") [[ -e "$SYSTEMCTL_ACTIVE" ]] ;;
  "--user is-enabled ram-keeper.service") [[ -e "$HOME/.config/systemd/user/ram-keeper.service" ]] ;;
  "--user try-restart ram-keeper.service") exit 0 ;;
  "--user restart ram-keeper.service"|"--user start ram-keeper.service") touch "$SYSTEMCTL_ACTIVE" ;;
  "--user try-restart ram-keeper-notify.service") exit 0 ;;
  "--user restart ram-keeper-notify.service"|"--user start ram-keeper-notify.service") touch "$NOTIFY_ACTIVE" ;;
  "--user is-active --quiet ram-keeper-notify.service") [[ -e "$NOTIFY_ACTIVE" ]] ;;
  "--user show ram-keeper.service -p ExecMainStartTimestamp --value") echo "now" ;;
  "--user show ram-keeper.service -p NRestarts --value") echo "0" ;;
  "--user show ram-keeper.service -p ExecStart --value") echo "/usr/bin/python3 helper/ram_keeper.py --daemon" ;;
  "is-active --quiet systemd-oomd") exit 0 ;;
  *) exit 0 ;;
esac
MOCK

cat >"$TMP/bin/journalctl" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK

chmod +x "$TMP/bin/systemctl" "$TMP/bin/journalctl"

if [[ -d /sys/fs/cgroup ]] && stat -fc %T /sys/fs/cgroup 2>/dev/null | grep -q cgroup2fs; then
  "$PLUGIN/install.sh" >"$TMP/install.log" 2>&1 || {
    cat "$TMP/install.log"
    echo "FAIL: installer exited nonzero"
    exit 1
  }

  if [[ ! -e "$SYSTEMCTL_ACTIVE" ]]; then
    echo "FAIL: installer left ram-keeper.service inactive"
    cat "$TMP/install.log"
    exit 1
  fi
  if [[ ! -e "$NOTIFY_ACTIVE" ]]; then
    echo "FAIL: installer left ram-keeper-notify.service inactive (D-16 try-restart bug)"
    cat "$TMP/install.log"
    exit 1
  fi
  if ! grep -q '^Environment=RAM_KEEPER_PYTHON=' "$HOME/.config/systemd/user/ram-keeper-notify.service"; then
    echo "FAIL: rendered notify unit is missing Environment=RAM_KEEPER_PYTHON="
    exit 1
  fi
  if [[ ! -f "$HOME/.config/user-tmpfiles.d/ram-keeper.conf" ]]; then
    echo "FAIL: user-tmpfiles.d/ram-keeper.conf was not created"
    exit 1
  fi
  echo "PASS: inactive RAM Keeper and notifier services were started"
else
  # Non-cgroup2fs host (container/non-Linux): seed expected post-install state
  # to exercise `./install.sh --check` read-only diagnostics.
  mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/state/ram-keeper/flags"
  touch "$HOME/.config/systemd/user/ram-keeper.service" \
        "$HOME/.config/systemd/user/ram-keeper-notify.service" \
        "$SYSTEMCTL_ACTIVE" "$NOTIFY_ACTIVE"
  echo "SKIP: full install skipped (no cgroup2fs); exercising --check"
fi

# Verify `--check` honours RAM_KEEPER_STATE_DIR and Hermes profile skills.
mkdir -p "$TMP/custom-state/flags" "$TMP/hermes/profiles/dev/skills/ram-memory-diagnosis"
touch "$TMP/hermes/profiles/dev/skills/ram-memory-diagnosis/SKILL.md"
RAM_KEEPER_STATE_DIR="$TMP/custom-state" HERMES_HOME="$TMP/hermes" \
  "$PLUGIN/install.sh" --check >"$TMP/check.log" 2>&1 || true
if ! grep -q 'ram-keeper.service is running' "$TMP/check.log"; then
  cat "$TMP/check.log"
  echo "FAIL: install.sh --check did not report running service"
  exit 1
fi
if ! grep -q "state directory writable: $TMP/custom-state/flags" "$TMP/check.log"; then
  cat "$TMP/check.log"
  echo "FAIL: install.sh --check ignored RAM_KEEPER_STATE_DIR"
  exit 1
fi
if ! grep -q "Hermes diagnosis skill present" "$TMP/check.log"; then
  cat "$TMP/check.log"
  echo "FAIL: install.sh --check missed Hermes profile skill"
  exit 1
fi
echo "PASS: install.sh --check verified service state, RAM_KEEPER_STATE_DIR and Hermes skill"
exit 0
