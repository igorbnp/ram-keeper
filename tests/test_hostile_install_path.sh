#!/usr/bin/env bash
# Render the units through the SHIPPED renderer and prove systemd accepts and
# executes the result.
#
# The earlier version of this file carried its own copy of the substitution, so
# it passed while the installer failed on a directory containing a space. Both
# now call helper/render_unit.py — the single implementation — and every case
# ends in a real `systemctl --user start`, because the quoting rules live in
# systemd, not in us.
set -uo pipefail

PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER="$PLUGIN/helper/render_unit.py"
fails=0
B="$(mktemp -d)"
UNIT_DIR="$HOME/.config/systemd/user"
trap 'rm -rf "$B"; rm -f "$UNIT_DIR"/rk-hostile-*.service; systemctl --user daemon-reload >/dev/null 2>&1' EXIT

ok()  { echo "  ok   $1"; }
bad() { echo "  FAIL $1"; fails=$((fails + 1)); }

render() {  # render <template> <dir> [python]
  PLUGIN_DIR="$2" /usr/bin/python3 "$RENDER" --stdout "$1" "$2" "${3:-/usr/bin/python3}"
}

# Start a throwaway unit from one rendered ExecStart line and report the result.
run_unit() {  # run_unit <label> <execstart-line>
  local label="$1" line="$2" unit
  unit="rk-hostile-$$-$RANDOM"
  mkdir -p "$UNIT_DIR"
  {
    echo "[Unit]"
    echo "Description=ram-keeper hostile path probe"
    echo "[Service]"
    echo "Type=oneshot"
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
# A ram-keeper copy is placed in each directory so --once runs the real helper.
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
  # The helper copies are identical; --once exits 0 only if it really ran.
  run_unit "systemd executa: $dirname" "${line/--daemon/--once}"
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
echo "── o notifier recebe o mesmo tratamento ──"
line="$(render "$PLUGIN/units/ram-keeper-notify.service.in" "$space" | grep '^ExecStart=' | head -1)"
want="ExecStart=\"$space/helper/ram_keeper_notify\""
if [[ "$line" == "$want" ]]; then
  ok "notifier com espaço"
else
  bad "notifier com espaço"
  echo "         esperado: $want"
  echo "         obtido:   $line"
fi

echo
echo "── um caminho que systemd NÃO consegue representar é recusado ──"
newline_dir="$B/broken"$'\n'"path"
mkdir -p "$newline_dir"
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
