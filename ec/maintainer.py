"""EC Cognition Maintainer — background hygiene for the Canonical Brain.

The Maintainer keeps the Canonical Brain healthy over time (design doc
Section 1, D33). It runs INSIDE the MCP server process — never as a
separate daemon — in two activation modes:

- **Startup check (eager):** when the MCP server starts, if maintenance is
  overdue it runs synchronously before the server accepts tool calls.
- **Background thread (lazy):** a daemon thread checks every
  ``maintainer.check_interval_minutes`` whether a trigger condition is met
  and runs maintenance without blocking the server's request loop.

A maintenance run executes four tasks in a fixed order (order matters:
grounding may deprecate ECUs, edge pruning removes edges to dead ECUs,
clustering should see the post-pruning brain):

1. ``forgetting``    — controlled forgetting: eager status transitions
                       (§17.5 persistence limits, §15.5 supersession
                       eligibility); confidence decay itself is lazy at
                       retrieval time (D34). Live since Phase 7.
2. ``grounding``     — verify ECU grounding references against live repos,
                        deprecating stale ECUs (§21.1, D35). Live since
                        Phase 8; throttled per repo to 72h.
3. ``edge_pruning``  — remove stale/dead edges, reversing their confidence
                        deltas (§14.5). Live since Phase 13 (D52);
                        ``confidence.reverse_update`` is its §15.6 engine.
4. ``clustering``    — HDBSCAN over canonical ECU embeddings. Live since
                        Phase 9; gated by 100 new ECUs since the last run.

Each task logs its own row to ``maintenance_log`` (action + JSON details +
ecus_affected) and ``run_maintenance`` adds a ``full_run`` summary row and
updates ``maintenance_state`` (last_run_at, last_run_ecu_count) so the
trigger checks can compare future brain state against this baseline.

The user never sees any of this: no CLI command, no user action. The only
visible surface is ``last_maintenance_run`` in ``ec_get_summary``.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .confidence import (
    ecu_effective_confidence,
    reverse_update,
    supersession_type,
)
from .confidence import (
    propagate as propagate_confidence,
)

log = logging.getLogger("ec.maintainer")

# Fixed task execution order (design doc §1.3).
TASK_ORDER = ("forgetting", "grounding", "edge_pruning", "clustering")

# maintenance_state keys
STATE_LAST_RUN_AT = "last_run_at"
STATE_LAST_RUN_ECU_COUNT = "last_run_ecu_count"
#: Per-repo grounding-throttle keys: ``grounding_last_check:<repo_path>`` ->
#: ISO timestamp of that repo's last verification (D35).
STATE_GROUNDING_CHECK_PREFIX = "grounding_last_check:"
#: Clustering-trigger baseline: ``COUNT(*) FROM ecus`` at the last clustering
#: run (design doc §4.2) — compared against the current count so only NEW
#: canonical ECUs count toward ``clustering.clustering_threshold``.
STATE_CLUSTERING_ECU_COUNT = "last_clustering_ecu_count"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 timestamp; naive strings are assumed UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# result types
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    """Outcome of one maintenance task."""
    action: str
    ecus_affected: int = 0
    details: dict = field(default_factory=dict)


@dataclass
class MaintenanceResult:
    """Outcome of one full maintenance run (structured, test-friendly)."""
    started_at: str
    finished_at: str = ""
    tasks: list[TaskResult] = field(default_factory=list)

    @property
    def total_ecus_affected(self) -> int:
        return sum(t.ecus_affected for t in self.tasks)

    def summary(self) -> dict:
        """JSON-safe summary for the 'full_run' maintenance_log row."""
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "tasks": {
                t.action: {"ecus_affected": t.ecus_affected, **t.details}
                for t in self.tasks
            },
            "total_ecus_affected": self.total_ecus_affected,
        }


# ---------------------------------------------------------------------------
# task functions — one per maintenance task, independently testable.
# All four tasks are live: forgetting (Phase 7), grounding (Phase 8),
# edge pruning (Phase 13), clustering (Phase 9).
# ---------------------------------------------------------------------------

def _contradicts_pair(brain, ecu_id: str) -> list[str]:
    """Canonical ECU ids joined to ``ecu_id`` by a contradicts edge (§17.5
    competing pair). Session edges never count — the pair lives in the
    Canonical Brain."""
    return [
        nb["other_id"]
        for nb in brain.get_neighbourhood(ecu_id)
        if nb["type"] == "contradicts" and nb["edge_brain"] == "canonical"
    ]


def _persistence_limit_pass(brain, cfg, now: datetime) -> tuple[list[dict], int]:
    """§17.5: challenged ECUs whose competing hypothesis has persisted past
    the scope's persistence limit become open_question — both sides.

    Freezing is a property of the STATUS, not a write: effective_confidence
    returns the stored value for frozen statuses, so parking the pair stops
    their decay by construction. engineering/domain limits are null =
    indefinite — never parked. Trigger uses whole-day comparison
    (``elapsed.days >= limit``), matching review_gate's sweep.
    """
    limits = cfg.contradiction.competing_hypothesis_persistence
    transitions: list[dict] = []
    for ecu in brain.list_ecus(status="challenged"):
        since = (ecu.get("metadata") or {}).get("competing_since")
        if not since:
            continue
        limit = limits.get(ecu["scope"]["level"])
        if limit is None:
            continue                       # engineering/domain: indefinite
        elapsed_days = (now - _parse_iso(since)).days
        if elapsed_days < limit:
            continue
        # Fresh read: a pair member parked earlier in THIS sweep already
        # flipped to open_question in the DB — don't process it twice.
        fresh = brain.get_ecu(ecu["id"])
        if fresh is None or fresh["status"] != "challenged":
            continue
        pair_ids = [
            pid for pid in _contradicts_pair(brain, ecu["id"])
            if (p := brain.get_ecu(pid)) is not None
            and p["status"] in ("active", "challenged")
        ]
        for target_id in [ecu["id"], *pair_ids]:
            brain.update_ecu_status(target_id, "open_question")
        transitions.append({
            "ecu": ecu["id"],
            "pair": pair_ids,
            "scope": ecu["scope"]["level"],
            "competing_since": since,
            "elapsed_days": elapsed_days,
            "limit_days": limit,
        })
    return transitions, len(transitions) + sum(len(t["pair"]) for t in transitions)


def _supersession_pass(brain, cfg, now: datetime) -> tuple[list[dict], list[str]]:
    """§15.5 supersession eligibility: an ECU whose EFFECTIVE confidence has
    decayed below theta_supersede is superseded when a replacement exists —
    a newer, more confident, semantically relevant active ECU (cosine >=
    diffuser.relevance_threshold). Rare by design: decay alone is never
    enough, you need a replacement.

    Per §15.4 the old ECU's confidence is NOT transferred; it is frozen at
    its current value by the superseded status. Dependents of the old ECU
    are challenged via §15.7 propagation (effective < theta_supersede <
    theta_dep_reevaluate).
    """
    theta = cfg.confidence.theta_supersede
    relevance_floor = cfg.diffuser.relevance_threshold
    superseded: list[dict] = []
    challenged_dependents: list[str] = []

    for ecu in brain.list_ecus(status="active") + \
            brain.list_ecus(status="challenged"):
        if brain.get_ecu(ecu["id"]) is None:
            continue                       # vanished mid-pass (defensive)
        effective = ecu_effective_confidence(ecu, now=now, config=cfg)
        if effective >= theta:
            continue
        replacement = _find_replacement(brain, ecu, effective, cfg,
                                        relevance_floor)
        if replacement is None:
            continue                       # decayed but no successor: stays
        similarity = replacement.pop("_similarity")
        stype = supersession_type(
            ecu["cognition"], replacement["cognition"],
            old_embedding=ecu["embedding"], new_embedding=replacement["embedding"],
        )
        brain.add_edge(replacement["id"], ecu["id"], "supersedes",
                       weight=similarity, supersession_type=stype)
        brain.update_ecu_status(ecu["id"], "superseded")
        challenged_dependents.extend(
            propagate_confidence(ecu["id"], effective, brain, cfg))
        superseded.append({
            "old": ecu["id"],
            "replacement": replacement["id"],
            "effective_confidence": round(effective, 4),
            "similarity": round(similarity, 4),
            "supersession_type": stype,
        })
    return superseded, challenged_dependents


def _find_replacement(brain, old_ecu: dict, old_effective: float, cfg,
                      relevance_floor: float) -> dict | None:
    """Newest-fit replacement for a decayed ECU, or None.

    Must be: a different active canonical ECU, semantically relevant
    (cosine >= relevance_floor), newer than the old ECU, and strictly more
    confident than the old ECU's effective value. Highest similarity wins.
    Requires the old ECU to have an embedding — without one semantic
    relevance cannot be established and we conservatively do nothing.
    """
    if old_ecu.get("embedding") is None:
        return None
    for cand in brain.find_similar(
        old_ecu["embedding"],
        threshold=relevance_floor,
        statuses=("active",),
    ):
        if cand["id"] == old_ecu["id"]:
            continue
        replacement = brain.get_ecu(cand["id"])
        if replacement is None:
            continue
        if replacement["provenance"]["created_at"] <= \
                old_ecu["provenance"]["created_at"]:
            continue
        if replacement["confidence"] <= old_effective:
            continue
        return {**replacement, "_similarity": cand["similarity"]}
    return None


def task_forgetting(brain, config, repo_path=None, now=None) -> TaskResult:
    """Controlled forgetting: eager status transitions (D34).

    1. Persistence-limit check (§17.5): ``challenged`` ECUs whose
       ``metadata.competing_since`` exceeds
       ``contradiction.competing_hypothesis_persistence[scope]`` transition —
       together with their contradicts pair — to ``open_question``
       (confidence frozen by status; null limit = indefinite →
       engineering/domain skipped).
    2. Supersession eligibility (§15.5): canonical ECUs whose EFFECTIVE
       confidence (lazy decay) dropped below ``theta_supersede`` AND that
       have a replacement get superseded (supersedes edge + status change +
       §15.7 propagation to dependents).

    Confidence DECAY itself is never written here — it is computed lazily
    at retrieval time (design doc Section 2). Only canonical ECUs are
    touched; session ECUs are ephemeral and never decay.
    """
    now_dt = now or datetime.now(timezone.utc)
    transitions, n_transitioned = _persistence_limit_pass(brain, config, now_dt)
    superseded, challenged_dependents = _supersession_pass(brain, config, now_dt)

    details = {
        "open_question_transitions": transitions,
        "superseded": superseded,
        "challenged_dependents": challenged_dependents,
    }
    ecus_affected = n_transitioned + len(superseded) + len(challenged_dependents)
    return TaskResult("forgetting", ecus_affected, details)


def task_grounding(brain, config, repo_path=None, now=None) -> TaskResult:
    """Grounding verification against the live repository (§21.1, D35).

    Verifies canonical ECUs whose ``grounding.repo_path`` matches
    ``repo_path`` and deprecates those whose grounding references vanished,
    challenging their direct dependents. Throttled to one run per
    ``maintainer.grounding_check_interval_hours`` (72h) per repo via
    maintenance_state. Skipped entirely when no repo is given, the repo is
    not on disk, or the throttle has not elapsed. Implemented in Phase 8
    (design doc Section 3); see ec/grounding.py for the checks.
    """
    from .grounding import GroundingVerifier
    return GroundingVerifier(brain, config, repo_path, now=now).run()


def task_edge_pruning(brain, config, repo_path=None, now=None) -> TaskResult:
    """Edge pruning (§14.5): remove stale edges, reversing their confidence
    deltas (§15.6 — the first caller of ``confidence.reverse_update``).

    Triggers implemented (D52):
    1. target ECU is deprecated/superseded — the edge no longer means
       anything to a live belief;
    2. orphaned edge whose target row is gone (defensive cleanup).

    Reversal rules: ``supports`` subtracts the stored delta from the
    target's confidence; ``contradicts`` adds it back (the stored delta is
    negative); ``depends_on`` just removes (no confidence effect);
    ``supersedes`` edges are NEVER pruned — they are the audit trail
    (§14.5). Weight-decayed/stale-edge pruning stays optional for v2.

    Runs after grounding in TASK_ORDER so edges to freshly deprecated ECUs
    are cleaned up in the same pass.
    """
    pruned = 0
    deltas_reversed = 0
    reversals: list[dict] = []
    orphans_removed = 0

    for edge in brain.list_all_edges():
        if edge["type"] == "supersedes":
            continue                    # §14.5: audit trail is never pruned

        target = brain.get_ecu(edge["target_id"])
        if target is None:
            # Target row already deleted — prune the dangling edge.
            brain.delete_edge(edge["id"])
            pruned += 1
            orphans_removed += 1
            continue

        if target["status"] not in ("deprecated", "superseded"):
            continue                    # live target — edge stays

        delta = edge.get("confidence_delta") or 0.0
        if edge["type"] in ("supports", "contradicts") and delta:
            reverted = reverse_update(target["confidence"], delta)
            brain.update_ecu_confidence(edge["target_id"], reverted)
            deltas_reversed += 1
            reversals.append({
                "edge_id": edge["id"],
                "edge_type": edge["type"],
                "target_id": edge["target_id"],
                "delta": round(delta, 6),
                "old_confidence": round(target["confidence"], 6),
                "new_confidence": round(reverted, 6),
            })
        brain.delete_edge(edge["id"])
        pruned += 1

    return TaskResult(
        "edge_pruning",
        deltas_reversed,
        {
            "edges_pruned": pruned,
            "deltas_reversed": deltas_reversed,
            "orphans_removed": orphans_removed,
            "reversals": reversals,
        },
    )


def task_clustering(brain, config, repo_path=None, now=None) -> TaskResult:
    """HDBSCAN clustering over canonical ECU embeddings (design doc
    Section 4). Live since Phase 9.

    Gated by ``clustering.clustering_threshold`` (100): runs only when that
    many NEW canonical ECUs have been added since the last clustering run
    (maintenance_state ``last_clustering_ecu_count`` baseline; a missing key
    means never clustered — the count starts from zero). The baseline is
    stamped on every non-gated invocation, including ones where the brain
    turned out too small to cluster — the examination happened. The actual
    clustering lives in ec/clustering.py.
    """
    from .clustering import run_clustering

    threshold = int(config.clustering.clustering_threshold)
    last_count_raw = brain.get_maintenance_state(STATE_CLUSTERING_ECU_COUNT)
    if last_count_raw is not None:
        added = brain.count_ecus() - int(last_count_raw)
        if added < threshold:
            return TaskResult("clustering", 0, {
                "skipped": "below_threshold",
                "new_ecus_since_last_run": added,
                "threshold": threshold,
            })

    result = run_clustering(brain, config)
    brain.set_maintenance_state(STATE_CLUSTERING_ECU_COUNT,
                                str(brain.count_ecus()))
    return result


_TASKS = {
    "forgetting": task_forgetting,
    "grounding": task_grounding,
    "edge_pruning": task_edge_pruning,
    "clustering": task_clustering,
}


# ---------------------------------------------------------------------------
# the maintenance run
# ---------------------------------------------------------------------------

def run_maintenance(
    brain, config, repo_path: str | None = None, now: datetime | None = None
) -> MaintenanceResult:
    """Run all maintenance tasks, logging each to maintenance_log.

    Called at MCP-server startup (if overdue) and by the MaintainerThread.
    A failing task is logged with its error and does NOT abort the remaining
    tasks — partial hygiene beats none. Finishes by writing the ``full_run``
    summary row and updating maintenance_state so trigger checks measure
    new work against this baseline.
    """
    now_dt = now or datetime.now(timezone.utc)
    result = MaintenanceResult(started_at=_now_iso())

    for action in TASK_ORDER:
        try:
            task_result = _TASKS[action](brain, config, repo_path=repo_path,
                                         now=now_dt)
        except Exception as exc:            # noqa: BLE001 — resilience over purity
            log.exception("EC Maintainer: task %s failed", action)
            task_result = TaskResult(action, 0, {"error": str(exc)})
        result.tasks.append(task_result)
        brain.log_maintenance(action, json.dumps(task_result.details),
                              ecus_affected=task_result.ecus_affected)

    result.finished_at = _now_iso()
    brain.log_maintenance("full_run", json.dumps(result.summary()),
                          ecus_affected=result.total_ecus_affected)

    # Update the persistent baseline AFTER the tasks run, so the next
    # trigger check counts only ECUs added since this pass.
    brain.set_maintenance_state(STATE_LAST_RUN_AT, result.finished_at)
    brain.set_maintenance_state(STATE_LAST_RUN_ECU_COUNT,
                                str(brain.count_ecus()))
    return result


# ---------------------------------------------------------------------------
# trigger logic
# ---------------------------------------------------------------------------

def is_maintenance_overdue(
    brain, config, now: datetime | None = None
) -> bool:
    """True when a maintenance trigger condition is met (design doc §1.2):

    - time trigger: ``now - last_run_at > maintainer.time_threshold_hours``
    - ECU trigger:  ``count(ecus) - last_run_ecu_count >= maintainer.ecu_threshold``

    Never-run brains are overdue: the first server start performs an initial
    pass that mostly seeds the baseline state. A disabled Maintainer is
    never overdue.
    """
    mcfg = config.maintainer
    if not mcfg.enabled:
        return False
    now_dt = now or datetime.now(timezone.utc)

    last_run_at = brain.get_maintenance_state(STATE_LAST_RUN_AT)
    if last_run_at is None:
        return True
    elapsed_hours = (
        (now_dt - _parse_iso(last_run_at)).total_seconds() / 3600.0
    )
    if elapsed_hours > float(mcfg.time_threshold_hours):
        return True

    last_count = int(brain.get_maintenance_state(STATE_LAST_RUN_ECU_COUNT) or 0)
    return (brain.count_ecus() - last_count) >= int(mcfg.ecu_threshold)


# ---------------------------------------------------------------------------
# background thread
# ---------------------------------------------------------------------------

class MaintainerThread(threading.Thread):
    """Daemon thread checking maintenance triggers on an interval (D33).

    Runs maintenance inside the MCP server process. SQLite WAL allows
    concurrent reads while maintenance writes run sequentially in short
    transactions, so tool handlers are never meaningfully blocked.

    ``db_path``: when given (the production path), the thread opens its OWN
    Brain connection over the database file for the lifetime of the thread
    — sqlite3 connections are thread-bound (``check_same_thread``), so a
    daemon thread must never reuse the server's connection. When ``brain``
    is passed directly (tests, single-threaded use) it is used as-is.
    Exactly one of the two must be available by the time ``run`` starts.

    The first check fires one full interval after start (§1.8: sessions
    shorter than check_interval_minutes see no mid-session maintenance).
    """

    def __init__(self, brain, config, check_interval: float | None = None,
                 repo_path: str | None = None, db_path=None):
        super().__init__(daemon=True, name="ec-maintainer")
        self._brain = brain
        self._config = config
        self._repo_path = repo_path
        self._db_path = db_path
        self._interval = (
            check_interval
            if check_interval is not None
            else float(config.maintainer.check_interval_minutes) * 60.0
        )
        self._stop_event = threading.Event()

    def run(self) -> None:
        from .brain import Brain

        brain = self._brain
        owned = False
        if brain is None and self._db_path is not None:
            brain = Brain(self._db_path)    # thread-private connection
            owned = True
        if brain is None:
            log.error("EC Maintainer: no brain or db_path, thread exiting")
            return
        try:
            while not self._stop_event.wait(self._interval):
                try:
                    if not self._config.maintainer.enabled:
                        continue
                    if is_maintenance_overdue(brain, self._config):
                        log.info("EC Maintainer: triggers met, running maintenance")
                        run_maintenance(brain, self._config,
                                        repo_path=self._repo_path)
                except Exception:
                    # Never crash the thread — a crashed daemon thread
                    # silently stops maintenance forever.
                    log.exception("EC Maintainer: maintenance run failed")
        finally:
            if owned:
                brain.close()

    def stop(self) -> None:
        """Signal the thread to exit after the current wait."""
        self._stop_event.set()
