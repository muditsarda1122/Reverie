"""Phase 13 Session B tests — diffuser structural proximity (D54, §8.3).

Design doc §5: the full diffuser's Step 2 has two candidate-discovery
methods — semantic similarity (find_similar) and structural proximity
(follow existing edges from semantically-similar ECUs). Only method 1
existed; these tests cover the new one-hop bounded expansion.

Run: .venv/bin/python -m pytest tests/test_phase13_structural_proximity.py -v
Offline: real Brain + real MiniLM embeddings for semantic seeding, mocked
classification LLM (ec.diffuser.call_llm).
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from unittest.mock import patch

import pytest

from ec.brain import Brain
from ec.diffuser import (
    MAX_CLASSIFY_BATCH,
    STRUCTURAL_PROXIMITY_MAX_EXTRA,
    _expand_with_structural_proximity,
    diffuse_ecu,
)

AUTH_TEXT = ("Auth tokens must be refreshed before each request to avoid "
             "stale-credential races")
NEAR_TEXT = "Token refresh happens optimistically before each request"
FAR_TEXT = ("PostgreSQL autovacuum scale factors must be tuned per table "
            "based on churn rate")


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


def make_canonical(brain, model, cognition, scope_level="repo",
                   path="repo:demo > module:auth", confidence=0.7,
                   embedding=None):
    return brain.insert_ecu({
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": scope_level, "path": path},
        "provenance": {"source_type": "debugging"},
        "grounding": {"files": ["auth/token.py"]},
        "confidence": confidence,
        "status": "active",
        "evidence_pointers": [],
    }, embedding=embedding if embedding is not None
       else model.encode_one(cognition))


def seed_rows(brain, ids, similarity=0.9):
    return [{"id": ecu_id, "similarity": similarity, "brain": "canonical"}
            for ecu_id in ids]


# ---------------------------------------------------------------------------
# unit — _expand_with_structural_proximity
# ---------------------------------------------------------------------------

def test_finds_one_hop_neighbor(brain, model):
    a = make_canonical(brain, model, AUTH_TEXT)
    b = make_canonical(brain, model, FAR_TEXT)
    brain.add_edge(a, b, "depends_on", weight=0.8)

    extra = _expand_with_structural_proximity(
        brain, seed_rows(brain, [a]))
    assert [row["id"] for row in extra] == [b]
    # inherited similarity: no direct semantic score exists
    assert extra[0]["similarity"] == 0.9


def test_follows_inbound_edges_too(brain, model):
    a = make_canonical(brain, model, AUTH_TEXT)
    b = make_canonical(brain, model, FAR_TEXT)
    brain.add_edge(b, a, "supports", weight=0.8)   # b -> a, hop still found

    extra = _expand_with_structural_proximity(
        brain, seed_rows(brain, [a]))
    assert [row["id"] for row in extra] == [b]


def test_bounded_to_max_extra(brain, model):
    a = make_canonical(brain, model, AUTH_TEXT)
    neighbours = [make_canonical(brain, model,
                                 f"Neighbour conclusion {i} about caching")
                  for i in range(7)]
    for i, n in enumerate(neighbours):
        brain.add_edge(a, n, "supports" if i % 2 else "depends_on",
                       weight=0.7)

    extra = _expand_with_structural_proximity(
        brain, seed_rows(brain, [a]))
    assert len(extra) == STRUCTURAL_PROXIMITY_MAX_EXTRA


def test_never_exceeds_classification_batch(brain, model):
    """10 semantic seeds leave zero room — extras must be empty so
    classification stays one batched call (MAX_CLASSIFY_BATCH)."""
    a = make_canonical(brain, model, AUTH_TEXT)
    b = make_canonical(brain, model, FAR_TEXT)
    brain.add_edge(a, b, "depends_on")

    seeds = seed_rows(brain, [a], similarity=0.9)
    seeds += [{"id": f"seed-{i}", "similarity": 0.9, "brain": "canonical"}
              for i in range(MAX_CLASSIFY_BATCH - 1)]
    assert len(seeds) == MAX_CLASSIFY_BATCH

    assert _expand_with_structural_proximity(brain, seeds) == []


def test_no_duplicates_seed_or_shared(brain, model):
    a1 = make_canonical(brain, model, AUTH_TEXT)
    a2 = make_canonical(brain, model, NEAR_TEXT)
    shared = make_canonical(brain, model, FAR_TEXT)
    brain.add_edge(a1, shared, "depends_on")
    brain.add_edge(a2, shared, "supports")

    extra = _expand_with_structural_proximity(
        brain, seed_rows(brain, [a1, a2]))
    assert [row["id"] for row in extra] == [shared]     # exactly once

    already_seeded = _expand_with_structural_proximity(
        brain, seed_rows(brain, [a1, a2, shared]))
    assert already_seeded == []


def test_skips_dead_statuses(brain, model):
    a = make_canonical(brain, model, AUTH_TEXT)
    deprecated_id = make_canonical(brain, model, FAR_TEXT)
    superseded_id = make_canonical(brain, model,
                                   "Superseded scaling belief for auth")
    brain.update_ecu_status(deprecated_id, "deprecated")
    brain.update_ecu_status(superseded_id, "superseded")
    brain.add_edge(a, deprecated_id, "depends_on")
    brain.add_edge(a, superseded_id, "supports")

    assert _expand_with_structural_proximity(
        brain, seed_rows(brain, [a])) == []


def test_excluded_ids_are_skipped(brain, model):
    """Reconsolidation excludes its target pair — a structural hop must
    not resurrect an excluded id."""
    a = make_canonical(brain, model, AUTH_TEXT)
    excluded = make_canonical(brain, model, FAR_TEXT)
    brain.add_edge(a, excluded, "depends_on")

    extra = _expand_with_structural_proximity(
        brain, seed_rows(brain, [a]), exclude_ids=frozenset({excluded}))
    assert extra == []


# ---------------------------------------------------------------------------
# integration — the expansion feeds classification in the full diffuser
# ---------------------------------------------------------------------------

def test_structural_neighbor_reaches_classification(brain, model):
    near = make_canonical(brain, model, NEAR_TEXT)
    auth = make_canonical(brain, model, AUTH_TEXT)
    far = make_canonical(brain, model, FAR_TEXT)
    brain.add_edge(auth, far, "depends_on", weight=0.8)

    prompts = []

    def fake_call_llm(user, system=None, temperature=0.0, config=None,
                      **_kw):
        prompts.append(user)
        return json.dumps({"classifications": [
            {"pair": 1, "relationship": "supports"},    # auth (semantic)
            {"pair": 2, "relationship": "supports"},    # far  (structural)
        ]})

    with patch("ec.diffuser.call_llm", side_effect=fake_call_llm):
        result = diffuse_ecu(brain, near)

    # The structurally-reachable-but-semantically-far ECU was classified…
    assert len(prompts) == 1
    assert FAR_TEXT.split()[0] in prompts[0]
    assert result.candidates == 2
    targets = {e["target_id"] for e in result.edges_created}
    assert targets == {auth, far}
    # …and its edge/update used the inherited seed similarity (the
    # semantic score of the AUTH ECU the hop started from).
    sims = {r["id"]: r["similarity"] for r in brain.find_similar(
        model.encode_one(NEAR_TEXT), threshold=-1.0)}
    support = next(e for e in result.edges_created if e["type"] == "supports"
                   and e["target_id"] == far)
    assert support["weight"] == pytest.approx(sims[auth])


def test_unrelated_structural_neighbor_discarded(brain, model):
    near = make_canonical(brain, model, NEAR_TEXT)
    auth = make_canonical(brain, model, AUTH_TEXT)
    far = make_canonical(brain, model, FAR_TEXT)
    brain.add_edge(auth, far, "depends_on", weight=0.8)

    def fake_call_llm(user, system=None, temperature=0.0, config=None,
                      **_kw):
        return json.dumps({"classifications": [
            {"pair": 1, "relationship": "unrelated"},
            {"pair": 2, "relationship": "unrelated"},
        ]})

    with patch("ec.diffuser.call_llm", side_effect=fake_call_llm):
        result = diffuse_ecu(brain, near)

    # classified (candidates include the structural extra) but harmless:
    # 'unrelated' creates no edge and touches no confidence.
    assert result.candidates == 2
    assert result.edges_created == []
    assert result.confidence_updates == []
