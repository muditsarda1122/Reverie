"""Reconsolidation loop (Spec §2.3 arch.decision 3, §4.2 Direction 3, §8.2
Input 2, §8.5 third context; design doc Section 5, D37/D38/D39).

Retrieval makes a canonical ECU labile: when the agent discovers new evidence
about a previously retrieved ECU during the session, ``ec_reconsolidate``
feeds that evidence back through the Diffuser. The change is written to the
Canonical Brain immediately — the ECU was already human-reviewed when it
entered the brain; reconsolidation is an evidence update, not a new ECU
entering (§5.5).

Mechanics (design doc §5.3): the evidence becomes a *pseudo-ECU* — neutral
confidence 0.5, scope/grounding/type inherited from the target — that runs
through the same find-related → classify → Bayesian-update pipeline as a real
ECU, except:

- The pseudo-ECU is NOT stored. ``Brain.add_edge`` requires both endpoints to
  exist (and the schema enforces FKs), so supports/contradicts reconsolidations
  persist their effect as confidence/status/metadata changes on existing ECUs;
  the audit trail lives in ``metadata.reconsolidations`` on the target.
- Only the ``supersedes`` path materializes an ECU: a replacement must exist
  for §15.5 supersession, so the evidence is inserted as a real canonical ECU
  (provenance source_type ``reconsolidation``) and the old ECU is superseded.
- If the agent passes an explicit ``relationship``, it is trusted for the
  target pair (the agent made the semantic judgment against the live code):
  classification AND §17.2 adjudication are skipped and the math is applied
  directly. Without one, the standard batched classifier decides.
- An explicit ``supersedes`` whose §15.5 trigger fails (old ECU still ≥
  θ_supersede) falls back to contradiction handling rather than D8's
  downgrade-to-supports: an agent asserting "this belief is outdated" is
  never evidence FOR it — the honest state is contested, i.e. challenged.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

import numpy as np

from . import confidence as conf
from . import diffuser
from .brain import _now_iso
from .config import RETRIEVABLE_STATUSES, get_config
from .diffuser import (
    _RELATIONSHIPS,
    CLASSIFICATION_SYSTEM,
    DiffuserError,
    DiffusionResult,
    _apply_supersession,
    _classify_pairs,
)
from .embeddings import from_blob

log = logging.getLogger("ec.reconsolidation")

#: How much evidence text is kept in the metadata audit note (the full text
#: lives with the agent; the note only needs to identify the update).
MAX_EVIDENCE_NOTE = 280

#: The pseudo-ECU's confidence (design doc §5.3 step 2: neutral — the
#: evidence is unreviewed). Also the weight of the Bayesian update (c_A).
PSEUDO_CONFIDENCE = 0.5

VALID_RELATIONSHIPS = ("supports", "contradicts", "supersedes")

# actions surfaced to the agent (design doc §5.2 result schema)
ACTION_CONFIDENCE_UPDATED = "confidence_updated"
ACTION_CHALLENGED = "challenged"
ACTION_SUPERSEDED = "superseded"
ACTION_NO_CHANGE = "no_change"


class ReconsolidationError(RuntimeError):
    """Reconsolidation failure with actionable guidance (§28.4 style)."""


@dataclass
class ReconsolidationResult:
    """§5.2 result body plus internal detail for tests/callers."""

    ecu_id: str = ""
    action_taken: str = ACTION_NO_CHANGE
    relationship: str = "unrelated"      # resolved relationship vs target
    classified: bool = False             # True when the classifier decided
    old_confidence: float = 0.0
    new_confidence: float = 0.0
    edges_created: int = 0
    new_ecu_id: str | None = None        # set when superseded
    related_updates: list[dict] = field(default_factory=list)
    diffusion_error: str | None = None   # related-ECU pass failed (best-effort)
    message: str = ""


def _validate_relationship(relationship: str | None) -> None:
    if relationship is not None and relationship not in VALID_RELATIONSHIPS:
        raise ReconsolidationError(
            f"invalid relationship {relationship!r}; expected one of "
            f"{list(VALID_RELATIONSHIPS)} (or omit it to let the Diffuser "
            "classify the evidence)"
        )


def _cosine(vec_a, vec_b) -> float:
    return float(np.dot(np.asarray(vec_a, dtype=np.float32),
                        np.asarray(vec_b, dtype=np.float32)))


def _build_pseudo_ecu(target: dict, evidence: str, model) -> dict:
    """The evidence as a temporary ECU (design doc §5.3 step 2): neutral
    confidence, inherited scope/grounding/type, never stored."""
    embedding = model.encode_one(evidence) if model is not None else None
    return {
        "id": f"pseudo::{uuid.uuid4()}",
        "cognition": evidence,
        "conclusion_type": target["conclusion_type"],
        "scope": dict(target["scope"]),
        "grounding": dict(target.get("grounding") or {}),
        "confidence": PSEUDO_CONFIDENCE,  # neutral — the evidence is unreviewed
        "status": "active",
        "provenance": {"source_type": "reconsolidation",
                       "created_at": _now_iso()},
        "evidence_pointers": [],
        "embedding": embedding,
        "metadata": {},
    }


def _as_vector(value):
    """bytes blob | ndarray | None -> ndarray | None."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return from_blob(bytes(value))
    return value


def _append_audit_note(brain, ecu_id: str, relationship: str, evidence: str,
                       delta: float) -> None:
    """Persist the evidence relationship on the target's metadata. Edges
    cannot reference the unstored pseudo-ECU; this is the audit trail."""
    ecu = brain.get_ecu(ecu_id)
    if ecu is None:
        return
    history = list((ecu.get("metadata") or {}).get("reconsolidations") or [])
    history.append({
        "at": _now_iso(),
        "relationship": relationship,
        "evidence": evidence[:MAX_EVIDENCE_NOTE],
        "confidence_delta": round(delta, 6),
        "via": "ec_reconsolidate",
    })
    brain.update_ecu_metadata(ecu_id, reconsolidations=history)


def _evidence_pointer(evidence: str) -> str:
    """The evidence text as a §20.3 supporting reference (design doc §8.3):
    tagged with its origin and truncated to the audit-note length so the
    pointer stays a reference, not a transcript."""
    return f"reconsolidation: {evidence[:MAX_EVIDENCE_NOTE]}"


def _apply_support_to_target(brain, target: dict, similarity: float,
                             evidence: str, cfg) -> tuple[float, float]:
    """Bayesian support update + evidence stamp, no edge (pseudo-ECU owns
    none). The new evidence is a §20.3 supporting reference — appended to
    the target's evidence_pointers alongside the confidence bump."""
    new_c, delta = conf.support_update(
        target["confidence"], PSEUDO_CONFIDENCE, similarity, cfg)
    brain.update_ecu_confidence(target["id"], new_c)
    brain.add_evidence_pointer(target["id"], _evidence_pointer(evidence))
    _append_audit_note(brain, target["id"], "supports", evidence, delta)
    return new_c, delta


def _apply_contradiction_to_target(brain, target: dict, similarity: float,
                                   evidence: str, cfg) -> tuple[float, float]:
    """Bayesian contradiction update + challenged (§8.3 step 6), no edge."""
    new_c, delta = conf.contradiction_update(
        target["confidence"], PSEUDO_CONFIDENCE, similarity, cfg)
    brain.update_ecu_confidence(target["id"], new_c)
    if target["status"] != "challenged":
        brain.update_ecu_status(target["id"], "challenged")
    brain.update_ecu_metadata(target["id"], last_challenged=_now_iso())
    _append_audit_note(brain, target["id"], "contradicts", evidence, delta)
    # §15.7 propagation — dependents of a weakened belief are re-evaluated.
    conf.propagate(target["id"], new_c, brain, cfg)
    return new_c, delta


def _materialize_evidence_ecu(brain, target: dict, evidence: str, session_id,
                              model) -> dict:
    """Supersession needs a real replacement (§15.5 condition 2): insert the
    evidence as a canonical ECU. Confidence is its own (0.5) — NOT inherited
    from the superseded ECU (§15.4)."""
    ecu = {
        "cognition": evidence,
        "conclusion_type": target["conclusion_type"],
        "scope": dict(target["scope"]),
        "provenance": {
            "source_type": "reconsolidation",
            "source_id": session_id,
            "created_at": _now_iso(),
        },
        "grounding": dict(target.get("grounding") or {}),
        "confidence": PSEUDO_CONFIDENCE,
        "status": "active",
        "evidence_pointers": [],
        "metadata": {"reconsolidates": target["id"]},
    }
    embedding = model.encode_one(evidence) if model is not None else None
    ecu_id = brain.insert_ecu(ecu, embedding=embedding)
    stored = brain.get_ecu(ecu_id)
    if stored is None:                       # pragma: no cover - defensive
        raise ReconsolidationError(f"materialized ECU {ecu_id} vanished")
    return stored


def reconsolidate(
    brain,
    ecu_id: str,
    evidence: str,
    relationship: str | None = None,
    embedding_model=None,
    config=None,
    session_id: str | None = None,
) -> ReconsolidationResult:
    """Re-evaluate a retrieved canonical ECU with new evidence (D37).

    Returns a :class:`ReconsolidationResult`; raises
    :class:`ReconsolidationError` for invalid input (unknown/frozen ECU, bad
    relationship). LLM failures while classifying raise DiffuserError — pass
    ``relationship`` explicitly to reconsolidate without an LLM.
    """
    cfg = config or get_config()
    _validate_relationship(relationship)

    if not isinstance(evidence, str) or not evidence.strip():
        raise ReconsolidationError(
            "ec_reconsolidate requires 'evidence' (a non-empty description of "
            "what you found and why it affects the ECU)"
        )
    evidence = evidence.strip()

    target = brain.get_ecu(ecu_id)
    if target is None:
        raise ReconsolidationError(
            f"No canonical ECU with id {ecu_id}. Use the ecu_id exactly as it "
            "appeared in the ec_query result."
        )
    if target["status"] not in RETRIEVABLE_STATUSES:
        raise ReconsolidationError(
            f"ECU {ecu_id} has status {target['status']!r} and can no longer "
            "be reconsolidated (only active, challenged, or open_question "
            "ECUs are revisable)"
        )

    if embedding_model is None:
        from .embeddings import get_embedding_model
        embedding_model = get_embedding_model()

    pseudo = _build_pseudo_ecu(target, evidence, embedding_model)
    target_vec = _as_vector(target.get("embedding"))
    if target_vec is None:
        target_vec = embedding_model.encode_one(target["cognition"])
    pseudo_vec = _as_vector(pseudo["embedding"])
    similarity = (
        _cosine(pseudo_vec, target_vec)
        if pseudo_vec is not None and target_vec is not None
        else 0.0
    )

    result = ReconsolidationResult(
        ecu_id=ecu_id,
        relationship=relationship or "unrelated",
        classified=relationship is None,
        old_confidence=target["confidence"],
    )
    scratch = DiffusionResult(ecu_id=ecu_id)   # collects supersession bookkeeping

    resolved = relationship
    if resolved is None:
        # One batched classification for the target pair (Rule A/B prompt).
        rels = _classify_pairs([(pseudo, target)], CLASSIFICATION_SYSTEM,
                               _RELATIONSHIPS, cfg)
        resolved = rels[0]
        result.relationship = resolved

    materialized: dict | None = None
    new_c = result.old_confidence
    if resolved == "supersedes":
        if conf.should_supersede(target["confidence"], replacement_exists=True,
                                 config=cfg):
            materialized = _materialize_evidence_ecu(
                brain, target, evidence, session_id, embedding_model)
            _apply_supersession(brain, materialized, target, scratch, cfg,
                                embedding_model)
            result.new_ecu_id = materialized["id"]
            result.action_taken = ACTION_SUPERSEDED
        else:
            # Trigger fails: the standing belief is still strong, but the
            # agent says it is outdated — contested, not supported.
            log.info("reconsolidate %s: supersedes trigger fails (c=%.2f); "
                     "recording as contradiction", ecu_id, target["confidence"])
            new_c, _ = _apply_contradiction_to_target(
                brain, target, similarity, evidence, cfg)
            result.action_taken = ACTION_CHALLENGED
    elif resolved == "contradicts":
        new_c, _ = _apply_contradiction_to_target(
            brain, target, similarity, evidence, cfg)
        result.action_taken = ACTION_CHALLENGED
    elif resolved == "supports":
        new_c, _ = _apply_support_to_target(
            brain, target, similarity, evidence, cfg)
        result.action_taken = ACTION_CONFIDENCE_UPDATED
    else:
        # unrelated (or a misclassified depends_on): nothing to update.
        result.action_taken = ACTION_NO_CHANGE
        result.relationship = "unrelated"

    # -- related ECUs: the evidence may affect other beliefs too (§5.3 step 5)
    try:
        if materialized is not None:
            # Real replacement → full integration, edges allowed. The
            # superseded target drops out (status no longer retrievable).
            dres = diffuser._diffuse_against_brain(
                brain, materialized, embedding_model, cfg)
        else:
            dres = diffuser._diffuse_against_brain(
                brain, pseudo, embedding_model, cfg,
                store_edges=False, exclude_ids=(ecu_id,))
        result.related_updates = dres.confidence_updates
        result.edges_created += len(dres.edges_created)
    except DiffuserError as exc:
        # The target update stands; enriching related ECUs is best-effort.
        log.warning("related-ECU diffusion failed for %s: %s", ecu_id, exc)
        result.diffusion_error = str(exc)

    result.edges_created += len(scratch.edges_created)
    updated = brain.get_ecu(ecu_id)
    result.new_confidence = (
        updated["confidence"] if updated is not None else new_c)
    result.message = _message_for(result)
    return result


def _message_for(r: ReconsolidationResult) -> str:
    what = {
        ACTION_CONFIDENCE_UPDATED: (
            f"Evidence recorded as support; confidence "
            f"{r.old_confidence:.2f} → {r.new_confidence:.2f}."),
        ACTION_CHALLENGED: (
            f"Evidence contradicts the ECU; it is now challenged "
            f"(confidence {r.old_confidence:.2f} → {r.new_confidence:.2f})."),
        ACTION_SUPERSEDED: (
            f"Evidence supersedes the ECU; new ECU {r.new_ecu_id} replaces it "
            "(the old belief is frozen for audit)."),
        ACTION_NO_CHANGE: (
            "The evidence was classified as unrelated to this ECU; nothing "
            "was changed."),
    }[r.action_taken]
    extra = ""
    if r.related_updates:
        extra = (f" {len(r.related_updates)} related ECU(s) also updated.")
    if r.diffusion_error:
        extra += (f" Note: related-ECU diffusion failed ({r.diffusion_error}); "
                  "the main update was applied.")
    return what + extra
