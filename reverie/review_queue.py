"""Cross-session review queue (feat/flow, D57) — review independent of
sessions.

One queue across all sessions and repos: every ``session_ecus`` row with
``review_status`` pending | skipped (+ expired when asked) is a candidate,
joined with its session for repo/branch context. This module is the
policy layer over the review gate: decisions are applied through
``review_gate.apply_review_decisions`` (close=False, undecided="leave")
so contradiction surfacing, pending updates, diffusion and the audit
trail behave exactly as the §28.6 stop-path gate — nothing is duplicated.

- **Ordering:** strongest first, by confidence. Topic grouping (the
  §7.4/D18 scope-path groups) stays available as an option.
- **Expiry:** candidates unreviewed for ``review.expire_after_days``
  (default 21; 0 disables) become ``expired`` — kept, never deleted,
  still usable in session memory, never promoted until the user revives
  them (``reverie review --expired``).
- **Nudge:** counts only, never contents (§11.3 anti-anchoring) —
  ``nudge_line`` renders the single summary line for summary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .brain import Brain
from .config import get_config
from .review_gate import _scope_groups, apply_review_decisions

#: Per-candidate rendering shared by the terminal TUI and the MCP
#: ``review_list`` view — one formatter so the two surfaces cannot diverge.
CANDIDATE_RULE = "─" * 36
OPTIONS_LINE = (
    "Options: a accept · r reject · s skip · e edit · g accept group · "
    "u undo · q quit"
)
EXPIRED_OPTIONS_LINE = (
    "Options: v revive (back to pending) · a accept · r reject · q quit"
)

#: Session statuses whose candidates are reviewable (archived sessions'
#: candidates stay in the queue — D56).
REVIEWABLE_SESSION_STATUSES = ("active", "archived", "closed")


class QueueError(RuntimeError):
    """Review-queue failure with actionable guidance."""


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# expiry (lazy at lookup, eager in the Maintainer)
# ---------------------------------------------------------------------------

def expire_stale(brain: Brain, config=None, now: datetime | None = None) -> list[str]:
    """Transition overdue pending/skipped candidates to ``expired`` (D57).

    Returns the expired candidate ids. ``review.expire_after_days = 0``
    disables expiry. Expired rows are KEPT: they remain in session memory
    (retrieval never filtered on review_status) and can be revived."""
    cfg = config or get_config()
    days = int(cfg.review.expire_after_days)
    if days <= 0:
        return []
    now = now or _utcnow()
    cutoff = _now_iso_start(now, days)
    stale = [
        row for row in brain.list_queue_candidates(statuses=("pending", "skipped"))
        if row["created_at"] and row["created_at"] < cutoff
    ]
    for row in stale:
        brain.update_session_ecu_review_status(row["id"], "expired")
    return [row["id"] for row in stale]


def _now_iso_start(now: datetime, days: int) -> str:
    """ISO cutoff: candidates created before ``now - days`` are stale."""
    from datetime import timedelta
    return (now - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# queue listing
# ---------------------------------------------------------------------------

def candidates(
    brain: Brain,
    repo: str | None = None,
    branch: str | None = None,
    include_expired: bool = False,
    config=None,
    now: datetime | None = None,
) -> list[dict]:
    """The queue: strongest first by confidence, optionally repo/branch
    narrowed. Runs lazy expiry first so callers never see stale pending
    rows (D57)."""
    expire_stale(brain, config=config, now=now)
    statuses = ("pending", "skipped", "expired") if include_expired \
        else ("pending", "skipped")
    rows = brain.list_queue_candidates(repo_path=repo, branch=branch,
                                       statuses=statuses)
    return [
        row for row in rows
        if row["session_status"] in REVIEWABLE_SESSION_STATUSES
    ]


def topic_groups(queue: list[dict]) -> list[tuple[str, list[dict]]]:
    """Existing §7.4 scope-path grouping, applied to the queue (optional
    ordering). Groups sort by label; candidates within a group keep the
    queue's confidence ordering."""
    grouped = _scope_groups([
        {**row, "scope": {"path": row["scope_path"]}} for row in queue
    ])
    return [(g.label, g.ecus) for g in grouped]


def group_of(candidate: dict, groups: list[tuple[str, list[dict]]]) -> str:
    """The topic label of one candidate (bulk accept scope, D57)."""
    for label, members in groups:
        if any(m["id"] == candidate["id"] for m in members):
            return label
    return candidate["scope_path"] or "unknown"


def page(queue: list[dict], page_no: int = 1, size: int | None = None,
         config=None) -> tuple[list[dict], int]:
    """One page of the queue (most ``review.page_size`` candidates). Purely
    presentational — nothing is discarded; ``total_pages`` tells callers
    whether more exist."""
    cfg = config or get_config()
    size = size or int(cfg.review.page_size)
    start = max(0, (page_no - 1) * size)
    return queue[start:start + size], max(1, (len(queue) + size - 1) // size)


# ---------------------------------------------------------------------------
# rendering — ONE formatter for the terminal TUI and MCP review_list
# ---------------------------------------------------------------------------

def candidate_detail(brain: Brain, candidate: dict) -> dict:
    """Grounding/evidence for display (queue rows stay lean).

    Both review surfaces call this — the evidence flag has exactly one
    source (``metadata.evidence_status``) and the grounding/repo fields
    shown by the terminal are also shown through the MCP tool.
    """
    row = brain.get_session_ecu_row(candidate["id"])
    if row is None:
        return {}
    doc = json.loads(row["document"]) if row.get("document") else {}
    grounding = doc.get("grounding") or {}
    return {
        "grounding_files": grounding.get("files") or [],
        "evidence": doc.get("evidence_pointers") or [],
        "grounding_symbols": grounding.get("symbols") or [],
        "evidence_status": (doc.get("metadata") or {}).get("evidence_status"),
        "grounding_dropped": (grounding.get("validation") or {}).get(
            "dropped_files") or [],
        "symbols_dropped": (grounding.get("validation") or {}).get(
            "dropped_symbols") or [],
    }


def evidence_flag(detail: dict | None) -> str:
    """The single evidence check used by every review surface."""
    status = (detail or {}).get("evidence_status")
    if status == "unsupported":
        return "UNSUPPORTED"
    if status == "supported":
        return "SUPPORTED"
    return "UNKNOWN"


def _muted(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "—"
    return str(value) if value not in (None, "") else "—"


def render_candidate_block(candidate: dict, detail: dict | None = None, *,
                           index: int, total: int, expired: bool = False,
                           notes: list[str] | None = None) -> str:
    """One candidate block — the shared snapshot surface for TUI and MCP."""
    detail = detail or {}
    lines = [
        CANDIDATE_RULE,
        f"Candidate {index} of {total} · "
        f"{candidate.get('conclusion_type') or '—'} · "
        f"confidence {candidate['confidence']:.2f} · "
        f"evidence: {evidence_flag(detail)}",
        candidate["cognition"],
    ]
    grounded = _muted(detail.get("grounding_files"))
    symbols = _muted(detail.get("grounding_symbols"))
    if symbols != "—":
        grounded = f"{grounded} (symbols: {symbols})"
    repo = candidate.get("repo_path") or "—"
    if candidate.get("branch"):
        repo = f"{repo} (branch {candidate['branch']})"
    lines.append(
        f"Scope: {candidate.get('scope_path') or '—'} · "
        f"Grounded: {grounded} · Repo: {repo} · ID: {candidate['id']}"
    )
    if notes:
        lines.extend(notes)
    if expired:
        lines.append("Status: expired")
    lines.append(EXPIRED_OPTIONS_LINE if expired else OPTIONS_LINE)
    lines.append(CANDIDATE_RULE)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# decisions through the review gate
# ---------------------------------------------------------------------------

def apply_decisions(
    brain: Brain,
    decisions: dict,
    *,
    channel: str,
    config=None,
    embedding_model=None,
    now: datetime | None = None,
) -> dict:
    """Apply ``{candidate_id: action-or-dict}`` across sessions.

    Candidates are grouped by their owning session and each batch goes
    through ``review_gate.apply_review_decisions(close=False,
    undecided="leave", channel=…)`` — identical gate behaviour, session
    kept open (D57). Returns per-session results plus bookkeeping:

    ``{"results": [ReviewResult...], "decided": [ids], "unknown": [ids]}``
    (``unknown`` = ids with no matching candidate row; their decisions
    were not applied).
    """
    by_session: dict[str, dict] = {}
    unknown: list[str] = []
    for candidate_id, action in (decisions or {}).items():
        row = brain.get_queue_candidate(candidate_id)
        if row is None:
            unknown.append(candidate_id)
            continue
        by_session.setdefault(row["session_id"], {})[candidate_id] = action
    results = []
    decided: list[str] = []
    for session_id, session_decisions in by_session.items():
        result = apply_review_decisions(
            brain, session_id, session_decisions,
            close=False, undecided="leave", channel=channel,
            config=config, embedding_model=embedding_model, now=now,
        )
        results.append(result)
        decided.extend(result.accepted + result.rejected + result.skipped)
    return {"results": results, "decided": decided, "unknown": unknown}


def revive(
    brain: Brain,
    candidate_id: str,
    *,
    channel: str = "cli",
    now: datetime | None = None,
) -> dict:
    """Bring an expired candidate back as pending (``reverie review --expired``).

    Revived candidates re-enter the normal queue. Nothing else is
    promoted implicitly."""
    row = brain.get_queue_candidate(candidate_id)
    if row is None:
        raise QueueError(f"no candidate with id {candidate_id}")
    if row["review_status"] != "expired":
        raise QueueError(
            f"candidate {candidate_id} is {row['review_status']!r}, not "
            "expired — nothing to revive"
        )
    brain.update_session_ecu_review_status(
        candidate_id, "pending",
        created_at=(now or _utcnow()).isoformat(),   # fresh expiry lease —
    )                                                # the document keeps the
    brain.add_review_decision(candidate_id, "revive", channel,   # original
                              session_id=row["session_id"],
                              decided_at=(now or _utcnow()).isoformat())
    return {"id": candidate_id, "status": "pending",
            "message": "Revived — back in the review queue."}


# ---------------------------------------------------------------------------
# nudge (feat/flow §4) — counts only, never contents
# ---------------------------------------------------------------------------

def queue_stats(brain: Brain, config=None, now: datetime | None = None,
                repo_path: str | None = None) -> dict:
    """``{"count", "oldest_age_days"}`` over unreviewed candidates
    (pending + skipped; expired ones are no longer waiting), narrowed to
    ``repo_path`` when given."""
    del config                           # config read happens in nudge_line
    now = now or _utcnow()
    rows = brain.list_queue_candidates(repo_path=repo_path,
                                       statuses=("pending", "skipped"))
    ages = [
        (now - _parse_iso(row["created_at"])).days
        for row in rows if row["created_at"]
    ]
    return {"count": len(rows),
            "oldest_age_days": max(ages) if ages else 0}


def nudge_line(brain: Brain, config=None, now: datetime | None = None,
               repo_path: str | None = None) -> str | None:
    """The one-line summary nudge, or None (nothing to say).

    Fires when the backlog exceeds ``review.nudge_threshold`` OR the oldest
    unreviewed candidate is older than ``review.nudge_age_days``. Counts
    only — never candidate contents (§11.3). With a known repo the line
    splits the count (``review_list`` defaults to the current repo, so
    the numbers must match what the agent will list); outside a repo it
    stays global-only."""
    cfg = config or get_config()
    stats = queue_stats(brain, config=cfg, now=now)
    if not stats["count"]:
        return None
    if stats["count"] <= int(cfg.review.nudge_threshold) \
            and stats["oldest_age_days"] <= int(cfg.review.nudge_age_days):
        return None
    if repo_path:
        repo_stats = queue_stats(brain, config=cfg, now=now,
                                 repo_path=repo_path)
        if repo_stats["count"]:
            return (f"{stats['count']} conclusions waiting for review "
                    f"({repo_stats['count']} in this repo, oldest "
                    f"{repo_stats['oldest_age_days']} days)")
    return (f"{stats['count']} conclusions waiting for review "
            f"(oldest {stats['oldest_age_days']} days)")
