#!/usr/bin/env bash
# Renders infra/host-setup.sh.tftpl with tofu and runs it in ubuntu:26.04 with
# WHITNEY_DRY_RUN=1 (mount, mkfs, chown, apt, systemctl and cpupower are
# printed, not run). Needs docker, tofu, shasum.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fails=0

ok() { echo "ok   - $1"; }
bad() { echo "FAIL - $1"; fails=$((fails + 1)); }
check() { # check <desc> <cmd...>
  local d="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi
}

# Fixture archive: whitney-<key>/{bin/*,bin/contracts/*,manifest.json}
FX="$WORK/fixture"
mkdir -p "$FX/src/whitney-test-key/bin/contracts"
for b in whitney-runner control_server control_cli hog-exec hog-db hog-event hog-indexer; do
  printf '#!/bin/sh\nexit 0\n' >"$FX/src/whitney-test-key/bin/$b"
  chmod +x "$FX/src/whitney-test-key/bin/$b"
done
echo x >"$FX/src/whitney-test-key/bin/contracts/a.rv32a"
echo '{"key":"test-key"}' >"$FX/src/whitney-test-key/manifest.json"
COPYFILE_DISABLE=1 tar -czf "$FX/whitney-test-key.tar.gz" -C "$FX/src" whitney-test-key
SHA="$(shasum -a 256 "$FX/whitney-test-key.tar.gz" | cut -d' ' -f1)"

# Fixture Vector release: vector-x86_64-unknown-linux-musl/bin/vector, a stub
# that prints the pinned version and logs any other call's arguments.
mkdir -p "$FX/vsrc/vector-x86_64-unknown-linux-musl/bin"
cat >"$FX/vsrc/vector-x86_64-unknown-linux-musl/bin/vector" <<'STUB'
#!/bin/sh
if [ "$1" = --version ]; then
  echo "vector 0.43.0 (x86_64-unknown-linux-musl fixture)"
  exit 0
fi
echo "vector-args: $*"
[ "$1" = validate ] && exit 0
# Run on a config: like Vector, write a metric_to_log line to the file sink
# path, when it is a concrete path (the self-test copy). VECTOR_STUB_SILENT=1
# writes nothing, like a config that drops every event.
prev=""
for a in "$@"; do
  if [ "$prev" = --config ] && [ -z "${VECTOR_STUB_SILENT:-}" ]; then
    p="$(sed -n 's/^path = "\([^$]*\)"$/\1/p' "$a")"
    [ -n "$p" ] && echo '{"name":"load1","namespace":"host","kind":"absolute","tags":{"whitney_run":"selftest"},"gauge":{"value":0.1}}' >>"$p"
  fi
  prev="$a"
done
exit 0
STUB
chmod +x "$FX/vsrc/vector-x86_64-unknown-linux-musl/bin/vector"
COPYFILE_DISABLE=1 tar -czf "$FX/vector.tar.gz" -C "$FX/vsrc" ./vector-x86_64-unknown-linux-musl
VSHA="$(shasum -a 256 "$FX/vector.tar.gz" | cut -d' ' -f1)"

# render <out-file> <role> <storage> <sha> <ports-hcl> <roles-hcl> [ebs-volume-type]
#        [metrics-ports-hcl] [vector-sha]
render() {
  local out="$1" role="$2" storage="$3" sha="$4" ports="$5" roles="$6" vtype="${7:-gp3}"
  local metrics="${8:-"{}"}" vsha="${9:-$VSHA}"
  local d="$WORK/render-${out##*/}"
  mkdir -p "$d"
  cat >"$d/main.tf" <<TF
output "script" {
  value = templatefile("$ROOT/infra/host-setup.sh.tftpl", {
    role           = "$role"
    storage        = "$storage"
    ebs_volume_type = "$vtype"
    archive_url    = "file:///fixture/whitney-test-key.tar.gz"
    archive_sha256 = "$sha"
    control_ports  = $ports
    work_roles     = $roles
    max_lifetime_minutes = 90
    cluster_name   = "test-cluster"
    instance_type  = "r8a.large"
    metrics_ports  = $metrics
    vector_version = "0.43.0"
    vector_url     = "file:///fixture/vector.tar.gz"
    vector_sha256  = "$vsha"
  })
}
TF
  (cd "$d" && tofu apply -auto-approve -no-color >/dev/null && tofu output -raw script >"$out")
}

# run_case <name> <script> [lsblk-json] : runs in a container, leaves results in
# $WORK/<name>/. With lsblk-json, a stub lsblk prints that file for the
# MOUNTPOINTS query the ebs path makes (the container has no EBS disks).
run_case() {
  local name="$1" script="$2" lsblk_json="${3:-}" o="$WORK/$1"
  local stub="$WORK/stub-$1"
  mkdir -p "$o" "$stub"
  cp "$script" "$FX/setup.sh"
  if [ -n "$lsblk_json" ]; then
    cp "$lsblk_json" "$stub/lsblk.json"
    cat >"$stub/lsblk" <<'STUB'
#!/bin/sh
case "$*" in *MOUNTPOINTS*) cat /stub/lsblk.json; exit 0 ;; esac
exit 1
STUB
    chmod +x "$stub/lsblk"
  fi
  set +e
  docker run --rm -e WHITNEY_DRY_RUN=1 -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    -v "$FX:/fixture:ro" -v "$stub:/stub:ro" -v "$o:/out" ubuntu:26.04 bash -c '
    apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq curl jq >/dev/null 2>&1
    PATH=/stub:$PATH bash /fixture/setup.sh >/out/stdout 2>/out/stderr; echo $? >/out/exit
    cp -a /etc/systemd/system /out/units 2>/dev/null
    cp -a /etc/whitney /out/env 2>/dev/null
    cp -a /var/lib/whitney /out/state 2>/dev/null
    cp -a /opt/whitney /out/opt 2>/dev/null
    cp -a /run/whitney /out/run 2>/dev/null
    cp -a /usr/local/sbin/whitney-vector /out/whitney-vector 2>/dev/null
    # The Vector wrapper, with the stub vector: check mode, then a start
    # without and with each remote-write auth form.
    if [ -x /usr/local/sbin/whitney-vector ] && [ -x /opt/whitney/vector/bin/vector ]; then
      mkdir -p /tmp/check-tmp /tmp/silent-tmp
      TMPDIR=/tmp/check-tmp /usr/local/sbin/whitney-vector check >/out/vector-check 2>&1
      echo $? >/out/vector-check-exit
      ls -A /tmp/check-tmp >/out/vector-check-left
      VECTOR_STUB_SILENT=1 TMPDIR=/tmp/silent-tmp /usr/local/sbin/whitney-vector check \
        >/out/vector-check-silent 2>&1
      echo $? >/out/vector-check-silent-exit
      mkdir -p /tmp/rt-none /tmp/rt-basic /tmp/rt-bearer
      RUNTIME_DIRECTORY=/tmp/rt-none /usr/local/sbin/whitney-vector >/out/vector-none 2>&1
      RUNTIME_DIRECTORY=/tmp/rt-basic WHITNEY_REMOTE_WRITE_URL=https://rw.example/push \
        WHITNEY_REMOTE_WRITE_USERNAME=u1 WHITNEY_REMOTE_WRITE_PASSWORD=pw-secret \
        /usr/local/sbin/whitney-vector >/out/vector-basic 2>&1
      RUNTIME_DIRECTORY=/tmp/rt-bearer WHITNEY_REMOTE_WRITE_URL=https://rw.example/push \
        WHITNEY_REMOTE_WRITE_BEARER_TOKEN=tok-secret \
        /usr/local/sbin/whitney-vector >/out/vector-bearer 2>&1
      cp -a /tmp/rt-none /tmp/rt-basic /tmp/rt-bearer /out/ 2>/dev/null
    fi
    # The dead-man check, with systemctl stubbed: past and future expiry.
    if [ -x /usr/local/sbin/whitney-deadman ]; then
      mkdir -p /tmp/stub
      printf "#!/bin/sh\necho \"systemctl \$*\"\n" >/tmp/stub/systemctl
      chmod +x /tmp/stub/systemctl
      cp /etc/whitney/expires-at /tmp/expires-at
      echo 1 >/etc/whitney/expires-at
      PATH=/tmp/stub:$PATH /usr/local/sbin/whitney-deadman >/out/deadman-past 2>&1
      echo 9999999999 >/etc/whitney/expires-at
      PATH=/tmp/stub:$PATH /usr/local/sbin/whitney-deadman >/out/deadman-future 2>&1
      echo garbage >/etc/whitney/expires-at
      PATH=/tmp/stub:$PATH /usr/local/sbin/whitney-deadman >/out/deadman-bad 2>&1
      cp /tmp/expires-at /etc/whitney/expires-at
    fi
    # The container runs as root: give the output back, so the host user can
    # read and delete it (Docker on Linux keeps root ownership).
    chown -R "$HOST_UID:$HOST_GID" /out
    exit 0' >"$o/docker.log" 2>&1
  set -e
}

PORTS1='[11111]'

# ---- Case A: db host, tmpfs --------------------------------------------
render "$WORK/a.sh" db tmpfs "$SHA" "$PORTS1" '["db"]' gp3 '{ db = 9102 }'
run_case a "$WORK/a.sh"
A="$WORK/a"
check "A: setup exits 0" grep -qx 0 "$A/exit"
check "A: work dir is /mnt/whitney/work/db" grep -qx 'WORK_DIR=/mnt/whitney/work/db' "$A/env/control-11111.env"
check "A: env file names the role" grep -qx 'ROLE=db' "$A/env/control-11111.env"
U="$A/units/whitney-control@.service"
check "A: unit TMPDIR" grep -qx 'Environment=TMPDIR=/mnt/whitney/tmp' "$U"
check "A: unit User=ubuntu" grep -qx 'User=ubuntu' "$U"
check "A: unit KillMode=control-group" grep -qx 'KillMode=control-group' "$U"
check "A: unit ExecStart" grep -q '^ExecStart=/opt/whitney/bin/control_server -p %i -w ${WORK_DIR}$' "$U"
check "A: binaries extracted to /opt/whitney/bin" test -x "$A/opt/bin/control_server"
check "A: manifest kept" test -f "$A/opt/manifest.json"
check "A: tmpfs mount line" grep -q 'mount -t tmpfs -o size=75% tmpfs /mnt/whitney' "$A/stdout"
m=$(grep -n 'mount -t tmpfs' "$A/stdout" | head -1 | cut -d: -f1 || true)
c=$(grep -n 'chown -R ubuntu:ubuntu .*/mnt/whitney' "$A/stdout" | head -1 | cut -d: -f1 || true)
check "A: chown /mnt/whitney comes after mount" test -n "$m" -a -n "$c" -a "${c:-0}" -gt "${m:-0}"
check "A: sar unit written" test -f "$A/units/whitney-sar.service"
check "A: sar command" grep -q 'sar -A -o /var/lib/whitney/sar.bin 1' "$A/units/whitney-sar.service"
check "A: units started" grep -q '^DRYRUN: systemctl start whitney-sar.service whitney-control@11111.service$' "$A/stdout"
check "A: control and sar units not enabled" bash -c "! grep -qE 'systemctl enable.*(whitney-control|whitney-sar)' '$A/stdout'"
check "A: control unit has no [Install]" bash -c "! grep -q '^\[Install\]' '$U'"
check "A: control unit needs the mount" grep -qx 'RequiresMountsFor=/mnt/whitney' "$U"
check "A: ready written under /run" test -f "$A/run/ready"
check "A: no ready under /var/lib/whitney" bash -c "! test -e '$A/state/ready'"
check "A: expiry file is an epoch about 90 min ahead" bash -c "now=\$(date +%s); e=\$(cat '$A/env/expires-at'); [ \"\$e\" -ge \$((now + 80 * 60)) ] && [ \"\$e\" -le \$((now + 100 * 60)) ]"
DS="$A/units/whitney-deadman.service"
DT="$A/units/whitney-deadman.timer"
check "A: deadman service runs the check" grep -qx 'ExecStart=/usr/local/sbin/whitney-deadman' "$DS"
check "A: deadman service is oneshot" grep -qx 'Type=oneshot' "$DS"
check "A: deadman timer OnBootSec=1min" grep -qx 'OnBootSec=1min' "$DT"
check "A: deadman timer OnUnitActiveSec=2min" grep -qx 'OnUnitActiveSec=2min' "$DT"
check "A: deadman timer enabled" grep -q '^DRYRUN: systemctl enable --now whitney-deadman.timer$' "$A/stdout"
dm=$(grep -n 'systemctl enable --now whitney-deadman.timer' "$A/stdout" | head -1 | cut -d: -f1 || true)
ap=$(grep -n 'apt-get update' "$A/stdout" | head -1 | cut -d: -f1 || true)
check "A: deadman timer enabled before packages" test -n "$dm" -a -n "$ap" -a "${dm:-0}" -lt "${ap:-0}"
check "A: deadman powers off after expiry" grep -qx 'systemctl poweroff' "$A/deadman-past"
check "A: deadman does nothing before expiry" bash -c "! grep -q poweroff '$A/deadman-future'"
check "A: deadman powers off on a bad expiry file" grep -qx 'systemctl poweroff' "$A/deadman-bad"
F="$A/state/host-facts.json"
check "A: host-facts is valid JSON with required keys" jq -e '.lscpu and .uname and .meminfo and has("instance_type") and has("ami_id") and has("az") and .mount and (.tuning_applied|type=="array")' "$F"
check "A: tmpfs host facts have no data volume" jq -e 'has("data_volume") and .data_volume == null' "$F"
check "A: tuning step recorded" jq -e '.tuning_applied | map(select(.name=="cpupower_performance")) | length == 1' "$F"
check "A: packages requested" grep -q 'apt-get install.*libjemalloc2 libatomic1 libsnappy1v5 sysstat jq nvme-cli xfsprogs' "$A/stdout"
check "A: dead-man shutdown scheduled" grep -q '^DRYRUN: shutdown -h +90 ' "$A/stdout"
s=$(grep -n '^DRYRUN: shutdown -h' "$A/stdout" | head -1 | cut -d: -f1 || true)
p=$(grep -n '^DRYRUN: ' "$A/stdout" | head -1 | cut -d: -f1 || true)
check "A: shutdown is the first host change" test -n "$s" -a "${s:-0}" = "${p:-x}"
check "A: apt-get update waits for the apt lock" grep -q 'apt-get update -q -o DPkg::Lock::Timeout=300' "$A/stdout"
check "A: apt-get install waits for the apt lock" grep -q 'apt-get install -y -q -o DPkg::Lock::Timeout=300' "$A/stdout"
check "A: archive curl retries all errors" grep -q 'curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors ' "$WORK/a.sh"
V="$A/env/vector/base.toml"
VU="$A/units/whitney-vector.service"
check "A: vector installed" test -x "$A/opt/vector/bin/vector"
check "A: vector curl retries all errors" grep -q 'curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors -o "$VECTOR_TAR"' "$WORK/a.sh"
check "A: vector config written" test -s "$V"
check "A: host metrics every 1 s" bash -c "grep -qx 'type = \"host_metrics\"' '$V' && grep -qx 'collectors = \[\"cpu\", \"memory\", \"network\", \"disk\", \"load\"\]' '$V'"
check "A: no loop disks or lo in host metrics" bash -c "grep -qx 'disk.devices.excludes = \[\"loop\*\"\]' '$V' && grep -qx 'network.devices.excludes = \[\"lo\"\]' '$V'"
check "A: db scraped on its metrics port" grep -qx 'endpoints = \["http://127.0.0.1:9102/metrics"\]' "$V"
check "A: one daemon scrape source" test "$(grep -c 'type = "prometheus_scrape"' "$V")" -eq 1
check "A: scrape interval 1 s everywhere" bash -c "[ \$(grep -c '^scrape_interval_secs = 1\$' '$V') -eq 2 ]"
check "A: host metrics labelled with the host role" grep -qx "source = '.tags.role = \"db\"'" "$V"
check "A: labels: job, cluster, instance type" bash -c "grep -qx '.tags.job = \"whitney\"' '$V' && grep -qx '.tags.whitney_cluster = \"test-cluster\"' '$V' && grep -qx '.tags.instance_type = \"r8a.large\"' '$V'"
check "A: run labels come from the environment" bash -c "grep -qxF '.tags.whitney_run = \"\${WHITNEY_RUN}\"' '$V' && grep -qxF '.tags.scenario = \"\${WHITNEY_SCENARIO}\"' '$V' && grep -qxF '.tags.n = \"\${WHITNEY_N}\"' '$V' && grep -qxF '.tags.rep = \"\${WHITNEY_REP}\"' '$V'"
check "A: labels take host and daemon metrics" grep -qx 'inputs = \["role_host", "role_db"\]' "$V"
check "A: file sink takes metric_to_log of the labels" bash -c "awk '/^\[transforms.as_logs\]/{f=1} f&&/^\[sinks/{exit} f' '$V' | grep -qx 'type = \"metric_to_log\"' && awk '/^\[transforms.as_logs\]/{f=1} f&&/^\[sinks/{exit} f' '$V' | grep -qx 'inputs = \[\"labels\"\]' && awk '/^\[sinks.files\]/{f=1} f' '$V' | grep -qx 'inputs = \[\"as_logs\"\]'"
check "A: one NDJSON file per run" bash -c "grep -qxF 'path = \"/var/lib/whitney/metrics/\${WHITNEY_RUN}.ndjson\"' '$V' && grep -qx 'encoding.codec = \"json\"' '$V'"
check "A: no other \$ in the vector config" bash -c "! grep -v 'WHITNEY_RUN}\|WHITNEY_SCENARIO}\|WHITNEY_N}\|WHITNEY_REP}' '$V' | grep -q '\\$'"
check "A: no remote config by default" bash -c "! grep -rq prometheus_remote_write '$A/env' && ! test -e '$A/env/vector/remote.env'"
check "A: metrics dir exists" test -d "$A/state/metrics"
check "A: vector unit User=ubuntu" grep -qx 'User=ubuntu' "$VU"
check "A: vector unit reads run.env" grep -qx 'EnvironmentFile=/etc/whitney/vector/run.env' "$VU"
check "A: vector unit reads remote.env only if present" grep -qx 'EnvironmentFile=-/etc/whitney/vector/remote.env' "$VU"
check "A: vector unit runs the wrapper" grep -qx 'ExecStart=/usr/local/sbin/whitney-vector' "$VU"
check "A: vector unit has no [Install]" bash -c "! grep -q '^\[Install\]' '$VU'"
check "A: vector not started or enabled at setup" bash -c "! grep -qE 'systemctl (start|enable).*whitney-vector' '$A/stdout'"
check "A: vector config checked at setup" grep -qx 'DRYRUN: /usr/local/sbin/whitney-vector check' "$A/stdout"
check "A: check validates base and both auth forms" bash -c "[ \$(grep -c '^vector-args: validate --no-environment /etc/whitney/vector/base.toml' '$A/vector-check') -eq 3 ]"
check "A: check passes with a working vector" grep -qx 0 "$A/vector-check-exit"
check "A: self-test runs vector, bounded, on a temp copy of base.toml" grep -qF 'timeout 5 "$VECTOR" --graceful-shutdown-limit-secs 2 --config "$tmp/selftest.toml"' "$WORK/a.sh"
check "A: self-test cleans up its temp files" bash -c "! test -s '$A/vector-check-left'"
check "A: self-test fails when vector writes nothing" bash -c "! grep -qx 0 '$A/vector-check-silent-exit' && grep -q 'vector self-test wrote no metrics' '$A/vector-check-silent'"
check "A: setup dies with the check's reason" grep -q 'die "vector config check failed' "$WORK/a.sh"
check "A: start without remote.env: base config only" grep -qx 'vector-args: --graceful-shutdown-limit-secs 10 --config /etc/whitney/vector/base.toml' "$A/vector-none"
check "A: start without remote.env writes no remote config" bash -c "! test -e '$A/rt-none/remote.toml'"
check "A: basic auth start adds the remote config" grep -qx 'vector-args: --graceful-shutdown-limit-secs 10 --config /etc/whitney/vector/base.toml --config /tmp/rt-basic/remote.toml' "$A/vector-basic"
RB="$A/rt-basic/remote.toml"
check "A: basic auth sink" bash -c "grep -qx 'type = \"prometheus_remote_write\"' '$RB' && grep -qx 'strategy = \"basic\"' '$RB' && grep -qxF 'password = \"\${WHITNEY_REMOTE_WRITE_PASSWORD}\"' '$RB' && grep -qxF 'endpoint = \"\${WHITNEY_REMOTE_WRITE_URL}\"' '$RB'"
check "A: remote sink takes the labelled metrics" grep -qx 'inputs = \["labels"\]' "$RB"
check "A: remote sink never blocks the files" grep -qx 'buffer.when_full = "drop_newest"' "$RB"
check "A: remote sink gives up after 3 retries" grep -qx 'request.retry_attempts = 3' "$RB"
check "A: vector stops within 10 s" grep -q -- '--graceful-shutdown-limit-secs 10 ' "$A/vector-bearer"
check "A: bearer auth sink" bash -c "grep -qx 'strategy = \"bearer\"' '$A/rt-bearer/remote.toml' && grep -qxF 'token = \"\${WHITNEY_REMOTE_WRITE_BEARER_TOKEN}\"' '$A/rt-bearer/remote.toml'"
check "A: no credential in any remote config" bash -c "! grep -rqE 'pw-secret|tok-secret|rw.example' '$A/rt-basic' '$A/rt-bearer'"
check "A: host facts record vector" jq -e '.vector.version == "0.43.0" and (.vector.version_output | startswith("vector 0.43.0 ")) and .vector.remote_write == false' "$F"

# ---- Case B: wrong sha ---------------------------------------------------
BAD="$(printf '0%.0s' $(seq 64))"
render "$WORK/b.sh" db tmpfs "$BAD" "$PORTS1" '["db"]'
run_case b "$WORK/b.sh"
B="$WORK/b"
check "B: wrong sha exits non-zero" bash -c "! grep -qx 0 '$B/exit'"
check "B: no control or sar unit written" bash -c "! ls '$B/units/' 2>/dev/null | grep -qE 'whitney-(control|sar)'"
check "B: nothing extracted" bash -c "! test -e '$B/opt/bin'"
check "B: no vector installed" bash -c "! test -e '$B/opt/vector'"
check "B: no ready file" bash -c "! test -e '$B/run/ready' && ! test -e '$B/state/ready'"
check "B: exit file is non-zero" bash -c "test -s '$B/exit' && ! grep -qx 0 '$B/exit'"
check "B: output names the sha mismatch" grep -q "archive sha256 mismatch" "$B/stdout"
check "B: failed marker names the sha mismatch" grep -q "archive sha256 mismatch" "$B/run/failed"
check "B: shutdown scheduled even though setup failed" grep -q '^DRYRUN: shutdown -h +90 ' "$B/stdout"
check "B: deadman timer enabled even though setup failed" grep -q '^DRYRUN: systemctl enable --now whitney-deadman.timer$' "$B/stdout"
check "B: expiry file written even though setup failed" test -s "$B/env/expires-at"

# ---- Case C: single host, four roles ------------------------------------
render "$WORK/c.sh" single tmpfs "$SHA" '[11111, 11112, 11113, 11114]' '["event", "db", "exec0", "exec1"]' gp3 \
  '{ event = 9101, db = 9102, exec0 = 9110, exec1 = 9111 }'
run_case c "$WORK/c.sh"
C="$WORK/c"
check "C: setup exits 0" grep -qx 0 "$C/exit"
for pr in 11111:event 11112:db 11113:exec0 11114:exec1; do
  check "C: port ${pr%%:*} -> ${pr##*:}" grep -qx "WORK_DIR=/mnt/whitney/work/${pr##*:}" "$C/env/control-${pr%%:*}.env"
done
CV="$C/env/vector/base.toml"
for rp in event:9101 db:9102 exec0:9110 exec1:9111; do
  check "C: ${rp%%:*} scraped on ${rp##*:}" bash -c "grep -qx '\[sources.daemon_${rp%%:*}\]' '$CV' && grep -qx 'endpoints = \[\"http://127.0.0.1:${rp##*:}/metrics\"\]' '$CV'"
done
check "C: host metrics labelled single" grep -qx "source = '.tags.role = \"single\"'" "$CV"
check "C: labels take every role" grep -qx 'inputs = \["role_host", "role_db", "role_event", "role_exec0", "role_exec1"\]' "$CV"
check "C: all four units started" grep -q '^DRYRUN: systemctl start whitney-sar.service whitney-control@11111.service whitney-control@11112.service whitney-control@11113.service whitney-control@11114.service$' "$C/stdout"

# ---- Case D: nvme ---------------------------------------------------------
render "$WORK/d.sh" db nvme "$SHA" "$PORTS1" '["db"]'
run_case d "$WORK/d.sh"
D="$WORK/d"
check "D: nvme db exits 0" grep -qx 0 "$D/exit"
check "D: nvme db runs mkfs.xfs -f" grep -q 'mkfs.xfs -f' "$D/stdout"
check "D: nvme db mounts with noatime" grep -q 'mount -o noatime .* /mnt/whitney' "$D/stdout"
render "$WORK/e.sh" exec0 nvme "$SHA" "$PORTS1" '["exec0"]'
run_case e "$WORK/e.sh"
E="$WORK/e"
check "E: nvme exec exits 0" grep -qx 0 "$E/exit"
check "E: nvme exec has no mkfs or mount" bash -c "! grep -qE 'mkfs|mount ' '$E/stdout'"
check "E: nvme exec work dir" grep -qx 'WORK_DIR=/mnt/whitney/work/exec0' "$E/env/control-11111.env"

# ---- Cases F-I: ebs -----------------------------------------------------------
# lsblk -J -b -o NAME,PATH,MODEL,SIZE,TYPE,MOUNTPOINTS as on an r8a host: the
# root EBS volume (partitioned, mounted), loop devices, and the data volume.
ebs_lsblk() { # ebs_lsblk <out> <extra disk objects...>
  local out="$1"; shift
  local extra=""
  for d in "$@"; do extra="$extra, $d"; done
  cat >"$out" <<JSON
{"blockdevices": [
  {"name": "loop0", "path": "/dev/loop0", "model": null, "size": 50331648, "type": "loop", "mountpoints": ["/snap/core/1"]},
  {"name": "nvme0n1", "path": "/dev/nvme0n1", "model": "Amazon Elastic Block Store              ", "size": 107374182400, "type": "disk", "mountpoints": [null],
   "children": [
     {"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "model": null, "size": 106300440064, "type": "part", "mountpoints": ["/"]},
     {"name": "nvme0n1p15", "path": "/dev/nvme0n1p15", "model": null, "size": 111149056, "type": "part", "mountpoints": ["/boot/efi"]}
   ]}$extra
]}
JSON
}
DATA='{"name": "nvme1n1", "path": "/dev/nvme1n1", "model": "Amazon Elastic Block Store              ", "size": 214748364800, "type": "disk", "mountpoints": [null]}'
DATA2='{"name": "nvme2n1", "path": "/dev/nvme2n1", "model": "Amazon Elastic Block Store              ", "size": 214748364800, "type": "disk", "mountpoints": [null]}'
MOUNTED='{"name": "nvme3n1", "path": "/dev/nvme3n1", "model": "Amazon Elastic Block Store              ", "size": 8589934592, "type": "disk", "mountpoints": ["/srv"]}'
STORE='{"name": "nvme4n1", "path": "/dev/nvme4n1", "model": "Amazon EC2 NVMe Instance Storage        ", "size": 3800000000000, "type": "disk", "mountpoints": [null]}'
WHOLE_ROOT='{"name": "nvme0n1", "path": "/dev/nvme0n1", "model": "Amazon Elastic Block Store", "size": 8589934592, "type": "disk", "mountpoints": ["/"]}'

ebs_lsblk "$WORK/lsblk-one.json" "$DATA" "$MOUNTED" "$STORE"
render "$WORK/f.sh" db ebs "$SHA" "$PORTS1" '["db"]' io2
run_case f "$WORK/f.sh" "$WORK/lsblk-one.json"
F="$WORK/f"
check "F: ebs db exits 0" grep -qx 0 "$F/exit"
check "F: ebs db formats the data volume" grep -qx 'DRYRUN: mkfs.xfs -f /dev/nvme1n1' "$F/stdout"
check "F: ebs db mounts it with noatime" grep -qx 'DRYRUN: mount -o noatime /dev/nvme1n1 /mnt/whitney' "$F/stdout"
check "F: ebs db touches no other disk" bash -c "! grep -qE 'nvme[0234]n1' '$F/stdout'"
check "F: ebs db has no tmpfs mount" bash -c "! grep -q 'mount -t tmpfs' '$F/stdout'"
check "F: ebs db work dir" grep -qx 'WORK_DIR=/mnt/whitney/work/db' "$F/env/control-11111.env"
check "F: host facts record the data volume" jq -e '.storage == "ebs" and .data_volume == {device: "/dev/nvme1n1", size_bytes: 214748364800, volume_type: "io2"}' "$F/state/host-facts.json"

ebs_lsblk "$WORK/lsblk-two.json" "$DATA" "$DATA2"
render "$WORK/g.sh" db ebs "$SHA" "$PORTS1" '["db"]'
run_case g "$WORK/g.sh" "$WORK/lsblk-two.json"
G="$WORK/g"
check "G: two candidates exit non-zero" bash -c "test -s '$G/exit' && ! grep -qx 0 '$G/exit'"
check "G: failed marker names both disks" grep -q 'more than one EBS data volume candidate: /dev/nvme1n1 /dev/nvme2n1' "$G/run/failed"
check "G: nothing formatted" bash -c "! grep -q mkfs '$G/stdout'"
check "G: no ready file" bash -c "! test -e '$G/run/ready'"

ebs_lsblk "$WORK/lsblk-none.json" "$MOUNTED" "$STORE"
render "$WORK/h.sh" event ebs "$SHA" "$PORTS1" '["event"]'
run_case h "$WORK/h.sh" "$WORK/lsblk-none.json"
H="$WORK/h"
check "H: no candidate exits non-zero" bash -c "test -s '$H/exit' && ! grep -qx 0 '$H/exit'"
check "H: failed marker says no data volume" grep -q 'no EBS data volume found' "$H/run/failed"
check "H: nothing formatted" bash -c "! grep -q mkfs '$H/stdout'"

printf '{"blockdevices": [%s, %s]}\n' "$WHOLE_ROOT" "$DATA" >"$WORK/lsblk-whole.json"
render "$WORK/j.sh" indexer ebs "$SHA" "$PORTS1" '["indexer"]'
run_case j "$WORK/j.sh" "$WORK/lsblk-whole.json"
J="$WORK/j"
check "J: unpartitioned root is skipped (mounted at /)" grep -qx 'DRYRUN: mkfs.xfs -f /dev/nvme1n1' "$J/stdout"

render "$WORK/i.sh" exec0 ebs "$SHA" "$PORTS1" '["exec0"]'
run_case i "$WORK/i.sh" "$WORK/lsblk-one.json"
I="$WORK/i"
check "I: ebs exec exits 0" grep -qx 0 "$I/exit"
check "I: ebs exec has no mkfs or mount" bash -c "! grep -qE 'mkfs|mount ' '$I/stdout'"
check "I: ebs exec work dir" grep -qx 'WORK_DIR=/mnt/whitney/work/exec0' "$I/env/control-11111.env"
check "I: ebs exec host facts have no data volume" jq -e '.data_volume == null' "$I/state/host-facts.json"

# ---- Case R: runner host, no daemon ------------------------------------------
render "$WORK/r.sh" runner tmpfs "$SHA" "$PORTS1" '["runner"]'
run_case r "$WORK/r.sh"
R="$WORK/r"
check "R: runner setup exits 0" grep -qx 0 "$R/exit"
check "R: runner host metrics only" bash -c "grep -q 'type = \"host_metrics\"' '$R/env/vector/base.toml' && ! grep -q prometheus_scrape '$R/env/vector/base.toml'"
check "R: labels take host metrics only" grep -qx 'inputs = \["role_host"\]' "$R/env/vector/base.toml"

# ---- Case V: wrong vector sha -------------------------------------------------
render "$WORK/v.sh" db tmpfs "$SHA" "$PORTS1" '["db"]' gp3 '{ db = 9102 }' "$BAD"
run_case v "$WORK/v.sh"
VV="$WORK/v"
check "V: wrong vector sha exits non-zero" bash -c "test -s '$VV/exit' && ! grep -qx 0 '$VV/exit'"
check "V: failed marker names the vector sha mismatch" grep -q "vector sha256 mismatch" "$VV/run/failed"
check "V: no vector installed" bash -c "! test -e '$VV/opt/vector'"
check "V: no unit or vector config written" bash -c "! ls '$VV/units/' 2>/dev/null | grep -qE 'whitney-(control|sar|vector)' && ! test -e '$VV/env/vector'"
check "V: no ready file" bash -c "! test -e '$VV/run/ready'"

# ---- Case M: metrics port for a role not on the host -----------------------------
render "$WORK/m.sh" db tmpfs "$SHA" "$PORTS1" '["db"]' gp3 '{ event = 9101 }'
run_case m "$WORK/m.sh"
check "M: bad metrics entry fails setup" grep -q "bad metrics port entry: event=9101" "$WORK/m/run/failed"

# ---- shellcheck -----------------------------------------------------------
check "shellcheck passes (tmpfs)" shellcheck -s bash "$WORK/a.sh"
check "shellcheck passes (ebs)" shellcheck -s bash "$WORK/f.sh"
check "shellcheck passes (single host)" shellcheck -s bash "$WORK/c.sh"
check "shellcheck passes (vector wrapper)" shellcheck -s sh "$A/whitney-vector"

echo
if [ "$fails" -ne 0 ]; then echo "$fails check(s) failed"; exit 1; fi
echo "all checks passed"
