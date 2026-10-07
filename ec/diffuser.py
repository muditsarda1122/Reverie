"""Engineering Diffuser (Spec Section 6 / §8.x) + Lightweight Diffuser (§4.4).

Two integration paths:

``diffuse_ecu`` — the FULL diffuser (Canonical Brain). Integrates a reviewed
ECU into the belief network: 5-way relationship classification
(supports/contradicts/supersedes/depends_on/unrelated) with Rule A and
Rule B disambiguation, edge creation, log-odds Bayesian confidence updates
in place (§11, D2), two-stage contradiction handling (§12), supersession
(both §15.5 conditions), and bounded propagation (§15.7).

``_diffuse_against_brain`` — the same pipeline as one reusable block (D38):
``diffuse_ecu`` calls it with a stored ECU; ``ec.reconsolidation.reconsolidate``
calls it with an ephemeral pseudo-ECU built from agent evidence
(``store_edges=False`` — an unstored source cannot own edges, and without a
stored replacement §15.5's second supersession condition is unmet).

``diffuse_session_ecu`` — the LIGHTWEIGHT diffuser (Session Brain, §4.4).
Fast, approximate, runs per-response during live sessions: 3-way
classification (supports/contradicts/unrelated), similarity threshold 0.7,
simple confidence bump c' = c ± alpha_light x similarity, session_edges,
pending_updates for cross-brain relationships (never writes to the
Canonical Brain — §6.7). No supersession, no propagation, no challenged
status, no depends_on.

The LLM proposes relationships; the spec's computable rules decide what
actually happens (supersession trigger §15.5, contradiction pre-filter
§17.2). Unparseable classifications default to `unrelated` — the safe
direction (§8.6: spurious edges are the harmful failure mode).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from . import confidence as conf
from .brain import _now_iso
from .config import RETRIEVABLE_STATUSES, get_config
from .llm import LLMError, call_llm, extract_json

log = logging.getLogger("ec.diffuser")

# Max ECU pairs per classification LLM call. find_similar top_k should not
# exceed this so classification stays a single batched call (§4.4 step 3).
MAX_CLASSIFY_BATCH = 10

#: D54 (design doc §5): how many edge-neighbours structural proximity may
#: add to the candidate set after find_similar, at most.
STRUCTURAL_PROXIMITY_MAX_EXTRA = 5

_RELATIONSHIPS = ("supports", "contradicts", "supersedes", "depends_on", "unrelated")
_LIGHT_RELATIONSHIPS = ("supports", "contradicts", "unrelated")


class DiffuserError(RuntimeError):
    """Diffusion failure (missing ECU, LLM error). Never a silent no-op."""


# ---------------------------------------------------------------------------
# Prompts — Rule A and Rule B are quoted verbatim from §8.3.
# ---------------------------------------------------------------------------

CLASSIFICATION_SYSTEM = """\
You are the relationship classifier for the Engineering Cognition (EC) system.
Given pairs of engineering conclusions (ECU_A is a NEW conclusion, ECU_B is an
EXISTING belief), classify each pair's relationship as exactly one of:

- supports:    ECU_A provides evidence that strengthens ECU_B's conclusion.
- contradicts: ECU_A provides evidence that weakens ECU_B's conclusion
               (same scope, same context, mutually exclusive).
- supersedes:  ECU_A replaces ECU_B as the current belief on this topic.
- depends_on:  ECU_A's conclusion is structurally dependent on ECU_B being
               valid — if ECU_B were invalidated, ECU_A would also be at risk.
- unrelated:   No meaningful relationship.

Rule A — supports vs depends_on (primary relationship test):

A relationship can be *both* evidence-based and structural, but you must
classify the **primary** relationship. Apply this decision procedure:

1. Does the new ECU provide evidence that strengthens the existing ECU's
   conclusion? (Test: "Knowing the new ECU is true, am I more confident in
   the existing ECU?") If yes -> classify as supports.
2. Only if the answer to (1) is NO, ask: Is the new ECU structurally
   dependent on the existing ECU? (Test: "If the existing ECU were
   invalidated, would the new ECU also be at risk?") If yes -> depends_on.
3. If neither -> unrelated.

The key insight: depends_on is reserved for cases where the **only**
relationship is structural — A doesn't provide evidence for B, but A would
be invalid if B were wrong. If A provides evidence for B, that's supports
even if A also happens to rely on B structurally. Evidence-based support
takes precedence over structural dependency.

Example: "Webhook processing must be idempotent" (invariant) and "set status
to 'charged' before ack" (fix decision). The fix decision is justified *by*
the invariant — it provides evidence that the invariant is a real problem
worth solving. This is supports, not depends_on, even though the fix
wouldn't exist without the invariant.

Counter-example: "PostgreSQL is suitable for message persistence" (decision)
and "PostgreSQL is sufficient at current load with partitioning as a scaling
path" (scaling implication). The scaling strategy doesn't provide evidence
that PostgreSQL is suitable — it *assumes* PostgreSQL is suitable and builds
on it. This is depends_on.

If ECU A is a recommendation, decision, or implication that follows logically
from ECU B (a mechanism, constraint, observation, or invariant), the
relationship is 'supports', never 'contradicts'. A 'contradicts'
classification requires that the two ECUs make mutually exclusive claims —
both cannot be true simultaneously. If knowing B makes A more likely to be
correct, the answer is 'supports'.

Rule B — unrelated threshold for same-subsystem ECUs:

Two ECUs about the same subsystem but addressing **different engineering
concerns** are unrelated unless one directly informs or constrains the
other. Sharing a subsystem is necessary but not sufficient for a
relationship.

Test: "Does knowing ECU_A change my confidence in ECU_B?" If A is about
write-scaling capacity and B is about schema-design fit, knowing that
PostgreSQL scales well doesn't make the schema observation more or less
true. They are unrelated.

Counter-test: If A is about consistency requirements and B is about database
choice, knowing that strong consistency is required directly constrains the
database selection. They are supports (or depends_on per Rule A).

Classify supersedes ONLY when ECU_A states the same belief as ECU_B in an
updated or replaced form (reworded, corrected, or overtaken by newer
evidence) — not merely when it contradicts it.

Respond with ONLY a JSON object:
{"classifications": [{"pair": <pair number>, "relationship": "<one word>"}]}
Include every pair number from the input. No prose."""

CLASSIFICATION_USER_TEMPLATE = """\
Classify the relationship for each ECU pair below. ECU_A is the NEW
conclusion; ECU_B is the EXISTING belief.

{pairs}

Respond with ONLY the JSON object described in your instructions."""

_LIGHTWEIGHT_SYSTEM = """\
You are the lightweight relationship classifier for the Engineering
Cognition (EC) system's Session Brain. Given pairs of engineering
conclusions (ECU_A is NEW, ECU_B is EXISTING), classify each pair as
exactly one of:

- supports:    knowing ECU_A is true makes you more confident in ECU_B.
- contradicts: ECU_A and ECU_B cannot both be true in the same scope and
               context — knowing ECU_A makes you less confident in ECU_B.
- unrelated:   no meaningful relationship.

Caution: two ECUs about the same subsystem but addressing different
engineering concerns are unrelated. Sharing a subsystem is necessary but
not sufficient for a relationship. Test: "Does knowing ECU_A change my
confidence in ECU_B?"

Respond with ONLY a JSON object:
{"classifications": [{"pair": <pair number>, "relationship": "<one word>"}]}
Include every pair number from the input. No prose."""

_CONTRADICTION_SYSTEM = """\
You are the contradiction adjudicator for the Engineering Cognition (EC)
system. Given two engineering conclusions that may contradict, decide whether
they are:

- genuine_contradiction: same scope, same context, mutually exclusive.
- orthogonal: both true in different contexts or under different conditions.
- unrelated: not actually about the same claim.

Respond with ONLY a JSON object:
{"verdict": "genuine_contradiction" | "orthogonal" | "unrelated",
 "differentiator": "<if orthogonal: the context/condition that
differentiates them; otherwise an empty string>"}"""

_CONTRADICTION_USER_TEMPLATE = """\
ECU_A (NEW): "{cognition_a}"
  Scope: {scope_a}
  Grounding: {grounding_a}
  Confidence: {confidence_a:.2f}

ECU_B (EXISTING): "{cognition_b}"
  Scope: {scope_b}
  Grounding: {grounding_b}
  Confidence: {confidence_b:.2f}

Question: Are these genuinely contradictory (same context, mutually
exclusive), or orthogonal (both true in different contexts/conditions)?"""


# ---------------------------------------------------------------------------
# result containers
# ---------------------------------------------------------------------------

@dataclass
class DiffusionResult:
    """What the full diffuser did (spec §8.3 step 9 state summary)."""

    ecu_id: str = ""
    candidates: int = 0
    edges_created: list[dict] = field(default_factory=list)
    confidence_updates: list[dict] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)       # user notifications
    supersessions: list[dict] = field(default_factory=list)
    challenged_by_propagation: list[str] = field(default_factory=list)


@dataclass
class LightDiffusionResult:
    """What the lightweight diffuser did (§4.4)."""

    ecu_id: str = ""
    session_id: str = ""
    candidates: int = 0
    session_edges: list[dict] = field(default_factory=list)
    pending_updates: list[dict] = field(default_factory=list)
    confidence_updates: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _format_pairs(pairs: list[tuple[dict, dict]]) -> str:
    """Render (new_ecu, existing_ecu) pairs for the classification prompt."""
    blocks = []
    for i, (new, old) in enumerate(pairs, start=1):
        blocks.append(
            f"Pair {i}:\n"
            f'  ECU_A (NEW): "{new["cognition"]}"\n'
            f"    type: {new['conclusion_type']} | scope: "
            f"{new['scope']['level']} ({new['scope']['path']})\n"
            f'  ECU_B (EXISTING): "{old["cognition"]}"\n'
            f"    type: {old['conclusion_type']} | scope: "
            f"{old['scope']['level']} ({old['scope']['path']}) | "
            f"confidence: {old['confidence']:.2f}"
        )
    return "\n\n".join(blocks)


def _classify_pairs(
    pairs: list[tuple[dict, dict]],
    system: str,
    allowed: tuple[str, ...],
    config=None,
) -> list[str]:
    """One batched classification call. Tolerant parse: any pair the model
    omits or mislabels defaults to 'unrelated' (the safe direction, §8.6).
    """
    if not pairs:
        return []
    if len(pairs) > MAX_CLASSIFY_BATCH:
        raise DiffuserError(
            f"classification batch of {len(pairs)} exceeds MAX_CLASSIFY_BATCH "
            f"({MAX_CLASSIFY_BATCH}); reduce top_k"
        )
    user = CLASSIFICATION_USER_TEMPLATE.format(pairs=_format_pairs(pairs))
    try:
        # Classification is a deterministic task: temperature 0.0, so the
        # same pairs always yield the same relationships (both the 5-way and
        # the lightweight 3-way prompts come through here).
        response = call_llm(user, system=system, temperature=0.0,
                            config=config or get_config())
    except LLMError as exc:
        raise DiffuserError(f"relationship classification failed: {exc}") from exc

    relationships = ["unrelated"] * len(pairs)
    try:
        payload = json.loads(extract_json(response))
        entries = payload.get("classifications", [])
    except json.JSONDecodeError:
        log.warning("classification response unparseable; all pairs unrelated. "
                    "Response: %.200r", response)
        return relationships
    for entry in entries:
        try:
            idx = int(entry.get("pair")) - 1
            rel = str(entry.get("relationship", "")).strip().lower()
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 <= idx < len(pairs) and rel in allowed:
            relationships[idx] = rel
        elif 0 <= idx < len(pairs):
            log.warning("pair %d: invalid relationship %r; treating as unrelated",
                        idx + 1, rel)
    return relationships


# ---------------------------------------------------------------------------
# contradiction handling — §17.2 two-stage classification
# ---------------------------------------------------------------------------

#: D40 — different-scope pairs at or above this similarity are near-duplicates
#: (likely extractor scope noise), so Stage 2 adjudicates them instead of the
#: pre-filter killing them (design doc §6.2).
PREFILTER_SIMILARITY_FLOOR = 0.8

_QUALIFIER_RE = re.compile(
    r"\b(?:for|when|in|under)\s+([a-z][a-z0-9_\- ]{2,40}?)(?:[,.;]|$)", re.IGNORECASE
)


def _qualifiers(cognition: str) -> set[str]:
    return {m.group(1).strip().lower() for m in _QUALIFIER_RE.finditer(cognition)}


def _contradiction_prefilter(
    new_ecu: dict, existing_ecu: dict, similarity: float | None = None
) -> str:
    """§17.2 Stage 1 — computable pre-filter, no LLM call.

    Returns 'orthogonal' (definitely not a contradiction), 'no_overlap'
    (weak evidence; Stage 2 decides), or 'potential' (Stage 2 must decide).

    D40 fix (design doc §6.2): a scope-level difference alone no longer kills
    the pair. The ASSESSMENT.md §4.2 probe showed the extractor assigning
    different scopes to a direct, mutually-exclusive contradiction about the
    same file — the old rule silently dropped it before Stage 2. Now a
    different-scope pair escapes to Stage 2 when (a) grounding files overlap
    (they are about the same code; the scope difference may be extractor
    noise) or (b) semantic similarity exceeds PREFILTER_SIMILARITY_FLOOR
    (near-identical claims). ``similarity`` comes from the caller's
    find_similar step; ``None`` (unit-test callers) skips check (b).
    """
    # 1. Scope comparison — different levels usually means principle-vs-
    #    specific (Case 2), but grounding overlap or near-identical text
    #    unblocks Stage 2 adjudication (D40).
    if new_ecu["scope"]["level"] != existing_ecu["scope"]["level"]:
        files_new = set(new_ecu.get("grounding", {}).get("files") or [])
        files_old = set(existing_ecu.get("grounding", {}).get("files") or [])
        if files_new & files_old:
            return "potential"
        if similarity is not None and similarity > PREFILTER_SIMILARITY_FLOOR:
            return "potential"
        return "orthogonal"
    # 2. Grounding overlap — same files => more likely same subject.
    files_new = set(new_ecu.get("grounding", {}).get("files") or [])
    files_old = set(existing_ecu.get("grounding", {}).get("files") or [])
    if not (files_new & files_old):
        return "no_overlap"
    # 3. Condition/qualifier extraction — different explicit conditions on
    #    both sides => orthogonal.
    q_new, q_old = _qualifiers(new_ecu["cognition"]), _qualifiers(existing_ecu["cognition"])
    if q_new and q_old and q_new.isdisjoint(q_old):
        return "orthogonal"
    return "potential"


def _adjudicate_contradiction(new_ecu: dict, existing_ecu: dict, config=None) -> str:
    """§17.2 Stage 2 — LLM semantic judgment for potentially-contradicting
    pairs. Returns 'genuine_contradiction' | 'orthogonal' | 'unrelated'.
    Fail-safe: on any LLM/parse problem, treat as genuine (challenged is
    recoverable; a missed contradiction is not — §17.3 accumulates evidence).
    """
    user = _CONTRADICTION_USER_TEMPLATE.format(
        cognition_a=new_ecu["cognition"],
        scope_a=f"{new_ecu['scope']['level']} ({new_ecu['scope']['path']})",
        grounding_a=", ".join(new_ecu.get("grounding", {}).get("files") or []) or "none",
        confidence_a=new_ecu["confidence"],
        cognition_b=existing_ecu["cognition"],
        scope_b=f"{existing_ecu['scope']['level']} ({existing_ecu['scope']['path']})",
        grounding_b=", ".join(existing_ecu.get("grounding", {}).get("files") or []) or "none",
        confidence_b=existing_ecu["confidence"],
    )
    try:
        response = call_llm(user, system=_CONTRADICTION_SYSTEM,
                            temperature=0.0, config=config or get_config())
        payload = json.loads(extract_json(response))
        verdict = str(payload.get("verdict", "")).strip().lower()
    except (LLMError, json.JSONDecodeError) as exc:
        log.warning("contradiction adjudication failed (%s); treating as genuine", exc)
        return "genuine_contradiction"
    if verdict not in ("genuine_contradiction", "orthogonal", "unrelated"):
        log.warning("adjudication returned %r; treating as genuine", verdict)
        return "genuine_contradiction"
    return verdict


# ---------------------------------------------------------------------------
# full diffuser — §8.3
# ---------------------------------------------------------------------------

def diffuse_ecu(brain, ecu_id: str, embedding_model=None, config=None) -> DiffusionResult:
    """Integrate a reviewed ECU into the Canonical Brain (§8.3 steps 1-9).

    The ECU must already exist in the canonical ``ecus`` table (inserted by
    the review-gate flow). Confidence updates happen in place (§13.3, D2).
    """
    cfg = config or get_config()
    if embedding_model is None:
        from .embeddings import get_embedding_model
        embedding_model = get_embedding_model()

    new_ecu = brain.get_ecu(ecu_id)
    if new_ecu is None:
        raise DiffuserError(f"no canonical ECU with id {ecu_id}")
    if new_ecu["status"] not in RETRIEVABLE_STATUSES:
        raise DiffuserError(
            f"ECU {ecu_id} has status {new_ecu['status']!r}; only live ECUs diffuse"
        )
    return _diffuse_against_brain(brain, new_ecu, embedding_model, cfg)


def _expand_with_structural_proximity(
    brain,
    seed_rows: list[dict],
    exclude_ids=frozenset(),
    max_extra: int = STRUCTURAL_PROXIMITY_MAX_EXTRA,
) -> list[dict]:
    """§8.3 Step 2 method 2 (D54): follow existing edges from the semantic
    candidates to find structurally-related ECUs that might not have high
    direct semantic similarity but are connected in the network.

    ``seed_rows`` are ``find_similar`` rows (``{"id", "similarity", ...}``)
    AFTER the caller's exclude-filter. One hop only, bounded: at most
    ``max_extra`` new ids AND never more than ``MAX_CLASSIFY_BATCH`` total
    candidates — classification must stay a single batched call. Dead
    neighbours (not in RETRIEVABLE_STATUSES) and excluded ids are skipped.

    A structural candidate inherits the similarity of the seed it was
    reached through: it has no direct semantic score, and the handlers use
    similarity as edge weight / update magnitude. The classifier still
    decides — 'unrelated' structural extras are discarded like any other
    candidate (no harm done).
    """
    seeds = [r for r in seed_rows if r.get("id")]
    room = min(max_extra, MAX_CLASSIFY_BATCH - len(seeds))
    seen = {row["id"] for row in seeds} | set(exclude_ids)
    extra: list[dict] = []
    if room <= 0:
        return extra
    for row in seeds:
        if len(extra) >= room:
            break
        for edge in brain.get_edges_for(row["id"]):
            if len(extra) >= room:
                break
            other = (edge["target_id"]
                     if edge["source_id"] == row["id"] else edge["source_id"])
            if other == row["id"] or other in seen:
                continue
            target = brain.get_ecu(other)
            if target is None or target["status"] not in RETRIEVABLE_STATUSES:
                continue
            extra.append({"id": other, "similarity": row["similarity"],
                          "brain": "canonical"})
            seen.add(other)
    return extra


def _diffuse_against_brain(
    brain,
    source_ecu: dict,
    embedding_model=None,
    config=None,
    store_edges: bool = True,
    exclude_ids: tuple[str, ...] | frozenset[str] = frozenset(),
) -> DiffusionResult:
    """Core diffusion pipeline (§8.3 steps 2-9), shared entry point (D38).

    ``source_ecu`` is a full ECU dict — a real stored ECU (``store_edges=True``
    from ``diffuse_ecu``) or an ephemeral pseudo-ECU built from reconsolidation
    evidence (``store_edges=False``, ec/reconsolidation.py). Edges always run
    source→existing, so an ephemeral source cannot persist them:
    ``brain.add_edge`` requires both endpoints to exist (no dangling edges),
    and with no stored replacement ECU the §15.5 supersession condition 2 is
    unmet — classifier-proposed supersedes therefore downgrades to supports
    and post-update supersession checks are skipped.

    ``exclude_ids`` drops candidates before classification (the reconsolidation
    target pair is handled by the caller).
    """
    cfg = config or get_config()

    result = DiffusionResult(ecu_id=source_ecu["id"])

    # -- Step 2: find related ECUs (semantic similarity; canonical only) ----
    embedding = source_ecu.get("embedding")
    if embedding is None:
        embedding = embedding_model.encode_one(source_ecu["cognition"])
    similar = brain.find_similar(
        embedding,
        threshold=cfg.diffuser.relevance_threshold,
        top_k=MAX_CLASSIFY_BATCH,
        include_session=False,
    )
    skip = {source_ecu["id"], *exclude_ids}
    similar = [s for s in similar if s["id"] not in skip]

    # -- Step 2b: structural proximity (§8.3 Step 2 method 2, D54) ----------
    # One hop along existing canonical edges from the semantic candidates.
    # Structural extras go through the same classification as everything
    # else; 'unrelated' ones are discarded there.
    similar.extend(_expand_with_structural_proximity(brain, similar, skip))
    result.candidates = len(similar)
    if not similar:
        return result

    # -- Step 3: classify each relationship (batched, Rule A + Rule B) ------
    candidates = []
    for s in similar:
        existing = brain.get_ecu(s["id"])
        if existing is not None:
            candidates.append((existing, s["similarity"]))
    pairs = [(source_ecu, existing) for existing, _ in candidates]
    relationships = _classify_pairs(pairs, CLASSIFICATION_SYSTEM, _RELATIONSHIPS, cfg)

    # -- Steps 4-8: edges, Bayesian updates, contradictions, supersession ---
    for (existing, similarity), relationship in zip(candidates, relationships):
        handler = {
            "supports": _handle_support,
            "contradicts": _handle_contradiction,
            "supersedes": _handle_supersedes,
            "depends_on": _handle_depends_on,
        }.get(relationship)
        if handler is None:
            continue  # unrelated — discard (§8.3 step 3)
        handler(brain, source_ecu, existing, similarity, result, cfg,
                embedding_model, store_edges)

    return result


def _check_opposing_edges(brain, source_id, target_id, new_type):
    """§14.3 exception clause (D53): if ECU_A both supports AND contradicts
    ECU_B, the relationship needs decomposition into more atomic ECUs —
    flag it for human review at the gate.

    Called after every supports/contradicts edge creation. The pair may
    have accumulated its first edge in any earlier session; this check is
    what catches the moment a pair becomes self-contradictory.
    """
    if new_type not in ("supports", "contradicts"):
        return None
    opposing = "contradicts" if new_type == "supports" else "supports"
    if not brain.get_edges_between(source_id, target_id, edge_type=opposing):
        return None
    return {
        "kind": "opposing_edges",
        "source_id": source_id,
        "target_id": target_id,
        "has_supports": bool(
            new_type == "supports"
            or brain.get_edges_between(source_id, target_id, "supports")
        ),
        "has_contradicts": bool(
            new_type == "contradicts"
            or brain.get_edges_between(source_id, target_id, "contradicts")
        ),
        "message": (
            f"ECU {source_id[:8]} both supports and contradicts ECU "
            f"{target_id[:8]}. This relationship may need decomposition "
            "into more atomic ECUs."
        ),
    }


def _handle_support(brain, new_ecu, existing, similarity, result, cfg, _model,
                    store_edges=True):
    """§8.3 steps 4+5: supports edge + in-place Bayesian update (§15.2)."""
    new_c, delta = conf.support_update(existing["confidence"], new_ecu["confidence"],
                                       similarity, cfg)
    if store_edges:
        edge_id = brain.add_edge(new_ecu["id"], existing["id"], "supports",
                                 weight=similarity, confidence_delta=delta)
        result.edges_created.append({"edge_id": edge_id, "type": "supports",
                                     "target_id": existing["id"], "weight": similarity})
        opposing = _check_opposing_edges(brain, new_ecu["id"], existing["id"],
                                         "supports")
        if opposing:
            result.flags.append(opposing)
    brain.update_ecu_confidence(existing["id"], new_c)
    result.confidence_updates.append({"ecu_id": existing["id"], "delta": delta,
                                      "old": existing["confidence"], "new": new_c})
    # Step 8: propagation (support raises confidence, so this only matters
    # if the ECU was already below theta and stays there).
    result.challenged_by_propagation.extend(
        conf.propagate(existing["id"], new_c, brain, cfg)
    )
    _maybe_supersede_after_update(brain, new_ecu, existing, new_c, result, cfg,
                                  _model, store_edges)


def _handle_depends_on(brain, new_ecu, existing, similarity, result, cfg, _model,
                       store_edges=True):
    """Structural edge only — §11 defines no confidence update for
    depends_on; its effect flows through §15.7 propagation."""
    if not store_edges:
        return
    edge_id = brain.add_edge(new_ecu["id"], existing["id"], "depends_on",
                             weight=similarity, confidence_delta=0.0)
    result.edges_created.append({"edge_id": edge_id, "type": "depends_on",
                                 "target_id": existing["id"], "weight": similarity})


def _handle_contradiction(brain, new_ecu, existing, similarity, result, cfg, _model,
                          store_edges=True):
    """§8.3 step 6 + §12: two-stage classification, challenged status,
    user flag when both sides are strong. Never auto-resolves (§17.3)."""
    stage1 = _contradiction_prefilter(new_ecu, existing, similarity)
    if stage1 == "orthogonal":
        log.info("prefilter: %s vs %s orthogonal — no contradicts edge",
                 new_ecu["id"], existing["id"])
        return
    verdict = _adjudicate_contradiction(new_ecu, existing, cfg)
    if verdict != "genuine_contradiction":
        log.info("adjudication: %s vs %s -> %s", new_ecu["id"], existing["id"], verdict)
        return

    # Genuine contradiction (Case 1): update + edge + challenged (§8.3 step 6).
    new_c, delta = conf.contradiction_update(
        existing["confidence"], new_ecu["confidence"], similarity, cfg)
    if store_edges:
        edge_id = brain.add_edge(new_ecu["id"], existing["id"], "contradicts",
                                 weight=similarity, confidence_delta=delta)
        result.edges_created.append({"edge_id": edge_id, "type": "contradicts",
                                     "target_id": existing["id"],
                                     "weight": similarity})
        opposing = _check_opposing_edges(brain, new_ecu["id"], existing["id"],
                                         "contradicts")
        if opposing:
            result.flags.append(opposing)
    brain.update_ecu_confidence(existing["id"], new_c)
    if existing["status"] != "challenged":
        brain.update_ecu_status(existing["id"], "challenged")
    brain.update_ecu_metadata(existing["id"], last_challenged=_now_iso())
    result.confidence_updates.append({"ecu_id": existing["id"], "delta": delta,
                                      "old": existing["confidence"], "new": new_c})
    result.contradictions.append({"ecu_id": existing["id"], "case": "genuine",
                                  "new_confidence": new_c})

    # §17.4: flag when BOTH sides have high confidence — the system cannot
    # resolve this through confidence decay alone.
    theta_flag = cfg.confidence.theta_contradiction_flag
    if new_ecu["confidence"] > theta_flag and existing["confidence"] > theta_flag:
        result.flags.append({
            "kind": "high_stakes_contradiction",
            "ecu_ids": [new_ecu["id"], existing["id"]],
            "message": (
                "Two well-supported engineering beliefs contradict each other: "
                f"{new_ecu['id']} ({new_ecu['confidence']:.2f}) vs "
                f"{existing['id']} ({existing['confidence']:.2f})."
            ),
        })

    result.challenged_by_propagation.extend(
        conf.propagate(existing["id"], new_c, brain, cfg)
    )
    # §8.3 step 7: contradiction may have pushed the old ECU below theta.
    _maybe_supersede_after_update(brain, new_ecu, existing, new_c, result, cfg, _model)


def _handle_supersedes(brain, new_ecu, existing, similarity, result, cfg, model,
                       store_edges=True):
    """LLM-proposed supersession — honored ONLY when the §15.5 trigger
    passes (both conditions). Otherwise the new ECU is evidence for the
    standing belief, not a replacement: downgrade to supports (D8).

    With ``store_edges=False`` (ephemeral pseudo-ECU) there is no stored
    replacement ECU, so condition 2 can never hold — always downgrade."""
    if store_edges and conf.should_supersede(
            existing["confidence"], replacement_exists=True, config=cfg):
        _apply_supersession(brain, new_ecu, existing, result, cfg, model)
    else:
        log.info("supersedes proposed for %s but trigger fails (c=%.2f); "
                 "downgrading to supports", existing["id"], existing["confidence"])
        _handle_support(brain, new_ecu, existing, similarity, result, cfg,
                        model, store_edges)


def _maybe_supersede_after_update(brain, new_ecu, existing, new_confidence,
                                  result, cfg, model, store_edges=True):
    """§8.3 step 7: after a confidence update, check the supersession
    trigger — both conditions (§15.5). The new ECU is the replacement.

    Skipped for ephemeral sources: no stored replacement exists, so the
    trigger's second condition is unmet by definition."""
    if not store_edges or existing["status"] == "superseded":
        return
    if conf.should_supersede(new_confidence, replacement_exists=True, config=cfg):
        _apply_supersession(brain, new_ecu, existing, result, cfg, model)


def _apply_supersession(brain, new_ecu, existing, result, cfg, model):
    """§15.4: supersedes edge + old status superseded + confidence frozen.

    The new ECU's confidence is its own — NOT inherited from the old one.
    """
    stype = conf.supersession_type(
        existing["cognition"], new_ecu["cognition"], embedding_model=model,
        old_embedding=existing.get("embedding"), new_embedding=new_ecu.get("embedding"),
    )
    edge_id = brain.add_edge(new_ecu["id"], existing["id"], "supersedes",
                             weight=1.0, confidence_delta=0.0,
                             supersession_type=stype)
    brain.update_ecu_status(existing["id"], "superseded")  # confidence frozen
    result.edges_created.append({"edge_id": edge_id, "type": "supersedes",
                                 "target_id": existing["id"], "weight": 1.0})
    result.supersessions.append({"old_ecu_id": existing["id"],
                                 "new_ecu_id": new_ecu["id"],
                                 "supersession_type": stype})


# ---------------------------------------------------------------------------
# lightweight diffuser — §4.4 (Session Brain)
# ---------------------------------------------------------------------------

def diffuse_session_ecu(
    brain,
    session_id: str,
    ecu_id: str,
    embedding_model=None,
    config=None,
) -> LightDiffusionResult:
    """Fast, approximate integration of a new session ECU (§4.4).

    - 3-way classification (supports/contradicts/unrelated), one batched call
    - similarity threshold 0.7, session targets bumped c' = c ± alpha_light x sim
    - canonical targets: pending_updates only — NEVER a Canonical write (§6.7)
    - no supersession, no propagation, no challenged status, no depends_on
    """
    cfg = config or get_config()
    if embedding_model is None:
        from .embeddings import get_embedding_model
        embedding_model = get_embedding_model()

    new_ecu = brain.get_session_ecu(ecu_id)
    if new_ecu is None:
        raise DiffuserError(f"no session ECU with id {ecu_id}")
    if new_ecu["session_id"] != session_id:
        raise DiffuserError(
            f"session ECU {ecu_id} belongs to session {new_ecu['session_id']}, "
            f"not {session_id}"
        )

    result = LightDiffusionResult(ecu_id=ecu_id, session_id=session_id)

    embedding = new_ecu.get("embedding")
    if embedding is None:
        embedding = embedding_model.encode_one(new_ecu["cognition"])
    threshold = cfg.lightweight_diffusion.similarity_threshold
    alpha = cfg.lightweight_diffusion.alpha_light

    similar = brain.find_similar(
        embedding,
        threshold=threshold,
        top_k=MAX_CLASSIFY_BATCH,
        include_session=True,
        session_id=session_id,
    )
    similar = [s for s in similar if s["id"] != ecu_id]
    result.candidates = len(similar)
    if not similar:
        return result

    candidates = []
    for s in similar:
        if s["brain"] == "session":
            target = brain.get_session_ecu(s["id"])
        else:
            target = brain.get_ecu(s["id"])
        if target is not None:
            candidates.append((target, s["similarity"], s["brain"]))
    pairs = [(new_ecu, target) for target, _, _ in candidates]
    relationships = _classify_pairs(pairs, _LIGHTWEIGHT_SYSTEM, _LIGHT_RELATIONSHIPS, cfg)

    for (target, similarity, target_brain), relationship in zip(candidates, relationships):
        if relationship == "unrelated":
            continue
        sign = 1.0 if relationship == "supports" else -1.0
        if target_brain == "session":
            # Edge + simple bump on the TARGET session ECU (§4.4 steps 4-5).
            edge_id = brain.add_session_edge(
                session_id, ecu_id, target["id"], relationship,
                weight=similarity, target_type="session_ecu",
            )
            result.session_edges.append({"edge_id": edge_id, "type": relationship,
                                         "target_id": target["id"],
                                         "weight": similarity})
            bumped = min(max(target["confidence"] + sign * alpha * similarity,
                             cfg.confidence.prior_floor),
                         cfg.confidence.prior_cap)
            brain.update_session_ecu_confidence(target["id"], bumped)
            result.confidence_updates.append({
                "ecu_id": target["id"], "brain": "session",
                "old": target["confidence"], "new": bumped,
            })
        else:
            # Cross-brain: record intent for the review gate (§6.7, §28.5).
            # The pending delta is the lightweight probability-space bump;
            # the full diffuser recomputes exact log-odds deltas on apply.
            delta = sign * alpha * similarity
            update_id = brain.add_pending_update(
                canonical_ecu_id=target["id"],
                session_ecu_id=ecu_id,
                session_id=session_id,
                relationship_type=relationship,
                proposed_confidence_delta=delta,
            )
            brain.update_ecu_metadata(target["id"], has_pending_updates=True)
            result.pending_updates.append({
                "update_id": update_id, "canonical_ecu_id": target["id"],
                "relationship": relationship, "delta": delta,
            })
    return result
