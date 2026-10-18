"""Phase 13 tests — polish items (design doc §9 + §10, D56 + D57).

§9 (D56): ``rejection_summary`` is parsed by the extractor since Phase 2 but
was never surfaced in the observe payload — §5.3 says the point is to
"make the filtering visible and debuggable". Now it ships.

§10 (D57): /ec-status must report a pending review count (§28.10 step 3).
``Brain.pending_review_count`` counts unreviewed session ECUs, scoped to the
session's (repo, branch) when one is active; ``brain_summary`` carries it in
the canonical_brain block.

Run: .venv/bin/python -m pytest tests/test_phase13_polish.py -v
Offline: mocked LLM calls for observe; real embeddings.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from unittest.mock import patch

import pytest

from reverie.brain import Brain
from reverie.mcp_server import ReverieServer
from reverie.session import SessionManager, brain_summary
from reverie.session import main as session_main

REPO_A, REPO_B = "/repos/project-a", "/repos/project-b"
BRANCH = "main"

OBSERVE_PAYLOAD = json.dumps({
    "ecus": [
        {
            "cognition": "Token refresh must invalidate the cached session "
                         "scope to avoid stale auth",
            "conclusion_type": "invariant",
            "scope": {"level": "repo", "path": "repo:demo > module:auth"},
            "source_type": "debugging",
            "evidence_pointer": "reasoning step 3",
        },
    ],
    "rejected_count": 2,
    "rejection_summary": "2 raw code descriptions rejected by the Lifting Test",
})

EMPTY_PAYLOAD = json.dumps({
    "ecus": [],
    "rejected_count": 0,
})


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def model():
    from reverie.embeddings import get_embedding_model
    return get_embedding_model()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "reverie.db")
    yield b
    b.close()


@pytest.fixture()
def manager(brain):
    return SessionManager(brain)


def enable_repo(brain):
    """feat/flow: enable the env-detected repo (call AFTER setenv)."""
    from reverie import repos
    repos.set_repo_state(brain, repos.resolve_repo(), "enabled")


@pytest.fixture()
def server(brain, manager, model):
    return ReverieServer(brain=brain, manager=manager, embedding_model=model)


def call_tool(server, name, arguments=None):
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    assert "result" in response, response
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result


def _pending_ecu(brain, sid, cognition):
    return brain.insert_session_ecu(sid, {
        "cognition": cognition,
        "conclusion_type": "decision",
        "scope": {"level": "repo", "path": "repo:x"},
        "provenance": {"source_type": "planning"},
        "confidence": 0.5,
    })


# ---------------------------------------------------------------------------
# §9 / D56 — rejection_summary in the observe payload
# ---------------------------------------------------------------------------

class TestRejectionSummary:
    def test_rejection_summary_in_payload(self, tmp_path, monkeypatch, brain,
                                          server, manager):
        monkeypatch.setenv("REVERIE_REPO_PATH", REPO_A)
        monkeypatch.setenv("REVERIE_BRANCH", BRANCH)
        enable_repo(brain)
        manager.start_session()

        with patch("reverie.extractor.call_llm", return_value=OBSERVE_PAYLOAD), \
             patch("reverie.diffuser.call_llm",
                  return_value=json.dumps({"classifications": []})):
            payload, result = call_tool(server, "observe", {
                "user_prompt": "p",
                "reasoning_trace": "I traced the auth race to refresh order...",
            })

        assert result["isError"] is False
        assert payload["status"] == "ok"
        # D56: the filtering is now visible and debuggable (§5.3)
        assert payload["rejection_summary"] == \
            "2 raw code descriptions rejected by the Lifting Test"
        assert payload["ecus_rejected"] == 2

    def test_rejection_summary_defaults_to_empty_string(self, tmp_path,
                                                        monkeypatch, brain,
                                                        server, manager):
        """Extractor omits the field (e.g. nothing rejected) → empty string,
        never a KeyError."""
        monkeypatch.setenv("REVERIE_REPO_PATH", REPO_A)
        monkeypatch.setenv("REVERIE_BRANCH", BRANCH)
        enable_repo(brain)
        manager.start_session()

        with patch("reverie.extractor.call_llm", return_value=EMPTY_PAYLOAD), \
             patch("reverie.diffuser.call_llm",
                  return_value=json.dumps({"classifications": []})):
            payload, _ = call_tool(server, "observe", {
                "user_prompt": "p", "reasoning_trace": "r"})

        assert payload["status"] == "ok"
        assert payload["rejection_summary"] == ""


# ---------------------------------------------------------------------------
# §10 / D57 — Brain.pending_review_count
# ---------------------------------------------------------------------------

class TestPendingReviewCount:
    def test_counts_pending_rows(self, brain):
        sid = brain.create_session(REPO_A, BRANCH)
        for i in range(3):
            _pending_ecu(brain, sid, f"Finding {i}")

        assert brain.pending_review_count() == 3
        assert brain.pending_review_count(REPO_A, BRANCH) == 3

    def test_scoped_to_repo_and_branch(self, brain):
        """Only the requested working context counts."""
        s_a = brain.create_session(REPO_A, BRANCH)
        s_b = brain.create_session(REPO_B, BRANCH)
        s_a2 = brain.create_session(REPO_A, "feature")   # same repo, other branch
        for i in range(3):
            _pending_ecu(brain, s_a, f"A{i}")
        _pending_ecu(brain, s_b, "B0")
        _pending_ecu(brain, s_a2, "A-feature")

        assert brain.pending_review_count() == 5                 # global
        assert brain.pending_review_count(REPO_A, BRANCH) == 3   # scoped
        assert brain.pending_review_count(REPO_B, BRANCH) == 1
        assert brain.pending_review_count(REPO_A, "feature") == 1

    def test_reviewed_rows_stop_counting(self, brain):
        """Accepted/rejected rows are removed at stop and skipped ones carry
        their own status — 'pending' means genuinely unreviewed."""
        sid = brain.create_session(REPO_A, BRANCH)
        p1 = _pending_ecu(brain, sid, "Will be accepted")
        p2 = _pending_ecu(brain, sid, "Will be skipped")
        _pending_ecu(brain, sid, "Stays pending")
        assert brain.pending_review_count(REPO_A, BRANCH) == 3

        brain.update_session_ecu_review_status(p1, "accepted")
        brain.update_session_ecu_review_status(p2, "skipped")
        assert brain.pending_review_count(REPO_A, BRANCH) == 1


# ---------------------------------------------------------------------------
# §10 — brain_summary carries it (summary + /ec-status payload)
# ---------------------------------------------------------------------------

class TestSummaryIntegration:
    def test_active_session_reports_both_counts_separately(self, brain):
        sid = brain.create_session(REPO_A, BRANCH)
        for i in range(3):
            _pending_ecu(brain, sid, f"Finding {i}")
        row = brain.get_active_session(REPO_A, BRANCH)

        summary = brain_summary(brain, row)

        assert summary["canonical_brain"]["pending_review_count"] == 3
        assert "3 candidates awaiting review (all sessions)" \
            in summary["message"]
        assert "3 conclusions in this session's memory" in summary["message"]

    def test_zero_pending_still_shows_the_zero_count(self, brain):
        brain.create_session(REPO_A, BRANCH)
        row = brain.get_active_session(REPO_A, BRANCH)

        summary = brain_summary(brain, row)

        assert summary["canonical_brain"]["pending_review_count"] == 0
        assert "0 candidates awaiting review (all sessions)" \
            in summary["message"]

    def test_sessionless_reports_brain_wide_count(self, brain):
        s_a = brain.create_session(REPO_A, BRANCH)
        s_b = brain.create_session(REPO_B, BRANCH)
        for i in range(2):
            _pending_ecu(brain, s_a, f"A{i}")
        _pending_ecu(brain, s_b, "B0")

        summary = brain_summary(brain, None)      # no active session

        assert summary["canonical_brain"]["pending_review_count"] == 3
        assert "3 candidates awaiting review (all sessions)" \
            in summary["message"]

    def test_status_cli_prints_pending_line(self, tmp_path, monkeypatch,
                                            capsys):
        """/ec-status surfaces the count through its printed message
        (§28.10 step 3)."""
        home = tmp_path / "reverie_home"
        home.mkdir()
        monkeypatch.setenv("REVERIE_HOME", str(home))
        b = Brain(home / "reverie.db")
        sid = b.create_session(REPO_A, BRANCH)
        for i in range(2):
            _pending_ecu(b, sid, f"Unreviewed {i}")
        b.close()

        code = session_main(["status", "--repo", REPO_A, "--branch", BRANCH])
        out = capsys.readouterr().out

        assert code == 0
        assert "2 candidates awaiting review (all sessions)" in out
