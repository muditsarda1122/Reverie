"""Phase 13 Session B tests — search UP the scope hierarchy (D50, §11.11).

Design doc §6: retrieval gains a working-scope anchor (the session's
repo_path at 'repo' level; an explicit ``scope=`` narrows it further).
Candidates are filtered UP-only — more-general scopes kept, more-specific
and sideways contexts dropped — with a fallback to the unfiltered set
when the filter empties everything.

Run: .venv/bin/python -m pytest tests/test_phase13_scope_hierarchy.py -v
Offline: real Brain + real MiniLM embeddings; no LLM calls.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest

from ec.brain import Brain
from ec.retrieval import (
    _filter_by_scope_hierarchy,
    retrieve,
)


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


def cand(level, path, cognition="cognition text", similarity=0.9,
         brain_name="canonical"):
    """A retrieval-shaped entry: {"ecu", "similarity", "brain"}."""
    return {
        "ecu": {
            "id": cognition,
            "cognition": cognition,
            "scope": {"level": level, "path": path},
        },
        "similarity": similarity,
        "brain": brain_name,
    }


# ---------------------------------------------------------------------------
# unit — _filter_by_scope_hierarchy
# ---------------------------------------------------------------------------

ANCHOR = ("repo", "/Users/dev/api-server")


def test_up_only_keeps_general_filters_specific():
    candidates = [
        cand("engineering", "engineering", "universal principle"),
        cand("domain", "domain:web-backend", "domain practice"),
        cand("project", "project:my-saas", "project convention"),
        cand("module", "repo:api-server > module:auth", "module detail"),
        cand("subsystem", "repo:api-server > module:auth > subsystem:tokens",
             "subsystem detail"),
    ]
    kept_ids = [c["ecu"]["id"]
                for c in _filter_by_scope_hierarchy(candidates, *ANCHOR)]
    assert kept_ids == ["universal principle", "domain practice"]


def test_same_level_same_context_kept():
    same_repo_label = cand("repo", "repo:api-server > module:auth")
    assert len(_filter_by_scope_hierarchy([same_repo_label], *ANCHOR)) == 1


def test_sibling_excluded():
    sibling = cand("repo", "repo:payments-service")
    assert _filter_by_scope_hierarchy([sibling], *ANCHOR) == []


def test_engineering_universal_regardless_of_working_scope():
    eng = cand("engineering", "engineering")
    for working in ("subsystem", "module", "repo", "project",
                    "organization", "domain", "engineering"):
        assert len(_filter_by_scope_hierarchy([eng], working, "/x/repo")) == 1


def test_unknown_level_and_paths_do_not_overfilter():
    unknown_level = cand("galaxy", "milky-way")
    default_path = cand("repo", "repo")           # extractor default: path==level
    empty_path = cand("repo", "")
    kept = _filter_by_scope_hierarchy(
        [unknown_level, default_path, empty_path], *ANCHOR)
    assert len(kept) == 3

    # ...but an unknown path never rescues a DOWN-scope ECU:
    assert _filter_by_scope_hierarchy(
        [cand("subsystem", "subsystem")], *ANCHOR) == []

    # unknown WORKING level keeps everything too
    everything = [cand("module", "m"), cand("repo", "r")]
    assert len(_filter_by_scope_hierarchy(everything, "galaxy", "")) == 2


def test_scope_param_narrows_working_scope():
    repo_ecu = cand("repo", "repo:api-server", "repo-level ECU")
    subsystem_ecu = cand("subsystem", "repo:api-server > subsystem:tokens",
                         "subsystem-level ECU")

    # anchor narrowed to module: repo is now UP (kept), subsystem is DOWN
    kept = _filter_by_scope_hierarchy(
        [repo_ecu, subsystem_ecu], "module", "/Users/dev/api-server")
    assert [c["ecu"]["id"] for c in kept] == ["repo-level ECU"]


# ---------------------------------------------------------------------------
# integration — the filter runs inside retrieve() when a session anchors it
# ---------------------------------------------------------------------------

def make_canonical(brain, model, cognition, level, path, confidence=0.8):
    return brain.insert_ecu({
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": level, "path": path},
        "provenance": {"source_type": "debugging"},
        "grounding": {"files": []},
        "confidence": confidence,
        "status": "active",
        "evidence_pointers": [],
    }, embedding=model.encode_one(cognition))


def test_retrieve_filters_down_scope_with_session_anchor(brain, model):
    """Working at repo level: a semantically-relevant module-scope ECU is
    NOT retrieved (down), while an engineering principle is."""
    session_id = brain.create_session("/repos/demo-app", "main")

    module_id = make_canonical(
        brain, model,
        "The auth module refreshes tokens optimistically before requests",
        "module", "repo:other-app > module:auth")
    eng_id = make_canonical(
        brain, model,
        "Always validate authentication tokens before trusting a request",
        "engineering", "engineering")

    result = retrieve(
        brain, "how does auth token refresh work", mode="debugging",
        session_id=session_id, embedding_model=model)
    ids = {g["core_ecu"]["id"] for g in result["groups"]}
    assert eng_id in ids
    assert module_id not in ids


def test_retrieve_falls_back_when_filter_empties_everything(brain, model):
    """All candidates below the anchor → fallback to unfiltered: empty
    results are worse than broad ones (§11.5 gate-fallback pattern)."""
    session_id = brain.create_session("/repos/demo-app", "main")

    module_id = make_canonical(
        brain, model,
        "The auth module refreshes tokens optimistically before requests",
        "module", "repo:some-other-app > module:auth")

    result = retrieve(
        brain, "how does auth token refresh work", mode="debugging",
        session_id=session_id, embedding_model=model)
    ids = {g["core_ecu"]["id"] for g in result["groups"]}
    assert module_id in ids


def test_retrieve_sessionless_is_unfiltered(brain, model):
    """No session → no working-scope anchor → no hierarchy filtering
    (the §11.11 proximity multiplier still ranks, as before)."""
    module_id = make_canonical(
        brain, model,
        "The auth module refreshes tokens optimistically before requests",
        "module", "repo:anywhere > module:auth")

    result = retrieve(
        brain, "how does auth token refresh work", mode="debugging",
        embedding_model=model)
    ids = {g["core_ecu"]["id"] for g in result["groups"]}
    assert module_id in ids


def test_retrieve_same_repo_via_convention_bridge(brain, model):
    """Session anchored at /repos/api-server + ECU labelled
    'repo:api-server > ...' at repo level: the label embeds the anchor's
    directory name, so the ECU is recognised as the SAME context and kept
    (sideways exclusion would otherwise drop the current repo)."""
    session_id = brain.create_session("/repos/api-server", "main")

    own_repo_id = make_canonical(
        brain, model,
        "The api-server auth module refreshes tokens optimistically",
        "repo", "repo:api-server > module:auth")

    result = retrieve(
        brain, "how does auth token refresh work", mode="debugging",
        session_id=session_id, embedding_model=model)
    ids = {g["core_ecu"]["id"] for g in result["groups"]}
    assert own_repo_id in ids


def test_retrieve_sibling_repo_excluded_but_not_alone(brain, model):
    """Sideways exclusion works when another candidate survives: a
    different repo's ECU is dropped while the engineering one stays."""
    session_id = brain.create_session("/repos/api-server", "main")

    eng_id = make_canonical(
        brain, model,
        "Always validate authentication tokens before trusting a request",
        "engineering", "engineering")
    sibling_id = make_canonical(
        brain, model,
        "The payments service validates authentication tokens on every call",
        "repo", "repo:payments-service")

    result = retrieve(
        brain, "how does auth token validation work", mode="debugging",
        session_id=session_id, embedding_model=model)
    ids = {g["core_ecu"]["id"] for g in result["groups"]}
    assert eng_id in ids
    assert sibling_id not in ids
