"""Smoke-group-5 tests — the status report (items 23, 25, 26, 27, 28, 37).

- 23: session-memory count and brain-wide pending count are separate
- 25: "1 conclusion" / "1 candidate" — never "1 conclusions"
- 26: timestamps show local time WITH the offset
- 27: a branch without a session says so and what happens next
- 28: the first line names the repo; `reverie status --all` lists every
      enabled repo's active sessions
- 37 (must fix): a disabled repo reports "Reverie is off for this repo."
      and never reports a session as active

Run: .venv/bin/python -m pytest tests/test_flow_status.py -v
Offline: temp HOME + REVERIE_HOME; no LLM, no real model.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import re
from datetime import datetime, timezone

import pytest

from reverie.brain import Brain
from reverie.session import main as session_main

T0 = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Temp REVERIE_HOME and HOME (harness config checks must never see the
    real home), no repo env left over from other tests."""
    (tmp_path / "reverie").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "reverie"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("REVERIE_REPO_PATH", raising=False)
    monkeypatch.delenv("REVERIE_BRANCH", raising=False)
    return tmp_path


def _session_ecu(brain, session_id, cognition, confidence=0.5):
    return brain.insert_session_ecu(session_id, {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:x > auth"},
        "provenance": {"source_type": "debugging",
                       "created_at": T0.isoformat()},
        "grounding": {"files": ["auth/token.py"]},
        "evidence_pointers": ["reasoning step 1"],
        "confidence": confidence,
    })


def test_disabled_repo_reports_off(env, capsys):
    """Item 37 (must fix): off means off — no active session reported."""
    repo = str(env / "proj")
    brain = Brain()
    try:
        sid = brain.create_session(repo, "main")
        _session_ecu(brain, sid, "A finding")
        brain.upsert_repo(repo, path=repo, state="disabled")
    finally:
        brain.close()

    code = session_main(["status", "--repo", repo, "--branch", "main"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Reverie is off for this repo." in out
    assert "Active session" not in out
    assert "session memory" not in out


def test_status_first_line_names_repo(env, capsys):
    """Item 28: the first line says which repo (and branch)."""
    repo = str(env / "proj")
    brain = Brain()
    try:
        brain.create_session(repo, "main")
    finally:
        brain.close()

    code = session_main(["status", "--repo", repo, "--branch", "main"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip().splitlines()[0] == \
        f"Reverie status — {repo} (branch main)."
    assert "Active session on branch 'main'" in out


def test_status_all_lists_enabled_repos_only(env, capsys):
    """Item 28: --all walks every enabled repo; disabled ones stay hidden.
    Item 25: a count of one stays singular."""
    repo_a, repo_b, repo_off = (str(env / name)
                                for name in ("a", "b", "off"))
    brain = Brain()
    try:
        sid_a = brain.create_session(repo_a, "main")
        _session_ecu(brain, sid_a, "only one")
        brain.create_session(repo_b, "main")
        sid_off = brain.create_session(repo_off, "main")
        _session_ecu(brain, sid_off, "hidden")
        for path in (repo_a, repo_b):
            brain.upsert_repo(path, path=path, state="enabled")
        brain.upsert_repo(repo_off, path=repo_off, state="disabled")
    finally:
        brain.close()

    code = session_main(["status", "--all"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Reverie status — all repos (2 enabled)." in out
    assert f"{repo_a} [main]" in out
    assert f"{repo_b} [main]" in out
    assert repo_off not in out
    assert "1 conclusion in session memory" in out       # singular, item 25


def test_status_shows_local_time_with_offset(env, capsys):
    """Item 26: the session start time carries the local UTC offset."""
    repo = str(env / "proj")
    brain = Brain()
    try:
        brain.create_session(repo, "main")
    finally:
        brain.close()

    code = session_main(["status", "--repo", repo, "--branch", "main"])
    out = capsys.readouterr().out
    assert code == 0
    assert re.search(
        r"started \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", out)


def test_no_session_message_and_singular_candidate(env, capsys):
    """Item 27: name the branch and the next step; item 25: one candidate."""
    repo = str(env / "proj")
    brain = Brain()
    try:
        sid = brain.create_session(repo, "other-branch")
        _session_ecu(brain, sid, "pending one")
    finally:
        brain.close()

    code = session_main(["status", "--repo", repo, "--branch", "dev"])
    out = capsys.readouterr().out
    assert code == 0
    assert "No session on branch dev yet; one starts on the next " \
           "Reverie call." in out
    assert "1 candidate awaiting review (all sessions)" in out
