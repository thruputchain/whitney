"""Test helpers: a fake Exec and archive and cluster builders."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable

from whitney.infra import Cluster
from whitney.proc import CommandError, CommandTimeout
from whitney.run import RunOptions

SIGNATURE = "X-Amz-Signature=deadbeefsecret"
PRESIGNED = f"https://bkt.s3.amazonaws.com/whitney/a.tar.gz?X-Amz-Expires=43200&{SIGNATURE}"

CALLER_ARN = "arn:aws:sts::123456789012:assumed-role/WhitneyOps/alice@example.com"
# What `aws configure export-credentials --format process` prints. Fake values.
PROCESS_CREDS = json.dumps({"Version": 1, "AccessKeyId": "ASIAFAKEFAKEFAKE",
                            "SecretAccessKey": "fake-secret", "SessionToken": "fake-token",
                            "Expiration": "2099-01-01T00:00:00Z"})

# The cache key of the fake prebuilt archive.
KEY = "v0.1.31-0123456789ab-znver5"

SCENARIO = """
name = "exec-scaling"
hogd_ref = "v0.1.31"
region = "us-east-1"
availability_zone = "us-east-1a"
ami_id = "ami-0123456789abcdef0"
n_list = [1, 2, 3]
"""


@dataclass
class Call:
    argv: list[str]
    cwd: Path | None
    env: dict | None = None
    secret: bool = False
    timeout: float | None = None

    @property
    def line(self) -> str:
        return " ".join(self.argv)


@dataclass
class Reply:
    rc: int = 0
    stdout: str = ""
    stderr: str = ""
    effect: Callable[[list[str]], str | None] | None = None  # a str replaces stdout
    hang: bool = False  # never exits: the call's timeout stops it (CommandTimeout)


@dataclass
class _Rule:
    pattern: str
    replies: list[Reply]


@dataclass
class FakeExec:
    """Records argv and returns canned replies.

    Rules match when their pattern is a substring of the joined argv. The
    newest matching rule wins. A rule's replies are used in order, and the
    last one repeats. An effect runs on each matching call. scp calls with
    no rule create the local target. describe-instances finds nothing. The AWS credential check (credential
    process, sts get-caller-identity) passes when no rule matches.
    """

    calls: list[Call] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)

    def on(self, pattern: str, *replies: Reply) -> None:
        self.rules.append(_Rule(pattern, list(replies) or [Reply()]))

    def __call__(self, argv, *, cwd=None, check=True, env=None, secret=False, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append(Call(argv, Path(cwd) if cwd else None,
                               dict(env) if env is not None else None, secret, timeout))
        reply = self._reply(argv)
        stdout = reply.stdout
        if reply.effect:
            out = reply.effect(argv)
            if isinstance(out, str):
                stdout = out
        if reply.hang:
            if timeout is None:
                raise AssertionError(f"{' '.join(argv)} hangs and has no timeout")
            raise CommandTimeout(CompletedProcess(argv, -15, stdout, reply.stderr), timeout)
        cp = CompletedProcess(argv, reply.rc, stdout, reply.stderr)
        if check and reply.rc != 0:
            raise CommandError(cp)
        return cp

    def _reply(self, argv: list[str]) -> Reply:
        line = " ".join(argv)
        for rule in reversed(self.rules):
            if rule.pattern in line:
                return rule.replies.pop(0) if len(rule.replies) > 1 else rule.replies[0]
        if argv[0] == "scp":
            return Reply(effect=fake_scp)
        if "export-credentials --format process" in line:
            return Reply(stdout=PROCESS_CREDS + "\n")
        if "ec2 describe-instances" in line:  # no orphaned instances
            return Reply(stdout="[]\n")
        if "sts get-caller-identity" in line:
            return Reply(stdout=CALLER_ARN + "\n")
        return Reply()

    def find(self, pattern: str) -> list[int]:
        return [i for i, c in enumerate(self.calls) if pattern in c.line]


def fake_scp(argv: list[str]) -> None:
    dest = Path(argv[-1])
    if ":" in argv[-1]:  # upload to a host
        return
    if "-r" in argv:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "hog-exec.stdout.abc123").write_text("daemon log\n")
    elif dest.name.endswith(".ndjson.gz"):
        dest.write_bytes(gzip.compress(b'{"name":"cpu_seconds_total"}\n'))
    elif dest.name == "trials.jsonl":
        dest.write_text(json.dumps({"nexec": 1, "batch_size": 1, "tps": 1.0}) + "\n")
    else:
        dest.write_text("{}\n")


def make_archive(out: Path, key: str, hogd_ref: str = "v0.1.31", arch: str = "znver5") -> Path:
    """Write whitney-<key>.tar.gz and its .sha256 the way build/package.sh does."""
    out.mkdir(parents=True, exist_ok=True)
    name = f"whitney-{key}"
    manifest = {"hogd_ref": hogd_ref, "hogd_commit": "c0ffee", "arch": arch,
                "whitney_src_hash": "ab" * 32}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo(f"{name}/manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        info = tarfile.TarInfo(f"{name}/bin")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    path = out / f"{name}.tar.gz"
    path.write_bytes(buf.getvalue())
    digest = hashlib.sha256(buf.getvalue()).hexdigest()
    (out / f"{name}.tar.gz.sha256").write_text(f"{digest}  {name}.tar.gz\n")
    return path


def host(i: int) -> dict:
    return {"id": f"i-{i:04d}", "private_ip": f"10.42.1.{10 + i}",
            "public_ip": f"203.0.113.{10 + i}", "instance_type": "r8a.16xlarge"}


def roles_output(n_execs: int, indexer: bool = False) -> dict:
    return {
        "runner": host(0),
        "event": host(1),
        "db": host(2),
        "indexer": host(3) if indexer else None,
        "execs": [host(100 + i) for i in range(n_execs)],
    }


def install_cluster(fx: FakeExec, n_execs: int) -> None:
    """Rules for a happy-path upload, apply, ready check, run, and destroy."""
    fx.on("aws s3 presign", Reply(stdout=PRESIGNED + "\n"))
    out = {"roles": {"value": roles_output(n_execs)},
           "placement_group": {"value": "pg-1"}, "subnet_az": {"value": "us-east-1a"}}
    fx.on("tofu output -json", Reply(stdout=json.dumps(out)))
    fx.on("then echo ready", Reply(stdout="ready\n"))
    # Exec state after the restart: one control unit, all 64 CPUs, glibc malloc.
    fx.on("cpuset.cpus.effective", Reply(stdout="11111 0-63 0\n"))
    fx.on("whitney-runner", Reply(stdout="role=exec0 stop\nrole=exec0 wait\n"))


def opts(tmp_path: Path, **kw) -> RunOptions:
    """RunOptions for a run or sweep under tmp_path, with a prebuilt archive."""
    kw.setdefault("archive", make_archive(tmp_path / "prebuilt", KEY))
    return RunOptions(bucket="bkt", ssh_key_name="me", operator_cidr="198.51.100.7/32",
                      results_root=tmp_path / "results", cache=tmp_path / "cache", **kw)


def status(run_dir: Path) -> dict:
    return json.loads((run_dir / "status.json").read_text())


def cluster(tmp_path: Path, n_execs: int = 2) -> Cluster:
    """A multi-host cluster with n_execs execs and a tofu workdir under tmp_path."""
    c = Cluster(roles=roles_output(n_execs), workdir=tmp_path / "wd")
    c.workdir.mkdir(exist_ok=True)
    return c


# A single-host scenario for four execs, and a cluster to run it on.
LAYOUT = ("cpu_layout={runner='0-15', event='16-23', db='24-39', exec0='40-77', "
          "exec1='78-115', exec2='116-153', exec3='154-191'}")
SINGLE = ("single_host=true", "storage='tmpfs'", "n_list=[4]", LAYOUT)


def single_cluster(workdir: Path, n: int = 4) -> Cluster:
    h = host(0)
    return Cluster(roles={"runner": h, "event": h, "db": h, "indexer": None,
                          "execs": [h] * n}, workdir=workdir)


def interrupt(exc: BaseException, partial: str = ""):
    """Reply effect: the interrupt arrives while the call runs."""
    def effect(argv):
        if partial:
            exc.partial_output = partial
        raise exc
    return effect


# What tofu apply prints when EC2 has no capacity for a host.
ICE = """
Error: creating EC2 Instance: operation error EC2: RunInstances, https response error \
StatusCode: 500, RequestID: 0f1e, api error InsufficientInstanceCapacity: We currently do not \
have sufficient r8a.16xlarge capacity in the Availability Zone you requested (us-west-2b). Our \
system will be working on provisioning additional capacity. You can currently get r8a.16xlarge \
capacity by not specifying an Availability Zone in your request or choosing us-west-2a, us-west-2c.

  with aws_instance.host["runner"],
  on main.tf line 108, in resource "aws_instance" "host":
 108: resource "aws_instance" "host" {

Error: creating EC2 Instance: api error InsufficientInstanceCapacity: We currently do not have \
sufficient r8a.16xlarge capacity in the Availability Zone you requested (us-west-2b).

  with aws_instance.host["exec0"],
"""
WEST = ("region='us-west-2'", "availability_zone='us-west-2b'")

# What tofu apply -no-color prints on stdout while it creates hosts.
PROGRESS = """aws_instance.host["db"]: Creating...
aws_instance.host["event"]: Creating...
aws_instance.host["runner"]: Creating...
aws_instance.host["exec0"]: Creating...
aws_instance.host["db"]: Creation complete after 13s [id=i-0aaa]
aws_instance.host["event"]: Creation complete after 14s [id=i-0bbb]
aws_instance.host["runner"]: Still creating... [5m50s elapsed]
aws_instance.host["exec0"]: Still creating... [5m50s elapsed]
"""
