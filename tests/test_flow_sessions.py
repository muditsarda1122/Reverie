"""feat/flow tests — automatic sessions (D56): ensure_session, branch
switching mid-conversation, detached HEAD, worktrees, labile tracking and
activation decay without any session close.

Run: .venv/bin/python -m pytest tests/test_flow_sessions.py -v
Real git repositories under tmp_path; no LLM calls.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ec.brain import Brain
from ec.repos import archive_missing_branch_sessions, branch_exists
from ec.session import SessionManager

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

REPO_A = "/repos/project-a"


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


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-b", "main")
    (r / "f.txt").write_text("one\n")
    git(r, "add", "-A")
    git(r, "commit", "-m", "one")
    return r


def make_candidate(brain, session_id, cognition="C", confidence=0.5,
                   review_status="pending"):
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:a > module:auth"},
        "provenance": {"source_type": "debugging"},
        "confidence": confidence,
    }
    cid = brain.insert_session_ecu(session_id, ecu)
    if review_status != "pending":
        brain.update_session_ecu_review_status(cid, review_status)
    return cid


# ---------------------------------------------------------------------------
# ensure_session — resolve or create (D56)
# ---------------------------------------------------------------------------

def test_ensure_creates_then_resumes(manager, brain):
    a = manager.ensure_session(REPO_A, "main")
    b = manager.ensure_session(REPO_A, "main")
    assert a["id"] == b["id"]
    assert brain.get_session(a["id"])["status"] == "active"


def test_ensure_carries_over_unreviewed_like_ec_start(manager, brain):
    """Same carry-over contract as §28.5 /ec-start step 5."""
    first = manager.ensure_session(REPO_A, "main")["id"]
    cid = make_candidate(brain, first, "left behind")
    skipped = make_candidate(brain, first, "skipped once",
                             review_status="skipped")
    brain.close_session(first)             # never reviewed
    second = manager.ensure_session(REPO_A, "main")["id"]
    assert second != first
    rows = brain.list_session_ecus(second)
    assert {r["id"] for r in rows} == {cid, skipped}


def test_ensure_archived_session_does_not_carry(manager, brain):
    """Archived sessions' candidates stay in the review queue instead."""
    first = manager.ensure_session(REPO_A, "main")["id"]
    cid = make_candidate(brain, first, "orphan")
    brain.archive_session(first)
    second = manager.ensure_session(REPO_A, "main")["id"]
    assert second != first
    assert brain.list_session_ecus(second) == []
    assert brain.get_queue_candidate(cid)["cognition"] == "orphan"


# ---------------------------------------------------------------------------
# branch switches mid-conversation (D56)
# ---------------------------------------------------------------------------

def test_branch_switch_switches_session_memory(manager, brain):
    main_a = manager.ensure_session(REPO_A, "main")["id"]
    feat = manager.ensure_session(REPO_A, "feat/auth")["id"]
    assert feat != main_a
    make_candidate(brain, feat, "branch-specific insight")
    back = manager.ensure_session(REPO_A, "main")["id"]
    assert back == main_a                        # switched back: same session
    assert brain.get_session(back)["status"] == "active"
    # the other branch's session is untouched and still active
    assert brain.get_session(feat)["status"] == "active"
    assert brain.count_session_ecus(feat) == 1


def test_labile_tracking_across_branch_switch(manager, brain):
    """Labile state is per session — a switch forgets the other branch's
    retrieved ECUs (D39/D56)."""
    main_s = manager.ensure_session(REPO_A, "main")["id"]
    feat_s = manager.ensure_session(REPO_A, "feat/auth")["id"]
    manager.mark_labile(main_s, ["ecu-1"])
    assert manager.is_labile(main_s, "ecu-1")
    assert not manager.is_labile(feat_s, "ecu-1")


def test_activation_decays_without_close(manager, brain):
    """Nothing depends on a close: persisted activation decays on its own
    when the session is next loaded (§6.6)."""
    sid = manager.ensure_session(REPO_A, "main")["id"]
    old = T0 - timedelta(days=2)
    brain.save_activation(sid, {"ecu-1": (0.8, old.isoformat())})
    state = manager.activation_for(sid, now=T0)
    assert state.get("ecu-1") < 0.8              # decayed on load
    assert brain.get_session(sid)["status"] == "active"


# ---------------------------------------------------------------------------
# git-reality: detached HEAD, worktrees
# ---------------------------------------------------------------------------

def test_detached_head_gets_own_session(manager, brain, repo):
    git(repo, "checkout", "--detach", "HEAD")
    from ec.session import detect_repo_branch
    repo_path, branch = detect_repo_branch(cwd=repo)
    assert branch.startswith("detached:")
    sid = manager.ensure_session(repo_path, branch)["id"]
    other = manager.ensure_session(repo_path, "main")
    assert other["id"] != sid


def test_two_worktrees_different_branches(manager, brain, repo, tmp_path):
    """Two worktrees on different branches → two sessions; both share the
    same repo *identity* for enablement (D58)."""
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-b", "feat/wt", str(wt))
    from ec.repos import resolve_repo
    info_main = resolve_repo(cwd=repo)
    info_wt = resolve_repo(cwd=wt)
    assert info_main.repo_path != info_wt.repo_path
    assert info_main.branch == "main" and info_wt.branch == "feat/wt"
    assert info_main.repo_key == info_wt.repo_key   # same repo
    s1 = manager.ensure_session(info_main.repo_path, info_main.branch)
    s2 = manager.ensure_session(info_wt.repo_path, info_wt.branch)
    assert s1["id"] != s2["id"]


# ---------------------------------------------------------------------------
# archiving: branch no longer exists (D56)
# ---------------------------------------------------------------------------

def test_branch_exists_checks_local_refs(repo):
    git(repo, "branch", "short-lived")
    assert branch_exists(str(repo), "short-lived") is True
    git(repo, "branch", "-D", "short-lived")
    assert branch_exists(str(repo), "short-lived") is False
    assert branch_exists(str(repo), "main") is True


def test_branch_exists_detached_tracks_commit(repo):
    head = git(repo, "rev-parse", "--short", "HEAD")
    assert branch_exists(str(repo), f"detached:{head}") is True
    assert branch_exists(str(repo), "detached:deadbeef") is False


def test_branch_exists_missing_repo_path(tmp_path):
    assert branch_exists(str(tmp_path / "gone"), "main") is False


def test_archive_missing_branch_sessions(brain, manager, repo):
    dead = manager.ensure_session(str(repo), "short-lived")["id"]
    alive = manager.ensure_session(str(repo), "main")["id"]
    cid = make_candidate(brain, dead, "queued candidate")
    git(repo, "branch", "short-lived")           # ensure it existed...
    git(repo, "branch", "-D", "short-lived")     # ...then delete it
    archived = archive_missing_branch_sessions(brain, str(repo), skip_branch="main")
    assert archived == [dead]
    assert brain.get_session(dead)["status"] == "archived"
    assert brain.get_session(alive)["status"] == "active"
    # its unreviewed candidates stay in the review queue
    assert brain.get_queue_candidate(cid) is not None
