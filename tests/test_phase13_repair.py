"""Phase 13 tests — ``ec repair`` (design doc §7, D51; SPEC §28.4).

repair() checks structural brain health and performs only SAFE, NON-DESTRUCTIVE
recovery: create missing tables (additive schema re-apply), WAL checkpoint,
remove edges whose endpoint ECUs are gone. ECU data is never modified and
orphan session rows are reported, never deleted.

Run: .venv/bin/python -m pytest tests/test_phase13_repair.py -v
All offline: tmp_path brains, no LLM calls.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import sqlite3

import pytest

from ec.brain import Brain
from ec.repair import REQUIRED_TABLES, main, repair


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "ec.db"


@pytest.fixture()
def brain(db_path):
    b = Brain(db_path)
    yield b
    b.close()


def _ecu(brain, cognition, confidence=0.6):
    return brain.insert_ecu({
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:demo > module:auth"},
        "provenance": {"source_type": "debugging"},
        "grounding": {"files": ["auth/token.py"]},
        "confidence": confidence,
    })


# ---------------------------------------------------------------------------
# healthy brain + fatal cases
# ---------------------------------------------------------------------------

class TestHealthyAndFatal:
    def test_healthy_brain_reports_ok(self, brain, db_path):
        a, b = _ecu(brain, "Belief one"), _ecu(brain, "Belief two")
        brain.add_edge(a, b, "supports", weight=0.8)
        brain.log_maintenance("full_run", "{}", ecus_affected=2)
        # fold any committed WAL pages back first so nothing needs fixing
        brain._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        report = repair(brain_path=db_path)

        assert report["healthy"] is True
        assert set(report["checks"]) == {
            "database_file", "schema_tables", "wal_checkpoint",
            "orphan_edges", "orphan_session_ecus", "table_queryability",
        }
        assert all(v == "ok" for v in report["checks"].values())
        assert report["fixes"] == []

    def test_missing_database_short_circuits(self, tmp_path):
        report = repair(brain_path=tmp_path / "nope.db")

        assert report["checks"]["database_file"].startswith("missing")
        assert report["healthy"] is False
        # nothing else could run — no other checks recorded
        assert list(report["checks"]) == ["database_file"]

    def test_corrupt_file_reported_not_repaired(self, tmp_path):
        bad = tmp_path / "corrupt.db"
        bad.write_bytes(b"this is not a sqlite database" * 100)

        report = repair(brain_path=bad)

        assert report["checks"]["database_file"].startswith("corrupt")
        assert report["fixes"] == []


# ---------------------------------------------------------------------------
# fixes: tables, WAL, orphan edges
# ---------------------------------------------------------------------------

class TestFixes:
    def test_missing_table_is_created(self, brain, db_path):
        a, b = _ecu(brain, "Belief one"), _ecu(brain, "Belief two")
        brain.add_edge(a, b, "supports", weight=0.5)
        brain._conn.execute("PRAGMA foreign_keys = OFF")
        brain._conn.execute("DROP TABLE clusters")
        brain._conn.commit()
        brain._conn.execute("PRAGMA foreign_keys = ON")
        brain.close()

        report = repair(brain_path=db_path)

        assert report["checks"]["schema_tables"] == "repaired"
        assert any("clusters" in f for f in report["fixes"])
        conn = sqlite3.connect(str(db_path))
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert set(REQUIRED_TABLES) <= names

    def test_wal_checkpointed_into_main_db(self, brain, db_path):
        """Committed pages sitting in the -wal file are folded back in.

        wal_autocheckpoint is disabled and two connections stay open, so
        SQLite's automatic checkpoints / last-close cleanup cannot drain the
        WAL before repair sees it."""
        brain._conn.execute("PRAGMA wal_autocheckpoint=0")
        _ecu(brain, "WAL resident")
        keeper = sqlite3.connect(str(db_path))
        keeper.execute("SELECT 1").fetchone()
        wal = db_path.parent / (db_path.name + "-wal")

        try:
            assert wal.exists() and wal.stat().st_size > 0
            size_before = wal.stat().st_size

            report = repair(brain_path=db_path)

            assert report["checks"]["wal_checkpoint"] == "checkpointed"
            assert any("WAL" in f or "wal" in f for f in report["fixes"])
            assert wal.stat().st_size == 0          # TRUNCATE mode
            assert size_before > 0
            # the committed data survived the checkpoint
            conn = sqlite3.connect(str(db_path))
            n = conn.execute(
                "SELECT COUNT(*) FROM ecus WHERE cognition = 'WAL resident'"
            ).fetchone()[0]
            conn.close()
            assert n == 1
        finally:
            keeper.close()

    def test_orphan_edges_removed(self, brain, db_path):
        a, b = _ecu(brain, "Live source"), _ecu(brain, "Live target")
        brain.add_edge(a, b, "supports", weight=0.7)
        brain._conn.execute("PRAGMA foreign_keys = OFF")
        brain._conn.execute(
            "INSERT INTO edges (id, source_id, target_id, type, weight, "
            "confidence_delta, created_at) VALUES "
            "('orphan-1', ?, 'deleted-target', 'supports', 0.5, 0.1, "
            "'2026-01-01T00:00:00+00:00'),"
            "('orphan-2', 'deleted-source', ?, 'contradicts', 0.4, -0.1, "
            "'2026-01-01T00:00:00+00:00')",
            (a, b),
        )
        brain._conn.commit()
        brain._conn.execute("PRAGMA foreign_keys = ON")

        report = repair(brain_path=db_path)

        assert report["checks"]["orphan_edges"] == "removed 2"
        assert any("2 orphan edge(s)" in f for f in report["fixes"])
        remaining = {e["id"] for e in brain.list_all_edges()}
        assert remaining == {
            e["id"] for e in brain.list_all_edges()
            if e["source_id"] in (a, b) and e["target_id"] in (a, b)
        }
        assert "orphan-1" not in remaining and "orphan-2" not in remaining

    def test_orphan_session_ecus_reported_only(self, brain, db_path):
        """Data-level inconsistencies are REPORTED, never auto-deleted."""
        brain._conn.execute("PRAGMA foreign_keys = OFF")
        brain._conn.execute(
            "INSERT INTO session_ecus (id, session_id, cognition, document, "
            "review_status) VALUES ('ghost-ecu', 'deleted-session', "
            "'Ghost finding', '{}', 'pending')"
        )
        brain._conn.commit()
        brain._conn.execute("PRAGMA foreign_keys = ON")

        report = repair(brain_path=db_path)

        assert "reference missing sessions" in report["checks"]["orphan_session_ecus"]
        # reported, not touched: no fix mentions the ghost row
        assert all("session" not in f.lower() and "ghost" not in f.lower()
                   for f in report["fixes"])
        assert report["healthy"] is False       # the warning counts
        conn = sqlite3.connect(str(db_path))
        n = conn.execute(
            "SELECT COUNT(*) FROM session_ecus WHERE id = 'ghost-ecu'"
        ).fetchone()[0]
        conn.close()
        assert n == 1


# ---------------------------------------------------------------------------
# dry run — reports everything, changes nothing
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_fixes_nothing(self, brain, db_path):
        a, b = _ecu(brain, "Belief one"), _ecu(brain, "Belief two")
        brain.add_edge(a, b, "supports", weight=0.5)
        brain._conn.execute("PRAGMA foreign_keys = OFF")
        brain._conn.execute("DROP TABLE clusters")
        brain._conn.execute(
            "INSERT INTO edges (id, source_id, target_id, type, weight, "
            "confidence_delta, created_at) VALUES "
            "('orphan-dry', ?, 'gone', 'supports', 0.5, 0.0, "
            "'2026-01-01T00:00:00+00:00')",
            (a,),
        )
        brain._conn.commit()
        brain._conn.execute("PRAGMA foreign_keys = ON")
        brain.close()

        report = repair(brain_path=db_path, dry_run=True)

        assert report["dry_run"] is True
        assert report["fixes"] == []
        # both problems reported, neither fixed
        assert "dry run" in report["checks"]["schema_tables"]
        assert "clusters" in report["checks"]["schema_tables"]
        assert report["checks"]["orphan_edges"].startswith("1 orphaned")
        assert report["healthy"] is False
        # the problems are still there afterwards
        conn = sqlite3.connect(str(db_path))
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        orphans = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE id = 'orphan-dry'").fetchone()[0]
        conn.close()
        assert "clusters" not in names
        assert orphans == 1

        # and a real run repairs what dry run found
        fixed = repair(brain_path=db_path)
        assert len(fixed["fixes"]) >= 2
        assert "clusters" in fixed["checks"]["schema_tables"] or \
            fixed["checks"]["schema_tables"] in ("ok", "repaired")


# ---------------------------------------------------------------------------
# non-destructiveness — cognition/confidence/status untouched
# ---------------------------------------------------------------------------

class TestNoDataModification:
    def test_ecu_and_edge_data_unchanged_by_repair(self, brain, db_path):
        a = _ecu(brain, "Belief one", confidence=0.62)
        b = _ecu(brain, "Belief two", confidence=0.81)
        brain.add_edge(a, b, "supports", weight=0.9, confidence_delta=0.05)
        brain.update_ecu_status(b, "challenged")
        brain.log_maintenance("grounding", "{}", ecus_affected=1)
        brain.close()

        before_conn = sqlite3.connect(str(db_path))
        before_conn.row_factory = sqlite3.Row
        ecus_before = [dict(r) for r in before_conn.execute(
            "SELECT * FROM ecus ORDER BY id")]
        edges_before = [dict(r) for r in before_conn.execute(
            "SELECT * FROM edges ORDER BY id")]
        log_before = [dict(r) for r in before_conn.execute(
            "SELECT * FROM maintenance_log ORDER BY id")]
        before_conn.close()

        report = repair(brain_path=db_path)

        after_conn = sqlite3.connect(str(db_path))
        after_conn.row_factory = sqlite3.Row
        ecus_after = [dict(r) for r in after_conn.execute(
            "SELECT * FROM ecus ORDER BY id")]
        edges_after = [dict(r) for r in after_conn.execute(
            "SELECT * FROM edges ORDER BY id")]
        log_after = [dict(r) for r in after_conn.execute(
            "SELECT * FROM maintenance_log ORDER BY id")]
        after_conn.close()

        assert report["fixes"] == []              # nothing needed fixing
        assert ecus_before == ecus_after
        assert edges_before == edges_after
        assert log_before == log_after


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_cli_healthy_brain_exit_zero(self, brain, db_path, capsys):
        _ecu(brain, "Belief one")

        code = main(["--db", str(db_path)])
        out = capsys.readouterr().out

        assert code == 0
        assert "✓ database_file: ok" in out
        assert "Repair complete" in out and str(db_path) in out

    def test_cli_repairs_and_exits_zero(self, brain, db_path, capsys):
        a = _ecu(brain, "Belief one")
        brain._conn.execute("PRAGMA foreign_keys = OFF")
        brain._conn.execute(
            "INSERT INTO edges (id, source_id, target_id, type, weight, "
            "confidence_delta, created_at) VALUES "
            "('cli-orphan', ?, 'gone', 'supports', 0.5, 0.0, "
            "'2026-01-01T00:00:00+00:00')",
            (a,),
        )
        brain._conn.commit()
        brain._conn.execute("PRAGMA foreign_keys = ON")

        code = main(["--db", str(db_path)])
        out = capsys.readouterr().out

        assert code == 0
        assert "→ Fixed:" in out and "orphan edge(s)" in out

    def test_cli_dry_run_with_problems_exits_one(self, brain, db_path, capsys):
        a = _ecu(brain, "Belief one")
        brain._conn.execute("PRAGMA foreign_keys = OFF")
        brain._conn.execute(
            "INSERT INTO edges (id, source_id, target_id, type, weight, "
            "confidence_delta, created_at) VALUES "
            "('dry-orphan', ?, 'gone', 'supports', 0.5, 0.0, "
            "'2026-01-01T00:00:00+00:00')",
            (a,),
        )
        brain._conn.commit()
        brain._conn.execute("PRAGMA foreign_keys = ON")

        code = main(["--db", str(db_path), "--dry-run"])
        out = capsys.readouterr().out

        assert code == 1                      # signals "repair needed"
        assert "dry run — nothing changed" in out
        # nothing was actually fixed
        assert brain.list_all_edges()         # still has both edges

    def test_cli_missing_db_exits_one(self, tmp_path, capsys):
        code = main(["--db", str(tmp_path / "absent.db")])

        assert code == 1
