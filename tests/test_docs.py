"""Public docs: required phrases present, stale ones gone, links resolve."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
METHODOLOGY = ROOT / "docs" / "methodology.md"
USAGE = ROOT / "docs" / "usage.md"
README = ROOT / "README.md"
DOCS = (README, METHODOLOGY, USAGE)


def test_methodology_has_required_phrases():
    text = METHODOLOGY.read_text()
    for p in ("Ed25519", "support hosts", "strict-increment", "warm-up", "tmpfs", "r8a.16xlarge",
              "r8a.24xlarge", "per-tx", "contract execution", 'uniquifier_layout = "per-sender"',
              "original runner", "### Core scaling", "AllowedCPUs=0-<K-1>", "thread pool",
              "### One executor per L3", "shared_cpu_list", "K must be a multiple of L",
              "### Allocator", 'exec_allocator = "jemalloc"', "glibc malloc",
              "## Per-second metrics", "Vector 0.43.0", "output of `whitney publish`",
              "hogd#4545", "hogd#4564", "event block and the db snapshot first"):
        assert p in text, f"methodology.md lacks: {p}"


def test_readme_shows_results_and_how_to_reproduce():
    text = README.read_text()
    for p in ("## Results", "## Reproduce the results", "results/published/", "whitney build",
              "whitney sweep", "whitney summarize", "whitney chart", "ec2-smoke-single",
              "Always destroy", "docs/methodology.md", "docs/usage.md", "1.17M", "7.02M"):
        assert p in text, f"README.md lacks: {p}"


def test_usage_covers_running():
    text = USAGE.read_text()
    for p in ("## AWS credentials", "aws login", "credential_process", "--aws-creds env",
              "## EC2 capacity", "InsufficientInstanceCapacity", "--apply-timeout",
              "Capacity Reservation", "## Cost and cleanup", "Dead-man switch",
              "### Scenario keys", "`exec_cores`", "`exec_per_l3`", "`exec_allocator`",
              "`txs_per_block_per_core`", "`--cores <K>`", "### Storage", "ebs_iops",
              "## Metrics", "--metrics-remote-write", "WHITNEY_REMOTE_WRITE_BEARER_TOKEN",
              "### Publish", "git add -f results/published/<name>", "`<ip>`", "INDEX.md",
              "## Results format", "-n<N>-c<K>-r<rep>", "exec-setup.json", "9200 + 64 × h + i"):
        assert p in text, f"usage.md lacks: {p}"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_name_no_private_details(path):
    text = path.read_text().lower()
    for p in ("stellar", "sdf", "grafana.net", "nightly"):
        assert p not in text, f"{path.name} names {p}"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_name_only_shipped_scenarios(path):
    shipped = {p.stem for p in (ROOT / "scenarios").glob("*.toml")}
    named = set(re.findall(r"`(exec-[a-z0-9-]+|ec2-smoke-[a-z]+)`", path.read_text()))
    assert named <= shipped, f"{path.name} names scenarios that do not ship: {named - shipped}"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(path):
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", path.read_text()):
        if target.startswith("http"):
            continue
        assert (path.parent / target).exists(), f"{path.name} links to missing {target}"
