"""feat/flow tests — storage layer: repos, review_decisions, queue listing,
session archiving, user_version, bulk data operations.

Run: .venv/bin/python -m pytest tests/test_flow_storage.py -v
All offline; no LLM calls.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from datetime import datetime, timezone

import pytest

from reverie.brain import Brain
from reverie.config import SCHEMA_VERSION

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "reverie.db")
    yield b
    b.close()


def make_snapshot(brain, path):
    """Consistent snapshot of ``brain``'s db at ``path`` (VACUUM INTO —
    the export mechanism itself, run on the open connection)."""
    brain._conn.execute("VACUUM INTO ?", (str(path),))


def make_session(brain, repo="/repos/a", branch="main", status="active"):
    sid = brain.create_session(repo, branch)
    if status != "active":
        brain._conn.execute(
            "UPDATE sessions SET status = ? WHERE id = ?", (status, sid))
        brain._conn.commit()
    return sid


def make_candidate(brain, session_id, cognition="C1", confidence=0.5,
                   review_status="pending", created_at=None):
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:a > module:auth"},
        "provenance": {"source_type": "debugging", "created_at":
                       (created_at or T0).isoformat()},
        "confidence": confidence,
    }
    cid = brain.insert_session_ecu(session_id, ecu)
    if review_status != "pending":
        brain.update_session_ecu_review_status(cid, review_status)
    return cid


# ---------------------------------------------------------------------------
# schema version
# ---------------------------------------------------------------------------

def test_schema_version_stamped(brain):
    assert brain.schema_version == SCHEMA_VERSION


def test_schema_version_never_lower(brain):
    brain._conn.execute("PRAGMA user_version = 7")
    brain._apply_schema()
    assert brain.schema_version == 7


# ---------------------------------------------------------------------------
# repos
# ---------------------------------------------------------------------------

def test_repo_default_state_undecided(brain):
    row = brain.upsert_repo("/git/repo/.git", path="/repos/a",
                            url="https://x/a.git")
    assert row["state"] == "undecided"
    assert row["repo_key"] == "/git/repo/.git"


def test_repo_state_transition(brain):
    brain.upsert_repo("/git/a/.git", state="enabled")
    assert brain.get_repo("/git/a/.git")["state"] == "enabled"
    brain.upsert_repo("/git/a/.git", state="disabled")
    assert brain.get_repo("/git/a/.git")["state"] == "disabled"


def test_repo_invalid_state_rejected(brain):
    with pytest.raises(ValueError, match="invalid repo state"):
        brain.upsert_repo("/git/a/.git", state="maybe")


def test_repo_asked_at_recorded(brain):
    brain.upsert_repo("/git/a/.git", asked_at="2026-03-01T00:00:00+00:00")
    assert brain.get_repo("/git/a/.git")["asked_at"] == \
        "2026-03-01T00:00:00+00:00"


def test_repo_rekey_moves_state(brain):
    """Move/re-clone rematch: the row (with its state) moves to the new key."""
    brain.upsert_repo("/old/.git", path="/old", url="https://x/a.git",
                      state="enabled")
    row = brain.upsert_repo("/new/.git", path="/new", rekey_from="/old/.git")
    assert row["state"] == "enabled"
    assert brain.get_repo("/old/.git") is None
    assert brain.get_repo("/new/.git")["url"] == "https://x/a.git"


def test_list_repos_sorted_by_path(brain):
    brain.upsert_repo("/git/b/.git", path="/repos/b")
    brain.upsert_repo("/git/a/.git", path="/repos/a")
    assert [r["path"] for r in brain.list_repos()] == ["/repos/a", "/repos/b"]


# ---------------------------------------------------------------------------
# review queue listing
# ---------------------------------------------------------------------------

def test_queue_lists_across_sessions_and_repos(brain):
    s1 = make_session(brain, repo="/repos/a", branch="main")
    s2 = make_session(brain, repo="/repos/b", branch="dev")
    make_candidate(brain, s1, "A1", confidence=0.4)
    make_candidate(brain, s2, "B1", confidence=0.9)
    candidates = brain.list_queue_candidates()
    assert [c["cognition"] for c in candidates] == ["B1", "A1"]  # strongest first
    assert candidates[0]["repo_path"] == "/repos/b"
    assert candidates[0]["branch"] == "dev"


def test_queue_filters_by_repo_and_branch(brain):
    s1 = make_session(brain, repo="/repos/a", branch="main")
    make_session(brain, repo="/repos/b", branch="main")
    make_candidate(brain, s1, "A1")
    only_a = brain.list_queue_candidates(repo_path="/repos/a")
    assert [c["cognition"] for c in only_a] == ["A1"]
    only_b = brain.list_queue_candidates(repo_path="/repos/b")
    assert only_b == []
    branched = brain.list_queue_candidates(repo_path="/repos/a", branch="dev")
    assert branched == []


def test_queue_status_filter(brain):
    s1 = make_session(brain)
    make_candidate(brain, s1, "pending-one")
    make_candidate(brain, s1, "skipped-one", review_status="skipped")
    make_candidate(brain, s1, "expired-one", review_status="expired")
    pending = brain.list_queue_candidates(statuses=("pending",))
    assert [c["cognition"] for c in pending] == ["pending-one"]
    everything = brain.list_queue_candidates()
    assert len(everything) == 3


def test_queue_excludes_accepted_and_rejected(brain):
    s1 = make_session(brain)
    make_candidate(brain, s1, "kept", review_status="skipped")
    make_candidate(brain, s1, "gone-accept", review_status="accepted")
    make_candidate(brain, s1, "gone-reject", review_status="rejected")
    candidates = brain.list_queue_candidates()
    assert [c["cognition"] for c in candidates] == ["kept"]


def test_get_queue_candidate(brain):
    s1 = make_session(brain, repo="/repos/a", branch="main")
    cid = make_candidate(brain, s1, "A1", confidence=0.6)
    row = brain.get_queue_candidate(cid)
    assert row["repo_path"] == "/repos/a"
    assert row["session_status"] == "active"
    assert row["review_status"] == "pending"
    assert brain.get_queue_candidate("missing") is None


def test_queue_candidate_document_null(brain):
    """Queue rows are lean: the full document is not carried."""
    s1 = make_session(brain)
    cid = make_candidate(brain, s1, "A1")
    assert brain.get_queue_candidate(cid)["document"] is None


# ---------------------------------------------------------------------------
# review decision audit trail
# ---------------------------------------------------------------------------

def test_review_decision_recorded(brain):
    brain.add_review_decision("cand-1", "accept", "tui",
                              session_id="s-1", decided_at="2026-03-02T00:00:00+00:00")
    brain.add_review_decision("cand-2", "reject", "agent")
    brain.add_review_decision("cand-3", "skip", "cli",
                              edited_cognition="better wording")
    rows = brain.list_review_decisions()
    assert len(rows) == 3
    assert rows[0]["candidate_id"] == "cand-3"      # newest first
    assert rows[0]["edited_cognition"] == "better wording"
    channels = {r["candidate_id"]: r["channel"] for r in rows}
    assert channels == {"cand-1": "tui", "cand-2": "agent", "cand-3": "cli"}


def test_review_decision_invalid_channel(brain):
    with pytest.raises(ValueError, match="invalid channel"):
        brain.add_review_decision("cand-1", "accept", "carrier-pigeon")


# ---------------------------------------------------------------------------
# restore_session_ecu (undo of reject)
# ---------------------------------------------------------------------------

def test_restore_session_ecu_round_trip(brain):
    s1 = make_session(brain)
    cid = make_candidate(brain, s1, "A1", confidence=0.7)
    snapshot = brain._conn.execute(
        "SELECT * FROM session_ecus WHERE id = ?", (cid,)).fetchone()
    row = dict(snapshot)
    brain.delete_session_ecu(cid)
    assert brain.get_session_ecu(cid) is None
    brain.restore_session_ecu(row)
    restored = brain.get_session_ecu(cid)
    assert restored is not None
    assert restored["cognition"] == "A1"
    assert restored["review_status"] == "pending"
    assert brain.count_session_ecus(s1) == 1


# ---------------------------------------------------------------------------
# session archiving
# ---------------------------------------------------------------------------

def test_archive_session_only_active(brain):
    sid = make_session(brain)
    assert brain.archive_session(sid) is True
    assert brain.get_session(sid)["status"] == "archived"
    assert brain.archive_session(sid) is False        # already archived


# ---------------------------------------------------------------------------
# bulk data operations (export/import/reset support)
# ---------------------------------------------------------------------------

def test_clear_all_data(brain):
    sid = make_session(brain)
    make_candidate(brain, sid)
    brain.upsert_repo("/git/a/.git", state="enabled")
    brain.add_review_decision("c1", "accept", "tui")
    brain.clear_all_data()
    assert brain.count_ecus() == 0
    assert brain.list_sessions() == []
    assert brain.list_repos() == []
    assert brain.list_review_decisions() == []
    # schema survives: the brain still works
    sid = make_session(brain)
    assert make_candidate(brain, sid, "fresh")


def test_copy_from_attached(brain, tmp_path):
    sid = make_session(brain, repo="/repos/a")
    make_candidate(brain, sid, "A1")
    brain.insert_ecu({
        "cognition": "canonical one",
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:a"},
        "provenance": {"source_type": "debugging"},
        "confidence": 0.8,
    })
    brain.upsert_repo("/git/a/.git", state="enabled")

    export_path = tmp_path / "snap.db"
    make_snapshot(brain, export_path)

    brain.clear_all_data()
    brain._conn.execute("ATTACH ? AS snap", (str(export_path),))
    counts = brain.copy_from_attached("snap")
    brain._conn.execute("DETACH snap")
    assert counts["ecus"] == 1
    assert counts["session_ecus"] == 1
    assert brain.get_repo("/git/a/.git")["state"] == "enabled"
    assert brain.list_queue_candidates()[0]["cognition"] == "A1"


def test_copy_from_attached_missing_json_metadata(brain, tmp_path):
    """Defensive: a metadata_json with cluster_memberships default survives."""
    sid = make_session(brain)
    make_candidate(brain, sid)
    brain._conn.execute(
        "UPDATE session_ecus SET document = ? WHERE session_id = ?",
        (json.dumps({"id": "x"}), sid))
    brain._conn.commit()
    export_path = tmp_path / "snap.db"
    make_snapshot(brain, export_path)
    brain.clear_all_data()
    brain._conn.execute("ATTACH ? AS snap", (str(export_path),))
    brain.copy_from_attached("snap")
    brain._conn.execute("DETACH snap")
    assert brain.count_session_ecus(sid) == 1
