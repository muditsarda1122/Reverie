"""Phase 4 tests — session lifecycle (§28.5/§28.6) + brain summary (§16.3).

Run: .venv/bin/python -m pytest tests/ -v
Offline everywhere: no LLM calls (review decisions here use skip/reject or
mocked-unrelated diffusion), real embeddings only where a fixture needs them.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ec.activation import ActivationState
from ec.brain import Brain
from ec.session import (
    SessionError,
    SessionManager,
    brain_summary,
    detect_repo_branch,
)

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 2, 1, 9, 0, 0, tzinfo=timezone.utc)

REPO_A, REPO_B = "/repos/project-a", "/repos/project-b"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EC_REPO_PATH", raising=False)
    monkeypatch.delenv("EC_BRANCH", raising=False)


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture()
def manager(brain):
    return SessionManager(brain)


def make_ecu(cognition: str, **overrides) -> dict:
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:project-a > module:auth"},
        "provenance": {"source_type": "debugging", "source_id": "s1"},
        "grounding": {"files": ["auth/token.py"], "symbols": ["refresh"]},
        "confidence": 0.5,
        "status": "active",
        "evidence_pointers": [],
    }
    ecu.update(overrides)
    return ecu


# ---------------------------------------------------------------------------
# repo/branch detection (§28.5 /ec-start step 1)
# ---------------------------------------------------------------------------

def test_detect_repo_branch_git(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    repo_path, branch = detect_repo_branch(cwd=repo)
    assert repo_path == str(repo.resolve())
    assert branch == "main"


def test_detect_repo_branch_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("EC_REPO_PATH", str(tmp_path / "x"))
    monkeypatch.setenv("EC_BRANCH", "feature/auth")
    repo_path, branch = detect_repo_branch()
    assert repo_path == str((tmp_path / "x").resolve())
    assert branch == "feature/auth"


def test_detect_repo_branch_not_a_repo(tmp_path):
    with pytest.raises(SessionError, match="git repository"):
        detect_repo_branch(cwd=tmp_path)


# ---------------------------------------------------------------------------
# /ec-start — create, resume, concurrent sessions (§28.5)
# ---------------------------------------------------------------------------

def test_start_creates_session(manager, brain):
    report = manager.start_session(REPO_A, "main")
    assert report["status"] == "ok"
    assert report["resumed"] is False
    assert report["carried_over"] == 0
    assert "Started EC session on branch main in project-a." == report["message"]
    row = brain.get_active_session(REPO_A, "main")
    assert row is not None and row["id"] == report["session_id"]
    # Summary rides the start report ONCE (awareness without anchoring, §11.3)
    summary = report["summary"]
    assert summary["session"]["active"] is True
    assert summary["session"]["branch"] == "main"
    assert "canonical_brain" in summary and "total_ecus" in summary["canonical_brain"]
    # fresh activation state
    assert len(manager.activation_for(report["session_id"])) == 0


def test_start_twice_resumes(manager):
    first = manager.start_session(REPO_A, "main")
    second = manager.start_session(REPO_A, "main")
    assert second["resumed"] is True
    assert second["session_id"] == first["session_id"]
    assert second["message"] == (
        "Resuming EC session on branch main (0 ECUs in session brain)."
    )


def test_resume_loads_activation_with_decay(manager, brain):
    sid = manager.start_session(REPO_A, "main")["session_id"]
    state = ActivationState(sid)
    state._scores["ecu-1"] = (1.0, T0)
    state.save(brain)

    # A new manager (e.g. restarted MCP server) resumes 15h later (§6.6)
    manager2 = SessionManager(brain)
    later = T0 + timedelta(hours=15)
    loaded = manager2.activation_for(sid, now=later)
    assert loaded.get("ecu-1") == pytest.approx(math.exp(-1.5), rel=1e-3)


def test_multiple_concurrent_sessions(manager, brain):
    """§28.5: one active session per (repo, branch); many across repos."""
    s1 = manager.start_session(REPO_A, "feature/auth")
    s2 = manager.start_session(REPO_B, "main")
    s3 = manager.start_session(REPO_A, "bugfix/cache")
    assert len({s1["session_id"], s2["session_id"], s3["session_id"]}) == 3
    active = brain.list_sessions(status="active")
    assert len(active) == 3
    # resolving per (repo, branch) routes correctly
    assert manager.current_session(REPO_A, "bugfix/cache")["id"] == s3["session_id"]
    assert manager.current_session(REPO_B, "main")["id"] == s2["session_id"]
    # second start on the same branch resumes, never duplicates
    assert manager.start_session(REPO_A, "feature/auth")["session_id"] == s1["session_id"]
    assert len(brain.list_sessions(status="active")) == 3


# ---------------------------------------------------------------------------
# carry-over of unreviewed ECUs (§28.5 step 5)
# ---------------------------------------------------------------------------

def test_carry_over_skipped_ecus(manager, brain):
    s1 = manager.start_session(REPO_A, "main")["session_id"]
    e1 = brain.insert_session_ecu(s1, make_ecu("First unreviewed finding"))
    e2 = brain.insert_session_ecu(s1, make_ecu("Second unreviewed finding"))
    c1 = brain.insert_ecu(make_ecu("Canonical belief about auth tokens"))
    brain.add_session_edge(s1, e1, e2, "supports", weight=0.8)
    brain.add_pending_update(c1, e1, s1, "supports", 0.07)
    brain.update_ecu_metadata(c1, has_pending_updates=True)

    manager.stop_session(REPO_A, "main", decisions={e1: "skip", e2: "skip"})
    assert brain.get_session(s1)["status"] == "closed"

    report = manager.start_session(REPO_A, "main")
    s2 = report["session_id"]
    assert s2 != s1 and report["resumed"] is False
    assert report["carried_over"] == 2
    assert report["session_brain_count"] == 2
    assert "carried over" in report["message"]
    # ECUs, their session edge, and the pending update all moved to s2
    assert {e["id"] for e in brain.list_session_ecus(s2)} == {e1, e2}
    assert [e["session_id"] for e in brain.list_session_edges(s2)] == [s2]
    moved_pu = brain.list_pending_updates(session_id=s2, status="pending")
    assert len(moved_pu) == 1 and moved_pu[0]["session_ecu_id"] == e1


def test_no_carry_over_when_fully_reviewed(manager, brain):
    s1 = manager.start_session(REPO_A, "main")["session_id"]
    e1 = brain.insert_session_ecu(s1, make_ecu("A rejected finding"))
    manager.stop_session(REPO_A, "main", decisions={e1: "reject"})
    report = manager.start_session(REPO_A, "main")
    assert report["carried_over"] == 0
    assert report["session_brain_count"] == 0


# ---------------------------------------------------------------------------
# /ec-stop — close semantics (§28.6)
# ---------------------------------------------------------------------------

def test_stop_closes_session(manager, brain):
    sid = manager.start_session(REPO_A, "main")["session_id"]
    e1 = brain.insert_session_ecu(sid, make_ecu("Deferred finding"))
    state = manager.activation_for(sid)
    state._scores["ecu-x"] = (0.5, T0)
    state.save(brain)

    result = manager.stop_session(REPO_A, "main", decisions={})  # 'done' path
    row = brain.get_session(sid)
    assert row["status"] == "closed" and row["ended_at"] is not None
    assert result.skipped == [e1]
    assert brain.get_session_ecu(e1)["review_status"] == "skipped"
    # activation rows dropped (§12.2 fresh slate) and cache evicted
    assert brain.load_activation(sid) == {}
    assert sid not in manager._activations


def test_stop_without_active_session(manager):
    with pytest.raises(SessionError, match="No active EC session"):
        manager.stop_session(REPO_A, "main")


def test_double_stop_raises(manager, brain):
    manager.start_session(REPO_A, "main")
    manager.stop_session(REPO_A, "main", decisions={})
    with pytest.raises(SessionError, match="No active EC session"):
        manager.stop_session(REPO_A, "main", decisions={})


# ---------------------------------------------------------------------------
# brain summary (§16.3 ec_get_summary)
# ---------------------------------------------------------------------------

def test_brain_summary_stats(brain):
    brain.insert_ecu(make_ecu("Belief one"))
    brain.insert_ecu(make_ecu(
        "Belief two", scope={"level": "engineering", "path": "engineering"}))
    c3 = brain.insert_ecu(make_ecu("Contested belief"))
    brain.update_ecu_status(c3, "challenged")

    summary = brain_summary(brain, None)
    canonical = summary["canonical_brain"]
    assert canonical["total_ecus"] == 3
    assert canonical["by_scope"]["repo"] == 2
    assert canonical["by_scope"]["engineering"] == 1
    assert canonical["by_scope"]["module"] == 0
    assert canonical["by_status"] == {
        "active": 2, "challenged": 1, "superseded": 0, "open_question": 0,
    }
    assert canonical["last_ecu_added"] is not None
    assert canonical["last_maintenance_run"] is None
    assert summary["session"]["active"] is False
    assert "The EC brain has 3 ECUs" in summary["message"]


def test_brain_summary_with_session(brain, manager):
    sid = manager.start_session(REPO_A, "main")["session_id"]
    brain.insert_session_ecu(sid, make_ecu("Session finding"))
    row = brain.get_active_session(REPO_A, "main")
    summary = brain_summary(brain, row)
    block = summary["session"]
    assert block["active"] is True
    assert block["session_brain_count"] == 1
    assert block["branch"] == "main" and block["repo"] == REPO_A
    assert block["started_at"] == row["started_at"]


# ---------------------------------------------------------------------------
# CLI smoke (§28.10 templates invoke python -m ec.session)
# ---------------------------------------------------------------------------

def test_cli_start_status_subprocess(tmp_path):
    env = dict(os.environ)
    env["EC_HOME"] = str(tmp_path / "ec_home")
    env["EC_REPO_PATH"] = "/repos/cli-repo"
    env["EC_BRANCH"] = "main"
    env["PYTHONPATH"] = str(ROOT)
    base = [sys.executable, "-m", "ec.session"]

    start = subprocess.run(base + ["start"], env=env, cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
    assert start.returncode == 0, start.stderr
    assert "Started EC session on branch main in cli-repo." in start.stdout

    again = subprocess.run(base + ["start"], env=env, cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
    assert "Resuming EC session on branch main" in again.stdout

    status = subprocess.run(base + ["status"], env=env, cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert status.returncode == 0
    assert "Active session on branch 'main'" in status.stdout

    stop = subprocess.run(base + ["stop", "--all-skip"], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert stop.returncode == 0, stop.stderr
    assert "Review complete." in stop.stdout


def test_cli_stop_all_accept_surfaces_diffusion_failure(tmp_path):
    """The non-interactive /ec-stop path prints the prominent warning when
    promotion succeeded but the full diffuser could not run (here: no API
    key in the environment, so the classification call fails)."""
    env = dict(os.environ)
    env["EC_HOME"] = str(tmp_path / "ec_home")
    env["EC_REPO_PATH"] = "/repos/cli-repo"
    env["EC_BRANCH"] = "main"
    env["PYTHONPATH"] = str(ROOT)
    env["HF_HUB_OFFLINE"] = "1"
    env.pop("OPENCODE_ZEN_API_KEY", None)  # the diffuser's LLM call will fail
    base = [sys.executable, "-m", "ec.session"]
    start = subprocess.run(base + ["start"], env=env, cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
    assert start.returncode == 0, start.stderr

    # seed the shared DB (WAL: the CLI subprocess sees these rows): one
    # canonical ECU + one similar session ECU (verified sim >= 0.6 forces
    # the classification call, which then fails without a key)
    from ec.embeddings import get_embedding_model
    model = get_embedding_model()
    brain = Brain(tmp_path / "ec_home" / "ec.db")
    row = brain.get_active_session("/repos/cli-repo", "main")
    assert row is not None
    canonical = "Token refresh must precede cache clear in the auth flow"
    session = "The auth bug was stale tokens from un-ordered refresh/clear"
    brain.insert_ecu(make_ecu(canonical),
                     embedding=model.encode_one(canonical))
    s = brain.insert_session_ecu(row["id"], make_ecu(session),
                                 embedding=model.encode_one(session))
    brain.close()

    stop = subprocess.run(base + ["stop", "--all-accept"], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert stop.returncode == 0, stop.stderr
    assert "Review complete. 1 accepted" in stop.stdout
    assert "⚠️ WARNING: 1 ECU(s) promoted without diffusion due to API " \
           "errors" in stop.stdout
    assert s in stop.stdout
    assert "Consider re-running diffusion manually" in stop.stdout
