#!/usr/bin/env bash
# A successful systemctl try-restart is a no-op when a unit is inactive.
# The installer must still start the service on a fresh installation.
set -uo pipefail

PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/home"
export HOME="$TMP/home"
export SYSTEMCTL_LOG="$TMP/systemctl.log"
export SYSTEMCTL_ACTIVE="$TMP/ram-keeper-active"
export PATH="$TMP/bin:/usr/local/bin:/usr/bin:/bin"

cat >"$TMP/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$SYSTEMCTL_LOG"
case "$*" in
  "--user is-active --quiet ram-keeper.service") [[ -e "$SYSTEMCTL_ACTIVE" ]];;
  "--user try-restart ram-keeper.service") exit 0 ;; # real systemd: inactive unit stays inactive
  "--user restart ram-keeper.service"|"--user start ram-keeper.service") touch "$SYSTEMCTL_ACTIVE" ;;
  "--user is-active --quiet ram-keeper-notify.service") exit 3 ;;
  "--user show ram-keeper.service -p ExecMainStartTimestamp --value") echo "now" ;;
  "--user show ram-keeper.service -p NRestarts --value") echo "0" ;;
  "--user show ram-keeper.service -p ExecStart --value") echo "/usr/bin/python3 helper/ram_keeper.py --daemon" ;;
  "is-active --quiet systemd-oomd") exit 3 ;;
  *) exit 0 ;;
esac
MOCK
chmod +x "$TMP/bin/systemctl"

"$PLUGIN/install.sh" >"$TMP/install.log" 2>&1 || {
  cat "$TMP/install.log"
  echo "FAIL: installer exited nonzero"
  exit 1
}

if [[ -e "$SYSTEMCTL_ACTIVE" ]]; then
  echo "PASS: inactive RAM Keeper service was started"
  exit 0
fi
printf '%s\n' "FAIL: installer left the service inactive after a successful try-restart"
cat "$TMP/install.log"
exit 1
