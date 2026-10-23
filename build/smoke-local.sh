#!/usr/bin/env bash
# Single-machine smoke run of a Whitney archive inside ubuntu:26.04.
#
# usage: build/smoke-local.sh <whitney-KEY.tar.gz>
#
# Run it on a host whose CPU supports the archive's arch (znver4 on a Zen 4
# builder). The archive is extracted on the host and mounted read-only at
# /opt/whitney. Inside the container, 4 control_servers listen on 127.0.0.1
# ports 11111-11114 (event, db, exec0, exec1), each with -w /work/<role> and
# TMPDIR=/work/tmp, and whitney-runner runs against a roles.json for them.
# roles.json gives each role the single-host metrics port (event 9101, db
# 9102, exec0 9110, exec1 9111).
#
# Assertions (each prints PASS: <name>):
#   smoke_run:                  exit 0; trials.jsonl has 2 batch sizes x 3
#                               blocks = 6 lines, 2 of them warmup; every
#                               tps > 0 and nexec == 2; phase times > 0 and
#                               within elapsed_ns; runner_cpu_ns > 0.
#   smoke_clears_work_dirs:     a sentinel in /work/db is gone after the run;
#                               the runner log shows stop, wait, clear for
#                               every role before any start, in each
#                               batch-size run.
#   smoke_no_daemons_after_exit: no hog-(db|event|exec|indexer) process is
#                               left after a clean exit.
#   smoke_metrics_ports:        every daemon logged its metrics server on the
#                               --metrics-port from roles.json.
#   smoke_recovers_after_killed_runner: kill -9 a long runner (one batch
#                               size, 200 blocks) after its first JSON line,
#                               while hog-db and both hog-exec run (its
#                               daemons stay up), run it again:
#                               exit 0, a full trials.jsonl, and never more
#                               than one hog-db process during that run.
set -euo pipefail

TEST_IMAGE=ubuntu:26.04
# Runtime packages a host installs, plus test tools (procps: pgrep, jq).
PACKAGES="libjemalloc2 libatomic1 libsnappy1v5 procps jq"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[[ $# -eq 1 ]] || {
  echo "usage: $0 <whitney-KEY.tar.gz>" >&2
  exit 2
}
tarball="$1"
[[ -f "$tarball" ]] || fail "no such archive: $tarball"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/x"
tar -C "$work/x" -xzf "$tarball" || fail "cannot extract $tarball"
shopt -s nullglob
tops=("$work"/x/*)
shopt -u nullglob
[[ ${#tops[@]} -eq 1 && -d "${tops[0]}" ]] || fail "archive must hold exactly one top dir"
root="${tops[0]}"

# The script runs from a file, not from bash -c, so that no process command
# line holds the text "hog-db" and pgrep -f sees only real daemons.
cat >"$work/inner.sh" <<'INNER'
set -euo pipefail
B=/opt/whitney/bin
ROLES=(event db exec0 exec1)
PORTS=(11111 11112 11113 11114)
METRICS_PORTS=(9101 9102 9110 9111)
RUNNER_ARGS=(--roles /work/roles.json --accounts 10 --txs-per-block 4000
  --batch-sizes "1,5" --warmup-blocks 1 --timed-blocks 2)

dump() {
  for f in /work/runner*.log; do
    [ -f "$f" ] || continue
    echo "--- tail $f"
    tail -n 40 "$f"
  done
  for f in /work/tmp/hogd_stderr_*; do
    [ -s "$f" ] || continue
    echo "--- tail $f"
    tail -n 15 "$f"
  done
}

fail() {
  echo "FAIL: $*"
  dump
  exit 1
}

apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends $PACKAGES >/dev/null

mkdir -p /work/tmp
for i in "${!ROLES[@]}"; do
  mkdir -p "/work/${ROLES[$i]}"
  TMPDIR=/work/tmp "$B/control_server" -p "${PORTS[$i]}" -w "/work/${ROLES[$i]}" \
    >"/work/control-${ROLES[$i]}.log" 2>&1 &
done
for port in "${PORTS[@]}"; do
  ok=0
  for _ in $(seq 1 100); do
    if "$B/control_cli" 127.0.0.1 "$port" ready >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 0.2
  done
  [ "$ok" = 1 ] || fail "control_server on port $port not ready"
done

role_json() { # name control-port metrics-port
  printf '{"host":"127.0.0.1","control_port":%s,"metrics_port":%s,"work_dir":"/work/%s","cpu_affinity":""}' "$2" "$3" "$1"
}
cat >/work/roles.json <<EOF
{"bin_dir":"$B","runner_bind_ip":"127.0.0.1",
 "event":$(role_json event 11111 9101),
 "db":$(role_json db 11112 9102),
 "indexer":null,
 "execs":[$(role_json exec0 11113 9110),$(role_json exec1 11114 9111)]}
EOF

# check_trials <file>: 6 lines, in run order, with sane values.
check_trials() {
  local f="$1"
  [ -f "$f" ] || fail "$f missing"
  [ "$(wc -l <"$f")" -eq 6 ] || fail "$f has $(wc -l <"$f") lines, want 6"
  [ "$(jq -s 'map(select(.warmup == true)) | length' "$f")" -eq 2 ] ||
    fail "$f: want 2 warmup lines"
  jq -s -e 'all(.tps > 0 and .nexec == 2)' "$f" >/dev/null ||
    fail "$f: want every tps > 0 and nexec == 2"
  jq -s -e '
    map(.batch_size) == [1,1,1,5,5,5] and
    map(.block_index) == [0,1,2,0,1,2] and
    map(.warmup) == [true,false,false,true,false,false] and
    all(.txs == 4000 and .accounts == 10 and .experiment == "erc20_payments"
        and .elapsed_ns > 0 and (.work_tx_hash | test("^[0-9a-f]{64}$"))
        and .open_ns > 0 and .exec_ns > 0 and .close_ns > 0
        and .open_ns + .exec_ns + .close_ns <= .elapsed_ns
        and .runner_cpu_ns > 0) and
    (.[0:3] | map(.work_tx_hash) | unique | length) == 1 and
    (.[3:6] | map(.work_tx_hash) | unique | length) == 1' "$f" >/dev/null ||
    fail "$f: batch_size, block_index, warmup or fields not as expected"
  if grep -vqE '"tps":[0-9]+\.[0-9]+,' "$f"; then
    fail "$f: tps must carry a fractional part"
  fi
}

# check_reset_order <log>: per batch-size run, every role does stop, wait,
# clear, and only then are the daemons started.
check_reset_order() {
  local log="$1" want="" one="" r
  for r in "${ROLES[@]}"; do one+="$r stop,$r wait,$r clear,"; done
  for r in "${ROLES[@]}"; do one+="$r start,"; done
  want="$one$one"
  got="$(grep -oE 'role=[a-z0-9]+ (stop|wait|clear|start)$' "$log" |
    sed 's/^role=//' | tr '\n' ',')"
  [ "$got" = "$want" ] || fail "reset order in $log:
got:  $got
want: $want"
  [ "$(grep -cE 'role=[a-z0-9]+ teardown$' "$log")" -eq 8 ] ||
    fail "want 8 teardown lines in $log"
}

check_no_daemons() {
  if pgrep -af 'hog-(db|event|exec|indexer)'; then
    fail "daemons left after a clean runner exit"
  fi
}

# --- smoke_run, smoke_clears_work_dirs, smoke_no_daemons_after_exit
echo sentinel >/work/db/sentinel
rc=0
timeout 600 "$B/whitney-runner" "${RUNNER_ARGS[@]}" --json-out /work/trials.jsonl \
  >/work/runner1.log 2>&1 || rc=$?
[ "$rc" -eq 0 ] || fail "whitney-runner exited $rc"
check_trials /work/trials.jsonl
echo "PASS: smoke_run"
cat /work/trials.jsonl
[ ! -e /work/db/sentinel ] || fail "sentinel in /work/db survived the run"
check_reset_order /work/runner1.log
echo "PASS: smoke_clears_work_dirs"
grep -E 'role=[a-z0-9]+ (stop|wait|clear|start|teardown)$' /work/runner1.log | head -n 16
check_no_daemons
echo "PASS: smoke_no_daemons_after_exit"
grep -E '^time taken: ' /work/runner1.log
# hogd logs "PORT <name>.metrics=<port>" once its metrics server listens.
for want in event.metrics=9101 hog_db.metrics=9102 hog_exec.metrics=9110 \
  hog_exec.metrics=9111; do
  grep -rqF "PORT $want" /work/tmp || fail "no daemon log line PORT $want"
done
echo "PASS: smoke_metrics_ports"

# --- smoke_recovers_after_killed_runner
# One batch size and many blocks: after the first JSON line the daemons stay
# up for the rest of the run, so the kill never lands between batch-size
# runs (when the runner has stopped them). The kill also waits until hog-db
# and both hog-exec are seen running.
KILL_ARGS=(--roles /work/roles.json --accounts 10 --txs-per-block 4000
  --batch-sizes 1 --warmup-blocks 0 --timed-blocks 200)
"$B/whitney-runner" "${KILL_ARGS[@]}" --json-out /work/trials2.jsonl \
  >/work/runner2.log 2>&1 &
pid=$!
live=0
for _ in $(seq 1 3000); do
  kill -0 "$pid" 2>/dev/null || fail "runner exited before its first JSON line"
  if [ -s /work/trials2.jsonl ] && [ "$(pgrep -c -x hog-db || true)" -eq 1 ] &&
    [ "$(pgrep -c -x hog-exec || true)" -eq 2 ]; then
    live=1
    break
  fi
  sleep 0.1
done
[ "$live" = 1 ] || fail "no JSON line with hog-db and 2 hog-exec running within 300 s"
kill -9 "$pid"
wait "$pid" 2>/dev/null || true
# stdout is a file here (a pipe under ssh): only line buffering keeps these
# lines after kill -9.
grep -qE 'role=[a-z0-9]+ (stop|wait|clear|start)$' /work/runner2.log ||
  fail "killed runner lost its stdout lines (stdout not line-buffered)"
grep -q 'Start trial' /work/runner2.log ||
  fail "killed runner lost its 'Start trial' line (stdout not line-buffered)"
left="$(pgrep -c -x hog-db || true)"
[ "$left" -eq 1 ] || fail "want the killed runner's hog-db still up, found $left"
echo "after kill -9: $(wc -l </work/trials2.jsonl) JSON line(s), hog-db processes: $left"
echo sentinel >/work/db/sentinel

timeout 600 "$B/whitney-runner" "${RUNNER_ARGS[@]}" --json-out /work/trials2.jsonl \
  >/work/runner3.log 2>&1 &
pid=$!
max=0 samples=0 seen_one=0
while kill -0 "$pid" 2>/dev/null; do
  n="$(pgrep -c -x hog-db || true)"
  samples=$((samples + 1))
  [ "$n" -gt "$max" ] && max="$n"
  [ "$n" -eq 1 ] && seen_one=1
  sleep 0.1
done
rc=0
wait "$pid" || rc=$?
[ "$rc" -eq 0 ] || fail "second whitney-runner exited $rc"
check_trials /work/trials2.jsonl
[ ! -e /work/db/sentinel ] || fail "sentinel in /work/db survived the second run"
check_reset_order /work/runner3.log
[ "$max" -eq 1 ] && [ "$seen_one" -eq 1 ] ||
  fail "hog-db count during the second run: max $max (want 1)"
echo "hog-db count during the second run: max $max over $samples samples"
check_no_daemons
echo "PASS: smoke_recovers_after_killed_runner"
echo "SMOKE PASS"
INNER

docker run --rm --init -e DEBIAN_FRONTEND=noninteractive -e PACKAGES="$PACKAGES" \
  -v "$root:/opt/whitney:ro" -v "$work/inner.sh:/smoke/inner.sh:ro" \
  "$TEST_IMAGE" bash /smoke/inner.sh || fail "smoke run in $TEST_IMAGE failed"
echo "PASS: $tarball"
