"""Load run dirs and summarize them. Reads result files only."""
from __future__ import annotations

import csv
import json
import math
import re
import statistics
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from whitney.config import Scenario, load_scenario
from whitney.cpus import cpuset_size, procs_per_host

# Runs pool into one summary only if every field here matches, except storage
# (one series per storage mode). The ebs_* fields must match among ebs runs
# only: other modes have no EBS data volume. archive_sha256 stands for the
# build, so it also covers hogd_ref. Not in the key, so free to differ:
# n_list, exec_cores, repetitions, region, availability_zone, ami_id.
EBS_FIELDS = ("ebs_volume_type", "ebs_volume_size_gib", "ebs_iops", "ebs_throughput_mibps")
GROUP_FIELDS = ("name", "archive_sha256", "storage", "indexer", "single_host", "experiment",
                "accounts", "txs_per_block", "txs_per_block_per_exec",
                "txs_per_block_per_core", "batch_sizes", "warmup_blocks", "timed_blocks",
                "connections_per_exec", "coros_per_connection", "uniquifier_layout",
                "exec_allocator", "exec_per_l3", "instance_types", "cpu_layout", *EBS_FIELDS)
_DIR = re.compile(r"-n(\d+)(?:-c(\d+))?-r(\d+)$")


@dataclass
class Run:
    dir: Path
    status: str  # "ok" | "failed" | "invalid" (ok, but trials do not match the scenario)
    scenario: Scenario
    trials: list[dict]
    n: int
    rep: int
    archive_sha256: str = ""
    error: str | None = None
    cores: int | None = None  # hog-exec cores per executor host; None: no limit
    # hog-execs per executor host: 1, or with exec_per_l3 the per-L3 count
    # (None when the run has no L3 record).
    procs: int | None = 1


@dataclass
class Point:
    n: int
    storage: str
    indexer: bool
    median_tps: float
    min_tps: float
    max_tps: float
    per_rep_median: list[tuple[int, float]]  # (rep, median), sorted by rep
    samples: int
    failed_runs: int
    single_host: bool = False
    cores: int | None = None
    per_l3: bool = False  # exec_per_l3: one hog-exec per L3 on each executor host
    procs: int | None = None  # hog-execs per executor host (exec_per_l3 only)
    block_min_tps: float = math.nan
    block_max_tps: float = math.nan
    # Txs per block, and the median timed block's wall time, so each point
    # states its block size and block time.
    block_txs: int = 0
    median_block_ms: float = math.nan
    # From the runner's phase times. exec_tps is txs over the propose phase
    # only, aggregated like median_tps. runner_cores is the runner's CPU time
    # over the block's wall time: its load during the block.
    median_exec_tps: float = math.nan
    median_open_ms: float = math.nan
    median_close_ms: float = math.nan
    median_runner_cores: float = math.nan
    # (run dir name, reason) for each invalid run; also counted in failed_runs.
    invalid_runs: list[tuple[str, str]] = field(default_factory=list)


TPS_REL_TOLERANCE = 1e-6


def load_runs(root: Path) -> list[Run]:
    """Find run dirs at any depth under root (a run dir has status.json).

    An ok run whose trials do not match its scenario gets status "invalid"
    and the reason in error. It is counted as failed and never used.
    """
    runs = []
    for status_file in sorted(root.rglob("status.json")):
        d = status_file.parent
        m = _DIR.search(d.name)
        if not m:
            raise ValueError(f"run dir name has no -n<N>[-c<K>]-r<rep> suffix: {d}")
        try:
            doc = json.loads(status_file.read_text())
        except ValueError:  # cut off mid-write: count it, never drop it
            doc = {"status": "failed", "error": "unreadable status.json"}
        status = doc.get("status") if isinstance(doc, dict) else None
        if status not in ("ok", "failed"):
            raise ValueError(f"{status_file}: bad status {status!r}")
        scenario = _load_run_scenario(d / "scenario.resolved.toml")
        sha_file = d / "archive.sha256"
        sha = sha_file.read_text().strip() if sha_file.is_file() else ""
        n, rep = int(m.group(1)), int(m.group(3))
        cores = int(m.group(2)) if m.group(2) else None
        if bool(scenario.exec_cores) != (cores is not None):
            raise ValueError(f"run dir name and scenario disagree on exec_cores "
                             f"({scenario.exec_cores}): {d}")
        error = doc.get("error")
        procs = 1
        if scenario.exec_per_l3:
            size = l3_size(d)
            procs = procs_per_host(size, cores) if size and cores else None
        trials: list[dict] = []
        if status == "ok":
            trials, reason = _read_trials(d / "trials.jsonl")
            if procs is None:
                reason = reason or "exec_per_l3 run without an L3 record in exec-setup.json"
            reason = reason or check_trials(trials, scenario, n, cores, procs or 1)
            if reason:
                status, error, trials = "invalid", reason, []
        runs.append(Run(d, status, scenario, trials, n, rep, sha, error, cores, procs))
    return runs


def l3_size(run_dir: Path) -> int | None:
    """The L3 size (CPUs per group) in a run's exec-setup.json: the first
    "l3" group of the exec hosts, which must agree. None when the file or
    the record is missing or bad."""
    try:
        doc = json.loads((run_dir / "exec-setup.json").read_text())
    except (OSError, ValueError):
        return None
    sizes = set()
    for entry in doc.values() if isinstance(doc, dict) else ():
        l3 = entry.get("l3") if isinstance(entry, dict) else None
        if not (isinstance(l3, list) and l3 and isinstance(l3[0], str)):
            return None
        try:
            sizes.add(cpuset_size(l3[0]))
        except ValueError:
            return None
    return sizes.pop() if len(sizes) == 1 else None


def _load_run_scenario(path: Path) -> Scenario:
    """A run's resolved scenario. whitney writes every field there, so a
    missing one means an older run dir: refused, never read with today's
    default (which may not be what that run used)."""
    s = load_scenario(path, [])
    missing = [f.name for f in fields(Scenario) if f.name not in tomllib.loads(path.read_text())]
    if missing:
        raise ValueError(f"{path}: written by an older whitney, no {', '.join(missing)}")
    return s


def _read_trials(tf: Path) -> tuple[list[dict], str | None]:
    if not tf.is_file():
        return [], "no trials.jsonl"
    trials = []
    for i, line in enumerate(tf.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict):
            return [], f"trials.jsonl line {i} is not a JSON object"
        trials.append(row)
    return trials, None


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# The runner's per-block phase times; every trial row has them.
PHASE_KEYS = ("open_ns", "exec_ns", "close_ns", "runner_cpu_ns")


def check_trials(trials: list[dict], s: Scenario, n: int, cores: int | None = None,
                 procs: int = 1) -> str | None:
    """Why trials do not match scenario s at N = n (and K = cores, with
    procs hog-execs per executor host), or None if they do.

    Each batch size of s needs warmup_blocks + timed_blocks rows, block_index
    0..k-1 in order, the warm-up flag on the first warmup_blocks only, and
    the scenario's txs, nexec (N × procs), accounts, and experiment. tps must equal
    txs / elapsed_ns (elapsed_ns is truncated, so a little above is fine).
    The PHASE_KEYS are ints >= 0 and open + exec + close is at most elapsed_ns.
    """
    blocks = s.warmup_blocks + s.timed_blocks
    by_batch: dict[object, list[dict]] = {}
    for t in trials:
        by_batch.setdefault(t.get("batch_size"), []).append(t)
    for b in by_batch:
        if b not in s.batch_sizes:
            return f"batch size {b} is not in the scenario"
    for b in s.batch_sizes:
        rows = by_batch.get(b, [])
        if len(rows) != blocks:
            return f"batch size {b}: {len(rows)} trials, want {blocks}"
        idx = [r.get("block_index") for r in rows]
        if idx != list(range(blocks)):
            return f"batch size {b}: block indexes {idx}, want 0..{blocks - 1}"
        for i, r in enumerate(rows):
            where = f"batch size {b} block {i}"
            for key in ("warmup", "txs", "elapsed_ns", "tps", "nexec", "accounts", "experiment",
                        *PHASE_KEYS):
                if key not in r:
                    return f"{where}: no {key}"
            want_warmup = i < s.warmup_blocks
            if r["warmup"] is not want_warmup:
                return (f"{where}: warmup is {json.dumps(r['warmup'])}, "
                        f"want {json.dumps(want_warmup)}")
            txs = s.block_txs(n, cores)
            if r["txs"] != txs:
                want = (f"txs_per_block_per_core × N × K {txs}" if s.txs_per_block_per_core > 0
                        else f"txs_per_block_per_exec × N {txs}" if s.txs_per_block_per_exec > 0
                        else f"txs_per_block {s.txs_per_block}")
                return f"{where}: txs {r['txs']} != {want}"
            if r["nexec"] != n * procs:
                return (f"{where}: nexec {r['nexec']} != N {n}" if procs == 1 else
                        f"{where}: nexec {r['nexec']} != N {n} × {procs} hog-execs per host")
            if r["accounts"] != s.accounts:
                return f"{where}: accounts {r['accounts']} != {s.accounts}"
            if r["experiment"] != s.experiment:
                return f"{where}: experiment {r['experiment']} != {s.experiment}"
            if not _is_int(r["elapsed_ns"]) or r["elapsed_ns"] <= 0:
                return f"{where}: elapsed_ns must be > 0"
            tps = r["tps"]
            if isinstance(tps, bool) or not isinstance(tps, (int, float)) or not math.isfinite(tps):
                return f"{where}: tps must be a number"
            hi = r["txs"] * 1e9 / r["elapsed_ns"]
            lo = r["txs"] * 1e9 / (r["elapsed_ns"] + 1)
            if not lo * (1 - TPS_REL_TOLERANCE) <= tps <= hi * (1 + TPS_REL_TOLERANCE):
                return f"{where}: tps {tps} does not match txs / elapsed_ns"
            if not all(_is_int(r[k]) and r[k] >= 0 for k in PHASE_KEYS):
                return f"{where}: {', '.join(PHASE_KEYS)} must be ints >= 0"
            if r["open_ns"] + r["exec_ns"] + r["close_ns"] > r["elapsed_ns"]:
                return f"{where}: open_ns + exec_ns + close_ns > elapsed_ns"
    return None


def _hashable(v):
    """Dicts and lists as tuples, so equal values give equal, hashable keys."""
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    return v


def group_key(r: Run) -> tuple:
    """GROUP_FIELDS values; the ebs_* fields are None unless storage is ebs."""
    s = r.scenario

    def value(f: str):
        if f == "archive_sha256":
            return r.archive_sha256
        if f in EBS_FIELDS and s.storage != "ebs":
            return None
        return _hashable(getattr(s, f))
    return tuple(value(f) for f in GROUP_FIELDS)


def _check_one_group(runs: list[Run]) -> None:
    keys = [dict(zip(GROUP_FIELDS, group_key(r))) for r in runs]
    ebs = [k for k in keys if k["storage"] == "ebs"]
    differing = [f for f in GROUP_FIELDS if f != "storage"
                 and len({k[f] for k in (ebs if f in EBS_FIELDS else keys)}) > 1]
    if differing:
        raise ValueError("mixed runs: differing fields: " + ", ".join(differing))


def _median_of(rows: list[dict], f) -> float:
    return statistics.median(f(t) for t in rows) if rows else math.nan


def summarize(runs: list[Run], batch_size: int) -> list[Point]:
    """One Point per (storage, N, cores), sorted by storage, N, then cores."""
    _check_one_group(runs)
    cells: dict[tuple[str, int, int | None], list[Run]] = {}
    for r in runs:
        cells.setdefault((r.scenario.storage, r.n, r.cores), []).append(r)
    points = []
    def order(cell):  # cores None (no limit) sorts before any K
        storage, n, cores = cell[0]
        return storage, n, cores or 0
    for (storage, n, cores), rs in sorted(cells.items(), key=order):
        per_rep: list[tuple[int, float]] = []
        samples: list[float] = []
        block_ns: list[int] = []
        exec_meds: list[float] = []
        phased: list[dict] = []
        for r in sorted(rs, key=lambda r: r.rep):
            if r.status != "ok":
                continue
            timed = [t for t in r.trials if t["batch_size"] == batch_size and not t["warmup"]]
            tps = [float(t["tps"]) for t in timed]
            block_ns += [t["elapsed_ns"] for t in timed]
            phased += timed
            exec_tps = [t["txs"] * 1e9 / t["exec_ns"] for t in timed if t["exec_ns"] > 0]
            if exec_tps:
                exec_meds.append(statistics.median(exec_tps))
            if tps:
                per_rep.append((r.rep, statistics.median(tps)))
                samples += tps
        failed = sum(1 for r in rs if r.status != "ok")
        invalid = [(r.dir.name, r.error or "") for r in sorted(rs, key=lambda r: r.rep)
                   if r.status == "invalid"]
        procs = {r.procs for r in rs if r.procs is not None}
        if len(procs) > 1:
            raise ValueError(f"mixed runs: hog-execs per host differ at N={n}, K={cores}: "
                             f"{sorted(procs)}")
        nan = math.nan
        meds = [m for _, m in per_rep]  # each rep (fresh cluster) is the independent unit
        points.append(Point(
            n=n, storage=storage, indexer=rs[0].scenario.indexer, cores=cores,
            median_tps=statistics.median(meds) if meds else nan,
            min_tps=min(meds) if meds else nan,
            max_tps=max(meds) if meds else nan,
            per_rep_median=per_rep, samples=len(samples), failed_runs=failed,
            single_host=rs[0].scenario.single_host,
            per_l3=rs[0].scenario.exec_per_l3,
            procs=next(iter(procs)) if procs and rs[0].scenario.exec_per_l3 else None,
            block_min_tps=min(samples) if samples else nan,
            block_max_tps=max(samples) if samples else nan,
            block_txs=rs[0].scenario.block_txs(n, cores),
            median_block_ms=statistics.median(block_ns) / 1e6 if block_ns else nan,
            median_exec_tps=statistics.median(exec_meds) if exec_meds else nan,
            median_open_ms=_median_of(phased, lambda t: t["open_ns"] / 1e6),
            median_close_ms=_median_of(phased, lambda t: t["close_ns"] / 1e6),
            median_runner_cores=_median_of(phased, lambda t: t["runner_cpu_ns"] / t["elapsed_ns"]),
            invalid_runs=invalid))
    return points


_COLS = ("storage", "n", "exec_cores", "procs_per_host", "indexer", "single_host", "block_txs",
         "median_block_ms", "median_tps", "min_tps", "max_tps",
         "block_min_tps", "block_max_tps", "median_exec_tps", "median_open_ms",
         "median_close_ms", "median_runner_cores", "per_rep_median", "samples", "failed_runs",
         "invalid_runs")


def _row(p: Point) -> list[str]:
    def f(x: float) -> str:
        return "" if math.isnan(x) else f"{x:.1f}"
    return [p.storage, str(p.n), "" if p.cores is None else str(p.cores),
            "" if p.procs is None else str(p.procs),
            str(p.indexer).lower(), str(p.single_host).lower(),
            str(p.block_txs), f(p.median_block_ms), f(p.median_tps), f(p.min_tps), f(p.max_tps),
            f(p.block_min_tps), f(p.block_max_tps), f(p.median_exec_tps),
            f(p.median_open_ms), f(p.median_close_ms),
            "" if math.isnan(p.median_runner_cores) else f"{p.median_runner_cores:.2f}",
            ";".join(f"r{r}={m:.1f}" for r, m in p.per_rep_median), str(p.samples), str(p.failed_runs),
            str(len(p.invalid_runs))]


def write_summary(points: list[Point], path: Path) -> None:
    """Write <path>.csv and <path>.md."""
    with open(path.parent / (path.name + ".csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_COLS)
        w.writerows(_row(p) for p in points)
    lines = ["| " + " | ".join(_COLS) + " |", "|" + "---|" * len(_COLS)]
    lines += ["| " + " | ".join(c or "-" for c in _row(p)) + " |" for p in points]
    invalid = [(name, why) for p in points for name, why in p.invalid_runs]
    if invalid:  # failed_runs counts them; say why each one was not used
        lines += ["", "Invalid runs (not used):", ""]
        lines += [f"- {name}: {why}" for name, why in invalid]
    (path.parent / (path.name + ".md")).write_text("\n".join(lines) + "\n")
