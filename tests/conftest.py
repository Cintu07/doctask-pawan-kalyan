"""Fixtures.

Every test in this suite runs offline. No key, no network, no paid call. That is
a requirement of the brief and it is also the only way a suite is worth running
on every change.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="session")
def playbook_path() -> pathlib.Path:
    """The playbook file, for callers that take a path."""
    return ROOT / "playbooks" / "vendor-contract.json"


@pytest.fixture(scope="session")
def rules(playbook_path):
    """The playbook already parsed, for callers that take rule objects."""
    from analyst.playbook import load_playbook

    return load_playbook(playbook_path)


@pytest.fixture
def corpus(tmp_path) -> pathlib.Path:
    """The contradictory synthetic file, freshly generated per test."""
    from make_corpus import build

    folder = tmp_path / "corpus" / "northwind"
    build(folder)
    return folder


@pytest.fixture
def clean_corpus(tmp_path) -> pathlib.Path:
    """A corpus with nothing wrong in it, to prove a clean report is possible."""
    from make_corpus import build_clean

    folder = tmp_path / "corpus" / "clean"
    build_clean(folder)
    return folder


@pytest.fixture
def analyst(tmp_path):
    from analyst.service import Analyst

    return Analyst(data_dir=tmp_path / "data")
