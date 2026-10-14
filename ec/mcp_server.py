"""EC MCP server (Spec §28.3) — stdio transport, newline-delimited JSON-RPC 2.0.

Runs as a local subprocess of the coding agent: reads JSON-RPC messages from
stdin (one per line, NDJSON per the MCP stdio transport), writes responses to
stdout. All logging goes to stderr — stdout is protocol-only.

Implemented protocol subset (D14 — hand-rolled; the ``mcp`` SDK is not in the
pinned venv): ``initialize``, ``notifications/initialized`` (ignored), ``ping``,
``tools/list``, ``tools/call``. Tool results carry the §16.3/§28.11 structured
payload as JSON text; ``isError`` mirrors the payload's ``status == "error"``.

Tools (feat/flow surface — the §28.3 four plus review/enablement, D55-D57):
  - ``ec_observe``       — extract + lightweight-diffuse reasoning into the
                           Session Brain (§28.13)
  - ``ec_query``         — demand-driven retrieval over both brains (§11.3)
  - ``ec_get_summary``   — brain statistics at session start (§11.3 awareness
                           without anchoring)
  - ``ec_reconsolidate`` — re-evaluate a retrieved ECU with new evidence
                           (design doc §5.2/D37; writes to the Canonical
                            Brain immediately — the ECU was reviewed when it
                            entered). Only ECUs retrieved in this session are
                            labile (D39): the server marks surfaced canonical
                            ids during ec_query and refuses others.
  - ``ec_set_repo_state``— record the user's answer to the first-time
                           enablement question (or turn Reverie off/on here)
  - ``ec_review_list``   — the review queue page, for the agent to relay
  - ``ec_review_decide`` — apply decisions the USER stated, through the
                           review gate (channel "agent", D57)

Repo enablement (feat/flow, D55): every tool call first resolves the repo
(the tool's optional ``repo_path`` argument — the agent's workspace path —
falls back to the server's working directory; some harnesses start global
MCP servers in the home folder). An ``undecided`` repo gets a short,
structured ask-the-user response instead of action; a ``disabled`` repo
gets a structured "off" response and NOTHING happens (no capture, no
retrieval); an ``enabled`` repo proceeds.

Automatic sessions (feat/flow, D56 — replaces D15 for the MCP surface):
in an enabled repo every tool call resolves or creates the session for
(repo, branch). The database stays the source of truth (WAL keeps the
CLI/MCP processes consistent); sessions are never closed by the user —
review is independent of sessions (``ec_review_list``/``ec_review_decide``
and the ``ec review`` TUI work across every session, branch and repo).

After each successful ec_query the server writes retrieval metadata
(``last_retrieved`` / ``retrieval_count``, canonical ECUs only — D17) and
applies retrieval reinforcement (design doc §2.1): the STORED confidence is
bumped by ``alpha_retrieval`` in log-odds space and ``last_reinforced`` is
reset to now — resetting the lazy-decay clock. Frozen-status ECUs
(open_question, superseded, deprecated, archived) keep their stamped
bookkeeping but skip the confidence bump: §17.5 freezes parked beliefs.
The server also saves the session's activation state (D22, §6.6 resume
fidelity).

Cognition Maintainer (D33): before the JSON-RPC loop starts, the server
runs maintenance synchronously if it is overdue (time or ECU-count
trigger), then launches the daemon MaintainerThread for the lifetime of
the process. Maintenance failures never prevent the server from starting.

Grounding verification (D35): the maintenance run receives a repo path so
the grounding task can verify ECUs against the live working tree. It is
resolved once at startup — ``EC_REPO_PATH`` env override (with
``EC_BRANCH``), else git toplevel of the server's cwd, else the most
recently started active session's repo from the database (the server may
be launched outside any work tree) — and reused by both the startup run
and the background thread. Grounding runs are additionally throttled per
repo (72h) inside the task itself.

commit_hash provenance (D49, design doc §4): every ec_observe stamps the
session repo's git HEAD into each extracted ECU's grounding before storage,
so §3.3's informational commit-staleness check has input.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

from . import (
    __version__,
    diffuser,
    extractor,
    maintainer,
    pending,
    reconsolidation,
    repos,
    retrieval,
    review_queue,
)
from .brain import Brain, BrainError, _now_iso
from .confidence import FROZEN_STATUSES, reinforce_stored_confidence
from .config import MODES, SCOPE_LEVELS, get_config
from .diffuser import DiffuserError
from .extractor import ExtractorError
from .reconsolidation import ReconsolidationError
from .session import SessionError, SessionManager, brain_summary, detect_repo_branch

log = logging.getLogger("ec.mcp_server")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ec"

# §28.4-style messages — verbatim, actionable (feat/flow).
NO_REPO_ERROR = (
    "Reverie could not locate a git repository for this call. Pass the "
    "workspace path as the tool's 'repo_path' argument (the directory you "
    "are working in) and try again."
)
EXTRACTION_FAILED = (
    "Extraction failed: {reason}. The reasoning trace may be too short or "
    "contain no engineering conclusions. This is not an error — not every "
    "response produces ECUs."
)
NO_ECUS_FOUND = (
    "No ECUs found for query '{query}'. Try broadening the scope or "
    "rephrasing. The brain may not have relevant cognition for this topic yet."
)

#: How long a review_token stays valid for ec_review_decide (feat/flow).
REVIEW_TOKEN_TTL_SECONDS = 3600

#: D49 (design doc §4): how long to wait for ``git rev-parse HEAD`` when
#: stamping grounding provenance at observe time.
COMMIT_HASH_TIMEOUT = 5


def _current_commit_hash(repo_path: str | None) -> str | None:
    """D49 — the session repo's git HEAD as a short hash, or None.

    Provenance is stamped server-side at observe time (the agent never has
    to know the commit hash — the system does). Any failure — no repo_path,
    not a git work tree, git missing, timeout — degrades to None and the
    ECU simply carries no commit anchor; extraction itself must never fail
    because of this.
    """
    if not repo_path:
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path), capture_output=True, text=True,
            timeout=COMMIT_HASH_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    short = proc.stdout.strip()[:12]
    return short or None

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# ---------------------------------------------------------------------------
# Tool definitions — descriptions verbatim from §28.3 (§28.12: they are the
# primary way the agent learns how to use EC).
# ---------------------------------------------------------------------------

_OBSERVE_DESCRIPTION = """\
Extract engineering conclusions from your reasoning trace and store them
in the Session Brain for immediate use. Call this AFTER you:
- Discover a non-obvious invariant or dependency
- Debug a tricky issue and find the root cause
- Make an architectural decision with rationale
- Discover that a previous assumption was wrong

Do NOT call ec_observe for:
- Simple code changes with no insight
- Things already captured in existing ECUs
- Pure factual observations (the Extractor handles the lifting to conclusions)

ec_observe runs the Extractor on your reasoning, produces candidate ECUs,
and stores them in the Session Brain. They are immediately available for
retrieval via ec_query. The USER reviews these candidates (via `ec review`
or by asking you to show them) — never promote or judge them yourself.

Sessions are automatic: in an enabled repo the session for the current
branch resolves or is created by this call. If the response asks you to
turn Reverie on for this repo, ask the user the question it contains
exactly once per conversation, and record their answer with
ec_set_repo_state. Pass the workspace path as 'repo_path' on every call."""

_QUERY_DESCRIPTION = """\
Retrieve engineering conclusions relevant to your current task. Call this
AFTER reading the relevant code files, when you recognize a gap that past
cognition might fill:
- Before making a decision between multiple approaches
- When debugging and the cause isn't obvious from the code alone
- Before an architectural change (query for existing architectural decisions)

Returns ECUs from both the Session Brain (current branch's working memory)
and the Canonical Brain (accumulated understanding from past sessions).
Each ECU includes confidence, scope, grounding references, and status.

Memory is framed as "past beliefs to verify against current code," not
authoritative context. Always verify grounding references against the
live codebase before acting on an ECU.

Sessions are automatic: in an enabled repo the session for the current
branch resolves or is created by this call. Pass the workspace path as
'repo_path' on every call."""

_SUMMARY_DESCRIPTION = """\
Get a summary of what the EC brain knows. Call this ONCE at session start
to understand what engineering cognition is already stored before you begin
work. Returns brain statistics and scope distribution — not individual ECUs.

If the response contains a pending-review nudge, mention it to the user at
most once per conversation (counts only, never contents)."""

_REPO_STATE_DESCRIPTION = """\
Record the user's answer about enabling Reverie for the current repo, or
turn Reverie on/off for it. Call this with:
- state="enabled"   — the user said yes (or wants it on)
- state="not_now"   — the user said "not now": stays off for now; do not
                      ask again in this conversation
- state="disabled"  — the user said never (or wants it off): fully off for
                      this repo, existing memory is kept
- state="undecided" — reset to the not-configured state
Pass the workspace path as 'repo_path'. The answer is remembered globally —
never stored inside the repo."""

_REVIEW_LIST_DESCRIPTION = """\
List the user's pending Reverie review candidates (one queue across every
branch and repo, strongest first). Use this when the user asks to see
their Reverie candidates or wants to review memory. Show EVERY candidate's
full conclusion text to the user before asking for their decisions, relay
only the decisions the user actually states, and never decide on their
behalf. Returns a short-lived review_token — pass it to ec_review_decide.
Pass the workspace path as 'repo_path' to scope to the current repo."""

_REVIEW_DECIDE_DESCRIPTION = """\
Apply review decisions the USER stated in the chat (accept | reject | skip,
optionally with edited_cognition they dictated). Requires the review_token
from ec_review_decide's sibling ec_review_list; only candidates that were
in that listing can be decided. Bulk-accepting every candidate is refused
— reverie review must stay per-candidate or per-group, on the user's
explicit instruction. Pass the workspace path as 'repo_path'."""

_RECONSOLIDATE_DESCRIPTION = """\
Re-evaluate a previously retrieved ECU in light of new evidence you discovered
during this session. Call this when you find that a past engineering conclusion
is wrong, outdated, or needs refinement based on what you've learned from the
current codebase.

This makes the ECU "labile" (revisable) and feeds the new evidence through the
Diffuser, which may update the ECU's confidence, create new edges, or trigger
supersession. The change is written to the Canonical Brain immediately (the ECU
was already human-reviewed when it entered the Canonical Brain — reconsolidation
is a confidence/evidence update, not a new ECU entering the brain).

Call ec_reconsolidate when:
- You verified a retrieved ECU against the code and found it's no longer accurate
- You discovered new evidence that strengthens a retrieved ECU (it should be
  more confident)
- You discovered new evidence that contradicts a retrieved ECU (it should be
  less confident or superseded)
- The code an ECU is grounded in has changed, and the conclusion needs updating

Do NOT call ec_reconsolidate for:
- New insights that aren't related to a previously retrieved ECU (use ec_observe)
- ECUs you haven't retrieved in this session (reconsolidation requires prior
  retrieval — you can't reconsolidate something you haven't looked at)
- Simple code changes with no impact on engineering conclusions"""

def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


_REPO_PATH_PROP = {
    "type": "string",
    "description": (
        "The workspace path you are working in (the repo directory). Pass "
        "it explicitly — global MCP servers may be started in the home "
        "folder, where the repo cannot be detected."
    ),
}


def _observe_schema() -> dict:
    return _schema({
        "user_prompt": {
            "type": "string",
            "description": (
                "The user's prompt that triggered this response. The "
                "Extractor processes the full interaction — prompt, "
                "reasoning, and output — not just the reasoning alone."
            ),
        },
        "reasoning_trace": {
            "type": "string",
            "description": (
                "Your reasoning trace since the last ec_observe call. "
                "Include your thinking, decisions, and rationale — not "
                "just the final code output."
            ),
        },
        "final_output": {
            "type": "string",
            "description": (
                "Your final output (code, explanation, plan, etc.). If "
                "the output is just a code diff with no explanation, "
                "this may be omitted."
            ),
        },
        "repo_path": dict(_REPO_PATH_PROP),
    }, ["user_prompt", "reasoning_trace"])


def _query_schema() -> dict:
    return _schema({
        "query": {
            "type": "string",
            "description": (
                "What you want to know. Natural language. Example: "
                "'How does the authentication token refresh work?'"
            ),
        },
        "scope": {
            "type": "string",
            "enum": [
                "engineering", "domain", "project",
                "repo", "module", "subsystem",
            ],
            "description": (
                "Restrict search scope. If omitted, searches all "
                "scopes (with scope proximity weighting)."
            ),
        },
        "mode": {
            "type": "string",
            "enum": list(MODES),
            "description": (
                "Retrieval mode. If omitted, uses default mode "
                "(broad retrieval)."
            ),
        },
        "repo_path": dict(_REPO_PATH_PROP),
    }, ["query"])


def _summary_schema() -> dict:
    return _schema({"repo_path": dict(_REPO_PATH_PROP)}, [])


def _reconsolidate_schema() -> dict:
    return _schema({
        "ecu_id": {
            "type": "string",
            "description": (
                "The ID of the ECU to reconsolidate. Must be a "
                "canonical ECU that was retrieved in this session."
            ),
        },
        "evidence": {
            "type": "string",
            "description": (
                "The new evidence you discovered. This should be "
                "your reasoning trace explaining what you found and "
                "why it affects the ECU. The Diffuser will use this "
                "to re-evaluate the ECU's confidence and edges."
            ),
        },
        "relationship": {
            "type": "string",
            "enum": list(reconsolidation.VALID_RELATIONSHIPS),
            "description": (
                "How does the new evidence relate to the ECU? If "
                "omitted, the Diffuser will classify the relationship."
            ),
        },
        "repo_path": dict(_REPO_PATH_PROP),
    }, ["ecu_id", "evidence"])


def _set_repo_state_schema() -> dict:
    return _schema({
        "state": {
            "type": "string",
            "enum": ["enabled", "disabled", "not_now", "undecided"],
            "description": (
                "The user's answer: yes -> enabled; not now -> not_now; "
                "never -> disabled."
            ),
        },
        "repo_path": dict(_REPO_PATH_PROP),
    }, ["state"])


def _review_list_schema() -> dict:
    return _schema({
        "repo_path": dict(_REPO_PATH_PROP),
        "all": {
            "type": "boolean",
            "description": (
                "List candidates from every repo instead of just the "
                "current one."
            ),
        },
        "by_topic": {
            "type": "boolean",
            "description": (
                "Group by topic instead of strongest-first ordering."
            ),
        },
        "page": {
            "type": "integer",
            "description": "1-based page number (default 1).",
        },
    }, [])


def _review_decide_schema() -> dict:
    return _schema({
        "review_token": {
            "type": "string",
            "description": "The token from ec_review_list.",
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string",
                           "description": "Candidate id from the listing."},
                    "action": {
                        "type": "string",
                        "enum": ["accept", "reject", "skip"],
                    },
                    "edited_cognition": {
                        "type": "string",
                        "description": (
                            "Only when the user dictated a rewording."
                        ),
                    },
                },
                "required": ["id", "action"],
                "additionalProperties": False,
            },
            "description": "The decisions the USER stated.",
        },
        "repo_path": dict(_REPO_PATH_PROP),
    }, ["review_token", "decisions"])


TOOLS = [
    {
        "name": "ec_observe",
        "description": _OBSERVE_DESCRIPTION,
        "inputSchema": _observe_schema(),
    },
    {
        "name": "ec_query",
        "description": _QUERY_DESCRIPTION,
        "inputSchema": _query_schema(),
    },
    {
        "name": "ec_get_summary",
        "description": _SUMMARY_DESCRIPTION,
        "inputSchema": _summary_schema(),
    },
    {
        "name": "ec_reconsolidate",
        "description": _RECONSOLIDATE_DESCRIPTION,
        "inputSchema": _reconsolidate_schema(),
    },
    {
        "name": "ec_set_repo_state",
        "description": _REPO_STATE_DESCRIPTION,
        "inputSchema": _set_repo_state_schema(),
    },
    {
        "name": "ec_review_list",
        "description": _REVIEW_LIST_DESCRIPTION,
        "inputSchema": _review_list_schema(),
    },
    {
        "name": "ec_review_decide",
        "description": _REVIEW_DECIDE_DESCRIPTION,
        "inputSchema": _review_decide_schema(),
    },
]


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

class ECServer:
    """One MCP server bound to one brain. Holds no session state of its own
    beyond the SessionManager's activation cache — the database is the source
    of truth (D15), so CLI-driven /ec-stop takes effect immediately."""

    def __init__(self, brain: Brain | None = None, manager=None, config=None,
                 embedding_model=None):
        self.config = config or get_config()
        self._brain = brain
        self._manager = manager
        self._embedding_model = embedding_model
        self._maintainer_thread: maintainer.MaintainerThread | None = None
        # feat/flow: short-lived review tokens for the agent review channel
        # (D57): token -> {"ids": [...], "created_at": iso}
        self._review_tokens: dict[str, dict] = {}

    # -- lazy resources ----------------------------------------------------

    def _ensure_brain(self) -> Brain:
        if self._brain is None:
            self._brain = Brain()  # may raise BrainError (§28.4 guidance)
        return self._brain

    def _ensure_manager(self) -> SessionManager:
        if self._manager is None:
            self._manager = SessionManager(self._ensure_brain(),
                                           config=self.config)
        return self._manager

    @property
    def embedding_model(self):
        if self._embedding_model is None:
            from .embeddings import get_embedding_model
            self._embedding_model = get_embedding_model()
        return self._embedding_model

    # -- Cognition Maintainer (D33) ------------------------------------------

    def start_maintenance(self, repo_path: str | None = None):
        """Startup check: run overdue maintenance synchronously, then start
        the background MaintainerThread. Called once before the JSON-RPC
        loop; returns the MaintenanceResult of the startup run (if any) so
        tests/callers can inspect it. Never raises — a failing startup run
        must not prevent the server from serving."""
        mcfg = self.config.maintainer
        if not mcfg.enabled:
            log.info("EC Maintainer: disabled by config, skipping")
            return None
        brain = self._ensure_brain()
        ran = None
        try:
            if maintainer.is_maintenance_overdue(brain, self.config):
                log.info("EC Maintainer: overdue at startup, running now")
                ran = maintainer.run_maintenance(brain, self.config,
                                                 repo_path=repo_path)
        except Exception:
            log.exception("EC Maintainer: startup run failed; continuing")
        # The daemon thread gets its OWN connection over the same DB file
        # (sqlite3 connections are thread-bound; WAL keeps reads concurrent).
        self._maintainer_thread = maintainer.MaintainerThread(
            None, self.config, repo_path=repo_path, db_path=brain.db_path,
        )
        self._maintainer_thread.start()
        return ran

    def stop_maintenance(self) -> None:
        """Stop the background thread (process shutdown)."""
        if self._maintainer_thread is not None:
            self._maintainer_thread.stop()
            self._maintainer_thread.join(timeout=5)
            self._maintainer_thread = None

    # -- JSON-RPC plumbing ---------------------------------------------------

    @staticmethod
    def _result(msg_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}

    def handle_line(self, line: str) -> dict | None:
        """Parse + dispatch one NDJSON line. None → no response (notification)."""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            return self._error(None, PARSE_ERROR, f"Parse error: {exc}")
        return self.handle_message(msg)

    def handle_message(self, msg: dict) -> dict | None:
        if not isinstance(msg, dict) or "method" not in msg:
            return self._error(msg.get("id") if isinstance(msg, dict) else None,
                               INVALID_REQUEST, "Invalid Request")
        if "id" not in msg:
            return None  # notifications (initialized, cancelled…) never answered
        msg_id, method = msg["id"], msg["method"]

        if method == "initialize":
            return self._result(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            })
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._handle_tool_call(msg_id, msg.get("params") or {})
        return self._error(msg_id, METHOD_NOT_FOUND,
                           f"Method not found: {method!r}")

    def _handle_tool_call(self, msg_id, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = {
            "ec_observe": self._tool_observe,
            "ec_query": self._tool_query,
            "ec_get_summary": self._tool_summary,
            "ec_reconsolidate": self._tool_reconsolidate,
            "ec_set_repo_state": self._tool_set_repo_state,
            "ec_review_list": self._tool_review_list,
            "ec_review_decide": self._tool_review_decide,
        }.get(name)
        if handler is None:
            return self._error(
                msg_id, INVALID_PARAMS,
                f"Unknown tool {name!r}. Available tools: ec_observe, "
                "ec_query, ec_get_summary, ec_reconsolidate.",
            )
        try:
            payload = handler(arguments)
        except (BrainError, SessionError) as exc:
            payload = {"status": "error", "session_active": False,
                       "error": str(exc)}
        except Exception as exc:  # never crash the stdio loop
            log.exception("tool %s failed", name)
            return self._error(msg_id, INTERNAL_ERROR,
                               f"Internal error running {name}: {exc}")
        return self._result(msg_id, {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            # feat/flow: gate responses (needs_consent / disabled) did not
            # act — surface them as errors so the agent cannot miss them.
            "isError": payload.get("status") in
                       ("error", "needs_consent", "disabled"),
        })

    # -- shared helpers ------------------------------------------------------

    def _resolve_repo(self, args: dict):
        """Locate the repo for this call (feat/flow, D55).

        The tool's optional ``repo_path`` argument (the agent's workspace
        path) wins — some harnesses start global MCP servers in the home
        folder, where cwd is useless. Falls back to the server's working
        directory (and the EC_REPO_PATH/EC_BRANCH overrides inside
        repos.resolve_repo). Returns ``(repos.RepoInfo, None)`` or
        ``(None, error_payload)``."""
        repo_arg = args.get("repo_path")
        if repo_arg is not None and not isinstance(repo_arg, str):
            return None, {"status": "error", "error_kind": "no_repo",
                          "error": "repo_path must be a string path"}
        try:
            info = repos.resolve_repo(cwd=repo_arg if repo_arg else None)
        except SessionError as exc:
            return None, {
                "status": "error", "error_kind": "no_repo",
                "error": NO_REPO_ERROR,
                "detail": str(exc),
            }
        return info, None

    def _gate(self, args: dict, *, require_repo: bool = True):
        """The enablement gate (D55) — runs before every tool acts.

        Returns ``(gate_payload, context)``: exactly one is None.

        - repo unresolvable and required → error payload (no context)
        - ``undecided`` repo → short structured ask-the-user response; the
          ask is recorded (``asked_at``) so the agent's once-per-
          conversation rule has a timestamp to anchor on
        - ``disabled`` repo → structured off-response; NOTHING happens
        - ``enabled`` repo → context with the RepoInfo; missing-branch
          sessions are archived here (D56) so the queue never shows
          zombie branches as active
        """
        info, error = self._resolve_repo(args)
        if error is not None:
            if not require_repo:
                return None, None          # e.g. summary works repo-less
            return error, None
        brain = self._ensure_brain()
        state_row = repos.resolve_repo_state(brain, info)
        state = state_row["state"] if state_row else "undecided"
        if state == "undecided":
            repos.mark_asked(brain, info)
            return {
                "status": "needs_consent",
                "state": "undecided",
                "repo": info.repo_path,
                "question": repos.FIRST_TIME_QUESTION,
                "error": repos.FIRST_TIME_QUESTION,
                "instructions": (
                    "Ask the user this exact question once per conversation. "
                    "Record their answer with ec_set_repo_state: 'yes' -> "
                    "enabled, 'not now' -> not_now, 'never' -> disabled. If "
                    "they say 'not now', do not ask again in this "
                    "conversation and continue without Reverie."
                ),
            }, None
        if state == "disabled":
            return {
                "status": "disabled",
                "state": "disabled",
                "repo": info.repo_path,
                "error": "Reverie is disabled for this repo. Nothing was "
                         "captured or retrieved. The user can re-enable it "
                         "any time with `ec enable` (existing memory is "
                         "kept).",
            }, None
        # enabled: archive sessions whose branch vanished (D56, best-effort)
        if info.via_git:
            try:
                repos.archive_missing_branch_sessions(
                    brain, info.repo_path, skip_branch=info.branch)
            except Exception:
                log.exception("archive-on-lookup failed; continuing")
        return None, {"info": info}

    def _enabled_session(self, info: repos.RepoInfo) -> dict:
        """D56: the session for (repo, branch), resolved or created."""
        return self._ensure_manager().ensure_session(info.repo_path,
                                                     info.branch)

    def _record_retrieval_metadata(self, result: dict,
                                   session_id: str | None = None) -> int:
        """§17.5 decay clock (D17) + retrieval reinforcement (design doc
        §2.1): stamp canonical ECUs actually surfaced — group cores and all
        neighbour slots — with last_retrieved / retrieval_count, bump the
        STORED confidence by alpha_retrieval, and reset last_reinforced to
        now (the lazy-decay clock restarts). Session ECUs are ephemeral: not
        stamped. Frozen-status ECUs keep the stamp but skip the bump — §17.5
        freezes parked beliefs; reinforcement would unfreeze them by stealth.

        With ``session_id``, surfaced canonical ECUs are also marked labile
        (D39, design doc §5.4): only these may be reconsolidated.
        """
        brain = self._ensure_brain()
        now = _now_iso()
        ids: list[str] = []
        for group in result.get("groups", []):
            core = group.get("core_ecu", {})
            if core.get("brain") == "canonical":
                ids.append(core["id"])
            for slot in ("supporting_evidence", "contradictions",
                         "dependencies", "superseded_by"):
                for item in group.get(slot, []):
                    if item.get("brain") == "canonical":
                        ids.append(item["id"])
        stamped = 0
        for ecu_id in dict.fromkeys(ids):
            ecu = brain.get_ecu(ecu_id)
            if ecu is None:
                continue
            meta = ecu.get("metadata") or {}
            brain.update_ecu_metadata(
                ecu_id,
                last_retrieved=now,
                last_reinforced=now,
                retrieval_count=int(meta.get("retrieval_count") or 0) + 1,
            )
            if ecu["status"] not in FROZEN_STATUSES:
                brain.update_ecu_confidence(
                    ecu_id, reinforce_stored_confidence(
                        ecu["confidence"], config=self.config)
                )
            stamped += 1
        if session_id and ids:
            self._ensure_manager().mark_labile(session_id, dict.fromkeys(ids))
        return stamped

    # -- tool handlers ---------------------------------------------------------

    def _tool_observe(self, args: dict) -> dict:
        """§28.13 flow: extract → store in Session Brain → lightweight diffuse."""
        gate, ctx = self._gate(args)
        if gate is not None:
            return gate
        info = ctx["info"]
        row = self._enabled_session(info)
        user_prompt = args.get("user_prompt")
        trace = args.get("reasoning_trace")
        if not isinstance(user_prompt, str) or not user_prompt.strip() \
                or not isinstance(trace, str) or not trace.strip():
            return {
                "status": "error",
                "session_active": True,
                "error": "ec_observe requires 'user_prompt' and "
                         "'reasoning_trace' (non-empty strings). Pass the "
                         "user's prompt and your reasoning since the last "
                         "ec_observe call.",
            }
        interaction = {
            "prompt": user_prompt,
            "reasoning_trace": trace,
            "session_context": {
                "session_id": row["id"],
                "repo": row["repo_path"],
                "branch": row["branch"],
            },
        }
        output = args.get("final_output")
        if isinstance(output, str) and output.strip():
            interaction["output"] = output

        brain = self._ensure_brain()
        commit_hash = _current_commit_hash(row["repo_path"])  # D49
        try:
            result, ids = extractor.extract_and_store_session(
                brain, row["id"], interaction,
                embedding_model=self.embedding_model, config=self.config,
                commit_hash=commit_hash,
            )
        except ExtractorError as exc:
            # feat/providers: the interaction is persisted so the failure
            # costs nothing — it is retried at the next successful
            # ec_observe (or via `ec-retry`).
            pending_id = pending.save_pending_extraction(
                brain, row, interaction, str(exc),
                commit_hash=commit_hash,
            )
            error = EXTRACTION_FAILED.format(reason=exc) + (
                f" The interaction was saved locally (pending id "
                f"{pending_id[:8]}) and will be retried automatically at "
                "the next successful ec_observe, or via `ec-retry`."
            )
            return {"status": "error", "session_active": True,
                    "error": error, "pending_extraction_id": pending_id}

        diffusion_failures = 0
        for ecu_id in ids:
            try:
                diffuser.diffuse_session_ecu(
                    brain, row["id"], ecu_id,
                    embedding_model=self.embedding_model, config=self.config,
                )
            except DiffuserError as exc:
                # Extraction succeeded — the ECUs are stored and retrievable;
                # lightweight diffusion is best-effort (offline-safe).
                log.warning("lightweight diffusion failed for %s: %s",
                            ecu_id, exc)
                diffusion_failures += 1

        # feat/providers: this observe succeeded — opportunistically retry
        # any pending extractions for this (repo, branch). Best-effort.
        retry_summary = pending.retry_pending(
            brain, repo_path=row["repo_path"], branch=row["branch"],
            embedding_model=self.embedding_model, config=self.config,
        )

        count = brain.count_session_ecus(row["id"])
        if ids:
            message = (
                f"Extracted {len(ids)} ECU{'s' if len(ids) != 1 else ''} into "
                f"the Session Brain ({result.rejected_count} rejected by the "
                f"Lifting Test). Session brain now holds {count} ECUs."
            )
        else:
            message = (
                "No engineering conclusions found in this response. This is "
                "not an error — not every response produces ECUs."
            )
        if retry_summary["recovered"]:
            message += (
                f" {retry_summary['recovered']} previously failed "
                "extraction(s) were also recovered into this session."
            )
        elif retry_summary["failed"] or retry_summary["skipped"]:
            message += (
                f" {retry_summary['failed'] + retry_summary['skipped']} "
                "pending extraction(s) could not be recovered yet "
                "(retry at the next observe, or via `ec-retry`)."
            )
        return {
            "status": "ok",
            "session_active": True,
            "ecus_extracted": len(ids),
            "ecus_rejected": result.rejected_count,
            "rejection_summary": result.rejection_summary,  # §5.3 — visible filtering
            "session_brain_count": count,
            "message": message,
            "skipped_invalid": result.skipped_invalid,
            "diffusion_failures": diffusion_failures,
            "pending_recovered": retry_summary["recovered"],
            "pending_failed": retry_summary["failed"] + retry_summary["skipped"],
        }

    def _tool_query(self, args: dict) -> dict:
        """The §11.3 demand-driven retrieval path (ec_query)."""
        gate, ctx = self._gate(args)
        if gate is not None:
            return gate
        info = ctx["info"]
        row = self._enabled_session(info)
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return {
                "status": "error",
                "session_active": True,
                "error": "ec_query requires a 'query' parameter (natural "
                         "language). Example: 'How does the authentication "
                         "token refresh work?'",
            }
        mode, scope = args.get("mode"), args.get("scope")
        brain = self._ensure_brain()
        manager = self._ensure_manager()
        activation = manager.activation_for(row["id"])
        try:
            result = retrieval.retrieve(
                brain, query, mode=mode, scope=scope, session_id=row["id"],
                activation=activation, config=self.config,
                embedding_model=self.embedding_model,
            )
        except ValueError as exc:
            return {
                "status": "error",
                "session_active": True,
                "error": f"{exc} Valid modes: {list(MODES)}; valid scopes: "
                         f"{list(SCOPE_LEVELS)}.",
            }
        self._record_retrieval_metadata(result,      # D17 — §17.5 decay clock
                                        session_id=row["id"])
        activation.save(brain)                       # D22 — §6.6 resume
        payload = {**result, "session_active": True}
        if result["groups_retrieved"] == 0:
            payload["message"] = NO_ECUS_FOUND.format(query=query)
        return payload

    def _tool_summary(self, args: dict) -> dict:
        """§16.3 ec_get_summary — the nudge line included (feat/flow §4).

        Gated on enablement when a repo resolves; works repo-less (a
        global server in the home folder still reports brain stats)."""
        gate, ctx = self._gate(args, require_repo=False)
        if gate is not None:
            return gate
        row = None
        if ctx is not None:
            info = ctx["info"]
            row = self._enabled_session(info)
        brain = self._ensure_brain()
        payload = {"status": "ok", **brain_summary(brain, row)}
        nudge = review_queue.nudge_line(brain, config=self.config)
        if nudge:
            payload["review_nudge"] = nudge
            payload["message"] = payload["message"] + " " + nudge
        return payload

    def _tool_reconsolidate(self, args: dict) -> dict:
        """D37 — ec_reconsolidate (design doc §5.2): re-evaluate a labile
        canonical ECU with new evidence. Writes to the Canonical Brain
        immediately; the §5.2 payload is returned verbatim."""
        gate, ctx = self._gate(args)
        if gate is not None:
            return gate
        info = ctx["info"]
        row = self._enabled_session(info)

        ecu_id = args.get("ecu_id")
        evidence = args.get("evidence")
        relationship = args.get("relationship")

        if not isinstance(ecu_id, str) or not ecu_id.strip():
            return {
                "status": "error",
                "session_active": True,
                "error": "ec_reconsolidate requires 'ecu_id' — the id exactly "
                         "as it appeared in the ec_query result.",
            }
        if relationship is not None \
                and relationship not in reconsolidation.VALID_RELATIONSHIPS:
            return {
                "status": "error",
                "session_active": True,
                "error": f"Invalid relationship {relationship!r}. Use one of "
                         f"{list(reconsolidation.VALID_RELATIONSHIPS)} or omit "
                         "it to let the Diffuser classify the evidence.",
            }

        # D39 soft gate: only ECUs retrieved in this session are labile.
        if not self._ensure_manager().is_labile(row["id"], ecu_id.strip()):
            return {
                "status": "error",
                "session_active": True,
                "ecu_id": ecu_id,
                "error": f"ECU {ecu_id} was not retrieved in this session, so "
                         "it cannot be reconsolidated — you can't "
                         "reconsolidate something you haven't looked at. Call "
                         "ec_query first; if the ECU appears in the result, "
                         "call ec_reconsolidate with its id.",
            }

        brain = self._ensure_brain()
        try:
            r = reconsolidation.reconsolidate(
                brain, ecu_id.strip(), evidence,
                relationship=relationship,
                embedding_model=self.embedding_model,
                config=self.config,
                session_id=row["id"],
            )
        except ReconsolidationError as exc:
            return {"status": "error", "session_active": True,
                    "ecu_id": ecu_id, "error": str(exc)}
        except DiffuserError as exc:
            # classification failed offline and no explicit relationship was
            # given — nothing was written; tell the agent how to proceed.
            return {
                "status": "error",
                "session_active": True,
                "ecu_id": ecu_id,
                "error": f"Reconsolidation could not classify the evidence "
                         f"({exc}). Nothing was changed. Retry with an "
                         "explicit relationship (supports/contradicts/"
                         "supersedes) once you have judged the evidence.",
            }
        except BrainError as exc:
            return {"status": "error", "session_active": True,
                    "ecu_id": ecu_id, "error": str(exc)}

        return {
            "status": "ok",
            "session_active": True,
            "ecu_id": r.ecu_id,
            "action_taken": r.action_taken,
            "new_confidence": round(r.new_confidence, 4),
            "old_confidence": round(r.old_confidence, 4),
            "edges_created": r.edges_created,
            "message": r.message,
        }

    # -- enablement + agent review (feat/flow, D55/D57) ----------------------

    def _tool_set_repo_state(self, args: dict) -> dict:
        """Record the user's enablement answer (D55): enabled | disabled |
        not_now (asked, stays off) | undecided (reset). Not gated — this is
        the tool that RECORDS the gate answer."""
        info, error = self._resolve_repo(args)
        if error is not None:
            return error
        state = args.get("state")
        if state not in ("enabled", "disabled", "not_now", "undecided"):
            return {
                "status": "error",
                "error": "state must be one of enabled | disabled | "
                         "not_now | undecided",
            }
        brain = self._ensure_brain()
        if state == "not_now":
            repos.mark_asked(brain, info)           # undecided-but-asked
            row = brain.get_repo(info.repo_key)
            effective = "undecided"
            message = ("Recorded: not now. Reverie stays off for this repo; "
                       "do not ask again in this conversation.")
        else:
            row = repos.set_repo_state(brain, info, state)
            effective = state
            message = {
                "enabled": "Reverie is on for this repo. Sessions are "
                           "automatic per branch; memory is shared across "
                           "branches.",
                "disabled": "Reverie is off for this repo. Nothing will be "
                            "captured or retrieved; existing memory is kept.",
                "undecided": "Reset: Reverie will ask about this repo again "
                             "on its next use.",
            }[state]
        return {"status": "ok", "repo": info.repo_path,
                "repo_key": info.repo_key, "state": effective,
                "asked_at": row["asked_at"], "message": message}

    def _tool_review_list(self, args: dict) -> dict:
        """The review queue page for the agent channel (D57): same ordered
        candidates as `ec review`, a compact verbatim block, and a
        short-lived review_token for ec_review_decide."""
        gate, ctx = self._gate(args)
        if gate is not None:
            return gate
        info = ctx["info"]
        scope_all = bool(args.get("all"))
        queue = review_queue.candidates(
            self._ensure_brain(),
            repo=None if scope_all else info.repo_path,
        )
        if args.get("by_topic"):
            ordered: list[dict] = []
            for _label, members in review_queue.topic_groups(queue):
                ordered.extend(members)
            queue = ordered
        total = len(queue)
        page_no = int(args.get("page") or 1)
        rows, total_pages = review_queue.page(queue, page_no,
                                              config=self.config)
        import uuid as _uuid
        token = str(_uuid.uuid4())
        self._prune_review_tokens()
        self._review_tokens[token] = {
            "ids": [r["id"] for r in rows], "created_at": _now_iso(),
        }
        lines = [
            f"Reverie review — {total} candidate(s) "
            f"(page {page_no}/{total_pages})",
        ]
        for i, row in enumerate(rows, start=1):
            lines.append(
                f"{i}. [{row['confidence']:.2f}] {row['cognition']}"
            )
            lines.append(
                f"   id: {row['id']} | type: {row['conclusion_type']} | "
                f"scope: {row['scope_path']} | repo: {row['repo_path']} | "
                f"branch: {row['branch']}"
            )
        if total_pages > page_no:
            lines.append(f"(more available — page {page_no + 1} of "
                         f"{total_pages})")
        message = "\n".join(lines)
        return {
            "status": "ok",
            "review_token": token,
            "total": total,
            "page": page_no,
            "total_pages": total_pages,
            "candidates": [
                {"id": r["id"], "cognition": r["cognition"],
                 "confidence": r["confidence"],
                 "conclusion_type": r["conclusion_type"],
                 "scope": r["scope_path"], "repo": r["repo_path"],
                 "branch": r["branch"], "review_status": r["review_status"]}
                for r in rows
            ],
            "message": message,
            "instructions": (
                "Show every candidate's full conclusion text, then relay "
                "only the decisions the user states via ec_review_decide. "
                "Never decide on the user's behalf. One listing feeds one "
                "decide call — after it succeeds, call ec_review_list again "
                "for the next batch (single-use tokens; this adds friction, "
                "it is not a security boundary). Reverie cannot verify "
                "who made a decision; the channel is recorded as 'agent'."
            ),
        }

    def _prune_review_tokens(self) -> None:
        """Drop expired tokens (TTL, D57)."""
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        stale = [
            token for token, entry in self._review_tokens.items()
            if (now - _dt.datetime.fromisoformat(entry["created_at"]))
            .total_seconds() > REVIEW_TOKEN_TTL_SECONDS
        ]
        for token in stale:
            del self._review_tokens[token]

    def _tool_review_decide(self, args: dict) -> dict:
        """Apply the USER's stated decisions (D57): token-scoped, never a
        global accept-all, channel recorded as 'agent'."""
        gate, ctx = self._gate(args)
        if gate is not None:
            return gate
        self._prune_review_tokens()
        token = args.get("review_token")
        entry = self._review_tokens.get(token) if isinstance(token, str) \
            else None
        if entry is None:
            return {
                "status": "error",
                "error": "Unknown or expired review_token. Call "
                         "ec_review_list again to get a fresh listing, "
                         "then decide from those ids.",
            }
        raw = args.get("decisions")
        decisions: dict[str, dict] = {}
        if isinstance(raw, dict):
            raw = [{"id": k, "action": v} for k, v in raw.items()]
        if not isinstance(raw, list) or not raw:
            return {
                "status": "error",
                "error": "decisions must be a non-empty array of "
                         "{id, action[, edited_cognition]}.",
            }
        for item in raw:
            if not isinstance(item, dict) or "id" not in item \
                    or "action" not in item:
                return {
                    "status": "error",
                    "error": "each decision needs 'id' and 'action' "
                             "(accept | reject | skip).",
                }
            decision = {"action": item["action"]}
            if item.get("edited_cognition") is not None:
                decision["edited_cognition"] = item["edited_cognition"]
            decisions[str(item["id"])] = decision
        token_ids = set(entry["ids"])
        unknown = [cid for cid in decisions if cid not in token_ids]
        if unknown:
            return {
                "status": "error",
                "error": f"Decision ids not in this listing: {unknown}. "
                         "Only candidates returned under this review_token "
                         "can be decided — call ec_review_list again.",
            }
        accepts = {cid for cid, d in decisions.items()
                   if d["action"] == "accept"}
        if len(token_ids) > 1 and accepts == token_ids:
            return {
                "status": "error",
                "error": "Refusing a bulk accept-all through the agent. "
                         "Review per candidate, or skip at least one and "
                         "re-list; the user can also run `ec review` in a "
                         "terminal.",
            }
        out = review_queue.apply_decisions(
            self._ensure_brain(), decisions,
            channel="agent", config=self.config,
        )
        # Single-use tokens (feat/flow review fix): the token is consumed
        # once a decide call succeeds, so each listing feeds exactly one
        # decision call — the next batch needs a fresh ec_review_list. This
        # adds friction, it is not a security boundary: Reverie cannot
        # verify who is on the other end of the channel.
        del self._review_tokens[token]
        return {
            "status": "ok",
            "decided": out["decided"],
            "unknown": out["unknown"],
            "results": [
                {"accepted": len(r.accepted), "rejected": len(r.rejected),
                 "skipped": len(r.skipped), "errors": r.errors}
                for r in out["results"]
            ],
            "message": "Decisions applied through the review gate "
                       "(channel: agent).",
        }


# ---------------------------------------------------------------------------
# stdio loop
# ---------------------------------------------------------------------------

def resolve_maintainer_repo_path(brain: Brain | None = None) -> str | None:
    """Repo the Maintainer verifies grounding against (D35), resolved once
    at server startup. Priority: ``EC_REPO_PATH`` (+``EC_BRANCH``) env
    override, git toplevel of the current directory, else the most recent
    active session's repo from the database. None when nothing resolves —
    grounding verification is then skipped by its task."""
    try:
        repo, _branch = detect_repo_branch()
        return repo
    except Exception:                        # not a repo / no git — fall through
        pass
    if brain is not None:
        try:
            return brain.most_recent_active_session_repo()
        except Exception:
            log.exception("EC Maintainer: session-repo lookup failed")
    return None


def serve(server: ECServer, stdin, stdout) -> None:
    """NDJSON loop: one JSON-RPC message per line (MCP stdio transport)."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        response = server.handle_line(line)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("EC MCP server starting (protocol %s)", PROTOCOL_VERSION)
    server = ECServer()
    server.start_maintenance(
        repo_path=resolve_maintainer_repo_path(server._ensure_brain()))
    try:
        serve(server, sys.stdin, sys.stdout)
    finally:
        server.stop_maintenance()
    return 0


if __name__ == "__main__":
    sys.exit(main())
