#!/usr/bin/env bash
# Run the whole suite. Everything here works against a fresh clone: paths are
# resolved relative to this file, so nothing needs to be installed first except
# the plugin itself being on disk (which it is — this suite lives inside it).
#
# The no-OOM stress test allocates 4.8 GB for several minutes and is opt-in;
# run it only where that memory and test duration are explicitly acceptable.
#
#   bash tests/run-all.sh
#   RAM_KEEPER_DANGEROUS_TESTS=1 bash tests/run-all.sh
# The other tests exercise daemon logic and hostile input. The integration cases
# touch the real machine; any transient scopes they create are stopped on exit.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
fails=0
test_out="$(mktemp "${TMPDIR:-/tmp}/ram-keeper-test.XXXXXX")"
trap 'rm -f "$test_out"' EXIT

run() {
  local label="$1"; shift
  printf '  %-24s ' "$label"
  if "$@" >"$test_out" 2>&1; then
    echo "PASS"
  else
    echo "FAIL"
    sed 's/^/      /' "$test_out" | tail -15
    fails=$((fails + 1))
  fi
}

echo "ram-keeper test suite"
echo
echo "installer tests:"
run "hostile install path" bash test_hostile_install_path.sh
run "inactive service start" bash test_installer_inactive_start.sh

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
run "forecast maths"      env RAM_KEEPER_LIVE=1 /usr/bin/python3 test_forecast.py
run "hostile configs"     env RAM_KEEPER_LIVE=1 /usr/bin/python3 test_config_hostile.py
run "compositor shield"   env RAM_KEEPER_LIVE=1 /usr/bin/python3 test_inviolable.py
run "resilience"          env RAM_KEEPER_LIVE=1 /usr/bin/python3 test_resilience.py
run "critical metrics"    /usr/bin/python3 test_critical_metrics.py
if [[ "${RAM_KEEPER_DANGEROUS_TESTS:-0}" == "1" ]]; then
  run "no OOM stress (live)" /usr/bin/python3 test_no_oom_kill.py
else
  run "no OOM safety"     /usr/bin/python3 test_no_oom_kill.py
fi
run "pass budget"        /usr/bin/python3 test_pass_budget.py

echo
echo "notifier (real script, stub binaries, throwaway HOME):"
run "notifier e2e"       bash test_notifier_e2e.sh

echo
echo "unit-test suites (pytest):"
# These are the biggest suites in the repo and they were previously not part of
# this runner at all — `run-all.sh` reported "all suites passed" while 200+
# tests in test_ram_keeper_core.py, test_integration.py and the rest had never
# been executed. pytest is an optional tool on a desktop, so report SKIP with
# the one command that fixes it rather than pretending to pass.
PYTEST_PY=""
for candidate in /usr/bin/python3 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import pytest' >/dev/null 2>&1; then
    PYTEST_PY="$candidate"
    break
  fi
done
if [[ -n "$PYTEST_PY" ]]; then
  run "pytest (core, policy, attacks)" bash -c \
    "cd '$PWD' && '$PYTEST_PY' -m pytest -q -p no:cacheprovider \
      test_ram_keeper_core.py test_integration.py test_process_guardian.py \
      test_bugfixes.py test_reclaim_policy.py test_config_attacks.py \
      test_unit_hardening.py"
else
  echo "  --   SKIP: pytest not installed for this python."
  echo "       install with: python3 -m pip install --user pytest   (see requirements.txt)"
fi

echo
if (( fails == 0 )); then
  echo "all suites passed"
else
  echo "$fails suite(s) failed"
fi
exit $(( fails > 0 ? 1 : 0 ))