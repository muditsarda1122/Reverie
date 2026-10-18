"""feat/providers — pending-extraction queue and re-diffusion tests.

Offline: LLM calls mocked (reverie.extractor.call_llm / reverie.pending.diffuse_ecu);
embeddings real (HF cache); brains in tmp_path. The `reverie retry` CLI runs in
process against a redirected REVERIE_HOME.
"""

import copy
import json
import os
from unittest.mock import patch

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest

from reverie import pending
from reverie.brain import Brain
from reverie.config import DEFAULT_CONFIG, _to_attrdict
from reverie.diffuser import DiffuserError
from reverie.llm import LLMError
from reverie.mcp_server import ReverieServer
from reverie.session import SessionManager

REPO, BRANCH = "/repos/retry-repo", "main"

EXTRACT_PAYLOAD = json.dumps({
    "ecus": [{
        "cognition": "Route ordering shadows static paths after parameterized ones",
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:retry-repo"},
        "source_type": "debugging",
        "grounding": {"files": ["app/routes.py"]},
        "evidence_pointer": "reasoning step 2",
    }],
    "rejected_count": 0,
    "rejection_summary": "",
})

INTERACTION = {
    "prompt": "Why does /items/latest 422?",
    "reasoning_trace": "Parameterized route declared first shadows the static one.",
    "session_context": {"session_id": "replaced-by-server"},
}


def config(**llm_overrides):
    cfg = _to_attrdict(copy.deepcopy(DEFAULT_CONFIG))
    cfg.llm.provider = "custom"
    for k, v in llm_overrides.items():
        cfg.llm[k] = v
    return cfg


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "reverie.db")
    yield b
    b.close()


@pytest.fixture()
def manager(brain):
    return SessionManager(brain)


@pytest.fixture()
def server(brain, manager):
    from reverie import repos
    s = ReverieServer(brain=brain, manager=manager, config=config())
    repos.set_repo_state(brain, repos.resolve_repo(), "enabled")  # feat/flow
    return s


@pytest.fixture(autouse=True)
def _repo_env(monkeypatch):
    monkeypatch.setenv("REVERIE_REPO_PATH", REPO)
    monkeypatch.setenv("REVERIE_BRANCH", BRANCH)


@pytest.fixture()
def active_session(manager):
    return manager.start_session()["session_id"]


@pytest.fixture(scope="session")
def model():
    from reverie.embeddings import get_embedding_model
    return get_embedding_model()


def call_tool(server, name, arguments=None, msg_id=1):
    response = server.handle_message({
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    assert "result" in response, response
    payload = json.loads(response["result"]["content"][0]["text"])
    return payload, response["result"]


# ---------------------------------------------------------------------------
# brain storage layer
# ---------------------------------------------------------------------------

class TestPendingExtractionStorage:
    def test_add_list_count_delete(self, brain):
        item_id = brain.add_pending_extraction(
            REPO, BRANCH, dict(INTERACTION), session_id="s1",
            origin_agent="m", commit_hash="abc", error="down")
        assert brain.count_pending_extractions() == 1
        items = brain.list_pending_extractions()
        assert items[0]["id"] == item_id
        assert items[0]["repo_path"] == REPO
        assert items[0]["interaction"]["prompt"] == INTERACTION["prompt"]
        assert items[0]["retry_count"] == 0
        assert items[0]["origin_agent"] == "m"
        brain.delete_pending_extraction(item_id)
        assert brain.count_pending_extractions() == 0

    def test_repo_branch_scoping(self, brain):
        brain.add_pending_extraction(REPO, BRANCH, dict(INTERACTION))
        brain.add_pending_extraction("/other", "dev", dict(INTERACTION))
        assert brain.count_pending_extractions() == 2
        assert brain.count_pending_extractions(repo_path=REPO) == 1
        assert brain.count_pending_extractions(repo_path=REPO,
                                                branch=BRANCH) == 1
        assert brain.count_pending_extractions(repo_path="/none") == 0

    def test_mark_attempt_bumps_and_records(self, brain):
        item_id = brain.add_pending_extraction(REPO, BRANCH, dict(INTERACTION))
        brain.mark_pending_extraction_attempt(item_id, "still down")
        item = brain.list_pending_extractions()[0]
        assert item["retry_count"] == 1
        assert item["error"] == "still down"
        assert item["last_attempt_at"]
        with pytest.raises(Exception):     # unknown id
            brain.mark_pending_extraction_attempt("nope", "x")


# ---------------------------------------------------------------------------
# retry_pending
# ---------------------------------------------------------------------------

class TestRetryPending:
    def _save(self, brain, active_session):
        return pending.save_pending_extraction(
            brain, {"id": active_session, "repo_path": REPO, "branch": BRANCH},
            dict(INTERACTION), "LLM down", commit_hash="abc123")

    def test_retry_recovers_into_active_session(self, brain, active_session, model):
        self._save(brain, active_session)
        with patch("reverie.extractor.call_llm", return_value=EXTRACT_PAYLOAD):
            summary = pending.retry_pending(brain, embedding_model=model)
        assert summary["recovered"] == 1
        assert brain.count_pending_extractions() == 0
        ecus = brain.list_session_ecus(active_session)
        assert len(ecus) == 1
        assert "Route ordering" in ecus[0]["cognition"]

    def test_retry_without_active_session_skips(self, brain, model):
        brain.add_pending_extraction("/ghost-repo", "main", dict(INTERACTION))
        summary = pending.retry_pending(brain, embedding_model=model)
        assert summary["skipped"] == 1
        assert summary["recovered"] == 0
        assert brain.count_pending_extractions() == 1
        assert "next tool call" in summary["details"][0]["reason"]

    def test_retry_failure_records_attempt_and_keeps_item(
            self, brain, active_session, model):
        item_id = self._save(brain, active_session)
        with patch("reverie.extractor.call_llm",
                   side_effect=LLMError("HTTP 503: still down")):
            summary = pending.retry_pending(brain, embedding_model=model)
        assert summary["failed"] == 1
        assert summary["recovered"] == 0
        item = brain.list_pending_extractions()[0]
        assert item["id"] == item_id
        assert item["retry_count"] == 1
        assert "503" in item["error"]

    def test_retry_item_id_filter(self, brain, active_session, model):
        self._save(brain, active_session)
        other = brain.add_pending_extraction(REPO, BRANCH, dict(INTERACTION))
        with patch("reverie.extractor.call_llm", return_value=EXTRACT_PAYLOAD):
            summary = pending.retry_pending(brain, item_id=other,
                                            embedding_model=model)
        assert summary["recovered"] == 1
        assert brain.count_pending_extractions() == 1   # the untargeted one stays


# ---------------------------------------------------------------------------
# observe integration: persist on failure, auto-retry on success
# ---------------------------------------------------------------------------

OBSERVE_ARGS = {
    "user_prompt": "Why does /items/latest 422?",
    "reasoning_trace": "Parameterized route declared first shadows the static one.",
}


class TestObservePersistence:
    def test_failed_observe_persists_interaction(self, server, active_session):
        with patch("reverie.extractor.call_llm",
                   side_effect=LLMError("HTTP 503: gateway down")):
            payload, envelope = call_tool(server, "observe", OBSERVE_ARGS)
        assert envelope["isError"] is True
        assert payload["pending_extraction_id"]
        assert "saved locally" in payload["error"]
        assert "reverie retry" in payload["error"]
        assert server._brain.count_pending_extractions() == 1

    def test_next_successful_observe_retries_pending(
            self, server, active_session, model):
        with patch("reverie.extractor.call_llm",
                   side_effect=LLMError("HTTP 503: gateway down")):
            failed, _ = call_tool(server, "observe", OBSERVE_ARGS)
        assert failed["status"] == "error"
        with patch("reverie.extractor.call_llm", return_value=EXTRACT_PAYLOAD):
            ok, _ = call_tool(server, "observe", OBSERVE_ARGS)
        assert ok["status"] == "ok"
        assert ok["pending_recovered"] == 1
        assert "recovered" in ok["message"]
        assert server._brain.count_pending_extractions() == 0
        assert len(server._brain.list_session_ecus(active_session)) == 2

    def test_successful_observe_with_no_pending_is_untouched(
            self, server, active_session):
        with patch("reverie.extractor.call_llm", return_value=EXTRACT_PAYLOAD):
            payload, _ = call_tool(server, "observe", OBSERVE_ARGS)
        assert payload["pending_recovered"] == 0
        assert "recovered" not in payload["message"]


# ---------------------------------------------------------------------------
# re-diffusion of undiffused ECUs
# ---------------------------------------------------------------------------

class TestRediffuse:
    def _make_ecu(self, brain, model) -> str:
        return brain.insert_ecu({
            "cognition": "Webhook handlers must be idempotent",
            "conclusion_type": "invariant",
            "scope": {"level": "repo", "path": "repo:retry-repo"},
            "provenance": {"source_type": "session"},
            "confidence": 0.6,
            "status": "active",
        }, embedding=model.encode_one("webhook handlers idempotent"))

    def test_review_gate_failure_flags_metadata(self, brain, manager, model):
        session_id = manager.start_session()["session_id"]
        ecu_id = brain.insert_session_ecu(
            session_id,
            {"cognition": "Webhook handlers must be idempotent",
             "confidence": 0.6, "status": "active",
             "scope": {"level": "repo", "path": "repo:retry-repo"},
             "conclusion_type": "invariant"},
            embedding=model.encode_one("webhook handlers idempotent"))
        with patch("reverie.diffuser.diffuse_ecu",
                   side_effect=DiffuserError("relationship classification "
                                             "failed: LLMError offline")):
            from reverie import review_gate
            result = review_gate.apply_review_decisions(
                brain, session_id, {ecu_id: "accept"}, embedding_model=model,
                config=config())
        assert len(result.undiffused) == 1
        ecu = brain.get_ecu(result.undiffused[0])
        assert ecu["metadata"]["diffusion_failed"] is True
        assert brain.list_undiffused_ecus() == [ecu["id"]]

    def test_rediffuse_clears_flag_on_success(self, brain, model):
        ecu_id = self._make_ecu(brain, model)
        brain.update_ecu_metadata(ecu_id, diffusion_failed=True)
        with patch("reverie.pending.diffuse_ecu"):
            summary = pending.rediffuse(brain, embedding_model=model)
        assert summary["rediffused"] == 1
        assert brain.get_ecu(ecu_id)["metadata"]["diffusion_failed"] is False
        assert brain.list_undiffused_ecus() == []

    def test_rediffuse_keeps_flag_on_failure(self, brain, model):
        ecu_id = self._make_ecu(brain, model)
        brain.update_ecu_metadata(ecu_id, diffusion_failed=True)
        with patch("reverie.pending.diffuse_ecu",
                   side_effect=DiffuserError("LLM API returned HTTP 503")):
            summary = pending.rediffuse(brain, embedding_model=model)
        assert summary["failed"] == 1
        assert brain.get_ecu(ecu_id)["metadata"]["diffusion_failed"] is True
        assert brain.list_undiffused_ecus() == [ecu_id]

    def test_rediffuse_target_id_scope(self, brain, model):
        a = self._make_ecu(brain, model)
        b = self._make_ecu(brain, model)
        for ecu in (a, b):
            brain.update_ecu_metadata(ecu, diffusion_failed=True)
        with patch("reverie.pending.diffuse_ecu"):
            summary = pending.rediffuse(brain, ecu_id=a, embedding_model=model)
        assert summary["targets"] == 1
        assert brain.get_ecu(a)["metadata"]["diffusion_failed"] is False
        assert brain.get_ecu(b)["metadata"]["diffusion_failed"] is True

    def test_rediffuse_missing_ecu_reported(self, brain):
        summary = pending.rediffuse(brain, ecu_id="ghost-id")
        assert summary["missing"] == 1


# ---------------------------------------------------------------------------
# reverie retry CLI
# ---------------------------------------------------------------------------

class TestRetryCli:
    @pytest.fixture()
    def reverie_home(self, tmp_path, monkeypatch):
        home = tmp_path / "ec-home"
        monkeypatch.setenv("REVERIE_HOME", str(home))
        return home

    def _session_and_pending(self) -> str:
        """Session + pending item in the REVERIE_HOME brain (what the CLI reads)."""
        b = Brain()          # uses REVERIE_HOME
        try:
            sid = SessionManager(b).start_session()["session_id"]
            return pending.save_pending_extraction(
                b, {"id": sid, "repo_path": REPO, "branch": BRANCH},
                dict(INTERACTION), "LLM down")
        finally:
            b.close()

    def _brain(self):
        return Brain()          # uses REVERIE_HOME

    def test_list_empty(self, reverie_home, capsys):
        assert pending.main(["--list"]) == 0
        assert "No pending extractions" in capsys.readouterr().out

    def test_list_and_default_retry(self, reverie_home, capsys, model):
        self._session_and_pending()
        assert pending.main(["--list"]) == 0
        assert REPO in capsys.readouterr().out
        with patch("reverie.extractor.call_llm", return_value=EXTRACT_PAYLOAD):
            assert pending.main([]) == 0
        assert "recovered" in capsys.readouterr().out
        b = self._brain()
        try:
            assert b.count_pending_extractions() == 0
        finally:
            b.close()

    def test_drop(self, reverie_home, capsys):
        item_id = self._session_and_pending()
        assert pending.main(["--drop", item_id]) == 0
        assert "Dropped" in capsys.readouterr().out
        b = self._brain()
        try:
            assert b.count_pending_extractions() == 0
        finally:
            b.close()

    def test_rediffuse_flag(self, reverie_home, capsys):
        b = self._brain()
        ecu_id = b.insert_ecu({
            "cognition": "x", "conclusion_type": "pattern",
            "scope": {"level": "repo", "path": "r"},
            "provenance": {"source_type": "session"},
            "confidence": 0.5, "status": "active",
        })
        b.update_ecu_metadata(ecu_id, diffusion_failed=True)
        b.close()
        with patch("reverie.pending.diffuse_ecu"):
            assert pending.main(["--rediffuse"]) == 0
        assert "rediffused" in capsys.readouterr().out
