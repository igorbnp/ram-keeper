#!/usr/bin/env bash
# The one list of checks a hosted runner can execute. test.yml and release.yml
# both call this file. A second copy of the list is how a release used to
# publish with the panel tests unrun.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Install system dependencies if missing. `sudo` may not be available in
# rootless or root containers, so check before invoking it.
need_apt=()
command -v shellcheck >/dev/null 2>&1 || need_apt+=(shellcheck)
command -v node >/dev/null 2>&1       || need_apt+=(nodejs)
python3 -m venv --help >/dev/null 2>&1 || need_apt+=(python3-venv)

if (( ${#need_apt[@]} > 0 )); then
  if [ "$(id -u)" = "0" ]; then
    apt-get update -qq
    apt-get install -y -qq "${need_apt[@]}"
  elif command -v sudo >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq "${need_apt[@]}"
  else
    echo "warning: missing ${need_apt[*]} and no root access; proceeding with available tools"
  fi
fi

# PEP 668 (Ubuntu 24.04+) blocks `pip install` outside a venv. Reuse the
# active interpreter when pytest and PyYAML are already importable (offline
# runners); otherwise build a throwaway venv.
if ! python3 -c 'import pytest, yaml' >/dev/null 2>&1; then
  VENV_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rk-ci-venv.XXXXXX")"
  trap 'rm -rf "$VENV_DIR"' EXIT
  python3 -m venv "$VENV_DIR"
  # shellcheck disable=SC1091
  . "$VENV_DIR/bin/activate"
  python3 -m pip install --quiet pytest pyyaml
fi

python3 -m compileall -q helper tests
for f in install.sh helper/ram_keeper_notify tests/*.sh; do
  bash -n "$f"
done

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck install.sh helper/ram_keeper_notify
fi

# Prevent B-04 regression: YAML files with duplicate keys are silently broken.
# Python's yaml.safe_load merges duplicates without warning; a custom Loader
# that rejects them is the cheapest gate that catches a double `run:` before
# the workflow even reaches GitHub Actions.
python3 -c '
import yaml, sys, glob

class NoDuplicateLoader(yaml.SafeLoader):
    pass

def no_dup_mapping(loader, node):
    keys = set()
    for kn, _vn in node.value:
        key = loader.construct_object(kn, deep=False)
        if key in keys:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"found duplicate key ({key!r})", kn.start_mark)
        keys.add(key)
    return loader.construct_mapping(node)

NoDuplicateLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dup_mapping)

ok = True
for path in sorted(glob.glob(".github/workflows/*.yml")):
    try:
        with open(path) as f:
            yaml.load(f, Loader=NoDuplicateLoader)
    except yaml.YAMLError as e:
        print(f"FAIL: {path}: {e}", file=sys.stderr)
        ok = False
if not ok:
    sys.exit(1)
print("YAML duplicate-key check: passed")
'

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
  tests/test_bugfixes.py \
  tests/test_reclaim_policy.py \
  tests/test_config_attacks.py \
  tests/test_unit_hardening.py \
  tests/test_config_isolation.py
python3 tests/test_qml_structure.py
bash tests/test_hostile_install_path.sh
bash tests/test_installer_inactive_start.sh
python3 tests/test_no_oom_kill.py
