import json
import math
import shutil
from dataclasses import fields, replace
from pathlib import Path

import pytest
from make_fixtures import scenario, write_run

import whitney.chart
from whitney.chart import scaling_chart
from whitney.cli import main
from whitney.config import Scenario
from whitney.results import GROUP_FIELDS, group_key, load_runs, summarize, write_summary

FIX = Path(__file__).parent / "fixtures"


def test_warmup_excluded():
    runs = load_runs(FIX)
    ok = [r for r in runs if r.status == "ok"]
    assert len(ok[0].trials) == 33  # 3 batch sizes x (1 warm-up + 10 timed)
    (p,) = summarize(runs, 1)
    assert p.samples == 10
    assert p.block_min_tps == 100.0  # the 1.0 warm-up block is not in the stats


def test_failed_counted_not_used():
    runs = load_runs(FIX)
    failed = [r for r in runs if r.status == "failed"]
    assert len(failed) == 1 and failed[0].trials == []
    assert failed[0].error.startswith("cluster apply failed")
    (p,) = summarize(runs, 1)
    assert (p.failed_runs, p.samples) == (1, 10)


def test_median_minmax(tmp_path):
    s = scenario(tmp_path, timed_blocks=4)
    write_run(tmp_path, "r/20261021-1000-x-v1-n3-r1", s, tps=[1, 2, 3, 3])
    write_run(tmp_path, "r/20261021-1010-x-v1-n3-r2", s, tps=[4, 10, 10, 11])
    write_run(tmp_path, "r/20261021-1020-x-v1-n3-r3", s, tps=[40, 50, 60, 70])
    (p,) = summarize(load_runs(tmp_path), 1)
    # medians 2.5, 10, 55 -> median 10
    assert p.per_rep_median == [(1, 2.5), (2, 10.0), (3, 55.0)]
    assert (p.median_tps, p.min_tps, p.max_tps) == (10.0, 2.5, 55.0)
    assert (p.block_min_tps, p.block_max_tps) == (1.0, 70.0)
    assert p.samples == 12


def test_batch_size_filter(tmp_path):
    s = scenario(tmp_path)
    write_run(tmp_path, "20261021-1000-x-v1-n1-r1", s, tps=10, batch_size=5)
    (p,) = summarize(load_runs(tmp_path), 5)
    assert p.samples == 5 and p.median_tps == 10.0
    (p,) = summarize(load_runs(tmp_path), 1)
    assert p.samples == 5 and p.median_tps == 100.0
    (p,) = summarize(load_runs(tmp_path), 7)  # not in the scenario
    assert p.samples == 0 and math.isnan(p.median_tps)


def test_finds_both_depths(tmp_path):
    s = scenario(tmp_path)
    write_run(tmp_path, "20261021-1000-x-v1-n1-r1", s, tps=1)
    write_run(tmp_path / "20261021-1100-x-v1-sweep", "20261021-1100-x-v1-n2-r1", s, tps=1)
    assert sorted(r.n for r in load_runs(tmp_path)) == [1, 2]


def test_older_run_dir_is_refused(tmp_path):
    """A resolved scenario without a field predates it: today's default may
    not be what that run used, so it is never read."""
    d = write_run(tmp_path, "20261021-1000-x-v1-n1-r1", scenario(tmp_path), tps=1)
    toml = d / "scenario.resolved.toml"
    toml.write_text("".join(x for x in toml.read_text().splitlines(True)
                            if not x.startswith("exec_allocator")))
    with pytest.raises(ValueError, match="written by an older whitney, no exec_allocator"):
        load_runs(tmp_path)


_X8AEDZ = {"runner": "r8a.16xlarge", "exec": "r8a.16xlarge", "db": "x8aedz.12xlarge",
           "event": "x8aedz.12xlarge", "indexer": "x8aedz.12xlarge", "single": "m8a.48xlarge"}


def test_three_storage_modes_group_together(tmp_path):
    """Same hosts, three storage modes: one summary, one series per mode."""
    for i, storage in enumerate(("tmpfs", "ebs", "nvme")):
        write_run(tmp_path, f"20261021-100{i}-x-v1-n1-r1",
                  scenario(tmp_path, storage=storage, instance_types=_X8AEDZ), tps=i + 1)
    pts = summarize(load_runs(tmp_path), 1)
    assert [(p.storage, p.median_tps) for p in pts] == [("ebs", 2.0), ("nvme", 3.0),
                                                         ("tmpfs", 1.0)]
    out = tmp_path / "chart"
    scaling_chart({s: [p for p in pts if p.storage == s] for s in ("tmpfs", "ebs", "nvme")}, out)
    svg = out.with_name("chart.svg").read_text()
    assert all(s in svg for s in ("tmpfs", "ebs", "nvme"))


def test_chart_has_a_fixed_color_per_storage_mode():
    from whitney.chart import COLORS
    from whitney.config import STORAGE
    assert set(COLORS) == set(STORAGE) and len(set(COLORS.values())) == len(STORAGE)


# ---- which runs pool --------------------------------------------------------------


def test_group_fields_cover_every_scenario_field():
    """Runs pool only if every scenario field matches, except the axes and
    placement. A new Scenario field must be added to GROUP_FIELDS or here."""
    free = {"n_list", "exec_cores", "repetitions", "region", "availability_zone", "ami_id",
            "hogd_ref"}  # archive_sha256 stands for the build, so it covers hogd_ref
    assert set(GROUP_FIELDS) == {f.name for f in fields(Scenario)} - free | {"archive_sha256"}


@pytest.mark.parametrize("a,b,sha_b,field", [
    ({}, {"accounts": 7}, "a" * 64, "accounts"),
    ({}, {}, "b" * 64, "archive_sha256"),
    # Among ebs runs the volume must match.
    ({"storage": "ebs"}, {"storage": "ebs", "ebs_iops": 8000}, "a" * 64, "ebs_iops"),
])
def test_refuses_mixed_groups(tmp_path, a, b, sha_b, field):
    write_run(tmp_path, "20261021-1000-x-v1-n1-r1", scenario(tmp_path, **a), tps=1)
    write_run(tmp_path, "20261021-1001-x-v1-n1-r2", scenario(tmp_path, **b), tps=1, sha=sha_b)
    with pytest.raises(ValueError, match=f"mixed runs: differing fields: {field}$"):
        summarize(load_runs(tmp_path), 1)


def test_ebs_knobs_ignored_outside_ebs_mode(tmp_path):
    """tmpfs runs have no volume; their ebs_* values do not split the group."""
    write_run(tmp_path, "20261021-1000-x-v1-n1-r1", scenario(tmp_path), tps=1)
    write_run(tmp_path, "20261021-1001-x-v1-n1-r2",
              scenario(tmp_path, ebs_volume_type="io2", ebs_iops=64000), tps=3)
    write_run(tmp_path, "20261021-1002-x-v1-n1-r3",
              scenario(tmp_path, storage="ebs", ebs_iops=8000), tps=5)
    pts = summarize(load_runs(tmp_path), 1)
    assert [(p.storage, p.median_tps) for p in pts] == [("ebs", 5.0), ("tmpfs", 2.0)]


def test_group_key_is_deterministic_for_dicts(tmp_path):
    a = scenario(tmp_path, storage="tmpfs", single_host=True,
                 cpu_layout={"runner": "0-15", "event": "16-31"})
    b = replace(a, cpu_layout={"event": "16-31", "runner": "0-15"})
    write_run(tmp_path, "20261021-1000-x-v1-n1-r1", a, tps=1)
    write_run(tmp_path, "20261021-1001-x-v1-n1-r2", b, tps=1)
    ra, rb = load_runs(tmp_path)
    assert group_key(ra) == group_key(rb)
    hash(group_key(ra))
    (p,) = summarize([ra, rb], 1)
    assert p.samples == 10


def test_axes_may_differ(tmp_path):
    """n_list, exec_cores, and repetitions are axes: runs of other lists pool."""
    write_run(tmp_path, "20261021-1000-x-v1-n1-r1", scenario(tmp_path), tps=1)
    write_run(tmp_path, "20261021-1001-x-v1-n1-r2",
              scenario(tmp_path, n_list=[1, 8], repetitions=3), tps=3)
    (p,) = summarize(load_runs(tmp_path), 1)
    assert p.median_tps == 2.0
    write_run(tmp_path, "c/20261021-1000-x-v1-n1-c2-r1", _cores_scenario(tmp_path), tps=2)
    write_run(tmp_path, "c/20261021-1010-x-v1-n1-c2-r2",
              _cores_scenario(tmp_path, exec_cores=[2, 64]), tps=4)
    (p,) = summarize(load_runs(tmp_path / "c"), 1)
    assert (p.cores, p.median_tps) == (2, 3.0)


# ---- charts and the summary files ---------------------------------------------------


def _svg_text(tmp_path, pts):
    out = tmp_path / "chart"
    scaling_chart({"nvme": pts}, out)
    assert out.with_name("chart.png").stat().st_size > 0
    return out.with_name("chart.svg").read_text()


@pytest.mark.parametrize("indexer,expect", [(False, "+ 3 support hosts"), (True, "+ 4 support hosts")])
def test_x_label_names_support_hosts(tmp_path, indexer, expect):
    write_run(tmp_path / "r", "20261021-1000-x-v1-n1-r1", scenario(tmp_path, indexer=indexer), tps=5)
    svg = _svg_text(tmp_path, summarize(load_runs(tmp_path / "r"), 1))
    assert expect in svg and "Executor hosts (N)" in svg and "ERC20 payments/sec" in svg


def test_chart_marks_failed_point(tmp_path, monkeypatch):
    s = scenario(tmp_path)
    r = tmp_path / "r"
    write_run(r, "20261021-1000-x-v1-n1-r1", s, tps=6)
    write_run(r, "20261021-1001-x-v1-n2-r1", s, status="failed", error="boom")
    write_run(r, "20261021-1002-x-v1-n3-r1", s, tps=10)
    segments = []
    real = whitney.chart._line

    def spy(ax, seg, color, label=None, **kw):
        segments.append([p.n for p in seg])
        real(ax, seg, color, label, **kw)

    monkeypatch.setattr(whitney.chart, "_line", spy)
    svg = _svg_text(tmp_path, summarize(load_runs(r), 1))
    assert [sg for sg in segments if sg] == [[1], [3]]  # no line or band through N=2
    assert "1 failed" in svg


def test_single_series_chart_has_no_series_name(tmp_path):
    s = scenario(tmp_path)
    r = tmp_path / "r"
    write_run(r, "20261021-1000-x-v1-n1-r1", s, tps=6)
    write_run(r, "20261021-1001-x-v1-n2-r1", s, tps=10)
    write_run(r, "20261021-1002-x-v1-n3-r1", s, status="failed", error="boom")
    svg = _svg_text(tmp_path, summarize(load_runs(r), 1))  # one series, named nvme
    assert "nvme" not in svg
    assert "1 failed" in svg and "linear" in svg


def test_two_series_chart_names_linear_once_and_has_no_legend(tmp_path):
    r = tmp_path / "r"
    write_run(r, "20261021-1000-x-v1-n1-r1", scenario(tmp_path), tps=6)
    write_run(r, "20261021-1001-x-v1-n2-r1", scenario(tmp_path), tps=12)
    write_run(r, "20261021-1002-x-v1-n1-r2", scenario(tmp_path, storage="ebs"), tps=5)
    write_run(r, "20261021-1003-x-v1-n2-r2", scenario(tmp_path, storage="ebs"), tps=10)
    by = {}
    for p in summarize(load_runs(r), 1):
        by.setdefault(p.storage, []).append(p)
    scaling_chart(by, tmp_path / "chart")
    svg = (tmp_path / "chart.svg").read_text()
    assert svg.count(">linear<") <= 1 and svg.count("linear") >= 1
    assert "legend" not in svg


def test_write_summary(tmp_path):
    pts = summarize(load_runs(FIX), 1)
    write_summary(pts, tmp_path / "sum")
    assert (tmp_path / "sum.csv").read_text().splitlines()[1].startswith("nvme,2,,,false")
    assert "| nvme | 2 |" in (tmp_path / "sum.md").read_text()


def test_cli_summarize_and_chart(tmp_path, capsys):
    root = tmp_path / "res"
    shutil.copytree(FIX, root)
    assert main(["summarize", str(root), "--batch-size", "1"]) == 0
    assert "nvme" in capsys.readouterr().out
    assert main(["chart", str(root), "--batch-size", "1"]) == 0
    assert (root / "scaling-b1.svg").is_file() and (root / "scaling-b1.png").is_file()


def test_cli_chart_single_host_exits_2(tmp_path, capsys):
    write_run(tmp_path, "20261021-1000-x-v1-n1-r1", scenario(tmp_path, single_host=True,
              storage="tmpfs"), tps=5)
    assert main(["chart", str(tmp_path), "--batch-size", "1"]) == 2
    assert "single-host" in capsys.readouterr().err


# ---- result validation --------------------------------------------------------------


def _edit_trials(d: Path, fn) -> None:
    """Rewrite trials.jsonl: fn gets the row dicts and returns the new list."""
    tf = d / "trials.jsonl"
    rows = [json.loads(x) for x in tf.read_text().splitlines()]
    tf.write_text("".join(json.dumps(r) + "\n" for r in fn(rows)))


def _set(i, **kw):
    def fn(rows):
        rows[i].update(kw)
        return rows
    return fn


# Default scenario: batch sizes 1, 5, 10; 0 warm-up + 5 timed blocks each;
# 1,000,000 txs; 100 accounts. Rows 0-4 are batch 1, 5-9 batch 5.
@pytest.mark.parametrize("edit,reason", [
    (lambda rows: rows[:4] + rows[5:], "batch size 1: 4 trials, want 5"),
    (lambda rows: rows[:5] + rows[10:], "batch size 5: 0 trials, want 5"),
    (lambda rows: rows + [dict(rows[0], batch_size=7)], "batch size 7 is not in the scenario"),
    (_set(2, block_index=7), "batch size 1: block indexes [0, 1, 7, 3, 4], want 0..4"),
    (_set(0, warmup=True), "batch size 1 block 0: warmup is true, want false"),
    (_set(1, txs=999_999), "batch size 1 block 1: txs 999999 != txs_per_block 1000000"),
    (_set(1, nexec=2), "batch size 1 block 1: nexec 2 != N 1"),
    (_set(1, accounts=7), "batch size 1 block 1: accounts 7 != 100"),
    (_set(1, experiment="erc721"), "batch size 1 block 1: experiment erc721 != erc20_payments"),
    (_set(1, tps=200.0), "batch size 1 block 1: tps 200.0 does not match txs / elapsed_ns"),
    (_set(1, tps=None), "batch size 1 block 1: tps must be a number"),
    (_set(1, elapsed_ns=0), "batch size 1 block 1: elapsed_ns must be > 0"),
    (lambda rows: [{k: v for k, v in r.items() if k != "accounts"} for r in rows],
     "batch size 1 block 0: no accounts"),
])
def test_inconsistent_ok_run_is_invalid(tmp_path, edit, reason):
    s = scenario(tmp_path)
    d = write_run(tmp_path / "r", "20261021-1000-x-v1-n1-r1", s, tps=100)
    write_run(tmp_path / "r", "20261021-1001-x-v1-n1-r2", s, tps=50)
    _edit_trials(d, edit)
    bad, good = load_runs(tmp_path / "r")
    assert (bad.status, good.status) == ("invalid", "ok")
    assert bad.error == reason
    (p,) = summarize([bad, good], 1)
    assert p.failed_runs == 1 and p.samples == 5 and p.median_tps == 50.0
    assert p.invalid_runs == [(bad.dir.name, reason)]


def test_warmup_flags_checked(tmp_path):
    s = scenario(tmp_path, warmup_blocks=2, timed_blocks=3)
    d = write_run(tmp_path, "20261021-1000-x-v1-n1-r1", s)
    assert load_runs(tmp_path)[0].status == "ok"
    _edit_trials(d, _set(1, warmup=False))
    (r,) = load_runs(tmp_path)
    assert r.status == "invalid" and r.error == "batch size 1 block 1: warmup is false, want true"


def test_ok_run_without_trials_is_invalid(tmp_path):
    d = write_run(tmp_path, "20261021-1000-x-v1-n1-r1", scenario(tmp_path))
    (d / "trials.jsonl").unlink()
    (r,) = load_runs(tmp_path)
    assert r.status == "invalid" and r.error == "no trials.jsonl"


def test_ok_run_with_bad_json_is_invalid(tmp_path):
    d = write_run(tmp_path, "20261021-1000-x-v1-n1-r1", scenario(tmp_path))
    with open(d / "trials.jsonl", "a") as f:
        f.write("{not json\n")
    (r,) = load_runs(tmp_path)
    assert r.status == "invalid" and r.error.startswith("trials.jsonl line 16 is not a JSON object")


def test_tps_tolerates_truncated_elapsed_ns(tmp_path):
    """The runner truncates elapsed_ns but computes tps from the exact time."""
    d = write_run(tmp_path, "20261021-1000-x-v1-n1-r1", scenario(tmp_path))
    exact = 1_234_567_890.75
    edit = _set(0, elapsed_ns=int(exact), tps=1_000_000 * 1e9 / exact)
    _edit_trials(d, lambda rows: _with_phases(edit(rows)))
    assert load_runs(tmp_path)[0].status == "ok"


def test_summary_lists_invalid_runs(tmp_path):
    s = scenario(tmp_path)
    d = write_run(tmp_path / "r", "20261021-1000-x-v1-n1-r1", s)
    write_run(tmp_path / "r", "20261021-1001-x-v1-n1-r2", s)
    _edit_trials(d, lambda rows: rows[:1])
    write_summary(summarize(load_runs(tmp_path / "r"), 1), tmp_path / "sum")
    md = (tmp_path / "sum.md").read_text()
    assert "Invalid runs (not used):" in md
    assert "20261021-1000-x-v1-n1-r1: batch size 1: 1 trials, want 5" in md
    header, row = (tmp_path / "sum.csv").read_text().splitlines()[:2]
    assert header.endswith(",failed_runs,invalid_runs") and row.endswith(",1,1")


def test_unreadable_status_is_a_failed_run(tmp_path):
    s = scenario(tmp_path)
    d = write_run(tmp_path, "20261021-1000-x-v1-n1-r1", s)
    write_run(tmp_path, "20261021-1001-x-v1-n1-r2", s)
    (d / "status.json").write_text('{"status": "o')
    bad, good = load_runs(tmp_path)
    assert (bad.status, bad.error, bad.trials) == ("failed", "unreadable status.json", [])
    (p,) = summarize([bad, good], 1)
    assert p.failed_runs == 1 and p.samples == 5


def test_per_exec_block_size_and_block_time(tmp_path):
    s = scenario(tmp_path, txs_per_block_per_exec=500_000, txs_per_block=1_000_000)
    write_run(tmp_path, "r/20261021-1000-x-v1-n2-r1", s, tps=2_000_000)
    write_run(tmp_path, "r/20261021-1010-x-v1-n4-r1", s, tps=4_000_000)
    p2, p4 = summarize(load_runs(tmp_path), 1)
    assert (p2.n, p2.block_txs, p2.median_block_ms) == (2, 1_000_000, 500.0)
    assert (p4.n, p4.block_txs, p4.median_block_ms) == (4, 2_000_000, 500.0)
    write_summary([p2, p4], tmp_path / "summary")
    rows = (tmp_path / "summary.csv").read_text().splitlines()
    assert rows[0].startswith("storage,n,exec_cores,procs_per_host,indexer,single_host,block_txs,"
                              "median_block_ms,median_tps,")
    assert rows[2].startswith("tmpfs,4,,,false,false,2000000,500.0,4000000.0,")


def test_per_exec_trials_must_match_their_n(tmp_path):
    s = scenario(tmp_path, txs_per_block_per_exec=500_000)
    d = write_run(tmp_path / "r", "20261021-1000-x-v1-n2-r1", s, tps=100)
    _edit_trials(d, lambda rows: [dict(r, txs=500_000) for r in rows])
    (run,) = load_runs(tmp_path / "r")
    assert run.status == "invalid"
    assert run.error == ("batch size 1 block 0: txs 500000 != "
                         "txs_per_block_per_exec × N 1000000")


def _with_phases(rows, open_ns=1_000_000, close_ns=40_000_000, cpu_ns=None):
    """Set the runner phase fields: the rest of elapsed_ns is the propose phase."""
    out = []
    for r in rows:
        e = r["elapsed_ns"]
        out.append(dict(r, open_ns=open_ns, exec_ns=e - open_ns - close_ns, close_ns=close_ns,
                        runner_cpu_ns=cpu_ns if cpu_ns is not None else 10 * e))
    return out


def test_phase_fields_in_summary(tmp_path):
    s = scenario(tmp_path, timed_blocks=2)
    # 1M txs at 1M tps: 1 s blocks, 1 ms open, 40 ms close, 959 ms propose.
    d = write_run(tmp_path / "r", "20261021-1000-x-v1-n2-r1", s, tps=1_000_000)
    _edit_trials(d, _with_phases)
    (p,) = summarize(load_runs(tmp_path / "r"), 1)
    assert (p.median_open_ms, p.median_close_ms) == (1.0, 40.0)
    assert p.median_exec_tps == pytest.approx(1e6 / 0.959)
    assert p.median_runner_cores == 10.0
    write_summary([p], tmp_path / "summary")
    head, row = (tmp_path / "summary.csv").read_text().splitlines()
    cols = dict(zip(head.split(","), row.split(",")))
    assert (cols["median_open_ms"], cols["median_close_ms"], cols["median_runner_cores"]) == \
           ("1.0", "40.0", "10.00")


@pytest.mark.parametrize("edit,reason", [
    (lambda rows: [{k: v for k, v in r.items() if k != "close_ns"} for r in rows],
     "batch size 1 block 0: no close_ns"),
    (lambda rows: _with_phases(rows, close_ns=-1),
     "batch size 1 block 0: open_ns, exec_ns, close_ns, runner_cpu_ns must be ints >= 0"),
    (lambda rows: [dict(r, exec_ns=r["exec_ns"] + 1) for r in _with_phases(rows)],
     "batch size 1 block 0: open_ns + exec_ns + close_ns > elapsed_ns"),
])
def test_bad_phase_fields_make_run_invalid(tmp_path, edit, reason):
    s = scenario(tmp_path)
    d = write_run(tmp_path / "r", "20261021-1000-x-v1-n2-r1", s, tps=100)
    _edit_trials(d, edit)
    (run,) = load_runs(tmp_path / "r")
    assert (run.status, run.error) == ("invalid", reason)


# ---- exec cores -----------------------------------------------------------------------


def _cores_scenario(tmp_path, **kw):
    kw = {"n_list": [1], "exec_cores": [1, 2, 4], "txs_per_block_per_core": 100_000, **kw}
    return scenario(tmp_path, **kw)


def test_core_runs_parse_and_group(tmp_path):
    s = _cores_scenario(tmp_path)
    for k, tps in ((4, 400), (1, 100), (2, 200)):
        for rep in (1, 2):
            write_run(tmp_path, f"r/20261021-1{k}0{rep}-x-v1-n1-c{k}-r{rep}", s, tps=tps * rep)
    runs = load_runs(tmp_path)
    assert sorted((r.n, r.cores, r.rep) for r in runs) == \
        [(1, k, rep) for k in (1, 2, 4) for rep in (1, 2)]
    assert all(r.status == "ok" for r in runs)
    pts = summarize(runs, 1)
    assert [(p.n, p.cores, p.block_txs, p.median_tps) for p in pts] == \
        [(1, 1, 100_000, 150.0), (1, 2, 200_000, 300.0), (1, 4, 400_000, 600.0)]
    write_summary(pts, tmp_path / "sum")
    head, *rows = (tmp_path / "sum.csv").read_text().splitlines()
    assert head.startswith("storage,n,exec_cores,procs_per_host,indexer,")
    assert [r.split(",")[:4] for r in rows] == [["tmpfs", "1", k, ""] for k in ("1", "2", "4")]
    assert "| tmpfs | 1 | 4 |" in (tmp_path / "sum.md").read_text()


def test_core_trials_must_match_their_k(tmp_path):
    s = _cores_scenario(tmp_path)
    d = write_run(tmp_path / "r", "20261021-1000-x-v1-n1-c4-r1", s, tps=100)
    _edit_trials(d, lambda rows: [dict(r, txs=100_000) for r in rows])
    (run,) = load_runs(tmp_path / "r")
    assert (run.status, run.error) == ("invalid", "batch size 1 block 0: txs 100000 != "
                                                  "txs_per_block_per_core × N × K 400000")


@pytest.mark.parametrize("name,kw", [
    ("20261021-1000-x-v1-n1-r1", {"exec_cores": [1, 2]}),  # no -c with exec_cores
    ("20261021-1000-x-v1-n1-c2-r1", {}),  # -c without exec_cores
])
def test_run_dir_and_scenario_must_agree_on_cores(tmp_path, name, kw):
    d = write_run(tmp_path, "20261021-1000-x-v1-n1-r1", scenario(tmp_path, **kw), tps=1)
    d.rename(tmp_path / name)
    with pytest.raises(ValueError, match="run dir name and scenario disagree on exec_cores"):
        load_runs(tmp_path)


def test_chart_core_axis_and_linear_line(tmp_path, monkeypatch):
    s = _cores_scenario(tmp_path)
    for k, tps in ((1, 100), (2, 190), (4, 350)):
        write_run(tmp_path, f"r/20261021-100{k}-x-v1-n1-c{k}-r1", s, tps=tps)
    lines = []
    real = whitney.chart._linear

    def spy(ax, first, xs, x, color, **kw):
        lines.append((x(first), xs))
        real(ax, first, xs, x, color, **kw)

    monkeypatch.setattr(whitney.chart, "_linear", spy)
    svg = _svg_text(tmp_path, summarize(load_runs(tmp_path / "r"), 1))
    assert "hog-exec cores (one executor host)" in svg and "Executor hosts" not in svg
    assert "linear" in svg
    assert lines == [(1, [1, 2, 4])]


def test_chart_n_axis_has_linear_line_from_smallest_n(tmp_path, monkeypatch):
    s = scenario(tmp_path)
    for n in (2, 3):
        write_run(tmp_path, f"r/20261021-100{n}-x-v1-n{n}-r1", s, tps=10 * n)
    lines = []
    monkeypatch.setattr(whitney.chart, "_linear",
                        lambda ax, first, xs, x, color, **kw: lines.append((x(first), xs)))
    svg = _svg_text(tmp_path, summarize(load_runs(tmp_path / "r"), 1))
    assert "Executor hosts (N)" in svg
    assert lines == [(2, [2, 3])]


def test_chart_refuses_n_and_cores_together(tmp_path):
    s = scenario(tmp_path, n_list=[1, 2], exec_cores=[1, 2], txs_per_block_per_core=100_000)
    for n, k in ((1, 1), (2, 2)):
        write_run(tmp_path, f"r/20261021-10{n}{k}-x-v1-n{n}-c{k}-r1", s, tps=1)
    pts = summarize(load_runs(tmp_path / "r"), 1)
    with pytest.raises(ValueError, match="vary in both N and exec cores: chart one axis at a time"):
        scaling_chart({"tmpfs": pts}, tmp_path / "c")
    assert main(["chart", str(tmp_path / "r"), "--batch-size", "1"]) == 2


def test_chart_n_axis_at_one_core_count(tmp_path):
    s = scenario(tmp_path, n_list=[1, 2], exec_cores=[8], txs_per_block_per_core=100_000)
    for n in (1, 2):
        write_run(tmp_path, f"r/20261021-100{n}-x-v1-n{n}-c8-r1", s, tps=n)
    svg = _svg_text(tmp_path, summarize(load_runs(tmp_path / "r"), 1))
    assert "Executor hosts (N)" in svg


# ---- one hog-exec per L3 ---------------------------------------------------------------

R8A_L3 = [f"{g * 8}-{g * 8 + 7}" for g in range(8)]


def _l3_scenario(tmp_path, **kw):
    kw = {"exec_cores": [4, 8, 16, 64], "exec_per_l3": True, **kw}
    return _cores_scenario(tmp_path, **kw)


def test_l3_runs_expect_n_times_procs_executors(tmp_path):
    s = _l3_scenario(tmp_path, n_list=[1, 2])
    for k, procs in ((4, 1), (8, 1), (16, 2), (64, 8)):
        write_run(tmp_path, f"r/20261021-10{k:02d}-x-v1-n2-c{k}-r1", s, tps=k, procs=procs,
                  l3=R8A_L3)
    runs = load_runs(tmp_path / "r")
    assert [(r.cores, r.procs, r.status) for r in sorted(runs, key=lambda r: r.cores)] == \
        [(4, 1, "ok"), (8, 1, "ok"), (16, 2, "ok"), (64, 8, "ok")]
    assert {r.trials[0]["nexec"] for r in runs if r.cores == 64} == {16}  # N 2 × 8
    pts = summarize(runs, 1)
    assert [(p.cores, p.procs, p.per_l3) for p in pts] == \
        [(4, 1, True), (8, 1, True), (16, 2, True), (64, 8, True)]
    write_summary(pts, tmp_path / "sum")
    head, *rows = (tmp_path / "sum.csv").read_text().splitlines()
    assert head.startswith("storage,n,exec_cores,procs_per_host,indexer,")
    assert [r.split(",")[:4] for r in rows] == \
        [["tmpfs", "2", "4", "1"], ["tmpfs", "2", "8", "1"], ["tmpfs", "2", "16", "2"],
         ["tmpfs", "2", "64", "8"]]


@pytest.mark.parametrize("procs,l3,reason", [
    # The runner started one hog-exec per host, not one per L3.
    (1, R8A_L3, "batch size 1 block 0: nexec 2 != N 2 × 2 hog-execs per host"),
    (2, None, "exec_per_l3 run without an L3 record in exec-setup.json"),
    # With a 16-CPU L3, K = 16 is one hog-exec per host.
    (2, ["0-15", "16-31"], "batch size 1 block 0: nexec 4 != N 2"),
])
def test_l3_trials_must_match_the_layout(tmp_path, procs, l3, reason):
    write_run(tmp_path / "r", "20261021-1000-x-v1-n2-c16-r1", _l3_scenario(tmp_path),
              procs=procs, l3=l3)
    (run,) = load_runs(tmp_path / "r")
    assert (run.status, run.error) == ("invalid", reason)


@pytest.mark.parametrize("doc", [
    "not json", "[]", '{"exec0": {"l3": []}}',
    '{"exec0": {"l3": ["0-x"]}}', '{"exec0": {"l3": ["0-7"]}, "exec1": {"l3": ["0-15"]}}',
])
def test_bad_l3_record_makes_run_invalid(tmp_path, doc):
    d = write_run(tmp_path / "r", "20261021-1000-x-v1-n2-c16-r1", _l3_scenario(tmp_path),
                  procs=2, l3=R8A_L3)
    (d / "exec-setup.json").write_text(doc)
    (run,) = load_runs(tmp_path / "r")
    assert (run.status, run.error) == \
        ("invalid", "exec_per_l3 run without an L3 record in exec-setup.json")


def test_runs_without_exec_per_l3_need_no_exec_setup(tmp_path):
    write_run(tmp_path / "r", "20261021-1000-x-v1-n2-c4-r1", _cores_scenario(tmp_path))
    (run,) = load_runs(tmp_path / "r")
    assert (run.status, run.procs) == ("ok", 1)
    (p,) = summarize([run], 1)
    assert p.procs is None and not p.per_l3


def test_failed_l3_run_counts_without_a_record(tmp_path):
    s = _l3_scenario(tmp_path)
    write_run(tmp_path, "r/20261021-1000-x-v1-n1-c16-r1", s, procs=2, l3=R8A_L3)
    write_run(tmp_path, "r/20261021-1010-x-v1-n1-c16-r2", s, status="failed", error="boom")
    (p,) = summarize(load_runs(tmp_path / "r"), 1)
    assert (p.procs, p.failed_runs, p.samples) == (2, 1, s.timed_blocks)


def test_l3_runs_with_other_procs_never_pool(tmp_path):
    s = _l3_scenario(tmp_path)
    write_run(tmp_path, "r/20261021-1000-x-v1-n1-c16-r1", s, procs=2, l3=R8A_L3)
    write_run(tmp_path, "r/20261021-1010-x-v1-n1-c16-r2", s, procs=1, l3=["0-15", "16-31"])
    with pytest.raises(ValueError, match=r"hog-execs per host differ at N=1, K=16: \[1, 2\]"):
        summarize(load_runs(tmp_path), 1)


def test_chart_core_axis_says_per_l3(tmp_path):
    s = _l3_scenario(tmp_path)
    for k, procs in ((8, 1), (16, 2)):
        write_run(tmp_path, f"r/20261021-10{k:02d}-x-v1-n1-c{k}-r1", s, tps=k, procs=procs,
                  l3=R8A_L3)
    svg = _svg_text(tmp_path, summarize(load_runs(tmp_path / "r"), 1))
    assert "cores per executor host (one hog-exec per L3)" in svg


def test_chart_n_axis_says_per_l3(tmp_path):
    s = _l3_scenario(tmp_path, n_list=[1, 2], exec_cores=[64])
    for n in (1, 2):
        write_run(tmp_path, f"r/20261021-100{n}-x-v1-n{n}-c64-r1", s, tps=n, procs=8, l3=R8A_L3)
    svg = _svg_text(tmp_path, summarize(load_runs(tmp_path / "r"), 1))
    assert "Executor hosts (N) + 3 support hosts (one hog-exec per L3)" in svg
