"""Helpers that write run dirs (used by tests and to regenerate tests/fixtures)."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from fakes import SCENARIO
from whitney.config import load_scenario


def scenario(tmp: Path, **kw):
    p = tmp / "s.toml"
    p.write_text(SCENARIO)
    return replace(load_scenario(p, []), **kw)


def trial(s, n: int, batch_size: int, block_index: int, tps: float,
          cores: int | None = None, procs: int = 1) -> dict:
    """One trials.jsonl row as whitney-runner writes it, consistent with s
    (procs hog-execs on each of the n exec hosts). The block's phases: 1%
    open, 4% close, the rest propose; the runner uses 10 cores."""
    txs = s.block_txs(n, cores)
    elapsed = round(txs * 1e9 / tps)
    open_ns, close_ns = elapsed // 100, elapsed // 25
    return {"nexec": n * procs, "batch_size": batch_size, "block_index": block_index,
            "warmup": block_index < s.warmup_blocks, "txs": txs,
            "elapsed_ns": elapsed, "tps": float(tps),
            "open_ns": open_ns, "exec_ns": elapsed - open_ns - close_ns, "close_ns": close_ns,
            "runner_cpu_ns": 10 * elapsed,
            "work_tx_hash": "00", "accounts": s.accounts, "experiment": s.experiment}


def write_run(root: Path, name: str, s, *, status="ok", sha="a" * 64, tps=100.0,
              batch_size=1, warmup_tps=1.0, error=None, procs=1, l3=None) -> Path:
    """Write a run dir whose trials match s.

    procs: hog-execs per exec host (nexec = N × procs). l3: the L3 groups
    (cpuset lists) to record for each exec host in exec-setup.json; None
    writes no exec-setup.json.

    tps: the timed-block values for batch_size, one number (every timed
    block) or a list of s.timed_blocks values. Other batch sizes get 100.0
    per timed block. Warm-up blocks get warmup_tps.
    """
    d = root / name
    d.mkdir(parents=True)
    (d / "scenario.resolved.toml").write_text(s.to_toml())
    (d / "archive.sha256").write_text(sha + "\n")
    (d / "manifest.json").write_text("{}\n")
    (d / "command.txt").write_text("whitney: whitney run s.toml\n")
    m = re.search(r"-n(\d+)(?:-c(\d+))?-r\d+$", name)
    n, cores = int(m.group(1)), int(m.group(2)) if m.group(2) else None
    (d / "status.json").write_text(json.dumps(
        {"status": status, "error": error, "started_at": "2026-10-21T10:00:00Z",
         "finished_at": "2026-10-21T10:10:00Z"}))
    if l3 is not None:
        (d / "exec-setup.json").write_text(json.dumps(
            {f"exec{h}": {"l3": l3, "ports": {}} for h in range(n)}))
    if status == "ok":
        rows = []
        for b in s.batch_sizes:
            timed = tps if b == batch_size else 100.0
            if isinstance(timed, (int, float)):
                timed = [timed] * s.timed_blocks
            values = [warmup_tps] * s.warmup_blocks + list(timed)
            rows += [trial(s, n, b, i, t, cores, procs) for i, t in enumerate(values)]
        (d / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return d


if __name__ == "__main__":
    import tempfile
    out = Path(__file__).parent / "fixtures"
    with tempfile.TemporaryDirectory() as t:
        x8 = {"runner": "r8a.16xlarge", "exec": "r8a.16xlarge", "db": "x8aedz.12xlarge",
              "event": "x8aedz.12xlarge", "indexer": "x8aedz.12xlarge",
              "single": "m8a.48xlarge"}
        s = scenario(Path(t), storage="nvme", instance_types=x8, warmup_blocks=1,
                     timed_blocks=10)
        write_run(out, "20261021-1000-exec-scaling-v0.1.31-n2-r1", s,
                  tps=[100 + i for i in range(10)])
        write_run(out, "20261021-1020-exec-scaling-v0.1.31-n2-r2", s, status="failed",
                  error="cluster apply failed: capacity")
