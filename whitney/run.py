"""One configuration end to end: build, apply, run, collect, destroy."""
from __future__ import annotations

import ipaddress
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from whitney.awscreds import prepare_aws
from whitney.build import Archive, ensure_build, load_archive, upload_archive
from whitney.config import SAFE_NAME, Scenario, ScenarioError, check_block_size
from whitney.cpus import cpuset_size, exec_layout, format_cpuset, parse_cpuset
from whitney.infra import (INSTANCE_CREATE_TIMEOUT, Cluster, DestroyError, apply_cluster,
                           destroy_cluster, destroy_command, new_tofu_workdir,
                           require_placement)
from whitney.metrics import (METRICS_DIR, SCRAPE_INTERVAL_SECS, VECTOR_UNIT, VECTOR_VERSION,
                             RemoteWrite, metrics_port, run_env)
from whitney.proc import CommandError, Exec, partial_output, redact, tail
from whitney.remote import (CONTROL_PORT, STATE, WORK_ROOT, exec_state, l3_topology,
                            restart_controls, scp_from, scp_to, setup_execs, ssh, start_metrics)

BIN_DIR = "/opt/whitney/bin"
REMOTE_ROLES = f"{STATE}/roles.json"
REMOTE_TRIALS = f"{STATE}/trials.jsonl"
RUNNER_TIMEOUT = 7200
RUN_MAX_HOURS = 8  # host dead-man switch, per cluster
SWEEP_MAX_HOURS = 24
INTERRUPT_COLLECT_SECONDS = 60  # best-effort collect after Ctrl-C or a signal


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    status: Literal["ok", "failed"]
    error: str | None


@dataclass(frozen=True)
class RunOptions:
    bucket: str
    ssh_key_name: str
    operator_cidr: str
    ssh_identity: Path | None = None
    arch: str = "znver5"
    archive: Path | None = None  # prebuilt archive: skip the build
    keep: bool = False
    runner_timeout: int = RUNNER_TIMEOUT
    max_lifetime_minutes: int = RUN_MAX_HOURS * 60  # hosts power off after this
    results_root: Path = Path("results")
    cache: Path = Path(".whitney-cache")
    aws_creds: str = "process"  # tofu credentials: see whitney.awscreds
    instance_create_timeout: str = INSTANCE_CREATE_TIMEOUT
    apply_timeout: str | None = None  # None: instance_create_timeout + 4m
    remote_write: RemoteWrite | None = None  # --metrics-remote-write


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    try:
        print(f"whitney: {msg}", file=sys.stderr, flush=True)
    except OSError:  # the terminal may be gone (SIGHUP); never block cleanup
        pass


# ---- run dir -------------------------------------------------------------------


def new_run_dir(results_root: Path, s: Scenario, n: int, rep: int,
                now: datetime | None = None, cores: int | None = None) -> Path:
    """Create results/<YYYYMMDD-HHMM>-<scenario>-<hogd_ref>-n<N>-r<rep>/ (UTC),
    with -c<K> after N when the scenario has exec_cores."""
    for what, value in (("scenario name", s.name), ("hogd_ref", s.hogd_ref)):
        if not SAFE_NAME.fullmatch(value):
            raise ValueError(f"{what} must match {SAFE_NAME.pattern} for the run dir: {value!r}")
    if bool(s.exec_cores) != (cores is not None):
        raise ValueError(f"run cores {cores} do not fit exec_cores {s.exec_cores}")
    now = now or datetime.now(timezone.utc)
    c = f"-c{cores}" if cores is not None else ""
    d = results_root / f"{now:%Y%m%d-%H%M}-{s.name}-{s.hogd_ref}-n{n}{c}-r{rep}"
    results_root.mkdir(parents=True, exist_ok=True)
    d.mkdir()  # FileExistsError: same run started in the same minute
    return d


def write_run_inputs(run_dir: Path, s: Scenario, a: Archive, command: list[str],
                     aws: dict | None = None) -> None:
    """Resolved scenario, manifest, archive hash, whitney command line, and the
    AWS credential mode and caller (aws.json)."""
    (run_dir / "scenario.resolved.toml").write_text(s.to_toml())
    (run_dir / "manifest.json").write_text(json.dumps(a.manifest, indent=2) + "\n")
    (run_dir / "archive.sha256").write_text(a.sha256 + "\n")
    _add_command(run_dir, "whitney", shlex.join(command))
    if aws is not None:
        (run_dir / "aws.json").write_text(json.dumps(aws, indent=2) + "\n")


def _add_command(run_dir: Path, label: str, line: str) -> None:
    with open(run_dir / "command.txt", "a") as f:
        f.write(f"{label}: {redact(line)}\n")


def write_text_atomic(path: Path, text: str) -> None:
    """Write path through a temp file in the same dir and os.replace, so an
    interrupt never leaves a half-written file."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_status(run_dir: Path, status: str, error: str | None, started_at: str,
                 finished_at: str) -> None:
    doc = {"status": status, "error": redact(error) if error else None,
           "started_at": started_at, "finished_at": finished_at}
    write_text_atomic(run_dir / "status.json", json.dumps(doc, indent=2) + "\n")


def add_cleanup(run_dir: Path, notes: list[str]) -> None:
    """Add tag sweep notes (orphaned instances) to status.json as "cleanup"."""
    if not notes:
        return
    try:
        doc = json.loads((run_dir / "status.json").read_text())
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        doc = {"status": "failed", "error": "no status recorded", "started_at": None,
               "finished_at": None}
    doc["cleanup"] = [*doc.get("cleanup", []), *(redact(n) for n in notes)]
    write_text_atomic(run_dir / "status.json", json.dumps(doc, indent=2) + "\n")


def read_status(run_dir: Path) -> str | None:
    """The status in run_dir/status.json ("ok" or "failed"), or None if the
    file is missing, unreadable, or has no valid status."""
    try:
        doc = json.loads((run_dir / "status.json").read_text())
    except (OSError, ValueError):
        return None
    status = doc.get("status") if isinstance(doc, dict) else None
    return status if status in ("ok", "failed") else None


# ---- roles.json ------------------------------------------------------------------


def _ip(role: str, value: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ValueError(f"roles: {role} host must be an IP literal, got {value!r}") from None
    return value


def _role(name: str, view: dict, control_port: int = CONTROL_PORT) -> dict:
    return {"host": _ip(name, view["private_ip"]), "control_port": control_port,
            "metrics_port": metrics_port(name, single_host=False),
            "work_dir": f"{WORK_ROOT}/{name}", "cpu_affinity": ""}


def _exec_roles(h: int, view: dict, procs: int) -> list[dict]:
    """The hog-execs of exec host h: exec<h> on CONTROL_PORT, then
    exec<h>-<i> on CONTROL_PORT + i (one hog-exec per L3). The unit's cgroup
    limits each one's CPUs, so cpu_affinity stays empty."""
    return [_role(f"exec{h}" if i == 0 else f"exec{h}-{i}", view, CONTROL_PORT + i)
            for i in range(procs)]


def roles_json(c: Cluster, n: int, s: Scenario, procs: int = 1) -> str:
    """The runner's roles.json for the first n execs of the cluster, with
    procs hog-execs on each (multi-host only)."""
    if procs < 1 or (procs > 1 and s.single_host):
        raise ValueError(f"procs={procs} hog-execs per exec host does not fit the scenario")
    if s.single_host:
        from whitney.sweep import single_host_roles  # sweep imports this module
        if not 1 <= n <= len(c.roles["execs"]):
            raise ValueError(f"n={n} but the cluster has {len(c.roles['execs'])} execs")
        ip = _ip("single", c.roles["runner"]["private_ip"])
        return json.dumps(single_host_roles(s, ip, n), indent=2) + "\n"
    execs = c.roles["execs"]
    if not 1 <= n <= len(execs):
        raise ValueError(f"n={n} but the cluster has {len(execs)} execs")
    if s.indexer and not c.roles.get("indexer"):
        raise ValueError("scenario has indexer = true but the cluster has no indexer host")
    doc = {
        "bin_dir": BIN_DIR,
        "runner_bind_ip": _ip("runner", c.roles["runner"]["private_ip"]),
        "event": _role("event", c.roles["event"]),
        "db": _role("db", c.roles["db"]),
        "indexer": _role("indexer", c.roles["indexer"]) if s.indexer else None,
        "execs": [r for h, e in enumerate(execs[:n]) for r in _exec_roles(h, e, procs)],
    }
    return json.dumps(doc, indent=2) + "\n"


# ---- one run on a live cluster ------------------------------------------------------


def runner_command(s: Scenario, n: int, timeout: int, cores: int | None = None) -> str:
    args = [
        f"{BIN_DIR}/whitney-runner", "--roles", REMOTE_ROLES,
        "--accounts", str(s.accounts),
        "--experiment", s.experiment,
        "--txs-per-block", str(s.block_txs(n, cores)),
        "--batch-sizes", ",".join(str(b) for b in s.batch_sizes),
        "--warmup-blocks", str(s.warmup_blocks),
        "--timed-blocks", str(s.timed_blocks),
        "--connections-per-exec", str(s.connections_per_exec),
        "--coros-per-connection", str(s.coros_per_connection),
        "--uniquifier-layout", s.uniquifier_layout,
        "--json-out", REMOTE_TRIALS,
    ]
    from whitney.sweep import runner_cpus  # sweep imports this module
    cpus = runner_cpus(s)
    if cpus:  # single host: pin the runner away from the daemons
        args = ["taskset", "-c", cpus, *args]
    # timeout: the runner waits forever if a control_server is down.
    return f"rm -f {REMOTE_TRIALS} && timeout {timeout} {shlex.join(args)} 2>&1"


def _exec_hosts(c: Cluster, n: int) -> dict[str, str]:
    return {f"exec{i}": e["public_ip"] for i, e in enumerate(c.roles["execs"][:n])}


def _hosts_in_use(c: Cluster, s: Scenario, n: int) -> dict[str, str]:
    if s.single_host:
        return {"single": c.roles["runner"]["public_ip"]}
    hosts = {r: c.roles[r]["public_ip"] for r in ("runner", "event", "db")}
    if s.indexer and c.roles.get("indexer"):
        hosts["indexer"] = c.roles["indexer"]["public_ip"]
    hosts.update(_exec_hosts(c, n))
    return hosts


def exec_plan(hosts: list[str], cores: int | None,
              l3: dict[str, list[list[int]]] | None) -> dict[str, list[str]]:
    """The AllowedCPUs of each hog-exec (control port CONTROL_PORT + i) on
    each exec host. Without l3: one hog-exec on CPUs 0..cores-1, or with no
    limit ("") when cores is None. With l3 (exec_per_l3: each host's L3
    groups): the per-L3 layout of K = cores (cpus.exec_layout). The hosts
    must agree on the L3 size, so each one runs the same number of hog-execs."""
    if l3 is None:
        return {h: ["" if cores is None else f"0-{cores - 1}"] for h in hosts}
    if cores is None:
        raise ValueError("exec_per_l3 needs the run's exec cores")
    sizes = {h: len(l3[h][0]) for h in hosts}
    if len(set(sizes.values())) > 1:
        raise RuntimeError("exec hosts disagree on the L3 size: "
                           + ", ".join(f"{h} {size} CPUs" for h, size in sizes.items()))
    plan = {}
    for h in hosts:
        try:
            plan[h] = [format_cpuset(cpus) for cpus in exec_layout(l3[h], cores)]
        except ValueError as e:
            raise RuntimeError(f"exec layout on {h}: {e}") from None
    return plan


def exec_setup_doc(state: dict[str, dict[str, dict]], plan: dict[str, list[str]],
                   l3: dict[str, list[list[int]]] | None) -> dict[str, dict]:
    """exec-setup.json: per exec host, the L3 groups the layout used ("l3",
    null without exec_per_l3) and each control unit ("ports"): the cpuset
    Whitney set ("want", null for no limit or a port it did not set), and
    the effective cpuset and allocator the host reported (exec_state)."""
    doc: dict[str, dict] = {}
    for name, ports in state.items():
        want = {str(CONTROL_PORT + i): cpus or None for i, cpus in enumerate(plan[name])}
        doc[name] = {"l3": [format_cpuset(g) for g in l3[name]] if l3 else None,
                     "ports": {port: {"want": want.get(port), **got}
                               for port, got in ports.items()}}
    return doc


def check_exec_setup(doc: dict[str, dict], plan: dict[str, list[str]], cores: int | None,
                     allocator: str) -> None:
    """Fail unless every exec host in doc (exec_setup_doc) has the control
    units of plan (exec_plan) and no others, each with exactly its planned
    cpuset ("" in plan: any) and allocator, and K = cores CPUs in all (any
    count when None)."""
    for name, entry in doc.items():
        ports = entry["ports"]
        if not ports:
            raise RuntimeError(f"exec setup: {name} has no control units")
        want = {str(CONTROL_PORT + i): cpus for i, cpus in enumerate(plan[name])}
        have = sorted(ports, key=int)
        if have != list(want):
            raise RuntimeError(f"exec setup: {name} has control units on ports "
                               f"{', '.join(have)}, want {', '.join(want)}")
        total = 0
        for port, cpus_want in want.items():
            got, unit = ports[port], f"whitney-control@{port}"
            cpus = got["cpus"]
            count = cpuset_size(cpus) if cpus else 0
            total += count
            if cpus_want and (not cpus or parse_cpuset(cpus) != parse_cpuset(cpus_want)):
                raise RuntimeError(f"core limit not applied on {name}: {unit} has cpuset "
                                   f"{cpus or 'none'} ({count} CPUs), want {cpus_want} "
                                   f"({cpuset_size(cpus_want)} CPUs)")
            if got["allocator"] is None:
                raise RuntimeError(f"exec setup: {unit} on {name} has no control_server process")
            if got["allocator"] != allocator:
                raise RuntimeError(f"allocator not applied on {name}: {unit} runs with "
                                   f"{got['allocator']}, want {allocator}")
        if cores is not None and total != cores:
            raise RuntimeError(f"core limit not applied on {name}: its control units have "
                               f"{total} CPUs in all, want {cores}")


def collect(c: Cluster, s: Scenario, n: int, run_dir: Path, ex: Exec, *,
            trials: bool = True, deadline: float | None = None) -> list[str]:
    """Stop Vector and sar, then copy trials, host facts, sar data, metrics,
    and daemon logs back. Returns errors.

    trials=False skips trials.jsonl: when the runner never started, the file on
    the runner host belongs to an earlier run. Steps that would start after
    deadline (time.monotonic()) are skipped.
    """
    errors: list[str] = []
    for d in ("host-facts", "sar", "metrics", "daemon-logs"):
        (run_dir / d).mkdir(exist_ok=True)

    def step(what: str, fn) -> None:
        if deadline is not None and time.monotonic() > deadline:
            errors.append(f"collect {what}: skipped: time budget spent")
            return
        try:
            fn()
        except CommandError as e:
            errors.append(f"collect {what}: {e}")

    runner_ip = c.roles["runner"]["public_ip"]
    if trials:
        step("trials", lambda: scp_from(c, runner_ip, REMOTE_TRIALS, run_dir / "trials.jsonl", ex))
    metrics = f"{METRICS_DIR}/{run_dir.name}.ndjson"
    hosts = _hosts_in_use(c, s, n)
    # Stopping Vector flushes its file sink (and the remote-write sink). The
    # stops run on every host at once; the metrics step below waits for each.
    for name, ip in hosts.items():
        step(f"{name} metrics stop", lambda: ssh(
            c, ip, f"sudo systemctl stop --no-block {VECTOR_UNIT}", ex))
    for name, ip in hosts.items():
        step(f"{name} sar stop", lambda: ssh(c, ip, "sudo systemctl stop whitney-sar.service", ex))
        step(f"{name} host facts", lambda: scp_from(
            c, ip, f"{STATE}/host-facts.json", run_dir / "host-facts" / f"{name}.json", ex))
        step(f"{name} sar", lambda: scp_from(
            c, ip, f"{STATE}/sar.bin", run_dir / "sar" / f"{name}.bin", ex))
        step(f"{name} metrics", lambda: (
            ssh(c, ip, _metrics_gzip(metrics), ex),
            scp_from(c, ip, f"{metrics}.gz", run_dir / "metrics" / f"{name}.ndjson.gz", ex)))
        if name != "runner":  # control_server writes daemon output to TMPDIR
            step(f"{name} daemon logs", lambda: scp_from(
                c, ip, "/mnt/whitney/tmp", run_dir / "daemon-logs" / name, ex, recursive=True))
    return errors


# Vector stops within its 10 s graceful-shutdown limit; systemd's own stop
# timeout is 90 s.
VECTOR_STOP_WAIT_SECONDS = 100


def _metrics_gzip(path: str) -> str:
    """Wait until Vector has stopped, then gzip the run's metrics file in
    place (the raw file goes, so it does not pile up on the root disk). A
    retry finds the .gz already there."""
    return f"""set -eu
i=0
while :; do
  case "$(systemctl show -p ActiveState --value {VECTOR_UNIT})" in
    inactive | failed) break ;;
  esac
  i=$((i + 1))
  if [ "$i" -ge {VECTOR_STOP_WAIT_SECONDS * 2} ]; then
    echo "whitney-vector still running after {VECTOR_STOP_WAIT_SECONDS} s"
    exit 1
  fi
  sleep 0.5
done
if [ -f {path} ]; then gzip -f {path}; fi
test -f {path}.gz
"""


def interrupt_reason(e: BaseException) -> str:
    """"interrupted: <signal>" for SIGTERM/SIGHUP, or the exception name for Ctrl-C."""
    return f"interrupted: {str(e) or type(e).__name__}"


def write_metrics_doc(run_dir: Path, remote_write: RemoteWrite | None) -> None:
    """metrics.json: how the run's metrics were collected. Of the remote-write
    endpoint it keeps only the host."""
    doc = {"remote_write": remote_write is not None,
           "remote_write_host": remote_write.host if remote_write else None,
           "vector_version": VECTOR_VERSION, "scrape_interval_secs": SCRAPE_INTERVAL_SECS}
    (run_dir / "metrics.json").write_text(json.dumps(doc, indent=2) + "\n")


def run_once(s: Scenario, cluster: Cluster, n: int, rep: int, out_dir: Path, ex: Exec, *,
             runner_timeout: int = RUNNER_TIMEOUT,
             remote_write: RemoteWrite | None = None, cores: int | None = None) -> RunResult:
    """Run the scenario on the first n execs and fill out_dir (the run dir).

    Multi-host: hog-exec on each exec in use gets CPUs 0..cores-1 (no limit
    when cores is None) and the scenario's exec_allocator. With exec_per_l3,
    each exec runs one hog-exec per L3 group instead (exec_plan), each in
    its own control unit. What the hosts report after the restart goes to
    exec-setup.json, and other CPUs than planned, or another allocator,
    fails the run.

    Vector starts on every host in use before the runner, with the run's
    labels (and the remote-write endpoint when remote_write is set), and
    stops before the collect.

    Never raises for a run failure: the result and status.json say "failed".
    An interrupt (Ctrl-C, SIGTERM, SIGHUP) is recorded as a failed run, with
    a best-effort collect, and then raised again so the caller destroys.
    """
    started = _now()
    out_dir.mkdir(parents=True, exist_ok=True)
    k = f" cores={cores}" if cores is not None else ""
    _log(f"run n={n}{k} rep={rep} -> {out_dir}")
    cmd = runner_command(s, n, runner_timeout, cores)
    _add_command(out_dir, "runner", cmd)
    write_metrics_doc(out_dir, remote_write)
    runner_ip = cluster.roles["runner"]["public_ip"]
    errors: list[str] = []
    log = out_dir / "runner.log"
    runner_launched = False
    try:
        try:
            # First, so a failure below never leaves an earlier run's files to collect.
            ssh(cluster, runner_ip, f"rm -f {REMOTE_TRIALS} {REMOTE_ROLES}", ex)
            procs = 1
            if not s.single_host:
                execs = _exec_hosts(cluster, n)
                l3 = l3_topology(cluster, execs, ex) if s.exec_per_l3 else None
                plan = exec_plan(list(execs), cores, l3)
                procs = len(next(iter(plan.values())))
                # The L3 read already bounds the per-L3 layout by the host's CPUs.
                setup_execs(cluster, execs, plan, s.exec_allocator, ex,
                            min_cpus=None if l3 else cores)
            restart_controls(cluster, ex)
            if not s.single_host:
                doc = exec_setup_doc(exec_state(cluster, execs, ex), plan, l3)
                (out_dir / "exec-setup.json").write_text(json.dumps(doc, indent=2) + "\n")
                check_exec_setup(doc, plan, cores, s.exec_allocator)
            start_metrics(cluster, _hosts_in_use(cluster, s, n),
                          run_env(out_dir.name, s.name, n, rep), remote_write, ex)
            (out_dir / "roles.json").write_text(roles_json(cluster, n, s, procs))
            scp_to(cluster, out_dir / "roles.json", runner_ip, REMOTE_ROLES, ex)
            runner_launched = True
            cp = ssh(cluster, runner_ip, cmd, ex, check=False)
            output = cp.stdout + cp.stderr
            log.write_text(output)
            if cp.returncode == 124:
                errors.append(f"runner timed out after {runner_timeout} s: {tail(output)}")
            elif cp.returncode != 0:
                errors.append(f"runner exited {cp.returncode}: {tail(output)}")
        except Exception as e:  # noqa: BLE001 - any failure is a failed run
            errors.append(str(e) or type(e).__name__)
        if not log.exists():
            log.write_text(f"runner not started: {errors[0] if errors else 'unknown'}\n")
        errors += collect(cluster, s, n, out_dir, ex, trials=runner_launched)
    except BaseException as e:
        if isinstance(e, Exception):
            raise
        _record_interrupt(e, cluster, s, n, out_dir, ex, started, runner_launched)
        raise
    trials = out_dir / "trials.jsonl"
    if not errors and not (trials.is_file() and trials.read_text().strip()):
        errors.append("runner exited 0 but trials.jsonl is empty")
    error = redact("\n".join(errors)) if errors else None
    status: Literal["ok", "failed"] = "failed" if errors else "ok"
    write_status(out_dir, status, error, started, _now())
    _log(f"run n={n}{k} rep={rep}: {status}")
    return RunResult(out_dir, status, error)


def _record_interrupt(e: BaseException, cluster: Cluster, s: Scenario, n: int, out_dir: Path,
                      ex: Exec, started: str, runner_launched: bool) -> None:
    """Write a failed status for an interrupted run, then collect what it can.

    The status is written before the collect, so a second interrupt still
    leaves it. The collect gets INTERRUPT_COLLECT_SECONDS; a second Ctrl-C
    ends it early (later SIGTERM/SIGHUP are ignored by the CLI).
    """
    errors = [interrupt_reason(e)]
    _log(f"run n={n}: {errors[0]}; collecting what is there")
    log = out_dir / "runner.log"
    # Partial output is runner output only if the runner call was the one
    # interrupted; before that it belongs to a setup step.
    partial = partial_output(e) if runner_launched else ""
    if not log.exists():
        if partial:
            log.write_text(partial)
        elif runner_launched:
            log.write_text(f"runner output not available: {errors[0]}\n")
        else:
            log.write_text(f"runner not started: {errors[0]}\n")
    write_status(out_dir, "failed", errors[0], started, _now())
    try:
        errors += collect(cluster, s, n, out_dir, ex, trials=runner_launched,
                          deadline=time.monotonic() + INTERRUPT_COLLECT_SECONDS)
    except BaseException as again:  # noqa: BLE001 - never mask the first interrupt
        errors.append("collect skipped: interrupted again" if not isinstance(again, Exception)
                      else f"collect failed: {again}")
    write_status(out_dir, "failed", "\n".join(errors), started, _now())


# ---- whitney run ---------------------------------------------------------------------


def run_scenario(s: Scenario, n: int, opts: RunOptions, ex: Exec,
                 command: list[str], cores: int | None = None) -> RunResult:
    """Build or reuse the archive, bring up a cluster for n execs, run once
    (hog-exec limited to cores CPUs when set), collect, and destroy (unless
    opts.keep). Destroy runs after any failure."""
    if s.exec_cores and cores is None:
        raise ScenarioError("the scenario has exec_cores: give --cores")
    if cores is not None and not s.exec_cores:
        raise ScenarioError("--cores needs a scenario with exec_cores")
    require_placement(s)
    check_block_size(s, n, cores)  # n and cores may be outside n_list and exec_cores
    if s.single_host:  # fail before paying for a cluster
        from whitney.sweep import validate_cpu_layout
        validate_cpu_layout(s, n)
    creds = prepare_aws(opts.aws_creds, s.region, opts.cache, ex,
                        max_lifetime_minutes=opts.max_lifetime_minutes)
    if opts.archive:
        archive = load_archive(opts.archive, s)
    else:
        archive = ensure_build(s, opts.cache, ex, arch=opts.arch)
    run_dir = new_run_dir(opts.results_root, s, n, rep=1, cores=cores)
    write_run_inputs(run_dir, s, archive, command, aws=creds.record())
    workdir = new_tofu_workdir(opts.cache / "clusters", run_dir.name)
    started = _now()
    _log(f"run dir {run_dir}, tofu workdir {workdir}")
    try:
        url = upload_archive(archive, opts.bucket, ex, region=s.region)
        cluster = apply_cluster(s, n, url, archive.sha256, workdir, ex,
                                ssh_key_name=opts.ssh_key_name, operator_cidr=opts.operator_cidr,
                                max_lifetime_minutes=opts.max_lifetime_minutes,
                                ssh_identity=opts.ssh_identity, tofu_env=creds.tofu_env,
                                instance_create_timeout=opts.instance_create_timeout,
                                apply_timeout=opts.apply_timeout)
        result = run_once(s, cluster, n, 1, run_dir, ex, runner_timeout=opts.runner_timeout,
                          remote_write=opts.remote_write, cores=cores)
    except Exception as e:  # noqa: BLE001 - recorded as a failed run
        error = redact(str(e) or type(e).__name__)
        write_status(run_dir, "failed", error, started, _now())
        result = RunResult(run_dir, "failed", error)
    except BaseException as e:  # Ctrl-C, SIGTERM, SIGHUP: record, then destroy
        if read_status(run_dir) is None:  # run_once writes its own; keep a good one
            write_status(run_dir, "failed", interrupt_reason(e), started, _now())
        raise
    finally:
        if opts.keep:
            _log("--keep: cluster left up. Destroy it with: "
                 f"{destroy_command(workdir, creds.tofu_env)}")
        else:
            try:
                notes = destroy_cluster(workdir, ex, creds.tofu_env, region=s.region)
            except DestroyError as e:
                add_cleanup(run_dir, e.notes)
                raise
            add_cleanup(run_dir, notes)
    return result
