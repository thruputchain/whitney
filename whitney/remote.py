"""SSH and scp to cluster hosts (user ubuntu), readiness, and unit restarts."""
from __future__ import annotations

import os
import secrets
import shlex
import tempfile
import time
from pathlib import Path
from subprocess import CompletedProcess
from typing import TYPE_CHECKING

from whitney.cpus import l3_groups
from whitney.metrics import REMOTE_ENV, RUN_ENV, VECTOR_UNIT, RemoteWrite
from whitney.proc import Exec, tail

if TYPE_CHECKING:
    from whitney.infra import Cluster

USER = "ubuntu"
STATE = "/var/lib/whitney"
# Setup markers (ready, failed) live on /run, a tmpfs: after a reboot the
# mount and control units are gone, and so is the ready marker.
READY_DIR = "/run/whitney"

_READY_CHECK = (
    f"if test -f {READY_DIR}/ready; then echo ready; "
    f"elif test -f {READY_DIR}/failed; then echo failed; cat {READY_DIR}/failed; fi"
)

# The control_server ports of a host, one env file each, in $ports.
_FIND_PORTS = r"""ports=""
for f in /etc/whitney/control-*.env; do
  [ -e "$f" ] || continue
  p=${f##*/control-}
  ports="$ports ${p%.env}"
done
"""

# Runs on every host before each run. A host without the ready marker (for
# example after a reboot) is refused. Vector stops (a run that was cut short
# may have left it running); start_metrics starts it for the hosts in use.
# sar restarts with an empty file, so sar.bin covers one run. On hosts with control_server units (one env file
# per port), old daemon logs are removed, the units restart (KillMode=
# control-group stops leftover daemons), and every port must listen again.
_RESTART = r"""set -eu
test -f /run/whitney/ready || { echo "host is not ready (rebooted?): no /run/whitney/ready"; exit 1; }
sudo systemctl stop --no-block whitney-vector.service
sudo systemctl stop whitney-sar.service
rm -f /var/lib/whitney/sar.bin
""" + _FIND_PORTS + r"""if [ -n "$ports" ]; then
  rm -rf /mnt/whitney/tmp/*
  for p in $ports; do
    sudo systemctl restart "whitney-control@$p.service"
  done
  i=0
  while :; do
    down=""
    for p in $ports; do
      ss -Hltn "sport = :$p" | grep -q . || down="$down $p"
    done
    [ -z "$down" ] && break
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
      echo "control ports not listening after 60 s:$down"
      exit 1
    fi
    sleep 1
  done
fi
sudo systemctl start whitney-sar.service
"""

# Per-run executor setup, on each exec host in use, before the restart.
# hog-exec runs in the cgroup of its control_server unit and sizes its thread
# pool from the CPUs it may use. A runtime AllowedCPUs (cgroup cpuset) limits
# both; an empty value removes the limit. control_server starts hog-exec with
# execvp, so an LD_PRELOAD in the unit's env file reaches hog-exec too. The
# restart after this applies both to the new daemons.
#
# An exec host boots with one control unit (CONTROL_PORT). With one hog-exec
# per L3 (exec_per_l3), hog-exec i >= 1 gets its own unit on CONTROL_PORT + i:
# its env file is a copy of the base one with ROLE <role>-<i> and WORK_DIR
# WORK_ROOT/<role>-<i>. Units above the run's last port are stopped and lose
# their env file and work dir, so the restart (which restarts every env
# file's port) starts only the units of this run.
CONTROL_PORT = 11111
WORK_ROOT = "/mnt/whitney/work"
JEMALLOC = "/usr/lib/x86_64-linux-gnu/libjemalloc.so.2"  # Ubuntu libjemalloc2


def _exec_setup(cpus: list[str], allocator: str, min_cpus: int | None = None) -> str:
    """Run hog-exec i of the host in control unit CONTROL_PORT + i with
    AllowedCPUs=cpus[i] ("" removes the limit), and preload jemalloc
    ("jemalloc") or not ("system") in every unit. min_cpus: fail unless the
    host has at least that many CPUs."""
    checks = preload = ""
    if min_cpus is not None:
        checks += (f'n=$(nproc --all)\n[ "$n" -ge {min_cpus} ] || '
                   f'{{ echo "host has $n CPUs, fewer than {min_cpus} exec cores"; exit 1; }}\n')
    if allocator == "jemalloc":
        checks += f'test -f {JEMALLOC} || {{ echo "no {JEMALLOC} (libjemalloc2)"; exit 1; }}\n'
        preload = f'  echo "LD_PRELOAD={JEMALLOC}" | sudo tee -a "$f" >/dev/null\n'
    last = CONTROL_PORT + len(cpus) - 1
    extra = "".join(
        f'd="{WORK_ROOT}/$role-{i}"\n'
        f'sudo install -d -o {USER} -g {USER} "$d"\n'
        f'sudo sed -e "s|^ROLE=.*|ROLE=$role-{i}|" -e "s|^WORK_DIR=.*|WORK_DIR=$d|" "$base" |\n'
        f'  sudo tee /etc/whitney/control-{CONTROL_PORT + i}.env >/dev/null\n'
        for i in range(1, len(cpus)))
    allowed = "".join(
        f'sudo systemctl set-property --runtime "whitney-control@{CONTROL_PORT + i}.service" '
        f"AllowedCPUs={c}\n" for i, c in enumerate(cpus))
    return f"""set -eu
{checks}base=/etc/whitney/control-{CONTROL_PORT}.env
test -f "$base" || {{ echo "no $base"; exit 1; }}
role=$(sudo sed -n 's/^ROLE=//p' "$base")
[ -n "$role" ] || {{ echo "no ROLE in $base"; exit 1; }}
{_FIND_PORTS}for p in $ports; do
  [ "$p" -gt {last} ] || continue
  sudo systemctl stop "whitney-control@$p.service"
  sudo rm -f "/etc/whitney/control-$p.env"
  sudo rm -rf "{WORK_ROOT}/$role-$((p - {CONTROL_PORT}))"
done
{extra}{_FIND_PORTS}for p in $ports; do
  f="/etc/whitney/control-$p.env"
  sudo sed -i '/^LD_PRELOAD=/d' "$f"
{preload}done
{allowed}"""


# The distinct L3 CPU lists of a host, one per line: shared_cpu_list of the
# level 3 cache of each online CPU (cpu0 has no online file: always online).
_L3_TOPOLOGY = r"""set -eu
for d in /sys/devices/system/cpu/cpu[0-9]*; do
  if [ -f "$d/online" ] && [ "$(cat "$d/online")" != 1 ]; then continue; fi
  c="$d/cache/index3"
  if [ -f "$c/shared_cpu_list" ] && [ "$(cat "$c/level" 2>/dev/null)" = 3 ]; then
    cat "$c/shared_cpu_list"
  fi
done | sort -u
"""


# One line per control unit, after the restart: the port, its effective
# cpuset ("-" when the unit's cgroup has no cpuset files: no limit), and the
# libjemalloc lines in its control_server's maps ("-" when it has no process).
# systemd puts template instances in their own slice
# (system.slice/system-whitney\x2dcontrol.slice), so the cgroup path comes
# from the unit's ControlGroup property.
_EXEC_STATE = "set -eu\n" + _FIND_PORTS + f"""for p in $ports; do
  cg=$(systemctl show -p ControlGroup --value "whitney-control@$p.service")
  f="/sys/fs/cgroup$cg/cpuset.cpus.effective"
  if [ -f "$f" ]; then cpus=$(cat "$f"); else cpus=-; fi
  pid=$(systemctl show -p MainPID --value "whitney-control@$p.service")
  maps=$(grep -c libjemalloc "/proc/$pid/maps" 2>/dev/null || true)
  echo "$p $cpus ${{maps:--}}"
done
"""


def _opts(c: Cluster) -> list[str]:
    argv = ["-o", "StrictHostKeyChecking=accept-new",
            # Per cluster: EC2 reuses public IPs, so a global known_hosts would
            # hold stale keys.
            "-o", f"UserKnownHostsFile={c.workdir / 'known_hosts'}",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            # A dropped link ends the call after about 2 min instead of
            # blocking past the runner timeout (and the destroy after it).
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]
    if c.ssh_identity:
        argv += ["-i", str(c.ssh_identity)]
    return argv


def ssh(c: Cluster, ip: str, command: str, ex: Exec, *, check: bool = True) -> CompletedProcess[str]:
    return ex(["ssh", *_opts(c), f"{USER}@{ip}", command], check=check)


def scp_from(c: Cluster, ip: str, remote: str, local: Path, ex: Exec, *,
             recursive: bool = False) -> None:
    ex(["scp", *_opts(c), *(["-r"] if recursive else []), f"{USER}@{ip}:{remote}", str(local)])


def scp_to(c: Cluster, local: Path, ip: str, remote: str, ex: Exec) -> None:
    ex(["scp", *_opts(c), str(local), f"{USER}@{ip}:{remote}"])


def wait_ready(c: Cluster, ex: Exec, *, timeout: int = 1800, interval: int = 10) -> None:
    """Poll every host for the ready marker. A failed marker fails at once."""
    pending = c.hosts()
    deadline = time.monotonic() + timeout
    while True:
        for name, ip in list(pending.items()):
            cp = ssh(c, ip, _READY_CHECK, ex, check=False)
            if cp.returncode != 0:
                continue  # still booting, or sshd not up yet
            lines = cp.stdout.splitlines()
            if lines and lines[0] == "ready":
                del pending[name]
            elif lines and lines[0] == "failed":
                reason = " ".join(lines[1:]).strip() or "no reason given"
                raise RuntimeError(f"host {name} ({ip}) setup failed: {reason}")
        if not pending:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"hosts not ready after {timeout} s: {', '.join(pending)}")
        time.sleep(interval)


def restart_controls(c: Cluster, ex: Exec) -> None:
    """Restart control_server units and sar on every host, then wait for the ports."""
    for name, ip in c.hosts().items():
        cp = ssh(c, ip, _RESTART, ex, check=False)
        if cp.returncode != 0:
            raise RuntimeError(
                f"restart on {name} ({ip}) exited {cp.returncode}: {tail(cp.stdout + cp.stderr)}")


def setup_execs(c: Cluster, hosts: dict[str, str], cpus: dict[str, list[str]], allocator: str,
                ex: Exec, min_cpus: int | None = None) -> None:
    """On exec hosts (name -> public IP): run hog-exec i of each host in
    control unit CONTROL_PORT + i, limited to cpus[name][i] ("" for no
    limit), and set its allocator (see _exec_setup). Takes effect when the
    control units restart."""
    for name, ip in hosts.items():
        _on_each(c, {name: ip}, "exec setup", _exec_setup(cpus[name], allocator, min_cpus), ex)


def l3_topology(c: Cluster, hosts: dict[str, str], ex: Exec) -> dict[str, list[list[int]]]:
    """Each host's L3 groups (cpus.l3_groups): name -> CPU lists sorted by
    their lowest CPU."""
    out: dict[str, list[list[int]]] = {}
    for name, ip in hosts.items():
        cp = ssh(c, ip, _L3_TOPOLOGY, ex, check=False)
        if cp.returncode != 0:
            raise RuntimeError(f"L3 topology on {name} ({ip}) exited {cp.returncode}: "
                               f"{tail(cp.stdout + cp.stderr)}")
        try:
            out[name] = l3_groups(cp.stdout.splitlines())
        except ValueError as e:
            raise RuntimeError(f"L3 topology on {name} ({ip}): {e}") from None
    return out


def exec_state(c: Cluster, hosts: dict[str, str], ex: Exec) -> dict[str, dict[str, dict]]:
    """Each host's control units: name -> {port: {"cpus": effective cpuset or
    None, "allocator": "jemalloc", "system", or None (no process)}}."""
    out: dict[str, dict[str, dict]] = {}
    for name, ip in hosts.items():
        cp = ssh(c, ip, _EXEC_STATE, ex, check=False)
        if cp.returncode != 0:
            raise RuntimeError(f"exec state on {name} ({ip}) exited {cp.returncode}: "
                               f"{tail(cp.stdout + cp.stderr)}")
        ports: dict[str, dict] = {}
        for line in cp.stdout.splitlines():
            parts = line.split()
            if len(parts) != 3:
                raise RuntimeError(f"exec state on {name} ({ip}): bad line {line!r}")
            port, cpus, maps = parts
            ports[port] = {"cpus": None if cpus == "-" else cpus,
                           "allocator": None if maps == "-" else
                           "jemalloc" if int(maps) > 0 else "system"}
        out[name] = ports
    return out


HOST_FACTS = f"{STATE}/host-facts.json"
# How long Vector gets after the restart on every host before the check that
# it stayed up. A bad config makes it exit within this time.
VECTOR_SETTLE_SECONDS = 1.0


def _metrics_start(run_env: str, remote: bool) -> str:
    """Write run.env, drop or keep remote.env, and restart Vector."""
    keep_or_drop = (f'test -f {REMOTE_ENV} || {{ echo "no {REMOTE_ENV}"; exit 1; }}' if remote
                    else f"sudo rm -f {REMOTE_ENV}")
    return f"""set -eu
printf '%s' {shlex.quote(run_env)} | sudo tee {RUN_ENV}.tmp >/dev/null
sudo mv {RUN_ENV}.tmp {RUN_ENV}
{keep_or_drop}
sudo systemctl restart {VECTOR_UNIT}
"""


def _metrics_check(remote: bool) -> str:
    """Check that Vector stayed up, and record in host-facts.json whether
    remote export is on."""
    return f"""set -eu
if ! systemctl is-active --quiet {VECTOR_UNIT}; then
  echo "whitney-vector is not active: see journalctl -u whitney-vector on the host"
  exit 1
fi
f={HOST_FACTS}
jq --argjson rw {"true" if remote else "false"} '.vector.remote_write = $rw' "$f" >"$f.tmp"
mv "$f.tmp" "$f"
"""


def _push_remote_env(c: Cluster, name: str, ip: str, local: Path, upload: str,
                     ex: Exec) -> None:
    """Copy remote.env to the host as root-owned 0600. The credentials travel
    only inside the file (scp, to upload in the home dir), never on a command
    line."""
    scp_to(c, local, ip, upload, ex)
    cp = ssh(c, ip, f"rc=0; sudo install -m 0600 -o root -g root {upload} "
                    f"{REMOTE_ENV} || rc=$?; rm -f {upload}; exit $rc", ex, check=False)
    if cp.returncode != 0:
        raise RuntimeError(f"remote.env on {name} ({ip}) exited {cp.returncode}: "
                           f"{tail(cp.stdout + cp.stderr)}")


def _on_each(c: Cluster, hosts: dict[str, str], what: str, script: str, ex: Exec) -> None:
    for name, ip in hosts.items():
        cp = ssh(c, ip, script, ex, check=False)
        if cp.returncode != 0:
            raise RuntimeError(f"{what} on {name} ({ip}) exited {cp.returncode}: "
                               f"{tail(cp.stdout + cp.stderr)}")


def start_metrics(c: Cluster, hosts: dict[str, str], run_env: str,
                  remote_write: RemoteWrite | None, ex: Exec) -> None:
    """Start Vector for one run on hosts (name -> public IP), with the run's
    labels, and with remote.env pushed first when remote_write is set.
    Vector restarts on every host, then one settle wait, then each host is
    checked."""
    remote = remote_write is not None
    with tempfile.TemporaryDirectory(prefix="whitney-") as tmp:
        local = Path(tmp) / "remote.env"
        if remote_write is not None:
            fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(remote_write.env_file())
            upload = f".whitney-remote-{secrets.token_hex(8)}.env"
            for name, ip in hosts.items():
                _push_remote_env(c, name, ip, local, upload, ex)
    _on_each(c, hosts, "metrics start", _metrics_start(run_env, remote), ex)
    time.sleep(VECTOR_SETTLE_SECONDS)
    _on_each(c, hosts, "metrics check", _metrics_check(remote), ex)
