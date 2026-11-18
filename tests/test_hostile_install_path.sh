#!/usr/bin/env bash
# Render the units through the SHIPPED renderer and prove systemd accepts and
# executes the result.
#
# On CI runners or containers without a live `systemd --user` manager, the
# rendering and quoting assertions run unconditionally and only the live
# `systemctl --user start` step is skipped cleanly.
set -uo pipefail

PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER="$PLUGIN/helper/render_unit.py"

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

HAS_BUS=0
if [[ -z "${CI:-}" || "${RAM_KEEPER_LIVE:-0}" == "1" ]]; then
  if command -v systemctl >/dev/null 2>&1 \
     && { [[ -e "/run/user/$(id -u 2>/dev/null || echo 0)/bus" ]] || [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; } \
     && systemctl --user show-environment >/dev/null 2>&1; then
    HAS_BUS=1
  fi
fi

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
B="$(mktemp -d)"
UNIT_DIR="$HOME/.config/systemd/user"
cleanup() {
  rm -rf "$B"
  [[ -n "$_own_cfg" ]] && rm -f "$_own_cfg"
  [[ -n "$_own_state" ]] && rm -rf "$_own_state"
  if (( HAS_BUS )); then
    rm -f "$UNIT_DIR"/rk-hostile-*.service
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

ok()  { echo "  ok   $1"; }
bad() { echo "  FAIL $1"; fails=$((fails + 1)); }

render() {  # render <template> <dir> [python]
  local r="$RENDER" tpl="$1"
  if command -v cygpath >/dev/null 2>&1; then
    r="$(cygpath -m "$r")"
    tpl="$(cygpath -m "$tpl")"
  fi
  MSYS2_ARG_CONV_EXCL='*' PLUGIN_DIR="$2" "$PY" "$r" --stdout "$tpl" "$2" "${3:-/usr/bin/python3}"
}

run_unit() {  # run_unit <label> <execstart-line>
  local label="$1" line="$2" unit
  unit="rk-hostile-$$-$RANDOM"
  mkdir -p "$UNIT_DIR"
  {
    echo "[Unit]"
    echo "Description=ram-keeper hostile path probe"
    echo "[Service]"
    echo "Type=oneshot"
    echo "Environment=RAM_KEEPER_CONFIG=$RAM_KEEPER_CONFIG"
    echo "Environment=RAM_KEEPER_STATE_DIR=$RAM_KEEPER_STATE_DIR"
    echo "$line"
  } >"$UNIT_DIR/$unit.service"
  systemctl --user daemon-reload >/dev/null 2>&1
  systemctl --user start "$unit.service" >/dev/null 2>&1
  local result status
  result="$(systemctl --user show "$unit.service" -p Result --value 2>/dev/null)"
  status="$(systemctl --user show "$unit.service" -p ExecMainStatus --value 2>/dev/null)"
  systemctl --user stop "$unit.service" >/dev/null 2>&1
  systemctl --user reset-failed "$unit.service" >/dev/null 2>&1
  rm -f "$UNIT_DIR/$unit.service"
  if [[ "$result" == "success" && "$status" == "0" ]]; then
    ok "$label"
  else
    bad "$label (Result=$result ExecMainStatus=$status)"
    echo "         $line"
  fi
}

echo "── diretórios hostis: renderizar e EXECUTAR no systemd ──"
if (( HAS_BUS == 0 )); then
  echo "  SKIP: no systemd --user bus; rendering-only checks follow"
fi
for dirname in 'plain' 'with space' 'with|pipe' 'with&amp' "with'quote" \
               'with@at' 'with"double' 'with%percent' 'with$dollar' \
               'with;semicolon' 'with#hash' 'with,comma' 'with=equals'; do
  dir="$B/$dirname"
  mkdir -p "$dir/helper"
  cp "$PLUGIN/helper/ram_keeper.py" "$dir/helper/"

  out="$(render "$PLUGIN/units/ram-keeper.service" "$dir" 2>&1)"
  if grep -q '@PLUGIN_DIR@' <<<"$out"; then
    bad "$dirname — placeholder sobreviveu"; continue
  fi
  line="$(grep '^ExecStart=' <<<"$out" | head -1)"
  if [[ -z "$line" ]]; then
    bad "$dirname — sem ExecStart"; printf '%s\n' "$out" | sed 's/^/         /'; continue
  fi
  ok "render: $dirname"
  if (( HAS_BUS )); then
    run_unit "systemd executa: $dirname" "${line/--daemon/--once}"
  fi
done

echo
echo "── aspas: o argumento INTEIRO é citado, não só o diretório ──"
space="$B/quote check"
mkdir -p "$space/helper" && cp "$PLUGIN/helper/ram_keeper.py" "$space/helper/"
line="$(render "$PLUGIN/units/ram-keeper.service" "$space" | grep '^ExecStart=' | head -1)"
want="ExecStart=/usr/bin/python3 \"$space/helper/ram_keeper.py\" --daemon"
if [[ "$line" == "$want" ]]; then
  ok "caminho com espaço entre aspas por inteiro"
else
  bad "aspas erradas"
  echo "         esperado: $want"
  echo "         obtido:   $line"
fi

echo
echo "── o notifier recebe o mesmo tratamento + PYTHON_OVERRIDE ──"
notify_out="$(render "$PLUGIN/units/ram-keeper-notify.service.in" "$space" "/opt/py 3/bin/python3")"
line="$(grep '^ExecStart=' <<<"$notify_out" | head -1)"
want="ExecStart=\"$space/helper/ram_keeper_notify\""
if [[ "$line" == "$want" ]]; then
  ok "notifier ExecStart com espaço"
else
  bad "notifier ExecStart com espaço"
  echo "         esperado: $want"
  echo "         obtido:   $line"
fi
env_line="$(grep '^Environment=' <<<"$notify_out" | head -1)"
want_env='Environment="RAM_KEEPER_PYTHON=/opt/py 3/bin/python3"'
if [[ "$env_line" == "$want_env" ]]; then
  ok "notifier propaga PYTHON_OVERRIDE com aspas"
else
  bad "notifier PYTHON_OVERRIDE incorreto"
  echo "         esperado: $want_env"
  echo "         obtido:   $env_line"
fi

echo
echo "── um caminho que systemd NÃO consegue representar é recusado ──"
newline_dir="/tmp/broken"$'\n'"path"
if out="$(render "$PLUGIN/units/ram-keeper.service" "$newline_dir" 2>&1)"; then
  bad "aceitou um caminho com newline (o unit file viraria duas linhas)"
else
  ok "recusou um caminho com newline"
fi

echo
echo "── o instalador usa o MESMO renderizador ──"
grep -q 'helper/render_unit.py' "$PLUGIN/install.sh" \
  && ok "install.sh delega para render_unit.py" \
  || bad "install.sh não usa o renderizador compartilhado"

echo
[[ "$fails" -eq 0 ]] && echo "todos os paths renderizam e executam" \
                     || echo "$fails falha(s)"
exit "$fails"
