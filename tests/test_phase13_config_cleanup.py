"""Phase 13 tests — dead config keys cleanup (design doc §11).

Five §15 keys existed but were never read. ``contradiction.notify_on_dependent``
is now WIRED: review_gate._notify_propagation (the dependent-chain notifier;
the design doc locates it in diffuser.py but it has always lived in
review_gate.py) gates its notifications on it — silencing the notice while
§15.7 propagation itself always runs. The other four are decorative and now
say so inline in DEFAULT_CONFIG.

Run: .venv/bin/python -m pytest tests/test_phase13_config_cleanup.py -v
Offline: real embeddings; LLM calls mocked at ec.diffuser.call_llm.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ec.brain import Brain
from ec.config import DEFAULT_CONFIG, _deep_merge, _to_attrdict, get_config
from ec.review_gate import ReviewResult, _notify_propagation, apply_review_decisions

T0 = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)

SUPPORT_T = "The cache layer uses write-through writes to stay consistent"
CONTRA_S = "The cache layer uses write-back writes for throughput"
DEP_T = "Queue retries must be idempotent because the cache write path is not"


def make_ecu(cognition: str, **overrides) -> dict:
    ecu = {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:project-a > module:auth"},
        "provenance": {"source_type": "debugging", "source_id": "s1"},
        "grounding": {"files": ["auth/cache.py"], "symbols": ["put"]},
        "confidence": 0.5,
        "status": "active",
        "evidence_pointers": [],
    }
    ecu.update(overrides)
    return ecu


def classifications_response(*rels: str) -> str:
    return json.dumps({
        "classifications": [
            {"pair": i + 1, "relationship": rel}
            for i, rel in enumerate(rels)
        ]
    })


GENUINE = json.dumps({"verdict": "genuine_contradiction",
                      "differentiator": ""})


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


def cfg_with(**overrides):
    merged = _deep_merge(DEFAULT_CONFIG, overrides)
    return _to_attrdict(merged)


# ---------------------------------------------------------------------------
# unit — the notification gate itself
# ---------------------------------------------------------------------------

class TestNotifyGateUnit:
    def _result(self):
        r = ReviewResult(session_id="s")
        before = list(r.notifications)
        return r, before

    def test_default_on_appends_notifications(self, brain):
        result, before = self._result()

        _notify_propagation(brain, result, ["dep-1", "dep-2"])

        kinds = [n["kind"] for n in result.notifications]
        assert kinds == ["depends_on_at_risk"] * 2
        assert len(result.notifications) - len(before) == 2

    def test_off_silences_notifications(self, brain):
        cfg = cfg_with(contradiction={"notify_on_dependent": False})
        result = ReviewResult(session_id="s")

        _notify_propagation(brain, result, ["dep-1"], cfg)

        assert result.notifications == []

    def test_explicit_true_still_notifies(self, brain):
        cfg = cfg_with(contradiction={"notify_on_dependent": True})
        result = ReviewResult(session_id="s")

        _notify_propagation(brain, result, ["dep-1"], cfg)

        assert len(result.notifications) == 1

    def test_config_default_is_true(self):
        """§15 default unchanged: notifications stay on out of the box."""
        assert get_config().contradiction.notify_on_dependent is True


# ---------------------------------------------------------------------------
# integration — gate through apply_review_decisions; propagation unaffected
# ---------------------------------------------------------------------------

class TestNotifyGateIntegration:
    def _setup_chain(self, brain, model):
        """B (weak, will be contradicted below theta) <- depends_on - C."""
        b = model.encode_one
        weak = brain.insert_ecu(
            make_ecu(SUPPORT_T, confidence=0.35),   # already < theta_dep_reevaluate
            embedding=b(SUPPORT_T),
        )
        dependent = brain.insert_ecu(
            make_ecu(DEP_T, confidence=0.7,
                     grounding={"files": ["queue/retry.py"],
                                "symbols": ["retry"]}),
            embedding=b(DEP_T),
        )
        brain.add_edge(dependent, weak, "depends_on", weight=0.8)
        s = brain.insert_session_ecu(
            brain.create_session("/repos/project-a", "main"),
            make_ecu(CONTRA_S, confidence=0.7),
            embedding=b(CONTRA_S),
        )
        return weak, dependent, s

    def test_off_no_notification_but_still_propagates(self, brain, model):
        """The core §11 contract: False kills the NOTICE, never the effect.
        C must still end up challenged via §15.7 propagation."""
        weak, dependent, s = self._setup_chain(brain, model)
        cfg = cfg_with(contradiction={"notify_on_dependent": False})

        with patch("ec.diffuser.call_llm", side_effect=[
            classifications_response("contradicts"), GENUINE,
        ]):
            result = apply_review_decisions(
                brain, _session_id(brain, s), {s: "accept"},
                config=cfg, embedding_model=model, now=T0,
            )

        assert brain.get_ecu(dependent)["status"] == "challenged"
        assert not [n for n in result.notifications
                    if n["kind"] == "depends_on_at_risk"]

    def test_on_notification_present(self, brain, model):
        weak, dependent, s = self._setup_chain(brain, model)
        cfg = cfg_with()      # defaults: notify_on_dependent True

        with patch("ec.diffuser.call_llm", side_effect=[
            classifications_response("contradicts"), GENUINE,
        ]):
            result = apply_review_decisions(
                brain, _session_id(brain, s), {s: "accept"},
                config=cfg, embedding_model=model, now=T0,
            )

        assert brain.get_ecu(dependent)["status"] == "challenged"
        notices = [n for n in result.notifications
                   if n["kind"] == "depends_on_at_risk"]
        assert len(notices) == 1
        assert dependent in notices[0]["ecu_ids"]


def _session_id(brain, session_ecu_id: str) -> str:
    row = brain._conn.execute(
        "SELECT session_id FROM session_ecus WHERE id = ?",
        (session_ecu_id,),
    ).fetchone()
    return row["session_id"]
