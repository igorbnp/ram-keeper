#!/usr/bin/env bash
# The one list of checks a hosted runner can execute. test.yml and release.yml
# both call this file. A second copy of the list is how a release used to
# publish with the panel tests unrun.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v shellcheck >/dev/null 2>&1 || ! command -v node >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq shellcheck nodejs
fi
python3 -m pip install --quiet pytest

python3 -m compileall -q helper tests
for f in install.sh helper/ram_keeper_notify tests/*.sh; do
  bash -n "$f"
done

shellcheck install.sh helper/ram_keeper_notify

node tests/test_model.js
node tests/test_unknown_ui.js

python3 tests/test_forecast.py
python3 tests/test_config_hostile.py
python3 tests/test_inviolable.py
python3 tests/test_resilience.py
python3 tests/test_pass_budget.py
bash tests/test_notifier_e2e.sh
python3 tests/test_critical_metrics.py
python3 -m pytest -q \
  tests/test_ram_keeper_core.py \
  tests/test_integration.py \
  tests/test_process_guardian.py \
  tests/test_bugfixes.py \
  tests/test_reclaim_policy.py \
  tests/test_config_attacks.py \
  tests/test_unit_hardening.py \
  tests/test_config_isolation.py
python3 tests/test_qml_structure.py
bash tests/test_hostile_install_path.sh
bash tests/test_installer_inactive_start.sh
python3 tests/test_no_oom_kill.py
