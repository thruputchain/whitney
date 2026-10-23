"""Sweeps: repetitions of a fresh max-size cluster, every N (and exec core
count) in random order.

Also the single-host layout: all roles on one host, pinned by cpu_layout.
"""
from __future__ import annotations

import json
import random
import secrets
from datetime import datetime, timezone
from pathlib import Path

from whitney.awscreds import prepare_aws
from whitney.build import ensure_build, load_archive, upload_archive
from whitney.config import Scenario, ScenarioError
from whitney.metrics import metrics_port
from whitney.infra import (CapacityError, DestroyError, LaunchTimeout, apply_cluster,
                           destroy_cluster, destroy_command, new_tofu_workdir,
                           require_placement)
from whitney.proc import Exec, redact
from whitney.run import (BIN_DIR, CONTROL_PORT, WORK_ROOT, RunOptions, RunResult, _log, _now,
                         add_cleanup, interrupt_reason, new_run_dir, read_status, run_once,
                         write_run_inputs, write_status, write_text_atomic)

# vCPU count per single-host instance type (AWS instance type docs). Add a
# row to support another type.
INSTANCE_CPUS = {
    "m8a.large": 2, "m8a.xlarge": 4, "m8a.2xlarge": 8, "m8a.48xlarge": 192,
    "r8a.large": 2, "r8a.xlarge": 4, "r8a.16xlarge": 64,
    "x8aedz.large": 2, "x8aedz.xlarge": 4, "x8aedz.12xlarge": 48,
}


def parse_cpus(value: str) -> set[int]:
    """Parse a comma list of `a-b` or `a` into a set of CPU numbers."""
    cpus: set[int] = set()
    for part in value.split(","):
        lo, sep, hi = part.strip().partition("-")
        try:
            a = int(lo)
            b = int(hi) if sep else a
        except ValueError:
            raise ScenarioError(f"cpu_layout: bad CPU range {value!r}") from None
        if a < 0 or b < a:
            raise ScenarioError(f"cpu_layout: bad CPU range {value!r}")
        cpus.update(range(a, b + 1))
    return cpus


def _role_names(s: Scenario, n: int) -> list[str]:
    """Control-port order: event, db, indexer (if on), exec0..exec{n-1}."""
    return ["event", "db", *(["indexer"] if s.indexer else []), *(f"exec{i}" for i in range(n))]


def validate_cpu_layout(s: Scenario, n: int) -> dict[str, set[int]]:
    """Check cpu_layout for a run with n execs. Returns role -> CPU set."""
    inst = s.instance_types["single"]
    if inst not in INSTANCE_CPUS:
        raise ScenarioError(f"cpu_layout: unknown CPU count for instance type {inst}")
    top = INSTANCE_CPUS[inst] - 1
    cpus: dict[str, set[int]] = {}
    for role in ["runner", *_role_names(s, n)]:
        if role not in s.cpu_layout:
            raise ScenarioError(f"cpu_layout needs {role}")
        cpus[role] = parse_cpus(s.cpu_layout[role])
        if max(cpus[role]) > top:
            raise ScenarioError(f"cpu_layout {role} is outside 0-{top}: {s.cpu_layout[role]}")
    seen: dict[int, str] = {}
    for role, cs in cpus.items():
        for c in sorted(cs):
            if c in seen:
                raise ScenarioError(f"cpu_layout overlaps: {seen[c]} and {role} both use CPU {c}")
            seen[c] = role
    return cpus


def single_host_roles(s: Scenario, ip: str, n: int | None = None) -> dict:
    """roles.json document with every role on one host (n defaults to max(n_list))."""
    n = max(s.n_list) if n is None else n
    validate_cpu_layout(s, n)

    def role(i: int, name: str) -> dict:
        return {"host": ip, "control_port": CONTROL_PORT + i,
                "metrics_port": metrics_port(name, single_host=True),
                "work_dir": f"{WORK_ROOT}/{name}", "cpu_affinity": s.cpu_layout[name]}

    named = {name: role(i, name) for i, name in enumerate(_role_names(s, n))}
    return {
        "bin_dir": BIN_DIR,
        "runner_bind_ip": ip,
        "event": named["event"],
        "db": named["db"],
        "indexer": named.get("indexer"),
        "execs": [named[f"exec{i}"] for i in range(n)],
    }


def runner_cpus(s: Scenario) -> str | None:
    return s.cpu_layout["runner"] if s.single_host else None


def plan_sweep(s: Scenario, seed: int) -> list[list]:
    """One shuffled copy of n_list per repetition. With exec_cores, one
    shuffled copy of every [N, K] pair of n_list × exec_cores instead."""
    rng = random.Random(seed)
    plan = []
    for _ in range(s.repetitions):
        order: list = ([[n, k] for n in s.n_list for k in s.exec_cores] if s.exec_cores
                       else list(s.n_list))
        rng.shuffle(order)
        plan.append(order)
    return plan


def _n_cores(item) -> tuple[int, int | None]:
    """A plan item (N, or [N, K]) as (N, K or None)."""
    return (item, None) if isinstance(item, int) else (item[0], item[1])


def sweep(s: Scenario, opts: RunOptions, ex: Exec, seed: int | None = None,
          command: list[str] | None = None) -> list[RunResult]:
    """Run every repetition on its own fresh max-size cluster.

    Run dirs go under <opts.results_root>/<stamp>-<name>-<ref>-sweep/, with
    sweep.json beside them. A failed run is recorded and the sweep goes on.
    Each run is in sweep.json from its start ("running"). If the sweep stops
    early (interrupt, destroy failure), the run in progress is recorded as
    failed and sweep.json gets "aborted": true and the reason.
    """
    require_placement(s)
    if s.single_host:
        validate_cpu_layout(s, max(s.n_list))
    seed = secrets.randbits(32) if seed is None else seed
    plan = plan_sweep(s, seed)
    creds = prepare_aws(opts.aws_creds, s.region, opts.cache, ex,
                        max_lifetime_minutes=opts.max_lifetime_minutes)
    archive = (load_archive(opts.archive, s) if opts.archive
               else ensure_build(s, opts.cache, ex, arch=opts.arch))
    now = datetime.now(timezone.utc)
    root = opts.results_root / f"{now:%Y%m%d-%H%M}-{s.name}-{s.hogd_ref}-sweep"
    root.mkdir(parents=True)
    doc = {"seed": seed, "order": plan, "scenario": s.name, "hogd_ref": s.hogd_ref,
           "archive_sha256": archive.sha256, "aborted": False, "abort_reason": None,
           "cleanup": [], "runs": []}

    def save() -> None:
        write_text_atomic(root / "sweep.json", json.dumps(doc, indent=2) + "\n")

    save()
    results: list[RunResult] = []
    n_exec = max(s.n_list)
    try:
        for rep, order in enumerate(plan, start=1):
            _log(f"repetition {rep}/{len(plan)}: order {order}")
            cluster = None
            workdir = new_tofu_workdir(opts.cache / "clusters", f"{root.name}-r{rep}")
            apply_error = None
            try:
                try:
                    url = upload_archive(archive, opts.bucket, ex, region=s.region)
                    cluster = apply_cluster(
                        s, n_exec, url, archive.sha256, workdir, ex,
                        ssh_key_name=opts.ssh_key_name, operator_cidr=opts.operator_cidr,
                        max_lifetime_minutes=opts.max_lifetime_minutes,
                        ssh_identity=opts.ssh_identity, tofu_env=creds.tofu_env,
                        instance_create_timeout=opts.instance_create_timeout,
                        apply_timeout=opts.apply_timeout)
                except Exception as e:  # noqa: BLE001 - recorded as failed runs
                    apply_error = redact(str(e) or type(e).__name__)
                    if not isinstance(e, (CapacityError, LaunchTimeout)):  # keep prefix
                        apply_error = f"cluster apply failed: {apply_error}"
                for item in order:
                    n, cores = _n_cores(item)
                    run_dir = new_run_dir(root, s, n, rep, cores=cores)
                    entry = {"run_dir": run_dir.name, "n": n, "cores": cores, "rep": rep,
                             "status": "running"}
                    doc["runs"].append(entry)
                    save()
                    results.append(_one(s, cluster, apply_error, run_dir, n, rep, archive, opts,
                                        ex, command or ["whitney", "sweep"], creds.record(),
                                        cores))
                    entry["status"] = results[-1].status
                    save()
            finally:
                if opts.keep:
                    _log("--keep: cluster left up. Destroy it with: "
                         f"{destroy_command(workdir, creds.tofu_env)}")
                else:
                    notes: list[str] = []
                    try:
                        notes = destroy_cluster(workdir, ex, creds.tofu_env, region=s.region)
                    except DestroyError as e:
                        notes = e.notes
                        raise
                    finally:
                        doc["cleanup"] += [{"rep": rep, "note": redact(n)} for n in notes]
                        for entry in doc["runs"]:
                            if entry["rep"] == rep:
                                add_cleanup(root / entry["run_dir"], notes)
                        save()
    except BaseException as e:
        doc["aborted"] = True
        doc["abort_reason"] = (interrupt_reason(e) if not isinstance(e, Exception)
                               else redact(str(e) or type(e).__name__))
        for entry in doc["runs"]:
            if entry["status"] == "running":
                entry["status"] = _status_of(root / entry["run_dir"])
        raise
    finally:
        save()
    return results


def _status_of(run_dir: Path) -> str:
    """The status a stopped run left in status.json, or failed without a good one."""
    return read_status(run_dir) or "failed"


def _one(s, cluster, apply_error, run_dir, n, rep, archive, opts, ex, command,
         aws, cores) -> RunResult:
    started = _now()
    try:
        write_run_inputs(run_dir, s, archive, command, aws=aws)
        if cluster is None:
            write_status(run_dir, "failed", apply_error, started, started)
            return RunResult(run_dir, "failed", apply_error)
        return run_once(s, cluster, n, rep, run_dir, ex, runner_timeout=opts.runner_timeout,
                        remote_write=opts.remote_write, cores=cores)
    except Exception as e:  # noqa: BLE001 - run_once should not raise, but never drop a run
        error = redact(str(e) or type(e).__name__)
        write_status(run_dir, "failed", error, started, _now())
        return RunResult(run_dir, "failed", error)
    except BaseException as e:  # Ctrl-C, SIGTERM, SIGHUP: record, then destroy
        if read_status(run_dir) is None:  # run_once writes its own; keep a good one
            write_status(run_dir, "failed", interrupt_reason(e), started, _now())
        raise
