"""OpenTofu cluster lifecycle. Each cluster gets its own copy of infra/."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from whitney.awscreds import env_prefix
from whitney.config import Scenario, ScenarioError
from whitney.proc import CommandError, CommandTimeout, EnvOverride, Exec, redact
from whitney.remote import wait_ready

REPO = Path(__file__).resolve().parents[1]
INFRA = REPO / "infra"
# Holds the presigned archive URL; tofu loads *.auto.tfvars.json on its own.
TFVARS = "whitney.auto.tfvars.json"
READY_TIMEOUT = 1800
# aws_instance timeouts.create: bounds the wait for each instance to reach
# running after RunInstances worked. It does not bound the SDK retries of
# InsufficientInstanceCapacity; the apply deadline and max_retries do.
INSTANCE_CREATE_TIMEOUT = "6m"
# Default whole-apply deadline: the create timeout plus this margin.
APPLY_MARGIN_SECONDS = 240
# Provider max_retries: how often the AWS SDK retries one API call
# (InsufficientInstanceCapacity is a retryable HTTP 500). Provider default: 25.
MAX_RETRIES = 5
CAPACITY_CODE = "InsufficientInstanceCapacity"
# Zones that offer x8aedz, per region, at the time of the first EC2 runs.
X8AEDZ_ZONES = {"us-west-2": ("us-west-2a", "us-west-2b")}


@dataclass
class Cluster:
    roles: dict  # tofu output "roles": runner, event, db, indexer, execs
    workdir: Path
    placement_group: str = ""
    ssh_identity: Path | None = None

    def hosts(self) -> dict[str, str]:
        """Every distinct host as name -> public IP, runner first."""
        named = [("runner", self.roles["runner"]), ("event", self.roles["event"]),
                 ("db", self.roles["db"]), ("indexer", self.roles.get("indexer"))]
        named += [(f"exec{i}", e) for i, e in enumerate(self.roles["execs"])]
        out: dict[str, str] = {}
        for name, view in named:
            if view and view["public_ip"] not in out.values():
                out[name] = view["public_ip"]
        return out


class DestroyError(RuntimeError):
    """tofu destroy failed. notes: what the tag sweep found or did."""

    def __init__(self, msg: str, notes: list[str] | None = None):
        super().__init__(msg)
        self.notes = notes or []


class CapacityError(RuntimeError):
    """EC2 capacity trouble, or an apply that ran past its deadline. The
    message starts with "capacity"."""


class LaunchTimeout(RuntimeError):
    """RunInstances worked, but an instance did not reach running in time."""


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)([hms])")
_DURATION = re.compile(r"(?:\d+(?:\.\d+)?[hms])+")


def duration_seconds(value: str) -> float:
    """Seconds in a Go duration as tofu timeouts take it (6m, 1h30m, 90s)."""
    if not _DURATION.fullmatch(value):
        raise ValueError(f"not a duration like 6m or 1h30m: {value!r}")
    unit = {"h": 3600, "m": 60, "s": 1}
    return sum(float(n) * unit[u] for n, u in _DURATION_PART.findall(value))


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:g}m"


def host_types(s: Scenario, n_exec: int) -> dict[str, str]:
    """tofu host name -> instance type, as infra/main.tf lays them out."""
    t = s.instance_types
    if s.single_host:
        return {"single": t["single"]}
    hosts = {"runner": t["runner"], "event": t["event"], "db": t["db"]}
    if s.indexer:
        hosts["indexer"] = t["indexer"]
    hosts.update({f"exec{i}": t["exec"] for i in range(n_exec)})
    return hosts


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


def _describe(names: list[str], hosts: dict[str, str]) -> str:
    """'r8a.16xlarge (runner, exec0), x8aedz.12xlarge (db)'."""
    by_type: dict[str, list[str]] = {}
    for n in names:
        by_type.setdefault(hosts[n], []).append(n)
    return ", ".join(f"{t} ({', '.join(ns)})" for t, ns in by_type.items()) or "the hosts"


# Only diagnostics (stderr) name a failed resource this way; progress lines
# on stdout ("aws_instance.host[...]: Creating...") name every host.
_DIAG_HOST = re.compile(r'with aws_instance\.host\["([^"]+)"\]')
_CREATED = re.compile(r'aws_instance\.host\["([^"]+)"\]: Creation complete')
_RUN_CANCELED = re.compile(r"RunInstances[^\n]*(?:canceled|deadline exceeded)")
_NOT_RUNNING = "timeout while waiting for state to become 'running'"


def apply_failure(stdout: str, stderr: str, s: Scenario, hosts: dict[str, str],
                  create_timeout: str, deadline: float | None = None,
                  elapsed: float = 0.0) -> Exception | None:
    """Classify a failed tofu apply (-no-color output). deadline is set when
    whitney stopped the apply at its deadline; elapsed is how long the apply
    ran, stop included. Returns None for other errors.

    - InsufficientInstanceCapacity in the output: CapacityError "capacity: ...".
    - Apply deadline hit, or RunInstances canceled at the create timeout:
      CapacityError "capacity or launch timeout: ...".
    - An instance that did not reach running: LaunchTimeout.
    """
    out = f"{stdout}\n{stderr}"
    failed = [h for h in _unique(_DIAG_HOST.findall(stderr)) if h in hosts]
    if CAPACITY_CODE in out:
        types = _unique(re.findall(r"sufficient (\S+) capacity", out)) or \
            _unique(hosts[h] for h in failed)
        zone = re.search(r"Availability Zone you requested \(([a-z0-9-]+)\)", out)
        az = zone.group(1) if zone else s.availability_zone
        msg = (f"capacity: EC2 has no {', '.join(types) or 'instance'} capacity in {az} "
               f"({CAPACITY_CODE}).")
    elif deadline is not None:
        created = set(_CREATED.findall(stdout))
        pending = [h for h in hosts if h not in created]
        az = s.availability_zone
        msg = (f"capacity or launch timeout: tofu apply ran past its {_minutes(deadline)} "
               f"deadline and was stopped after {elapsed / 60:.1f}m (most often "
               f"{CAPACITY_CODE} for {_describe(pending, hosts)}) in {az}.")
    elif failed and _RUN_CANCELED.search(stderr):
        az = s.availability_zone
        msg = (f"capacity or launch timeout: RunInstances for {_describe(failed, hosts)} did "
               f"not finish within {create_timeout} (most often {CAPACITY_CODE}) in {az}.")
    elif failed and _NOT_RUNNING in stderr:
        names = ", ".join(f"{h} ({hosts[h]})" for h in failed)
        return LaunchTimeout(f"launch timeout: instance {names} did not reach running within "
                             f"{create_timeout} (--instance-create-timeout)")
    else:
        return None
    suggested = re.search(r"choosing ([a-z0-9-]+(?:, [a-z0-9-]+)*)", out)
    if suggested:
        msg += f" AWS suggests {suggested.group(1)}."
    msg += " Set availability_zone to another zone that offers every instance type"
    others = [z for z in X8AEDZ_ZONES.get(s.region, ()) if z != az]
    if others and any(t.startswith("x8aedz.") for t in hosts.values()):
        msg += f" (x8aedz in {s.region}: {' or '.join(others)})"
    msg += ", retry later, or use a capacity reservation for large runs."
    return CapacityError(msg)


def require_placement(s: Scenario) -> None:
    if not s.availability_zone or not s.ami_id:
        raise ScenarioError(
            "availability_zone and ami_id must be set in the scenario "
            "(see the Scenarios section of docs/usage.md)")


def new_tofu_workdir(out_root: Path, run_id: str) -> Path:
    """Copy infra/ to <out_root>/<run_id>. Its tofu state stays there."""
    wd = out_root / run_id
    if wd.exists():
        raise FileExistsError(f"tofu workdir already exists: {wd}")
    out_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(INFRA, wd, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "tests"))
    return wd


def apply_cluster(s: Scenario, n_exec: int, archive_url: str, sha: str, workdir: Path, ex: Exec,
                  *, ssh_key_name: str, operator_cidr: str, max_lifetime_minutes: int,
                  ssh_identity: Path | None = None, tofu_env: EnvOverride | None = None,
                  instance_create_timeout: str = INSTANCE_CREATE_TIMEOUT,
                  apply_timeout: str | None = None,
                  ready_timeout: int = READY_TIMEOUT) -> Cluster:
    """tofu apply in workdir, then wait until every host is ready.

    Each host powers off, and so terminates, max_lifetime_minutes after boot.
    tofu_env changes the environment of every tofu call (AWS credentials).
    tofu apply is stopped (SIGTERM, so it cancels and writes its state) after
    apply_timeout, by default instance_create_timeout + 4m. EC2 capacity
    trouble and that deadline raise CapacityError; the caller destroys.
    """
    require_placement(s)
    if n_exec < 1:
        raise ValueError(f"n_exec must be >= 1, got {n_exec}")
    tfvars = {
        "region": s.region,
        "availability_zone": s.availability_zone,
        "ami_id": s.ami_id,
        "ssh_key_name": ssh_key_name,
        "operator_cidr": operator_cidr,
        "n_exec": n_exec,
        "storage": s.storage,
        "ebs_volume_type": s.ebs_volume_type,
        "ebs_volume_size_gib": s.ebs_volume_size_gib,
        "ebs_iops": s.ebs_iops,
        "ebs_throughput_mibps": s.ebs_throughput_mibps,
        "indexer": s.indexer,
        "single_host": s.single_host,
        "instance_types": s.instance_types,
        "archive_url": archive_url,
        "archive_sha256": sha,
        "cluster_name": workdir.name,
        "max_lifetime_minutes": max_lifetime_minutes,
        "instance_create_timeout": instance_create_timeout,
        "max_retries": MAX_RETRIES,
    }
    fd = os.open(workdir / TFVARS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tfvars, f, indent=2)
    deadline = (duration_seconds(apply_timeout) if apply_timeout
                else duration_seconds(instance_create_timeout) + APPLY_MARGIN_SECONDS)
    ex(["tofu", "init", "-input=false", "-no-color"], cwd=workdir, env=tofu_env)
    started = monotonic()
    try:
        ex(["tofu", "apply", "-auto-approve", "-input=false", "-no-color"], cwd=workdir,
           env=tofu_env, timeout=deadline)
    except CommandError as e:
        why = apply_failure(e.stdout, e.stderr, s, host_types(s, n_exec), instance_create_timeout,
                            deadline if isinstance(e, CommandTimeout) else None,
                            elapsed=monotonic() - started)
        if why:
            raise why from e
        raise
    out = json.loads(ex(["tofu", "output", "-json", "-no-color"], cwd=workdir,
                        env=tofu_env).stdout)
    c = Cluster(roles=out["roles"]["value"], workdir=workdir,
                placement_group=out.get("placement_group", {}).get("value", ""),
                ssh_identity=ssh_identity)
    wait_ready(c, ex, timeout=ready_timeout)
    return c


def destroy_command(workdir: Path, env: EnvOverride | None = None) -> str:
    """The manual destroy command, with an absolute path so it works from any
    cwd, and with the tofu env (the credential_process profile) if any."""
    return f"{env_prefix(env or {})}tofu -chdir={workdir.resolve()} destroy"


def _say(msg: str) -> None:
    try:
        print(f"whitney: {msg}", file=sys.stderr, flush=True)
    except OSError:  # the terminal may be gone (SIGHUP)
        pass


ORPHAN_STATES = "pending,running,stopping,stopped"


def _tag_filters(cluster_name: str) -> list[str]:
    return ["--filters", f"Name=tag:whitney,Values={cluster_name}",
            f"Name=instance-state-name,Values={ORPHAN_STATES}"]


def _terminate_command(region: str, ids: list[str] | None = None) -> str:
    return " ".join(["aws ec2 terminate-instances --region", shlex.quote(region),
                     "--instance-ids", " ".join(ids) if ids else "<ids>"])


def orphan_check_command(region: str, cluster_name: str) -> str:
    """The manual check and terminate commands for the tag sweep."""
    describe = shlex.join(["aws", "ec2", "describe-instances", "--region", region,
                           *_tag_filters(cluster_name),
                           "--query", "Reservations[].Instances[].InstanceId", "--output", "text"])
    return f"{describe}  then, for any ids it lists: {_terminate_command(region)}"


def terminate_orphans(region: str, cluster_name: str, ex: Exec) -> list[str]:
    """Find instances tagged whitney=<cluster_name> that are still alive and
    terminate them. tofu state can miss an instance whose RunInstances call
    was cut off (apply deadline, SIGKILL). Returns notes for the results.
    Uses the caller's env. Never raises an Exception: on failure it prints
    the manual commands and returns a note.
    """
    try:
        cp = ex(["aws", "ec2", "describe-instances", "--region", region,
                 *_tag_filters(cluster_name), "--query",
                 "Reservations[].Instances[].[InstanceId, Tags[?Key=='Name']|[0].Value]",
                 "--output", "json"])
        found = [(str(i), str(n or "")) for i, n in json.loads(cp.stdout or "[]")]
    except Exception as e:  # noqa: BLE001 - CommandError, bad JSON: report, keep going
        _say(f"orphan check failed: {redact(str(e))}\nCheck by hand: "
             f"{orphan_check_command(region, cluster_name)}")
        return [f"orphan check failed: {redact(str(e))}"]
    if not found:
        return []
    ids = [i for i, _ in found]
    names = ", ".join(f"{i} ({n})" for i, n in found)
    _say(f"instances tagged whitney={cluster_name} outlived tofu destroy: {names}; "
         f"terminating them")
    try:
        ex(["aws", "ec2", "terminate-instances", "--region", region, "--instance-ids", *ids])
    except Exception as e:  # noqa: BLE001
        _say(f"terminate failed: {redact(str(e))}\nTerminate them with: "
             f"{_terminate_command(region, ids)}")
        return [f"orphaned instances NOT terminated: {names}: {redact(str(e))}"]
    return [f"orphaned instances terminated: {names}"]


def destroy_cluster(workdir: Path, ex: Exec, env: EnvOverride | None = None, *,
                    region: str) -> list[str]:
    """tofu destroy in workdir, with env as for apply. Works after a partial
    apply. Then a tag sweep terminates instances that tofu state missed
    (see terminate_orphans). Returns the tag sweep notes.

    If destroy fails, the tag sweep still runs, and the DestroyError has the
    manual destroy command and the notes. If destroy is interrupted, the
    manual commands are printed before the interrupt goes up.
    """
    if not (workdir / TFVARS).is_file():
        return []  # apply never started: nothing to destroy
    try:
        ex(["tofu", "destroy", "-auto-approve", "-input=false", "-no-color"], cwd=workdir,
           env=env)
    except CommandError as e:
        notes = terminate_orphans(region, workdir.name, ex)
        raise DestroyError(
            f"tofu destroy failed; the cluster may still be running. "
            f"Retry with: {destroy_command(workdir, env)}\n{e}"
            + "".join(f"\n{n}" for n in notes), notes) from e
    except BaseException:
        _say(f"destroy interrupted; the cluster may still be running. "
             f"Destroy it with: {destroy_command(workdir, env)}\nThen check for tagged "
             f"instances: {orphan_check_command(region, workdir.name)}")
        raise
    return terminate_orphans(region, workdir.name, ex)
