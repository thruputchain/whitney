import json
from pathlib import Path

import pytest

from fakes import (ICE, KEY, SCENARIO, SINGLE, WEST, Reply, host, install_cluster, interrupt,
                   make_archive, opts, single_cluster)
from whitney.cli import Terminated, main
from whitney.config import ScenarioError
from whitney.infra import DestroyError
from whitney.run import roles_json, run_once
from whitney.sweep import plan_sweep, runner_cpus, single_host_roles, sweep, validate_cpu_layout


def test_plan_sweep_shuffles_per_rep(scenario):
    s = scenario("repetitions=3", "n_list=[1,2,3,4,5,6,8,10]")
    plan = plan_sweep(s, 7)
    assert len(plan) == 3
    assert all(sorted(row) == sorted(s.n_list) for row in plan)
    assert plan == plan_sweep(s, 7)
    assert plan != plan_sweep(s, 8)
    assert len({tuple(r) for r in plan}) > 1


def test_one_cluster_per_rep(tmp_path, fx, scenario):
    s = scenario("repetitions=3")
    install_cluster(fx, 3)
    results = sweep(s, opts(tmp_path), fx, seed=1)
    assert len(results) == 9
    assert all(r.status == "ok" for r in results)
    assert len(fx.find("tofu apply")) == 3
    assert len(fx.find("tofu destroy")) == 3
    applies = [c for c in fx.calls if "tofu apply" in c.line]
    assert len({c.cwd for c in applies}) == 3
    tfvars = [json.loads((c.cwd / "whitney.auto.tfvars.json").read_text()) for c in applies]
    assert all(v["n_exec"] == 3 for v in tfvars)


def test_sweep_json_and_run_dirs(tmp_path, fx, scenario):
    s = scenario("repetitions=2")
    install_cluster(fx, 3)
    results = sweep(s, opts(tmp_path), fx, seed=5)
    root = results[0].run_dir.parent
    meta = json.loads((root / "sweep.json").read_text())
    assert meta["seed"] == 5
    assert meta["order"] == plan_sweep(s, 5)
    assert meta["scenario"] == "exec-scaling"
    assert len(meta["archive_sha256"]) == 64
    for r in results:
        assert (r.run_dir / "status.json").is_file()
        assert (r.run_dir / "scenario.resolved.toml").is_file()
    names = [r.run_dir.name for r in results]
    for rep, order in enumerate(meta["order"], start=1):
        got = [int(n.split("-n")[1].split("-")[0]) for n in names if n.endswith(f"-r{rep}")]
        assert got == order
    assert all("-exec-scaling-v0.1.31-n" in n for n in names)


def test_failure_continues(tmp_path, fx, scenario):
    s = scenario("repetitions=1")
    install_cluster(fx, 3)
    fx.on("whitney-runner", Reply(stdout="ok\n"), Reply(rc=1, stdout="boom\n"), Reply(stdout="ok\n"))
    results = sweep(s, opts(tmp_path), fx, seed=1)
    assert [r.status for r in results] == ["ok", "failed", "ok"]
    assert len(fx.find("whitney-runner")) == 3
    assert "boom" in results[1].error
    assert len(fx.find("tofu destroy")) == 1


def test_apply_failure_records_failed_runs_and_continues(tmp_path, fx, scenario):
    s = scenario("repetitions=2")
    install_cluster(fx, 3)
    fx.on("tofu apply", Reply(rc=1, stderr="quota exceeded"), Reply())
    results = sweep(s, opts(tmp_path), fx, seed=1)
    assert [r.status for r in results] == ["failed"] * 3 + ["ok"] * 3
    assert "quota exceeded" in results[0].error
    assert len(fx.find("tofu destroy")) == 2


def test_capacity_failure_fails_the_repetition_and_continues(tmp_path, fx, scenario):
    s = scenario(*WEST, "repetitions=2", "n_list=[1, 2]")
    install_cluster(fx, 2)
    fx.on("tofu apply", Reply(rc=1, stderr=ICE), Reply())
    results = sweep(s, opts(tmp_path), fx, seed=1)
    assert [r.status for r in results] == ["failed", "failed", "ok", "ok"]
    for r in results[:2]:
        err = json.loads((r.run_dir / "status.json").read_text())["error"]
        assert err.startswith("capacity: EC2 has no r8a.16xlarge capacity in us-west-2b")
        assert r.error == err
    assert len(fx.find("tofu destroy")) == 2


def test_sweep_records_orphans_per_repetition(tmp_path, fx, scenario):
    s = scenario("repetitions=2", "n_list=[1]")
    install_cluster(fx, 1)
    fx.on("aws ec2 describe-instances",
          Reply(stdout=json.dumps([["i-0orphan", "x-exec0"]])), Reply(stdout="[]"))
    results = sweep(s, opts(tmp_path), fx, seed=1)
    assert len(fx.find("aws ec2 describe-instances")) == 2
    assert len(fx.find("aws ec2 terminate-instances")) == 1
    root = results[0].run_dir.parent
    meta = json.loads((root / "sweep.json").read_text())
    assert meta["cleanup"] == [{"rep": 1, "note": "orphaned instances terminated: i-0orphan (x-exec0)"}]
    st = json.loads((results[0].run_dir / "status.json").read_text())
    assert st["cleanup"] == ["orphaned instances terminated: i-0orphan (x-exec0)"]
    assert "cleanup" not in json.loads((results[1].run_dir / "status.json").read_text())


def test_main_sweep(tmp_path, fx, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.toml").write_text(SCENARIO + "repetitions = 2\n")
    archive = make_archive(tmp_path / "pre", KEY)
    install_cluster(fx, 3)
    rc = main(["sweep", "s.toml", "--bucket", "bkt", "--ssh-key-name", "me",
               "--operator-cidr", "198.51.100.7/32", "--archive", str(archive), "--seed", "3"], ex=fx)
    assert rc == 0
    assert capsys.readouterr().out.count("ok ") == 6
    assert len(fx.find("tofu destroy")) == 2


# ---- single host -----------------------------------------------------------------


def test_single_host_roles_disjoint(scenario):
    s = scenario(*SINGLE)
    r = single_host_roles(s, "10.42.1.10")
    roles = [r["event"], r["db"], *r["execs"]]
    assert [x["control_port"] for x in roles] == [11111 + i for i in range(6)]
    assert [x["metrics_port"] for x in roles] == [9101, 9102, 9110, 9111, 9112, 9113]
    assert len({x["work_dir"] for x in roles}) == 6
    assert r["event"]["work_dir"] == "/mnt/whitney/work/event"
    assert r["execs"][3]["work_dir"] == "/mnt/whitney/work/exec3"
    assert all(x["host"] == "10.42.1.10" for x in roles)
    assert r["runner_bind_ip"] == "10.42.1.10"
    assert r["bin_dir"] == "/opt/whitney/bin"
    assert r["indexer"] is None
    assert r["db"]["cpu_affinity"] == "24-39"
    seen: set[int] = set()
    for spec in [s.cpu_layout["runner"], *(x["cpu_affinity"] for x in roles)]:
        lo, hi = map(int, spec.split("-"))
        cpus = set(range(lo, hi + 1))
        assert not cpus & seen
        assert max(cpus) <= 191
        seen |= cpus


def test_single_host_roles_indexer_port_order(scenario):
    s = scenario(*SINGLE, "indexer=true", "cpu_layout={runner='0', event='1', db='2-3', "
                 "indexer='4', exec0='5', exec1='6', exec2='7', exec3='8,10-11'}")
    r = single_host_roles(s, "10.0.0.1")
    assert [r["event"]["control_port"], r["db"]["control_port"],
            r["indexer"]["control_port"], r["execs"][0]["control_port"]] == [11111, 11112, 11113, 11114]
    assert [r["event"]["metrics_port"], r["db"]["metrics_port"],
            r["indexer"]["metrics_port"], r["execs"][0]["metrics_port"]] == [9101, 9102, 9103, 9110]
    assert r["execs"][3]["cpu_affinity"] == "8,10-11"


@pytest.mark.parametrize("layout, msg", [
    ("cpu_layout={runner='0-15', event='16-23', db='24-39', exec0='40-77', exec1='78-115', exec2='116-153'}",
     "cpu_layout needs exec3"),
    ("cpu_layout={runner='0-17', event='16-23', db='24-39', exec0='40-77', exec1='78-115', exec2='116-153', exec3='154-191'}",
     "cpu_layout overlaps: "),
    ("cpu_layout={runner='0-15', event='16-23', db='24-39', exec0='40-77', exec1='78-115', exec2='116-153', exec3='154-192'}",
     "outside 0-191"),
    ("cpu_layout={runner='0-15', event='x', db='24-39', exec0='40-77', exec1='78-115', exec2='116-153', exec3='154-191'}",
     "bad CPU range"),
])
def test_cpu_layout_errors(scenario, layout, msg):
    s = scenario("single_host=true", "storage='tmpfs'", "n_list=[4]", layout)
    with pytest.raises(ScenarioError, match=msg):
        single_host_roles(s, "10.0.0.1")


def test_indexer_needs_cpu_layout_entry(scenario):
    s = scenario(*SINGLE, "indexer=true")
    with pytest.raises(ScenarioError, match="cpu_layout needs indexer"):
        single_host_roles(s, "10.0.0.1")


def test_unknown_single_host_type_still_errors(scenario):
    s = scenario(*SINGLE, "instance_types={single='m8a.4xlarge'}")
    with pytest.raises(ScenarioError, match="unknown CPU count for instance type m8a.4xlarge"):
        validate_cpu_layout(s, 4)


def test_roles_json_single_host_uses_n(tmp_path, scenario):
    s = scenario(*SINGLE)
    r = json.loads(roles_json(single_cluster(tmp_path), 2, s))
    assert len(r["execs"]) == 2
    assert r["event"]["host"] == "10.42.1.10"
    with pytest.raises(ValueError, match="n=5 but"):
        roles_json(single_cluster(tmp_path), 5, s)


def test_runner_launched_with_taskset(tmp_path, fx, scenario):
    s = scenario(*SINGLE)
    assert runner_cpus(s) == "0-15"
    assert runner_cpus(scenario()) is None  # multi-host: not pinned
    install_cluster(fx, 4)
    r = run_once(s, single_cluster(tmp_path), 4, 1, tmp_path / "run", fx)
    assert r.status == "ok"
    cmd = fx.calls[fx.find("whitney-runner")[0]].argv[-1]
    assert "timeout 7200 taskset -c 0-15 /opt/whitney/bin/whitney-runner" in cmd


def test_single_host_sweep(tmp_path, fx, scenario):
    s = scenario(*SINGLE, "repetitions=1")
    fx.on("aws s3 presign", Reply(stdout="https://x/y?sig=1\n"))
    single = host(0)
    out = {"roles": {"value": {"runner": single, "event": single, "db": single,
                               "indexer": None, "execs": [single] * 4}}}
    fx.on("tofu output -json", Reply(stdout=json.dumps(out)))
    fx.on("then echo ready", Reply(stdout="ready\n"))
    results = sweep(s, opts(tmp_path), fx, seed=1)
    assert [r.status for r in results] == ["ok"]
    assert len(fx.find("tofu apply")) == 1
    roles = json.loads((results[0].run_dir / "roles.json").read_text())
    assert len(roles["execs"]) == 4


def test_sweep_rejects_bad_layout_before_any_call(tmp_path, fx, scenario):
    s = scenario("single_host=true", "storage='tmpfs'", "n_list=[4]", "cpu_layout={runner='0-3'}")
    with pytest.raises(ScenarioError, match="cpu_layout needs event"):
        sweep(s, opts(tmp_path), fx)
    assert fx.calls == []


# ---- interrupts ------------------------------------------------------------------


def _sweep_dir(tmp_path) -> Path:
    [root] = (tmp_path / "results").glob("*-sweep")
    return root


def test_interrupt_during_runner_records_the_run(tmp_path, fx, scenario):
    s = scenario("repetitions=2", "n_list=[1, 2]")
    install_cluster(fx, 2)
    fx.on("whitney-runner", Reply(stdout="ok\n"),
          Reply(effect=interrupt(Terminated("received SIGTERM"), "role=exec0 stop\npartial\n")))
    with pytest.raises(Terminated):
        sweep(s, opts(tmp_path), fx, seed=1)
    root = _sweep_dir(tmp_path)
    meta = json.loads((root / "sweep.json").read_text())
    assert meta["aborted"] is True
    assert meta["abort_reason"] == "interrupted: received SIGTERM"
    assert [r["status"] for r in meta["runs"]] == ["ok", "failed"]
    d = root / meta["runs"][1]["run_dir"]
    st = json.loads((d / "status.json").read_text())
    assert st["status"] == "failed"
    assert st["error"].startswith("interrupted: received SIGTERM")
    assert "partial" in (d / "runner.log").read_text()
    assert (d / "host-facts" / "db.json").is_file()  # best-effort collect ran
    assert (d / "trials.jsonl").is_file()  # partial trials, the runner had started
    [runner2] = fx.find("whitney-runner")[1:]
    [destroy] = fx.find("tofu destroy")
    assert runner2 < destroy
    assert len(fx.find("tofu apply")) == 1  # the second repetition never started


def test_ctrl_c_status_names_keyboard_interrupt(tmp_path, fx, scenario):
    s = scenario("repetitions=1", "n_list=[1]")
    install_cluster(fx, 1)
    fx.on("whitney-runner", Reply(effect=interrupt(KeyboardInterrupt())))
    with pytest.raises(KeyboardInterrupt):
        sweep(s, opts(tmp_path), fx, seed=1)
    meta = json.loads((_sweep_dir(tmp_path) / "sweep.json").read_text())
    assert meta["aborted"] is True and meta["abort_reason"] == "interrupted: KeyboardInterrupt"
    [run] = meta["runs"]
    st = json.loads((_sweep_dir(tmp_path) / run["run_dir"] / "status.json").read_text())
    assert run["status"] == "failed" and st["error"].startswith("interrupted: KeyboardInterrupt")
    assert fx.find("tofu destroy")


def test_second_interrupt_skips_collect(tmp_path, fx, scenario):
    s = scenario("repetitions=1", "n_list=[1]")
    install_cluster(fx, 1)
    fx.on("whitney-runner", Reply(effect=interrupt(KeyboardInterrupt("first"))))
    fx.on(":/var/lib/whitney/host-facts.json",  # the collect scp
          Reply(effect=interrupt(KeyboardInterrupt("second"))))
    with pytest.raises(KeyboardInterrupt, match="first"):
        sweep(s, opts(tmp_path), fx, seed=1)
    meta = json.loads((_sweep_dir(tmp_path) / "sweep.json").read_text())
    st = json.loads((_sweep_dir(tmp_path) / meta["runs"][0]["run_dir"] / "status.json").read_text())
    assert st["status"] == "failed" and st["error"].startswith("interrupted: first")
    assert "collect skipped: interrupted again" in st["error"]
    assert fx.find("tofu destroy")


def test_interrupt_during_apply_marks_sweep_aborted(tmp_path, fx, scenario):
    s = scenario("repetitions=1", "n_list=[1]")
    install_cluster(fx, 1)
    fx.on("tofu apply", Reply(effect=interrupt(KeyboardInterrupt())))
    with pytest.raises(KeyboardInterrupt):
        sweep(s, opts(tmp_path), fx, seed=1)
    meta = json.loads((_sweep_dir(tmp_path) / "sweep.json").read_text())
    assert meta["aborted"] is True and meta["runs"] == []
    assert fx.find("tofu destroy")


def test_destroy_error_marks_sweep_aborted(tmp_path, fx, scenario):
    s = scenario("repetitions=2", "n_list=[1]")
    install_cluster(fx, 1)
    fx.on("tofu destroy", Reply(rc=1, stderr="ExpiredToken"))
    with pytest.raises(DestroyError):
        sweep(s, opts(tmp_path), fx, seed=1)
    meta = json.loads((_sweep_dir(tmp_path) / "sweep.json").read_text())
    assert meta["aborted"] is True and "tofu destroy failed" in meta["abort_reason"]
    assert [r["status"] for r in meta["runs"]] == ["ok"]


def test_finished_sweep_is_not_aborted(tmp_path, fx, scenario):
    install_cluster(fx, 3)
    sweep(scenario(), opts(tmp_path), fx, seed=1)
    meta = json.loads((_sweep_dir(tmp_path) / "sweep.json").read_text())
    assert meta["aborted"] is False and meta["abort_reason"] is None


def test_interrupt_rewrites_a_torn_status_sweep(tmp_path, fx, scenario, monkeypatch):
    def torn(s, cluster, n, rep, out_dir, ex, **kw):
        (out_dir / "status.json").write_text("")  # cut off mid-write
        raise KeyboardInterrupt
    monkeypatch.setattr("whitney.sweep.run_once", torn)
    install_cluster(fx, 1)
    with pytest.raises(KeyboardInterrupt):
        sweep(scenario("repetitions=1", "n_list=[1]"), opts(tmp_path), fx, seed=1)
    meta = json.loads((_sweep_dir(tmp_path) / "sweep.json").read_text())
    d = _sweep_dir(tmp_path) / meta["runs"][0]["run_dir"]
    st = json.loads((d / "status.json").read_text())
    assert (st["status"], st["error"]) == ("failed", "interrupted: KeyboardInterrupt")
    assert meta["runs"][0]["status"] == "failed"


# ---- exec cores ------------------------------------------------------------------

CORES = ("n_list=[1, 2]", "exec_cores=[1, 4, 8]", "txs_per_block_per_core=100000")


def test_plan_sweep_shuffles_n_core_pairs(scenario):
    s = scenario(*CORES, "repetitions=3")
    plan = plan_sweep(s, 7)
    pairs = sorted([n, k] for n in (1, 2) for k in (1, 4, 8))
    assert len(plan) == 3 and all(sorted(row) == pairs for row in plan)
    assert plan == plan_sweep(s, 7) and plan != plan_sweep(s, 8)
    assert len({tuple(map(tuple, r)) for r in plan}) > 1


def test_sweep_runs_every_n_core_pair(tmp_path, fx, scenario):
    s = scenario(*CORES, "repetitions=2")
    install_cluster(fx, 2)
    fx.on("cpuset.cpus.effective", *(Reply(stdout=f"11111 0-{k - 1} 0\n")
                                     for row in plan_sweep(s, 5) for n, k in row
                                     for _ in range(n)))
    results = sweep(s, opts(tmp_path), fx, seed=5)
    assert len(results) == 12 and all(r.status == "ok" for r in results), \
        [r.error for r in results]
    tfvars = [json.loads((c.cwd / "whitney.auto.tfvars.json").read_text())
              for c in fx.calls if "tofu apply" in c.line]
    assert [v["n_exec"] for v in tfvars] == [2, 2]  # sized for max(n_list)
    root = results[0].run_dir.parent
    meta = json.loads((root / "sweep.json").read_text())
    assert meta["order"] == plan_sweep(s, 5)
    got = [[e["n"], e["cores"]] for e in meta["runs"]]
    assert got == [pair for row in meta["order"] for pair in row]
    for e in meta["runs"]:
        assert e["run_dir"].endswith(f"-n{e['n']}-c{e['cores']}-r{e['rep']}")
        assert e["status"] == "ok"
    runners = [fx.calls[i].argv[-1] for i in fx.find("whitney-runner")]
    assert len(runners) == 12
    assert all(f"--txs-per-block {100000 * n * k} " in cmd for cmd, (n, k) in zip(runners, got))
    # Each run limits its N exec hosts to its K cores.
    limits = [fx.calls[i].argv[-1] for i in fx.find("systemctl set-property")]
    want = [f"AllowedCPUs=0-{k - 1}\n" for n, k in got for _ in range(n)]
    assert len(limits) == len(want) and all(w in x for w, x in zip(want, limits))


def test_sweep_json_cores_null_without_exec_cores(tmp_path, fx, scenario):
    install_cluster(fx, 3)
    results = sweep(scenario("repetitions=1"), opts(tmp_path), fx, seed=1)
    meta = json.loads((results[0].run_dir.parent / "sweep.json").read_text())
    assert [e["cores"] for e in meta["runs"]] == [None] * 3
    assert all(isinstance(n, int) for n in meta["order"][0])
