"""Phase 6 tests — Cognition Maintainer (design doc Section 1, D33).

The Maintainer harness: trigger logic (is_maintenance_overdue), the
maintenance run orchestrator (run_maintenance + per-task logging to
maintenance_log, state persistence in maintenance_state), the background
MaintainerThread, and the MCP-server startup check.

Run: .venv/bin/python -m pytest tests/test_phase6_maintainer.py -v
All offline: no LLM calls (maintenance task bodies are stubs until Phases
7–9 fill them in), tmp_path brains, injectable `now`.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import copy
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from ec import maintainer
from ec.brain import Brain
from ec.config import DEFAULT_CONFIG, AttrDict, _to_attrdict
from ec.maintainer import (
    TASK_ORDER,
    MaintainerThread,
    MaintenanceResult,
    is_maintenance_overdue,
    run_maintenance,
)

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------

def make_config(**maintainer_overrides) -> AttrDict:
    """Deep-copied defaults with optional maintainer-block overrides."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["maintainer"].update(maintainer_overrides)
    return _to_attrdict(cfg)


@pytest.fixture()
def config():
    return make_config()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def insert_ecu(brain, cognition="Cache invalidation must follow token refresh",
               **overrides):
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:demo > module:auth"},
        "provenance": {"source_type": "debugging"},
        "grounding": {"files": ["auth/token.py"]},
        "confidence": 0.8,
    }
    ecu.update(overrides)
    return brain.insert_ecu(ecu)   # embedding=None is fine for these tests


def seed_run(brain, last_run_at, ecu_count):
    """Seed maintenance_state as if a run finished at last_run_at."""
    brain.set_maintenance_state("last_run_at", iso(last_run_at))
    brain.set_maintenance_state("last_run_ecu_count", str(ecu_count))


# ---------------------------------------------------------------------------
# schema: additive migration of pre-existing databases
# ---------------------------------------------------------------------------

class TestSchemaMigration:
    def test_fresh_db_has_maintenance_tables(self, tmp_path):
        b = Brain(tmp_path / "ec.db")
        try:
            names = set(b.table_names())
            assert "maintenance_state" in names
            cols = {r[1] for r in b._conn.execute(
                "PRAGMA table_info(maintenance_log)")}
            assert "ecus_affected" in cols
        finally:
            b.close()

    def test_legacy_db_gains_ecus_affected_column(self, tmp_path):
        """A Phase 1-era database (maintenance_log without ecus_affected)
        must be upgraded additively on the next Brain() connect — without
        losing its existing rows."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE maintenance_log (
                id      TEXT PRIMARY KEY,
                run_at  TEXT NOT NULL,
                action  TEXT NOT NULL,
                details TEXT
            );
            INSERT INTO maintenance_log (id, run_at, action)
                VALUES ('old-row', '2026-01-01T00:00:00+00:00', 'review_gate');
        """)
        conn.commit()
        conn.close()

        b = Brain(db_path)   # runs _apply_schema -> guarded ALTER
        try:
            cols = {r[1] for r in b._conn.execute(
                "PRAGMA table_info(maintenance_log)")}
            assert "ecus_affected" in cols
            # old row survived, new column defaulted to 0
            rows = b.list_maintenance_log()
            assert rows[0]["id"] == "old-row"
            assert rows[0]["ecus_affected"] == 0
            # reopening again (column now present) does not raise
        finally:
            b.close()
        b2 = Brain(db_path)
        b2.close()

    def test_maintenance_state_roundtrip(self, brain):
        assert brain.get_maintenance_state("last_run_at") is None
        brain.set_maintenance_state("last_run_at", "2026-08-22T10:00:00+00:00")
        brain.set_maintenance_state("last_run_ecu_count", "7")
        assert brain.get_maintenance_state("last_run_at") == \
            "2026-08-22T10:00:00+00:00"
        assert brain.get_maintenance_state("last_run_ecu_count") == "7"
        # upsert overwrites
        brain.set_maintenance_state("last_run_ecu_count", "12")
        assert brain.get_maintenance_state("last_run_ecu_count") == "12"

    def test_log_maintenance_records_ecus_affected(self, brain):
        brain.log_maintenance("forgetting", json.dumps({"n": 2}),
                              ecus_affected=2)
        row = brain.list_maintenance_log()[0]
        assert row["action"] == "forgetting"
        assert json.loads(row["details"]) == {"n": 2}
        assert row["ecus_affected"] == 2


# ---------------------------------------------------------------------------
# run_maintenance — task orchestration, per-task logging, state baseline
# ---------------------------------------------------------------------------

class TestRunMaintenance:
    def test_maintenance_log_written(self, brain, config):
        """Each task logs a row (action + JSON details + ecus_affected) and
        the run adds a full_run summary row (design doc test #4)."""
        insert_ecu(brain)
        result = run_maintenance(brain, config)

        rows = brain.list_maintenance_log()
        actions = [r["action"] for r in rows]           # newest first
        assert actions[0] == "full_run"
        assert set(TASK_ORDER) <= set(actions)

        forgetting = next(r for r in rows if r["action"] == "forgetting")
        # live since Phase 7: transition/supersession lists (empty on a
        # healthy brain), never the old stub payload
        assert set(json.loads(forgetting["details"])) == {
            "open_question_transitions", "superseded", "challenged_dependents",
        }
        assert json.loads(forgetting["details"])["superseded"] == []
        assert forgetting["ecus_affected"] == 0

        full_run = rows[0]
        summary = json.loads(full_run["details"])
        assert set(summary["tasks"]) == set(TASK_ORDER)
        assert full_run["ecus_affected"] == 0

        assert isinstance(result, MaintenanceResult)
        assert [t.action for t in result.tasks] == list(TASK_ORDER)
        # ec_get_summary's last_maintenance_run is now populated
        assert brain.last_maintenance_run() is not None

    def test_maintenance_state_persisted(self, brain, config):
        """last_run_at + last_run_ecu_count persist in maintenance_state
        and reflect the post-run ECU count (design doc test #5)."""
        for i in range(3):
            insert_ecu(brain, cognition=f"ECU number {i} invariant")
        run_maintenance(brain, config)

        assert brain.get_maintenance_state("last_run_at") is not None
        assert brain.get_maintenance_state("last_run_ecu_count") == "3"

    def test_task_failure_does_not_abort_run(self, brain, config):
        """A raising task is logged with its error; the remaining tasks
        still run and the baseline state is still updated."""
        insert_ecu(brain)

        def boom(*args, **kwargs):
            raise RuntimeError("hdbscan exploded")

        with patch.dict(maintainer._TASKS, {"grounding": boom}):
            result = run_maintenance(brain, config)

        grounding = next(t for t in result.tasks if t.action == "grounding")
        assert grounding.details["error"] == "hdbscan exploded"
        assert grounding.ecus_affected == 0
        # later tasks unaffected
        assert [t.action for t in result.tasks] == list(TASK_ORDER)
        rows = brain.list_maintenance_log()
        assert {r["action"] for r in rows} == set(TASK_ORDER) | {"full_run"}
        assert brain.get_maintenance_state("last_run_ecu_count") == "1"


# ---------------------------------------------------------------------------
# is_maintenance_overdue — trigger logic
# ---------------------------------------------------------------------------

class TestTriggerLogic:
    def test_maintenance_overdue_time(self, brain, config):
        """now - last_run > time_threshold_hours (6h) -> overdue (#1)."""
        seed_run(brain, utcnow() - timedelta(hours=7), ecu_count=0)
        assert is_maintenance_overdue(brain, config) is True

    def test_maintenance_overdue_ecu_count(self, brain, config):
        """>= ecu_threshold (10) new canonical ECUs since last run ->
        overdue even with a recent run (#2)."""
        seed_run(brain, utcnow() - timedelta(minutes=5), ecu_count=0)
        for i in range(10):
            insert_ecu(brain, cognition=f"invariant {i}")
        assert is_maintenance_overdue(brain, config) is True
        # 9 new ECUs: below the threshold
        seed_run(brain, utcnow() - timedelta(minutes=5), ecu_count=1)
        assert is_maintenance_overdue(brain, config) is False

    def test_maintenance_not_overdue(self, brain, config):
        """Recent run + few new ECUs -> not overdue (#3)."""
        seed_run(brain, utcnow() - timedelta(hours=1), ecu_count=5)
        insert_ecu(brain)
        assert is_maintenance_overdue(brain, config) is False

    def test_overdue_when_never_ran(self, brain, config):
        """A brain with no maintenance_state is overdue: the first server
        start seeds the baseline (documented decision, design doc §1.2)."""
        assert is_maintenance_overdue(brain, config) is True
        run_maintenance(brain, config)
        assert is_maintenance_overdue(brain, config) is False

    def test_disabled_maintainer_never_overdue(self, brain):
        cfg = make_config(enabled=False)
        seed_run(brain, utcnow() - timedelta(days=30), ecu_count=0)
        assert is_maintenance_overdue(brain, cfg) is False

    def test_run_maintenance_clears_triggers(self, brain, config):
        """After a run, both triggers reset: time baseline moved to now,
        ECU baseline equals the current count."""
        seed_run(brain, utcnow() - timedelta(hours=10), ecu_count=0)
        insert_ecu(brain)
        assert is_maintenance_overdue(brain, config) is True
        run_maintenance(brain, config)
        assert is_maintenance_overdue(brain, config) is False


# ---------------------------------------------------------------------------
# MaintainerThread — daemon lifecycle (#7, #8, #9)
# ---------------------------------------------------------------------------

class TestMaintainerThread:
    @pytest.fixture()
    def fast(self):
        """Short check interval so thread tests finish quickly."""
        return make_config(check_interval_minutes=5)   # overridden per-thread

    def test_background_thread_starts_and_checks_interval(
        self, brain, fast
    ):
        """Daemon thread wakes each interval and runs maintenance when
        overdue (#7)."""
        with patch("ec.maintainer.is_maintenance_overdue",
                   return_value=True), \
             patch("ec.maintainer.run_maintenance") as mock_run:
            t = MaintainerThread(brain, fast, check_interval=0.05)
            t.start()
            assert t.daemon is True
            assert t.name == "ec-maintainer"
            deadline = time.monotonic() + 5.0
            while mock_run.call_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            t.stop()
            t.join(timeout=2)
            assert not t.is_alive()
        assert mock_run.call_count >= 1

    def test_background_thread_skips_when_not_overdue(self, brain, fast):
        """Trigger check gates the run: not overdue -> no maintenance."""
        with patch("ec.maintainer.is_maintenance_overdue",
                   return_value=False), \
             patch("ec.maintainer.run_maintenance") as mock_run:
            t = MaintainerThread(brain, fast, check_interval=0.05)
            t.start()
            time.sleep(0.15)
            t.stop()
            t.join(timeout=2)
        mock_run.assert_not_called()

    def test_background_thread_stops(self, brain, fast):
        """stop() sets the event; the thread exits without running again (#8)."""
        with patch("ec.maintainer.is_maintenance_overdue", return_value=True), \
             patch("ec.maintainer.run_maintenance"):
            t = MaintainerThread(brain, fast, check_interval=0.05)
            t.start()
            t.stop()
            t.join(timeout=2)
            assert not t.is_alive()

    def test_background_thread_never_crashes(self, brain, fast, caplog):
        """An exception inside the run is logged; the loop continues and
        calls again on the next tick (#9)."""
        calls = []

        def flaky_run(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("transient failure")

        with patch("ec.maintainer.is_maintenance_overdue",
                   return_value=True), \
             patch("ec.maintainer.run_maintenance", side_effect=flaky_run):
            t = MaintainerThread(brain, fast, check_interval=0.05)
            t.start()
            deadline = time.monotonic() + 5.0
            while len(calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            t.stop()
            t.join(timeout=2)
            assert not t.is_alive()
        assert len(calls) >= 3          # kept going after failures
        assert any("maintenance run failed" in r.message for r in caplog.records)

    def test_disabled_maintainer_thread_does_nothing(self, brain):
        cfg = make_config(enabled=False)
        with patch("ec.maintainer.run_maintenance") as mock_run:
            t = MaintainerThread(brain, cfg, check_interval=0.05)
            t.start()
            time.sleep(0.15)
            t.stop()
            t.join(timeout=2)
        mock_run.assert_not_called()

    def test_thread_with_db_path_opens_own_connection(self, brain, fast):
        """Production path: the thread opens a private Brain over the same
        DB file (sqlite3 connections are thread-bound) — documented decision."""
        seen = {}

        def capture(brain_arg, *args, **kwargs):
            seen["brain"] = brain_arg
            return MaintenanceResult(started_at=iso(utcnow()),
                                     finished_at=iso(utcnow()))

        with patch("ec.maintainer.is_maintenance_overdue", return_value=True), \
             patch("ec.maintainer.run_maintenance", side_effect=capture):
            t = MaintainerThread(None, fast, check_interval=0.05,
                                 db_path=brain.db_path)
            t.start()
            deadline = time.monotonic() + 5.0
            while "brain" not in seen and time.monotonic() < deadline:
                time.sleep(0.01)
            t.stop()
            t.join(timeout=2)
        assert seen["brain"] is not brain
        assert str(seen["brain"].db_path) == str(brain.db_path)


# ---------------------------------------------------------------------------
# ECServer startup check + non-blocking guarantee (#6, #10)
# ---------------------------------------------------------------------------

from ec.mcp_server import ECServer  # noqa: E402  (import here to keep the

                                     # module-level import list maintainer-only)


class TestServerStartupCheck:
    @pytest.fixture()
    def server(self, brain, config):
        s = ECServer(brain=brain, config=config)
        yield s
        s.stop_maintenance()

    def test_startup_check_runs_maintenance(self, brain, server):
        """Overdue brain -> synchronous run at startup + thread started (#6)."""
        seed_run(brain, utcnow() - timedelta(days=30), ecu_count=0)
        insert_ecu(brain)
        with patch("ec.maintainer.run_maintenance",
                   wraps=maintainer.run_maintenance) as mock_run:
            ran = server.start_maintenance()
        assert ran is not None
        mock_run.assert_called_once()
        assert server._maintainer_thread is not None
        assert server._maintainer_thread.is_alive()

    def test_startup_check_skipped_when_fresh(self, brain, server):
        """Recent run + few new ECUs -> no synchronous run, but the
        background thread still starts."""
        seed_run(brain, utcnow() - timedelta(minutes=1), ecu_count=0)
        with patch("ec.maintainer.run_maintenance") as mock_run:
            ran = server.start_maintenance()
        assert ran is None
        mock_run.assert_not_called()
        assert server._maintainer_thread is not None

    def test_startup_check_disabled(self, brain):
        server = ECServer(brain=brain, config=make_config(enabled=False))
        try:
            with patch("ec.maintainer.run_maintenance") as mock_run:
                ran = server.start_maintenance()
            assert ran is None
            mock_run.assert_not_called()
            assert server._maintainer_thread is None
        finally:
            server.stop_maintenance()

    def test_startup_run_failure_does_not_prevent_serving(self, brain, server):
        """A failing startup run is swallowed; the server still starts the
        thread and answers requests."""
        seed_run(brain, utcnow() - timedelta(days=30), ecu_count=0)
        with patch("ec.maintainer.run_maintenance",
                   side_effect=RuntimeError("boom")):
            ran = server.start_maintenance()      # must not raise
        assert ran is None
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert response["result"] == {}

    def test_maintenance_does_not_block_mcp(self, brain):
        """A tool call issued while a maintenance run is in progress is not
        delayed (#10): maintenance holds no locks the handler needs (WAL),
        and the stdio loop never joins the maintainer thread."""
        cfg = make_config(check_interval_minutes=0.001)   # ~60ms checks
        seed_run(brain, utcnow() - timedelta(minutes=1), ecu_count=0)
        server = ECServer(brain=brain, config=cfg)

        started = threading.Event()
        release = threading.Event()

        def slow_run(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            return MaintenanceResult(started_at=iso(utcnow()),
                                     finished_at=iso(utcnow()))

        try:
            with patch("ec.maintainer.run_maintenance", side_effect=slow_run):
                assert server.start_maintenance() is None   # fresh: no sync run
                # trip the real ECU-count trigger
                for i in range(10):
                    insert_ecu(brain, cognition=f"blocking invariant {i}")
                assert started.wait(timeout=10), "maintenance never started"

                t0 = time.monotonic()
                response = server.handle_message(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                elapsed = time.monotonic() - t0
                assert "result" in response
                assert elapsed < 5.0     # not joined against the 10s block
        finally:
            release.set()
            server.stop_maintenance()
