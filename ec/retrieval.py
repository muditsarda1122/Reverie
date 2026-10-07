"""Demand-driven retrieval (Spec §8/§11.1–11.11) — the ec_query pipeline.

§11.3: retrieval is PURE demand-driven — this module acts only when the
agent explicitly queries (the ``ec_query`` path). Nothing here injects
cognition proactively; that structural choice is anti-anchoring Defence 1
(§11.6). Mode controls WHAT comes back, never WHEN (§11.9).

Pipeline (mirrors the verified Experiment-2 reference implementation,
``test_retrieval_ranking_zen_v2.py``):

  1. resolve mode (explicit or ``detect_mode``, §11.9)
  2. embed query → ``find_similar`` over BOTH brains (§9.4)
  3. optional explicit scope restriction (§16.3 ``scope`` param)
  3b. §11.11 scope-hierarchy filter (D50): UP from the session's repo
      anchor, never down/sideways; unfiltered fallback when it empties
  4. relevance gate 0.3 — hard filter (§11.5, anti-anchoring fix);
     fallback: top-K by rank when nothing passes (§11.5)
   5. four-factor ranking (§11.5): 0.6 relevance / 0.15 confidence /
      0.10 normalized activation / 0.15 network richness (edge_count_cap 10);
      factor 2 uses EFFECTIVE confidence for canonical ECUs — lazy
      scope-dependent decay, no writes (design doc §2, D34)
  6. × session/canonical trust weight 0.8/1.0 (§11.5)
  7. × scope proximity multiplier (§11.11 — multiplier, never a filter)
  8. × 0.5 for open_question ECUs (§15 open_question_retrieval_weight);
     challenged ECUs are flagged, never deprioritised (§11.5, D10)
  9. mode-aware reweighting bonuses (§11.9, D11)
 10. cognitive grouping: depth-1 edge-type-aware traversal (§11.4),
     within-group dedup by slot priority (§11.5)
 11. token-budget packing (§11.7): whole group → core-only (truncated) → stop
 12. cross-group dedup: "see Group N for full context" (§11.5)
 13. spreading activation on the retrieved cores (§11.10/§12)
 14. §11.8 formatted output + §16.3 structured result

The brain is treated read-only here: retrieval never mutates ECUs.
(last_retrieved / retrieval_count metadata is a Phase-4/Maintainer concern.)
"""

from __future__ import annotations

import logging
from pathlib import PurePath

from .activation import ActivationState
from .confidence import ecu_effective_confidence
from .config import MODES, RETRIEVABLE_STATUSES, SCOPE_LEVELS, get_config
from .embeddings import get_embedding_model
from .mode_detection import detect_mode, get_mode_params

log = logging.getLogger("ec.retrieval")

# ---------------------------------------------------------------------------
# Output text (§11.5 flags, §11.6 Defence 2 framing, §17.4/§17.5 status flags)
# ---------------------------------------------------------------------------

FRAMING_NOTE = (
    "This is past engineering understanding. "
    "Verify against current code before acting."
)
TRUNCATED_NOTE = "Context truncated due to token budget. Only core ECU included."
LOW_CONFIDENCE_FLAG = (
    "⚠️ Low confidence — this cognition is uncertain. "
    "Treat as a hypothesis, not a conclusion."
)
VERY_LOW_CONFIDENCE_FLAG = (
    "⚠️ Very low confidence — this cognition is highly uncertain. May be noise."
)
CHALLENGED_FLAG = (
    "⚠️ This cognition is challenged — there is contradicting evidence."
)
OPEN_QUESTION_FLAG = "⚠️ Open question — unresolved since {date}."

# Within-group slot priority (§11.5 Rule 1): contradictions first — the agent
# most needs to know about conflicting evidence.
_SLOT_PRIORITY = {"contradicts": 0, "depends_on": 1, "supports": 2, "supersedes": 3}
_SLOT_NAMES = {
    "supports": "supporting_evidence",
    "contradicts": "contradictions",
    "depends_on": "dependencies",
    "supersedes": "superseded_by",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def confidence_label(confidence: float) -> str:
    """§16.3: high >0.7, medium 0.3–0.7, low <0.3."""
    if confidence > 0.7:
        return "high"
    if confidence >= 0.3:
        return "medium"
    return "low"


def estimate_tokens(ecu: dict) -> int:
    """Reference heuristic: ~4 chars/token over cognition + grounding, min 50."""
    text = ecu.get("cognition", "")
    grounding = ecu.get("grounding") or {}
    for f in grounding.get("files", []):
        text += f
    for s in grounding.get("symbols", []):
        text += s
    return max(50, len(text) // 4)


def _fetch_ecu(brain, ecu_id: str, brain_hint: str | None = None) -> dict | None:
    """Fetch an ECU from either brain, tagged with ``brain``."""
    if brain_hint != "session":
        ecu = brain.get_ecu(ecu_id)
        if ecu is not None:
            ecu["brain"] = "canonical"
            return ecu
    if brain_hint != "canonical":
        ecu = brain.get_session_ecu(ecu_id)
        if ecu is not None:
            ecu["brain"] = "session"
            return ecu
    return None


def _has_contradicts_edge(brain, ecu_id: str, session_id: str | None) -> bool:
    return any(
        nb["type"] == "contradicts"
        for nb in brain.get_neighbourhood(ecu_id, session_id=session_id)
    )


# ---------------------------------------------------------------------------
# §11.11 scope hierarchy — search UP, never down or sideways (D50)
# ---------------------------------------------------------------------------

#: Universal levels (§11.11 "why up but not sideways"): general principles
#: apply to any working context.
_UNIVERSAL_SCOPES = ("engineering", "domain")


def _scope_of(ecu: dict) -> tuple[str, str]:
    scope = ecu.get("scope") or {}
    return scope.get("level") or "", scope.get("path") or ""


def _path_is_unknown(ecu: dict) -> bool:
    """No usable path information: the path is empty or it is the
    extractor's default (``validate_ecu`` stores the level name when the
    LLM omits a path). Unknown paths never over-filter."""
    level, path = _scope_of(ecu)
    return not path or path == level


def _is_scope_prefix(candidate_path: str, working_path: str) -> bool:
    """True if ``candidate_path`` identifies an ancestor-or-equal scope of
    ``working_path``. Empty paths are universal.

    Convention bridge: session anchors are filesystem paths while ECU
    scope paths are hierarchical labels ("repo:api-server > module:auth"),
    so a label that embeds the anchor directory's name also counts as the
    same context — without this, same-repo ECUs could never match their
    own session and every query would degrade to the fallback.
    """
    if not candidate_path or not working_path:
        return True
    if working_path.startswith(candidate_path):
        return True
    token = PurePath(working_path).name.lower()
    return bool(token) and token in candidate_path.lower()


def _filter_by_scope_hierarchy(candidates: list[dict],
                               working_scope_level: str,
                               working_scope_path: str) -> list[dict]:
    """§11.11 (D50): search UP the hierarchy, never down or sideways.

    ``candidates`` are retrieval entries (``{"ecu", "similarity", ...}``).
    An ECU is kept when:
    - its level is MORE GENERAL than the working scope (UP): engineering/
      domain unconditionally ("universal principles always in scope");
      intermediate levels when their path is compatible with the anchor;
    - its level EQUALS the working scope and its path identifies the same
      context (exact match or the convention bridge above);
    - its level is unknown to the hierarchy (don't over-filter).

    ECUs at a MORE SPECIFIC level than the working scope are filtered out
    (down), as are same-level ECUs from a different context (sideways).
    The caller falls back to the unfiltered set if this empties the
    candidate list — an empty result is worse than a broad one.

    Note on ordering: SCOPE_LEVELS runs general → specific, so "UP" means
    a SMALLER index. (The design-doc sketch compared indices in the
    opposite direction of its own array layout; this follows the stated
    semantics and the design doc §16.2 test matrix.)
    """
    if working_scope_level not in SCOPE_LEVELS:
        return list(candidates)          # unknown working level — keep all
    working_idx = SCOPE_LEVELS.index(working_scope_level)

    kept = []
    for cand in candidates:
        ecu = cand["ecu"]
        level, path = _scope_of(ecu)
        if level not in SCOPE_LEVELS:
            kept.append(cand)            # unknown level — keep
            continue
        idx = SCOPE_LEVELS.index(level)
        if idx < working_idx:
            # UP the hierarchy — more general than where we work.
            if level in _UNIVERSAL_SCOPES:
                kept.append(cand)
            elif _path_is_unknown(ecu) or _is_scope_prefix(
                    path, working_scope_path):
                kept.append(cand)
        elif idx == working_idx:
            # Same level — sideways unless it is our own context.
            if _path_is_unknown(ecu) or _is_scope_prefix(
                    path, working_scope_path):
                kept.append(cand)
        # else: DOWN — more specific than the working scope; filtered.
    return kept


def _working_scope_from_session(brain, session_id: str | None):
    """D50 anchor: the session's repo at 'repo' scope, or None when there
    is no session to anchor to (filtering skipped entirely — with no
    anchor there is no UP/DOWN to speak of)."""
    if not session_id:
        return None
    session = brain.get_session(session_id)
    if not session:
        return None
    return "repo", session.get("repo_path") or ""


# ---------------------------------------------------------------------------
# Ranking (§11.5, §11.9, §11.11)
# ---------------------------------------------------------------------------

def _rank_candidates(
    brain,
    candidates: list[dict],
    *,
    activation: ActivationState | None,
    mode_params,
    session_id: str | None,
    config,
    now=None,
) -> list[dict]:
    """Four-factor ranking + trust/scope/open-question multipliers + mode bonus.

    ``candidates`` are ``{ecu, similarity, brain}`` dicts. Returns entries
    sorted by rank, descending, each with a factor-by-factor ``breakdown``.

    Factor 2 uses the EFFECTIVE confidence for canonical ECUs (D34: lazy
    scope-dependent decay from metadata.last_reinforced / created_at).
    Session ECUs never decay (ephemeral — design doc §2.6) and pass their
    stored confidence straight through. Display/flags keep using the stored
    value: the audit trail shows what the last event set; decay affects
    ranking only.
    """
    cfg = config
    norm_act = activation.normalized_scores() if activation is not None else {}
    prioritize = list(mode_params.get("prioritize", []))
    bias = mode_params.get("bias")
    scored = []

    for cand in candidates:
        ecu = cand["ecu"]
        sim = cand["similarity"]
        confidence = ecu["confidence"]          # stored — display + flags
        if cand["brain"] == "canonical":
            rank_confidence = ecu_effective_confidence(ecu, now=now, config=cfg)
        else:
            rank_confidence = confidence
        act = norm_act.get(ecu["id"], 0.0)
        n_edges = brain.edge_count(ecu["id"]) + sum(
            1
            for e in brain.get_session_edges_for(ecu["id"])
            if session_id is None or e["session_id"] == session_id
        )
        richness = min(n_edges, cfg.ranking.edge_count_cap) / cfg.ranking.edge_count_cap

        rank = (
            cfg.ranking.w_relevance * sim
            + cfg.ranking.w_confidence * rank_confidence
            + cfg.ranking.w_activation * act
            + cfg.ranking.w_network * richness
        )
        trust = (
            cfg.retrieval.session_brain_trust_weight
            if cand["brain"] == "session"
            else cfg.retrieval.canonical_brain_trust_weight
        )
        rank *= trust
        proximity = cfg.retrieval.scope_proximity.get(ecu["scope"]["level"], 0.1)
        rank *= proximity  # §11.11: multiplier, never a filter
        oq_weight = 1.0
        if ecu["status"] == "open_question":
            oq_weight = cfg.contradiction.open_question_retrieval_weight
            rank *= oq_weight
        # challenged: NO ranking penalty (§11.5 — flagged, not deprioritised; D10)

        # Mode-aware reweighting (§11.9, magnitudes from the reference — D11)
        bonus = 0.0
        if "contradictions" in prioritize:
            # edge-type slot (debugging): challenged ECUs / ECUs with
            # contradicts edges — known pitfalls and failed approaches.
            if ecu["status"] == "challenged" or _has_contradicts_edge(
                brain, ecu["id"], session_id
            ):
                bonus += cfg.retrieval.mode_prioritize_bonus
        elif ecu["conclusion_type"] in prioritize:
            bonus += cfg.retrieval.mode_prioritize_bonus
        if bias and bias != "none" and ecu["provenance"]["source_type"] == bias:
            bonus += cfg.retrieval.mode_bias_bonus
        rank += bonus

        scored.append({
            "ecu": ecu,
            "similarity": sim,
            "brain": cand["brain"],
            "rank": rank,
            "bonus": bonus,
            "breakdown": {
                "semantic_sim": sim,
                "confidence": confidence,
                "rank_confidence": rank_confidence,
                "normalized_activation": act,
                "network_richness": richness,
                "trust_weight": trust,
                "scope_proximity": proximity,
                "open_question_weight": oq_weight,
                "mode_bonus": bonus,
                "rank": rank,
            },
        })

    scored.sort(key=lambda e: e["rank"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Cognitive grouping (§11.4, §11.5)
# ---------------------------------------------------------------------------

def _neighbour_entry(ecu: dict, edge: dict) -> dict:
    return {
        "id": ecu["id"],
        "cognition": ecu["cognition"],
        "confidence": ecu["confidence"],
        "scope_level": ecu["scope"]["level"],
        "brain": ecu["brain"],
        "status": ecu["status"],
        "edge_type": edge["type"],
        "edge_weight": edge["weight"],
    }


def _build_group(brain, core_entry: dict, *, depth: int, session_id: str | None) -> dict:
    """Core ECU + depth-1 neighbourhood as one cognitive group (§11.4/§11.5).

    All four edge types are traversed in both directions, across brains.
    Neighbours are included regardless of status (a superseded ECU reached
    via a supersedes edge is exactly the context §11.4 asks for).
    Within-group dedup (§11.5 Rule 1): each neighbour appears once, in the
    highest-priority slot (contradicts > depends_on > supports > supersedes).
    """
    core = core_entry["ecu"]
    group = {
        "core": core_entry,
        "supporting_evidence": [],
        "contradictions": [],
        "dependencies": [],
        "superseded_by": [],
        "token_cost": estimate_tokens(core),
        "truncated": False,
    }
    if depth < 1:
        return group  # e.g. debugging mode — just the matches (§11.9)

    neighbours = []
    for edge in brain.get_neighbourhood(core["id"], session_id=session_id):
        n_ecu = _fetch_ecu(brain, edge["other_id"])
        if n_ecu is not None:
            neighbours.append((edge, n_ecu))
    neighbours.sort(key=lambda t: _SLOT_PRIORITY.get(t[0]["type"], 99))

    added = {core["id"]}  # never slot the core into its own group
    for edge, n_ecu in neighbours:
        if n_ecu["id"] in added:
            continue  # already included via another edge (§11.5 Rule 1)
        added.add(n_ecu["id"])
        group[_SLOT_NAMES[edge["type"]]].append(_neighbour_entry(n_ecu, edge))
        group["token_cost"] += estimate_tokens(n_ecu)
    return group


def _pack_groups(brain, scored: list[dict], *, depth: int, max_tokens: int,
                 session_id: str | None) -> list[dict]:
    """§11.5 budgeting: whole group → core-only (truncated) → stop (§11.7)."""
    remaining = max_tokens
    selected = []
    for entry in scored:
        group = _build_group(brain, entry, depth=depth, session_id=session_id)
        if group["token_cost"] <= remaining:
            selected.append(group)
            remaining -= group["token_cost"]
            continue
        core_cost = estimate_tokens(entry["ecu"])
        if core_cost <= remaining:
            group["truncated"] = True
            group["token_cost"] = core_cost
            for slot in _SLOT_NAMES.values():
                group[slot] = []
            selected.append(group)
            remaining -= core_cost
            continue
        break  # even the core doesn't fit — the budget is a hard constraint
    return selected


def _apply_cross_group_dedup(selected: list[dict]) -> int:
    """§11.5 Rule 2: a core ECU of group N shows as 'see Group N' elsewhere."""
    core_to_group = {
        g["core"]["ecu"]["id"]: i + 1 for i, g in enumerate(selected)
    }
    refs = 0
    for i, group in enumerate(selected):
        for slot in _SLOT_NAMES.values():
            for item in group[slot]:
                ref = core_to_group.get(item["id"])
                if ref is not None and ref != i + 1:
                    item["see_group"] = ref
                    refs += 1
    return refs


# ---------------------------------------------------------------------------
# Formatting (§11.8 + reference layout)
# ---------------------------------------------------------------------------

def _confidence_flag(confidence: float, config) -> str:
    if confidence < config.retrieval.confidence_very_low_threshold:
        return VERY_LOW_CONFIDENCE_FLAG
    if confidence < config.retrieval.confidence_flag_threshold:
        return LOW_CONFIDENCE_FLAG
    return ""


def _status_flags(group: dict) -> list[str]:
    """§17.4 challenged flag (with pointer), §17.5 open_question flag."""
    core = group["core"]["ecu"]
    flags = []
    if core["status"] == "challenged":
        flag = CHALLENGED_FLAG
        if group["contradictions"]:
            flag += f" See: [{group['contradictions'][0]['id']}]."
        flags.append(flag)
    elif core["status"] == "open_question":
        since = (core.get("metadata") or {}).get("competing_since") or (
            core["provenance"].get("created_at") or ""
        )
        flags.append(OPEN_QUESTION_FLAG.format(date=since[:10] or "unknown"))
    return flags


def _format_neighbour(item: dict) -> str:
    if "see_group" in item:
        return f"  - [see Group {item['see_group']} for full context]"
    return (
        f'  - "{item["cognition"]}" '
        f'(confidence: {item["confidence"]}, scope: {item["scope_level"]})'
    )


def _format_group(group: dict, number: int, config) -> str:
    core = group["core"]["ecu"]
    rank = group["core"]["rank"]
    brain = group["core"]["brain"]
    reviewed = "reviewed" if brain == "canonical" else "unreviewed"
    conf = core["confidence"]

    lines = [f"--- Group {number} (rank: {rank:.4f}) ---"]
    lines.append(f'CONCLUSION: "{core["cognition"]}"')
    conf_flag = _confidence_flag(conf, config)
    lines.append(
        f"CONFIDENCE: {conf} ({confidence_label(conf)}, {brain}, {reviewed})"
        + (f" {conf_flag}" if conf_flag else "")
    )
    lines.extend(_status_flags(group))
    lines.append(f'SCOPE: {core["scope"]["level"]} ({core["scope"]["path"]})')
    lines.append(f'TYPE: {core["conclusion_type"]}')
    lines.append(f'SOURCE: {core["provenance"]["source_type"]}')

    grounding = core.get("grounding") or {}
    if grounding.get("files"):
        lines.append(f'GROUNDING: {", ".join(grounding["files"])}')
    if grounding.get("symbols"):
        lines.append(f'SYMBOLS: {", ".join(grounding["symbols"])}')

    sections = (
        ("SUPPORTING EVIDENCE:", "supporting_evidence"),
        ("CONTRADICTIONS:", "contradictions"),
        ("DEPENDS ON:", "dependencies"),
        ("SUPERSEDED BY:", "superseded_by"),
    )
    for title, slot in sections:
        if group[slot]:
            lines.append(title)
            lines.extend(_format_neighbour(item) for item in group[slot])

    if group["truncated"]:
        lines.append(f"NOTE: {TRUNCATED_NOTE}")
    lines.append(f"NOTE: {FRAMING_NOTE}")  # §11.6 Defence 2 — every group
    return "\n".join(lines)


def _format_output(mode: str, max_tokens: int, groups: list[dict], *,
                   fallback: bool, cross_group_refs: int, config) -> str:
    lines = ["=== Engineering Cognition ===", ""]
    lines.append(
        f"[Mode: {mode}] [Budget: {max_tokens} tokens] "
        f"[{len(groups)} cognitive groups retrieved]"
    )
    if fallback:
        lines.append(
            "[Fallback: no ECUs passed the relevance gate — "
            "returning top-ranked results anyway]"
        )
    if cross_group_refs:
        lines.append(f"[Cross-group dedup: {cross_group_refs} references replaced]")
    lines.append("")
    for i, group in enumerate(groups):
        lines.append(_format_group(group, i + 1, config))
        lines.append("")
    lines.append("=== End Engineering Cognition ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Structured result (§16.3 ec_query return shape)
# ---------------------------------------------------------------------------

def _structured_group(group: dict) -> dict:
    core = group["core"]["ecu"]
    return {
        "rank": round(group["core"]["rank"], 4),
        "core_ecu": {
            "id": core["id"],
            "cognition": core["cognition"],
            "conclusion_type": core["conclusion_type"],
            "confidence": core["confidence"],
            "confidence_label": confidence_label(core["confidence"]),
            "status": core["status"],
            "scope_level": core["scope"]["level"],
            "scope_path": core["scope"]["path"],
            "grounding": core.get("grounding") or {},
            "source_type": core["provenance"]["source_type"],
            "origin_agent": core["provenance"].get("origin_agent"),
            "created_at": core["provenance"].get("created_at"),
            "brain": group["core"]["brain"],
        },
        "supporting_evidence": group["supporting_evidence"],
        "contradictions": group["contradictions"],
        "dependencies": group["dependencies"],
        "superseded_by": group["superseded_by"],
        "truncated": group["truncated"],
        "framing_note": FRAMING_NOTE,
    }


# ---------------------------------------------------------------------------
# Public entry point — the ec_query pipeline (§11.3: call-driven only)
# ---------------------------------------------------------------------------

def retrieve(
    brain,
    query_text: str,
    *,
    mode: str | None = None,
    scope: str | None = None,
    session_id: str | None = None,
    activation: ActivationState | None = None,
    config=None,
    embedding_model=None,
    use_llm_mode_detection: bool = True,
    now=None,
) -> dict:
    """Retrieve cognition relevant to a query, as cognitive groups.

    Demand-driven only (§11.3): this runs exclusively when the agent calls
    it. ``mode`` overrides detection (§11.9); ``scope`` hard-restricts to one
    scope level (§16.3 — distinct from the §11.11 proximity multiplier);
    ``activation`` is the session's ActivationState (ranking factor 3 and
    spreading-activation target); ``now`` injects the decay clock for the
    canonical ranking factor (D34 — tests). Returns the §16.3-shaped result
    dict with the §11.8 formatted text under ``formatted``.
    """
    cfg = config or get_config()
    if not query_text or not query_text.strip():
        raise ValueError("query_text must be a non-empty string")
    if scope is not None and scope not in SCOPE_LEVELS:
        raise ValueError(f"invalid scope {scope!r}; expected one of {SCOPE_LEVELS}")

    # 1. mode (§11.9 — controls WHAT comes back, not when)
    mode_detected = mode is None
    if mode is None:
        mode = detect_mode(query_text, use_llm=use_llm_mode_detection, config=cfg)
    elif mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    params = get_mode_params(mode, cfg)

    # 2. embed query → both-brain similarity search (§9.4)
    model = embedding_model or get_embedding_model()
    query_embedding = model.encode_one(query_text)

    # quiet-period fade before scores are read (§11.10/§12.4)
    if activation is not None:
        activation.apply_time_decay()

    candidates = brain.find_similar(
        query_embedding,
        # Full set (cosine >= -1 always): the relevance gate and the
        # fallback both live here in the pipeline — the fallback ranks the
        # full set when nothing passes the gate (§11.5).
        threshold=-1.0,
        include_session=True,
        session_id=session_id,
        statuses=RETRIEVABLE_STATUSES,
    )
    fetched = []
    for cand in candidates:
        ecu = _fetch_ecu(brain, cand["id"], brain_hint=cand["brain"])
        if ecu is not None:
            fetched.append({"ecu": ecu, "similarity": cand["similarity"],
                            "brain": cand["brain"]})

    # 3. explicit scope restriction (§16.3)
    if scope is not None:
        fetched = [c for c in fetched if c["ecu"]["scope"]["level"] == scope]

    # 3b. §11.11 search-UP-the-hierarchy filter (D50): anchored at the
    # session's repo; an explicit scope= narrows the anchor further.
    # Fallback to the unfiltered set when the filter empties it — empty
    # results are worse than broad ones (the relevance-gate principle).
    anchor = _working_scope_from_session(brain, session_id)
    if anchor is not None:
        level, path = anchor
        if scope is not None:
            level = scope
        scoped = _filter_by_scope_hierarchy(fetched, level, path)
        if scoped:
            fetched = scoped
        else:
            log.info("scope-hierarchy filter removed all candidates; "
                     "keeping unfiltered set")

    # 4. relevance gate (§11.5) + fallback
    gate = cfg.ranking.relevance_gate_threshold
    gated = [c for c in fetched if c["similarity"] >= gate]
    fallback = bool(fetched) and not gated
    pool = gated if gated else fetched

    # 5–9. ranking
    scored = _rank_candidates(
        brain, pool,
        activation=activation, mode_params=params,
        session_id=session_id, config=cfg, now=now,
    )
    if fallback:
        scored = scored[: cfg.retrieval.fallback_top_k]

    # 10–11. grouping + packing
    selected = _pack_groups(
        brain, scored,
        depth=params["depth"], max_tokens=params["max_tokens"],
        session_id=session_id,
    )

    # 12. cross-group dedup
    cross_refs = _apply_cross_group_dedup(selected)

    # 13. spreading activation on retrieved cores (§11.10)
    if activation is not None and selected:
        activation.spread(
            brain, [g["core"]["ecu"]["id"] for g in selected], mode=mode
        )

    # 14. result
    tokens_used = sum(g["token_cost"] for g in selected)
    warnings = []
    low_n = sum(
        1 for g in selected
        if g["core"]["ecu"]["confidence"] < cfg.retrieval.confidence_flag_threshold
    )
    if low_n:
        warnings.append(
            f"{low_n} ECU{'s' if low_n != 1 else ''} "
            f"{'have' if low_n != 1 else 'has'} low confidence"
        )
    challenged_n = sum(
        1 for g in selected if g["core"]["ecu"]["status"] == "challenged"
    )
    if challenged_n:
        warnings.append(
            f"{challenged_n} ECU{'s' if challenged_n != 1 else ''} "
            f"{'are' if challenged_n != 1 else 'is'} challenged"
        )
    if fallback:
        warnings.append(
            f"No ECUs passed the relevance gate ({gate}) — "
            "returned top-ranked results anyway (fallback)"
        )

    brains = {g["core"]["brain"] for g in selected}
    brain_source = "both" if len(brains) != 1 else brains.pop()
    if selected:
        message = (
            f"Retrieved {len(selected)} cognitive "
            f"group{'s' if len(selected) != 1 else ''} "
            f"for mode '{mode}' ({tokens_used}/{params['max_tokens']} tokens)."
        )
    else:
        message = "No relevant engineering cognition found."

    formatted = _format_output(
        mode, params["max_tokens"], selected,
        fallback=fallback, cross_group_refs=cross_refs, config=cfg,
    )

    return {
        "status": "ok",
        "mode": mode,
        "mode_detected": mode_detected,
        "budget": params["max_tokens"],
        "budget_used": tokens_used,
        "groups_retrieved": len(selected),
        "groups": [_structured_group(g) for g in selected],
        "warnings": warnings,
        "brain_source": brain_source,
        "message": message,
        "formatted": formatted,
        "fallback": fallback,
        "filtered_out": len(fetched) - len(gated) if not fallback else len(fetched),
        "cross_group_dedup_count": cross_refs,
    }
