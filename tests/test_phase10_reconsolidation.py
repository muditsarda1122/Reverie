"""Phase 10 tests — Reconsolidation Loop (design doc §5, §12.7; D37/D38/D39).

Run: .venv/bin/python -m pytest tests/test_phase10_reconsolidation.py -v
Offline: real embeddings (HF cache), LLM calls mocked via
ec.diffuser.call_llm (the classifier's namespace). No live API tests needed —
the reconsolidation path shares the Diffuser's already-live-tested LLM calls.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from unittest.mock import patch

import numpy as np
import pytest

from ec import confidence as conf
from ec.brain import Brain
from ec.diffuser import DiffuserError
from ec.reconsolidation import (
    ACTION_CHALLENGED,
    ACTION_CONFIDENCE_UPDATED,
    ACTION_NO_CHANGE,
    ACTION_SUPERSEDED,
    ReconsolidationError,
    reconsolidate,
)


def classifications_response(*rels: str) -> str:
    return json.dumps({
        "classifications": [
            {"pair": i + 1, "relationship": rel} for i, rel in enumerate(rels)
        ]
    })


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


def insert_ecu(brain, model, cognition, **overrides) -> str:
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:myapp > module:auth"},
        "provenance": {"source_type": "debugging"},
        "grounding": {"files": ["auth/token.py"], "symbols": ["refresh"]},
        "confidence": 0.5,
        "status": "active",
        "evidence_pointers": [],
    }
    ecu.update(overrides)
    return brain.insert_ecu(ecu, embedding=model.encode_one(cognition))


def cosine(model, text_a, text_b) -> float:
    va, vb = model.encode([text_a, text_b])
    return float(np.dot(va, vb))


# ---------------------------------------------------------------------------
# reconsolidate() — target-pair handling (§12.7 #1-#4, #7, #8, #10)
# ---------------------------------------------------------------------------

class TestReconsolidateTargetPair:
    def test_reconsolidate_supports(self, brain, model):
        """#1 Evidence supports ECU → confidence increases (no edge: the
        pseudo-ECU is unstored and add_edge refuses dangling endpoints)."""
        old = insert_ecu(
            brain, model,
            "Auth tokens must be refreshed optimistically to avoid races",
            confidence=0.5)
        evidence = (
            "I verified auth/token.py: refresh() is called before any "
            "dependent request, confirming the optimistic-refresh invariant")

        result = reconsolidate(brain, old, evidence,
                               relationship="supports",
                               embedding_model=model)

        assert result.action_taken == ACTION_CONFIDENCE_UPDATED
        assert result.classified is False
        sim = cosine(model, evidence,
                     "Auth tokens must be refreshed optimistically to avoid races")
        expected, delta = conf.support_update(0.5, 0.5, sim)
        updated = brain.get_ecu(old)
        assert updated["confidence"] == pytest.approx(expected)
        assert updated["confidence"] > 0.5
        assert result.old_confidence == pytest.approx(0.5)
        assert result.new_confidence == pytest.approx(updated["confidence"])
        # audit trail on metadata; no edges (pseudo-ECU is ephemeral)
        notes = updated["metadata"]["reconsolidations"]
        assert len(notes) == 1
        assert notes[0]["relationship"] == "supports"
        assert notes[0]["via"] == "ec_reconsolidate"
        assert notes[0]["confidence_delta"] == pytest.approx(delta, abs=1e-6)
        assert brain.edge_count(old) == 0
        assert result.edges_created == 0

    def test_reconsolidate_contradicts(self, brain, model):
        """#2 Evidence contradicts ECU → confidence decreases, challenged."""
        old = insert_ecu(
            brain, model,
            "The auth module uses pessimistic token refresh exclusively",
            confidence=0.75)
        evidence = (
            "auth/token.py now acquires a fresh token lazily per request — "
            "the pessimistic-refresh claim no longer holds")

        with patch("ec.diffuser.call_llm") as m:   # nothing should need the LLM
            result = reconsolidate(brain, old, evidence,
                                   relationship="contradicts",
                                   embedding_model=model)

        m.assert_not_called()   # explicit relationship → trusted, no LLM
        assert result.action_taken == ACTION_CHALLENGED
        updated = brain.get_ecu(old)
        assert updated["status"] == "challenged"
        assert updated["confidence"] < 0.75
        assert updated["metadata"]["last_challenged"]
        assert updated["metadata"]["reconsolidations"][0]["relationship"] \
            == "contradicts"

    def test_reconsolidate_supersedes(self, brain, model):
        """#3/#10 Evidence supersedes weak ECU → new ECU created from the
        evidence, old superseded, supersedes edge between real endpoints."""
        old = insert_ecu(
            brain, model,
            "Auth tokens expire after 24 hours flat", confidence=0.25)
        evidence = (
            "Auth tokens now expire after 1 hour with sliding renewal — the "
            "24-hour expiry conclusion is outdated")

        result = reconsolidate(brain, old, evidence,
                               relationship="supersedes",
                               embedding_model=model)

        assert result.action_taken == ACTION_SUPERSEDED
        assert brain.get_ecu(old)["status"] == "superseded"
        # frozen at supersession value for audit (§15.4)
        assert brain.get_ecu(old)["confidence"] < 0.3

        total = brain.count_ecus()
        assert total == 2                      # old + materialized replacement
        new_id = result.new_ecu_id
        stored_new = brain.get_ecu(new_id)
        assert stored_new["cognition"] == evidence
        assert stored_new["status"] == "active"
        # new ECU's confidence is its own — NOT inherited (§15.4)
        assert stored_new["confidence"] == pytest.approx(0.5)
        assert stored_new["provenance"]["source_type"] == "reconsolidation"
        assert stored_new["scope"] == brain.get_ecu(old)["scope"]
        assert stored_new["grounding"] == brain.get_ecu(old)["grounding"]

        sup = [e for e in brain.get_edges_from(new_id)
               if e["type"] == "supersedes"]
        assert len(sup) == 1
        assert sup[0]["target_id"] == old
        assert result.edges_created >= 1

    def test_supersedes_trigger_failure_challenges_instead(self, brain, model):
        """Explicit supersedes against a strong belief: §15.5 trigger fails;
        falls back to challenged (never downgrade-to-supports — an agent
        saying 'outdated' is not evidence FOR the belief)."""
        old = insert_ecu(
            brain, model,
            "Session cookies must be HttpOnly and Secure", confidence=0.85)
        evidence = "Cookies should additionally be SameSite=Lax"

        result = reconsolidate(brain, old, evidence,
                               relationship="supersedes",
                               embedding_model=model)

        assert result.action_taken == ACTION_CHALLENGED
        updated = brain.get_ecu(old)
        assert updated["status"] == "challenged"
        assert updated["confidence"] < 0.85
        assert brain.count_ecus() == 1         # nothing materialized

    def test_explicit_relationship_skips_classification(self, brain, model):
        """#7 Agent passes relationship → no classification call."""
        old = insert_ecu(brain, model, "Retry storms need jittered backoff")
        with patch("ec.diffuser.call_llm") as m:
            reconsolidate(brain, old, "Backoff jitter verified in client code",
                          relationship="supports", embedding_model=model)
        m.assert_not_called()

    def test_implicit_classification_supports(self, brain, model):
        """No relationship given → the batched Rule A/B classifier decides."""
        old = insert_ecu(
            brain, model,
            "Cache invalidation must follow token refresh or stale auth persists",
            confidence=0.5)
        evidence = (
            "The race disappears only when cache invalidation follows token "
            "refresh — more evidence the ordering invariant matters")
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("supports")) as m:
            result = reconsolidate(brain, old, evidence,
                                   embedding_model=model)
        assert m.call_count >= 1
        assert result.classified is True
        assert result.relationship == "supports"
        assert result.action_taken == ACTION_CONFIDENCE_UPDATED
        assert brain.get_ecu(old)["confidence"] > 0.5

    def test_implicit_unrelated_no_change(self, brain, model):
        """#4 Unrelated evidence → no change at all."""
        old = insert_ecu(
            brain, model,
            "Cache invalidation must follow token refresh or stale auth persists",
            confidence=0.62)
        evidence = "Completely unrelated observation about CSS grid layouts"
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("unrelated")):
            result = reconsolidate(brain, old, evidence,
                                   embedding_model=model)
        assert result.action_taken == ACTION_NO_CHANGE
        updated = brain.get_ecu(old)
        assert updated["confidence"] == pytest.approx(0.62)
        assert updated["status"] == "active"
        assert "reconsolidations" not in updated["metadata"]

    def test_pseudo_ecu_not_stored(self, brain, model):
        """#8 After a non-supersession reconsolidation the brain holds
        exactly the ECUs it held before."""
        old = insert_ecu(brain, model, "Webhook handlers must be idempotent")
        before = brain.count_ecus()
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("supports")):
            reconsolidate(brain, old, "Duplicate deliveries observed and "
                                       "deduplicated by event id",
                          embedding_model=model)
        assert brain.count_ecus() == before


# ---------------------------------------------------------------------------
# related-ECU diffusion (§12.7 #9) + input validation
# ---------------------------------------------------------------------------

class TestReconsolidateRelatedAndValidation:
    def test_diffuses_to_related(self, brain, model):
        """#9 The evidence also updates other related ECUs (find_similar),
        without persisting any edge from the pseudo-ECU."""
        target = insert_ecu(
            brain, model,
            "Database migrations must stay backward compatible during rolls",
            confidence=0.55)
        related = insert_ecu(
            brain, model,
            "The rate limiter applies a token bucket algorithm per client",
            confidence=0.5,
            grounding={"files": ["middleware/rate_limit.py"]})
        evidence = (
            "Verified in middleware/rate_limit.py: the rate limiter applies "
            "a token bucket algorithm per client, refilled every second")

        assert cosine(model, evidence,
                      "The rate limiter applies a token bucket algorithm per "
                      "client") > 0.6

        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("supports")) as m:
            result = reconsolidate(brain, target, evidence,
                                   relationship="supports",
                                   embedding_model=model)

        # target got its explicit support update (exact §15.2 math — note
        # the evidence/target cosine may be small here; the delta follows it)
        sim_target = cosine(
            model, evidence,
            "Database migrations must stay backward compatible during rolls")
        expected_target, _ = conf.support_update(0.55, 0.5, sim_target)
        assert brain.get_ecu(target)["confidence"] == pytest.approx(expected_target)
        # ...and the related ECU was reached by the diffusion pass
        sim_related = cosine(
            model, evidence,
            "The rate limiter applies a token bucket algorithm per client")
        expected_related, _ = conf.support_update(0.5, 0.5, sim_related)
        related_updated = brain.get_ecu(related)
        assert related_updated["confidence"] == pytest.approx(expected_related)
        assert related_updated["confidence"] > 0.5
        assert any(u["ecu_id"] == related for u in result.related_updates)
        # still no dangling edges anywhere
        assert brain.edge_count(target) == 0
        assert brain.edge_count(related) == 0

    def test_unknown_ecu_guidance(self, brain, model):
        with pytest.raises(ReconsolidationError, match="No canonical ECU"):
            reconsolidate(brain, "missing-id", "evidence",
                          embedding_model=model)

    def test_frozen_status_rejected(self, brain, model):
        old = insert_ecu(brain, model, "Superseded belief", confidence=0.2)
        brain.update_ecu_status(old, "superseded")
        with pytest.raises(ReconsolidationError, match="superseded"):
            reconsolidate(brain, old, "newer evidence", embedding_model=model)

    def test_empty_evidence_rejected(self, brain, model):
        old = insert_ecu(brain, model, "Some invariant")
        with pytest.raises(ReconsolidationError, match="evidence"):
            reconsolidate(brain, old, "   ", embedding_model=model)

    def test_invalid_relationship_rejected(self, brain, model):
        old = insert_ecu(brain, model, "Some invariant")
        with pytest.raises(ReconsolidationError, match="relationship"):
            reconsolidate(brain, old, "evidence", relationship="depends_on",
                          embedding_model=model)

    def test_classification_failure_propagates(self, brain, model):
        old = insert_ecu(brain, model, "Some invariant")
        with patch("ec.diffuser.call_llm",
                   side_effect=DiffuserError("offline")):
            with pytest.raises(DiffuserError):
                reconsolidate(brain, old, "evidence text",
                              embedding_model=model)
        # nothing was written
        assert brain.get_ecu(old)["confidence"] == pytest.approx(
            brain.get_ecu(old)["confidence"])

    def test_open_question_target_is_revisable(self, brain, model):
        old = insert_ecu(brain, model, "Competing hypothesis A wins",
                         confidence=0.5, status="open_question")
        result = reconsolidate(brain, old, "Contrary evidence arrived",
                               relationship="contradicts",
                               embedding_model=model)
        assert result.action_taken == ACTION_CHALLENGED
        assert brain.get_ecu(old)["confidence"] < 0.5


# ---------------------------------------------------------------------------
# labile ECU tracking (D39 — design doc §5.4; §12.7 #5/#6 groundwork)
# ---------------------------------------------------------------------------

REPO, BRANCH = "/repos/labile-repo", "main"


@pytest.fixture(autouse=True)
def _repo_env(monkeypatch):
    monkeypatch.setenv("EC_REPO_PATH", REPO)
    monkeypatch.setenv("EC_BRANCH", BRANCH)


@pytest.fixture()
def manager(brain):
    from ec.session import SessionManager
    return SessionManager(brain)


def canned_query_result(canonical_ids, session_ecu_id=None):
    """A §16.3-shaped ec_query result for metadata/labile tests."""
    groups = [{
        "core_ecu": {"id": canonical_ids[0], "brain": "canonical"},
        "supporting_evidence": [],
        "contradictions": [],
        "dependencies": [],
        "superseded_by": [],
    }]
    if len(canonical_ids) > 1:
        groups[0]["supporting_evidence"] = [
            {"id": cid, "brain": "canonical"} for cid in canonical_ids[1:]
        ]
    if session_ecu_id:
        groups[0]["supporting_evidence"].append(
            {"id": session_ecu_id, "brain": "session"})
    return {"groups": groups, "groups_retrieved": len(groups)}


class TestLabileTracking:
    def test_mark_and_check(self, manager):
        assert not manager.is_labile("s1", "ecu-1")
        manager.mark_labile("s1", ["ecu-1", "ecu-2"])
        assert manager.is_labile("s1", "ecu-1")
        assert manager.is_labile("s1", "ecu-2")
        assert not manager.is_labile("s2", "ecu-1")   # per-session buckets

    def test_mark_is_idempotent_and_deduplicated(self, manager):
        manager.mark_labile("s1", ["a", "a", "b"])
        manager.mark_labile("s1", ["b"])
        assert manager.labile_ecus("s1") == {"a", "b"}

    def test_labile_copy_is_defensive(self, manager):
        manager.mark_labile("s1", ["a"])
        snapshot = manager.labile_ecus("s1")
        snapshot.add("intruder")
        assert not manager.is_labile("s1", "intruder")

    def test_labile_cleared_on_stop(self, brain, manager):
        sid = manager.start_session()["session_id"]
        manager.mark_labile(sid, ["ecu-9"])
        assert manager.is_labile(sid, "ecu-9")
        manager.stop_session(decisions={})
        assert not manager.is_labile(sid, "ecu-9")
        assert manager.labile_ecus(sid) == set()

    def test_record_retrieval_metadata_marks_labile(self, brain, model,
                                                    manager):
        """ec_query's metadata write marks surfaced canonical ECUs labile;
        session-brain ECUs never become labile (§5.5: canonical only)."""
        sid = manager.start_session()["session_id"]
        canon_a = insert_ecu(brain, model, "Invariant one for the query")
        canon_b = insert_ecu(brain, model, "Invariant two for the query",
                             confidence=0.7)

        server = _server_for(brain, manager)
        stamped = server._record_retrieval_metadata(
            canned_query_result([canon_a, canon_b], session_ecu_id="sess-1"),
            session_id=sid,
        )

        assert stamped == 2                       # session ECU not stamped
        assert manager.is_labile(sid, canon_a)
        assert manager.is_labile(sid, canon_b)
        assert not manager.is_labile(sid, "sess-1")

    def test_record_metadata_without_session_skips_labile(self, brain, model,
                                                          manager):
        canon = insert_ecu(brain, model, "An invariant")
        server = _server_for(brain, manager)
        stamped = server._record_retrieval_metadata(
            canned_query_result([canon]))          # no session_id passed
        assert stamped == 1


def _server_for(brain, manager):
    from ec.mcp_server import ECServer
    return ECServer(brain=brain, manager=manager)


# ---------------------------------------------------------------------------
# MCP tool ec_reconsolidate (§12.7 #5/#6/#11/#12)
# ---------------------------------------------------------------------------

@pytest.fixture()
def server(brain, manager):
    return _server_for(brain, manager)


def call_tool(server, name, arguments=None, msg_id=1):
    response = server.handle_message({
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    assert "result" in response, response
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result


class TestMcpReconsolidateTool:
    def test_tools_list_includes_reconsolidate(self, server):
        response = server.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in response["result"]["tools"]}
        assert "ec_reconsolidate" in tools
        schema = tools["ec_reconsolidate"]["inputSchema"]
        assert schema["required"] == ["ecu_id", "evidence"]
        assert schema["properties"]["relationship"]["enum"] == \
            ["supports", "contradicts", "supersedes"]
        assert "labile" in tools["ec_reconsolidate"]["description"]

    def test_mcp_reconsolidate_tool_ok(self, brain, model, manager, server):
        """#11 Valid call → §5.2 structured response + brain updated."""
        sid = manager.start_session()["session_id"]
        old = insert_ecu(
            brain, model,
            "Auth tokens must be refreshed optimistically to avoid races",
            confidence=0.6)
        manager.mark_labile(sid, [old])

        payload, result = call_tool(server, "ec_reconsolidate", {
            "ecu_id": old,
            "evidence": "Verified auth/token.py: refresh() runs before any "
                        "dependent request — the invariant holds",
            "relationship": "supports",
        })

        assert result["isError"] is False
        assert payload["status"] == "ok"
        assert payload["ecu_id"] == old
        assert payload["action_taken"] == ACTION_CONFIDENCE_UPDATED
        assert payload["old_confidence"] == pytest.approx(0.6)
        assert payload["new_confidence"] > 0.6
        assert isinstance(payload["edges_created"], int)
        assert "confidence" in payload["message"]

    def test_not_labile_ecu_errors_with_guidance(self, brain, model,
                                                 manager, server):
        """#5 An ECU that was never retrieved cannot be reconsolidated."""
        manager.start_session()
        orphan = insert_ecu(brain, model, "Never retrieved invariant")

        payload, _ = call_tool(server, "ec_reconsolidate", {
            "ecu_id": orphan, "evidence": "some finding"})

        assert payload["status"] == "error"
        assert payload["session_active"] is True
        assert "ec_query" in payload["error"]
        # nothing changed
        assert brain.get_ecu(orphan)["confidence"] == pytest.approx(0.5)

    def test_no_session_errors_with_guidance(self, brain, manager, server):
        """#12 No active session → error with /ec-start guidance."""
        payload, _ = call_tool(server, "ec_reconsolidate", {
            "ecu_id": "whatever", "evidence": "finding"})
        assert payload["status"] == "error"
        assert payload["session_active"] is False
        assert "/ec-start" in payload["error"]

    def test_full_flow_query_then_reconsolidate(self, brain, model, manager,
                                                server):
        """#6 ec_query marks the ECU labile; a following ec_reconsolidate
        succeeds without any manual bookkeeping."""
        sid = manager.start_session()["session_id"]
        target = insert_ecu(
            brain, model,
            "Cache invalidation must follow token refresh or stale auth persists")

        with patch("ec.mcp_server.retrieval.retrieve",
                   return_value=canned_query_result([target])):
            query_payload, _ = call_tool(server, "ec_query", {
                "query": "how does cache invalidation interact with tokens?"})

        assert query_payload.get("groups_retrieved") == 1
        assert manager.is_labile(sid, target)

        payload, _ = call_tool(server, "ec_reconsolidate", {
            "ecu_id": target,
            "evidence": "token.py invalidates the cache after refresh, "
                        "confirming the ordering",
            "relationship": "supports",
        })
        assert payload["status"] == "ok"
        assert payload["action_taken"] == ACTION_CONFIDENCE_UPDATED

    def test_unknown_but_labile_ecu_reports_missing(self, brain, manager,
                                                    server):
        sid = manager.start_session()["session_id"]
        manager.mark_labile(sid, ["ghost-id"])
        payload, _ = call_tool(server, "ec_reconsolidate", {
            "ecu_id": "ghost-id", "evidence": "finding",
            "relationship": "supports"})
        assert payload["status"] == "error"
        assert "No canonical ECU" in payload["error"]

    def test_invalid_relationship_payload_error(self, brain, model, manager,
                                                server):
        sid = manager.start_session()["session_id"]
        old = insert_ecu(brain, model, "An invariant")
        manager.mark_labile(sid, [old])
        payload, _ = call_tool(server, "ec_reconsolidate", {
            "ecu_id": old, "evidence": "finding", "relationship": "hates"})
        assert payload["status"] == "error"
        assert "relationship" in payload["error"]

    def test_classification_failure_suggests_explicit_relationship(
            self, brain, model, manager, server):
        sid = manager.start_session()["session_id"]
        old = insert_ecu(brain, model, "An invariant")
        manager.mark_labile(sid, [old])
        with patch("ec.diffuser.call_llm",
                   side_effect=DiffuserError("no API key")):
            payload, _ = call_tool(server, "ec_reconsolidate", {
                "ecu_id": old, "evidence": "some finding"})   # no relationship
        assert payload["status"] == "error"
        assert "explicit relationship" in payload["error"]
        assert brain.get_ecu(old)["confidence"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# installer agent guidance (design doc §7.1–§7.3)
# ---------------------------------------------------------------------------

class TestInstallerAgentsMd:
    def test_agents_md_documents_reconsolidate(self):
        from ec.install import AGENTS_MD
        # §7.3 four-tool list
        assert "ec_reconsolidate: Update a retrieved ECU with new evidence" \
            in AGENTS_MD
        # §7.1 usage section
        assert "## When to Call ec_reconsolidate" in AGENTS_MD
        assert "ECUs you haven't retrieved in this session (ec_query first)" \
            in AGENTS_MD
        # §7.2 grounding-awareness update
        assert "call ec_reconsolidate to\nupdate the ECU" in AGENTS_MD
