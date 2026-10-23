"""tofu apply and destroy: inputs, capacity errors, deadlines, and the orphan sweep."""
import json
from pathlib import Path

import pytest

from fakes import (ICE, PRESIGNED, PROGRESS, SIGNATURE, WEST, Reply, install_cluster, interrupt,
                   opts, status)
from whitney.config import ScenarioError
from whitney.infra import apply_cluster
from whitney.run import run_scenario

REPO = Path(__file__).resolve().parents[1]
STATUS_KEYS = {"status", "error", "started_at", "finished_at"}
# Opt-in local NVMe: db and event on x8aedz, so capacity errors name its zones.
X8AEDZ = ("storage='nvme'", "instance_types={db='x8aedz.12xlarge', event='x8aedz.12xlarge'}")


# ---- inputs ------------------------------------------------------------------


@pytest.mark.parametrize("ov", ["availability_zone=''", "ami_id=''"])
def test_apply_requires_az_and_ami(tmp_path, fx, scenario, ov):
    with pytest.raises(ScenarioError, match="availability_zone and ami_id must be set"):
        apply_cluster(scenario(ov), 1, PRESIGNED, "ab" * 32, tmp_path / "wd", fx,
                      ssh_key_name="me", operator_cidr="198.51.100.7/32",
                      max_lifetime_minutes=60)
    assert not fx.find("tofu")


def test_apply_writes_vars_file_not_argv(tmp_path, fx, scenario):
    install_cluster(fx, 2)
    wd = tmp_path / "wd"
    wd.mkdir()
    c = apply_cluster(scenario(), 2, PRESIGNED, "ab" * 32, wd, fx,
                      ssh_key_name="me", operator_cidr="198.51.100.7/32",
                      max_lifetime_minutes=90)
    tfvars = json.loads((wd / "whitney.auto.tfvars.json").read_text())
    assert tfvars["n_exec"] == 2 and tfvars["archive_url"] == PRESIGNED
    assert tfvars["max_lifetime_minutes"] == 90
    assert tfvars["availability_zone"] == "us-east-1a" and tfvars["cluster_name"] == "wd"
    assert tfvars["instance_types"]["exec"] == "r8a.16xlarge"
    assert (tfvars["storage"], tfvars["ebs_volume_type"], tfvars["ebs_volume_size_gib"],
            tfvars["ebs_iops"], tfvars["ebs_throughput_mibps"]) == ("tmpfs", "gp3", 200, 16000, 1000)
    assert (wd / "whitney.auto.tfvars.json").stat().st_mode & 0o077 == 0
    assert not any(SIGNATURE in c.line for c in fx.calls)
    assert len(c.roles["execs"]) == 2 and c.placement_group == "pg-1"


def test_tofu_calls_have_no_color(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    tofu = [c for c in fx.calls if c.argv[0] == "tofu"]
    assert len(tofu) == 4 and all("-no-color" in c.argv for c in tofu)


def test_provider_max_retries_in_tfvars(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    [apply] = fx.find("tofu apply")
    tfvars = json.loads((fx.calls[apply].cwd / "whitney.auto.tfvars.json").read_text())
    assert tfvars["max_retries"] == 5
    versions = (REPO / "infra" / "versions.tf").read_text()
    assert "max_retries = var.max_retries" in versions


@pytest.mark.parametrize("kw,seconds", [({}, 600), ({"instance_create_timeout": "8m"}, 720),
                                        ({"apply_timeout": "1h"}, 3600)])
def test_apply_deadline(tmp_path, fx, scenario, kw, seconds):
    install_cluster(fx, 1)
    run_scenario(scenario(), 1, opts(tmp_path, **kw), fx, command=["whitney"])
    [apply] = fx.find("tofu apply")
    assert fx.calls[apply].timeout == seconds
    assert all(c.timeout is None for c in fx.calls if "tofu apply" not in c.line)


# ---- capacity ----------------------------------------------------------------


def test_capacity_error_names_type_and_zone(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(rc=1, stderr=ICE))
    r = run_scenario(scenario(*WEST, *X8AEDZ), 1, opts(tmp_path), fx, command=["whitney"])
    err = status(r.run_dir)["error"]
    assert err.startswith("capacity: EC2 has no r8a.16xlarge capacity in us-west-2b "
                          "(InsufficientInstanceCapacity)")
    assert "AWS suggests us-west-2a, us-west-2c" in err
    assert "x8aedz in us-west-2: us-west-2a" in err  # the other zone with x8aedz
    assert "--instance-create-timeout" not in err
    [apply], [destroy] = fx.find("tofu apply"), fx.find("tofu destroy")
    assert apply < destroy
    assert not fx.find("whitney-runner")


def test_capacity_hosts_come_from_diagnostics_only(tmp_path, fx, scenario):
    """Progress lines name every host; only the diagnostic `with` lines name the failed one."""
    install_cluster(fx, 1)
    err = ('\nError: creating EC2 Instance: api error InsufficientInstanceCapacity: no capacity\n'
           '\n  with aws_instance.host["runner"],\n')
    fx.on("tofu apply", Reply(rc=1, stdout=PROGRESS, stderr=err))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    error = status(r.run_dir)["error"]
    # No type or zone in the message: the failing host's type and the scenario zone.
    assert error.startswith("capacity: EC2 has no r8a.16xlarge capacity in us-east-1a")
    assert "x8aedz" not in error


def test_hung_apply_hits_the_deadline_and_destroys(tmp_path, fx, scenario, monkeypatch):
    clock = iter([100.0, 730.0])  # around the apply call: 10.5 min
    monkeypatch.setattr("whitney.infra.monotonic", lambda: next(clock))
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(stdout=PROGRESS, hang=True))
    r = run_scenario(scenario(*WEST, *X8AEDZ), 1, opts(tmp_path), fx, command=["whitney"])
    [apply], [destroy] = fx.find("tofu apply"), fx.find("tofu destroy")
    assert fx.calls[apply].timeout == 600  # 6m create timeout + 4m
    assert apply < destroy
    error = status(r.run_dir)["error"]
    assert error.startswith("capacity or launch timeout: tofu apply ran past its 10m deadline "
                            "and was stopped after 10.5m (most often "
                            "InsufficientInstanceCapacity for r8a.16xlarge (runner, exec0))")
    assert "x8aedz in us-west-2: us-west-2a" in error
    assert not fx.find("whitney-runner")


def test_hung_apply_with_capacity_code_says_capacity(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(stdout=PROGRESS, stderr=ICE, hang=True))
    r = run_scenario(scenario(*WEST), 1, opts(tmp_path), fx, command=["whitney"])
    assert status(r.run_dir)["error"].startswith(
        "capacity: EC2 has no r8a.16xlarge capacity in us-west-2b")


def test_canceled_run_instances_reads_as_capacity(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    out = ('Error: creating EC2 Instance: operation error EC2: RunInstances, request canceled, '
           'context deadline exceeded\n\n  with aws_instance.host["exec0"],\n')
    fx.on("tofu apply", Reply(rc=1, stdout=PROGRESS, stderr=out))
    r = run_scenario(scenario(*WEST), 1, opts(tmp_path, instance_create_timeout="4m"), fx,
                     command=["whitney"])
    error = status(r.run_dir)["error"]
    assert error.startswith("capacity or launch timeout: RunInstances for r8a.16xlarge (exec0) "
                            "did not finish within 4m (most often InsufficientInstanceCapacity)")
    assert fx.find("tofu destroy")


def test_slow_launch_is_not_capacity(tmp_path, fx, scenario):
    """RunInstances worked; the instance did not reach running in time."""
    install_cluster(fx, 1)
    out = ("Error: waiting for EC2 Instance (i-0ccc) create: timeout while waiting for state to "
           "become 'running' (last state: 'pending', timeout: 6m0s)\n\n"
           '  with aws_instance.host["exec0"],\n')
    fx.on("tofu apply", Reply(rc=1, stdout=PROGRESS, stderr=out))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    error = status(r.run_dir)["error"]
    assert error == ("launch timeout: instance exec0 (r8a.16xlarge) did not reach running within "
                     "6m (--instance-create-timeout)")


def test_other_apply_errors_are_not_capacity(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(rc=1, stderr="Error: VcpuLimitExceeded\n"))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    err = status(r.run_dir)["error"]
    assert not err.startswith("capacity:") and "VcpuLimitExceeded" in err


# ---- orphaned instances (tag sweep after destroy) ----------------------------


ORPHAN = Reply(stdout=json.dumps([["i-0orphan", "wd-runner"]]) + "\n")


def _describe_calls(fx):
    return fx.find("aws ec2 describe-instances")


def test_deadline_then_orphan_is_terminated(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(stdout=PROGRESS, hang=True))
    fx.on("aws ec2 describe-instances", ORPHAN)
    r = run_scenario(scenario(*WEST), 1, opts(tmp_path), fx, command=["whitney"])
    [destroy], [describe], [term] = (fx.find("tofu destroy"), _describe_calls(fx),
                                      fx.find("aws ec2 terminate-instances"))
    assert destroy < describe < term
    wd = fx.calls[destroy].cwd.name
    d = fx.calls[describe]
    assert d.env is None  # the caller's env, not tofu's
    assert d.argv[3:8] == ["--region", "us-west-2", "--filters", f"Name=tag:whitney,Values={wd}",
                           "Name=instance-state-name,Values=pending,running,stopping,stopped"]
    assert fx.calls[term].argv == ["aws", "ec2", "terminate-instances", "--region", "us-west-2",
                                   "--instance-ids", "i-0orphan"]
    st = status(r.run_dir)
    assert st["error"].startswith("capacity or launch timeout:")
    assert st["cleanup"] == ["orphaned instances terminated: i-0orphan (wd-runner)"]


def test_no_orphans_no_terminate(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    [destroy], [describe] = fx.find("tofu destroy"), _describe_calls(fx)
    assert destroy < describe
    assert not fx.find("terminate-instances")
    assert set(status(r.run_dir)) == STATUS_KEYS  # no cleanup entry


def test_keep_skips_the_tag_sweep(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    run_scenario(scenario(), 1, opts(tmp_path, keep=True), fx, command=["whitney"])
    assert not _describe_calls(fx)


def test_orphan_check_failure_prints_manual_command(tmp_path, fx, scenario, capsys):
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(rc=1, stderr=ICE))
    fx.on("aws ec2 describe-instances", Reply(rc=255, stderr="ExpiredToken"))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    st = status(r.run_dir)
    assert st["error"].startswith("capacity: ")  # the original error stays
    assert st["cleanup"][0].startswith("orphan check failed: ")
    err = capsys.readouterr().err
    wd = fx.calls[fx.find("tofu destroy")[0]].cwd.name
    assert ("aws ec2 describe-instances --region us-east-1 --filters "
            f"Name=tag:whitney,Values={wd} "
            "Name=instance-state-name,Values=pending,running,stopping,stopped") in err
    assert "aws ec2 terminate-instances --region us-east-1 --instance-ids <ids>" in err
    assert not fx.find("terminate-instances")


def test_terminate_failure_prints_manual_command(tmp_path, fx, scenario, capsys):
    install_cluster(fx, 1)
    fx.on("aws ec2 describe-instances", ORPHAN)
    fx.on("aws ec2 terminate-instances", Reply(rc=1, stderr="UnauthorizedOperation"))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    assert r.status == "ok"
    assert status(r.run_dir)["cleanup"][0].startswith(
        "orphaned instances NOT terminated: i-0orphan (wd-runner)")
    assert "aws ec2 terminate-instances --region us-east-1 --instance-ids i-0orphan" in \
        capsys.readouterr().err


def test_orphan_check_after_destroy_failure(tmp_path, fx, scenario):
    from whitney.infra import DestroyError
    install_cluster(fx, 1)
    fx.on("tofu destroy", Reply(rc=1, stderr="Error: ExpiredToken"))
    fx.on("aws ec2 describe-instances", ORPHAN)
    with pytest.raises(DestroyError) as e:
        run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    assert "orphaned instances terminated: i-0orphan (wd-runner)" in str(e.value)
    assert "Retry with:" in str(e.value)
    [d] = (tmp_path / "results").iterdir()
    assert status(d)["cleanup"] == ["orphaned instances terminated: i-0orphan (wd-runner)"]


def test_interrupted_run_still_sweeps_tags(tmp_path, fx, scenario):
    from whitney.cli import Terminated
    install_cluster(fx, 1)
    fx.on("whitney-runner", Reply(effect=interrupt(Terminated("received SIGTERM"))))
    fx.on("aws ec2 describe-instances", Reply(rc=255, stderr="network down"))
    with pytest.raises(Terminated):  # a failed check never replaces the interrupt
        run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    [destroy], [describe] = fx.find("tofu destroy"), _describe_calls(fx)
    assert destroy < describe
    [d] = (tmp_path / "results").iterdir()
    st = status(d)
    assert st["error"].startswith("interrupted: received SIGTERM")
    assert st["cleanup"][0].startswith("orphan check failed: ")
