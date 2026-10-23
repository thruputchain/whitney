"""The committed reference results in results/published: every run still
loads as ok, the committed summary is what today's code makes from the
published files, and no private detail is in them."""
from pathlib import Path

import pytest

from whitney.publish import scan_leftovers
from whitney.results import load_runs, summarize, write_summary

PUBLISHED = Path(__file__).resolve().parents[1] / "results" / "published"
SWEEPS = sorted(PUBLISHED.glob("*/*-sweep")) if PUBLISHED.is_dir() else []
DIRS = sorted(p for p in PUBLISHED.iterdir() if p.is_dir()) if PUBLISHED.is_dir() else []


def _none_found(what: str):
    if not PUBLISHED.is_dir():
        pytest.skip("no results/published")
    pytest.fail(f"results/published has no {what}")


@pytest.mark.parametrize("sweep", SWEEPS or [None], ids=lambda p: p.name if p else "none")
def test_published_sweep_reproduces_its_summary(sweep, tmp_path):
    if sweep is None:
        _none_found("sweep dirs")
    runs = load_runs(sweep)
    assert runs and {r.status for r in runs} == {"ok"}, [(r.dir.name, r.error) for r in runs]
    committed = sorted(sweep.glob("summary-b*.csv"))
    assert committed
    for csv in committed:
        batch_size = int(csv.stem.removeprefix("summary-b"))
        write_summary(summarize(runs, batch_size), tmp_path / csv.stem)
        for suffix in (".csv", ".md"):
            got = (tmp_path / (csv.stem + suffix)).read_bytes()
            assert got == csv.with_suffix(suffix).read_bytes(), csv.with_suffix(suffix)


@pytest.mark.parametrize("root", DIRS or [None], ids=lambda p: p.name if p else "none")
def test_published_results_hold_no_private_details(root):
    if root is None:
        _none_found("result dirs")
    assert scan_leftovers(root) == []
