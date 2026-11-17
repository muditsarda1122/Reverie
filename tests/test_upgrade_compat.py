"""Upgrading must never lose a user's memory. Each released version leaves a real database here
(made by that version's own code); the current code must open it, keep every row, and keep working.
When you ship a release, add its fixture to FIXTURES (see CONTRIBUTING.md)."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from reverie.brain import Brain

FIXTURES = sorted(Path(__file__).parent.joinpath("fixtures").glob("db_v*.sqlite"))


def test_there_is_at_least_one_released_database_fixture():
    assert FIXTURES, "tests/fixtures/db_v*.sqlite is missing"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_current_code_opens_an_older_database_and_loses_nothing(fixture, tmp_path):
    db = tmp_path / "reverie.db"
    shutil.copy(fixture, db)
    before = Brain(db)
    n_active = before.count_ecus(status="active")
    n_sessions = len(before.list_sessions("/r", "main"))
    ids = [e["id"] for e in before.list_ecus(status="active")] if hasattr(before, "list_ecus") else []
    before.close()
    assert n_active >= 1

    b = Brain(db)                                   # opening migrates in place
    try:
        assert b.count_ecus(status="active") == n_active
        assert len(b.list_sessions("/r", "main")) == n_sessions
        for i in ids:
            assert b.get_ecu(i) is not None
        v = np.ones(384, dtype=np.float32) / np.sqrt(384)
        new = b.insert_ecu({"cognition": "A conclusion written after the upgrade.",
                            "conclusion_type": "decision", "confidence": 0.6,
                            "scope": {"level": "repo", "repo": "/r"}}, v)       # still writable
        assert b.get_ecu(new) is not None
        assert b.count_ecus(status="active") == n_active + 1
    finally:
        b.close()


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_a_second_open_changes_nothing(fixture, tmp_path):
    db = tmp_path / "reverie.db"
    shutil.copy(fixture, db)
    Brain(db).close()
    first = Brain(db)
    state = (first.count_ecus(status="active"), first.schema_version)
    first.close()
    second = Brain(db)
    assert (second.count_ecus(status="active"), second.schema_version) == state
    second.close()
