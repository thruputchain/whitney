import hashlib
import json
import re
from pathlib import Path

import pytest

from fakes import (PRESIGNED, SIGNATURE, SINGLE, Reply, cluster, install_cluster, interrupt, opts,
                   roles_output, single_cluster, status)
from whitney.config import ScenarioError, load_scenario
from whitney.infra import Cluster
from whitney.proc import redact
from whitney.run import roles_json, run_once, run_scenario

STATUS_KEYS = {"status", "error", "started_at", "finished_at"}


def files_containing(root: Path, needle: str) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and needle in p.read_text(errors="replace")]


# ---- roles.json --------------------------------------------------------------


def test_roles_json_first_n_execs(tmp_path, scenario):
    c = Cluster(roles=roles_output(10), workdir=tmp_path)
    r = json.loads(roles_json(c, 3, scenario()))
    assert set(r) == {"bin_dir", "runner_bind_ip", "event", "db", "indexer", "execs"}
    assert r["bin_dir"] == "/opt/whitney/bin" and r["runner_bind_ip"] == "10.42.1.10"
    assert [e["host"] for e in r["execs"]] == ["10.42.1.110", "10.42.1.111", "10.42.1.112"]
    assert r["execs"][2] == {"host": "10.42.1.112", "control_port": 11111, "metrics_port": 9104,
                             "work_dir": "/mnt/whitney/work/exec2", "cpu_affinity": ""}
    assert r["db"]["work_dir"] == "/mnt/whitney/work/db" and r["indexer"] is None
    # Multi-host: fixed ports per role, the same as infra/main.tf gives Vector.
    assert [r["event"]["metrics_port"], r["db"]["metrics_port"]] == [9101, 9102]
    assert {e["metrics_port"] for e in r["execs"]} == {9104}


def test_roles_json_guards(tmp_path, scenario):
    c = Cluster(roles=roles_output(2), workdir=tmp_path)
    with pytest.raises(ValueError, match="n=3 but the cluster has 2 execs"):
        roles_json(c, 3, scenario())
    with pytest.raises(ValueError, match="indexer"):
        roles_json(c, 1, scenario("indexer=true"))
    bad = roles_output(1)
    bad["db"]["private_ip"] = "db.internal"
    with pytest.raises(ValueError, match="IP literal"):
        roles_json(Cluster(roles=bad, workdir=tmp_path), 1, scenario())


def test_roles_json_with_indexer(tmp_path, scenario):
    c = Cluster(roles=roles_output(1, indexer=True), workdir=tmp_path)
    r = json.loads(roles_json(c, 1, scenario("indexer=true")))
    assert r["indexer"]["host"] == "10.42.1.13"
    assert r["indexer"]["work_dir"] == "/mnt/whitney/work/indexer"
    assert r["indexer"]["metrics_port"] == 9103


# ---- one run -----------------------------------------------------------------


def test_run_writes_all_artifacts(tmp_path, fx, scenario):
    install_cluster(fx, 3)
    s, o = scenario(), opts(tmp_path)
    r = run_scenario(s, 3, o, fx, command=["whitney", "run", "s.toml", "--n", "3"])
    d = r.run_dir
    assert r.status == "ok" and r.error is None
    assert re.fullmatch(r"\d{8}-\d{4}-exec-scaling-v0\.1\.31-n3-r1", d.name)
    assert d.parent == tmp_path / "results"
    for f in ("trials.jsonl", "runner.log", "scenario.resolved.toml", "manifest.json",
              "command.txt", "status.json", "archive.sha256"):
        assert (d / f).is_file(), f
    for role in ("runner", "event", "db", "exec0", "exec1", "exec2"):
        assert (d / "host-facts" / f"{role}.json").is_file(), role
        assert (d / "sar" / f"{role}.bin").is_file(), role
    for role in ("event", "db", "exec0", "exec1", "exec2"):
        assert any((d / "daemon-logs" / role).iterdir()), role
    st = status(d)
    assert set(st) == STATUS_KEYS and st["status"] == "ok" and st["error"] is None
    assert (d / "archive.sha256").read_text().strip() == \
        hashlib.sha256(o.archive.read_bytes()).hexdigest()
    assert json.loads((d / "manifest.json").read_text())["hogd_ref"] == "v0.1.31"
    assert load_scenario(d / "scenario.resolved.toml", []) == s
    cmd = (d / "command.txt").read_text()
    assert "whitney run s.toml --n 3" in cmd and "/opt/whitney/bin/whitney-runner" in cmd
    assert not files_containing(d, SIGNATURE)
    assert fx.find("tofu destroy")


def test_runner_command_block_size_per_exec(tmp_path, fx, scenario):
    install_cluster(fx, 3)
    r = run_scenario(scenario("txs_per_block_per_exec=500000"), 3, opts(tmp_path), fx,
                     command=["whitney"])
    assert r.status == "ok"
    [i] = fx.find("whitney-runner")
    assert "--txs-per-block 1500000" in fx.calls[i].argv[-1]


def test_run_checks_block_size_for_its_n(tmp_path, fx, scenario):
    # n_list is [1, 2, 3]; --n 7 is outside it, so load_scenario did not check it.
    s = scenario("txs_per_block_per_exec=650000000", "accounts=4000000000")
    with pytest.raises(ScenarioError, match=r"at N=7\) must be <= 4294967295"):
        run_scenario(s, 7, opts(tmp_path), fx, command=["whitney"])
    assert not fx.calls  # nothing provisioned


def test_runner_command(tmp_path, fx, scenario):
    install_cluster(fx, 2)
    r = run_scenario(scenario("accounts=10"), 2, opts(tmp_path, runner_timeout=600), fx,
                     command=["whitney"])
    assert r.status == "ok"
    [i] = fx.find("whitney-runner")
    argv = fx.calls[i].argv
    assert argv[0] == "ssh" and "StrictHostKeyChecking=accept-new" in argv
    # A dead link must end the ssh call (about 2 min), not hang it.
    for call in fx.calls:
        if call.argv[0] in ("ssh", "scp"):
            line = " ".join(call.argv)
            assert "-o ServerAliveInterval=30 -o ServerAliveCountMax=4" in line, line
    assert argv[-2] == "ubuntu@203.0.113.10"  # runner public ip
    cmd = argv[-1]
    assert "timeout 600 /opt/whitney/bin/whitney-runner --roles /var/lib/whitney/roles.json" in cmd
    for part in ("--accounts 10", "--experiment erc20_payments", "--txs-per-block 1000000",
                 "--batch-sizes 1,5,10", "--warmup-blocks 0", "--timed-blocks 5",
                 "--connections-per-exec 10", "--coros-per-connection 100",
                 "--uniquifier-layout per-sender",
                 "--json-out /var/lib/whitney/trials.jsonl"):
        assert part in cmd
    [up] = fx.find("ubuntu@203.0.113.10:/var/lib/whitney/roles.json")
    assert up < i
    roles = json.loads((r.run_dir / "roles.json").read_text())
    assert len(roles["execs"]) == 2


def test_run_failure_writes_failed_status(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("whitney-runner", Reply(rc=1, stdout="role=exec0 stop\n",
                                  stderr="panic: tx 17 failed with status 7\n"))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    st = status(r.run_dir)
    assert r.status == st["status"] == "failed"
    assert "panic: tx 17 failed with status 7" in st["error"] and r.error == st["error"]
    assert "panic: tx 17" in (r.run_dir / "runner.log").read_text()
    assert (r.run_dir / "host-facts" / "db.json").is_file()  # collected anyway
    assert (r.run_dir / "scenario.resolved.toml").is_file()


def test_runner_timeout_reported(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("whitney-runner", Reply(rc=124))
    r = run_scenario(scenario(), 1, opts(tmp_path, runner_timeout=60), fx, command=["whitney"])
    assert r.status == "failed" and "timed out after 60 s" in r.error


def test_destroy_runs_on_failure(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("whitney-runner", Reply(rc=1, stderr="boom"))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    assert r.status == "failed"
    assert fx.find("tofu destroy")


def test_partial_apply_failure_destroys(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(rc=1, stderr="Error: InsufficientInstanceCapacity\n"))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    [apply], [destroy] = fx.find("tofu apply"), fx.find("tofu destroy")
    wd = fx.calls[apply].cwd
    assert apply < destroy and fx.calls[destroy].cwd == wd
    assert (wd / "main.tf").is_file() and (wd / "host-setup.sh.tftpl").is_file()
    assert tmp_path / "results" not in wd.parents  # tofu state never lands in results
    st = status(r.run_dir)
    assert st["status"] == "failed" and "InsufficientInstanceCapacity" in st["error"]
    assert (r.run_dir / "scenario.resolved.toml").is_file()
    assert not fx.find("whitney-runner")


def test_presigned_url_redacted(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(rc=1, stderr=f"Error: bad url {PRESIGNED}\n"))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney", "run", PRESIGNED])
    assert r.status == "failed" and "bad url https://bkt.s3.amazonaws.com/" in r.error
    assert not files_containing(r.run_dir, SIGNATURE)
    assert SIGNATURE not in redact(f"curl -o x {PRESIGNED} done")
    assert redact(f"curl {PRESIGNED} done") == \
        "curl https://bkt.s3.amazonaws.com/whitney/a.tar.gz?<redacted> done"


def test_waits_for_ready(tmp_path, fx, scenario, monkeypatch):
    sleeps = []
    monkeypatch.setattr("whitney.remote.time.sleep", sleeps.append)
    install_cluster(fx, 2)
    fx.on("then echo ready", Reply(rc=255, stderr="Connection refused"),
          Reply(stdout=""), Reply(stdout="ready\n"))
    r = run_scenario(scenario(), 2, opts(tmp_path), fx, command=["whitney"])
    assert r.status == "ok"
    [runner] = fx.find("whitney-runner")
    polls = [i for i in fx.find("then echo ready") if i < runner]
    for ip in ("203.0.113.10", "203.0.113.11", "203.0.113.12", "203.0.113.110", "203.0.113.111"):
        assert any(f"ubuntu@{ip}" in fx.calls[i].argv for i in polls), ip
    assert sleeps


def test_host_setup_failure_fails_fast(tmp_path, fx, scenario, monkeypatch):
    sleeps = []
    monkeypatch.setattr("whitney.remote.time.sleep", sleeps.append)
    install_cluster(fx, 1)
    fx.on("then echo ready",
          Reply(stdout="failed\nsetup failed (exit 1): archive sha256 mismatch\n"))
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    assert r.status == "failed" and "archive sha256 mismatch" in status(r.run_dir)["error"]
    assert not sleeps and not fx.find("whitney-runner") and fx.find("tofu destroy")


def test_restarts_control_units_between_runs(tmp_path, fx, scenario):
    install_cluster(fx, 2)
    s, c = scenario(), cluster(tmp_path, 2)
    results = [run_once(s, c, n, 1, tmp_path / f"run-n{n}", fx) for n in (1, 2)]
    assert [r.status for r in results] == ["ok", "ok"]
    runners = fx.find("whitney-runner")
    restarts = fx.find('sudo systemctl restart "whitney-control@$p.service"')
    control_ips = {"203.0.113.11", "203.0.113.12", "203.0.113.110", "203.0.113.111"}
    bounds = [-1, *runners]
    for lo, hi in zip(bounds, bounds[1:]):
        ips = {fx.calls[i].argv[-2].removeprefix("ubuntu@") for i in restarts if lo < i < hi}
        assert control_ips <= ips
    assert (tmp_path / "run-n1" / "status.json").is_file()
    assert len(json.loads((tmp_path / "run-n2" / "roles.json").read_text())["execs"]) == 2


@pytest.mark.parametrize("fail", ["rm -f /var/lib/whitney/trials.jsonl /var/lib/whitney/roles.json",
                                  "ubuntu@203.0.113.10:/var/lib/whitney/roles.json"])
def test_no_stale_trials_when_runner_never_started(tmp_path, fx, scenario, fail):
    """A previous run's trials.jsonl on the runner host must not land in this run dir."""
    install_cluster(fx, 2)
    fx.on(fail, Reply(rc=1, stderr="step failed"))
    c = cluster(tmp_path, 2)
    r = run_once(scenario(), c, 2, 1, tmp_path / "run", fx)
    assert r.status == "failed" and "step failed" in r.error
    assert not fx.find("whitney-runner")
    assert not fx.find("ubuntu@203.0.113.10:/var/lib/whitney/trials.jsonl")  # never copied
    assert not (r.run_dir / "trials.jsonl").exists()
    assert (r.run_dir / "host-facts" / "db.json").is_file()  # other collection still runs


def test_remote_trials_cleared_first(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    c = cluster(tmp_path, 1)
    assert run_once(scenario(), c, 1, 1, tmp_path / "run", fx).status == "ok"
    first = fx.calls[0].argv
    assert first[0] == "ssh" and first[-2] == "ubuntu@203.0.113.10"
    assert first[-1] == "rm -f /var/lib/whitney/trials.jsonl /var/lib/whitney/roles.json"
    [runner] = fx.find("whitney-runner")
    [copy] = fx.find("ubuntu@203.0.113.10:/var/lib/whitney/trials.jsonl")
    assert runner < copy


# ---- interrupts --------------------------------------------------------------


def test_interrupt_during_runner_whitney_run(tmp_path, fx, scenario):
    from whitney.cli import Terminated
    install_cluster(fx, 1)
    fx.on("whitney-runner", Reply(effect=interrupt(Terminated("received SIGHUP"), "trial 0\n")))
    with pytest.raises(Terminated):
        run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    [d] = (tmp_path / "results").iterdir()
    st = status(d)
    assert st["status"] == "failed" and st["error"].startswith("interrupted: received SIGHUP")
    assert (d / "runner.log").read_text() == "trial 0\n"
    assert (d / "host-facts" / "db.json").is_file()
    [runner], [destroy] = fx.find("whitney-runner"), fx.find("tofu destroy")
    assert runner < destroy


def test_interrupt_before_cluster_whitney_run(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(effect=interrupt(KeyboardInterrupt())))
    with pytest.raises(KeyboardInterrupt):
        run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    [d] = (tmp_path / "results").iterdir()
    assert status(d)["error"] == "interrupted: KeyboardInterrupt"
    assert fx.find("tofu destroy")


def test_interrupt_collect_has_a_time_budget(tmp_path, fx, scenario, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("whitney.run.time.monotonic", lambda: clock[0])
    install_cluster(fx, 2)
    fx.on("whitney-runner", Reply(effect=interrupt(KeyboardInterrupt())))

    def slow(argv):  # the first collect step uses up the budget
        clock[0] += 61
    fx.on("ubuntu@203.0.113.10:/var/lib/whitney/trials.jsonl", Reply(effect=slow))
    c = cluster(tmp_path, 2)
    with pytest.raises(KeyboardInterrupt):
        run_once(scenario(), c, 2, 1, tmp_path / "run", fx)
    [runner] = fx.find("whitney-runner")
    assert len(fx.calls) == runner + 2  # the trials copy, then nothing
    st = status(tmp_path / "run")
    assert st["status"] == "failed" and "skipped: time budget spent" in st["error"]


def test_interrupt_rewrites_a_torn_status_whitney_run(tmp_path, fx, scenario, monkeypatch):
    def torn(s, cluster, n, rep, out_dir, ex, **kw):
        (out_dir / "status.json").write_text('{"status": "fa')  # cut off mid-write
        raise KeyboardInterrupt
    monkeypatch.setattr("whitney.run.run_once", torn)
    install_cluster(fx, 1)
    with pytest.raises(KeyboardInterrupt):
        run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    [d] = (tmp_path / "results").iterdir()
    st = status(d)
    assert (st["status"], st["error"]) == ("failed", "interrupted: KeyboardInterrupt")


def test_interrupt_keeps_a_good_status_whitney_run(tmp_path, fx, scenario, monkeypatch):
    from whitney.run import write_status

    def recorded(s, cluster, n, rep, out_dir, ex, **kw):
        write_status(out_dir, "failed", "interrupted: received SIGTERM\ncollect x", "a", "b")
        raise KeyboardInterrupt
    monkeypatch.setattr("whitney.run.run_once", recorded)
    install_cluster(fx, 1)
    with pytest.raises(KeyboardInterrupt):
        run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney"])
    [d] = (tmp_path / "results").iterdir()
    assert status(d)["error"] == "interrupted: received SIGTERM\ncollect x"


def test_interrupt_before_launch_keeps_runner_not_started(tmp_path, fx, scenario):
    """Output of an interrupted pre-launch step (here the roles upload) is not runner output."""
    install_cluster(fx, 1)
    fx.on("ubuntu@203.0.113.10:/var/lib/whitney/roles.json",
          Reply(effect=interrupt(KeyboardInterrupt(), "scp: progress 50%\n")))
    c = cluster(tmp_path, 1)
    with pytest.raises(KeyboardInterrupt):
        run_once(scenario(), c, 1, 1, tmp_path / "run", fx)
    log = (tmp_path / "run" / "runner.log").read_text()
    assert log == "runner not started: interrupted: KeyboardInterrupt\n"
    assert not fx.find("whitney-runner")
    assert not (tmp_path / "run" / "trials.jsonl").exists()


# ---- status files ------------------------------------------------------------


def test_write_status_is_atomic(tmp_path, monkeypatch):
    from whitney.run import write_status
    write_status(tmp_path, "ok", None, "a", "b")

    def fail(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr("whitney.run.os.replace", fail)
    with pytest.raises(OSError, match="disk full"):
        write_status(tmp_path, "failed", "boom", "a", "c")
    assert status(tmp_path)["status"] == "ok"  # the old file is whole
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]  # no temp left


def test_read_status(tmp_path):
    from whitney.run import read_status, write_status
    assert read_status(tmp_path) is None
    (tmp_path / "status.json").write_text('{"status": "fai')
    assert read_status(tmp_path) is None
    (tmp_path / "status.json").write_text('{"status": "weird"}')
    assert read_status(tmp_path) is None
    write_status(tmp_path, "failed", "x", "a", "b")
    assert read_status(tmp_path) == "failed"


# ---- exec cores and allocator ------------------------------------------------


CORES = ("exec_cores=[1, 2, 4]", "txs_per_block_per_core=100000")
STATE = "cpuset.cpus.effective"  # in the post-restart state read only
SETUP = "systemctl set-property"  # in the pre-restart setup only


def test_run_dir_and_block_size_per_core(tmp_path, fx, scenario):
    install_cluster(fx, 2)
    fx.on(STATE, Reply(stdout="11111 0-3 0\n"))
    r = run_scenario(scenario(*CORES), 2, opts(tmp_path), fx, command=["whitney"], cores=4)
    assert r.status == "ok", r.error
    assert re.fullmatch(r"\d{8}-\d{4}-exec-scaling-v0\.1\.31-n2-c4-r1", r.run_dir.name)
    [i] = fx.find("whitney-runner")
    assert "--txs-per-block 800000" in fx.calls[i].argv[-1]  # 100,000 × N 2 × K 4


@pytest.mark.parametrize("ov,cores,msg", [
    (CORES, None, "the scenario has exec_cores: give --cores"),
    ((), 4, "--cores needs a scenario with exec_cores"),
    # K need not be in exec_cores, but its block size is checked first.
    ((*CORES, "accounts=4000000000"), 50000, r"at N=1, K=50000\) must be <= 4294967295"),
])
def test_cores_checked_before_any_spend(tmp_path, fx, scenario, ov, cores, msg):
    with pytest.raises(ScenarioError, match=msg):
        run_scenario(scenario(*ov), 1, opts(tmp_path), fx, command=["whitney"], cores=cores)
    assert not fx.calls


def test_cores_outside_exec_cores_runs(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on(STATE, Reply(stdout="11111 0-2 0\n"))
    r = run_scenario(scenario(*CORES), 1, opts(tmp_path), fx, command=["whitney"], cores=3)
    assert r.status == "ok" and r.run_dir.name.endswith("-n1-c3-r1")


def test_new_run_dir_needs_cores_with_exec_cores(tmp_path, scenario):
    from whitney.run import new_run_dir
    with pytest.raises(ValueError, match="run cores None do not fit exec_cores"):
        new_run_dir(tmp_path, scenario(*CORES), 1, 1)
    with pytest.raises(ValueError, match="run cores 4 do not fit exec_cores"):
        new_run_dir(tmp_path, scenario(), 1, 1, cores=4)
    assert new_run_dir(tmp_path, scenario(), 2, 1).name.endswith("-n2-r1")


def test_exec_setup_on_execs_in_use(tmp_path, fx, scenario):
    install_cluster(fx, 3)
    fx.on(STATE, Reply(stdout="11111 0-3 0\n"))
    out = tmp_path / "run"
    r = run_once(scenario(*CORES), cluster(tmp_path, 3), 2, 1, out, fx, cores=4)
    assert r.status == "ok", r.error
    setups, reads = fx.find(SETUP), fx.find(STATE)
    restarts = fx.find('sudo systemctl restart "whitney-control@$p.service"')
    [runner] = fx.find("whitney-runner")
    # Only exec0 and exec1 (N = 2); never the third exec or the support hosts.
    execs = ["ubuntu@203.0.113.110", "ubuntu@203.0.113.111"]
    assert [fx.calls[i].argv[-2] for i in setups] == execs
    assert [fx.calls[i].argv[-2] for i in reads] == execs
    assert max(setups) < min(restarts) and max(restarts) < min(reads) and max(reads) < runner
    script = fx.calls[setups[0]].argv[-1]
    assert '[ "$n" -ge 4 ] ||' in script  # the host must have K CPUs
    assert script.endswith('sudo systemctl set-property --runtime "whitney-control@11111.service" '
                           "AllowedCPUs=0-3\n")
    assert "libjemalloc" not in script
    host = {"l3": None, "ports": {"11111": {"want": "0-3", "cpus": "0-3", "allocator": "system"}}}
    assert json.loads((out / "exec-setup.json").read_text()) == {"exec0": host, "exec1": host}


def test_no_cores_resets_the_limit(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    out = tmp_path / "run"
    assert run_once(scenario(), cluster(tmp_path, 1), 1, 1, out, fx).status == "ok"
    [setup] = fx.find(SETUP)
    script = fx.calls[setup].argv[-1]
    assert script.endswith('set-property --runtime "whitney-control@11111.service" '
                           "AllowedCPUs=\n")
    assert "nproc" not in script
    assert json.loads((out / "exec-setup.json").read_text()) == \
        {"exec0": {"l3": None, "ports": {"11111": {"want": None, "cpus": "0-63",
                                                   "allocator": "system"}}}}


def test_jemalloc_is_preloaded(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on(STATE, Reply(stdout="11111 0-63 4\n"))
    out = tmp_path / "run"
    r = run_once(scenario("exec_allocator='jemalloc'"), cluster(tmp_path, 1), 1, 1, out, fx)
    assert r.status == "ok", r.error
    script = fx.calls[fx.find(SETUP)[0]].argv[-1]
    assert 'echo "LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2"' in script
    assert json.loads((out / "exec-setup.json").read_text()) == \
        {"exec0": {"l3": None, "ports": {"11111": {"want": None, "cpus": "0-63",
                                                   "allocator": "jemalloc"}}}}


@pytest.mark.parametrize("ov,state,msg", [
    ((*CORES,), "11111 0-63 0\n", "core limit not applied on exec0: whitney-control@11111 "
                                  "has cpuset 0-63 (64 CPUs), want 0-3 (4 CPUs)"),
    # The right count on other CPUs is wrong too.
    ((*CORES,), "11111 4-7 0\n", "has cpuset 4-7 (4 CPUs), want 0-3 (4 CPUs)"),
    ((*CORES,), "11111 - 0\n", "whitney-control@11111 has cpuset none (0 CPUs), want 0-3"),
    ((*CORES,), "","exec setup: exec0 has no control units"),
    ((*CORES,), "11111 0-3 -\n", "exec setup: whitney-control@11111 on exec0 has no "
                                 "control_server process"),
    # The maps check, both ways.
    ((*CORES, "exec_allocator='jemalloc'"), "11111 0-3 0\n",
     "allocator not applied on exec0: whitney-control@11111 runs with system, want jemalloc"),
    ((*CORES,), "11111 0-3 2\n",
     "allocator not applied on exec0: whitney-control@11111 runs with jemalloc, want system"),
    ((*CORES,), "11111 0-3\n", "exec state on exec0 (203.0.113.110): bad line '11111 0-3'"),
])
def test_wrong_exec_state_fails_the_run(tmp_path, fx, scenario, ov, state, msg):
    install_cluster(fx, 1)
    fx.on(STATE, Reply(stdout=state))
    out = tmp_path / "run"
    r = run_once(scenario(*ov), cluster(tmp_path, 1), 1, 1, out, fx, cores=4)
    assert r.status == "failed" and msg in r.error, r.error
    assert not fx.find("whitney-runner")
    assert (out / "host-facts" / "db.json").is_file()  # collected anyway
    if "bad line" not in msg:
        assert (out / "exec-setup.json").is_file()  # kept to show what was there


@pytest.mark.parametrize("ov,cores,stdout", [
    (CORES, 4, "host has 2 CPUs, fewer than 4 exec cores\n"),
    (("exec_allocator='jemalloc'",), None,
     "no /usr/lib/x86_64-linux-gnu/libjemalloc.so.2 (libjemalloc2)\n"),
])
def test_failed_exec_setup_fails_the_run(tmp_path, fx, scenario, ov, cores, stdout):
    install_cluster(fx, 1)
    fx.on(SETUP, Reply(rc=1, stdout=stdout))
    r = run_once(scenario(*ov), cluster(tmp_path, 1), 1, 1, tmp_path / "run", fx, cores=cores)
    assert r.status == "failed"
    assert f"exec setup on exec0 (203.0.113.110) exited 1: {stdout.strip()}" in r.error
    assert not fx.find('sudo systemctl restart "whitney-control@$p.service"')
    assert not fx.find("whitney-runner")


def test_single_host_has_no_exec_setup(tmp_path, fx, scenario):
    install_cluster(fx, 4)
    out = tmp_path / "run"
    assert run_once(scenario(*SINGLE), single_cluster(tmp_path), 4, 1, out, fx).status == "ok"
    assert not fx.find(SETUP) and not fx.find(STATE)
    assert not (out / "exec-setup.json").exists()


# ---- one hog-exec per L3 -----------------------------------------------------


L3 = ("exec_cores=[8, 16, 32]", "txs_per_block_per_core=100000", "exec_per_l3=true")
TOPOLOGY = "shared_cpu_list"  # in the pre-setup L3 read only
R8A_L3 = "".join(f"{g * 8}-{g * 8 + 7}\n" for g in range(8))  # r8a.16xlarge, sysfs repeats
R8A_L3 = R8A_L3 * 2


def _l3_state(procs: int, allocator_maps: int = 0) -> str:
    return "".join(f"{11111 + i} {i * 8}-{i * 8 + 7} {allocator_maps}\n" for i in range(procs))


def test_l3_run_layout_units_and_roles(tmp_path, fx, scenario):
    install_cluster(fx, 3)
    fx.on(TOPOLOGY, Reply(stdout=R8A_L3))
    fx.on(STATE, Reply(stdout=_l3_state(4)))
    out = tmp_path / "run"
    r = run_once(scenario(*L3), cluster(tmp_path, 3), 2, 1, out, fx, cores=32)
    assert r.status == "ok", r.error
    topo, setups, reads = fx.find(TOPOLOGY), fx.find(SETUP), fx.find(STATE)
    restarts = fx.find('sudo systemctl restart "whitney-control@$p.service"')
    execs = ["ubuntu@203.0.113.110", "ubuntu@203.0.113.111"]
    assert [fx.calls[i].argv[-2] for i in topo] == execs
    assert [fx.calls[i].argv[-2] for i in setups] == execs
    assert max(topo) < min(setups) and max(setups) < min(restarts) < min(reads)
    script = fx.calls[setups[0]].argv[-1]
    assert "nproc" not in script  # the L3 read bounds K
    doc = json.loads((out / "exec-setup.json").read_text())
    l3 = [f"{g * 8}-{g * 8 + 7}" for g in range(8)]
    ports = {str(11111 + i): {"want": f"{i * 8}-{i * 8 + 7}", "cpus": f"{i * 8}-{i * 8 + 7}",
                              "allocator": "system"} for i in range(4)}
    assert doc == {"exec0": {"l3": l3, "ports": ports}, "exec1": {"l3": l3, "ports": ports}}
    roles = json.loads((out / "roles.json").read_text())
    assert [e["host"] for e in roles["execs"]] == ["10.42.1.110"] * 4 + ["10.42.1.111"] * 4
    # Block size follows K, not the hog-exec count.
    [runner] = fx.find("whitney-runner")
    assert "--txs-per-block 6400000" in fx.calls[runner].argv[-1]  # 100,000 × N 2 × K 32


def test_l3_run_small_k_is_one_hog_exec(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on(TOPOLOGY, Reply(stdout=R8A_L3))
    fx.on(STATE, Reply(stdout="11111 0-3 0\n"))
    out = tmp_path / "run"
    r = run_once(scenario(*L3), cluster(tmp_path, 1), 1, 1, out, fx, cores=4)
    assert r.status == "ok", r.error
    script = fx.calls[fx.find(SETUP)[0]].argv[-1]
    assert script.endswith('sudo systemctl set-property --runtime "whitney-control@11111.service" '
                           "AllowedCPUs=0-3\n")
    assert "11112" not in script and "sudo install -d" not in script
    assert len(json.loads((out / "roles.json").read_text())["execs"]) == 1


@pytest.mark.parametrize("cores,topology,msg", [
    (12, [R8A_L3], "exec layout on exec0: exec_per_l3: K=12 is not a multiple of the L3 size 8"),
    (16, [""], "L3 topology on exec0 (203.0.113.110): no L3 cache groups"),
    (16, [R8A_L3, "0-15\n16-31\n32-47\n48-63\n"],
     "exec hosts disagree on the L3 size: exec0 8 CPUs, exec1 16 CPUs"),
])
def test_bad_l3_layout_fails_before_setup(tmp_path, fx, scenario, cores, topology, msg):
    install_cluster(fx, 2)
    fx.on(TOPOLOGY, *(Reply(stdout=t) for t in topology))
    r = run_once(scenario(*L3), cluster(tmp_path, 2), 2, 1, tmp_path / "run", fx, cores=cores)
    assert r.status == "failed" and msg in r.error, r.error
    assert not fx.find(SETUP) and not fx.find("whitney-runner")
    assert not fx.find('sudo systemctl restart "whitney-control@$p.service"')


def test_failed_l3_read_fails_the_run(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on(TOPOLOGY, Reply(rc=1, stdout="boom\n"))
    r = run_once(scenario(*L3), cluster(tmp_path, 1), 1, 1, tmp_path / "run", fx, cores=16)
    assert r.status == "failed"
    assert "L3 topology on exec0 (203.0.113.110) exited 1: boom" in r.error


def test_roles_json_with_hog_execs_per_l3(tmp_path, scenario):
    c = Cluster(roles=roles_output(3), workdir=tmp_path)
    r = json.loads(roles_json(c, 2, scenario(*L3), procs=4))
    execs = r["execs"]
    assert len(execs) == 8
    names = [e["work_dir"].rsplit("/", 1)[1] for e in execs]
    assert names == ["exec0", "exec0-1", "exec0-2", "exec0-3",
                     "exec1", "exec1-1", "exec1-2", "exec1-3"]
    assert [e["host"] for e in execs] == ["10.42.1.110"] * 4 + ["10.42.1.111"] * 4
    assert [e["control_port"] for e in execs] == [11111, 11112, 11113, 11114] * 2
    assert {e["cpu_affinity"] for e in execs} == {""}
    ports = [e["metrics_port"] for e in execs]
    assert ports == [9104, 9201, 9202, 9203, 9104, 9265, 9266, 9267]
    # Unique per host and across hosts (but the first hog-exec of each host
    # keeps 9104), and clear of every fixed port and the single-host range.
    assert len(set(ports[1:4] + ports[5:])) == 6
    fixed = {9101, 9102, 9103, 9104} | {9110 + k for k in range(64)}
    assert not fixed & set(ports[1:4] + ports[5:])
    assert r["execs"][0] == json.loads(roles_json(c, 2, scenario()))["execs"][0]


def test_roles_json_rejects_hog_execs_per_host_on_single_host(tmp_path, scenario):
    with pytest.raises(ValueError, match="procs=2 hog-execs per exec host"):
        roles_json(single_cluster(tmp_path), 2, scenario(*SINGLE), procs=2)


def _setup_doc(*cpus_by_port: str, l3=None) -> dict:
    return {"exec0": {"l3": l3, "ports": {str(11111 + i): {"want": None, "cpus": c,
                                                             "allocator": "system"}
                                          for i, c in enumerate(cpus_by_port)}}}


@pytest.mark.parametrize("plan,cores,state,msg", [
    (["0-7", "8-15"], 16, ("0-7", "8-15"), None),
    # Effective cpusets in another form but the same CPUs pass.
    (["0-2"], 3, ("0,1,2",), None),
    (["0-7", "8-15"], 16, ("0-7", "0-7"), "whitney-control@11112 has cpuset 0-7 (8 CPUs), "
                                          "want 8-15 (8 CPUs)"),
    (["0-7", "8-15"], 16, ("0-7",), "exec0 has control units on ports 11111, want 11111, 11112"),
    # A plan that does not add up to K fails too.
    (["0-7", "8-15"], 32, ("0-7", "8-15"), "its control units have 16 CPUs in all, want 32"),
])
def test_check_exec_setup_slices(plan, cores, state, msg):
    from whitney.run import check_exec_setup
    doc = _setup_doc(*state)
    if msg is None:
        check_exec_setup(doc, {"exec0": plan}, cores, "system")
    else:
        with pytest.raises(RuntimeError, match=re.escape(msg)):
            check_exec_setup(doc, {"exec0": plan}, cores, "system")


def test_exec_plan():
    from whitney.run import exec_plan
    groups = [list(range(g * 8, g * 8 + 8)) for g in range(8)]
    assert exec_plan(["exec0", "exec1"], 4, None) == {"exec0": ["0-3"], "exec1": ["0-3"]}
    assert exec_plan(["exec0"], None, None) == {"exec0": [""]}
    assert exec_plan(["exec0", "exec1"], 16, {"exec0": groups, "exec1": groups}) == \
        {"exec0": ["0-7", "8-15"], "exec1": ["0-7", "8-15"]}
    with pytest.raises(ValueError, match="exec_per_l3 needs the run's exec cores"):
        exec_plan(["exec0"], None, {"exec0": groups})
