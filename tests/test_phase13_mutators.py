"""Phase 13 tests — grounding/evidence mutators (design doc §8, D55;
SPEC §20.3).

§20.3 declares ``grounding`` and ``evidence`` mutable: file paths may be
refreshed after a refactor, and new supporting references may be appended.
Phase 13 adds the two mutators that make those fields actually updatable —
``Brain.update_ecu_grounding`` (merge semantics; commit anchors preserved)
and ``Brain.add_evidence_pointer`` (append-only, deduped) — and wires the
latter into reconsolidation's supports path so strengthened evidence is
stamped as a reference alongside its confidence bump.

Run: .venv/bin/python -m pytest tests/test_phase13_mutators.py -v
All offline: real embeddings, no LLM calls (explicit relationship).
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json

import pytest

from ec.brain import Brain, BrainError
from ec.reconsolidation import reconsolidate


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


def _ecu(brain, **overrides) -> str:
    body = {
        "cognition": "Token refresh must invalidate the cached session scope",
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:demo > module:auth"},
        "provenance": {"source_type": "debugging", "source_id": "s1"},
        "grounding": {
            "repo_path": "/repos/demo",
            "files": ["auth/token.py"],
            "symbols": ["refresh"],
            "commit_hash": "abc123def456",
            "code_snapshot": "invalidate(session_scope)",
        },
        "evidence_pointers": ["auth/token.py", "PR #42"],
        "confidence": 0.6,
    }
    body.update(overrides)
    return brain.insert_ecu(body)


# ---------------------------------------------------------------------------
# update_ecu_grounding (§20.3 item 1)
# ---------------------------------------------------------------------------

class TestUpdateGrounding:
    def test_file_paths_update_after_refactor(self, brain):
        ecu_id = _ecu(brain)

        brain.update_ecu_grounding(ecu_id, {
            "files": ["auth/session.py", "auth/refresh.py"],
            "symbols": ["SessionScope.refresh"],
        })

        g = brain.get_ecu(ecu_id)["grounding"]
        assert g["files"] == ["auth/session.py", "auth/refresh.py"]
        assert g["symbols"] == ["SessionScope.refresh"]

    def test_commit_hash_and_snapshot_preserved(self, brain):
        """§20.3: commit_hash and code_snapshot remain as historical
        references — a partial update must not drop them."""
        ecu_id = _ecu(brain)

        brain.update_ecu_grounding(ecu_id, {"files": ["auth/moved.py"]})

        g = brain.get_ecu(ecu_id)["grounding"]
        assert g["commit_hash"] == "abc123def456"
        assert g["code_snapshot"] == "invalidate(session_scope)"
        assert g["repo_path"] == "/repos/demo"
        assert g["files"] == ["auth/moved.py"]

    def test_explicit_new_value_wins_over_old(self, brain):
        ecu_id = _ecu(brain)

        brain.update_ecu_grounding(
            ecu_id, {"commit_hash": "fresh456old789"})

        assert brain.get_ecu(ecu_id)["grounding"]["commit_hash"] == \
            "fresh456old789"

    def test_audit_document_regenerated(self, brain):
        """The document column mirrors authoritative columns (house rule for
        every mutator)."""
        ecu_id = _ecu(brain)

        brain.update_ecu_grounding(ecu_id, {"files": ["auth/new.py"]})

        doc = json.loads(brain._conn.execute(
            "SELECT document FROM ecus WHERE id = ?", (ecu_id,)
        ).fetchone()[0])
        assert doc["grounding"]["files"] == ["auth/new.py"]

    def test_unknown_ecu_raises(self, brain):
        with pytest.raises(BrainError, match="no ECU with id"):
            brain.update_ecu_grounding("no-such-id", {"files": []})

    def test_non_dict_grounding_rejected(self, brain):
        ecu_id = _ecu(brain)
        with pytest.raises(TypeError):
            brain.update_ecu_grounding(ecu_id, ["not", "a", "dict"])

    def test_cognition_unchanged(self, brain):
        """§13.2: grounding is mutable, cognition is not — one more guard
        that no code path blurs that line."""
        ecu_id = _ecu(brain)
        before = brain.get_ecu(ecu_id)["cognition"]

        brain.update_ecu_grounding(ecu_id, {"files": ["x.py"]})

        assert brain.get_ecu(ecu_id)["cognition"] == before


# ---------------------------------------------------------------------------
# add_evidence_pointer (§20.3 item 4)
# ---------------------------------------------------------------------------

class TestAddEvidencePointer:
    def test_appends_preserving_existing(self, brain):
        ecu_id = _ecu(brain)

        brain.add_evidence_pointer(ecu_id, "ADR-017")

        pointers = brain.get_ecu(ecu_id)["evidence_pointers"]
        assert pointers == ["auth/token.py", "PR #42", "ADR-017"]

    def test_identical_pointer_not_duplicated(self, brain):
        ecu_id = _ecu(brain)
        brain.add_evidence_pointer(ecu_id, "ADR-017")

        brain.add_evidence_pointer(ecu_id, "ADR-017")

        pointers = brain.get_ecu(ecu_id)["evidence_pointers"]
        assert pointers.count("ADR-017") == 1
        assert len(pointers) == 3

    def test_audit_document_regenerated(self, brain):
        ecu_id = _ecu(brain)

        brain.add_evidence_pointer(ecu_id, "slack://eng-auth/threads/99")

        doc = json.loads(brain._conn.execute(
            "SELECT document FROM ecus WHERE id = ?", (ecu_id,)
        ).fetchone()[0])
        assert "slack://eng-auth/threads/99" in doc["evidence_pointers"]

    def test_unknown_ecu_raises(self, brain):
        with pytest.raises(BrainError, match="no ECU with id"):
            brain.add_evidence_pointer("no-such-id", "ref")

    def test_empty_pointer_rejected(self, brain):
        ecu_id = _ecu(brain)
        with pytest.raises(ValueError):
            brain.add_evidence_pointer(ecu_id, "   ")
        assert brain.get_ecu(ecu_id)["evidence_pointers"] == [
            "auth/token.py", "PR #42",
        ]


# ---------------------------------------------------------------------------
# reconsolidation wiring (design doc §8.3): support evidence becomes a
# §20.3 reference on the target
# ---------------------------------------------------------------------------

class TestReconsolidationWiring:
    def test_support_evidence_stamped_as_pointer(self, brain, model):
        ecu_id = _ecu(brain, confidence=0.5)
        evidence = (
            "Re-verified against the live code today: refresh_token() calls "
            "session_scope.invalidate() on every path — the invariant holds."
        )

        result = reconsolidate(
            brain, ecu_id, evidence,
            relationship="supports", embedding_model=model,
        )

        assert result.action_taken == "confidence_updated"
        pointers = brain.get_ecu(ecu_id)["evidence_pointers"]
        stamped = [p for p in pointers if p.startswith("reconsolidation:")]
        assert len(stamped) == 1
        assert evidence[:60] in stamped[0]
        # original references untouched
        assert pointers[0] == "auth/token.py" and pointers[1] == "PR #42"
        # confidence did move too
        assert result.new_confidence > result.old_confidence

    def test_same_evidence_twice_single_pointer(self, brain, model):
        ecu_id = _ecu(brain, confidence=0.5)
        evidence = "Refresh path confirmed idempotent under retry storm."

        reconsolidate(brain, ecu_id, evidence,
                      relationship="supports", embedding_model=model)
        reconsolidate(brain, ecu_id, evidence,
                      relationship="supports", embedding_model=model)

        pointers = [p for p in brain.get_ecu(ecu_id)["evidence_pointers"]
                    if p.startswith("reconsolidation:")]
        assert len(pointers) == 1          # deduped by add_evidence_pointer

    def test_contradiction_does_not_add_support_pointer(self, brain, model):
        """§20.3's evidence mutability covers SUPPORTING references;
        contradicting evidence challenges via status/confidence instead."""
        ecu_id = _ecu(brain, confidence=0.5)

        result = reconsolidate(
            brain, ecu_id,
            "The refresh flow was removed entirely in commit 9a8b — tokens "
            "are now stateless JWTs.",
            relationship="contradicts", embedding_model=model,
        )

        assert result.action_taken == "challenged"
        assert not [p for p in brain.get_ecu(ecu_id)["evidence_pointers"]
                    if p.startswith("reconsolidation:")]
