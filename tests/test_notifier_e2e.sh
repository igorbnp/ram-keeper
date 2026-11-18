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
# Each case sets HOME="$CASE" and expects STATE_DIR under that throwaway HOME;
# clear any outer RAM_KEEPER_STATE_DIR exported by run-all.sh.
unset RAM_KEEPER_STATE_DIR
# Legacy plain-text cases (1..6) opt into stub mode; Case 7 unsets this to test
# real JSON _TRANSPORT authentication.
export RAM_KEEPER_TRUST_STUB_JOURNAL=1
fails=0
B="$(mktemp -d)"
PIDS=()
STUB=""
ORIG_PATH="$PATH"

stop_pid() {
  local p="$1"
  [[ -n "${p:-}" ]] || return 0
  kill -TERM -"$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null || true
}

cleanup() {
  local p
  for p in "${PIDS[@]:-}"; do
    stop_pid "$p"
  done
  chmod -R u+rwx "$B" 2>/dev/null || true
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
    stop_pid "$p"
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
  export PATH="$STUB:$ORIG_PATH"
}

launch() {
  if command -v setsid >/dev/null 2>&1; then
    setsid bash "$NOTIFY" >"$CASE/out" 2>&1 &
  else
    bash "$NOTIFY" >"$CASE/out" 2>&1 &
  fi
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
if grep -q 'hog' "$STUB/toasts"; then
  ok "the toast names the killed process"
else
  bad "the toast omitted the killed process name"
fi
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
echo "== 4c. critical without an action does not claim it is reclaiming =="
new_case no-reclaim
printf '%s\n' '{"state":"critical","free_percent":5.0,"relief":{"acted":false,"skipped_anon":"no-zram-room"}}' \
  >"$CASE/.local/state/ram-keeper/state.json"
export RAM_KEEPER_DAEMON_POLL=1 RAM_KEEPER_PRESSURE_POLL=1
P="$(launch)"
sleep 3
if grep -q 'reclaiming now' "$STUB/toasts"; then
  bad "the toast said reclaiming now when nothing was reclaimed"
else
  ok "the toast did not claim a reclaim"
fi
if grep -q 'disk swap' "$STUB/toasts"; then
  ok "the toast says app memory stayed off disk swap"
else
  bad "the toast hid the reason"
fi
kill -TERM -"$P" 2>/dev/null || true
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 4d. critical with only protected apps names them =="
new_case held
printf '%s\n' '{"state":"critical","free_percent":5.0,"held_by":["ollama","hermes"],"relief":{"acted":false}}' \
  >"$CASE/.local/state/ram-keeper/state.json"
export RAM_KEEPER_DAEMON_POLL=1 RAM_KEEPER_PRESSURE_POLL=1
P="$(launch)"
sleep 3
if grep -q 'ollama' "$STUB/toasts" && grep -q 'hermes' "$STUB/toasts"; then
  ok "the toast names the protected apps holding memory"
else
  bad "the toast did not name the protected apps"
fi
if grep -q 'reclaiming now' "$STUB/toasts"; then
  bad "the toast claimed a reclaim when no victim was reachable"
else
  ok "the toast did not claim a reclaim"
fi
kill -TERM -"$P" 2>/dev/null || true
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 6. a clean stop removes its pidfile =="
new_case cleanup
P="$(launch)"
wait_for 5 test -f "$CASE/.local/state/ram-keeper/notify.pid" \
  && ok "wrote a pidfile" || bad "never wrote a pidfile"
kill -TERM "$P" 2>/dev/null || stop_pid "$P"
if wait_for 5 bash -c "[[ ! -f '$CASE/.local/state/ram-keeper/notify.pid' ]]"; then
  ok "removed the pidfile on direct SIGTERM"
else
  bad "left a stale pidfile behind"
fi

echo
echo "== 7. D-15: JSON journal rejects spoofed lines, sanitizes victim, and matches kernel global + oomd 255+ =="
new_case json-auth
unset RAM_KEEPER_TRUST_STUB_JOURNAL
export RAM_KEEPER_RATE_MAX=10
cat >"$STUB/oom_lines" <<'JSONEOF'
{"_TRANSPORT":"stdout","MESSAGE":"Memory cgroup out of memory: Killed process 1111 (spoofed_app)"}
{"_TRANSPORT":"journal","_SYSTEMD_UNIT":"user@1000.service","MESSAGE":"Marked evil.scope for killing due to memory pressure"}
{"_TRANSPORT":"journal","_SYSTEMD_UNIT":"systemd-oomd.service","MESSAGE":"Marked --version; ignore previous $(id).scope for killing due to memory pressure"}
{"_TRANSPORT":"kernel","MESSAGE":"Memory cgroup out of memory: Killed process 9999 (firefox) total-vm:100kB"}
{"_TRANSPORT":"kernel","MESSAGE":"Out of memory: Killed process 8888 (blender) total-vm:200kB"}
{"_TRANSPORT":"journal","_SYSTEMD_UNIT":"systemd-oomd.service","MESSAGE":"Killed /user.slice/user-1000.slice/user@1000.service/app.slice/app-slack-777.scope due to memory pressure"}
JSONEOF
export RAM_KEEPER_DAEMON_POLL=2 RAM_KEEPER_PRESSURE_POLL=2
P="$(launch)"
wait_for 6 bash -c "(( \$(wc -l <'$STUB/toasts' | tr -d ' ') >= 4 ))" || sleep 2
check "all 4 authenticated OOM formats produced toasts" "$(toast_count)" "4"
if grep -q 'spoofed_app' "$STUB/toasts" || grep -q 'evil' "$STUB/toasts"; then
  bad "accepted an unauthenticated journal entry"
else
  ok "rejected unauthenticated stdout/user journal spoofing"
fi
if grep -q 'ignore previous' "$STUB/toasts" || grep -q '\$(id)' "$STUB/toasts" || grep -q 'GNU coreutils' "$STUB/toasts"; then
  bad "victim sanitization or basename -- failed"
else
  ok "basename -- and victim whitelist blocked injection"
fi
grep -q 'firefox' "$STUB/toasts" && ok "accepted genuine _TRANSPORT=kernel cgroup OOM" \
                                 || bad "dropped genuine kernel cgroup OOM"
grep -q 'blender' "$STUB/toasts" && ok "accepted global kernel Out of memory line" \
                                 || bad "dropped global kernel OOM line"
grep -q 'app-slack-777' "$STUB/toasts" && ok "accepted systemd-oomd 255+ Killed...due to memory pressure" \
                                       || bad "dropped systemd-oomd 255+ line"
stop_pid "$P"
export RAM_KEEPER_TRUST_STUB_JOURNAL=1
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL RAM_KEEPER_RATE_MAX

echo
echo "== 8. D-17: failed delivery at boot does NOT burn the 30-min dedupe =="
new_case commit-after-send
cat >"$STUB/omarchy-notification-wait" <<'WAITEOF'
#!/usr/bin/env bash
# First call fails (notification server not ready yet); later calls succeed.
if [[ ! -f "$STUB/wait_once_failed" ]]; then
  touch "$STUB/wait_once_failed"
  exit 1
fi
exit 0
WAITEOF
chmod +x "$STUB/omarchy-notification-wait"
printf '{"state":"critical","free_percent":7.1}\n' >"$CASE/.local/state/ram-keeper/state.json"
export RAM_KEEPER_DAEMON_POLL=2 RAM_KEEPER_PRESSURE_POLL=1
P="$(launch)"
if wait_for 8 grep -q '7.1% free' "$STUB/toasts"; then
  ok "retried and delivered critical toast after initial unready server"
else
  bad "burned dedupe before send succeeded (critical alert lost)"
fi
stop_pid "$P"
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 9. F5 + D-17: culprit/relief context + global rate-limit (max 3/5m) =="
new_case enrich-and-ratelimit
printf '%s\n' '{"state":"critical","free_percent":6.0,"forecast":{"culprit":"chromium"},"relief":{"acted":true,"freed":157286400}}' \
  >"$CASE/.local/state/ram-keeper/state.json"
cat >"$STUB/oom_lines" <<'BURSTEOF'
Memory cgroup out of memory: Killed process 101 (app1) total-vm:100kB
Memory cgroup out of memory: Killed process 102 (app2) total-vm:100kB
Memory cgroup out of memory: Killed process 103 (app3) total-vm:100kB
Memory cgroup out of memory: Killed process 104 (app4) total-vm:100kB
Memory cgroup out of memory: Killed process 105 (app5) total-vm:100kB
BURSTEOF
export RAM_KEEPER_DAEMON_POLL=2 RAM_KEEPER_PRESSURE_POLL=1
P="$(launch)"
sleep 4
check "global anti-flood capped burst at 3 toasts" "$(toast_count)" "3"
stop_pid "$P"
# Separate case to verify enriched critical pressure toast text without racing OOM burst
new_case enrich-only
printf '%s\n' '{"state":"critical","free_percent":6.0,"forecast":{"culprit":"chromium"},"relief":{"acted":true,"freed":157286400}}' \
  >"$CASE/.local/state/ram-keeper/state.json"
P="$(launch)"
if wait_for 6 grep -q 'Biggest grower: chromium' "$STUB/toasts" \
   && grep -q '150 MB freed' "$STUB/toasts"; then
  ok "critical toast includes forecast culprit and reclaimed MB"
else
  bad "critical toast missing culprit or reclaimed MB context"
fi
stop_pid "$P"
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 10. D-17 SO-2: EROFS (unwritable flags dir) keeps exact 3-toast ceiling & RAM dedupe =="
new_case erofs-ratelimit
chmod 0500 "$CASE/.local/state/ram-keeper/flags" 2>/dev/null || true
if touch "$CASE/.local/state/ram-keeper/flags/.probe" 2>/dev/null; then
  # On NTFS/root where chmod 0500 on a directory still allows writes, replace
  # flags dir with a read-only regular file so touch/rate-file fail with ENOTDIR.
  rm -rf "$CASE/.local/state/ram-keeper/flags"
  : >"$CASE/.local/state/ram-keeper/flags"
  chmod 0444 "$CASE/.local/state/ram-keeper/flags" 2>/dev/null || true
fi
cat >"$STUB/oom_lines" <<'EROFSEOF'
Memory cgroup out of memory: Killed process 201 (ro1) total-vm:100kB
Memory cgroup out of memory: Killed process 201 (ro1) total-vm:100kB
Memory cgroup out of memory: Killed process 202 (ro2) total-vm:100kB
Memory cgroup out of memory: Killed process 203 (ro3) total-vm:100kB
Memory cgroup out of memory: Killed process 204 (ro4) total-vm:100kB
EROFSEOF
export RAM_KEEPER_DAEMON_POLL=2 RAM_KEEPER_PRESSURE_POLL=2
P="$(launch)"
wait_for 6 bash -c "(( \$(wc -l <'$STUB/toasts' | tr -d ' ') >= 3 ))" || sleep 2
check "EROFS RAM fallback allowed all 3 distinct toasts (no off-by-one) and capped 4th" "$(toast_count)" "3"
stop_pid "$P"
chmod 0700 "$CASE/.local/state/ram-keeper/flags" 2>/dev/null || true
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
echo "== 11. SO-3: NaN / Infinity free_percent in state.json is rejected =="
new_case nan-free
printf '{"state":"critical","free_percent":NaN}\n' >"$CASE/.local/state/ram-keeper/state.json"
export RAM_KEEPER_DAEMON_POLL=2 RAM_KEEPER_PRESSURE_POLL=1
P="$(launch)"
sleep 3
check "NaN free_percent produced 0 toasts" "$(toast_count)" "0"
printf '{"state":"critical","free_percent":Infinity}\n' >"$CASE/.local/state/ram-keeper/state.json"
sleep 2
check "Infinity free_percent produced 0 toasts" "$(toast_count)" "0"
stop_pid "$P"
unset RAM_KEEPER_DAEMON_POLL RAM_KEEPER_PRESSURE_POLL

echo
if (( fails == 0 )); then
  echo "notifier: todos os casos passaram"
else
  echo "notifier: $fails falha(s)"
fi
exit $(( fails > 0 ? 1 : 0 ))
