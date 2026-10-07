"""Phase 1 foundation tests — brain, config, embeddings, mode detection.

Run: .venv/bin/pytest tests/ -v
No network access is required (the embedding model loads from the local HF
cache). The one live LLM test skips unless OPENCODE_ZEN_API_KEY is set.
"""

import os

# The embedding model is cached locally; never hit the network in tests.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np
import pytest

from ec.brain import Brain, BrainError
from ec.config import get_base_prior, load_config
from ec.embeddings import from_blob, get_embedding_model, to_blob
from ec.llm import extract_json
from ec.mode_detection import MODES, detect_mode, get_mode_params


def make_ecu(cognition: str, **overrides) -> dict:
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "engineering > repo:fastapi > module:routing"},
        "provenance": {
            "source_type": "debugging",
            "source_id": "session-test",
            "origin_agent": "claude-haiku-4-5",
            "origin_engineer": None,
        },
        "grounding": {
            "repo_path": "/tmp/fastapi",
            "files": ["fastapi/routing.py"],
            "symbols": ["APIRouter.add_route"],
            "commit_hash": "abc123",
        },
        "confidence": 0.72,
        "status": "active",
        "evidence_pointers": ["session-test:msg-4"],
    }
    ecu.update(overrides)
    return ecu


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture(scope="session")
def model():
    return get_embedding_model()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_config_defaults(self):
        cfg = load_config(path="/nonexistent/config.yaml")  # pure defaults
        # ranking (§11.5 / §15)
        assert cfg.ranking.w_relevance == 0.6
        assert cfg.ranking.w_confidence == 0.15
        assert cfg.ranking.w_activation == 0.10
        assert cfg.ranking.w_network == 0.15
        assert cfg.ranking.relevance_gate_threshold == 0.3
        assert cfg.ranking.edge_count_cap == 10
        # confidence priors (§5.9)
        priors = cfg.confidence.base_priors
        assert priors.debugging == 0.70
        assert priors.implementation == 0.65
        assert priors.code_review == 0.60
        assert priors.architectural_reasoning == 0.50
        assert priors.observation == 0.45
        assert priors.planning == 0.35
        assert cfg.confidence.prior_cap == 0.95
        assert cfg.confidence.theta_supersede == 0.3
        assert cfg.confidence.w_support == 1.0
        assert cfg.confidence.w_contradict == 1.0
        # activation (§9)
        assert cfg.activation.base_boost == 0.5
        assert cfg.activation.decay_factor == 0.5
        assert cfg.activation.normalization == "divide_by_max"
        # llm
        assert cfg.llm.model == "claude-haiku-4-5"
        assert cfg.llm.temperature == 0.3
        assert cfg.llm.temperature_mode_detection == 0.0
        assert cfg.llm.api_key_env == "OPENCODE_ZEN_API_KEY"
        # embedding
        assert cfg.embedding.model == "all-MiniLM-L6-v2"
        assert cfg.embedding.dimensions == 384

    def test_mode_params_match_spec(self):
        """Critical detail #7 — the five mode parameter blocks."""
        cfg = load_config(path="/nonexistent/config.yaml")
        assert cfg.modes.debugging.depth == 0
        assert cfg.modes.debugging.max_tokens == 2000
        assert cfg.modes.debugging.prioritize == ["contradictions"]
        assert cfg.modes.debugging.bias == "debugging"

        assert cfg.modes.architecture.depth == 1
        assert cfg.modes.architecture.max_tokens == 6000
        assert cfg.modes.architecture.prioritize == ["decision", "pattern", "constraint"]
        assert cfg.modes.architecture.bias == "architectural_reasoning"

        assert cfg.modes.implementation.depth == 1
        assert cfg.modes.implementation.max_tokens == 3000
        assert cfg.modes.implementation.prioritize == ["pattern", "constraint"]
        assert cfg.modes.implementation.bias == "implementation"

        assert cfg.modes.investigation.depth == 1
        assert cfg.modes.investigation.max_tokens == 4000
        assert cfg.modes.investigation.prioritize == ["pattern", "decision"]
        assert cfg.modes.investigation.bias == "none"

        assert cfg.modes.planning.depth == 1
        assert cfg.modes.planning.max_tokens == 5000
        assert cfg.modes.planning.prioritize == ["constraint", "decision"]
        assert cfg.modes.planning.bias == "planning"

    def test_config_yaml_override(self, tmp_path):
        override = tmp_path / "config.yaml"
        override.write_text("ranking:\n  w_relevance: 0.9\nllm:\n  timeout: 5\n")
        cfg = load_config(path=override)
        assert cfg.ranking.w_relevance == 0.9          # overridden
        assert cfg.ranking.w_confidence == 0.15      # untouched default kept
        assert cfg.llm.timeout == 5                  # overridden
        assert cfg.llm.model == "claude-haiku-4-5"   # untouched default kept

    def test_source_prior_aliases(self):
        # extractor emits "review"/"session"; §15 names "code_review"
        assert get_base_prior("review") == 0.60
        assert get_base_prior("session") == 0.45
        assert get_base_prior("debugging") == 0.70
        with pytest.raises(ValueError):
            get_base_prior("nonsense")


# ---------------------------------------------------------------------------
# brain
# ---------------------------------------------------------------------------

class TestBrain:
    def test_brain_auto_create(self, tmp_path):
        b = Brain(tmp_path / "sub" / "dir" / "ec.db")  # nested dirs auto-created
        try:
            assert b.journal_mode == "wal"
            expected = {
                "ecus", "edges", "sessions", "session_ecus",
                "session_edges", "pending_updates", "maintenance_log",
            }
            assert expected <= set(b.table_names())
        finally:
            b.close()

    def test_insert_and_get_ecu(self, brain, model):
        vec = model.encode_one("auth token refresh ordering invariant")
        blob = to_blob(vec)
        ecu_id = brain.insert_ecu(make_ecu(
            "Cache invalidation must follow token refresh; violating this "
            "ordering causes stale authentication tokens"
        ), embedding=blob)

        got = brain.get_ecu(ecu_id)
        assert got is not None
        assert got["id"] == ecu_id
        assert "Cache invalidation must follow token refresh" in got["cognition"]
        assert got["conclusion_type"] == "invariant"
        assert got["scope"]["level"] == "repo"
        assert got["scope"]["path"].endswith("module:routing")
        assert got["provenance"]["source_type"] == "debugging"
        assert got["provenance"]["origin_agent"] == "claude-haiku-4-5"
        assert got["provenance"]["created_at"]  # auto-populated ISO 8601
        assert got["grounding"]["files"] == ["fastapi/routing.py"]
        assert got["confidence"] == pytest.approx(0.72)
        assert got["status"] == "active"
        assert got["evidence_pointers"] == ["session-test:msg-4"]
        assert got["metadata"]["retrieval_count"] == 0
        assert got["embedding"] == blob  # byte-identical roundtrip
        assert brain.ecu_exists(ecu_id)
        assert brain.count_ecus() == 1
        assert brain.get_ecu("does-not-exist") is None

    def test_ecu_validation(self, brain):
        with pytest.raises(ValueError):
            brain.insert_ecu(make_ecu("x", conclusion_type="hunch"))
        with pytest.raises(ValueError):
            brain.insert_ecu(make_ecu("x", scope={"level": "galaxy", "path": "galaxy"}))
        with pytest.raises(ValueError):
            brain.insert_ecu(make_ecu("x", confidence=1.5))
        with pytest.raises(ValueError):
            brain.insert_ecu(make_ecu("   "))
        assert brain.count_ecus() == 0  # nothing leaked in

    def test_confidence_and_status_updates(self, brain):
        ecu_id = brain.insert_ecu(make_ecu("pool hard-caps at 10 connections"))
        brain.update_ecu_confidence(ecu_id, 0.31)
        assert brain.get_ecu(ecu_id)["confidence"] == pytest.approx(0.31)
        brain.update_ecu_status(ecu_id, "superseded")
        assert brain.get_ecu(ecu_id)["status"] == "superseded"
        brain.update_ecu_metadata(ecu_id, retrieval_count=3)
        assert brain.get_ecu(ecu_id)["metadata"]["retrieval_count"] == 3
        with pytest.raises(ValueError):
            brain.update_ecu_confidence(ecu_id, 2.0)
        with pytest.raises(ValueError):
            brain.update_ecu_status(ecu_id, "bogus")
        with pytest.raises(BrainError):
            brain.update_ecu_status("missing-id", "active")
        # cognition has no mutator — §13.2 immutability is structural
        assert not hasattr(brain, "update_ecu_cognition")

    def test_edges(self, brain):
        a = brain.insert_ecu(make_ecu("A supports B in every context"))
        b = brain.insert_ecu(make_ecu("B is strengthened by A's evidence"))
        c = brain.insert_ecu(make_ecu("C is an old belief replaced by A"))

        edge_id = brain.add_edge(a, b, "supports", weight=0.8, confidence_delta=0.2)
        # §14.3: same (source, target, type) accumulates instead of duplicating
        again = brain.add_edge(a, b, "supports", weight=0.5, confidence_delta=0.1)
        assert again == edge_id
        edges_ab = brain.get_edges_from(a)
        assert len(edges_ab) == 1
        assert edges_ab[0]["weight"] == pytest.approx(1.0)      # 0.8 + 0.5, capped
        assert edges_ab[0]["confidence_delta"] == pytest.approx(0.3)

        brain.add_edge(a, c, "supersedes", supersession_type="cosmetic")
        sup = [e for e in brain.get_edges_to(c) if e["type"] == "supersedes"]
        assert sup[0]["supersession_type"] == "cosmetic"
        # supersession_type forced to None on non-supersedes edges
        assert edges_ab[0]["supersession_type"] is None

        assert brain.edge_count(a) == 2
        assert brain.edge_count(b) == 1
        assert {e["id"] for e in brain.get_edges_for(b)} == {edge_id}
        with pytest.raises(ValueError):
            brain.add_edge(a, b, "admires")
        with pytest.raises(BrainError):
            brain.add_edge(a, "ghost-ecu", "supports")

        brain.delete_edge(edge_id)
        remaining = brain.get_edges_from(a)
        assert all(e["type"] != "supports" for e in remaining)
        assert len(remaining) == 1  # only the supersedes edge a->c remains


# ---------------------------------------------------------------------------
# embeddings
# ---------------------------------------------------------------------------

class TestEmbeddings:
    def test_encode_and_blob_roundtrip(self, model):
        vec = model.encode_one("Authentication depends on optimistic token refresh")
        assert vec.shape == (384,)
        assert vec.dtype == np.float32
        assert np.linalg.norm(vec) == pytest.approx(1.0, abs=1e-5)  # normalized

        blob = to_blob(vec)
        assert len(blob) == 384 * 4
        assert np.array_equal(from_blob(blob), vec)

        batch = model.encode(["one text", "another text"])
        assert batch.shape == (2, 384)


# ---------------------------------------------------------------------------
# similarity search across both brains
# ---------------------------------------------------------------------------

class TestFindSimilar:
    def test_find_similar_ranks_and_filters(self, brain, model):
        auth1 = brain.insert_ecu(make_ecu(
            "Authentication correctness depends on optimistic token refresh; "
            "cache invalidation must follow refresh or stale tokens persist"
        ), embedding=model.encode_one("auth token refresh ordering and cache invalidation"))
        auth2 = brain.insert_ecu(make_ecu(
            "JWT validation must happen before route handler execution"
        ), embedding=model.encode_one("JWT validation ordering in auth middleware"))
        cooking = brain.insert_ecu(make_ecu(
            "Sourdough starter must be fed twice daily before baking"
        ), embedding=model.encode_one("sourdough bread baking starter feeding schedule"))

        session_id = brain.create_session("/tmp/fastapi", "main")
        sess_ecu = brain.insert_session_ecu(session_id, make_ecu(
            "Session finding: the auth middleware swallows refresh errors"
        ), embedding=model.encode_one("auth middleware silently ignores token refresh failures"))

        q = model.encode_one("how does authentication token refresh work")
        results = brain.find_similar(q, threshold=0.3)
        ids = [r["id"] for r in results]

        # both auth ECUs and the session ECU surface; cooking is gated out
        assert auth1 in ids and auth2 in ids and sess_ecu in ids
        assert cooking not in ids
        assert results[0]["id"] != cooking

        by_id = {r["id"]: r for r in results}
        assert by_id[auth1]["brain"] == "canonical"
        assert by_id[sess_ecu]["brain"] == "session"
        assert all(0.3 <= r["similarity"] <= 1.0 for r in results)

        # top_k bounds the result list
        assert len(brain.find_similar(q, threshold=0.0, top_k=2)) == 2

        # canonical-only search excludes the session ECU
        no_session = brain.find_similar(q, threshold=0.0, include_session=False)
        assert sess_ecu not in [r["id"] for r in no_session]

        # superseded canonical ECUs are excluded by the default status filter
        brain.update_ecu_status(auth2, "superseded")
        assert auth2 not in [r["id"] for r in brain.find_similar(q, threshold=0.0)]


# ---------------------------------------------------------------------------
# mode detection
# ---------------------------------------------------------------------------

class TestModeDetection:
    def test_keyword_fallback(self):
        # use_llm=False — pure offline path
        assert detect_mode(
            "why is the login endpoint throwing 500 errors", use_llm=False
        ) == "debugging"
        assert detect_mode(
            "the tests fail intermittently in CI", use_llm=False
        ) == "debugging"
        assert detect_mode(
            "refactor the auth module to reduce coupling", use_llm=False
        ) == "architecture"
        assert detect_mode(
            "add a pagination endpoint", use_llm=False
        ) == "implementation"
        assert detect_mode(
            "how is the routing layer structured", use_llm=False
        ) in ("investigation", "architecture")
        assert detect_mode(
            "plan the migration to postgres", use_llm=False
        ) == "planning"
        # no signal -> broad default
        assert detect_mode("xqzv blorp", use_llm=False) == "investigation"

    def test_no_api_key_falls_back_to_keywords(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
        assert detect_mode("fix the crash in the scheduler") == "debugging"

    def test_mode_params_lookup(self):
        params = get_mode_params("debugging")
        assert params.max_tokens == 2000 and params.depth == 0
        with pytest.raises(ValueError):
            get_mode_params("daydreaming")

    @pytest.mark.skipif(
        not os.environ.get("OPENCODE_ZEN_API_KEY"),
        reason="OPENCODE_ZEN_API_KEY not set — live Zen test skipped",
    )
    def test_mode_detection_llm_live(self):
        mode = detect_mode("why does the server crash on startup under load")
        assert mode in MODES


# ---------------------------------------------------------------------------
# JSON fence stripping (§15 JSON post-processing)
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_fenced_json(self):
        text = '```json\n{"a": 1, "b": [2, 3]}\n```'
        assert extract_json(text) == '{"a": 1, "b": [2, 3]}'

    def test_prose_wrapped(self):
        text = 'Here is the result:\n```json\n{"ecus": []}\n```\nDone.'
        assert extract_json(text) == '{"ecus": []}'

    def test_no_braces_passthrough(self):
        assert extract_json("  no json here  ") == "no json here"
