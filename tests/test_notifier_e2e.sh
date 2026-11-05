#!/usr/bin/env bash
# End-to-end tests for the notifier — the part of the plugin that is supposed to
# tell a human when protection stops working.
#
# Everything here runs the SHIPPED script (no reimplementation) against stub
# binaries on PATH and a throwaway HOME, so it exercises the real control flow:
# PID-file identity, watcher supervision, dedupe, self-traffic filtering and the
# critical-pressure transition. It needs no notification daemon, no journal and
# no live cgroup, so it runs on a CI runner too.
set -uo pipefail

NOTIFY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/helper/ram_keeper_notify"
fails=0
B="$(mktemp -d)"
PIDS=()
STUB=""

cleanup() {
  local p
  for p in "${PIDS[@]:-}"; do
    [[ -n "${p:-}" ]] && kill -TERM -"$p" 2>/dev/null
  done
  rm -rf "$B"
}
trap cleanup EXIT

ok()   { echo "  ok   $1"; }
bad()  { echo "  FAIL $1"; fails=$((fails + 1)); }
check() { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (esperado '$3', obtido '$2')"; fi; }

# ---------------------------------------------------------------------------
# Stub binaries
# ---------------------------------------------------------------------------
make_stubs() {
  mkdir -p "$STUB"

  cat >"$STUB/systemctl" <<'EOF'
#!/usr/bin/env bash
# `systemctl --user is-active --quiet` — only the exit code matters.
grep -q '^inactive' "$STUB/systemctl_state" 2>/dev/null && exit 3
exit 0
EOF

cat >"$STUB/omarchy-notification-send" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB/toasts"
exit 0
EOF

cat >"$STUB/omarchy-notification-wait" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat >"$STUB/journalctl" <<'EOF'
#!/usr/bin/env bash
# Counts invocations (proves supervision) and emits the configured lines, then
# exits — exactly what a watcher looks like when journalctl dies.
echo x >>"$STUB/journal_calls"
if [[ -f "$STUB/oom_lines" ]]; then
  cat "$STUB/oom_lines"
fi
exit 0
EOF

  chmod +x "$STUB"/*
}

new_case() {
  local name="$1" p
  # Every case gets its own HOME *and its own stubs*. Sharing the stub
  # directory silently cross-contaminates cases: a watcher left running from a
  # previous case keeps appending to the same toasts file.
  for p in "${PIDS[@]:-}"; do
    [[ -n "${p:-}" ]] && kill -TERM -"$p" 2>/dev/null
  done
  PIDS=()
  CASE="$B/$name"
  HOME="$CASE"
  STUB="$CASE/bin"
  mkdir -p "$CASE/.local/state/ram-keeper/flags" "$CASE/.hermes/skills"
  make_stubs
  echo active >"$STUB/systemctl_state"
  : >"$STUB/toasts"
  : >"$STUB/journal_calls"
  export HOME STUB
  export PATH="$STUB:$PATH"
}

launch() {
  # A separate process group, so the suite can stop every supervisor at once.
  setsid bash "$NOTIFY" >"$CASE/out" 2>&1 &
  local pid=$!
  PIDS+=("$pid")
  echo "$pid"
}

wait_for() {   # wait_for <seconds> <command...>
  local deadline=$((SECONDS + $1)); shift
  while (( SECONDS < deadline )); do
    "$@" && return 0
    sleep 0.2
  done
  return 1
}

toast_count() { wc -l <"$STUB/toasts" | tr -d ' '; }
pidfile()    { cat "$CASE/.local/state/ram-keeper/notify.pid" 2>/dev/null; }

echo "== 1. a stale PID file must not disable the notifier forever =="
new_case stale-pid
sleep 300 >/dev/null 2>&1 &
VICTIM=$!
echo "$VICTIM" >"$CASE/.local/state/ram-keeper/notify.pid"
P="$(launch)"
if wait_for 6 bash -c "[[ -f '$CASE/.local/state/ram-keeper/notify.pid' ]] && [[ \"\$(cat '$CASE/.local/state/ram-keeper/notify.pid')\" != '$VICTIM' ]]"; then
  ok "replaced a pidfile pointing at an unrelated live process"
else
  bad "refused to start because an unrelated process owns the old PID"
fi
kill -TERM "$VICTIM" 2>/dev/null

echo
echo "== 2. a watcher that dies must be restarted, and duplicates deduped =="
new_case supervision
printf 'Memory cgroup out of memory: Killed process 4242 (hog) total-vm:900000kB\n' >"$STUB/oom_lines"
export RAM_KEEPER_DAEMON_POLL=1 RAM_KEEPER_PRESSURE_POLL=1
P="$(launch)"
sleep 7
calls="$(wc -l <"$STUB/journal_calls" | tr -d ' ')"
if (( calls >= 2 )); then
  ok "journalctl was restarted ($calls invocation(s))"
else
  bad "a dead OOM watcher was never restarted (journalctl ran $calls time)"
fi
check "the same kill produced exactly one toast" "$(toast_count)" "1"
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 3. the notifier must never alert on its own traffic =="
new_case self-traffic
printf 'ram-keeper: Memory cgroup out of memory (from our own prompt)\n' >"$STUB/oom_lines"
export RAM_KEEPER_DAEMON_POLL=1 RAM_KEEPER_PRESSURE_POLL=1
P="$(launch)"
sleep 4
check "no toast for the plugin's own words" "$(toast_count)" "0"
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 4. critical pressure toasts once, and a corrupt file is silent =="
new_case pressure
printf '{"state":"critical","free_percent":6.2}\n' >"$CASE/.local/state/ram-keeper/state.json"
export RAM_KEEPER_DAEMON_POLL=1 RAM_KEEPER_PRESSURE_POLL=1
P="$(launch)"
sleep 3
check "one toast on the transition into critical" "$(toast_count)" "1"
printf '%s\n' "$(cat "$STUB/toasts")" | grep -q '6.2% free' \
  && ok "the toast carries the measured figure" \
  || bad "the toast lost the free percentage"
# Same state again: it is not a new transition.
sleep 2
check "no duplicate while it stays critical" "$(toast_count)" "1"
# A half-written file must be ignored, not read as "critical".
printf '{"state": "crit' >"$CASE/.local/state/ram-keeper/state.json"
sleep 3
check "a corrupt snapshot adds no toast" "$(toast_count)" "1"
if kill -0 "$P" 2>/dev/null; then ok "the notifier survived the corrupt file"; else bad "the notifier died on a corrupt snapshot"; fi
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 4b. a free percentage that is not a number is not a prompt =="
new_case injected-free
printf '{"state":"critical","free_percent":"${HOME}"}\n' >"$CASE/.local/state/ram-keeper/state.json"
export RAM_KEEPER_DAEMON_POLL=1 RAM_KEEPER_PRESSURE_POLL=1
P="$(launch)"
sleep 3
check "a non-numeric free percentage produced no toast" "$(toast_count)" "0"
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 5. the daemon stopping is announced =="
new_case daemon-down
export RAM_KEEPER_DAEMON_POLL=1 RAM_KEEPER_PRESSURE_POLL=2
P="$(launch)"
sleep 2                       # one poll: was_alive=1
echo inactive >"$STUB/systemctl_state"
if wait_for 8 grep -q 'RAM protection stopped' "$STUB/toasts"; then
  ok "a stopped daemon produced a toast"
else
  bad "the daemon stopped and nothing was said"
fi
# And it must not nag every second while it stays down.
sleep 3
count="$(grep -c 'RAM protection stopped' "$STUB/toasts")"
[[ "$count" == "1" ]] && ok "announced the transition once, not repeatedly" \
                      || bad "nagged $count times for one failure"
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 6b. a pause the user asked for is not an alarm =="
new_case user-pause
export RAM_KEEPER_DAEMON_POLL=1 RAM_KEEPER_PRESSURE_POLL=2
P="$(launch)"
sleep 2
touch "$CASE/.local/state/ram-keeper/paused-by-user"
echo inactive >"$STUB/systemctl_state"
sleep 3
check "a user pause produced no toast" "$(toast_count)" "0"
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 6. a clean stop removes its pidfile =="
new_case cleanup
P="$(launch)"
wait_for 5 test -f "$CASE/.local/state/ram-keeper/notify.pid" \
  && ok "wrote a pidfile" || bad "never wrote a pidfile"
kill -TERM -"$P" 2>/dev/null
if wait_for 5 bash -c "[[ ! -f '$CASE/.local/state/ram-keeper/notify.pid' ]]"; then
  ok "removed the pidfile on exit"
else
  bad "left a stale pidfile behind"
fi

echo
if (( fails == 0 )); then
  echo "notifier: todos os casos passaram"
else
  echo "notifier: $fails falha(s)"
fi
exit $(( fails > 0 ? 1 : 0 ))
