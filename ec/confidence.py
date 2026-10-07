"""Confidence mathematics (Spec Section 11) — pure functions, no DB, no LLM.

All Bayesian updates happen in log-odds space for numerical stability
(§15.1), then convert back to [0, 1] for storage:

    L = log(c / (1 - c))          c = 1 / (1 + exp(-L))

Support (§15.2):       L_B' = L_B + w_support   x r(A,B) x c_A
Contradiction (§15.3): L_B' = L_B - w_contradict x r(A,B) x c_A

The update magnitude (w x r x c_A) is stored on the edge as
``confidence_delta`` so updates are reversible (§15.6).

Controlled forgetting (design doc Section 2, D34) adds the lazy time
decay: ``effective_confidence`` computes the scope-decayed confidence
from the stored value and the ``last_reinforced`` timestamp WITHOUT any
database writes. Decay only ever lowers confidence; reinforcement
(``reinforce_stored_confidence``) bumps the STORED value and resets the
decay clock — a write performed by the retrieval path.

Also home to the §5.9 initial-prior computation (base prior x scope
modifier + corroboration bump, capped/floored), the §15.5 supersession
trigger, and §15.7 bounded transitive propagation.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np

from .config import get_base_prior, get_config
from .embeddings import from_blob

if TYPE_CHECKING:
    from .brain import Brain

log = logging.getLogger("ec.confidence")

# §13.4 / §20.4: a supersession is "cosmetic" (reworded, same meaning) or
# "semantic" (genuinely replaced). The spec defines the distinction but no
# detector — we use embedding cosine similarity of the two cognitions.
# Above this threshold the new ECU is a cosmetic rewording (decision D6).
COSMETIC_SIMILARITY = 0.92

_EPS = 1e-9


# ---------------------------------------------------------------------------
# §15.1 representation
# ---------------------------------------------------------------------------

def to_log_odds(c: float) -> float:
    """Probability [0,1] -> log-odds. Clamped to (0,1) exclusive."""
    c = min(max(c, _EPS), 1.0 - _EPS)
    return math.log(c / (1.0 - c))


def to_probability(L: float) -> float:
    """Log-odds -> probability in [0,1] (sigmoid)."""
    if L >= 0:
        return 1.0 / (1.0 + math.exp(-L))
    exp_l = math.exp(L)
    return exp_l / (1.0 + exp_l)


# ---------------------------------------------------------------------------
# Controlled forgetting (design doc Section 2, D34) — lazy decay
# ---------------------------------------------------------------------------

#: Statuses whose confidence is FROZEN (§17.5): parked/terminal states keep
#: the value they had at the transition so the audit trail stays intact.
FROZEN_STATUSES = frozenset(
    {"open_question", "superseded", "deprecated", "archived"}
)


def _parse_ts(value: str | datetime) -> datetime:
    """ISO 8601 string or datetime -> aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def effective_confidence(
    stored_confidence: float,
    scope_level: str,
    last_reinforced: str | datetime | None,
    status: str | None = None,
    now: str | datetime | None = None,
    config=None,
) -> float:
    """Time-decayed confidence, computed lazily — never written (D34).

    The stored confidence is the value set by the last *event* (Diffuser
    update, retrieval reinforcement, status transition). The effective
    confidence is what ranking should use:

        L_eff = logit(c_stored) - lambda_decay[scope] * elapsed_days

    - Frozen statuses (open_question, superseded, deprecated, archived)
      return the stored value unchanged — no decay on parked beliefs.
    - ``last_reinforced=None`` means "never reinforced": the caller decides
      the fallback clock. Use ``ecu_effective_confidence`` for the ECU-dict
      convenience wrapper that falls back to ``created_at``.
    - ``now`` is injectable for tests; defaults to real UTC now. Elapsed
      time is clamped at zero (future timestamps do not boost confidence).
    """
    cfg = config or get_config()
    if status in FROZEN_STATUSES:
        return stored_confidence
    if last_reinforced is None:
        return stored_confidence

    lambda_decay = cfg.confidence.lambda_decay.get(scope_level)
    if lambda_decay is None:
        raise ValueError(
            f"unknown scope_level {scope_level!r}; "
            f"expected one of {sorted(cfg.confidence.lambda_decay)}"
        )
    now_dt = _parse_ts(now) if now is not None else datetime.now(timezone.utc)
    elapsed_days = max(0.0, (now_dt - _parse_ts(last_reinforced)).total_seconds()) / 86400.0
    L_stored = to_log_odds(stored_confidence)
    return to_probability(L_stored - lambda_decay * elapsed_days)


def ecu_effective_confidence(ecu: dict, now=None, config=None) -> float:
    """``effective_confidence`` for an ECU dict.

    Resolves the decay clock as ``metadata.last_reinforced`` with fallback
    to ``provenance.created_at`` (§2.6: an ECU that has never been
    reinforced decays from its creation time). Session-brain ECUs must NOT
    be passed here — they are ephemeral and never decay (callers pass the
    stored confidence straight through for them).
    """
    metadata = ecu.get("metadata") or {}
    reference = metadata.get("last_reinforced") or (
        ecu.get("provenance") or {}
    ).get("created_at")
    return effective_confidence(
        ecu["confidence"],
        ecu["scope"]["level"],
        reference,
        status=ecu.get("status"),
        now=now,
        config=config,
    )


def reinforce_stored_confidence(stored_confidence: float, config=None) -> float:
    """Retrieval reinforcement bump (§2.1): applied to the STORED value.

        c' = sigmoid(logit(c_stored) + alpha_retrieval)

    Resetting ``last_reinforced`` to now is the caller's job (a write).
    Pure math — no status awareness here; frozen-status handling lives at
    the write site (the retrieval path skips reinforcement for them).
    """
    cfg = config or get_config()
    return to_probability(to_log_odds(stored_confidence) + cfg.confidence.alpha_retrieval)


# ---------------------------------------------------------------------------
# §5.9 initial confidence priors (used by the Extractor)
# ---------------------------------------------------------------------------

def initial_prior(
    source_type: str,
    scope_level: str,
    n_sources: int = 1,
    config=None,
) -> float:
    """Two-dimensional prior: base (source_type) x scope modifier + bump.

    prior = base_prior x scope_multiplier
          + min((n_sources - 1) x corroboration_bump, corroboration_bump_cap)
    then capped at prior_cap (0.95) and floored at prior_floor (0.05).
    """
    cfg = config or get_config()
    base = get_base_prior(source_type, cfg)
    multiplier = cfg.confidence.scope_multipliers.get(scope_level)
    if multiplier is None:
        raise ValueError(
            f"unknown scope_level {scope_level!r}; "
            f"expected one of {sorted(cfg.confidence.scope_multipliers)}"
        )
    bump = min(
        max(0, n_sources - 1) * cfg.confidence.corroboration_bump,
        cfg.confidence.corroboration_bump_cap,
    )
    prior = base * multiplier + bump
    return min(max(prior, cfg.confidence.prior_floor), cfg.confidence.prior_cap)


# ---------------------------------------------------------------------------
# §15.2 / §15.3 Bayesian updates
# ---------------------------------------------------------------------------

def support_update(
    c_B: float, c_A: float, relevance: float, config=None
) -> tuple[float, float]:
    """Apply a `supports` edge A->B. Returns (new_c_B, confidence_delta).

    delta = w_support x r(A,B) x c_A  (log-odds space, stored on the edge)
    """
    cfg = config or get_config()
    delta = cfg.confidence.w_support * relevance * c_A
    new_c = to_probability(to_log_odds(c_B) + delta)
    return new_c, delta


def contradiction_update(
    c_B: float, c_A: float, relevance: float, config=None
) -> tuple[float, float]:
    """Apply a `contradicts` edge A->B. Returns (new_c_B, confidence_delta).

    delta = -w_contradict x r(A,B) x c_A  (log-odds space, negative)
    """
    cfg = config or get_config()
    delta = -(cfg.confidence.w_contradict * relevance * c_A)
    new_c = to_probability(to_log_odds(c_B) + delta)
    return new_c, delta


def reverse_update(c_B: float, confidence_delta: float) -> float:
    """§15.6 edge-pruning reversal: L_B' = L_B - edge.confidence_delta."""
    return to_probability(to_log_odds(c_B) - confidence_delta)


# ---------------------------------------------------------------------------
# §15.4 / §15.5 supersession
# ---------------------------------------------------------------------------

def should_supersede(
    old_confidence: float,
    replacement_exists: bool,
    config=None,
) -> bool:
    """§15.5 trigger — BOTH conditions must hold:

    1. old ECU's confidence < theta_supersede (default 0.3)
    2. a replacement ECU exists that better explains the evidence

    You don't supersede a belief just because it's weakened; you need a
    replacement.
    """
    cfg = config or get_config()
    return bool(replacement_exists) and old_confidence < cfg.confidence.theta_supersede


def supersession_type(
    old_cognition: str,
    new_cognition: str,
    embedding_model=None,
    old_embedding: bytes | None = None,
    new_embedding: bytes | None = None,
) -> str:
    """Classify a supersession as 'cosmetic' or 'semantic' (§13.4).

    Cosmetic = reworded but same meaning (embedding cosine >=
    COSMETIC_SIMILARITY); semantic = genuinely replaced. Prefers provided
    embedding blobs; falls back to encoding the cognitions; falls back to
    'semantic' if no embedding model is available (never guesses cosmetic).
    """
    try:
        if old_embedding is not None and new_embedding is not None:
            v_old, v_new = from_blob(old_embedding), from_blob(new_embedding)
        else:
            model = embedding_model
            if model is None:
                from .embeddings import get_embedding_model
                model = get_embedding_model()
            v_old, v_new = model.encode([old_cognition, new_cognition])
        sim = float(np.dot(v_old, v_new))  # vectors are L2-normalized
    except Exception as exc:  # embedding failure must not block supersession
        log.warning("supersession_type: embedding failed (%s); defaulting semantic", exc)
        return "semantic"
    return "cosmetic" if sim >= COSMETIC_SIMILARITY else "semantic"


# ---------------------------------------------------------------------------
# §15.7 propagation
# ---------------------------------------------------------------------------

def propagate(ecu_id: str, new_confidence: float, brain: "Brain", config=None) -> list[str]:
    """Bounded transitive propagation after a confidence change (§15.7).

    If the ECU's confidence dropped below theta_dep_reevaluate (0.4), ECUs
    that depend_on it are marked `challenged`. Propagation is transitive but
    bounded to max_propagation_depth (2). A challenged dependent's own
    dependents are re-checked at the next depth (their confidence is
    unchanged, so they only propagate further if already below the theta).

    Returns the list of ECU ids newly marked challenged (excluding ecu_id).
    """
    cfg = config or get_config()
    theta = cfg.confidence.theta_dep_reevaluate
    max_depth = cfg.confidence.max_propagation_depth
    if new_confidence >= theta:
        return []

    challenged: list[str] = []
    visited = {ecu_id}
    # (node_id, confidence_at_check, depth)
    frontier = [(ecu_id, new_confidence, 0)]
    while frontier:
        node_id, node_conf, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        if node_conf >= theta:
            continue
        # ECUs that depend on `node_id`: sources of inbound depends_on edges.
        for edge in brain.get_edges_to(node_id):
            if edge["type"] != "depends_on":
                continue
            dependent_id = edge["source_id"]
            if dependent_id in visited:
                continue
            visited.add(dependent_id)
            dependent = brain.get_ecu(dependent_id)
            if dependent is None or dependent["status"] in (
                "superseded", "deprecated", "archived",
            ):
                continue
            if dependent["status"] != "challenged":
                brain.update_ecu_status(dependent_id, "challenged")
                challenged.append(dependent_id)
            frontier.append((dependent_id, dependent["confidence"], depth + 1))
    return challenged
