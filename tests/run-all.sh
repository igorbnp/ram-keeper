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
# Never let a child inherit the owner's config. Track whether we created the
# scratch paths ourselves so EXIT cleans up our temp files without touching
# caller-supplied overrides.
_own_cfg=""
_own_state=""
if [[ -z "${RAM_KEEPER_CONFIG:-}" ]]; then
  _own_cfg="$(mktemp "${TMPDIR:-/tmp}/rk-config.XXXXXX")"
  printf '{}\n' >"$_own_cfg"
  export RAM_KEEPER_CONFIG="$_own_cfg"
fi
if [[ -z "${RAM_KEEPER_STATE_DIR:-}" ]]; then
  _own_state="$(mktemp -d "${TMPDIR:-/tmp}/rk-state.XXXXXX")"
  export RAM_KEEPER_STATE_DIR="$_own_state"
fi
fails=0
skipped=0
test_out="$(mktemp "${TMPDIR:-/tmp}/ram-keeper-test.XXXXXX")"
trap 'rm -f "$test_out" ${_own_cfg:+"$_own_cfg"}; [[ -n "$_own_state" ]] && rm -rf "$_own_state"' EXIT

PY=""
for _cand in "${RAM_KEEPER_PYTHON:-}" /usr/bin/python3 \
             "$(command -v python3 2>/dev/null || true)" \
             "$(command -v python 2>/dev/null || true)"; do
  if [[ -n "$_cand" && -x "$_cand" ]] \
     && "$_cand" -c "import sys" >/dev/null 2>&1; then
    PY="$_cand"
    break
  fi
done
[[ -n "$PY" ]] || PY=python3

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
run "qml structure"      "$PY" test_qml_structure.py

echo
echo "node tests (panel data shaping):"
if command -v node >/dev/null; then
  run "model + forecast" node test_model.js
  run "unknown state"     node test_unknown_ui.js
else
  echo "  --   SKIP: node not installed — skipping the JS tests"
  skipped=$((skipped + 1))
fi

echo
echo "python tests (daemon logic and hostile input):"
run "forecast maths"      env RAM_KEEPER_LIVE=1 "$PY" test_forecast.py
run "hostile configs"     env RAM_KEEPER_LIVE=1 "$PY" test_config_hostile.py
run "compositor shield"   env RAM_KEEPER_LIVE=1 "$PY" test_inviolable.py
run "resilience"          env RAM_KEEPER_LIVE=1 "$PY" test_resilience.py
run "critical metrics"    "$PY" test_critical_metrics.py
if [[ "${RAM_KEEPER_DANGEROUS_TESTS:-0}" == "1" ]]; then
  run "no OOM stress (live)" "$PY" test_no_oom_kill.py
else
  run "no OOM safety"     "$PY" test_no_oom_kill.py
fi
run "pass budget"        "$PY" test_pass_budget.py

echo
echo "notifier (real script, stub binaries, throwaway HOME):"
run "notifier e2e"       bash test_notifier_e2e.sh

echo
echo "unit-test suites (pytest or unittest fallback):"
# These are the biggest suites in the repo (270+ tests). Prefer pytest when
# available, and fall back to stdlib `unittest` via tests/_pytest_shim.py so a
# desktop without pytest still executes every unit test instead of silently
# skipping them.
PYTEST_PY=""
for candidate in "$PY" /usr/bin/python3 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import pytest' >/dev/null 2>&1; then
    PYTEST_PY="$candidate"
    break
  fi
done
UNIT_MODULES=(
  test_ram_keeper_core.py
  test_integration.py
  test_bugfixes.py
  test_reclaim_policy.py
  test_config_attacks.py
  test_unit_hardening.py
  test_config_isolation.py
)
if [[ -n "$PYTEST_PY" ]]; then
  run "pytest (core, policy, attacks)" \
    "$PYTEST_PY" -m pytest -q -p no:cacheprovider "${UNIT_MODULES[@]}"
else
  run "unittest fallback (core, policy, attacks)" \
    "$PY" -m unittest \
      test_ram_keeper_core test_integration test_bugfixes \
      test_reclaim_policy test_config_attacks test_unit_hardening \
      test_config_isolation
fi

echo
if (( fails == 0 && skipped == 0 )); then
  echo "all suites passed"
elif (( fails == 0 )); then
  echo "executed suites passed ($skipped skipped)"
else
  echo "$fails suite(s) failed"
fi
exit $(( fails > 0 ? 1 : 0 ))