"""Phase 4 tests — Human Review Gate (§5/§7, §28.6/§28.7, §17.4/§17.5).

Run: .venv/bin/python -m pytest tests/ -v
Offline: real embeddings (HF cache); all LLM calls mocked at
ec.diffuser.call_llm (classification + adjudication), following Phase 2.
Verified embedding similarities (MiniLM, offline):
  supports pair 0.695 | contradicts pair 0.927 | engineering pair 0.993
  depends_on pair 0.901 | case-3 pair 0.925   (diffuser threshold: 0.6)
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pytest

from ec import confidence as conf
from ec.brain import Brain
from ec.llm import LLMError
from ec.review_gate import (
    ReviewGateError,
    ReviewResult,
    apply_review_decisions,
    diffusion_failure_warning,
    group_for_review,
    interactive_review,
)

T0 = datetime(2026, 2, 1, 9, 0, 0, tzinfo=timezone.utc)

SUPPORTS_T = "Cache invalidation must follow token refresh or stale auth tokens persist"
SUPPORTS_S = "The auth race condition is caused by refresh completing before cache clear"
CONTRA_T = "The auth module uses optimistic token refresh"
CONTRA_S = "The auth module uses pessimistic token refresh"
ENG_T = "Input validation at trust boundaries is always required"
ENG_S = "Input validation at trust boundaries is never required"
DEP_B = "The cache layer uses write-through writes to stay consistent"
DEP_S = "The cache layer uses write-back writes for throughput"
CASE3_T = "The login failure is caused by a stale session cache"
CASE3_S = "The login failure is caused by a corrupted session cache"

GENUINE = json.dumps({"verdict": "genuine_contradiction", "differentiator": ""})


def make_ecu(cognition: str, **overrides) -> dict:
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:project-a > module:auth"},
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


@pytest.fixture()
def session_id(brain):
    return brain.create_session("/repos/project-a", "main")


def insert_canonical(brain, model, cognition, **overrides):
    return brain.insert_ecu(
        make_ecu(cognition, **overrides),
        embedding=model.encode_one(cognition),
    )


def insert_session(brain, model, session_id, cognition, **overrides):
    return brain.insert_session_ecu(
        session_id, make_ecu(cognition, **overrides),
        embedding=model.encode_one(cognition),
    )


def sim(model, a, b):
    va, vb = m_encode(model, a), m_encode(model, b)
    return float(np.dot(va, vb))


def m_encode(model, text):
    return model.encode_one(text)


# ---------------------------------------------------------------------------
# grouping (§7.4)
# ---------------------------------------------------------------------------

def test_group_for_review(brain, model, session_id):
    insert_session(brain, model, session_id, "Finding alpha", confidence=0.6)
    insert_session(brain, model, session_id, "Finding beta", confidence=0.8)
    insert_session(brain, model, session_id, "Finding gamma",
                   scope={"level": "repo", "path": "repo:project-a > module:queue"})
    e4 = insert_session(brain, model, session_id, "Finding delta")
    brain.update_session_ecu_review_status(e4, "skipped")  # comes back next review

    plan = group_for_review(brain, session_id)
    assert plan.total == 4
    assert [g.label for g in plan.groups] == [
        "repo:project-a > module:auth", "repo:project-a > module:queue",
    ]
    auth = plan.groups[0]
    assert len(auth.ecus) == 3  # pending + skipped together
    # confidence descending within a group
    assert [e["confidence"] for e in auth.ecus] == [0.8, 0.6, 0.5]


def test_group_for_review_unknown_session(brain):
    with pytest.raises(ReviewGateError):
        group_for_review(brain, "no-such-session")


# ---------------------------------------------------------------------------
# accept → promotion + full diffuser (§7.3, §8.3, §28.6)
# ---------------------------------------------------------------------------

def test_accept_promotes_and_diffuses(brain, model, session_id):
    target = insert_canonical(brain, model, SUPPORTS_T, confidence=0.5)
    s = insert_session(brain, model, session_id, SUPPORTS_S, confidence=0.63)

    with patch("ec.diffuser.call_llm",
               return_value=classifications_response("supports")):
        result = apply_review_decisions(brain, session_id, {s: "accept"})

    assert result.accepted == [s]
    promoted = brain.get_ecu(s)  # same id (D21)
    assert promoted is not None and promoted["cognition"] == SUPPORTS_S
    assert promoted["confidence"] == pytest.approx(0.63)  # §15.4: own confidence
    assert brain.get_session_ecu(s) is None  # removed from session brain

    edges = brain.get_edges_from(s)
    assert len(edges) == 1 and edges[0]["type"] == "supports"
    assert edges[0]["target_id"] == target
    r = sim(model, SUPPORTS_S, SUPPORTS_T)
    expected, delta = conf.support_update(0.5, 0.63, r)
    assert brain.get_ecu(target)["confidence"] == pytest.approx(expected, abs=1e-3)
    assert edges[0]["confidence_delta"] == pytest.approx(delta, abs=1e-3)
    assert result.canonical_total == 2
    assert "Brain now has 2 ECUs." in result.message


def test_accept_with_edit(brain, model, session_id):
    s = insert_session(brain, model, session_id, SUPPORTS_S)
    edited = "The auth race condition comes from unordered refresh and cache clear"
    with patch("ec.diffuser.call_llm",
               return_value=classifications_response("unrelated")):
        result = apply_review_decisions(
            brain, session_id,
            {s: {"action": "accept", "edited_cognition": edited}},
        )
    assert result.accepted == [s]
    promoted = brain.get_ecu(s)
    assert promoted["cognition"] == edited
    # §7.3: the Extractor's original version is preserved for audit (D19)
    edit_meta = promoted["metadata"]["review_edit"]
    assert edit_meta["original_cognition"] == SUPPORTS_S
    assert edit_meta["edited_at"]
    # re-embedded: the stored embedding matches the edited text, not the original
    from ec.embeddings import from_blob
    assert np.dot(from_blob(promoted["embedding"]),
                  m_encode(model, edited)) > 0.99


def test_accept_diffusion_failure_still_promotes(brain, model, session_id):
    # a similar canonical ECU forces the classification LLM call, which fails
    insert_canonical(brain, model, SUPPORTS_T, confidence=0.5)
    s = insert_session(brain, model, session_id, SUPPORTS_S)
    with patch("ec.diffuser.call_llm", side_effect=LLMError("no key")):
        result = apply_review_decisions(brain, session_id, {s: "accept"})
    assert result.accepted == [s]
    assert brain.get_ecu(s) is not None
    assert any("diffusion failed" in e for e in result.errors)
    # the failure is tracked for the prominent CLI warning
    assert result.undiffused == [s]
    warning = diffusion_failure_warning(result)
    assert "1 ECU(s) promoted without diffusion due to API errors" in warning
    assert s in warning
    assert "no edges and unchanged confidence" in warning
    assert "re-running diffusion manually" in warning


def test_diffusion_warning_only_for_diffusion_failures():
    # non-diffusion errors (e.g. invalid actions) must not raise the alarm
    result = ReviewResult(errors=["invalid review action 'x' for abc; skipped"])
    assert diffusion_failure_warning(result) is None
    assert diffusion_failure_warning(ReviewResult()) is None


def test_interactive_review_prints_diffusion_warning(brain, model, session_id):
    insert_canonical(brain, model, SUPPORTS_T, confidence=0.5)
    insert_session(brain, model, session_id, SUPPORTS_S)
    printed = []
    with patch("ec.diffuser.call_llm", side_effect=LLMError("offline")):
        result = interactive_review(
            brain, session_id,
            input_fn=lambda _prompt="": "a",
            print_fn=printed.append,
            embedding_model=model,
        )
    assert len(result.undiffused) == 1
    text = "\n".join(printed)
    assert "⚠️ WARNING: 1 ECU(s) promoted without diffusion" in text


# ---------------------------------------------------------------------------
# reject / skip (§7.5, §28.6)
# ---------------------------------------------------------------------------

def test_reject_removes_ecu(brain, model, session_id):
    s = insert_session(brain, model, session_id, "A mistaken conclusion")
    other = insert_session(brain, model, session_id, "An unrelated note")
    brain.add_session_edge(session_id, s, other, "supports", weight=0.8)

    result = apply_review_decisions(
        brain, session_id, {s: "reject", other: "skip"})

    assert result.rejected == [s]
    assert brain.get_session_ecu(s) is None
    assert brain.get_ecu(s) is None  # never promoted
    assert brain.list_session_edges(session_id) == []  # edges cleaned
    assert brain.get_session_ecu(other)["review_status"] == "skipped"


def test_skip_is_default_for_undecided(brain, model, session_id):
    s = insert_session(brain, model, session_id, "Deferred conclusion")
    result = apply_review_decisions(brain, session_id, {})
    assert result.skipped == [s]
    assert brain.get_session_ecu(s)["review_status"] == "skipped"


def test_invalid_action_treated_as_skip(brain, model, session_id):
    s = insert_session(brain, model, session_id, "Conclusion with bad action")
    result = apply_review_decisions(brain, session_id, {s: "maybe"})
    assert result.skipped == [s]
    assert any("invalid review action" in e for e in result.errors)


# ---------------------------------------------------------------------------
# pending updates (§28.6, D16)
# ---------------------------------------------------------------------------

def test_pending_update_applied_with_exact_delta(brain, model, session_id):
    target = insert_canonical(brain, model, SUPPORTS_T, confidence=0.5)
    s = insert_session(brain, model, session_id, SUPPORTS_S, confidence=0.63)
    brain.add_pending_update(target, s, session_id, "supports", 0.0695)
    brain.update_ecu_metadata(target, has_pending_updates=True)

    # full diffuser classifies the pair unrelated → the pending path applies
    with patch("ec.diffuser.call_llm",
               return_value=classifications_response("unrelated")):
        result = apply_review_decisions(brain, session_id, {s: "accept"})

    assert result.pending_applied == 1
    assert brain.list_pending_updates(session_id=session_id) == []  # deleted
    r = sim(model, SUPPORTS_S, SUPPORTS_T)
    expected, delta = conf.support_update(0.5, 0.63, r)
    assert brain.get_ecu(target)["confidence"] == pytest.approx(expected, abs=1e-3)
    edge = brain.get_edges_from(s)[0]
    assert edge["type"] == "supports"
    assert edge["confidence_delta"] == pytest.approx(delta, abs=1e-3)
    assert brain.get_ecu(target)["metadata"]["has_pending_updates"] is False


def test_pending_update_no_double_apply(brain, model, session_id):
    """The diffuser already edged the pair → pending marked applied, but the
    confidence delta is applied exactly once (D16)."""
    target = insert_canonical(brain, model, SUPPORTS_T, confidence=0.5)
    s = insert_session(brain, model, session_id, SUPPORTS_S, confidence=0.63)
    brain.add_pending_update(target, s, session_id, "supports", 0.0695)

    with patch("ec.diffuser.call_llm",
               return_value=classifications_response("supports")):
        result = apply_review_decisions(brain, session_id, {s: "accept"})

    assert result.pending_applied == 1
    r = sim(model, SUPPORTS_S, SUPPORTS_T)
    expected, delta = conf.support_update(0.5, 0.63, r)
    assert brain.get_ecu(target)["confidence"] == pytest.approx(expected, abs=1e-3)
    edges = brain.get_edges_from(s)
    assert len(edges) == 1  # one edge, one delta — not two
    assert edges[0]["confidence_delta"] == pytest.approx(delta, abs=1e-3)


def test_pending_update_discarded_on_reject(brain, model, session_id):
    target = insert_canonical(brain, model, SUPPORTS_T, confidence=0.5)
    s = insert_session(brain, model, session_id, SUPPORTS_S, confidence=0.63)
    brain.add_pending_update(target, s, session_id, "supports", 0.0695)
    brain.update_ecu_metadata(target, has_pending_updates=True)

    result = apply_review_decisions(brain, session_id, {s: "reject"})

    assert result.pending_discarded == 1
    assert brain.list_pending_updates(session_id=session_id) == []
    assert brain.get_ecu(target)["confidence"] == pytest.approx(0.5)
    assert brain.get_ecu(target)["metadata"]["has_pending_updates"] is False


def test_pending_update_survives_skip(brain, model, session_id):
    target = insert_canonical(brain, model, SUPPORTS_T, confidence=0.5)
    s = insert_session(brain, model, session_id, SUPPORTS_S)
    brain.add_pending_update(target, s, session_id, "supports", 0.0695)
    brain.update_ecu_metadata(target, has_pending_updates=True)

    apply_review_decisions(brain, session_id, {s: "skip"})

    pending = brain.list_pending_updates(session_id=session_id, status="pending")
    assert len(pending) == 1
    assert brain.get_ecu(target)["metadata"]["has_pending_updates"] is True


# ---------------------------------------------------------------------------
# contradiction surfacing (§17.4)
# ---------------------------------------------------------------------------

def test_contradiction_flag_both_high_confidence(brain, model, session_id):
    target = insert_canonical(brain, model, CONTRA_T, confidence=0.75)
    s = insert_session(brain, model, session_id, CONTRA_S, confidence=0.72)

    responses = [classifications_response("contradicts"), GENUINE]
    with patch("ec.diffuser.call_llm", side_effect=responses):
        result = apply_review_decisions(brain, session_id, {s: "accept"})

    kinds = [f["kind"] for f in result.flags]
    assert "high_stakes_contradiction" in kinds
    flag = result.flags[0]
    assert set(flag["ecu_ids"]) == {s, target}
    assert brain.get_ecu(target)["status"] == "challenged"
    assert brain.get_ecu(target)["confidence"] < 0.75


def test_contradiction_flag_engineering_scope(brain, model, session_id):
    """§17.4: engineering/domain-scope contradictions always flag, even
    below the 0.7 both-high threshold."""
    target = insert_canonical(
        brain, model, ENG_T, confidence=0.5,
        scope={"level": "engineering", "path": "engineering"},
        provenance={"source_type": "architectural_reasoning"},
    )
    s = insert_session(
        brain, model, session_id, ENG_S, confidence=0.5,
        scope={"level": "engineering", "path": "engineering"},
        provenance={"source_type": "implementation"},
        grounding={"files": ["auth/token_manager.py"]},
    )
    responses = [classifications_response("contradicts"), GENUINE]
    with patch("ec.diffuser.call_llm", side_effect=responses):
        result = apply_review_decisions(brain, session_id, {s: "accept"})

    kinds = [f["kind"] for f in result.flags]
    assert "scope_contradiction" in kinds
    assert "high_stakes_contradiction" not in kinds  # both sides are 0.5


def test_depends_on_chain_notification(brain, model, session_id):
    """§17.4 item 3: a depends_on chain is affected → notify."""
    b = insert_canonical(brain, model, DEP_B, confidence=0.5,
                         provenance={"source_type": "implementation"})
    a = insert_canonical(
        brain, model,
        "The session store serializes signed user claims into cookies",
        confidence=0.6, provenance={"source_type": "implementation"},
    )
    brain.add_edge(a, b, "depends_on", weight=0.9)  # A depends on B
    s = insert_session(brain, model, session_id, DEP_S, confidence=0.9)

    responses = [classifications_response("contradicts"), GENUINE]
    with patch("ec.diffuser.call_llm", side_effect=responses):
        result = apply_review_decisions(brain, session_id, {s: "accept"})

    # B dropped below theta_dep_reevaluate → propagation challenged A
    assert brain.get_ecu(b)["confidence"] < 0.4
    assert brain.get_ecu(a)["status"] == "challenged"
    notes = [n for n in result.notifications if n["kind"] == "depends_on_at_risk"]
    assert any(a in n["ecu_ids"] for n in notes)


# ---------------------------------------------------------------------------
# competing hypotheses (§17.5, D20)
# ---------------------------------------------------------------------------

def test_case3_sets_competing_since(brain, model, session_id):
    target = insert_canonical(brain, model, CASE3_T, confidence=0.45)
    s = insert_session(brain, model, session_id, CASE3_S, confidence=0.5)

    responses = [classifications_response("contradicts"), GENUINE]
    with patch("ec.diffuser.call_llm", side_effect=responses):
        apply_review_decisions(brain, session_id, {s: "accept"}, now=T0)

    for ecu_id in (s, target):
        meta = brain.get_ecu(ecu_id)["metadata"]
        assert meta["competing_since"] == T0.isoformat()


def test_case3_competing_since_never_overwritten(brain, model, session_id):
    target = insert_canonical(brain, model, CASE3_T, confidence=0.45)
    first_seen = (T0 - timedelta(days=5)).isoformat()
    brain.update_ecu_metadata(target, competing_since=first_seen)
    s = insert_session(brain, model, session_id, CASE3_S, confidence=0.5)

    responses = [classifications_response("contradicts"), GENUINE]
    with patch("ec.diffuser.call_llm", side_effect=responses):
        apply_review_decisions(brain, session_id, {s: "accept"}, now=T0)

    assert brain.get_ecu(target)["metadata"]["competing_since"] == first_seen
    assert brain.get_ecu(s)["metadata"]["competing_since"] == T0.isoformat()


def test_open_question_parking(brain, model, session_id):
    """§17.5: challenged ECUs past their scope's persistence limit are parked
    as open_question (confidence frozen) with a notification."""
    a = insert_canonical(brain, model, CASE3_T, confidence=0.45,
                         scope={"level": "module", "path": "repo:p > module:m"})
    b = insert_canonical(brain, model, CASE3_S, confidence=0.5,
                         scope={"level": "module", "path": "repo:p > module:m"})
    brain.add_edge(a, b, "contradicts", weight=0.9)
    since = (T0 - timedelta(days=40)).isoformat()  # module limit: 30 days
    for ecu_id in (a, b):
        brain.update_ecu_status(ecu_id, "challenged")
        brain.update_ecu_metadata(ecu_id, competing_since=since)
    # engineering scope: indefinite — never parked
    e = insert_canonical(brain, model, "Debate about a fundamental principle",
                         confidence=0.5,
                         scope={"level": "engineering", "path": "engineering"})
    brain.update_ecu_status(e, "challenged")
    brain.update_ecu_metadata(
        e, competing_since=(T0 - timedelta(days=1000)).isoformat())

    result = apply_review_decisions(brain, session_id, {}, now=T0)

    for ecu_id in (a, b):
        parked = brain.get_ecu(ecu_id)
        assert parked["status"] == "open_question"
    assert brain.get_ecu(a)["confidence"] == pytest.approx(0.45)  # frozen
    assert brain.get_ecu(e)["status"] == "challenged"  # indefinite limit
    oq = [n for n in result.notifications if n["kind"] == "open_question"]
    assert len(oq) == 2
    assert "competing for 40 days" in oq[0]["message"]
    assert set(oq[0]["ecu_ids"]) == {a, b}


# ---------------------------------------------------------------------------
# close-out: session closed, maintenance log, interactive driver
# ---------------------------------------------------------------------------

def test_review_closes_session_and_logs(brain, model, session_id):
    s1 = insert_session(brain, model, session_id, "Accepted conclusion")
    s2 = insert_session(brain, model, session_id, "Rejected conclusion")
    s3 = insert_session(brain, model, session_id, "Skipped conclusion")

    with patch("ec.diffuser.call_llm",
               return_value=classifications_response("unrelated")):
        result = apply_review_decisions(
            brain, session_id,
            {s1: "accept", s2: "reject", s3: "skip"}, now=T0,
        )

    assert result.message == (
        "Review complete. 1 accepted, 1 rejected, 1 skipped (pending). "
        "Diffusing to Canonical Brain... done. Brain now has 1 ECUs."
    )
    row = brain.get_session(session_id)
    assert row["status"] == "closed"
    assert row["ended_at"] == T0.isoformat()
    assert brain.last_maintenance_run() is not None
    # gate is a one-shot transition for a session
    with pytest.raises(ReviewGateError, match="closed"):
        apply_review_decisions(brain, session_id, {})


def test_interactive_review_driver(brain, model, session_id):
    e1 = insert_session(brain, model, session_id, "Auth finding one",
                        confidence=0.7)
    e2 = insert_session(brain, model, session_id, "Auth finding two",
                        confidence=0.6)
    e3 = insert_session(brain, model, session_id, "Queue finding",
                        scope={"level": "repo", "path": "repo:project-a > module:queue"})

    answers = iter(["i", "y", "d", "n", "a"])  # group auth: i → y, d, n; queue: a
    printed = []
    with patch("ec.diffuser.call_llm",
               return_value=classifications_response("unrelated")):
        result = interactive_review(
            brain, session_id,
            input_fn=lambda _prompt="": next(answers),
            print_fn=printed.append,
            embedding_model=model,
        )

    assert result.accepted == [e1, e3]
    assert result.rejected == [e2]
    assert brain.get_ecu(e1) is not None and brain.get_ecu(e3) is not None
    assert brain.get_ecu(e2) is None
    text = "\n".join(printed)
    assert "EC Review Gate — 3 candidate ECUs in 2 groups" in text
    assert "provenance:" in text  # the 'd' detail print
    assert "Cleaning up Session Brain..." in text
