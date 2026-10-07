"""Phase 13 tests — Opposing-Edges Decomposition Flag (design doc §3, D53).

§14.3 exception clause: if ECU_A both supports AND contradicts ECU_B, the
relationship needs decomposition into more atomic ECUs and must be flagged
for human review. The diffuser checks after every supports/contradicts edge
creation; the review gate additionally scans for pairs accumulated across
sessions (or created via pending updates, which bypass the handlers).

Run: .venv/bin/python -m pytest tests/test_phase13_opposing_edges.py -v
All offline: no embedding model needed (handlers take ECU dicts directly);
contradiction adjudication is mocked at the LLM boundary.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest

from ec.brain import Brain
from ec.config import DEFAULT_CONFIG, _to_attrdict
from ec.diffuser import (
    DiffusionResult,
    _check_opposing_edges,
    _handle_contradiction,
    _handle_support,
)
from ec.review_gate import ReviewResult, _opposing_edge_notifications

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def config():
    return _to_attrdict(DEFAULT_CONFIG)


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture(scope="session")
def embedding_model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


def _ecu(brain, cognition):
    return brain.insert_ecu({
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:demo > module:auth"},
        "provenance": {"source_type": "debugging"},
        "grounding": {"files": ["auth/token.py"]},
        "confidence": 0.8,
    })


def _pair(brain):
    a = _ecu(brain, "Token refresh must invalidate the cached session scope")
    b = _ecu(brain, "Session cache is keyed by user id, not by token")
    return brain.get_ecu(a), brain.get_ecu(b)


def _run_support(brain, new_ecu, existing, cfg):
    result = DiffusionResult(ecu_id=new_ecu["id"])
    _handle_support(brain, new_ecu, existing, 0.85, result, cfg,
                    _model=None)
    return result


def _run_contradiction(brain, new_ecu, existing, cfg, monkeypatch):
    """Drive the real contradiction handler with Stage-2 adjudication
    mocked genuine (same scope + grounding overlap passes Stage 1)."""
    monkeypatch.setattr(
        "ec.diffuser._adjudicate_contradiction",
        lambda *args, **kwargs: "genuine_contradiction",
    )
    result = DiffusionResult(ecu_id=new_ecu["id"])
    _handle_contradiction(brain, new_ecu, existing, 0.85, result, cfg,
                          _model=None)
    return result


# ---------------------------------------------------------------------------
# brain.get_edges_between helper
# ---------------------------------------------------------------------------

class TestGetEdgesBetween:
    def test_directional_and_type_filtered(self, brain, config):
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)

        assert len(brain.get_edges_between(a["id"], b["id"])) == 1
        # reverse direction is a different (empty) pair
        assert brain.get_edges_between(b["id"], a["id"]) == []
        # type filter
        assert brain.get_edges_between(a["id"], b["id"],
                                       edge_type="contradicts") == []
        got = brain.get_edges_between(a["id"], b["id"], edge_type="supports")
        assert got[0]["type"] == "supports"

    def test_multiple_types_coexist_on_one_pair(self, brain, config):
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)
        brain.add_edge(a["id"], b["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)
        both = brain.get_edges_between(a["id"], b["id"])
        assert {e["type"] for e in both} == {"supports", "contradicts"}


# ---------------------------------------------------------------------------
# the diffuser check
# ---------------------------------------------------------------------------

class TestCheckOpposingEdges:
    def test_flag_when_opposing_edge_exists(self, brain, config):
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)

        flag = _check_opposing_edges(brain, a["id"], b["id"], "supports")

        assert flag is not None
        assert flag["kind"] == "opposing_edges"
        assert flag["source_id"] == a["id"]
        assert flag["target_id"] == b["id"]
        assert flag["has_supports"] is True       # the new type counts
        assert flag["has_contradicts"] is True    # pre-existing opposing edge
        assert "decomposition" in flag["message"]

    def test_no_flag_when_only_same_type_exists(self, brain, config):
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)

        assert _check_opposing_edges(brain, a["id"], b["id"],
                                     "supports") is None

    def test_no_flag_when_pair_has_no_edges(self, brain, config):
        a, b = _pair(brain)
        assert _check_opposing_edges(brain, a["id"], b["id"],
                                     "supports") is None

    @pytest.mark.parametrize("edge_type", ["depends_on", "supersedes"])
    def test_non_evidence_types_never_checked(self, brain, config,
                                              edge_type):
        """The §14.3 signal is specifically supports-vs-contradicts."""
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)
        assert _check_opposing_edges(brain, a["id"], b["id"],
                                     edge_type) is None


# ---------------------------------------------------------------------------
# wiring inside the full-diffuser handlers
# ---------------------------------------------------------------------------

class TestHandlerWiring:
    def test_handle_support_flags_opposing_pair(self, brain, config):
        """Second session's support onto an earlier contradicted pair."""
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)

        result = _run_support(brain, a, b, config)

        kinds = [f["kind"] for f in result.flags]
        assert "opposing_edges" in kinds
        assert any(e["type"] == "supports" for e in result.edges_created)

    def test_handle_contradiction_flags_opposing_pair(self, brain, config,
                                                      monkeypatch):
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)

        result = _run_contradiction(brain, a, b, config, monkeypatch)

        kinds = [f["kind"] for f in result.flags]
        assert "opposing_edges" in kinds
        assert any(e["type"] == "contradicts" for e in result.edges_created)

    def test_clean_diffusion_raises_no_flag(self, brain, config,
                                            monkeypatch):
        a, b = _pair(brain)
        result = _run_contradiction(brain, a, b, config, monkeypatch)
        # genuine first-time contradiction: challenged flags may fire on
        # high stakes, but never an opposing_edges one
        assert all(f["kind"] != "opposing_edges" for f in result.flags)

    def test_downgraded_supersedes_still_checked(self, brain, config,
                                                 monkeypatch):
        """D8 path: classifier says supersedes but the §15.5 trigger fails →
        downgrade to supports → the opposing-edge check still runs."""
        from ec.diffuser import _handle_supersedes
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)

        result = DiffusionResult(ecu_id=a["id"])
        _handle_supersedes(brain, a, b, 0.85, result, config, model=None)

        assert any(e["type"] == "supports" for e in result.edges_created)
        assert any(f["kind"] == "opposing_edges" for f in result.flags)


# ---------------------------------------------------------------------------
# review-gate surfacing (notifications)
# ---------------------------------------------------------------------------

class TestReviewGateSurfacing:
    def test_accumulated_pair_surfaced_as_notification(self, brain, config):
        """A pair that accumulated its two edges across sessions (never in
        the same diffusion) is surfaced at the NEXT gate run."""
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)
        brain.add_edge(a["id"], b["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)

        result = ReviewResult(session_id="s1")
        _opposing_edge_notifications(brain, result)

        assert len(result.notifications) == 1
        note = result.notifications[0]
        assert note["kind"] == "opposing_edges"
        assert set(note["ecu_ids"]) == {a["id"], b["id"]}
        assert "decomposition" in note["message"]

    def test_healthy_brain_no_notification(self, brain, config):
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)
        c = _ecu(brain, "Auth tokens expire after 24 hours")
        brain.add_edge(a["id"], c, "depends_on", weight=0.7)

        result = ReviewResult(session_id="s1")
        _opposing_edge_notifications(brain, result)

        assert result.notifications == []

    def test_no_duplicate_when_diffuser_already_flagged(self, brain, config):
        """When the diffuser flagged the pair during THIS gate run, the
        post-pass does not report it a second time."""
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)
        brain.add_edge(a["id"], b["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)

        result = ReviewResult(session_id="s1")
        result.flags.append({
            "kind": "opposing_edges",
            "ecu_ids": [a["id"], b["id"]],
            "message": "flagged during diffusion",
        })
        _opposing_edge_notifications(brain, result)

        assert len(result.flags) == 1
        assert result.notifications == []

    def test_other_flags_do_not_block_notification(self, brain, config):
        """Only opposing_edges FLAGS dedupe; unrelated flags don't."""
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)
        brain.add_edge(a["id"], b["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)

        result = ReviewResult(session_id="s1")
        result.flags.append({
            "kind": "high_stakes_contradiction",
            "ecu_ids": [a["id"], b["id"]],
            "message": "different concern",
        })
        _opposing_edge_notifications(brain, result)

        assert [n["kind"] for n in result.notifications] == ["opposing_edges"]

    def test_reverse_direction_pair_caught(self, brain, config):
        """Edges are directional: A→B supports + B→A contradicts is two
        different pairs. §14.3 speaks of one ECU doing both TO another
        (same direction), so reverse-direction pairs stay unflagged
        (documented in the handoff)."""
        a, b = _pair(brain)
        brain.add_edge(a["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)
        brain.add_edge(b["id"], a["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)   # opposite direction

        result = ReviewResult(session_id="s1")
        _opposing_edge_notifications(brain, result)

        assert result.notifications == []


# ---------------------------------------------------------------------------
# end-to-end through apply_review_decisions' post-pass call site
# ---------------------------------------------------------------------------

class TestGateEndToEnd:
    def test_apply_review_decisions_surfaces_accumulated_pair(
        self, brain, config, embedding_model,
    ):
        """The gate's post-pass runs on every review: a canonical pair that
        accumulated its two edges across earlier sessions surfaces in the
        ReviewResult even when THIS gate's accept creates no edges at all."""
        from ec.review_gate import apply_review_decisions

        # History: pair accumulated both edge types across sessions.
        x, b = _pair(brain)
        brain.add_edge(x["id"], b["id"], "supports", weight=0.9,
                       confidence_delta=0.05)
        brain.add_edge(x["id"], b["id"], "contradicts", weight=0.8,
                       confidence_delta=-0.04)

        # This session accepts one candidate with no similar canonical ECU —
        # diffusion finds nothing, so any opposing_edges report must come
        # from the post-pass scan.
        sid = brain.create_session("/tmp/repo-demo", "main")
        se_id = brain.insert_session_ecu(
            sid,
            {
                "cognition": "Release trains depart every Tuesday at noon",
                "conclusion_type": "observation",
                "scope": {"level": "project", "path": "project:demo"},
                "provenance": {"source_type": "planning"},
                "grounding": {"files": ["release/calendar.md"]},
                "confidence": 0.5,
            },
        )

        result = apply_review_decisions(
            brain, sid, {se_id: "accept"},
            config=config, embedding_model=embedding_model,
        )

        kinds = [n.get("kind") for n in result.notifications + result.flags]
        assert kinds.count("opposing_edges") == 1
