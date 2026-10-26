"""Smoke-group-4 tests — candidate quality at extraction time.

Items covered:
- 10: the instructions say observe is called AT MOST ONCE per task
- 11: grounding references are validated BEFORE a candidate is stored;
      unverifiable ones are dropped and recorded (grounding.validation)
- 12: the evidence standard — evidence without a file/test/commit reference
      is stored as metadata.evidence_status="unsupported" and appears in
      the review listing (tool + TUI)
- 13: candidates carry grounding.repo_path — the field the maintainer and
      the `reverie repos` count filter on
- 14: observe's result message always counts extracted and rejected
- 17: the reconsolidate tool description states the evidence standard
- 46: reconsolidate refusals name the failed check (not_found /
      not_canonical / not_labile)

Run: .venv/bin/python -m pytest tests/test_flow_candidate_quality.py -v
Offline: call_llm and the embedding model are faked; real git for the
commit-mode checks.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
import subprocess

import numpy as np
import pytest

from reverie import extractor, grounding, install, mcp_server, review_gate
from reverie.brain import Brain
from reverie.mcp_server import ReverieServer
from reverie.session import SessionManager

REPO, BRANCH = "/repos/quality-repo", "main"


class FakeModel:
    """Deterministic 8-dim embeddings — no real model load."""

    def encode_one(self, text):
        return np.zeros(8, dtype=np.float32)


@pytest.fixture()
def model():
    return FakeModel()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "reverie.db")
    yield b
    b.close()


@pytest.fixture()
def manager(brain):
    return SessionManager(brain)


@pytest.fixture(autouse=True)
def _repo_env(monkeypatch):
    monkeypatch.setenv("REVERIE_REPO_PATH", REPO)
    monkeypatch.setenv("REVERIE_BRANCH", BRANCH)


@pytest.fixture()
def server(brain, manager, model):
    s = ReverieServer(brain=brain, manager=manager, embedding_model=model)
    payload, _ = call_tool(s, "set_repo_state", {"state": "enabled"})
    assert payload["status"] == "ok"
    return s


@pytest.fixture()
def git_repo(tmp_path):
    """A real repo with one commit; returns (path, HEAD)."""
    r = tmp_path / "git-project"
    (r / "auth").mkdir(parents=True)
    (r / "auth" / "token.py").write_text(
        "class TokenManager:\n"
        "    def refresh(self):\n        pass\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"], cwd=r, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True,
    ).stdout.strip()
    return r, head


def call_tool(server, name, arguments=None, msg_id=1):
    response = server.handle_message({
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    assert "result" in response, response
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result


def extraction_payload(ecus, rejected=0, summary=""):
    return json.dumps({"ecus": ecus, "rejected_count": rejected,
                       "rejection_summary": summary})


def raw_ecu(cognition="Connection pools must be sized for peak concurrency",
            files=None, symbols=None, evidence="agent reasoning step 3"):
    return {
        "cognition": cognition,
        "conclusion_type": "constraint",
        "scope": {"level": "repo", "path": "repo:x > module:db"},
        "source_type": "debugging",
        "grounding": {"files": files or [], "symbols": symbols or []},
        "evidence_pointer": evidence,
    }


# ---------------------------------------------------------------------------
# item 11 — validate_references (pure)
# ---------------------------------------------------------------------------

class TestValidateReferences:
    def test_files_checked_at_captured_commit(self, git_repo):
        repo, head = git_repo
        (repo / "auth" / "token.py").unlink()      # deleted since capture
        out = grounding.validate_references(
            str(repo), ["auth/token.py", "auth/ghost.py"], [],
            commit_hash=head)
        assert out["checked"] is True
        assert out["mode"] == "commit"
        assert out["valid_files"] == ["auth/token.py"]
        assert out["invalid_files"] == ["auth/ghost.py"]

    def test_worktree_fallback_without_commit(self, git_repo):
        repo, _ = git_repo
        out = grounding.validate_references(
            str(repo), ["auth/token.py", "nope.py"], [])
        assert out["mode"] == "worktree"
        assert out["valid_files"] == ["auth/token.py"]
        assert out["invalid_files"] == ["nope.py"]

    def test_symbols_searched_in_existing_files(self, git_repo):
        repo, _ = git_repo
        out = grounding.validate_references(
            str(repo), ["auth/token.py"],
            ["TokenManager", "TotallyMissingThing"])
        assert out["valid_symbols"] == ["TokenManager"]
        assert out["invalid_symbols"] == ["TotallyMissingThing"]

    def test_missing_repo_verifies_nothing(self, tmp_path):
        out = grounding.validate_references(
            str(tmp_path / "gone"), ["x.py"], ["sym"])
        assert out["checked"] is False
        assert out["mode"] == "unavailable"
        assert out["valid_files"] == ["x.py"]      # kept untouched
        assert out["valid_symbols"] == ["sym"]


# ---------------------------------------------------------------------------
# items 11/12/13 — extraction-time stamping + validation
# ---------------------------------------------------------------------------

class TestExtractionStamping:
    def _store(self, brain, model, repo, head, ecu):
        sid = brain.create_session(str(repo), "main")
        payload = extraction_payload([ecu])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(extractor, "call_llm", lambda *a, **k: payload)
            _result, ids = extractor.extract_and_store_session(
                brain, sid, {"prompt": "p", "reasoning_trace": "t"},
                embedding_model=model, commit_hash=head,
                repo_path=str(repo),
            )
        return brain.get_session_ecu(ids[0])

    def test_repo_path_stamped_for_repos_count(self, brain, model,
                                               git_repo):
        repo, head = git_repo
        stored = self._store(brain, model, repo, head, raw_ecu(
            files=["auth/token.py"], evidence="auth/token.py:2"))
        assert stored["grounding"]["repo_path"] == str(repo)

    def test_invalid_references_dropped_and_recorded(self, brain, model,
                                                     git_repo):
        repo, head = git_repo
        stored = self._store(brain, model, repo, head, raw_ecu(
            files=["auth/token.py", "auth/ghost.py"],
            symbols=["TokenManager", "GhostThing"],
            evidence="auth/token.py:2"))
        g = stored["grounding"]
        assert g["files"] == ["auth/token.py"]
        assert g["symbols"] == ["TokenManager"]
        assert g["validation"]["dropped_files"] == ["auth/ghost.py"]
        assert g["validation"]["dropped_symbols"] == ["GhostThing"]

    def test_evidence_standard_stamped(self, brain, model, git_repo):
        repo, head = git_repo
        supported = self._store(brain, model, repo, head, raw_ecu(
            files=["auth/token.py"], evidence="auth/token.py:2"))
        assert supported["metadata"]["evidence_status"] == "supported"
        unsupported = self._store(brain, model, repo, head, raw_ecu(
            files=["auth/token.py"], evidence="the user said so"))
        assert unsupported["metadata"]["evidence_status"] == "unsupported"

    def test_promotion_keeps_repo_path_so_count_works(self, brain, model,
                                                      git_repo):
        """reverie repos counts list_ecus_for_repo(grounding.repo_path) —
        the promoted canonical ECU must match that filter."""
        repo, head = git_repo
        sid = brain.create_session(str(repo), "main")
        payload = extraction_payload([raw_ecu(
            files=["auth/token.py"], evidence="auth/token.py:2")])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(extractor, "call_llm", lambda *a, **k: payload)
            _result, ids = extractor.extract_and_store_session(
                brain, sid, {"prompt": "p", "reasoning_trace": "t"},
                embedding_model=model, commit_hash=head,
                repo_path=str(repo),
            )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("reverie.diffuser.call_llm",
                       lambda *a, **k: '{"edges": []}')
            review_gate.apply_review_decisions(
                brain, sid, {ids[0]: "accept"}, embedding_model=model)
        assert brain.get_ecu(ids[0]) is not None
        assert len(brain.list_ecus_for_repo(str(repo))) == 1


# ---------------------------------------------------------------------------
# item 12 — the review listing surfaces unsupported evidence
# ---------------------------------------------------------------------------

def test_review_list_flags_unsupported_evidence(server, brain, manager):
    payload = {"user_prompt": "p", "reasoning_trace": "t"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(extractor, "call_llm",
                   lambda *a, **k: extraction_payload([raw_ecu(
                       files=[], evidence="the agent just felt it")]))
        mp.setattr("reverie.diffuser.diffuse_session_ecu", lambda *a, **k: None)
        observed, _ = call_tool(server, "observe", payload)
        assert observed["status"] == "ok", observed
    listing, _ = call_tool(server, "review_list", {})
    assert "evidence: UNSUPPORTED" in listing["message"]
    assert listing["candidates"][0]["evidence_status"] == "unsupported"


def test_render_screen_shows_unsupported_marker():
    candidate = {
        "id": "c1", "cognition": "A conclusion", "conclusion_type": "invariant",
        "scope_path": "repo:x", "confidence": 0.5,
        "repo_path": "/r", "branch": "main",
    }
    from reverie.review_cli import render_screen
    screen = render_screen(candidate, {"evidence_status": "unsupported"},
                           index=1, total=1, progress={})
    assert "evidence: UNSUPPORTED" in screen
    plain = render_screen(candidate, {}, index=1, total=1, progress={})
    assert "evidence: UNSUPPORTED" not in plain


# ---------------------------------------------------------------------------
# item 14 — observe always counts extracted and rejected
# ---------------------------------------------------------------------------

class TestObserveCounts:
    def _observe(self, server, ecus, rejected):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(extractor, "call_llm",
                       lambda *a, **k: extraction_payload(ecus,
                                                          rejected=rejected))
            mp.setattr("reverie.diffuser.diffuse_session_ecu",
                       lambda *a, **k: None)
            return call_tool(server, "observe",
                             {"user_prompt": "p", "reasoning_trace": "t"})[0]

    def test_extracted_message_counts_both(self, server):
        payload = self._observe(server, [raw_ecu(evidence="a.py:1")], 2)
        assert payload["ecus_extracted"] == 1
        assert payload["ecus_rejected"] == 2
        assert "Extracted 1 conclusion" in payload["message"]
        assert "2 rejected by the Lifting Test" in payload["message"]

    def test_zero_extracted_still_counts_rejected(self, server):
        payload = self._observe(server, [], 3)
        assert payload["ecus_extracted"] == 0
        assert payload["ecus_rejected"] == 3
        assert "Extracted 0 conclusions" in payload["message"]
        assert "3 candidates rejected by the Lifting Test" in payload["message"]


# ---------------------------------------------------------------------------
# items 10/17 — instruction wording
# ---------------------------------------------------------------------------

def test_observe_says_at_most_once_per_task():
    assert "AT MOST ONCE per task" in install.AGENTS_MD
    assert "AT MOST ONCE per task" in mcp_server._OBSERVE_DESCRIPTION


def test_reconsolidate_description_states_evidence_standard():
    d = mcp_server._RECONSOLIDATE_DESCRIPTION
    assert "file path with line numbers, a test name, or a commit" in d
    assert "not_found" in d and "not_canonical" in d and "not_labile" in d
    assert "line numbers, a test name, or a commit hash" in install.AGENTS_MD


# ---------------------------------------------------------------------------
# item 46 — reconsolidate refusal reasons
# ---------------------------------------------------------------------------

class TestReconsolidateReasons:
    def test_not_found(self, server):
        payload, _ = call_tool(server, "reconsolidate",
                               {"ecu_id": "missing-id", "evidence": "x"})
        assert payload["status"] == "error"
        assert payload["reason"] == "not_found"

    def test_not_canonical(self, server, brain, manager):
        sid = manager.ensure_session(REPO, BRANCH)["id"]
        cid = brain.insert_session_ecu(sid, {
            "cognition": "Still an unreviewed candidate",
            "conclusion_type": "invariant",
            "scope": {"level": "repo", "path": "repo:x"},
            "provenance": {"source_type": "debugging"},
            "confidence": 0.5,
        })
        payload, _ = call_tool(server, "reconsolidate",
                               {"ecu_id": cid, "evidence": "x"})
        assert payload["reason"] == "not_canonical"

    def test_not_labile(self, server, brain, model):
        cid = brain.insert_ecu({
            "cognition": "Canonical but never retrieved this session",
            "conclusion_type": "invariant",
            "scope": {"level": "repo", "path": "repo:x"},
            "provenance": {"source_type": "debugging"},
            "confidence": 0.7,
        }, embedding=model.encode_one("x"))
        payload, _ = call_tool(server, "reconsolidate",
                               {"ecu_id": cid, "evidence": "a.py:1"})
        assert payload["reason"] == "not_labile"

    def _labile(self, brain, manager, model, cognition="A labile belief"):
        sid = manager.ensure_session(REPO, BRANCH)["id"]
        cid = brain.insert_ecu({
            "cognition": cognition,
            "conclusion_type": "invariant",
            "scope": {"level": "repo", "path": "repo:x"},
            "provenance": {"source_type": "debugging"},
            "confidence": 0.7,
        }, embedding=model.encode_one(cognition))
        manager.mark_labile(sid, [cid])
        return cid

    # item 17 — same evidence standard as item 12
    def test_unsupported_evidence_refused(self, server, brain, manager,
                                          model):
        cid = self._labile(brain, manager, model)
        before = brain.get_ecu(cid)["confidence"]
        payload, _ = call_tool(server, "reconsolidate", {
            "ecu_id": cid, "evidence": "the user said so",
            "relationship": "supports"})
        assert payload["status"] == "error"
        assert payload["reason"] == "unsupported_evidence"
        assert brain.get_ecu(cid)["confidence"] == before

    def test_verifiable_evidence_accepted(self, server, brain, manager,
                                          model):
        cid = self._labile(brain, manager, model)
        payload, _ = call_tool(server, "reconsolidate", {
            "ecu_id": cid,
            "evidence": "auth/token.py:12 confirms the refresh ordering",
            "relationship": "supports"})
        assert payload["status"] == "ok", payload
