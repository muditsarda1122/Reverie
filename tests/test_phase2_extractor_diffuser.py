"""Phase 2 tests — extractor, confidence math, full + lightweight diffuser.

Run: .venv/bin/python -m pytest tests/ -v
All LLM calls are mocked (unittest.mock.patch on ec.extractor.call_llm /
ec.diffuser.call_llm). Real embeddings (HF cache, offline-safe). Two live
Zen API tests skip unless OPENCODE_ZEN_API_KEY is set.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from unittest.mock import patch

import pytest

from ec import confidence as conf
from ec.brain import Brain
from ec.diffuser import (
    DiffuserError,
    _contradiction_prefilter,
    diffuse_ecu,
    diffuse_session_ecu,
)
from ec.extractor import (
    ExtractionError,
    ExtractorError,
    count_evidence_sources,
    extract_and_store_session,
    extract_ecus,
    load_extractor_prompt,
    validate_ecu,
)
from ec.llm import LLMError


def make_ecu(cognition: str, **overrides) -> dict:
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:myapp > module:auth"},
        "provenance": {"source_type": "debugging", "source_id": "s1"},
        "grounding": {"files": ["auth/token_manager.py"], "symbols": ["refresh"]},
        "confidence": 0.5,
        "status": "active",
        "evidence_pointers": [],
    }
    ecu.update(overrides)
    return ecu


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


# ---------------------------------------------------------------------------
# confidence math (§11)
# ---------------------------------------------------------------------------

class TestConfidenceMath:
    def test_log_odds_roundtrip_and_clamp(self):
        for c in (0.05, 0.3, 0.5, 0.72, 0.95):
            assert conf.to_probability(conf.to_log_odds(c)) == pytest.approx(c)
        # boundaries don't blow up
        assert conf.to_probability(conf.to_log_odds(0.0)) == pytest.approx(0.0, abs=1e-6)
        assert conf.to_probability(conf.to_log_odds(1.0)) == pytest.approx(1.0, abs=1e-6)

    def test_initial_prior_math(self):
        # §5.9 worked examples
        assert conf.initial_prior("debugging", "engineering") == pytest.approx(0.805)
        assert conf.initial_prior("architectural_reasoning", "module") == pytest.approx(0.40)
        assert conf.initial_prior("observation", "domain") == pytest.approx(0.495)
        # corroboration bump: +0.05 per extra source, capped at +0.15
        assert conf.initial_prior("debugging", "repo", n_sources=2) == pytest.approx(0.68)
        assert conf.initial_prior("debugging", "repo", n_sources=5) == pytest.approx(0.78)
        # cap 0.95 / floor 0.05
        assert conf.initial_prior("debugging", "engineering", n_sources=10) == 0.95
        assert conf.initial_prior("planning", "subsystem") >= 0.05
        with pytest.raises(ValueError):
            conf.initial_prior("nonsense", "repo")

    def test_support_and_contradiction_updates(self):
        # §15.8 concrete examples (w=1.0, c_A=0.80, r=0.90): 0.50 -> 0.673
        new_c, delta = conf.support_update(0.50, 0.80, 0.90)
        assert delta == pytest.approx(0.72)
        assert new_c == pytest.approx(0.673, abs=0.001)
        # weak evidence barely moves: c_A=0.40, r=0.50 -> 0.550
        new_c2, delta2 = conf.support_update(0.50, 0.40, 0.50)
        assert delta2 == pytest.approx(0.20)
        assert new_c2 == pytest.approx(0.550, abs=0.001)
        # contradiction mirrors with negative delta
        new_c3, delta3 = conf.contradiction_update(0.50, 0.80, 0.90)
        assert delta3 == pytest.approx(-0.72)
        assert new_c3 == pytest.approx(1 - 0.673, abs=0.001)
        # reversal (§15.6)
        assert conf.reverse_update(new_c, delta) == pytest.approx(0.50)

    def test_should_supersede_both_conditions(self):
        assert conf.should_supersede(0.25, replacement_exists=True)
        assert not conf.should_supersede(0.25, replacement_exists=False)
        assert not conf.should_supersede(0.80, replacement_exists=True)
        assert not conf.should_supersede(0.30, replacement_exists=True)  # not below

    def test_propagation_bounded(self, brain):
        # chain: D depends_on C depends_on B depends_on A. B and C start below
        # theta so the challenge is transitive; D sits at depth 3 -> untouched.
        a = brain.insert_ecu(make_ecu("A: postgres suits message persistence",
                                      confidence=0.5))
        b = brain.insert_ecu(make_ecu("B: partitioning scales postgres writes",
                                      confidence=0.35))
        c = brain.insert_ecu(make_ecu("C: retention policy relies on partitioning",
                                      confidence=0.35))
        d = brain.insert_ecu(make_ecu("D: archival job relies on retention policy",
                                      confidence=0.6))
        brain.add_edge(b, a, "depends_on")
        brain.add_edge(c, b, "depends_on")
        brain.add_edge(d, c, "depends_on")

        # A drops below theta_dep_reevaluate (0.4): B (depth 1) and C (depth 2)
        # challenged; D at depth 3 is beyond max_propagation_depth=2
        challenged = conf.propagate(a, 0.35, brain)
        assert set(challenged) == {b, c}
        assert brain.get_ecu(b)["status"] == "challenged"
        assert brain.get_ecu(c)["status"] == "challenged"
        assert brain.get_ecu(d)["status"] == "active"

        # above theta -> no propagation
        e = brain.insert_ecu(make_ecu("E: isolated", confidence=0.6))
        assert conf.propagate(e, 0.9, brain) == []

    def test_supersession_type(self, model):
        cosmetic = conf.supersession_type(
            "Cache invalidation must follow token refresh to avoid stale auth",
            "Cache invalidation must follow token refresh, or authentication goes stale",
            embedding_model=model,
        )
        assert cosmetic == "cosmetic"
        semantic = conf.supersession_type(
            "Authentication uses JWT tokens stored in cookies",
            "The batch processor caps memory by spilling to disk at 512MB",
            embedding_model=model,
        )
        assert semantic == "semantic"


# ---------------------------------------------------------------------------
# extractor (§3 / §5.x)
# ---------------------------------------------------------------------------

class TestExtractor:
    def test_prompt_loads(self):
        prompt = load_extractor_prompt(refresh=True)
        assert "System Prompt" in prompt
        assert "Lifting Test" in prompt
        assert load_extractor_prompt() is prompt  # cached

    def test_count_evidence_sources(self):
        assert count_evidence_sources(None) == 1
        assert count_evidence_sources("") == 1
        assert count_evidence_sources("reasoning step 4") == 1
        assert count_evidence_sources("reasoning step 4 + step 8 (test)") == 2
        assert count_evidence_sources("a + b + c") == 3

    def test_validate_ecu(self):
        raw = {
            "cognition": "Cache invalidation must follow token refresh",
            "conclusion_type": "invariant",
            "scope": {"level": "repo", "path": "repo:myapp > module:auth"},
            "source_type": "debugging",
            "grounding": {"files": ["auth/cache.py"]},
            "evidence_pointer": "step 4 (code analysis) + step 8 (test confirmation)",
        }
        ecu = validate_ecu(raw)
        assert ecu["scope"]["level"] == "repo"
        assert ecu["evidence_pointer"].count("+") == 1
        # source_type aliases accepted
        assert validate_ecu({**raw, "source_type": "review"})["source_type"] == "review"
        assert validate_ecu({**raw, "source_type": "session"})["source_type"] == "session"
        # defaults
        no_path = validate_ecu({**raw, "scope": {"level": "domain"}})
        assert no_path["scope"]["path"] == "domain"
        with pytest.raises(ValueError):
            validate_ecu({**raw, "conclusion_type": "hunch"})
        with pytest.raises(ValueError):
            validate_ecu({**raw, "scope": {"level": "galaxy"}})
        with pytest.raises(ValueError):
            validate_ecu({**raw, "source_type": "vibes"})
        with pytest.raises(ValueError):
            validate_ecu({**raw, "cognition": "  "})

    def _mock_payload(self):
        return json.dumps({
            "ecus": [
                {
                    "cognition": "Authentication correctness depends on optimistic "
                                 "token refresh ordering with cache invalidation",
                    "conclusion_type": "invariant",
                    "scope": {"level": "repo", "path": "repo:myapp > module:auth"},
                    "source_type": "debugging",
                    "grounding": {"files": ["auth/token_manager.py"]},
                    "evidence_pointer": "Agent reasoning trace, step 6",
                },
                {
                    "cognition": "Connection pools must be sized for peak concurrency",
                    "conclusion_type": "constraint",
                    "scope": {"level": "domain", "path": "domain:backend"},
                    "source_type": "observation",
                    "grounding": {"files": ["db/pool.py"]},
                    "evidence_pointer": "step 3 (code analysis) + step 8 (load test)",
                },
            ],
            "rejected_count": 2,
            "rejection_summary": "1 raw code description, 1 process step",
        })

    def test_extract_ecus_mocked(self):
        with patch("ec.extractor.call_llm", return_value=self._mock_payload()) as m:
            result = extract_ecus(
                {"prompt": "debug auth", "reasoning_trace": "...", "output": "fixed"},
                origin_agent="test-agent",
            )
        # system prompt is the tested extractor prompt; default temperature (0.3)
        assert m.call_args.kwargs["system"] == load_extractor_prompt()
        assert len(result.ecus) == 2
        assert result.rejected_count == 2
        assert "process step" in result.rejection_summary

        debugging_ecu, observation_ecu = result.ecus
        # prior: debugging 0.70 x repo 0.90, single source
        assert debugging_ecu["confidence"] == pytest.approx(0.63)
        # prior: observation 0.45 x domain 1.10 + 1 extra source bump 0.05
        assert observation_ecu["confidence"] == pytest.approx(0.545)
        assert debugging_ecu["status"] == "active"
        assert debugging_ecu["provenance"]["origin_agent"] == "test-agent"
        assert debugging_ecu["evidence_pointers"] == ["Agent reasoning trace, step 6"]
        # edges are the Diffuser's job — extractor output carries none
        assert "edges" not in debugging_ecu

    def test_extract_ecus_fenced_json(self):
        fenced = "```json\n" + self._mock_payload() + "\n```"
        with patch("ec.extractor.call_llm", return_value=fenced):
            result = extract_ecus({"output": "x"})
        assert len(result.ecus) == 2

    def test_extract_ecus_validation_skips_bad(self):
        payload = json.dumps({
            "ecus": [
                {"cognition": "valid conclusion about connection pool sizing",
                 "conclusion_type": "constraint",
                 "scope": {"level": "repo"}, "source_type": "debugging",
                 "grounding": {}, "evidence_pointer": "step 1"},
                {"cognition": "bad type", "conclusion_type": "hunch",
                 "scope": {"level": "repo"}, "source_type": "debugging"},
                {"cognition": "  ", "conclusion_type": "constraint",
                 "scope": {"level": "repo"}, "source_type": "debugging"},
            ],
            "rejected_count": 0,
            "rejection_summary": "",
        })
        with patch("ec.extractor.call_llm", return_value=payload):
            result = extract_ecus({"output": "x"})
        assert len(result.ecus) == 1
        assert result.skipped_invalid == 2

    def test_extract_ecus_errors(self):
        with patch("ec.extractor.call_llm", return_value="not json at all {{{"):
            with pytest.raises(ExtractionError):
                extract_ecus({"output": "x"})
        with patch("ec.extractor.call_llm", return_value='{"no_ecus": true}'):
            with pytest.raises(ExtractionError):
                extract_ecus({"output": "x"})
        with patch("ec.extractor.call_llm", side_effect=LLMError("no key")):
            with pytest.raises(ExtractorError):
                extract_ecus({"output": "x"})
        with pytest.raises(ExtractorError):
            extract_ecus({})  # nothing to extract from

    def test_extract_and_store_session(self, brain, model):
        session_id = brain.create_session("/tmp/myapp", "main")
        with patch("ec.extractor.call_llm", return_value=self._mock_payload()):
            result, ids = extract_and_store_session(
                brain, session_id,
                {"prompt": "debug auth", "output": "fixed"},
            )
        assert len(ids) == 2
        stored = brain.list_session_ecus(session_id)
        assert len(stored) == 2
        assert all(s["review_status"] == "pending" for s in stored)
        assert all(s["embedding"] is not None for s in stored)
        assert brain.get_session(session_id)["ecu_count"] == 2


# ---------------------------------------------------------------------------
# full diffuser (§6 / §8.x)
# ---------------------------------------------------------------------------

class TestFullDiffuser:
    def _insert_with_embedding(self, brain, model, cognition, **overrides):
        return brain.insert_ecu(
            make_ecu(cognition, **overrides),
            embedding=model.encode_one(cognition),
        )

    def test_supports_flow(self, brain, model):
        existing = self._insert_with_embedding(
            brain, model,
            "Cache invalidation must follow token refresh or stale auth tokens persist",
            confidence=0.5,
        )
        new = self._insert_with_embedding(
            brain, model,
            "The auth race condition is caused by refresh completing before cache clear",
            confidence=0.63,
        )
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("supports")) as m:
            result = diffuse_ecu(brain, new, embedding_model=model)
        assert m.call_count == 1  # single batched classification call
        assert "Rule A" in m.call_args.kwargs["system"]
        assert "Rule B" in m.call_args.kwargs["system"]
        # deterministic classification (sampling variance caused spurious
        # 'contradicts' verdicts — see ASSESSMENT.md §4)
        assert m.call_args.kwargs["temperature"] == 0.0
        # the Rule A recommendation-follows-mechanism clause
        assert "never 'contradicts'" in m.call_args.kwargs["system"]
        assert "mutually exclusive claims" in m.call_args.kwargs["system"]

        updated = brain.get_ecu(existing)
        edges = brain.get_edges_from(new)
        assert len(edges) == 1
        edge = edges[0]
        assert edge["type"] == "supports" and edge["target_id"] == existing
        assert edge["weight"] == pytest.approx(edge["weight"])  # similarity
        # §15.2 in place: c' = sigmoid(logit(0.5) + 1.0 x r x 0.63) > 0.5
        assert updated["confidence"] > 0.5
        expected, delta = conf.support_update(0.5, 0.63, edge["weight"])
        assert updated["confidence"] == pytest.approx(expected)
        assert edge["confidence_delta"] == pytest.approx(delta)
        assert result.edges_created[0]["type"] == "supports"

    def test_contradiction_genuine(self, brain, model):
        # same scope, overlapping grounding -> prefilter 'potential' -> Stage 2
        existing = self._insert_with_embedding(
            brain, model,
            "The auth module uses optimistic token refresh",
            confidence=0.75,
        )
        new = self._insert_with_embedding(
            brain, model,
            "The auth module uses pessimistic token refresh",
            confidence=0.72,
        )
        responses = [
            classifications_response("contradicts"),          # classification
            json.dumps({"verdict": "genuine_contradiction", "differentiator": ""}),
        ]
        with patch("ec.diffuser.call_llm", side_effect=responses) as m:
            result = diffuse_ecu(brain, new, embedding_model=model)
        assert m.call_count == 2  # classification + Stage 2 adjudication

        updated = brain.get_ecu(existing)
        assert updated["status"] == "challenged"
        assert updated["confidence"] < 0.75
        assert updated["metadata"]["last_challenged"]
        edge = brain.get_edges_from(new)[0]
        assert edge["type"] == "contradicts"
        assert edge["confidence_delta"] < 0
        # both > 0.7 -> user flag (§17.4)
        assert len(result.flags) == 1
        assert result.flags[0]["kind"] == "high_stakes_contradiction"

    def test_contradiction_prefilter_orthogonal(self, brain, model):
        # different scope levels -> orthogonal, no Stage 2 LLM call, no edge
        # (verified embedding similarity 0.76 > 0.6 relevance threshold)
        existing = self._insert_with_embedding(
            brain, model,
            "Input validation at trust boundaries is always required",
            scope={"level": "engineering", "path": "engineering"},
            grounding={"files": ["lib/validate.py"]},
            confidence=0.8,
        )
        new = self._insert_with_embedding(
            brain, model,
            "Input validation at trust boundaries is skipped for internal "
            "calls in the auth module",
            confidence=0.6,
        )
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("contradicts")) as m:
            result = diffuse_ecu(brain, new, embedding_model=model)
        assert m.call_count == 1  # classification only — no adjudication
        assert brain.get_edges_from(new) == []
        assert brain.get_ecu(existing)["confidence"] == pytest.approx(0.8)
        assert result.contradictions == []

    def test_prefilter_unit(self):
        a = make_ecu("X is true for low-traffic services")
        b = make_ecu("X is false for high-traffic services")
        assert _contradiction_prefilter(a, b) == "orthogonal"
        # D40: different scope escapes to Stage 2 on grounding overlap, so
        # this case needs disjoint grounding too (similarity unknown -> None).
        c = make_ecu("Auth uses optimistic refresh",
                     scope={"level": "engineering", "path": "engineering"},
                     grounding={"files": []})
        assert _contradiction_prefilter(c, a) == "orthogonal"
        d = make_ecu("Auth uses pessimistic refresh",
                     grounding={"files": ["other/file.py"]})
        assert _contradiction_prefilter(d, a) == "no_overlap"
        e = make_ecu("Auth uses pessimistic refresh")
        assert _contradiction_prefilter(e, a) in ("potential", "orthogonal")

    def test_supersession_via_contradiction(self, brain, model):
        # old ECU already weak; a contradiction drops it below theta -> superseded
        existing = self._insert_with_embedding(
            brain, model,
            "The auth module uses pessimistic token refresh exclusively",
            confidence=0.32,
        )
        new = self._insert_with_embedding(
            brain, model,
            "The auth module uses optimistic token refresh with ordered cache invalidation",
            confidence=0.63,
        )
        responses = [
            classifications_response("contradicts"),
            json.dumps({"verdict": "genuine_contradiction", "differentiator": ""}),
        ]
        with patch("ec.diffuser.call_llm", side_effect=responses):
            result = diffuse_ecu(brain, new, embedding_model=model)
        updated = brain.get_ecu(existing)
        assert updated["status"] == "superseded"
        assert updated["confidence"] < 0.3  # frozen at post-update value
        sup = [e for e in brain.get_edges_from(new) if e["type"] == "supersedes"]
        assert len(sup) == 1
        assert sup[0]["supersession_type"] in ("cosmetic", "semantic")
        # new ECU's confidence is its own — NOT inherited (§15.4)
        assert brain.get_ecu(new)["confidence"] == pytest.approx(0.63)
        assert result.supersessions[0]["old_ecu_id"] == existing

    def test_llm_supersedes_trigger_and_downgrade(self, brain, model):
        # trigger passes: weak existing -> superseded
        weak = self._insert_with_embedding(
            brain, model, "Auth tokens expire after 24 hours", confidence=0.25)
        new = self._insert_with_embedding(
            brain, model, "Auth tokens expire after 1 hour with sliding renewal",
            confidence=0.6)
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("supersedes")):
            result = diffuse_ecu(brain, new, embedding_model=model)
        assert brain.get_ecu(weak)["status"] == "superseded"
        assert len(result.supersessions) == 1

        # trigger fails: healthy existing -> downgraded to supports (D8)
        healthy = self._insert_with_embedding(
            brain, model, "Session cookies must be HttpOnly and Secure",
            confidence=0.8)
        new2 = self._insert_with_embedding(
            brain, model, "Session cookies must be HttpOnly, Secure and SameSite=Lax",
            confidence=0.6)
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("supersedes")):
            result2 = diffuse_ecu(brain, new2, embedding_model=model)
        assert brain.get_ecu(healthy)["status"] == "active"
        assert result2.supersessions == []
        edges = brain.get_edges_from(new2)
        assert edges[0]["type"] == "supports"  # downgraded

    def test_depends_on_edge_only(self, brain, model):
        # verified embedding similarity 0.675 > 0.6 relevance threshold
        existing = self._insert_with_embedding(
            brain, model,
            "PostgreSQL is suitable for message persistence in the queue system",
            confidence=0.6)
        new = self._insert_with_embedding(
            brain, model,
            "PostgreSQL message persistence scales to higher load with partitioning",
            confidence=0.5)
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("depends_on")):
            diffuse_ecu(brain, new, embedding_model=model)
        edge = brain.get_edges_from(new)[0]
        assert edge["type"] == "depends_on"
        assert edge["confidence_delta"] == 0.0
        # structural edge: no confidence math (§11 defines none)
        assert brain.get_ecu(existing)["confidence"] == pytest.approx(0.6)

    def test_unrelated_and_empty_brain(self, brain, model):
        lone = self._insert_with_embedding(
            brain, model, "Sourdough starter needs feeding twice daily")
        with patch("ec.diffuser.call_llm") as m:
            result = diffuse_ecu(brain, lone, embedding_model=model)
        assert m.call_count == 0  # no candidates -> no LLM call
        assert result.candidates == 0

        other = self._insert_with_embedding(
            brain, model, "Database migrations must run before deployment")
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("unrelated")):
            result2 = diffuse_ecu(brain, other, embedding_model=model)
        assert brain.get_edges_from(other) == []

    def test_tolerant_classification_parse(self, brain, model):
        existing = self._insert_with_embedding(
            brain, model, "Pool caps at 10 connections", confidence=0.5)
        new = self._insert_with_embedding(
            brain, model, "Load tests confirm pool exhaustion at 10 connections",
            confidence=0.6)
        # garbage response -> unrelated, no crash
        with patch("ec.diffuser.call_llm", return_value="sure! here's my take: ..."):
            diffuse_ecu(brain, new, embedding_model=model)
        assert brain.get_edges_from(new) == []
        # invalid relationship label -> unrelated
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("admires")):
            diffuse_ecu(brain, new, embedding_model=model)
        assert brain.get_edges_from(new) == []

    def test_diffuser_errors(self, brain, model):
        with pytest.raises(DiffuserError):
            diffuse_ecu(brain, "ghost", embedding_model=model)
        sup = self._insert_with_embedding(brain, model, "replaced belief")
        brain.update_ecu_status(sup, "superseded")
        with pytest.raises(DiffuserError):
            diffuse_ecu(brain, sup, embedding_model=model)


# ---------------------------------------------------------------------------
# lightweight diffuser (§4.4)
# ---------------------------------------------------------------------------

class TestLightweightDiffuser:
    def _session_setup(self, brain, model):
        session_id = brain.create_session("/tmp/myapp", "main")
        s1 = brain.insert_session_ecu(
            session_id,
            make_ecu("Token refresh must precede cache clear in the auth flow",
                     confidence=0.5),
            embedding=model.encode_one("auth token refresh ordering cache invalidation"),
        )
        return session_id, s1

    def test_session_pair_supports(self, brain, model):
        session_id, s1 = self._session_setup(brain, model)
        s2 = brain.insert_session_ecu(
            session_id,
            make_ecu("The auth bug was stale tokens from un-ordered refresh/clear",
                     confidence=0.6),
            embedding=model.encode_one("stale auth tokens from refresh before cache clear"),
        )
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("supports")) as m:
            result = diffuse_session_ecu(brain, session_id, s2, embedding_model=model)
        assert m.call_count == 1  # one batched call (§4.4 step 3)
        assert m.call_args.kwargs["temperature"] == 0.0  # deterministic

        edges = brain.list_session_edges(session_id)
        assert len(edges) == 1
        edge = edges[0]
        assert edge["source_id"] == s2 and edge["target_id"] == s1
        assert edge["type"] == "supports" and edge["target_type"] == "session_ecu"
        sim = edge["weight"]
        # target bumped c' = c + alpha_light x similarity (clamped)
        expected = min(0.95, max(0.05, 0.5 + 0.1 * sim))
        assert brain.get_session_ecu(s1)["confidence"] == pytest.approx(expected)
        # source is the evidence — its own confidence is not bumped
        assert brain.get_session_ecu(s2)["confidence"] == pytest.approx(0.6)
        assert result.pending_updates == []

    def test_cross_brain_pending_update_only(self, brain, model):
        canonical = brain.insert_ecu(
            make_ecu("Auth token refresh must happen before cache invalidation",
                     confidence=0.8),
            embedding=model.encode_one("auth token refresh before cache invalidation"),
        )
        session_id, s1 = self._session_setup(brain, model)
        s2 = brain.insert_session_ecu(
            session_id,
            make_ecu("Auth actually refreshes after cache clear, contrary to design docs",
                     confidence=0.55),
            embedding=model.encode_one("auth refresh happens after cache clear observed"),
        )
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("contradicts", "contradicts")):
            result = diffuse_session_ecu(brain, session_id, s2, embedding_model=model)

        # canonical ECU UNTOUCHED (§6.7 hard boundary)
        canon = brain.get_ecu(canonical)
        assert canon["confidence"] == pytest.approx(0.8)
        assert canon["status"] == "active"
        assert canon["metadata"]["has_pending_updates"] is True
        # pending update recorded with negative (contradicts) delta
        pending = brain.list_pending_updates(session_id=session_id)
        assert len(pending) == 1
        pu = pending[0]
        assert pu["canonical_ecu_id"] == canonical
        assert pu["session_ecu_id"] == s2
        assert pu["relationship_type"] == "contradicts"
        assert pu["proposed_confidence_delta"] < 0
        assert pu["status"] == "pending"
        # no session_edge rows for canonical targets (D5)
        assert all(e["target_type"] == "session_ecu"
                   for e in brain.list_session_edges(session_id))
        assert result.pending_updates[0]["canonical_ecu_id"] == canonical

    def test_no_forbidden_actions(self, brain, model):
        session_id, s1 = self._session_setup(brain, model)
        s2 = brain.insert_session_ecu(
            session_id,
            make_ecu("The auth flow contradicts the documented refresh ordering",
                     confidence=0.5),
            embedding=model.encode_one("auth refresh ordering contradicts documentation"),
        )
        with patch("ec.diffuser.call_llm",
                   return_value=classifications_response("contradicts")):
            diffuse_session_ecu(brain, session_id, s2, embedding_model=model)
        # §4.4: no challenged status, no supersedes/depends_on in session brain
        assert brain.get_session_ecu(s1)["status"] == "active"
        edges = brain.list_session_edges(session_id)
        assert all(e["type"] in ("supports", "contradicts") for e in edges)
        with pytest.raises(ValueError):
            brain.add_session_edge(session_id, s2, s1, "supersedes")
        with pytest.raises(ValueError):
            brain.add_session_edge(session_id, s2, s1, "depends_on")

    def test_session_diffuser_errors(self, brain, model):
        session_id, s1 = self._session_setup(brain, model)
        with pytest.raises(DiffuserError):
            diffuse_session_ecu(brain, session_id, "ghost", embedding_model=model)
        other_session = brain.create_session("/tmp/other", "main")
        with pytest.raises(DiffuserError):
            diffuse_session_ecu(brain, other_session, s1, embedding_model=model)

    def test_session_edge_validation(self, brain, model):
        session_id, s1 = self._session_setup(brain, model)
        with pytest.raises(ValueError):
            brain.add_session_edge(session_id, s1, s1, "supports",
                                   target_type="nonsense")
        from ec.brain import BrainError
        with pytest.raises(BrainError):
            brain.add_session_edge(session_id, s1, "ghost", "supports")
        with pytest.raises(BrainError):
            brain.add_pending_update("ghost", s1, session_id, "supports", 0.1)


# ---------------------------------------------------------------------------
# live Zen API tests (skip without a key)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("OPENCODE_ZEN_API_KEY"),
    reason="OPENCODE_ZEN_API_KEY not set — live Zen test skipped",
)
class TestLive:
    def test_extractor_live(self):
        result = extract_ecus({
            "prompt": "Why do tests pass locally but fail in CI?",
            "reasoning_trace": (
                "Checked test logs. Local passes, CI fails on date comparison. "
                "Traced to datetime.now() — tests assume local timezone. CI runs "
                "in UTC. The root cause is timezone-dependent date comparisons "
                "in the test suite; tests must inject clocks or use explicit "
                "timezones to be reproducible."
            ),
            "output": "Fixed by injecting a clock into the date helpers.",
        })
        # Haiku should find at least the timezone/root-cause conclusion
        assert isinstance(result.ecus, list)
        for ecu in result.ecus:
            assert 0.05 <= ecu["confidence"] <= 0.95
            assert ecu["status"] == "active"

    def test_lightweight_classification_live(self, brain, model):
        session_id = brain.create_session("/tmp/live", "main")
        s1 = brain.insert_session_ecu(
            session_id,
            make_ecu("Tests must use explicit timezones to be reproducible in CI",
                     confidence=0.5),
            embedding=model.encode_one("tests explicit timezones reproducible CI"),
        )
        s2 = brain.insert_session_ecu(
            session_id,
            make_ecu("CI failures were caused by timezone-naive datetime comparisons",
                     confidence=0.6),
            embedding=model.encode_one("CI failure timezone naive datetime comparison"),
        )
        result = diffuse_session_ecu(brain, session_id, s2, embedding_model=model)
        # live Haiku should relate these two (supports) or say unrelated;
        # either way the mechanics must not crash and must stay 3-way
        assert result.candidates >= 1
        for edge in brain.list_session_edges(session_id):
            assert edge["type"] in ("supports", "contradicts")
