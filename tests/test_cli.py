import json
import signal

import pytest

from fakes import KEY, SCENARIO, Reply, install_cluster, make_archive
from whitney.cli import build_parser, main

COMMANDS = ["build", "run", "sweep", "summarize", "chart", "publish"]
CLUSTER = ["--ssh-key-name", "me", "--operator-cidr", "198.51.100.7/32"]


def test_subcommand_help(capsys):
    # A stray % in a help string makes --help crash.
    for cmd in COMMANDS:
        with pytest.raises(SystemExit) as e:
            build_parser().parse_args([cmd, "--help"])
        assert e.value.code == 0
        assert "usage" in capsys.readouterr().out, cmd


def test_run_requires_n(capsys):
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["run", "s.toml", "--bucket", "b"])
    assert e.value.code == 2
    assert "--n" in capsys.readouterr().err


def test_parses_args():
    a = build_parser().parse_args(["run", "s.toml", "--n", "3", "--bucket", "b", "--set", "a=1",
                                   "--keep", *CLUSTER])
    assert (a.n, a.bucket, a.overrides, a.keep) == (3, "b", ["a=1"], True)
    assert (a.arch, a.archive, a.ssh_identity, a.runner_timeout) == ("znver5", None, None, 7200)
    assert a.aws_creds == "process"
    assert (a.ssh_key_name, a.operator_cidr) == ("me", "198.51.100.7/32")
    a = build_parser().parse_args(["build", "--hogd-ref", "main"])
    assert (a.arch, a.out) == ("znver5", ".whitney-cache")


@pytest.mark.parametrize("cmd", [["run", "s.toml", "--n", "1"], ["sweep", "s.toml"]])
def test_cluster_flags_required(cmd, capsys):
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args([*cmd, "--bucket", "b"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--ssh-key-name" in err and "--operator-cidr" in err


def test_operator_cidr_validated(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "s.toml", "--n", "1", "--bucket", "b",
                                   "--ssh-key-name", "me", "--operator-cidr", "my-laptop"])
    assert "--operator-cidr" in capsys.readouterr().err


def test_main_run(tmp_path, monkeypatch, fx, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.toml").write_text(SCENARIO)
    archive = make_archive(tmp_path / "prebuilt", KEY)
    install_cluster(fx, 2)
    rc = main(["run", "s.toml", "--n", "2", "--bucket", "bkt", *CLUSTER,
               "--archive", str(archive), "--set", "accounts=10"], ex=fx)
    assert rc == 0
    [d] = (tmp_path / "results").iterdir()
    assert json.loads((d / "status.json").read_text())["status"] == "ok"
    assert "--set accounts=10" in (d / "command.txt").read_text()
    assert capsys.readouterr().out == f"ok results/{d.name}\n"


def test_main_run_bad_scenario(tmp_path, fx, capsys):
    (tmp_path / "s.toml").write_text('name="x"')
    rc = main(["run", str(tmp_path / "s.toml"), "--n", "1", "--bucket", "b", *CLUSTER], ex=fx)
    assert rc == 2 and "missing required scenario key" in capsys.readouterr().err
    assert not fx.calls


def test_main_build(fx, capsys):
    fx.on("build.sh", Reply(stdout="log line\n/out/whitney-v1-abc-znver5.tar.gz\n"))
    assert main(["build", "--hogd-ref", "v1", "--out", "/out"], ex=fx) == 0
    argv = fx.calls[0].argv
    assert argv[1:] == ["--hogd-ref", "v1", "--arch", "znver5", "--out", "/out"]
    assert argv[0].endswith("build/build.sh")
    assert capsys.readouterr().out == "/out/whitney-v1-abc-znver5.tar.gz\n"


def test_main_build_failure(fx, capsys):
    fx.on("build.sh", Reply(rc=3, stderr="docker not found"))
    assert main(["build", "--hogd-ref", "v1"], ex=fx) == 1
    cap = capsys.readouterr()
    assert "build failed" in cap.err and "docker not found" in cap.err
    assert cap.out == ""


def _tfvars(root):
    [f] = (root / ".whitney-cache" / "clusters").glob("*/whitney.auto.tfvars.json")
    return json.loads(f.read_text())


@pytest.mark.parametrize("cmd,extra,minutes", [
    (["run", "s.toml", "--n", "1"], [], 8 * 60),
    (["run", "s.toml", "--n", "1"], ["--max-hours", "2.5"], 150),
    (["sweep", "s.toml", "--seed", "1"], [], 24 * 60),
])
def test_max_hours_sets_host_lifetime(tmp_path, monkeypatch, fx, cmd, extra, minutes):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.toml").write_text(SCENARIO.replace("n_list = [1, 2, 3]", "n_list = [1]"))
    archive = make_archive(tmp_path / "prebuilt", KEY)
    install_cluster(fx, 1)
    assert main([*cmd, "--bucket", "bkt", *CLUSTER, "--archive", str(archive), *extra], ex=fx) == 0
    assert _tfvars(tmp_path)["max_lifetime_minutes"] == minutes


@pytest.mark.parametrize("bad", ["0", "x", "inf"])
def test_max_hours_must_be_positive(bad, capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "s.toml", "--n", "1", "--bucket", "b", *CLUSTER,
                                   "--max-hours", bad])
    assert "argument --max-hours: must be a positive number of hours" in capsys.readouterr().err


# ---- signals and cleanup ------------------------------------------------------


def _send(sig):
    """FakeExec effect: deliver sig the way the OS would, through the installed handler."""
    def effect(argv):
        signal.getsignal(sig)(sig, None)
    return effect


def _setup_run(tmp_path, monkeypatch, fx, extra=""):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.toml").write_text(SCENARIO.replace("n_list = [1, 2, 3]", "n_list = [1]") + extra)
    archive = make_archive(tmp_path / "prebuilt", KEY)
    install_cluster(fx, 1)
    return ["--bucket", "bkt", *CLUSTER, "--archive", str(archive)]


def test_sigterm_during_run_destroys_and_restores_handlers(tmp_path, monkeypatch, fx, capsys):
    args = _setup_run(tmp_path, monkeypatch, fx)
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    fx.on("whitney-runner", Reply(effect=_send(signal.SIGTERM)))
    assert main(["run", "s.toml", "--n", "1", *args], ex=fx) == 130
    [runner], [destroy] = fx.find("whitney-runner"), fx.find("tofu destroy")
    assert runner < destroy
    [d] = (tmp_path / "results").iterdir()
    st = json.loads((d / "status.json").read_text())
    assert st["status"] == "failed" and "received SIGTERM" in st["error"]
    assert "interrupted (received SIGTERM)" in capsys.readouterr().err
    assert {s: signal.getsignal(s) for s in before} == before


def test_sighup_during_sweep_destroys(tmp_path, monkeypatch, fx, capsys):
    args = _setup_run(tmp_path, monkeypatch, fx, "repetitions = 2\n")
    fx.on("whitney-runner", Reply(effect=_send(signal.SIGHUP)))
    assert main(["sweep", "s.toml", "--seed", "1", *args], ex=fx) == 130
    assert len(fx.find("tofu apply")) == 1 and len(fx.find("tofu destroy")) == 1
    [sweep_dir] = (tmp_path / "results").iterdir()
    assert json.loads((sweep_dir / "sweep.json").read_text())["seed"] == 1
    assert "interrupted (received SIGHUP)" in capsys.readouterr().err


def test_second_signal_does_not_interrupt_destroy(tmp_path, monkeypatch, fx, capsys):
    args = _setup_run(tmp_path, monkeypatch, fx)
    fx.on("whitney-runner", Reply(effect=_send(signal.SIGTERM)))
    fx.on("tofu destroy", Reply(effect=_send(signal.SIGHUP)))
    assert main(["run", "s.toml", "--n", "1", *args], ex=fx) == 130
    err = capsys.readouterr().err
    assert "SIGHUP ignored: cleanup is running" in err
    assert "destroy interrupted" not in err


def test_interrupted_destroy_prints_absolute_hint(tmp_path, monkeypatch, fx, capsys):
    args = _setup_run(tmp_path, monkeypatch, fx)

    def ctrl_c(argv):
        raise KeyboardInterrupt
    fx.on("tofu destroy", Reply(effect=ctrl_c))
    assert main(["run", "s.toml", "--n", "1", *args], ex=fx) == 130
    err = capsys.readouterr().err
    wd = (tmp_path / ".whitney-cache" / "clusters").resolve()
    assert "destroy interrupted; the cluster may still be running. Destroy it with: env -u " in err
    assert f"AWS_PROFILE=whitney AWS_SDK_LOAD_CONFIG=1 tofu -chdir={wd}/" in err


def test_failed_destroy_hint_is_absolute(tmp_path, monkeypatch, fx, capsys):
    args = _setup_run(tmp_path, monkeypatch, fx)
    fx.on("tofu destroy", Reply(rc=1, stderr="Error: ExpiredToken"))
    assert main(["run", "s.toml", "--n", "1", *args], ex=fx) == 1
    wd = (tmp_path / ".whitney-cache" / "clusters").resolve()
    err = capsys.readouterr().err
    assert "Retry with: env -u " in err and f"AWS_PROFILE=whitney AWS_SDK_LOAD_CONFIG=1 tofu -chdir={wd}/" in err


def test_keep_hint_is_absolute(tmp_path, monkeypatch, fx, capsys):
    args = _setup_run(tmp_path, monkeypatch, fx)
    monkeypatch.delenv("AWS_CREDENTIAL_EXPIRATION", raising=False)
    assert main(["run", "s.toml", "--n", "1", "--keep", "--aws-creds", "env", *args], ex=fx) == 0
    wd = (tmp_path / ".whitney-cache" / "clusters").resolve()
    assert f"Destroy it with: tofu -chdir={wd}/" in capsys.readouterr().err  # env mode: no prefix
    assert not fx.find("tofu destroy")


def test_aws_creds_flag(tmp_path, monkeypatch, fx):
    args = _setup_run(tmp_path, monkeypatch, fx)
    monkeypatch.delenv("AWS_CREDENTIAL_EXPIRATION", raising=False)
    assert main(["run", "s.toml", "--n", "1", "--aws-creds", "env", *args], ex=fx) == 0
    assert not fx.find("export-credentials")
    assert all(not c.env for c in fx.calls if c.argv[0] == "tofu")


def test_credential_failure_exits_before_any_spend(tmp_path, monkeypatch, fx, capsys):
    args = _setup_run(tmp_path, monkeypatch, fx)
    monkeypatch.setenv("AWS_CREDENTIAL_EXPIRATION", "2000-01-01T00:00:00Z")
    rc = main(["sweep", "s.toml", "--aws-creds", "env", *args], ex=fx)
    assert rc == 1
    assert "AWS credentials expire in 0 min, before this run's max lifetime (1440 min)" \
        in capsys.readouterr().err
    assert not fx.calls and not (tmp_path / "results").exists()


def test_aws_creds_choices(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "s.toml", "--n", "1", "--bucket", "b", *CLUSTER,
                                   "--aws-creds", "sso"])
    assert "--aws-creds" in capsys.readouterr().err


def test_instance_create_timeout_flag(tmp_path, monkeypatch, fx):
    args = _setup_run(tmp_path, monkeypatch, fx)
    a = build_parser().parse_args(["sweep", "s.toml", *args])
    assert a.instance_create_timeout == "6m"
    assert main(["run", "s.toml", "--n", "1", "--instance-create-timeout", "1h30m", *args],
                ex=fx) == 0
    assert _tfvars(tmp_path)["instance_create_timeout"] == "1h30m"


@pytest.mark.parametrize("bad", ["6", "0m", "soon"])
def test_instance_create_timeout_validated(bad, capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "s.toml", "--n", "1", "--bucket", "b", *CLUSTER,
                                   f"--instance-create-timeout={bad}"])
    assert "argument --instance-create-timeout: must be a duration" in capsys.readouterr().err


def test_apply_timeout_flag(tmp_path, monkeypatch, fx):
    args = _setup_run(tmp_path, monkeypatch, fx)
    assert build_parser().parse_args(["sweep", "s.toml", *args]).apply_timeout is None
    assert main(["run", "s.toml", "--n", "1", "--apply-timeout", "20m", *args], ex=fx) == 0
    [apply] = fx.find("tofu apply")
    assert fx.calls[apply].timeout == 1200


def test_apply_timeout_validated(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "s.toml", "--n", "1", "--bucket", "b", *CLUSTER,
                                   "--apply-timeout=0m"])
    assert "argument --apply-timeout: must be a duration" in capsys.readouterr().err


CORES_SCENARIO = SCENARIO.replace("n_list = [1, 2, 3]", "n_list = [1]") + \
    "exec_cores = [1, 2, 4]\ntxs_per_block_per_core = 100000\n"


def test_cores_must_be_positive(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "s.toml", "--n", "1", "--cores", "0", "--bucket", "b",
                                   *CLUSTER])
    assert "--cores" in capsys.readouterr().err


def test_main_run_with_cores(tmp_path, monkeypatch, fx, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.toml").write_text(CORES_SCENARIO)
    archive = make_archive(tmp_path / "prebuilt", KEY)
    install_cluster(fx, 1)
    fx.on("cpuset.cpus.effective", Reply(stdout="11111 0-1 0\n"))
    rc = main(["run", "s.toml", "--n", "1", "--cores", "2", "--bucket", "bkt", *CLUSTER,
               "--archive", str(archive)], ex=fx)
    assert rc == 0
    [d] = (tmp_path / "results").iterdir()
    assert d.name.endswith("-n1-c2-r1")
    assert "--cores 2" in (d / "command.txt").read_text()
    assert json.loads((d / "exec-setup.json").read_text()) == \
        {"exec0": {"l3": None, "ports": {"11111": {"want": "0-1", "cpus": "0-1",
                                                   "allocator": "system"}}}}
