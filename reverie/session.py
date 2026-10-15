"""Session lifecycle (Spec §28.5) — sessions are automatic per branch.

Sessions are tied to ``(repo_path, branch)`` (§28.5). The global brain stores
everything; a session is metadata tracking which ECUs belong to which working
context. Multiple sessions may be active concurrently — one per (repo,
branch); sessions are never closed by the user: in an enabled repo the
session for the current branch resolves or is created automatically on the
first MCP tool call (feat/flow D56, ``SessionManager.ensure_session``).

Session-creation logic (§28.5):
  1. detect repo + branch (git, or REVERIE_REPO_PATH/REVERIE_BRANCH env override)
  2. active session for (repo, branch) → resume (session ECUs are already in
     the DB; activation scores load with §6.6 time decay)
  3. else create — carrying over unreviewed (pending/skipped) ECUs, with
     their session edges and pending updates, from closed sessions of the
     same (repo, branch)
  4. report session id + brain summary ONCE (awareness without anchoring,
     §11.3 — statistics, never ECU contents)
  5. run the throttled grounding verification for this repo (D35: at most
     once per maintainer.grounding_check_interval_hours; best-effort, never
     blocks or fails the start)

Review is independent of sessions (§29.4): one queue across every session,
branch and repo — ``reverie review`` in a terminal or the
reverie_review_list / reverie_review_decide MCP tools; nothing needs closing.

The ``python -m reverie.session`` CLI exposes ``status`` (the report behind
``reverie status``) plus the bench-only ``bench-start`` / ``bench-close``
hooks (hidden; EC-Bench uses them to measure isolated sessions). The MCP
server (reverie/mcp_server.py) resolves the current session from the
database per call (decision D15).

Labile ECUs (design doc §5.4, D39): the SessionManager keeps an in-memory
set per session of canonical ECU ids surfaced by reverie_query. reverie_reconsolidate
checks this set — only retrieved (labile) ECUs can be reconsolidated.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import review_gate
from .activation import ActivationState
from .brain import Brain, BrainError
from .config import SCOPE_LEVELS, get_config

log = logging.getLogger("reverie.session")

_REPO_ENV, _BRANCH_ENV = "REVERIE_REPO_PATH", "REVERIE_BRANCH"


class SessionError(RuntimeError):
    """Lifecycle failure with actionable guidance (§28.4 message style)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# repo + branch detection (§28.5 session-creation step 1)
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SessionError(
            f"Could not run git from {cwd}: {exc}. EC sessions are tied to "
            "(repo, branch) — git must be installed and this must be a "
            "repository."
        ) from exc
    if proc.returncode != 0:
        raise SessionError(
            f"Could not detect a git repository from {cwd} "
            f"(git {' '.join(args)} failed: {proc.stderr.strip() or 'not a repo'}). "
            "Reverie sessions are tied to (repo, branch) — use it from inside "
            "a git repository."
        )
    return proc.stdout.strip()


def detect_repo_branch(cwd: str | Path | None = None) -> tuple[str, str]:
    """Current ``(repo_path, branch)`` for session routing (§28.5).

    ``REVERIE_REPO_PATH`` + ``REVERIE_BRANCH`` (both) override detection — used by
    tests and by MCP server launches outside a work tree. A detached HEAD
    yields ``detached:<short-hash>`` (a distinct working context).
    """
    env_repo, env_branch = os.environ.get(_REPO_ENV), os.environ.get(_BRANCH_ENV)
    if env_repo and env_branch:
        return str(Path(env_repo).expanduser().resolve()), env_branch

    base = Path(cwd) if cwd else Path.cwd()
    repo_path = str(Path(_git(["rev-parse", "--show-toplevel"], base)).resolve())
    branch = _git(["branch", "--show-current"], Path(repo_path))
    if not branch:  # detached HEAD
        branch = f"detached:{_git(['rev-parse', '--short', 'HEAD'], Path(repo_path))}"
    return repo_path, branch


# ---------------------------------------------------------------------------
# brain summary — reverie_summary payload (§16.3), awareness without anchoring
# ---------------------------------------------------------------------------

def brain_summary(brain: Brain, session_row: dict | None = None,
                  session_brain_count: int | None = None) -> dict:
    """§16.3 reverie_summary body: brain statistics, never individual ECUs."""
    total = brain.count_ecus()
    by_scope_raw = brain.count_ecus_by_scope()
    by_scope = {level: by_scope_raw.get(level, 0) for level in SCOPE_LEVELS}
    by_status = {
        status: brain.count_ecus(status=status)
        for status in ("active", "challenged", "superseded", "open_question")
    }
    last_maintenance = brain.last_maintenance_run()
    last_added = brain.last_ecu_added()
    active = session_row is not None
    if active and session_brain_count is None:
        session_brain_count = brain.count_session_ecus(session_row["id"])
    # §28.10 step 3: pending review count, scoped to the session's working
    # context when one is active, brain-wide otherwise.
    if active:
        pending = brain.pending_review_count(session_row["repo_path"],
                                             session_row["branch"])
    else:
        pending = brain.pending_review_count()
    session_block = {
        "active": active,
        "session_brain_count": session_brain_count if active else 0,
        "started_at": session_row["started_at"] if active else None,
        "repo": session_row["repo_path"] if active else None,
        "branch": session_row["branch"] if active else None,
    }

    nonzero = [f"{level}: {n}" for level, n in by_scope.items() if n]
    message = f"The brain has {total} ECUs"
    if nonzero:
        message += f" ({', '.join(nonzero)})"
    message += (
        f". Last maintenance run: {last_maintenance or 'never'}; "
        f"last ECU added: {last_added or 'never'}."
    )
    if active:
        message += (
            f" Active session on branch '{session_row['branch']}' "
            f"({session_row['repo_path']}) — {session_brain_count} ECUs "
            "in the session brain."
        )
    else:
        message += " No active session."
    if pending:
        message += f" {pending} ECU(s) awaiting review."

    return {
        "canonical_brain": {
            "total_ecus": total,
            "by_scope": by_scope,
            "by_status": by_status,
            "last_maintenance_run": last_maintenance,
            "last_ecu_added": last_added,
            "pending_review_count": pending,
        },
        "session": session_block,
        "message": message,
    }


# ---------------------------------------------------------------------------
# SessionManager — start / resume / stop, activation cache
# ---------------------------------------------------------------------------

class SessionManager:
    """Owns session lifecycle for one brain (§28.5/§28.6).

    The manager caches one ``ActivationState`` per active session (created
    fresh on start, loaded with §6.6 decay on resume/server-restart). The
    database is authoritative: another process (the CLI) can close a
    session and the next ``current_session`` lookup simply misses.

    It also tracks each session's *labile* canonical ECUs (design doc §5.4,
    D39): ECUs retrieved via reverie_query during this session. Like the
    activation state this is session memory, not the database — retrieval
    makes a belief revisable only while the retrieval is in living memory.
    ``reverie_reconsolidate`` refuses ECU ids that are not labile.
    """

    def __init__(self, brain: Brain, config=None, embedding_model=None):
        self.brain = brain
        self.config = config or get_config()
        self.embedding_model = embedding_model
        self._activations: dict[str, ActivationState] = {}
        self._labile: dict[str, set[str]] = {}

    # -- lookups ---------------------------------------------------------

    def current_session(
        self, repo_path: str | None = None, branch: str | None = None
    ) -> dict | None:
        """Active session for (repo, branch) — detected when omitted."""
        if repo_path is None or branch is None:
            repo_path, branch = detect_repo_branch()
        return self.brain.get_active_session(repo_path, branch)

    def activation_for(
        self, session_id: str, now: datetime | None = None
    ) -> ActivationState:
        """The session's activation state (§6.6 decay applied on load)."""
        state = self._activations.get(session_id)
        if state is None:
            state = ActivationState.load(
                self.brain, session_id, config=self.config, now=now
            )
            self._activations[session_id] = state
        return state

    # -- labile ECUs (D39 — §5.4) ------------------------------------------

    def mark_labile(self, session_id: str, ecu_ids) -> None:
        """Record canonical ECUs surfaced by reverie_query as revisable."""
        bucket = self._labile.setdefault(session_id, set())
        bucket.update(ecu_ids)

    def is_labile(self, session_id: str, ecu_id: str) -> bool:
        """True when the ECU was retrieved in this session."""
        return ecu_id in self._labile.get(session_id, set())

    def labile_ecus(self, session_id: str) -> set[str]:
        """The session's labile set (a copy — callers must not mutate)."""
        return set(self._labile.get(session_id, set()))

    # -- automatic sessions (feat/flow, D56) --------------------------------

    def ensure_session(
        self,
        repo_path: str | None = None,
        branch: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Resolve or create the session for (repo, branch) — D56.

        Replaces the D15 rule inside the MCP tools: in an enabled repo any
        Reverie tool call lands in its session without the user ever
        running a tool call. Carry-over of unreviewed candidates is exactly
        session creation's (§28.5 step 5: pending/skipped rows of closed
        sessions of the same (repo, branch) move over). Archived sessions
        (branch deleted) do NOT carry over — their candidates stay in the
        cross-session review queue (reverie/review_queue.py).

        The database stays the source of truth (D15): the MCP server can
        call this on every tool call — an existing session is a single
        indexed lookup.
        """
        if repo_path is None or branch is None:
            repo_path, branch = detect_repo_branch()
        existing = self.brain.get_active_session(repo_path, branch)
        if existing is not None:
            return existing
        self.start_session(repo_path, branch, now=now)
        row = self.brain.get_active_session(repo_path, branch)
        assert row is not None
        return row

    # -- session start (§28.5) --------------------------------------------

    def start_session(
        self,
        repo_path: str | None = None,
        branch: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        if repo_path is None or branch is None:
            repo_path, branch = detect_repo_branch()

        existing = self.brain.get_active_session(repo_path, branch)
        if existing is not None:
            session_id = existing["id"]
            self.activation_for(session_id, now=now)  # §6.6 decayed resume
            self._run_startup_grounding(repo_path, now=now)   # D35
            count = self.brain.count_session_ecus(session_id)
            message = (
                f"Resuming EC session on branch {branch} "
                f"({count} ECUs in session brain)."
            )
            return {
                "status": "ok",
                "session_id": session_id,
                "repo": repo_path,
                "branch": branch,
                "resumed": True,
                "carried_over": 0,
                "session_brain_count": count,
                "started_at": existing["started_at"],
                "message": message,
                "summary": brain_summary(self.brain, existing, count),
            }

        session_id = self.brain.create_session(repo_path, branch)
        closed = [
            s for s in self.brain.list_sessions(repo_path, branch, status="closed")
            if s["id"] != session_id
        ]
        moved = self.brain.reassign_session_rows(
            [s["id"] for s in closed], session_id
        )
        self._activations[session_id] = ActivationState(
            session_id, config=self.config
        )
        count = self.brain.count_session_ecus(session_id)
        repo_name = Path(repo_path).name or repo_path
        message = f"Started EC session on branch {branch} in {repo_name}."
        if moved:
            message += (
                f" ({len(moved)} unreviewed ECU{'s' if len(moved) != 1 else ''} "
                "carried over from a previous session.)"
            )
        row = self.brain.get_session(session_id)
        self._run_startup_grounding(repo_path, now=now)   # D35
        return {
            "status": "ok",
            "session_id": session_id,
            "repo": repo_path,
            "branch": branch,
            "resumed": False,
            "carried_over": len(moved),
            "session_brain_count": count,
            "started_at": row["started_at"],
            "message": message,
            "summary": brain_summary(self.brain, row, count),
        }

    def _run_startup_grounding(
        self, repo_path: str, now: datetime | None = None
    ) -> None:
        """D35: grounding verification on session start for the session's
        repo. Throttled per repo inside the task (72h), so repeated
        start/resume is cheap. Best-effort — a failing check never blocks
        the session; it logs its own maintenance_log row (it bypasses
        run_maintenance) and any error is swallowed after logging."""
        if not self.config.maintainer.enabled:
            return
        try:
            from . import maintainer
            result = maintainer.task_grounding(
                self.brain, self.config, repo_path=repo_path, now=now)
            self.brain.log_maintenance(
                result.action, json.dumps(result.details),
                ecus_affected=result.ecus_affected)
            if result.ecus_affected:
                log.info(
                    "EC grounding: %d ECU(s) affected on %s",
                    result.ecus_affected, repo_path)
        except Exception:
            log.exception("EC: post-start grounding check failed (ignored)")

    # -- session close (review-gate close path, used by the bench hook) ----

    def stop_session(
        self,
        repo_path: str | None = None,
        branch: str | None = None,
        decisions: dict | None = None,
        now: datetime | None = None,
    ):
        """Close the current session through the review gate (§28.6).

        ``decisions`` maps ecu_id -> action (see
        ``review_gate.apply_review_decisions``); ECUs without a decision are
        skipped (the §28.7 'done' path). Returns the ReviewResult.
        """
        row = self.current_session(repo_path, branch)
        if row is None:
            raise SessionError(
                "No active Reverie session for this (repo, branch). In an "
                "enabled repo it resolves automatically on the next tool "
                "call; check that the repo is enabled (`reverie enable`)."
            )
        result = review_gate.apply_review_decisions(
            self.brain,
            row["id"],
            decisions or {},
            config=self.config,
            embedding_model=self.embedding_model,
            now=now,
        )
        self._activations.pop(row["id"], None)
        self._labile.pop(row["id"], None)   # labile state dies with the session
        return result


# ---------------------------------------------------------------------------
# CLI — invoked by the slash-command templates (§28.10): python -m reverie.session
# ---------------------------------------------------------------------------

def _print_report(report: dict, print_fn=print) -> None:
    print_fn(report["message"])
    print_fn(f"  session: {report['session_id']}")
    print_fn(f"  repo:    {report['repo']}")
    print_fn(f"  branch:  {report['branch']}")
    print_fn(report["summary"]["message"])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "bench-close":
        # bench/E2E-only hook — closes the session (never user-facing).
        return _bench_close(argv[1:])
    if argv and argv[0] == "bench-start":
        # bench/E2E-only hook — enable + resolve-or-create the session.
        return _bench_start(argv[1:])
    if argv and argv[0] == "status":
        argv = argv[1:]
    parser = argparse.ArgumentParser(
        prog="python -m reverie.session",
        description="Reverie session report (also `reverie status`); "
                    "bench-only hooks: bench-start | bench-close",
    )
    parser.add_argument("--repo", default=None, help="repo path (default: git detect)")
    parser.add_argument("--branch", default=None, help="branch (default: git detect)")
    args = parser.parse_args(argv)

    try:
        brain = Brain()
    except BrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        manager = SessionManager(brain)
        row = manager.current_session(args.repo, args.branch)
        print(brain_summary(brain, row)["message"])
        return 0
    except (SessionError, review_gate.ReviewGateError, BrainError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        brain.close()


def _bench_start(argv: list[str] | None = None) -> int:
    """Bench/E2E-only lifecycle hook (NOT user-facing): enable the repo and
    resolve-or-create its session (D56) — the harness equivalent of what a
    user gets automatically in an enabled repo. Hidden from help."""
    parser = argparse.ArgumentParser(prog="reverie.session bench-start")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args(argv or [])
    try:
        brain = Brain()
    except BrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        from . import repos
        info = repos.RepoInfo(repo_path=args.repo, branch=args.branch,
                              repo_key=args.repo, url=None, via_git=False)
        repos.set_repo_state(brain, info, "enabled")
        row = SessionManager(brain).ensure_session(args.repo, args.branch)
        print(f"Bench session {row['id']} on {args.branch} in {args.repo}")
        return 0
    except (SessionError, BrainError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        brain.close()


def _bench_close(argv: list[str] | None = None) -> int:
    """Bench/E2E-only lifecycle hook (NOT user-facing): apply review
    decisions to the current session's candidates and CLOSE the session —
    the §28.6 stop semantics the EC-Bench harness needs for per-spec-session
    isolation. The user-facing flow never closes sessions (D56); this
    subcommand exists so the benchmark keeps measuring what it always
    measured. Hidden from help on purpose."""
    parser = argparse.ArgumentParser(prog="reverie.session bench-close")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--review", default="accept",
                        choices=("accept", "skip"))
    args = parser.parse_args(argv or [])
    try:
        brain = Brain()
    except BrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        manager = SessionManager(brain)
        row = manager.current_session(args.repo, args.branch)
        if row is None:
            return 0
        decisions = {
            ecu["id"]: args.review
            for ecu in brain.list_session_ecus(row["id"])
            if ecu["review_status"] in ("pending", "skipped")
        }
        result = review_gate.apply_review_decisions(brain, row["id"],
                                                    decisions)
        print(result.message)
        warning = review_gate.diffusion_failure_warning(result)
        if warning:
            print(warning)
        manager._activations.pop(row["id"], None)
        return 0
    except (SessionError, review_gate.ReviewGateError, BrainError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        brain.close()


if __name__ == "__main__":
    sys.exit(main())
