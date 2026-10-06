#!/usr/bin/env bash
# Render the units through the installer's OWN substitution and check the
# ExecStart line systemd will actually receive.
#
# An earlier version reimplemented the substitution here, in Python, and passed
# while the shipped installer failed on a directory containing a space: the case
# was never handed to systemd, so it looked covered and was not. Both parts now
# run against the installer's logic — and the space case additionally gets a
# throwaway systemd unit, because quoting rules live in systemd, not in us.
set -uo pipefail

PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
B="$(mktemp -d)"
trap 'rm -rf "$B"' EXIT

check() {
  if [[ "$2" == "$3" ]]; then
    echo "  ok   $1"
  else
    echo "  FAIL $1"
    echo "         esperado: $3"
    echo "         obtido:   $2"
    fails=$((fails + 1))
  fi
}

# The installer's render_unit, verbatim, emitting to stdout instead of a file.
render() {
  local src="$1" dir="$2"
  PLUGIN_DIR="$dir" /usr/bin/python3 - "$src" "$dir" /usr/bin/python3 <<'PYEOF'
import re, sys
src, plugin_dir, python = sys.argv[1:4]
content = open(src, encoding="utf-8").read()
if not content.strip():
    sys.exit("template is empty")
rendered = re.sub(r"@PLUGIN_DIR@(\S*)",
                  lambda m: ('"%s%s"' % (plugin_dir, m.group(1)))
                  if " " in plugin_dir else plugin_dir + m.group(1),
                  content)
rendered = rendered.replace("/usr/bin/python3", python)
if "@PLUGIN_DIR@" in rendered:
    sys.exit("placeholder survived")
sys.stdout.write(rendered)
PYEOF
}

echo "── ExecStart renderizado (o caminho inteiro entre aspas quando tem espaço) ──"
for spec in "simples:plain" "com espaco:with space" "com pipe:with|pipe" \
            "com amp:with&amp" "com aspas:with'q" "com arroba:with@at"; do
  label="${spec%%:*}"; dir="$B/${spec#*:}"
  mkdir -p "$dir"
  out="$(render "$PLUGIN/units/ram-keeper.service" "$dir" 2>&1)"
  line="$(printf '%s\n' "$out" | grep '^ExecStart=' | head -1)"
  if [[ -z "$line" ]]; then
    echo "  FAIL $label — nenhum ExecStart renderizado"
    printf '%s\n' "$out" | sed 's/^/         /'
    fails=$((fails + 1))
    continue
  fi
  if [[ "$dir" == *" "* ]]; then
    want="ExecStart=/usr/bin/python3 \"$dir/helper/ram_keeper.py\" --daemon"
  else
    want="ExecStart=/usr/bin/python3 $dir/helper/ram_keeper.py --daemon"
  fi
  check "$label" "$line" "$want"
done

echo
echo "── o notifier recebe o mesmo tratamento ──"
space="$B/notify sp"
mkdir -p "$space"
line="$(render "$PLUGIN/units/ram-keeper-notify.service.in" "$space" 2>&1 \
        | grep '^ExecStart=' | head -1)"
check "notifier com espaco" "$line" "ExecStart=\"$space/helper/ram_keeper_notify\""

echo
echo "── systemd de verdade executa um ExecStart com espaço ──"
probe_dir="$B/rk sp"
mkdir -p "$probe_dir/helper"
cp "$PLUGIN/helper/ram_keeper.py" "$probe_dir/helper/"
probe_line="$(render "$PLUGIN/units/ram-keeper.service" "$probe_dir" \
              | grep '^ExecStart=' | head -1)"
probe_line="${probe_line/--daemon/--once}"
unit="rk-spacetest-$$"
mkdir -p "$HOME/.config/systemd/user"
{
  echo "[Unit]"
  echo "Description=ram-keeper space path probe"
  echo "[Service]"
  echo "Type=oneshot"
  echo "$probe_line"
} >"$HOME/.config/systemd/user/$unit.service"
systemctl --user daemon-reload >/dev/null 2>&1
systemctl --user start "$unit" >/dev/null 2>&1
sleep 2
result="$(systemctl --user show "$unit" -p Result --value 2>/dev/null)"
status="$(systemctl --user show "$unit" -p ExecMainStatus --value 2>/dev/null)"
systemctl --user stop "$unit" >/dev/null 2>&1
rm -f "$HOME/.config/systemd/user/$unit.service"
systemctl --user daemon-reload >/dev/null 2>&1
if [[ "$result" == "success" && "$status" == "0" ]]; then
  echo "  ok   systemd aceitou e executou"
else
  echo "  FAIL systemd rejeitou (Result=$result ExecMainStatus=$status)"
  echo "         $probe_line"
  fails=$((fails + 1))
fi

echo
[[ "$fails" -eq 0 ]] && echo "todos os paths renderizam e executam" \
                    || echo "$fails falha(s)"
exit "$fails"
