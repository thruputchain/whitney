"""Shared fixtures."""
from __future__ import annotations

import pytest

from fakes import SCENARIO, FakeExec
from whitney.config import load_scenario


@pytest.fixture(autouse=True)
def fake_aws_cli(monkeypatch):
    """Tests never need a real aws CLI on PATH (and never call it)."""
    monkeypatch.setattr("whitney.awscreds.find_aws", lambda: "/opt/fake/bin/aws")


@pytest.fixture(autouse=True)
def no_vector_settle(monkeypatch):
    """Runs need not wait for Vector to settle on fake hosts."""
    monkeypatch.setattr("whitney.remote.VECTOR_SETTLE_SECONDS", 0)


@pytest.fixture
def fx() -> FakeExec:
    return FakeExec()


@pytest.fixture
def scenario(tmp_path):
    def make(*overrides: str):
        p = tmp_path / "scenario.toml"
        p.write_text(SCENARIO)
        return load_scenario(p, list(overrides))
    return make
