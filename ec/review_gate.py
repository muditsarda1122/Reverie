"""Human Review Gate (Spec §5/§7, §28.6/§28.7) — the only path into the
Canonical Brain (§6.7: no bypass, no exceptions).

Flow (§28.6 /ec-stop):
  1. candidate session ECUs (review_status pending|skipped) are grouped by
     topic for batch review (§7.4 — scope-path grouping, D18)
  2. accept → promote into the Canonical Brain (same id, D21; optional edit
     per §7.3, D19) and run the FULL Diffuser (§8.3) — edges, Bayesian
     updates, contradiction handling, supersession, propagation
  3. reject → removed from the Session Brain (§7.5)
  4. skip → remains with review_status 'skipped', available next review
  5. pending_updates (§28.6): accepted sources → applied with EXACT deltas
     recomputed via full-diffuser math — never double-applied when the
     diffuser already edged the pair (D16); rejected sources → discarded;
     skipped → stay pending. has_pending_updates cleared when none remain.
  6. contradiction surfacing (§17.4): both->0.7 flags (from the diffuser),
     engineering/domain-scope contradictions, depends_on-chain risk notices
  7. competing hypotheses (§17.5, D20): Case-3 contradictions get
     competing_since; challenged ECUs past their scope's persistence limit
     are parked as open_question (confidence frozen) with a notification
  8. cleanup (session edges + rows for accepted/rejected), session marked
     closed, activation rows dropped (§12.2), maintenance_log entry
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from . import confidence as conf
from . import diffuser
from .activation import _parse_iso
from .brain import Brain, _now_iso
from .config import get_config
from .diffuser import DiffuserError

log = logging.getLogger("ec.review_gate")


class ReviewGateError(RuntimeError):
    """Review-gate failure with actionable guidance (§28.4 style)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# result containers
# ---------------------------------------------------------------------------

@dataclass
class ReviewGroup:
    """One topic group of candidate ECUs (§7.4 batch review)."""

    label: str
    ecus: list[dict] = field(default_factory=list)


@dataclass
class ReviewPlan:
    session_id: str = ""
    groups: list[ReviewGroup] = field(default_factory=list)
    total: int = 0


@dataclass
class ReviewResult:
    """What the review gate did (§28.6 cleanup + §28.7 summary)."""

    session_id: str = ""
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    diffusions: list = field(default_factory=list)      # DiffusionResult list
    flags: list[dict] = field(default_factory=list)      # §17.4 user flags
    notifications: list[dict] = field(default_factory=list)  # §17.4/§17.5
    pending_applied: int = 0
    pending_discarded: int = 0
    errors: list[str] = field(default_factory=list)
    undiffused: list[str] = field(default_factory=list)  # promoted, not diffused
    canonical_total: int = 0
    message: str = ""


# ---------------------------------------------------------------------------
# grouping (§7.4) — D36 strategies
# ---------------------------------------------------------------------------

def _scope_groups(ecus: list[dict]) -> list[ReviewGroup]:
    """Scope-path grouping (D18): deterministic, works at any brain size."""
    by_topic: dict[str, list[dict]] = {}
    for ecu in ecus:
        by_topic.setdefault(ecu["scope"]["path"], []).append(ecu)
    return [
        ReviewGroup(
            label=topic,
            ecus=sorted(group, key=lambda e: e["confidence"], reverse=True),
        )
        for topic, group in sorted(by_topic.items())
    ]


def _cluster_groups(brain: Brain, ecus: list[dict],
                    cfg) -> list[ReviewGroup] | None:
    """Cognitive-cluster grouping (§4.6/D36), or None when no clusters exist.

    Each candidate joins the cluster of its most-similar canonical ECU
    (cosine >= diffuser.relevance_threshold); a candidate with no such
    anchor keeps its own scope-path group rather than being forced into an
    unrelated topic. Groups sort by label; ECUs within a group by
    confidence descending — same ordering contract as scope grouping.
    """
    if brain.count_clusters() == 0:
        return None
    from .embeddings import from_blob

    relevance_floor = cfg.diffuser.relevance_threshold
    by_label: dict[str, list[dict]] = {}
    leftovers: list[dict] = []
    for ecu in ecus:
        assigned = None
        emb = ecu.get("embedding")
        if emb is not None:
            vec = (
                from_blob(bytes(emb))
                if isinstance(emb, (bytes, bytearray))
                else np.asarray(emb, dtype=np.float32)
            )
            for hit in brain.find_similar(
                vec, threshold=relevance_floor, include_session=False,
            ):
                memberships = brain.clusters_for_ecu(hit["id"])
                if memberships:
                    assigned = memberships[0]
                    break
        if assigned is None:
            leftovers.append(ecu)
            continue
        by_label.setdefault(f"cluster:{assigned['label']}", []).append(ecu)

    groups = [
        ReviewGroup(
            label=label,
            ecus=sorted(members, key=lambda e: e["confidence"], reverse=True),
        )
        for label, members in sorted(by_label.items())
    ]
    if leftovers:
        groups.extend(_scope_groups(leftovers))
    return groups


def group_for_review(brain: Brain, session_id: str, config=None) -> ReviewPlan:
    """Candidate ECUs grouped by topic (§7.4 batch review).

    Candidates are session ECUs with review_status pending|skipped (skipped
    ECUs from earlier reviews come back, §28.6). ``review_gate.
    grouping_strategy`` (D36) selects the strategy: "scope" (default,
    scope_path topics) or "cluster" (HDBSCAN cognitive clusters, falling
    back to scope when no clusters exist). Groups sort by label; ECUs
    within a group sort by confidence descending.
    """
    if brain.get_session(session_id) is None:
        raise ReviewGateError(f"no session with id {session_id}")
    ecus = [
        e for e in brain.list_session_ecus(session_id)
        if e["review_status"] in ("pending", "skipped")
    ]
    cfg = config or get_config()
    groups = None
    if getattr(cfg.review_gate, "grouping_strategy", "scope") == "cluster":
        groups = _cluster_groups(brain, ecus, cfg)
    if groups is None:                      # scope default / cluster fallback
        groups = _scope_groups(ecus)
    return ReviewPlan(session_id=session_id, groups=groups, total=len(ecus))


# ---------------------------------------------------------------------------
# promotion (§7.3 accept/edit) + full diffusion (§8.3)
# ---------------------------------------------------------------------------

def _promote(brain: Brain, session_ecu: dict, edited_cognition: str | None,
             embedding_model) -> str:
    """Move an accepted session ECU into the Canonical Brain (same id, D21).

    §7.3 edit: the edited cognition replaces the candidate text (and is
    re-embedded); the Extractor's original is preserved under
    ``metadata.review_edit`` for audit (D19).
    """
    doc = {k: v for k, v in session_ecu.items()
           if k not in ("session_id", "review_status", "embedding")}
    embedding = session_ecu.get("embedding")
    if edited_cognition and edited_cognition.strip() \
            and edited_cognition.strip() != doc["cognition"]:
        metadata = dict(doc.get("metadata") or {})
        metadata["review_edit"] = {
            "original_cognition": doc["cognition"],
            "edited_at": _now_iso(),
        }
        doc["metadata"] = metadata
        doc["cognition"] = edited_cognition.strip()
        embedding = embedding_model.encode_one(doc["cognition"])
    return brain.insert_ecu(doc, embedding=embedding)


def _cosine(brain: Brain, ecu_a: dict, ecu_b: dict, embedding_model) -> float:
    """Cosine similarity from stored embeddings (recomputed if missing)."""
    va, vb = ecu_a.get("embedding"), ecu_b.get("embedding")
    if va is None:
        va = embedding_model.encode_one(ecu_a["cognition"])
    if vb is None:
        vb = embedding_model.encode_one(ecu_b["cognition"])
    from .embeddings import from_blob
    a = from_blob(va) if isinstance(va, (bytes, bytearray)) else np.asarray(va)
    b = from_blob(vb) if isinstance(vb, (bytes, bytearray)) else np.asarray(vb)
    return float(np.dot(a, b))  # embeddings are L2-normalized


# ---------------------------------------------------------------------------
# contradiction surfacing (§17.4) + competing hypotheses (§17.5)
# ---------------------------------------------------------------------------

_CASE3_SOURCES = ("debugging", "investigation")
_CASE3_CONF_RANGE = (0.30, 0.60)


def _is_case3(ecu_a: dict, ecu_b: dict) -> bool:
    """§17.2 Case-3 (competing hypotheses) computable signals: same scope
    level, overlapping grounding, both from investigation contexts, both
    moderate confidence (0.30–0.60)."""
    if ecu_a["scope"]["level"] != ecu_b["scope"]["level"]:
        return False
    files_a = set(ecu_a.get("grounding", {}).get("files") or [])
    files_b = set(ecu_b.get("grounding", {}).get("files") or [])
    if not (files_a & files_b):
        return False
    if ecu_a["provenance"]["source_type"] not in _CASE3_SOURCES:
        return False
    if ecu_b["provenance"]["source_type"] not in _CASE3_SOURCES:
        return False
    lo, hi = _CASE3_CONF_RANGE
    return lo <= ecu_a["confidence"] <= hi and lo <= ecu_b["confidence"] <= hi


def _mark_competing(brain: Brain, ecu_ids: list[str], now: datetime) -> None:
    """§17.5: competing_since is set when the Case-3 relationship is FIRST
    detected — never overwritten (the persistence clock keeps running)."""
    for ecu_id in ecu_ids:
        ecu = brain.get_ecu(ecu_id)
        if ecu is None:
            continue
        if (ecu.get("metadata") or {}).get("competing_since"):
            continue
        brain.update_ecu_metadata(ecu_id, competing_since=now.isoformat())


def _flag_pair(result: ReviewResult, kind: str, ecu_a: dict, ecu_b: dict,
               message: str) -> None:
    result.flags.append({
        "kind": kind,
        "ecu_ids": [ecu_a["id"], ecu_b["id"]],
        "message": message,
    })


def _surface_contradiction(brain: Brain, result: ReviewResult, new_ecu: dict,
                           existing: dict, cfg, now: datetime,
                           already_flagged: set) -> None:
    """§17.4 review-gate flag rules for one genuine contradiction pair.

    - both sides > theta_contradiction_flag (0.7): the diffuser already emits
      this flag during diffusion; pending-update applications emit it here.
    - engineering/domain scope: ALWAYS flag (fundamental philosophy conflict).
    - Case 3 (§17.2 signals): set competing_since on both (§17.5).
    ``already_flagged`` holds frozensets of pair ids that already got a
    both-high flag from the diffuser, so the scope rule doesn't double-report.
    """
    theta = cfg.confidence.theta_contradiction_flag
    pair = frozenset((new_ecu["id"], existing["id"]))
    if new_ecu["confidence"] > theta and existing["confidence"] > theta \
            and pair not in already_flagged:
        _flag_pair(
            result, "high_stakes_contradiction", new_ecu, existing,
            "Two well-supported engineering beliefs contradict each other: "
            f"{new_ecu['id']} ({new_ecu['confidence']:.2f}) vs "
            f"{existing['id']} ({existing['confidence']:.2f}).",
        )
    always = set(cfg.contradiction.always_flag_scopes)
    if pair not in already_flagged and (
        new_ecu["scope"]["level"] in always
        or existing["scope"]["level"] in always
    ):
        _flag_pair(
            result, "scope_contradiction", new_ecu, existing,
            "Contradiction at "
            f"{new_ecu['scope']['level'] if new_ecu['scope']['level'] in always else existing['scope']['level']} "
            f"scope: {new_ecu['id']} vs {existing['id']} — a fundamental "
            "conflict in engineering beliefs worth investigating.",
        )
    if _is_case3(new_ecu, existing):
        _mark_competing(brain, [new_ecu["id"], existing["id"]], now)


def _notify_propagation(brain: Brain, result: ReviewResult,
                        challenged_ids: list[str], cfg=None) -> None:
    """§17.4 item 3: a depends_on chain is affected — the user should know.

    Gated by ``contradiction.notify_on_dependent`` (design doc §11): False
    silences the NOTIFICATION only — §15.7 propagation itself has already
    happened at the caller and is never disabled by this setting."""
    if not (cfg or get_config()).contradiction.notify_on_dependent:
        return
    for dep_id in challenged_ids:
        dep = brain.get_ecu(dep_id)
        cognition = dep["cognition"] if dep else dep_id
        result.notifications.append({
            "kind": "depends_on_at_risk",
            "ecu_ids": [dep_id],
            "message": (
                f"ECU {dep_id} depends on a cognition that was challenged in "
                f"this review and may need re-evaluation: \"{cognition}\""
            ),
        })


def _opposing_edge_notifications(brain: Brain, result: ReviewResult) -> None:
    """§14.3 exception clause (D53): a pair holding BOTH a supports and a
    contradicts edge is a decomposition signal — surface it at the gate.

    Scans all canonical edges every gate run (not just pairs edged here):
    pending-update applications create edges without passing through the
    diffuser's handlers, and pairs can accumulate their second edge across
    sessions. Pairs the diffuser flagged during THIS run are skipped so
    nothing is reported twice.
    """
    already = {
        frozenset(f.get("ecu_ids", []))
        for f in result.flags
        if f.get("kind") == "opposing_edges"
    }
    by_pair: dict[tuple[str, str], set[str]] = {}
    for edge in brain.list_all_edges():
        if edge["type"] not in ("supports", "contradicts"):
            continue
        key = (edge["source_id"], edge["target_id"])
        by_pair.setdefault(key, set()).add(edge["type"])
    for (source_id, target_id), types in by_pair.items():
        if types != {"supports", "contradicts"}:
            continue
        if frozenset((source_id, target_id)) in already:
            continue
        result.notifications.append({
            "kind": "opposing_edges",
            "ecu_ids": [source_id, target_id],
            "message": (
                f"ECU {source_id[:8]} both supports and contradicts ECU "
                f"{target_id[:8]}. This relationship may need decomposition "
                "into more atomic ECUs."
            ),
        })


def _open_question_sweep(brain: Brain, cfg, now: datetime,
                         result: ReviewResult) -> None:
    """§17.5 persistence limits — runs at the review gate (the notification
    mechanism). Challenged ECUs whose competing hypotheses have persisted
    past their scope's limit are parked as open_question (confidence frozen).
    """
    limits = cfg.contradiction.competing_hypothesis_persistence
    for ecu in brain.list_ecus(status="challenged"):
        since = (ecu.get("metadata") or {}).get("competing_since")
        if not since:
            continue
        limit = limits.get(ecu["scope"]["level"])
        if limit is None:  # engineering/domain persist indefinitely
            continue
        elapsed = (now - _parse_iso(since)).days
        if elapsed < limit:
            continue
        brain.update_ecu_status(ecu["id"], "open_question")
        others = [
            nb["other_id"] for nb in brain.get_neighbourhood(ecu["id"])
            if nb["type"] == "contradicts" and nb["edge_brain"] == "canonical"
        ]
        pair = [ecu["id"], others[0]] if others else [ecu["id"]]
        result.notifications.append({
            "kind": "open_question",
            "ecu_ids": pair,
            "message": (
                f"{' and '.join(pair)} have been competing for {elapsed} days "
                f"without resolution (scope limit: {limit} days). Both are now "
                "parked as open_question — confidence frozen, retrieval will "
                "flag them as unresolved. Options: investigate further, mark "
                "one preferred, reclassify as orthogonal, or archive."
            ),
        })


# ---------------------------------------------------------------------------
# open-question resolution UI (§17.5 options a-d, design doc §6.3, D41)
# ---------------------------------------------------------------------------

_RESOLUTION_CHOICES = ("a", "b", "c", "d")


def _contradicts_edges_between(brain: Brain, id_a: str, id_b: str) -> list[dict]:
    """All canonical contradicts edges joining the pair, either direction."""
    out = []
    for edge in brain.get_edges_for(id_a):
        if edge["type"] != "contradicts":
            continue
        other = (edge["target_id"] if edge["source_id"] == id_a
                 else edge["source_id"])
        if other == id_b:
            out.append(edge)
    return out


def find_open_question_pairs(brain: Brain, now: datetime | None = None,
                             config=None) -> list[dict]:
    """Open_question ECUs grouped into their competing pairs (§17.5).

    A pair is two open_question canonical ECUs joined by a contradicts edge.
    Unpaired open_question ECUs are returned separately under ``"singles"``
    (their partner may already be superseded/deprecated); resolution options
    (b)/(c) need a partner, so singles only offer investigate/archive.
    Deterministic order: oldest ``competing_since`` first.
    """
    now = now or _utcnow()
    cfg = config or get_config()
    limits = cfg.contradiction.competing_hypothesis_persistence
    parked = brain.list_ecus(status="open_question")
    by_id = {e["id"]: e for e in parked}
    seen: set[frozenset] = set()
    pairs, paired_ids = [], set()
    entries = []
    for ecu in parked:
        since = (ecu.get("metadata") or {}).get("competing_since")
        entries.append((_parse_iso(since) if since else None, ecu))
    entries.sort(key=lambda item: (item[0] is None,
                                   item[0] or now, item[1]["id"]))
    for _, ecu in entries:
        for nb in brain.get_neighbourhood(ecu["id"]):
            other_id = nb.get("other_id")
            if nb["type"] != "contradicts" or nb["edge_brain"] != "canonical":
                continue
            if other_id not in by_id:
                continue
            key = frozenset((ecu["id"], other_id))
            if key in seen:
                continue
            seen.add(key)
            other = by_id[other_id]
            paired_ids.update((ecu["id"], other_id))
            since = (ecu.get("metadata") or {}).get("competing_since") or \
                    (other.get("metadata") or {}).get("competing_since")
            since_dt = _parse_iso(since) if since else None
            pairs.append({
                "ecu_a": ecu,
                "ecu_b": other,
                "since": since_dt,
                "days_unresolved": (now - since_dt).days if since_dt else None,
                "limit_days": limits.get(ecu["scope"]["level"]),
            })
    singles = [e for e in parked if e["id"] not in paired_ids]
    return {"pairs": pairs, "singles": singles}


def apply_open_question_resolution(
    brain: Brain,
    id_a: str,
    id_b: str | None,
    choice: str,
    preferred_id: str | None = None,
    *,
    config=None,
    now: datetime | None = None,
) -> dict:
    """Apply one §17.5 resolution option to a competing pair (D41).

    - (a) investigate: both back to ``challenged``, ``competing_since`` reset
      (the persistence clock restarts).
    - (b) preferred: the preferred ECU gets an ``alpha_retrieval`` confidence
      bump (design doc §6.3) and returns to ``active``; the other is
      superseded by it (status + real ``supersedes`` edge — retrieval derives
      its SUPERSEDED-BY slot from that edge).
    - (c) orthogonal: both back to ``active``; the contradicts edges between
      them are removed; a user-resolution note goes into each metadata.
    - (d) archive: both archived (confidence frozen at the parked value).

    Returns a record describing what happened. Raises ReviewGateError on an
    unknown choice, a missing/mis-scoped preferred id, or an ECU that is no
    longer open_question (the interactive driver re-checks before applying).
    """
    cfg = config or get_config()
    now = now or _utcnow()
    choice = str(choice).strip().lower()
    if choice not in _RESOLUTION_CHOICES:
        raise ReviewGateError(
            f"unknown resolution choice {choice!r}; expected one of "
            f"{', '.join(_RESOLUTION_CHOICES)}"
        )
    ecu_a = brain.get_ecu(id_a)
    ecu_b = brain.get_ecu(id_b) if id_b else None
    if ecu_a is None or (id_b is not None and ecu_b is None):
        raise ReviewGateError(f"no such ECU pair: {id_a} / {id_b}")
    for ecu in (ecu_a, ecu_b):
        if ecu is not None and ecu["status"] != "open_question":
            raise ReviewGateError(
                f"ECU {ecu['id']} is {ecu['status']!r}, not open_question"
            )
    if choice == "b":
        if preferred_id not in (id_a, id_b):
            raise ReviewGateError(
                f"choice (b) needs preferred_id of the pair, got {preferred_id!r}"
            )
    note_date = now.date().isoformat()

    def _both(action) -> None:
        action(ecu_a)
        if ecu_b is not None:
            action(ecu_b)

    if choice == "a":                                   # (a) investigate
        def _investigate(ecu) -> None:
            brain.update_ecu_status(ecu["id"], "challenged")
            brain.update_ecu_metadata(ecu["id"],
                                      competing_since=now.isoformat())
        _both(_investigate)
        message = (f"{id_a} and {id_b} returned to challenged; "
                   "the persistence clock was reset.")
    elif choice == "b":                                 # (b) mark one preferred
        loser = ecu_b if preferred_id == id_a else ecu_a
        winner = ecu_a if preferred_id == id_a else ecu_b
        bumped = conf.reinforce_stored_confidence(winner["confidence"], cfg)
        brain.update_ecu_confidence(winner["id"], bumped)
        brain.update_ecu_status(winner["id"], "active")
        brain.update_ecu_status(loser["id"], "superseded")
        brain.add_edge(winner["id"], loser["id"], "supersedes",
                       weight=1.0, confidence_delta=0.0,
                       supersession_type="semantic")
        message = (f"{winner['id']} stays active (confidence "
                   f"{winner['confidence']:.2f} -> {bumped:.2f}); "
                   f"{loser['id']} superseded.")
    elif choice == "c":                                 # (c) orthogonal
        def _orthogonal(ecu) -> None:
            brain.update_ecu_status(ecu["id"], "active")
            brain.update_ecu_metadata(
                ecu["id"],
                orthogonal_note=(f"User reclassified as orthogonal on "
                                 f"{note_date}"))
        for edge in _contradicts_edges_between(brain, id_a, id_b):
            brain.delete_edge(edge["id"])
        _both(_orthogonal)
        message = (f"{id_a} and {id_b} are both active again; the contradicts "
                   f"edge was removed and both carry an orthogonal-note "
                   f"(user resolution, {note_date}).")
    else:                                               # (d) archive
        _both(lambda ecu: brain.update_ecu_status(ecu["id"], "archived"))
        message = f"{id_a} and {id_b} archived (confidence frozen)."

    return {
        "ecu_ids": [id_a] + ([id_b] if id_b else []),
        "choice": choice,
        "preferred_id": preferred_id if choice == "b" else None,
        "message": message,
    }


def _print_open_question(pair: dict, index: int, print_fn) -> None:
    a, b = pair["ecu_a"], pair["ecu_b"]
    print_fn(f"\n[{index}] \"{_truncate(a['cognition'])}\" vs "
             f"\"{_truncate(b['cognition'])}\"")
    if pair["since"] is not None:
        detail = (f"Unresolved since: {pair['since'].date().isoformat()} "
                  f"({pair['days_unresolved']} days")
        if pair["limit_days"]:
            detail += f", exceeds {pair['limit_days']}-day limit"
        print_fn(f"    {detail})")
    print_fn(f"    Scope: {a['scope']['path']}")
    print_fn("    Options:")
    print_fn("    (a) Investigate further — reset the timer, keep as challenged")
    print_fn("    (b) Mark one preferred — choose the ECU you believe is correct")
    print_fn("    (c) Reclassify as orthogonal — both are true in different contexts")
    print_fn("    (d) Archive — no longer relevant")


def resolve_open_questions(
    brain: Brain,
    *,
    input_fn=input,
    print_fn=print,
    config=None,
    now: datetime | None = None,
) -> list[dict]:
    """Interactive §17.5 resolution, presented BEFORE the normal review flow
    (design doc §6.3/D41). Unpaired open_question ECUs are listed but only
    offered investigate/archive. An EOF/non-answer leaves the question
    parked — open questions never block anything (§17.5 item 4).
    """
    now = now or _utcnow()
    found = find_open_question_pairs(brain, now=now, config=config)
    total = len(found["pairs"]) + len(found["singles"])
    records: list[dict] = []
    if not total:
        return records
    print_fn(f"\nEC Review Gate — Open Questions\n\n{total} open question(s) "
             "need resolution:")
    for i, pair in enumerate(found["pairs"], start=1):
        _print_open_question(pair, i, print_fn)
        while True:
            try:
                answer = input_fn("\n    Choose (a/b/c/d): ").strip().lower()
            except EOFError:
                answer = ""
            if answer == "":
                print_fn("    (left as open_question)")
                break
            if answer in _RESOLUTION_CHOICES:
                preferred_id = None
                if answer == "b":
                    while preferred_id is None:
                        try:
                            pick = input_fn(
                                f"    Which ECU is preferred? [1] "
                                f"{pair['ecu_a']['id'][:8]} / [2] "
                                f"{pair['ecu_b']['id'][:8]}: "
                            ).strip()
                        except EOFError:
                            pick = ""
                        if pick == "1":
                            preferred_id = pair["ecu_a"]["id"]
                        elif pick == "2":
                            preferred_id = pair["ecu_b"]["id"]
                        elif pick == "":
                            break
                        if preferred_id is None and pick != "":
                            print_fn("    Please answer 1 or 2.")
                    if preferred_id is None:
                        print_fn("    (left as open_question)")
                        break
                try:
                    records.append(apply_open_question_resolution(
                        brain, pair["ecu_a"]["id"], pair["ecu_b"]["id"],
                        answer, preferred_id, config=config, now=now))
                except ReviewGateError as exc:
                    print_fn(f"    ! {exc}")
                break
            print_fn("    Please answer a, b, c, or d.")
    for j, single in enumerate(found["singles"], start=len(found["pairs"]) + 1):
        print_fn(f"\n[{j}] \"{_truncate(single['cognition'])}\" — open "
                 "question without a competing partner")
        print_fn(f"    Scope: {single['scope']['path']}")
        try:
            answer = input_fn("    (a) Investigate / (d) Archive / enter to "
                              "leave parked: ").strip().lower()
        except EOFError:
            answer = ""
        if answer in ("a", "d"):
            records.append(apply_open_question_resolution(
                brain, single["id"], None, answer, config=config, now=now))
    return records


def resolve_open_questions_archive_all(
    brain: Brain, *, config=None, now: datetime | None = None
) -> list[dict]:
    """Non-interactive ``--resolve-open-questions archive`` (§6.3 item 5):
    archive every open_question ECU, paired or single."""
    now = now or _utcnow()
    found = find_open_question_pairs(brain, now=now, config=config)
    records = [
        apply_open_question_resolution(
            brain, p["ecu_a"]["id"], p["ecu_b"]["id"], "d",
            config=config, now=now)
        for p in found["pairs"]
    ]
    done = {i for r in records for i in r["ecu_ids"]}
    for single in found["singles"]:
        if single["id"] in done:
            continue
        brain.update_ecu_status(single["id"], "archived")
        records.append({
            "ecu_ids": [single["id"]], "choice": "d", "preferred_id": None,
            "message": f"{single['id']} archived (confidence frozen).",
        })
    return records


# ---------------------------------------------------------------------------
# grounding-deprecation surfacing (§3.5/§3.6) — accumulated at review gate
# ---------------------------------------------------------------------------

def grounding_deprecation_notifications(brain: Brain,
                                        scan_limit: int = 200) -> list[dict]:
    """§3.6: deprecated ECUs from grounding verification runs since the LAST
    review gate are surfaced here (notifications accumulate at the review
    gate, §17.4). Reads maintenance_log only — never writes. One aggregated
    notification listing every deprecated ECU plus the dependents count.
    """
    rows = brain.list_maintenance_log(limit=scan_limit)
    cutoff = next((r["run_at"] for r in rows if r["action"] == "review_gate"),
                  None)
    deprecated: list[dict] = []
    for row in rows:
        if cutoff and row["run_at"] <= cutoff:
            break                       # newest first — everything older surfaced
        if row["action"] != "grounding":
            continue
        try:
            details = json.loads(row["details"] or "{}")
        except json.JSONDecodeError:
            continue
        deprecated.extend(details.get("deprecated") or [])
    if not deprecated:
        return []
    lines = [
        "During the last maintenance cycle, EC deprecated "
        f"{len(deprecated)} ECU(s) whose grounding references are no longer "
        "present in the repository:"
    ]
    dependents: set[str] = set()
    for d in deprecated:
        ecu = brain.get_ecu(d.get("ecu", ""))
        cognition = ecu["cognition"] if ecu else d.get("ecu", "?")
        missing = ", ".join((d.get("missing_files") or [])
                            + (d.get("missing_symbols") or [])) or "grounding gone"
        lines.append(f'  - "{cognition}" ({d.get("scope", "?")} scope, '
                     f"{missing})")
        dependents.update(d.get("challenged_dependents") or [])
    if dependents:
        lines.append(f"{len(dependents)} ECU(s) that depend on these were "
                     "marked challenged and need re-evaluation.")
    return [{
        "kind": "grounding_deprecations",
        "ecu_ids": [d.get("ecu") for d in deprecated],
        "message": "\n".join(lines),
    }]


# ---------------------------------------------------------------------------
# pending updates (§28.6) — exact deltas, never double-applied (D16)
# ---------------------------------------------------------------------------

def _resolve_pending_updates(brain: Brain, session_id: str,
                             accepted: list[str], rejected: list[str],
                             handled_pairs: set, result: ReviewResult, cfg,
                             embedding_model, now: datetime) -> None:
    """Apply/discard the session's pending updates.

    Accepted source + the full diffuser already created an edge to the target
    → the exact delta was applied by the diffuser; mark applied, delete, no
    second update. Accepted source, pair NOT handled → recompute the exact
    log-odds delta (§15.2/§15.3) from embedding cosine, create the canonical
    edge, update in place, propagate (§15.7); contradicts also marks
    challenged (fail-safe, §17.3). Rejected source → discarded. Both paths
    then delete the record and clear has_pending_updates when none remain.
    """
    pending = brain.list_pending_updates(session_id=session_id, status="pending")
    accepted_set, rejected_set = set(accepted), set(rejected)
    for pu in pending:
        source_id = pu["session_ecu_id"]
        if source_id in accepted_set:
            target = brain.get_ecu(pu["canonical_ecu_id"])
            pair = (source_id, pu["canonical_ecu_id"])
            if target is not None and pair not in handled_pairs:
                new_ecu = brain.get_ecu(source_id)  # promoted by now
                if new_ecu is not None:
                    sim = _cosine(brain, new_ecu, target, embedding_model)
                    if pu["relationship_type"] == "supports":
                        new_c, delta = conf.support_update(
                            target["confidence"], new_ecu["confidence"], sim, cfg)
                    else:
                        new_c, delta = conf.contradiction_update(
                            target["confidence"], new_ecu["confidence"], sim, cfg)
                    brain.add_edge(source_id, target["id"],
                                   pu["relationship_type"], weight=sim,
                                   confidence_delta=delta)
                    brain.update_ecu_confidence(target["id"], new_c)
                    if pu["relationship_type"] == "contradicts":
                        if target["status"] != "challenged":
                            brain.update_ecu_status(target["id"], "challenged")
                        brain.update_ecu_metadata(target["id"],
                                                  last_challenged=now.isoformat())
                        refreshed = brain.get_ecu(target["id"])
                        _surface_contradiction(
                            brain, result, new_ecu, refreshed, cfg, now, set())
                    _notify_propagation(
                        brain, result,
                        conf.propagate(target["id"], new_c, brain, cfg), cfg)
                    result.diffusions.append({
                        "via": "pending_update",
                        "session_ecu_id": source_id,
                        "canonical_ecu_id": target["id"],
                        "relationship": pu["relationship_type"],
                        "exact_delta": delta,
                        "indicative_delta": pu["proposed_confidence_delta"],
                    })
            brain.set_pending_update_status(pu["id"], "applied")
            brain.delete_pending_update(pu["id"])
            result.pending_applied += 1
            _maybe_clear_pending_flag(brain, pu["canonical_ecu_id"])
        elif source_id in rejected_set:
            brain.set_pending_update_status(pu["id"], "discarded")
            brain.delete_pending_update(pu["id"])
            result.pending_discarded += 1
            _maybe_clear_pending_flag(brain, pu["canonical_ecu_id"])
        # skipped sources: their pending updates stay pending (§28.6)


def _maybe_clear_pending_flag(brain: Brain, canonical_ecu_id: str) -> None:
    """§28.6: clear has_pending_updates when no pending rows remain."""
    if not brain.list_pending_updates(
        canonical_ecu_id=canonical_ecu_id, status="pending"
    ):
        try:
            brain.update_ecu_metadata(canonical_ecu_id, has_pending_updates=False)
        except Exception:  # target may be gone — flag is moot
            log.warning("could not clear has_pending_updates on %s",
                        canonical_ecu_id)


# ---------------------------------------------------------------------------
# decision application (§5/§28.6)
# ---------------------------------------------------------------------------

_ACTIONS = ("accept", "reject", "skip")


def _parse_decisions(decisions: dict | None) -> dict:
    """Normalize {ecu_id: action | {"action": …, "edited_cognition": …}}."""
    parsed = {}
    for ecu_id, raw in (decisions or {}).items():
        if isinstance(raw, str):
            parsed[ecu_id] = (raw, None)
        elif isinstance(raw, dict):
            parsed[ecu_id] = (raw.get("action"), raw.get("edited_cognition"))
    return parsed


def apply_review_decisions(
    brain: Brain,
    session_id: str,
    decisions: dict | None,
    *,
    config=None,
    embedding_model=None,
    now: datetime | None = None,
) -> ReviewResult:
    """Apply the human's review decisions and close the session (§28.6).

    ``decisions`` maps ecu_id -> "accept" | "reject" | "skip", or a dict
    ``{"action": "accept", "edited_cognition": "…"}`` for §7.3 edits.
    Candidates without a decision are skipped (the §28.7 'done' path).
    Invalid actions are recorded in ``result.errors`` and treated as skip —
    one bad decision must not lose the batch.
    """
    cfg = config or get_config()
    now = now or _utcnow()
    session = brain.get_session(session_id)
    if session is None:
        raise ReviewGateError(f"no session with id {session_id}")
    if session["status"] != "active":
        raise ReviewGateError(
            f"session {session_id} is {session['status']}; only active "
            "sessions can go through the review gate"
        )
    if embedding_model is None:
        from .embeddings import get_embedding_model
        embedding_model = get_embedding_model()

    result = ReviewResult(session_id=session_id)
    parsed = _parse_decisions(decisions)
    plan = group_for_review(brain, session_id, config=cfg)
    handled_pairs: set = set()       # (new_id, target_id) edged by the diffuser
    already_flagged: set = set()     # pairs the diffuser already flagged (>0.7)

    # -- steps 2-4: accept / reject / skip --------------------------------
    for group in plan.groups:
        for ecu in group.ecus:
            action, edited = parsed.get(ecu["id"], ("skip", None))
            if action not in _ACTIONS:
                result.errors.append(
                    f"invalid review action {action!r} for {ecu['id']}; skipped"
                )
                action = "skip"
            if action == "accept":
                canonical_id = _promote(brain, ecu, edited, embedding_model)
                result.accepted.append(canonical_id)
                try:
                    dres = diffuser.diffuse_ecu(
                        brain, canonical_id,
                        embedding_model=embedding_model, config=cfg,
                    )
                except DiffuserError as exc:
                    # Promotion stands (the human decided); diffusion is
                    # enrichment and can fail offline — recorded, not fatal.
                    log.warning("diffusion failed for %s: %s", canonical_id, exc)
                    result.errors.append(
                        f"diffusion failed for {canonical_id}: {exc}"
                    )
                    result.undiffused.append(canonical_id)
                    continue
                result.diffusions.append(dres)
                result.flags.extend(dres.flags)
                for f in dres.flags:
                    already_flagged.add(frozenset(f.get("ecu_ids", [])))
                for edge in dres.edges_created:
                    handled_pairs.add((canonical_id, edge["target_id"]))
                new_ecu = brain.get_ecu(canonical_id)
                for contra in dres.contradictions:
                    existing = brain.get_ecu(contra["ecu_id"])
                    if existing is not None:
                        _surface_contradiction(
                            brain, result, new_ecu, existing, cfg, now,
                            already_flagged,
                        )
                _notify_propagation(
                    brain, result, dres.challenged_by_propagation, cfg
                )
            elif action == "reject":
                result.rejected.append(ecu["id"])
            else:
                brain.update_session_ecu_review_status(ecu["id"], "skipped")
                result.skipped.append(ecu["id"])

    # -- step 5: pending updates -------------------------------------------
    _resolve_pending_updates(
        brain, session_id, result.accepted, result.rejected,
        handled_pairs, result, cfg, embedding_model, now,
    )

    # -- §14.3: opposing-edge pairs (decomposition signals) -----------------
    _opposing_edge_notifications(brain, result)

    # -- step 8: cleanup + close -------------------------------------------
    brain.delete_session_edges_for(result.accepted + result.rejected)
    for ecu_id in result.accepted + result.rejected:
        brain.delete_session_ecu(ecu_id)

    # -- step 7: §17.5 persistence sweep (review gate is the clock) --------
    _open_question_sweep(brain, cfg, now, result)

    # -- §3.6: grounding deprecations accumulated since the last gate ------
    result.notifications.extend(grounding_deprecation_notifications(brain))

    brain.close_session(session_id, ended_at=now.isoformat())
    brain.delete_session_activation(session_id)  # fresh slate next time (§12.2)

    result.canonical_total = brain.count_ecus()
    brain.log_maintenance("review_gate", json.dumps({
        "session_id": session_id,
        "accepted": len(result.accepted),
        "rejected": len(result.rejected),
        "skipped": len(result.skipped),
        "pending_applied": result.pending_applied,
        "pending_discarded": result.pending_discarded,
        "canonical_total": result.canonical_total,
    }))
    result.message = (
        f"Review complete. {len(result.accepted)} accepted, "
        f"{len(result.rejected)} rejected, {len(result.skipped)} skipped "
        f"(pending). Diffusing to Canonical Brain... done. Brain now has "
        f"{result.canonical_total} ECUs."
    )
    return result


def diffusion_failure_warning(result: ReviewResult) -> str | None:
    """Prominent CLI warning when ECUs were promoted without diffusion.

    A promoted-but-undiffused ECU has no edges and unchanged confidence —
    silent edge loss would corrupt benchmark results, so both the interactive
    and non-interactive /ec-stop paths print this when it happens.
    """
    if not result.undiffused:
        return None
    ids = ", ".join(result.undiffused)
    return (
        f"⚠️ WARNING: {len(result.undiffused)} ECU(s) promoted without "
        f"diffusion due to API errors: [{ids}]. These ECUs have no edges "
        "and unchanged confidence. Consider re-running diffusion manually."
    )


# ---------------------------------------------------------------------------
# interactive driver (§28.7) — thin wrapper over the programmatic API
# ---------------------------------------------------------------------------

def _truncate(text: str, n: int = 70) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _print_ecu_brief(ecu: dict, index: int, print_fn) -> None:
    print_fn(f'  [{index}] "{_truncate(ecu["cognition"])}"')
    print_fn(
        f"      confidence: {ecu['confidence']:.2f} | scope: "
        f"{ecu['scope']['path']} | source: {ecu['provenance']['source_type']}"
    )


def _print_ecu_detail(brain: Brain, ecu: dict, print_fn) -> None:
    """§28.7 'd': full ECU — cognition, scope, provenance, grounding,
    evidence, edges."""
    prov = ecu["provenance"]
    grounding = ecu.get("grounding") or {}
    print_fn(f"      cognition:   {ecu['cognition']}")
    print_fn(f"      type:        {ecu['conclusion_type']}")
    print_fn(f"      scope:       {ecu['scope']['level']} ({ecu['scope']['path']})")
    print_fn(f"      confidence:  {ecu['confidence']:.2f}")
    print_fn(
        f"      provenance:  {prov['source_type']} | source_id "
        f"{prov.get('source_id')} | agent {prov.get('origin_agent')} | "
        f"created {prov.get('created_at')}"
    )
    print_fn(f"      grounding:   files={grounding.get('files') or []} "
             f"symbols={grounding.get('symbols') or []} "
             f"commit={grounding.get('commit_hash')}")
    print_fn(f"      evidence:    {ecu.get('evidence_pointers') or []}")
    edges = brain.get_session_edges_for(ecu["id"])
    if edges:
        print_fn("      edges:")
        for e in edges:
            other = e["target_id"] if e["source_id"] == ecu["id"] else e["source_id"]
            print_fn(f"        - {e['type']} -> {other} (weight {e['weight']})")


def interactive_review(
    brain: Brain,
    session_id: str,
    *,
    input_fn=input,
    print_fn=print,
    config=None,
    embedding_model=None,
    now: datetime | None = None,
) -> ReviewResult:
    """The §28.7 terminal interaction: per group a/r/i, per ECU y/n/s/d,
    'skip' remaining groups, 'done' exits (unreviewed become skipped)."""
    plan = group_for_review(brain, session_id, config=config)
    print_fn(
        f"EC Review Gate — {plan.total} candidate ECUs in "
        f"{len(plan.groups)} groups"
    )
    decisions: dict[str, str] = {}
    halted = False
    for gi, group in enumerate(plan.groups, start=1):
        if halted:
            break
        print_fn(f"\nGroup {gi}: {group.label} ({len(group.ecus)} ECUs)")
        for i, ecu in enumerate(group.ecus, start=1):
            _print_ecu_brief(ecu, i, print_fn)
        while True:
            answer = input_fn(
                "\n  accept all | reject all | review individually (a/r/i), "
                "or skip/done: "
            ).strip().lower()
            if answer == "a":
                decisions.update({e["id"]: "accept" for e in group.ecus})
                break
            if answer == "r":
                decisions.update({e["id"]: "reject" for e in group.ecus})
                break
            if answer in ("skip", "done"):
                halted = True  # everything unreviewed becomes skipped
                break
            if answer == "i":
                for i, ecu in enumerate(group.ecus, start=1):
                    while True:
                        choice = input_fn(
                            f"  [{i}] accept? (y/n/s, d for detail): "
                        ).strip().lower()
                        if choice == "d":
                            _print_ecu_detail(brain, ecu, print_fn)
                            continue
                        if choice == "y":
                            decisions[ecu["id"]] = "accept"
                        elif choice == "n":
                            decisions[ecu["id"]] = "reject"
                        elif choice != "s":
                            print_fn("  Please answer y, n, s, or d.")
                            continue
                        break  # 's' leaves the ECU undecided → skipped
                break
            print_fn("  Please answer a, r, i, skip, or done.")

    result = apply_review_decisions(
        brain, session_id, decisions, config=config,
        embedding_model=embedding_model, now=now,
    )
    print_fn(f"\n{result.message}")
    warning = diffusion_failure_warning(result)
    if warning:
        print_fn(warning)
    print_fn("Cleaning up Session Brain...")
    if result.accepted:
        print_fn("  ✓ Accepted ECUs: promoted to Canonical Brain, removed "
                 "from session_ecus")
    if result.rejected:
        print_fn("  ✓ Rejected ECUs: removed from session_ecus")
    if result.accepted or result.rejected:
        print_fn("  ✓ Session edges for accepted/rejected ECUs: removed")
    if result.pending_applied:
        print_fn(f"  ✓ Pending updates applied: {result.pending_applied}")
    if result.pending_discarded:
        print_fn(f"  ✓ Pending updates discarded: {result.pending_discarded}")
    if result.skipped:
        print_fn(f"  ({len(result.skipped)} skipped ECUs remain in "
                 "session_ecus for next review.)")
    for flag in result.flags + result.notifications:
        print_fn(f"  ⚠️ {flag['message']}")
    return result

