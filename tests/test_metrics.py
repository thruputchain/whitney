"""Per-run metrics: ports, Vector per run, collection, remote write."""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest

from fakes import (KEY, SCENARIO, SINGLE, Reply, cluster, fake_scp, install_cluster, make_archive,
                   opts, single_cluster)
from whitney.cli import main
from whitney.metrics import (EXEC_METRICS_PORT, METRICS_PORTS, SINGLE_EXEC_METRICS_BASE,
                             VECTOR_VERSION, RemoteWrite, metrics_port, remote_write_from_env,
                             run_env)
from whitney.run import run_once, run_scenario
from whitney.sweep import sweep

REPO = Path(__file__).resolve().parents[1]
URL = "https://prometheus.example.net/api/prom/push"
USER_ENV = "WHITNEY_REMOTE_WRITE_USERNAME"
PASS_ENV = "WHITNEY_REMOTE_WRITE_PASSWORD"
TOKEN_ENV = "WHITNEY_REMOTE_WRITE_BEARER_TOKEN"
SECRET = "glc_s3cr3t-Pa55word+/="


def ip_of(call) -> str:
    return next(a for a in call.argv if a.startswith("ubuntu@")).split("@")[1].split(":")[0]


def all_text(fx, root: Path) -> str:
    calls = "\n".join(c.line for c in fx.calls)
    files = "\n".join(p.read_text(errors="replace") for p in root.rglob("*")
                      if p.is_file() and p.suffix != ".gz")
    return calls + "\n" + files


# ---- ports ---------------------------------------------------------------------


def test_metrics_ports():
    assert METRICS_PORTS == {"event": 9101, "db": 9102, "indexer": 9103}
    assert EXEC_METRICS_PORT == 9104 and SINGLE_EXEC_METRICS_BASE == 9110
    assert [metrics_port(r, single_host=False) for r in ("event", "db", "indexer", "exec0",
                                                         "exec7")] == [9101, 9102, 9103,
                                                                       9104, 9104]
    assert [metrics_port(r, single_host=True) for r in ("event", "exec0", "exec7")] == \
        [9101, 9110, 9117]
    with pytest.raises(ValueError):
        metrics_port("runner", single_host=False)


def test_extra_exec_metrics_ports():
    """exec_per_l3: hog-exec i >= 1 of exec host h is exec<h>-<i> on 9200 + 64h + i."""
    assert metrics_port("exec0-1", single_host=False) == 9201
    assert metrics_port("exec3-7", single_host=False) == 9200 + 3 * 64 + 7
    for bad in ("exec0-0", "exec0-64"):
        with pytest.raises(ValueError, match="no metrics port"):
            metrics_port(bad, single_host=False)
    with pytest.raises(ValueError, match="no metrics port"):
        metrics_port("exec0-1", single_host=True)


def test_tofu_uses_the_same_ports_and_vector_pin():
    """infra/main.tf renders the per-host role -> port map Vector scrapes."""
    tf = (REPO / "infra" / "main.tf").read_text()
    for role, port in METRICS_PORTS.items():
        assert re.search(rf"\b{role}\s*=\s*{port}\b", tf), role
    assert re.search(rf"\bexec_metrics_port\s*=\s*{EXEC_METRICS_PORT}\b", tf)
    assert re.search(rf"\bsingle_exec_metrics_base\s*=\s*{SINGLE_EXEC_METRICS_BASE}\b", tf)
    assert f'version = "{VECTOR_VERSION}"' in tf
    assert "aa3229c525a933e1af0a63a0cc6f6e5ce126b60c0ebacc17a7f553ba55b0bb52" in tf


# ---- run.env ---------------------------------------------------------------------


def test_run_env():
    assert run_env("20261022-1000-exec-scaling-v1-n2-r1", "exec-scaling", 2, 1) == (
        "WHITNEY_RUN=20261022-1000-exec-scaling-v1-n2-r1\n"
        "WHITNEY_SCENARIO=exec-scaling\nWHITNEY_N=2\nWHITNEY_REP=1\n")
    with pytest.raises(ValueError, match="run name"):
        run_env('bad"name', "s", 1, 1)


# ---- remote write: validation -------------------------------------------------------


def test_remote_write_basic_auth():
    rw = remote_write_from_env(URL, {USER_ENV: "123456", PASS_ENV: SECRET})
    assert rw.host == "prometheus.example.net"
    assert rw.env_file() == (f"WHITNEY_REMOTE_WRITE_URL={URL}\n"
                             f"{USER_ENV}=123456\n{PASS_ENV}={SECRET}\n")
    assert SECRET not in repr(rw) and SECRET not in str(rw)


def test_remote_write_bearer():
    rw = remote_write_from_env(URL, {TOKEN_ENV: SECRET})
    assert rw.env_file() == f"WHITNEY_REMOTE_WRITE_URL={URL}\n{TOKEN_ENV}={SECRET}\n"
    assert SECRET not in repr(rw)


@pytest.mark.parametrize("url, env, msg", [
    ("http://prom.example.net/push", {TOKEN_ENV: "t"}, "must be an https URL"),
    ("https://u:p@prom.example.net/push", {TOKEN_ENV: "t"}, "must not hold credentials"),
    ('https://prom.example.net/pu"sh', {TOKEN_ENV: "t"}, "characters"),
    (URL, {}, "needs credentials"),
    (URL, {USER_ENV: "u"}, f"{USER_ENV} and {PASS_ENV} go together"),
    (URL, {USER_ENV: "u", PASS_ENV: "p", TOKEN_ENV: "t"}, "not both"),
    (URL, {TOKEN_ENV: "has space"}, f"{TOKEN_ENV} has characters"),
    (URL, {USER_ENV: "u", PASS_ENV: 'q"uote'}, f"{PASS_ENV} has characters"),
    (URL, {USER_ENV: "u", PASS_ENV: "back\\slash"}, f"{PASS_ENV} has characters"),
    (URL, {USER_ENV: "u", PASS_ENV: "do$llar"}, f"{PASS_ENV} has characters"),
])
def test_remote_write_rejects(url, env, msg):
    with pytest.raises(ValueError, match=re.escape(msg)) as e:
        remote_write_from_env(url, env)
    for v in env.values():
        if len(v) > 1:
            assert v not in str(e.value)  # never echo a credential


def test_empty_env_values_count_as_unset():
    rw = remote_write_from_env(URL, {USER_ENV: "", PASS_ENV: "", TOKEN_ENV: "t"})
    assert rw.bearer_token == "t" and rw.username is None


# ---- one run: Vector start, stop, collect -----------------------------------------------


def test_vector_restarted_with_run_env_before_runner(tmp_path, fx, scenario):
    install_cluster(fx, 2)
    c = cluster(tmp_path)
    out = tmp_path / "20261022-1000-exec-scaling-v0.1.31-n2-r3"
    r = run_once(scenario(), c, 2, 3, out, fx)
    assert r.status == "ok", r.error
    runner = fx.find("whitney-runner")[0]
    starts = fx.find("systemctl restart whitney-vector.service")
    in_use = {"203.0.113.10", "203.0.113.11", "203.0.113.12", "203.0.113.110",
              "203.0.113.111"}
    assert {ip_of(fx.calls[i]) for i in starts} == in_use
    assert all(i < runner for i in starts)
    # Started on all hosts first, then checked: one wait, not one per host.
    checks = fx.find("is-active --quiet whitney-vector.service")
    assert {ip_of(fx.calls[i]) for i in checks} == in_use
    assert max(starts) < min(checks) and max(checks) < runner
    start = fx.calls[starts[0]].argv[-1]
    assert f"WHITNEY_RUN={out.name}" in start and "WHITNEY_SCENARIO=exec-scaling" in start
    assert "WHITNEY_N=2" in start and "WHITNEY_REP=3" in start
    assert "/etc/whitney/vector/run.env" in start
    # No remote write: any remote.env from before goes away.
    assert "sudo rm -f /etc/whitney/vector/remote.env" in start
    assert not fx.find("remote.env.") and not fx.find("install -m 0600")
    # Control units restart before the run's Vector starts.
    assert max(fx.find('systemctl restart "whitney-control@$p.service"')) < min(starts)


def test_vector_stopped_then_metrics_collected(tmp_path, fx, scenario):
    install_cluster(fx, 2)
    out = tmp_path / "20261022-1000-exec-scaling-v0.1.31-n2-r1"
    r = run_once(scenario(), cluster(tmp_path), 2, 1, out, fx)
    assert r.status == "ok", r.error
    runner = fx.find("whitney-runner")[0]
    for name, ip in (("runner", "203.0.113.10"), ("db", "203.0.113.12"),
                     ("exec1", "203.0.113.111")):
        stop = [i for i in fx.find("sudo systemctl stop --no-block whitney-vector.service")
                if i > runner and ip_of(fx.calls[i]) == ip]
        gz = [i for i in fx.find(f"gzip -f /var/lib/whitney/metrics/{out.name}.ndjson")
              if ip_of(fx.calls[i]) == ip]
        fetch = [i for i in fx.find(f"/var/lib/whitney/metrics/{out.name}.ndjson.gz")
                 if fx.calls[i].argv[0] == "scp" and ip_of(fx.calls[i]) == ip]
        assert stop and gz and fetch and stop[0] < gz[0] < fetch[0], name
        # The gzip waits for Vector to finish stopping (it flushes on stop).
        assert "ActiveState" in fx.calls[gz[0]].line
        assert fx.calls[fetch[0]].argv[-1] == str(out / "metrics" / f"{name}.ndjson.gz")
        assert gzip.decompress((out / "metrics" / f"{name}.ndjson.gz").read_bytes())
    # Vector stops on every host before the first wait: the stops overlap.
    stops = [i for i in fx.find("systemctl stop --no-block whitney-vector.service") if i > runner]
    assert len(stops) == 5 and max(stops) < min(fx.find("gzip -f"))
    assert not fx.find("gzip -kf")  # the raw NDJSON does not stay on the root disk
    doc = json.loads((out / "metrics.json").read_text())
    assert doc == {"remote_write": False, "remote_write_host": None,
                   "vector_version": VECTOR_VERSION, "scrape_interval_secs": 1}


def test_vector_start_failure_fails_the_run(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("is-active --quiet whitney-vector.service",
          Reply(rc=1, stdout="whitney-vector is not active\n"))
    r = run_once(scenario(), cluster(tmp_path, 1), 1, 1, tmp_path / "run", fx)
    assert r.status == "failed" and "whitney-vector is not active" in r.error
    assert not fx.find("whitney-runner")


def test_metrics_collect_failure_fails_the_run(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    fx.on("gzip -f", Reply(rc=1, stderr="gzip: no such file\n"))
    r = run_once(scenario(), cluster(tmp_path, 1), 1, 1, tmp_path / "run", fx)
    assert r.status == "failed" and "metrics" in r.error


def test_restart_stops_vector_on_every_host():
    from whitney.remote import _RESTART
    # --no-block: a Vector left by a cut-short run stops while the rest goes on.
    assert "sudo systemctl stop --no-block whitney-vector.service" in _RESTART


def test_vector_settles_once_per_run(tmp_path, fx, scenario, monkeypatch):
    import whitney.remote
    sleeps: list[float] = []
    monkeypatch.setattr(whitney.remote, "VECTOR_SETTLE_SECONDS", 1.5)
    monkeypatch.setattr(whitney.remote.time, "sleep", sleeps.append)
    install_cluster(fx, 2)
    r = run_once(scenario(), cluster(tmp_path), 2, 1, tmp_path / "run", fx)
    assert r.status == "ok", r.error
    assert sleeps == [1.5]


def test_single_host_metrics_file_is_named_single(tmp_path, fx, scenario):
    install_cluster(fx, 1)
    c = single_cluster(tmp_path / "wd")
    c.workdir.mkdir()
    out = tmp_path / "run"
    r = run_once(scenario(*SINGLE), c, 2, 1, out, fx)
    assert r.status == "ok", r.error
    assert len(fx.find("systemctl restart whitney-vector.service")) == 1
    assert [p.name for p in (out / "metrics").iterdir()] == ["single.ndjson.gz"]


# ---- remote write: push ---------------------------------------------------------------


def _rw() -> RemoteWrite:
    return remote_write_from_env(URL, {USER_ENV: "123456", PASS_ENV: SECRET})


def test_remote_env_pushed_to_every_host_before_vector(tmp_path, fx, scenario):
    install_cluster(fx, 2)
    pushed: list[tuple[str, str, int]] = []

    def capture(argv):
        if argv[0] == "scp" and ":" in argv[-1]:
            src = Path(argv[-2])
            pushed.append((argv[-1], src.read_text(), src.stat().st_mode & 0o777))
    fx.on("scp", Reply(effect=lambda a: capture(a) or fake_scp(a)))
    out = tmp_path / "20261022-1000-exec-scaling-v0.1.31-n2-r1"
    r = run_once(scenario(), cluster(tmp_path), 2, 1, out, fx, remote_write=_rw())
    assert r.status == "ok", r.error
    remote = [p for p in pushed if "remote" in p[0]]
    assert len(remote) == 5
    # An unguessable name in the home dir, not a fixed /tmp path.
    names = {p[0].split(":", 1)[1] for p in remote}
    assert len(names) == 1 and re.fullmatch(r"\.whitney-remote-[0-9a-f]{16}\.env", names.pop())
    assert all(text == _rw().env_file() and mode == 0o600 for _, text, mode in remote)
    installs = fx.find("sudo install -m 0600 -o root -g root")
    starts = fx.find("systemctl restart whitney-vector.service")
    assert len(installs) == 5
    for i in installs:
        ip = ip_of(fx.calls[i])
        assert any(j > i and ip_of(fx.calls[j]) == ip for j in starts)
        assert "/etc/whitney/vector/remote.env" in fx.calls[i].line
    assert all("rm -f /etc/whitney/vector/remote.env" not in fx.calls[i].line for i in starts)
    # The secret is only in the pushed file: never in argv or results.
    assert SECRET not in all_text(fx, out)
    doc = json.loads((out / "metrics.json").read_text())
    assert doc["remote_write"] is True and doc["remote_write_host"] == "prometheus.example.net"
    assert not list(tmp_path.rglob("*remote.env*"))  # local temp file removed


# ---- CLI ---------------------------------------------------------------------------------


CLUSTER_ARGS = ["--bucket", "b", "--ssh-key-name", "k", "--operator-cidr", "198.51.100.7/32"]


def _scenario_file(tmp_path) -> Path:
    p = tmp_path / "s.toml"
    p.write_text(SCENARIO)
    return p


@pytest.mark.parametrize("cmd", ["run", "sweep"])
def test_cli_rejects_bad_remote_write_before_spend(tmp_path, fx, monkeypatch, capsys, cmd):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setenv(USER_ENV, "u")
    monkeypatch.delenv(PASS_ENV, raising=False)
    argv = [cmd, str(_scenario_file(tmp_path)), *(["--n", "1"] if cmd == "run" else []),
            *CLUSTER_ARGS, "--metrics-remote-write", URL]
    assert main(argv, ex=fx) == 2
    assert "go together" in capsys.readouterr().err
    assert not fx.calls  # no build, no AWS, no tofu


def test_cli_passes_remote_write_and_redacts_command(tmp_path, fx, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    monkeypatch.delenv(USER_ENV, raising=False)
    monkeypatch.delenv(PASS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    install_cluster(fx, 1)
    archive = make_archive(tmp_path / "prebuilt", KEY)
    rc = main(["run", str(_scenario_file(tmp_path)), "--n", "1", *CLUSTER_ARGS,
               "--archive", str(archive), "--metrics-remote-write", URL], ex=fx)
    assert rc == 0
    run_dir = next((tmp_path / "results").iterdir())
    cmd = (run_dir / "command.txt").read_text()
    assert "--metrics-remote-write https://prometheus.example.net/REDACTED" in cmd
    assert "/api/prom/push" not in cmd
    assert SECRET not in all_text(fx, tmp_path)
    assert len(fx.find("sudo install -m 0600 -o root -g root")) == 4
    assert not list((tmp_path / "cache").rglob("*.tfvars.json")) or \
        all(SECRET not in p.read_text() for p in (tmp_path / "cache").rglob("*.tfvars.json"))


def test_cli_flag_abbreviations_rejected(tmp_path, fx, capsys):
    """An abbreviated flag would get past the command.txt redaction."""
    with pytest.raises(SystemExit) as e:
        main(["run", str(_scenario_file(tmp_path)), "--n", "1", *CLUSTER_ARGS,
              "--metrics-remote", URL], ex=fx)
    assert e.value.code == 2 and not fx.calls


def test_sweep_passes_remote_write(tmp_path, fx, scenario):
    install_cluster(fx, 3)
    results = sweep(scenario("n_list=[1]"), opts(tmp_path, remote_write=_rw()), fx, seed=1)
    assert [r.status for r in results] == ["ok"]
    assert json.loads((results[0].run_dir / "metrics.json").read_text())["remote_write"]
    assert fx.find("sudo install -m 0600 -o root -g root")


def test_run_without_flag_ignores_env(tmp_path, fx, scenario, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, SECRET)
    install_cluster(fx, 1)
    r = run_scenario(scenario(), 1, opts(tmp_path), fx, command=["whitney", "run"])
    assert r.status == "ok"
    assert not fx.find("install -m 0600")
    assert json.loads((r.run_dir / "metrics.json").read_text())["remote_write"] is False
