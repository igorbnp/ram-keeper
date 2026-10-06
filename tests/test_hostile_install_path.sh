#!/bin/bash
# Prove the unit renders correctly for any plausible home directory, including
# ones with spaces, |, & and @ in the name.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRCU="$HERE/../units"
PYTHON=/usr/bin/python3
fails=0

render_unit() {
  local src="$1" dst="$2" plugin_dir="$3"
  "$PYTHON" - "$src" "$dst" "$plugin_dir" <<'PYEOF'
import sys
src, dst, plugin_dir = sys.argv[1:4]
content = open(src, encoding="utf-8").read()
rendered = content.replace("@PLUGIN_DIR@", plugin_dir)
if not rendered.strip():
    sys.exit(f"rendered unit is empty: {dst}")
if "@PLUGIN_DIR@" in rendered:
    sys.exit(f"template placeholder survived in {dst}")
open(dst, "w", encoding="utf-8").write(rendered)
PYEOF
}

check_path() {
  local label="$1" dir="$2"
  mkdir -p "$dir"
  cp "$SRCU/ram-keeper.service" "$SRCU/ram-keeper-notify.service.in" "$dir/" 2>/dev/null
  if render_unit "$dir/ram-keeper.service" /tmp/u1 "$dir" \
     && render_unit "$dir/ram-keeper-notify.service.in" /tmp/u2 "$dir"; then
    if grep -q "ExecStart=/usr/bin/python3 $dir/helper/ram_keeper.py" /tmp/u1; then
      echo "  ok   $label"
    else
      echo "  FAIL $label — ExecStart incorreto:"; grep ExecStart /tmp/u1 | sed 's/^/         /'
      fails=$((fails+1))
    fi
  else
    echo "  FAIL $label — render falhou"; fails=$((fails+1))
  fi
  rm -rf "$dir"
}

B="$(mktemp -d)/pt"
rm -rf "$B"; mkdir -p "$B"
check_path "path simples"            "$B/a"
check_path "com espaço"              "$B/with space"
check_path "com pipe |"              "$B/with|pipe"
check_path "com amp &"               "$B/with&amp"
check_path "com @ no meio"           "$B/with@at"
check_path "com % e \$"              "$B/with%dollar"
check_path "com aspas"               "$B/with'quote"
rm -rf "$B" /tmp/u1 /tmp/u2
echo
[ "$fails" -eq 0 ] && echo "todos os paths renderizam" || echo "$fails falha(s)"
exit $fails
