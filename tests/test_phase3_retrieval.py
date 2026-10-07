"""Phase 3 tests — demand-driven retrieval + spreading activation.

Run: .venv/bin/python -m pytest tests/ -v
Offline: real embeddings from the HF cache (HF_HUB_OFFLINE=1); no LLM calls
(explicit mode= or the keyword fallback everywhere). One live test skips
unless OPENCODE_ZEN_API_KEY is set.

The canonical fixture ports the verified Experiment-2 reference brain
(~/Downloads/test_retrieval_ranking_zen_v2.py): 9 ECUs + 23 edges whose
similarity structure and expected retrieval outcomes were validated against
the real pipeline.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import math
from datetime import datetime, timedelta, timezone

import pytest

from ec import retrieval
from ec.activation import ActivationState
from ec.brain import Brain
from ec.config import get_config, load_config
from ec.llm import api_key

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


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


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


def insert(brain, model, ecu, ecu_id):
    ecu["id"] = ecu_id
    return brain.insert_ecu(ecu, embedding=model.encode_one(ecu["cognition"]))


# ---------------------------------------------------------------------------
# Reference brain (Experiment 2 v2 — verified similarity structure)
# ---------------------------------------------------------------------------

REF_ECUS = {
    "T1-E1": "Webhook processing must be idempotent; if the charge succeeds but acknowledgment fails, the retry will re-process the webhook and charge the customer again unless the system tracks which webhooks have already been charged",
    "T1-E2": "The payment queue's status model creates a race condition: status is 'processing' during charge and acknowledgment, but only transitions to 'completed' after acknowledgment succeeds; if acknowledgment fails, the event reverts to 'pending' with the charge already applied, causing duplicate charges on retry",
    "T1-E3": "The retry utility does not distinguish between failures that occur before a side effect (charge) is applied versus failures that occur after; this makes it unsuitable for non-idempotent operations without additional state tracking",
    "T1-E4": "For webhook processing, the event status must transition to 'charged' immediately after the charge succeeds and before the acknowledgment is attempted; this ensures retries will not re-charge even if acknowledgment fails",
    "T3-E1": "Chat message persistence requires strong consistency guarantees because messages can contain payment confirmations; eventual consistency systems like MongoDB (default) or Cassandra are unsuitable without significant configuration overhead that negates their throughput advantages",
    "T3-E2": "PostgreSQL is suitable for this chat platform's message persistence because the query patterns (by channel, by user, by time range) require flexible ad-hoc indexing that PostgreSQL provides natively; Cassandra's rigid schema-per-query model would require maintaining separate tables for each query pattern, adding operational complexity",
    "T3-E3": "At the current write throughput of ~23 messages/second (2M/day), PostgreSQL on a single instance is sufficient; write scaling via partitioning is deferred until reaching 10x load (~230 msg/sec), at which point declarative partitioning by channel ID can be applied without schema redesign",
    "T3-E4": "A two-tier caching strategy with Redis as a hot cache for recent messages (last 1 hour) and PostgreSQL as the source of truth reduces read latency for active channels while maintaining durability; this pattern is necessary because PostgreSQL alone cannot match Redis's sub-millisecond read latency for frequently accessed data",
    "T3-E5": "MongoDB's flexible schema advantage is not applicable to this chat platform because the message schema is fixed and well-defined; the flexibility would add operational burden without providing value, making MongoDB a worse choice than PostgreSQL for this use case",
}

REF_META = {
    "T1-E1": ("invariant", "repo", "repo:payments-api > module:webhook", "debugging", 0.85),
    "T1-E2": ("observation", "repo", "repo:payments-api > module:queue", "debugging", 0.80),
    "T1-E3": ("implication", "repo", "repo:payments-api > module:retry", "debugging", 0.80),
    "T1-E4": ("decision", "repo", "repo:payments-api > module:webhook", "debugging", 0.85),
    "T3-E1": ("constraint", "project", "project:chat-platform > subsystem:message-persistence", "architectural_reasoning", 0.85),
    "T3-E2": ("decision", "project", "project:chat-platform > subsystem:message-persistence", "architectural_reasoning", 0.80),
    "T3-E3": ("implication", "project", "project:chat-platform > subsystem:message-persistence", "architectural_reasoning", 0.75),
    "T3-E4": ("pattern", "project", "project:chat-platform > subsystem:message-persistence", "architectural_reasoning", 0.80),
    "T3-E5": ("observation", "project", "project:chat-platform > subsystem:message-persistence", "architectural_reasoning", 0.75),
}

REF_EDGES = [
    ("T1-E1", "T1-E2", "supports"), ("T1-E1", "T1-E3", "supports"),
    ("T1-E1", "T1-E4", "supports"), ("T1-E2", "T1-E1", "supports"),
    ("T1-E2", "T1-E3", "supports"), ("T1-E2", "T1-E4", "supports"),
    ("T1-E3", "T1-E1", "supports"), ("T1-E3", "T1-E2", "supports"),
    ("T1-E3", "T1-E4", "supports"), ("T1-E4", "T1-E1", "supports"),
    ("T1-E4", "T1-E2", "supports"), ("T1-E4", "T1-E3", "supports"),
    ("T3-E2", "T3-E1", "depends_on"), ("T3-E3", "T3-E1", "depends_on"),
    ("T3-E3", "T3-E2", "depends_on"), ("T3-E4", "T3-E1", "depends_on"),
    ("T3-E1", "T3-E2", "supports"), ("T3-E1", "T3-E4", "supports"),
    ("T3-E1", "T3-E5", "supports"), ("T3-E2", "T3-E4", "supports"),
    ("T3-E5", "T3-E2", "supports"), ("T3-E4", "T3-E3", "supports"),
]

REF_QUERIES = {
    "Q1": ("How does the webhook payment processing work? Are there any known "
           "issues with idempotency or retry logic?", "debugging",
           {"T1-E1", "T1-E2", "T1-E3", "T1-E4"}),
    "Q2": ("We need to choose a database for our new chat platform's message "
           "persistence. What are the existing architectural decisions and "
           "constraints?", "architecture",
           {"T3-E1", "T3-E2", "T3-E3", "T3-E4", "T3-E5"}),
}


@pytest.fixture()
def ref_brain(brain, model):
    """The verified Experiment-2 brain: 9 canonical ECUs + reference edges."""
    for ecu_id, cognition in REF_ECUS.items():
        ctype, level, path, source, conf = REF_META[ecu_id]
        insert(brain, model, make_ecu(
            cognition,
            conclusion_type=ctype,
            scope={"level": level, "path": path},
            provenance={"source_type": source},
            grounding={"files": ["src/services/x.ts"], "symbols": []},
            confidence=conf,
        ), ecu_id)
    for src, tgt, etype in REF_EDGES:
        brain.add_edge(src, tgt, etype, weight=0.9)
    return brain


def core_ids(result) -> set:
    return {g["core_ecu"]["id"] for g in result["groups"]}


# ---------------------------------------------------------------------------
# Spreading activation (§12)
# ---------------------------------------------------------------------------

class TestSpreadingActivation:
    def test_spread_formula_and_max_hops(self, brain, model):
        # chain A—B—C—D (+ E—B converging on B from another branch)
        ids = {}
        for name in ("A", "B", "C", "D"):
            ids[name] = insert(brain, model, make_ecu(f"chain node {name}"), name)
        brain.add_edge("A", "B", "supports")
        brain.add_edge("B", "C", "supports")
        brain.add_edge("C", "D", "supports")

        state = ActivationState("sess-1")
        state.spread(brain, ["A"], now=T0)

        assert state.get("A") == pytest.approx(0.5)     # base_boost
        assert state.get("B") == pytest.approx(0.25)    # hop 1: 0.5 × 0.5
        assert state.get("C") == pytest.approx(0.125)   # hop 2: 0.5 × 0.5²
        assert state.get("D") == 0.0                    # hop 3: beyond max_hops

    def test_spread_max_merge_and_seed_accumulation(self, brain, model):
        for name in ("A", "B", "C"):
            insert(brain, model, make_ecu(f"merge node {name}"), name)
        brain.add_edge("A", "B", "supports")
        brain.add_edge("C", "B", "supports")
        brain.add_edge("A", "C", "supports")

        state = ActivationState("sess-1")
        state.spread(brain, ["A", "C"], now=T0)
        # B is hop-1 from BOTH seeds — one max-merged boost, not two
        assert state.get("B") == pytest.approx(0.25)
        # repeated retrieval of A accumulates the full base boost
        state.spread(brain, ["A"], now=T0)
        assert state.get("A") == pytest.approx(1.0)
        # existing score never decreases via spread (max with existing)
        assert state.get("B") == pytest.approx(0.25)

    def test_normalization_divide_by_max(self, brain, model):
        for name in ("A", "B"):
            insert(brain, model, make_ecu(f"norm node {name}"), name)
        brain.add_edge("A", "B", "supports")
        state = ActivationState("sess-1")
        state.spread(brain, ["A"], now=T0)
        norm = state.normalized_scores()
        assert norm["A"] == pytest.approx(1.0)   # divide by max (0.5)
        assert norm["B"] == pytest.approx(0.5)
        # cold ECU and fresh state both read 0.0 (§12.5 all-zero skip)
        assert state.normalized("missing") == 0.0
        assert ActivationState("sess-2").normalized("A") == 0.0

    def test_time_decay(self, brain, model):
        insert(brain, model, make_ecu("decaying node A"), "A")
        state = ActivationState("sess-1")
        state.spread(brain, ["A"], now=T0)
        state.apply_time_decay(now=T0 + timedelta(hours=1))
        assert state.get("A") == pytest.approx(0.5 * math.exp(-0.1))
        # decay is idempotent over the same instant (timestamps restamped)
        state.apply_time_decay(now=T0 + timedelta(hours=1))
        assert state.get("A") == pytest.approx(0.5 * math.exp(-0.1))

    def test_persist_and_resume(self, brain, model):
        insert(brain, model, make_ecu("resumable node A"), "A")
        brain.create_session("/repo", "main", session_id="sess-1")
        state = ActivationState("sess-1")
        state.spread(brain, ["A"], now=T0)
        state.save(brain)

        # §6.6: resume after a 15-hour break — significant decay
        resumed = ActivationState.load(brain, "sess-1", now=T0 + timedelta(hours=15))
        assert resumed.get("A") == pytest.approx(0.5 * math.exp(-1.5))
        # a 5-minute break is nearly seamless
        quick = ActivationState.load(brain, "sess-1", now=T0 + timedelta(minutes=5))
        assert quick.get("A") == pytest.approx(0.5 * math.exp(-0.1 / 12))

    def test_session_isolation(self, brain, model):
        insert(brain, model, make_ecu("isolated node A"), "A")
        brain.create_session("/repo", "main", session_id="sess-1")
        brain.create_session("/repo", "main", session_id="sess-2")
        warm = ActivationState("sess-1")
        warm.spread(brain, ["A"], now=T0)
        warm.save(brain)

        cold = ActivationState.load(brain, "sess-2", now=T0)
        assert cold.get("A") == 0.0  # §12.2: no cross-session contamination
        assert ActivationState.load(brain, "sess-1", now=T0).get("A") == pytest.approx(0.5)

    def test_mode_aware_spread(self, brain, model):
        # D9/§11.9: debugging spreads more aggressively via contradicts edges
        for name in ("S", "X", "Y"):
            insert(brain, model, make_ecu(f"mode node {name}"), name)
        brain.add_edge("S", "X", "contradicts")
        brain.add_edge("S", "Y", "supports")

        dbg = ActivationState("sess-1")
        dbg.spread(brain, ["S"], mode="debugging", now=T0)
        # contradicts: min(cap 0.5, 0.25 × 1.5) = 0.375 > supports 0.25
        assert dbg.get("X") == pytest.approx(0.375)
        assert dbg.get("Y") == pytest.approx(0.25)

        arch = ActivationState("sess-2")
        arch.spread(brain, ["S"], mode="architecture", now=T0)
        # architecture prefers depends_on — neither edge matches here
        assert arch.get("X") == pytest.approx(0.25)
        assert arch.get("Y") == pytest.approx(0.25)

    def test_cross_brain_spread_via_session_edges(self, brain, model):
        brain.create_session("/repo", "main", session_id="sess-1")
        insert(brain, model, make_ecu("canonical anchor"), "CAN")
        brain.insert_session_ecu("sess-1", make_ecu("session leaf"),
                                 embedding=model.encode_one("session leaf"))
        ses = brain.list_session_ecus("sess-1")[0]["id"]
        brain.add_session_edge("sess-1", ses, "CAN", "supports",
                               weight=0.8, target_type="canonical_ecu")

        state = ActivationState("sess-1")
        state.spread(brain, [ses], now=T0)
        # §11.10: activation crosses brains along session edges
        assert state.get("CAN") == pytest.approx(0.25)

        # another session must not follow sess-1's edges (session scoping)
        brain.create_session("/repo", "feature", session_id="sess-2")
        state2 = ActivationState("sess-2")
        state2.spread(brain, ["CAN"], now=T0)
        assert state2.get(ses) == 0.0


# ---------------------------------------------------------------------------
# Ranking pipeline (§11.5, §11.9, §11.11)
# ---------------------------------------------------------------------------

class TestRanking:
    def test_four_factor_rank_math(self, ref_brain, model):
        cfg = get_config()
        q_emb = model.encode_one(REF_QUERIES["Q2"][0])
        sims = {c["id"]: c["similarity"]
                for c in ref_brain.find_similar(q_emb, threshold=-1.0,
                                                include_session=False)}
        ecu = ref_brain.get_ecu("T3-E1")
        ecu["brain"] = "canonical"
        params = get_config().modes["architecture"]
        scored = retrieval._rank_candidates(
            ref_brain, [{"ecu": ecu, "similarity": sims["T3-E1"], "brain": "canonical"}],
            activation=None, mode_params=params, session_id=None, config=cfg,
        )
        b = scored[0]["breakdown"]
        edges = ref_brain.edge_count("T3-E1")
        expected_richness = min(edges, 10) / 10
        expected = (0.6 * sims["T3-E1"] + 0.15 * 0.85 + 0.10 * 0.0
                    + 0.15 * expected_richness) * 1.0 * 0.55  # project proximity
        expected += 0.05 + 0.03  # architecture prioritize (constraint) + bias
        assert b["network_richness"] == pytest.approx(expected_richness)
        assert b["rank"] == pytest.approx(expected)
        assert scored[0]["rank"] == pytest.approx(expected)

    def test_relevance_gate_filters(self, ref_brain, model):
        result = retrieval.retrieve(ref_brain, REF_QUERIES["Q1"][0],
                                    mode="debugging", embedding_model=model)
        assert not result["fallback"]
        retrieved = core_ids(result)
        # 4 of 5 T3 ECUs sit below the 0.3 gate for this query (verified sims)
        assert result["filtered_out"] == 4
        assert not retrieved & {"T3-E2", "T3-E3", "T3-E4", "T3-E5"}
        # T3-E1 (sim 0.351, mentions "payment confirmations") passes the gate
        # by design — §11.5: permissive enough for tangentially-relevant ECUs —
        # but the 0.6 relevance weight ranks it below every on-topic ECU.
        if "T3-E1" in retrieved:
            ranks = [g["core_ecu"]["id"] for g in result["groups"]]
            assert ranks.index("T3-E1") > max(
                ranks.index(t) for t in ("T1-E1", "T1-E2", "T1-E3", "T1-E4")
            )

    def test_fallback_top_k(self, brain, model):
        # brain contains only off-topic (cooking) ECUs; query is engineering
        for i, text in enumerate((
            "Risotto needs constant stirring and warm stock added gradually",
            "Sourdough starter should be fed twice daily at room temperature",
            "Blanching vegetables preserves color before freezing",
        )):
            insert(brain, model, make_ecu(text), f"COOK-{i}")
        result = retrieval.retrieve(
            brain, "why is the auth token refresh failing with a 500",
            mode="debugging", embedding_model=model)
        assert result["fallback"] is True
        assert result["groups_retrieved"] >= 1
        assert result["groups_retrieved"] <= get_config().retrieval.fallback_top_k
        assert any("fallback" in w for w in result["warnings"])
        assert "[Fallback:" in result["formatted"]

    def test_trust_weight_session_vs_canonical(self, brain, model):
        brain.create_session("/repo", "main", session_id="sess-1")
        text = "The auth module always rotates refresh tokens on every use"
        insert(brain, model, make_ecu(text), "CAN")
        brain.insert_session_ecu("sess-1", make_ecu(text),
                                 embedding=model.encode_one(text))
        ses_id = brain.list_session_ecus("sess-1")[0]["id"]

        cfg = get_config()
        cands = [
            {"ecu": brain.get_ecu("CAN"), "similarity": 0.9, "brain": "canonical"},
            {"ecu": brain.get_session_ecu(ses_id), "similarity": 0.9, "brain": "session"},
        ]
        scored = retrieval._rank_candidates(
            brain, cands, activation=None,
            mode_params=cfg.modes["investigation"], session_id="sess-1", config=cfg)
        ranks = {s["brain"]: s["rank"] for s in scored}
        assert ranks["canonical"] > ranks["session"]
        assert ranks["session"] == pytest.approx(ranks["canonical"] * 0.8)

    def test_scope_proximity_multiplier(self, brain, model):
        text = "Always validate input at trust boundaries before processing"
        insert(brain, model, make_ecu(
            text, scope={"level": "engineering", "path": "engineering"}), "ENG")
        insert(brain, model, make_ecu(
            text, scope={"level": "repo", "path": "repo:myapp"}), "REPO")

        result = retrieval.retrieve(
            brain, "should I validate input at the service boundary",
            mode="investigation", embedding_model=model)
        ranks = {g["core_ecu"]["id"]: g["rank"] for g in result["groups"]}
        assert ranks["REPO"] > ranks["ENG"]  # 0.7 vs 0.1 multiplier
        # §11.11: multiplier, never a filter — both still retrieved
        assert {"ENG", "REPO"} <= core_ids(result)

    def test_open_question_downweight(self, brain, model):
        text = "The queue may drop messages under memory pressure — unconfirmed"
        insert(brain, model, make_ecu(text), "ACT")
        insert(brain, model, make_ecu(text, status="open_question"), "OQ")
        cfg = get_config()
        cands = []
        for cid in ("ACT", "OQ"):
            cands.append({"ecu": {**brain.get_ecu(cid), "brain": "canonical"},
                          "similarity": 0.9, "brain": "canonical"})
        scored = {s["ecu"]["id"]: s for s in retrieval._rank_candidates(
            brain, cands, activation=None, mode_params=cfg.modes["investigation"],
            session_id=None, config=cfg)}
        assert scored["OQ"]["rank"] == pytest.approx(scored["ACT"]["rank"] * 0.5)

        result = retrieval.retrieve(
            brain, "does the queue drop messages under memory pressure",
            mode="investigation", embedding_model=model)
        assert "⚠️ Open question — unresolved since" in result["formatted"]

    def test_challenged_flag_and_no_penalty(self, brain, model):
        a_text = "Token refresh is optimistic — the client retries on 401"
        b_text = "Token refresh must be pessimistic — refresh before expiry always"
        insert(brain, model, make_ecu(a_text, confidence=0.6), "A")
        insert(brain, model, make_ecu(b_text, confidence=0.6), "B")
        brain.add_edge("B", "A", "contradicts", weight=0.9)

        active = retrieval.retrieve(brain, "how does token refresh work",
                                    mode="architecture", embedding_model=model)
        rank_active = {g["core_ecu"]["id"]: g["rank"]
                       for g in active["groups"]}["A"]

        brain.update_ecu_status("A", "challenged")
        brain.update_ecu_metadata("A", last_challenged="2026-08-12T00:00:00+00:00")
        challenged = retrieval.retrieve(brain, "how does token refresh work",
                                        mode="architecture", embedding_model=model)
        groups = {g["core_ecu"]["id"]: g for g in challenged["groups"]}
        # §11.5: challenged is flagged, NOT deprioritised
        assert groups["A"]["rank"] == pytest.approx(rank_active)
        assert groups["A"]["core_ecu"]["status"] == "challenged"
        assert "⚠️ This cognition is challenged" in challenged["formatted"]
        assert f"See: [B]" in challenged["formatted"]
        assert any("1 ECU is challenged" == w for w in challenged["warnings"])

    def test_mode_reweighting(self, brain, model):
        text = "PostgreSQL was chosen over MongoDB for flexible indexing needs"
        insert(brain, model, make_ecu(text, conclusion_type="decision",
                                      provenance={"source_type": "architectural_reasoning"}), "DEC")
        insert(brain, model, make_ecu(text, conclusion_type="observation",
                                      provenance={"source_type": "observation"}), "OBS")
        cfg = get_config()
        cands = [{"ecu": {**brain.get_ecu(cid), "brain": "canonical"},
                  "similarity": 0.9, "brain": "canonical"} for cid in ("DEC", "OBS")]
        scored = {s["ecu"]["id"]: s for s in retrieval._rank_candidates(
            brain, cands, activation=None, mode_params=cfg.modes["architecture"],
            session_id=None, config=cfg)}
        assert scored["DEC"]["bonus"] == pytest.approx(0.05 + 0.03)
        assert scored["OBS"]["bonus"] == pytest.approx(0.0)
        assert scored["DEC"]["rank"] > scored["OBS"]["rank"]

    def test_debugging_prioritize_contradictions_slot(self, brain, model):
        # D11: debugging's "contradictions" prioritize is an edge-type slot
        text = "Retries amplify the outage when the downstream is saturated"
        insert(brain, model, make_ecu(text), "PLAIN")
        insert(brain, model, make_ecu(text, status="challenged"), "CHAL")
        cfg = get_config()
        cands = [{"ecu": {**brain.get_ecu(cid), "brain": "canonical"},
                  "similarity": 0.9, "brain": "canonical"} for cid in ("PLAIN", "CHAL")]
        scored = {s["ecu"]["id"]: s for s in retrieval._rank_candidates(
            brain, cands, activation=None, mode_params=cfg.modes["debugging"],
            session_id=None, config=cfg)}
        # CHAL: +0.05 (challenged matches the "contradictions" slot)
        #       +0.03 (debugging bias — make_ecu source_type is debugging)
        assert scored["CHAL"]["bonus"] == pytest.approx(0.05 + 0.03)
        assert scored["PLAIN"]["bonus"] == pytest.approx(0.03)  # bias only


# ---------------------------------------------------------------------------
# Cognitive grouping, dedup, packing (§11.4, §11.5, §11.7)
# ---------------------------------------------------------------------------

class TestGrouping:
    def test_depth_per_mode(self, ref_brain, model):
        # debugging: depth 0 — bare cores despite a rich edge network
        dbg = retrieval.retrieve(ref_brain, REF_QUERIES["Q1"][0],
                                 mode="debugging", embedding_model=model)
        assert all(not g["supporting_evidence"] and not g["dependencies"]
                   and not g["contradictions"] for g in dbg["groups"])
        # architecture: depth 1 — neighbours present
        arch = retrieval.retrieve(ref_brain, REF_QUERIES["Q2"][0],
                                  mode="architecture", embedding_model=model)
        assert any(g["dependencies"] or g["supporting_evidence"]
                   for g in arch["groups"])

    def test_within_group_dedup(self, brain, model):
        insert(brain, model, make_ecu("The core conclusion about auth flow"), "A")
        insert(brain, model, make_ecu("Neighbour evidence about tokens"), "B")
        # B reachable from A via TWO edge types → included once, highest-priority slot
        brain.add_edge("B", "A", "supports")
        brain.add_edge("A", "B", "depends_on")

        entry = {"ecu": {**brain.get_ecu("A"), "brain": "canonical"},
                 "similarity": 0.9, "brain": "canonical", "rank": 1.0}
        group = retrieval._build_group(brain, entry, depth=1, session_id=None)
        total = (len(group["supporting_evidence"]) + len(group["contradictions"])
                 + len(group["dependencies"]) + len(group["superseded_by"]))
        assert total == 1
        assert group["dependencies"][0]["id"] == "B"  # depends_on beats supports

    def test_cross_group_dedup(self, ref_brain, model):
        result = retrieval.retrieve(ref_brain, REF_QUERIES["Q2"][0],
                                    mode="architecture", embedding_model=model)
        assert result["cross_group_dedup_count"] >= 1
        assert "[see Group " in result["formatted"]
        # structured form carries see_group on the referenced entry
        referenced = [
            item["see_group"]
            for g in result["groups"]
            for slot in ("supporting_evidence", "contradictions", "dependencies")
            for item in g[slot] if "see_group" in item
        ]
        assert referenced and all(1 <= n <= result["groups_retrieved"] for n in referenced)

    def test_token_budget_packing(self, brain, model):
        # short cognitions -> estimate_tokens floor of 50 per ECU
        insert(brain, model, make_ecu("Auth tokens rotate hourly"), "A")
        insert(brain, model, make_ecu("Auth tokens are JWTs"), "B")
        brain.add_edge("A", "B", "supports")

        cfg = load_config()
        cfg.modes["architecture"]["max_tokens"] = 70  # < group cost (100), >= core (50)
        result = retrieval.retrieve(
            brain, "how do auth tokens work", mode="architecture",
            config=cfg, embedding_model=model)
        assert result["groups_retrieved"] == 1
        assert result["groups"][0]["truncated"] is True
        assert result["groups"][0]["supporting_evidence"] == []
        assert result["budget_used"] <= 70
        assert "NOTE: Context truncated" in result["formatted"]

        # budget below even one core -> stop, zero groups
        cfg.modes["architecture"]["max_tokens"] = 40
        starved = retrieval.retrieve(
            brain, "how do auth tokens work", mode="architecture",
            config=cfg, embedding_model=model)
        assert starved["groups_retrieved"] == 0

    def test_reference_q1_debugging(self, ref_brain, model):
        query, mode, expected = REF_QUERIES["Q1"]
        result = retrieval.retrieve(ref_brain, query, mode=mode,
                                    embedding_model=model)
        assert result["mode"] == "debugging"
        assert result["budget"] == 2000
        assert expected <= core_ids(result)      # all 4 debugging ECUs
        # the 4 clearly off-topic T3 ECUs are gate-filtered (verified sims)
        assert not core_ids(result) & {"T3-E2", "T3-E3", "T3-E4", "T3-E5"}
        assert result["budget_used"] <= result["budget"]

    def test_reference_q2_architecture(self, ref_brain, model):
        query, mode, expected = REF_QUERIES["Q2"]
        result = retrieval.retrieve(ref_brain, query, mode=mode,
                                    embedding_model=model)
        assert result["budget"] == 6000
        assert expected <= core_ids(result)      # all 5 architecture ECUs
        assert not core_ids(result) & {"T1-E1", "T1-E2", "T1-E3", "T1-E4"}
        # cognitive grouping brought dependency context along
        assert any(g["dependencies"] for g in result["groups"])
        assert result["cross_group_dedup_count"] >= 1  # T3-E1 hub referenced


# ---------------------------------------------------------------------------
# Activation ↔ retrieval interaction (§11.10)
# ---------------------------------------------------------------------------

class TestActivationContinuity:
    def test_warm_neighbour_ranks_higher_next_query(self, brain, model):
        insert(brain, model, make_ecu(
            "The auth middleware validates JWT tokens on every request before dispatching"), "A")
        insert(brain, model, make_ecu(
            "The cache invalidation strategy uses short TTLs to bound stale reads"), "B")
        brain.add_edge("A", "B", "supports")

        state = ActivationState("sess-1")
        state.spread(brain, ["A"], now=T0)  # B warms to 0.25 (hop 1)

        q_emb = model.encode_one("cache invalidation TTL strategy")
        sim_b = next(c["similarity"] for c in brain.find_similar(
            q_emb, threshold=-1.0, include_session=False) if c["id"] == "B")
        cand = [{"ecu": {**brain.get_ecu("B"), "brain": "canonical"},
                 "similarity": sim_b, "brain": "canonical"}]
        cfg = get_config()
        warm = retrieval._rank_candidates(
            brain, cand, activation=state, mode_params=cfg.modes["investigation"],
            session_id=None, config=cfg)[0]["rank"]
        cold = retrieval._rank_candidates(
            brain, cand, activation=None, mode_params=cfg.modes["investigation"],
            session_id=None, config=cfg)[0]["rank"]
        # warm bonus = w_activation × normalized(B) × trust × scope_proximity
        assert state.normalized("B") == pytest.approx(0.5)
        assert warm - cold == pytest.approx(0.10 * 0.5 * 1.0 * 0.7)

    def test_retrieve_applies_spread(self, ref_brain, model):
        state = ActivationState("sess-1")
        result = retrieval.retrieve(ref_brain, REF_QUERIES["Q1"][0],
                                    mode="debugging", embedding_model=model,
                                    activation=state)
        retrieved = core_ids(result)
        assert retrieved and all(state.get(eid) >= 0.5 for eid in retrieved)
        # depth-0 debugging still spreads to network neighbours (§12.1)
        if "T1-E1" in retrieved and "T1-E2" not in retrieved:
            assert state.get("T1-E2") == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Result format (§11.8 / §16.3) and edges cases
# ---------------------------------------------------------------------------

class TestResultFormat:
    def test_format_and_structure(self, ref_brain, model):
        result = retrieval.retrieve(ref_brain, REF_QUERIES["Q2"][0],
                                    mode="architecture", embedding_model=model)
        text = result["formatted"]
        assert text.startswith("=== Engineering Cognition ===")
        assert text.endswith("=== End Engineering Cognition ===")
        assert "[Mode: architecture] [Budget: 6000 tokens]" in text
        assert text.count(f"NOTE: {retrieval.FRAMING_NOTE}") == result["groups_retrieved"]
        assert "CONCLUSION:" in text and "CONFIDENCE:" in text
        assert "GROUNDING:" in text and "SCOPE:" in text

        for key in ("status", "mode", "mode_detected", "budget", "budget_used",
                    "groups_retrieved", "groups", "warnings", "brain_source",
                    "message", "formatted", "fallback"):
            assert key in result
        assert result["status"] == "ok"
        assert result["brain_source"] == "canonical"
        core = result["groups"][0]["core_ecu"]
        for key in ("id", "cognition", "conclusion_type", "confidence",
                    "confidence_label", "status", "scope_level", "scope_path",
                    "grounding", "source_type", "brain"):
            assert key in core
        assert result["groups"][0]["framing_note"] == retrieval.FRAMING_NOTE

    def test_confidence_flags(self, brain, model):
        insert(brain, model, make_ecu(
            "The session store might lose data on deploys — unverified hunch",
            confidence=0.25), "LOW")
        insert(brain, model, make_ecu(
            "The session store might lose data on deploys — wild guess",
            confidence=0.15), "VLOW")
        result = retrieval.retrieve(
            brain, "does the session store lose data on deploy",
            mode="investigation", embedding_model=model)
        text = result["formatted"]
        assert "⚠️ Low confidence — this cognition is uncertain." in text
        assert "⚠️ Very low confidence" in text
        assert any("low confidence" in w for w in result["warnings"])
        labels = {g["core_ecu"]["id"]: g["core_ecu"]["confidence_label"]
                  for g in result["groups"]}
        assert labels["LOW"] == "low" and labels["VLOW"] == "low"

    def test_empty_brain_and_scope_restrict(self, brain, model):
        empty = retrieval.retrieve(brain, "anything at all", mode="investigation",
                                   embedding_model=model)
        assert empty["status"] == "ok"
        assert empty["groups_retrieved"] == 0
        assert "No relevant engineering cognition" in empty["message"]
        assert "=== End Engineering Cognition ===" in empty["formatted"]

        insert(brain, model, make_ecu(
            "Always validate input at trust boundaries",
            scope={"level": "engineering", "path": "engineering"}), "ENG")
        insert(brain, model, make_ecu(
            "This repo validates input at the API gateway",
            scope={"level": "repo", "path": "repo:myapp"}), "REPO")
        restricted = retrieval.retrieve(
            brain, "where is input validated", mode="investigation",
            scope="repo", embedding_model=model)
        assert "ENG" not in core_ids(restricted)
        assert "REPO" in core_ids(restricted)

    def test_mode_autodetect_offline(self, ref_brain, model):
        result = retrieval.retrieve(
            ref_brain, "why is login throwing 500 after the deploy",
            use_llm_mode_detection=False, embedding_model=model)
        assert result["mode_detected"] is True
        assert result["mode"] == "debugging"  # keyword fallback
        assert result["budget"] == 2000

    def test_invalid_mode_and_scope_rejected(self, brain, model):
        with pytest.raises(ValueError):
            retrieval.retrieve(brain, "q", mode="nonsense", embedding_model=model)
        with pytest.raises(ValueError):
            retrieval.retrieve(brain, "q", scope="nonsense", embedding_model=model)
        with pytest.raises(ValueError):
            retrieval.retrieve(brain, "  ", mode="debugging", embedding_model=model)


# ---------------------------------------------------------------------------
# Live (requires OPENCODE_ZEN_API_KEY)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not api_key(), reason="OPENCODE_ZEN_API_KEY not set")
def test_retrieval_live(ref_brain, model):
    """One real LLM mode-detection call + real retrieval."""
    result = retrieval.retrieve(
        ref_brain, "How does the webhook payment processing work?",
        embedding_model=model)
    assert result["status"] == "ok"
    assert result["mode"] in ("debugging", "architecture", "implementation",
                              "investigation", "planning")
    assert result["mode_detected"] is True
    assert result["groups_retrieved"] >= 1
