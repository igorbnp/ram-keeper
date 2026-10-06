#!/usr/bin/env bash
# Run the whole suite. Everything here works against a fresh clone: paths are
# resolved relative to this file, so nothing needs to be installed first except
# the plugin itself being on disk (which it is — this suite lives inside it).
#
#   bash tests/run-all.sh
#
# The Python tests exercise the daemon's pure logic and its tolerance of hostile
# input. They touch the real machine for the cgroup-reclaim cases (that is the
# point — mocks prove nothing about memory) but never leave state behind: the
# only unit files created are transient `systemd-run` scopes, stopped on exit.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
fails=0

run() {
  local label="$1"; shift
  printf '  %-24s ' "$label"
  if "$@" >/tmp/rk-test.out 2>&1; then
    echo "PASS"
  else
    echo "FAIL"
    sed 's/^/      /' /tmp/rk-test.out | tail -15
    fails=$((fails + 1))
  fi
}

echo "ram-keeper test suite"
echo
echo "installer tests:"
run "hostile install path" bash test_hostile_install_path.sh

echo
echo "qml structure (must pass BEFORE restarting the shell):"
run "qml structure"      /usr/bin/python3 test_qml_structure.py

echo
echo "node tests (panel data shaping):"
command -v node >/dev/null && {
  run "model + forecast" node test_model.js
  run "unknown state"     node test_unknown_ui.js
} || echo "  node not installed — skipping the JS tests"

echo
echo "python tests (daemon logic and hostile input):"
run "forecast maths"      /usr/bin/python3 test_forecast.py
run "hostile configs"     /usr/bin/python3 test_config_hostile.py
run "compositor shield"   /usr/bin/python3 test_inviolable.py
run "resilience"          /usr/bin/python3 test_resilience.py
run "no OOM kill"         /usr/bin/python3 test_no_oom_kill.py
run "pass budget"        /usr/bin/python3 test_pass_budget.py

rm -f /tmp/rk-test.out
echo
if (( fails == 0 )); then
  echo "all suites passed"
else
  echo "$fails suite(s) failed"
fi
exit $(( fails > 0 ? 1 : 0 ))