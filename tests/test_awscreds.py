"""AWS credentials: the credential_process profile for tofu and the upfront check."""
import json
import os
import shlex
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fakes import Reply, install_cluster, opts
from whitney.awscreds import (STATIC_VARS, AwsCredsError, caller_name,
                              credential_process_command, prepare_aws, write_aws_config)
from whitney.run import run_scenario

AWS = "/opt/fake/bin/aws"
NOW = datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)


def _tofu_calls(fx):
    return [c for c in fx.calls if c.argv[0] == "tofu"]


# ---- credential_process command ----------------------------------------------------


def test_command_makes_a_relative_config_path_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = credential_process_command(AWS, {"AWS_CONFIG_FILE": "cfg"})
    assert f"AWS_CONFIG_FILE={tmp_path.resolve() / 'cfg'}" in shlex.split(cmd)


def test_command_rejects_newlines():
    with pytest.raises(AwsCredsError, match="newline"):
        credential_process_command(AWS, {"AWS_PROFILE": "a\nb"})


def _fake_aws(d: Path) -> Path:
    """An `aws` that prints the selector vars it got and its args."""
    d.mkdir(parents=True)
    aws = d / "aws"
    aws.write_text('#!/bin/sh\n'
                   'echo "cfg=${AWS_CONFIG_FILE-unset}"\n'
                   'echo "profile=${AWS_PROFILE-unset}"\n'
                   'echo "default=${AWS_DEFAULT_PROFILE-unset}"\n'
                   'echo "shared=${AWS_SHARED_CREDENTIALS_FILE-unset}"\n'
                   'for a in "$@"; do echo "arg=$a"; done\n')
    aws.chmod(0o755)
    return aws


@pytest.mark.parametrize("caller", [
    {},
    {"AWS_CONFIG_FILE": "/home/me/it's here/config", "AWS_PROFILE": "dev $(x)",
     "AWS_SHARED_CREDENTIALS_FILE": "/home/me/c d/credentials"},
])
def test_command_runs_as_intended_in_sh_and_without_a_shell(tmp_path, caller):
    """The Go SDK runs credential_process with sh -c; the aws CLI splits it like
    shlex. Both must give the caller's selectors back, never whitney's file."""
    aws = _fake_aws(tmp_path / "bin dir" / "it's")
    cmd = credential_process_command(str(aws), caller)
    tofu = {**os.environ, "AWS_CONFIG_FILE": str(tmp_path / "whitney.config"),
            "AWS_PROFILE": "whitney", "AWS_DEFAULT_PROFILE": "whitney",
            "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "empty")}
    want = (f"cfg={caller.get('AWS_CONFIG_FILE', 'unset')}\n"
            f"profile={caller.get('AWS_PROFILE', 'unset')}\n"
            "default=unset\n"
            f"shared={caller.get('AWS_SHARED_CREDENTIALS_FILE', 'unset')}\n"
            "arg=configure\narg=export-credentials\narg=--format\narg=process\n")
    via_sh = subprocess.run(["/bin/sh", "-c", cmd], env=tofu, capture_output=True, text=True,
                            check=True)
    assert via_sh.stdout == want
    split = subprocess.run(shlex.split(cmd), env=tofu, capture_output=True, text=True, check=True)
    assert split.stdout == want


# ---- config file -------------------------------------------------------------------


def test_config_file(tmp_path):
    cmd = credential_process_command(AWS, {})
    path = write_aws_config(tmp_path / "cache", "us-west-2", cmd)
    assert path.is_absolute() and path.parent == (tmp_path / "cache" / "aws").resolve()
    assert path.read_text() == ("[profile whitney]\nregion = us-west-2\n"
                                f"credential_process = {cmd}\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert write_aws_config(tmp_path / "cache", "us-west-2", cmd) == path  # same content, same file
    assert write_aws_config(tmp_path / "cache", "us-east-1", cmd) != path


# ---- caller name ---------------------------------------------------------------------


@pytest.mark.parametrize("arn,name", [
    ("arn:aws:sts::123456789012:assumed-role/WhitneyOps/alice@example.com", "assumed-role/WhitneyOps"),
    ("arn:aws:iam::123456789012:user/bench/alice", "user/bench/alice"),
    ("arn:aws:iam::123456789012:root", "root"),
    ("not an arn", "unknown"),
])
def test_caller_name_drops_account_and_session(arn, name):
    assert caller_name(arn) == name


# ---- upfront check: process mode ---------------------------------------------------------


def test_process_mode_checks_and_returns_tofu_env(tmp_path, fx):
    creds = prepare_aws("process", "us-west-2", tmp_path, fx, max_lifetime_minutes=480,
                        environ={"AWS_ACCESS_KEY_ID": "x"}, aws=AWS)
    [check] = fx.find("export-credentials")
    assert fx.calls[check].argv[:2] == ["/bin/sh", "-c"]
    assert fx.calls[check].secret and fx.calls[check].env == creds.tofu_env
    [sts] = fx.find("sts get-caller-identity")
    assert fx.calls[sts].env == creds.tofu_env and fx.calls[sts].secret
    env = creds.tofu_env
    assert env["AWS_PROFILE"] == "whitney" and env["AWS_SDK_LOAD_CONFIG"] == "1"
    assert Path(env["AWS_CONFIG_FILE"]).read_text().startswith("[profile whitney]\n")
    assert all(env[v] is None for v in STATIC_VARS) and env["AWS_DEFAULT_PROFILE"] is None
    # A [whitney] profile in ~/.aws/credentials must not win over the process profile.
    shared = Path(env["AWS_SHARED_CREDENTIALS_FILE"])
    assert shared.read_text() == "" and stat.S_IMODE(shared.stat().st_mode) == 0o600
    assert shared.parent == (tmp_path / "aws").resolve()
    assert creds.record() == {"mode": "process", "caller": "assumed-role/WhitneyOps"}


@pytest.mark.parametrize("reply,msg", [
    (Reply(rc=255, stderr="Error when retrieving token from sso: Token has expired\n"),
     "credential_process command failed \\(exit 255\\).*Token has expired"),
    (Reply(stdout="not json"), "did not print JSON"),
    (Reply(stdout=json.dumps({"Version": 1, "AccessKeyId": "AKIAEXAMPLE"})),
     "lacks SecretAccessKey"),
])
def test_process_mode_failures(tmp_path, fx, reply, msg):
    fx.on("export-credentials", reply)
    with pytest.raises(AwsCredsError, match=msg) as e:
        prepare_aws("process", "us-west-2", tmp_path, fx, max_lifetime_minutes=60, aws=AWS)
    assert "AKIAEXAMPLE" not in str(e.value)
    assert not fx.find("sts")


def test_profile_that_does_not_work_fails(tmp_path, fx):
    fx.on("sts get-caller-identity", Reply(rc=254, stderr="Unable to locate credentials\n"))
    with pytest.raises(AwsCredsError, match="get-caller-identity exited 254.*Unable to locate"):
        prepare_aws("process", "us-west-2", tmp_path, fx, max_lifetime_minutes=60, aws=AWS)


def test_process_mode_needs_the_aws_cli(tmp_path, fx, monkeypatch):
    monkeypatch.setattr("whitney.awscreds.find_aws", lambda: None)
    with pytest.raises(AwsCredsError, match="aws CLI not found"):
        prepare_aws("process", "us-west-2", tmp_path, fx, max_lifetime_minutes=60)
    assert not fx.calls


# ---- upfront check: env mode ---------------------------------------------------------------


def _expiring(minutes: int) -> dict:
    t = NOW + timedelta(minutes=minutes)
    return {"AWS_CREDENTIAL_EXPIRATION": t.strftime("%Y-%m-%dT%H:%M:%SZ")}


def test_env_mode_needs_a_30_minute_margin(tmp_path, fx):
    with pytest.raises(AwsCredsError, match="expire in 509 min"):
        prepare_aws("env", "us-west-2", tmp_path, fx, max_lifetime_minutes=480,
                    environ=_expiring(509), now=NOW)
    creds = prepare_aws("env", "us-west-2", tmp_path, fx, max_lifetime_minutes=480,
                        environ=_expiring(510), now=NOW)
    assert creds.tofu_env == {} and creds.record()["mode"] == "env"
    [sts] = fx.find("sts get-caller-identity")
    assert fx.calls[sts].env in (None, {})  # the caller's own env
    assert not fx.find("export-credentials")
    assert not (tmp_path / "aws").exists()


def test_env_mode_without_expiry_is_ok(tmp_path, fx):
    creds = prepare_aws("env", "us-west-2", tmp_path, fx, max_lifetime_minutes=480,
                        environ={}, now=NOW)
    assert creds.caller == "assumed-role/WhitneyOps"


def test_env_mode_bad_expiry_fails(tmp_path, fx):
    with pytest.raises(AwsCredsError, match="AWS_CREDENTIAL_EXPIRATION"):
        prepare_aws("env", "us-west-2", tmp_path, fx, max_lifetime_minutes=60,
                    environ={"AWS_CREDENTIAL_EXPIRATION": "soon"}, now=NOW)


# ---- through whitney run and sweep -------------------------------------------------------------


def test_tofu_gets_the_profile_and_aws_cli_calls_do_not(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    assert r.status == "ok"
    tofu = _tofu_calls(fx)
    assert {c.argv[1] for c in tofu} == {"init", "apply", "output", "destroy"}
    for c in tofu:
        assert c.env["AWS_PROFILE"] == "whitney" and c.env["AWS_SDK_LOAD_CONFIG"] == "1"
        assert Path(c.env["AWS_CONFIG_FILE"]).is_file()
        assert all(c.env[v] is None for v in STATIC_VARS)
    s3 = [c for c in fx.calls if c.argv[:2] == ["aws", "s3"]]
    assert len(s3) == 2 and all(c.env is None for c in s3)
    aws = json.loads((r.run_dir / "aws.json").read_text())
    assert aws == {"mode": "process", "caller": "assumed-role/WhitneyOps"}
    assert "123456789012" not in (r.run_dir / "aws.json").read_text()


def test_check_runs_before_build_upload_and_apply(tmp_path, fx, scenario):
    fx.on("export-credentials", Reply(rc=1, stderr="no login session\n"))
    with pytest.raises(AwsCredsError, match="no login session"):
        run_scenario(scenario(), 1, opts(tmp_path, archive=None), fx, command=["whitney"])
    assert [c.argv[0] for c in fx.calls] == ["/bin/sh"]
    assert not (tmp_path / "results").exists()


def test_sweep_tofu_calls_get_the_profile(tmp_path, fx, scenario):
    from whitney.sweep import sweep
    install_cluster(fx, 1)
    results = sweep(scenario("n_list=[1]", "repetitions=2"), opts(tmp_path), fx, seed=1)
    assert [r.status for r in results] == ["ok", "ok"]
    assert len(fx.find("export-credentials")) == 1
    assert all(c.env["AWS_PROFILE"] == "whitney" for c in _tofu_calls(fx))
    assert all(json.loads((r.run_dir / "aws.json").read_text())["mode"] == "process"
               for r in results)


def test_env_mode_leaves_tofu_env_alone(tmp_path, fx, scenario, monkeypatch):
    monkeypatch.delenv("AWS_CREDENTIAL_EXPIRATION", raising=False)
    install_cluster(fx, 1)
    r = run_scenario(scenario(), 1, opts(tmp_path, aws_creds="env"), fx, command=["whitney"])
    assert r.status == "ok"
    assert all(not c.env for c in _tofu_calls(fx))
    assert not fx.find("export-credentials")
    assert json.loads((r.run_dir / "aws.json").read_text())["mode"] == "env"


def test_manual_destroy_hint_carries_the_profile(tmp_path, fx, scenario, capsys):
    install_cluster(fx, 1)
    run_scenario(scenario(), 1, opts(tmp_path, keep=True), fx, command=["whitney"])
    err = capsys.readouterr().err
    [line] = [x for x in err.splitlines() if "Destroy it with:" in x]
    hint = shlex.split(line.split("Destroy it with: ", 1)[1])
    assert hint[0] == "env" and "-u" in hint and "AWS_ACCESS_KEY_ID" in hint
    assert "AWS_PROFILE=whitney" in hint
    assert any(a.startswith("AWS_CONFIG_FILE=/") for a in hint)
    wd = (tmp_path / "cache" / "clusters").resolve()
    assert hint[-3] == "tofu" and hint[-2].startswith(f"-chdir={wd}/") and hint[-1] == "destroy"


def test_callers_config_inside_whitney_cache_is_refused(tmp_path, fx):
    cfg = tmp_path / "aws" / "config-0123456789ab"
    with pytest.raises(AwsCredsError, match="AWS_CONFIG_FILE points into whitney's own"):
        prepare_aws("process", "us-west-2", tmp_path, fx, max_lifetime_minutes=60,
                    environ={"AWS_CONFIG_FILE": str(cfg)}, aws=AWS)
    assert not fx.calls
