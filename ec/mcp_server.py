"""EC MCP server (Spec §28.3) — stdio transport, newline-delimited JSON-RPC 2.0.

Runs as a local subprocess of the coding agent: reads JSON-RPC messages from
stdin (one per line, NDJSON per the MCP stdio transport), writes responses to
stdout. All logging goes to stderr — stdout is protocol-only.

Implemented protocol subset (D14 — hand-rolled; the ``mcp`` SDK is not in the
pinned venv): ``initialize``, ``notifications/initialized`` (ignored), ``ping``,
``tools/list``, ``tools/call``. Tool results carry the §16.3/§28.11 structured
payload as JSON text; ``isError`` mirrors the payload's ``status == "error"``.

Tools (exactly the §28.3 surface plus D37's ec_reconsolidate, descriptions
verbatim per §28.12/design doc §5.2):
  - ``ec_observe``       — extract + lightweight-diffuse reasoning into the
                           Session Brain (§28.13)
  - ``ec_query``         — demand-driven retrieval over both brains (§11.3)
  - ``ec_get_summary``   — brain statistics at session start (§11.3 awareness
                           without anchoring); works without a session
  - ``ec_reconsolidate`` — re-evaluate a retrieved ECU with new evidence
                           (design doc §5.2/D37; writes to the Canonical
                           Brain immediately — the ECU was reviewed when it
                           entered). Only ECUs retrieved in this session are
                           labile (D39): the server marks surfaced canonical
                           ids during ec_query and refuses others.

The server NEVER creates sessions (the user controls lifecycle, §28.9): it
resolves the current session from the database per call via (repo, branch)
detection — WAL makes this safe across the CLI/MCP processes (D15). Sessions
are started by ``python -m ec.session start`` (invoked by /ec-start).

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

from . import __version__, diffuser, extractor, maintainer, reconsolidation, retrieval
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

# §28.4 error messages — verbatim, actionable.
NO_SESSION_ERROR = (
    "No active EC session. Suggest the user run /ec-start to begin a session."
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
retrieval via ec_query. The user reviews these ECUs at session close (/ec-stop).

If no session is active, returns an error — tell the user to run /ec-start."""

_QUERY_DESCRIPTION = """\
Retrieve engineering conclusions relevant to your current task. Call this
AFTER reading the relevant code files, when you recognize a gap that past
cognition might fill:
- Before making a decision between multiple approaches
- When debugging and the cause isn't obvious from the code alone
- Before an architectural change (query for existing architectural decisions)

Returns ECUs from both the Session Brain (current session's working memory)
and the Canonical Brain (accumulated understanding from past sessions).
Each ECU includes confidence, scope, grounding references, and status.

Memory is framed as "past beliefs to verify against current code," not
authoritative context. Always verify grounding references against the
live codebase before acting on an ECU.

If no session is active, returns an error — tell the user to run /ec-start."""

_SUMMARY_DESCRIPTION = """\
Get a summary of what the EC brain knows. Call this ONCE at session start
to understand what engineering cognition is already stored before you begin
work. Returns brain statistics and scope distribution — not individual ECUs.

Works without an active session (reads Canonical Brain only)."""

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

TOOLS = [
    {
        "name": "ec_observe",
        "description": _OBSERVE_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
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
            },
            "required": ["user_prompt", "reasoning_trace"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ec_query",
        "description": _QUERY_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
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
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ec_get_summary",
        "description": _SUMMARY_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "ec_reconsolidate",
        "description": _RECONSOLIDATE_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
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
            },
            "required": ["ecu_id", "evidence"],
            "additionalProperties": False,
        },
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
            "isError": payload.get("status") == "error",
        })

    # -- shared helpers ------------------------------------------------------

    def _current_session(self) -> dict | None:
        return self._ensure_manager().current_session()

    @staticmethod
    def _no_session_payload() -> dict:
        return {"status": "error", "session_active": False,
                "error": NO_SESSION_ERROR}

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
        row = self._current_session()
        if row is None:
            return self._no_session_payload()
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
        try:
            result, ids = extractor.extract_and_store_session(
                brain, row["id"], interaction,
                embedding_model=self.embedding_model, config=self.config,
                commit_hash=_current_commit_hash(row["repo_path"]),  # D49
            )
        except ExtractorError as exc:
            return {"status": "error", "session_active": True,
                    "error": EXTRACTION_FAILED.format(reason=exc)}

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
        }

    def _tool_query(self, args: dict) -> dict:
        """The §11.3 demand-driven retrieval path (ec_query)."""
        row = self._current_session()
        if row is None:
            return self._no_session_payload()
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

    def _tool_summary(self, _args: dict) -> dict:
        """§16.3 ec_get_summary — works without an active session."""
        try:
            row = self._current_session()
        except SessionError:
            row = None  # summary works session-less (and repo-less)
        return {"status": "ok", **brain_summary(self._ensure_brain(), row)}

    def _tool_reconsolidate(self, args: dict) -> dict:
        """D37 — ec_reconsolidate (design doc §5.2): re-evaluate a labile
        canonical ECU with new evidence. Writes to the Canonical Brain
        immediately; the §5.2 payload is returned verbatim."""
        row = self._current_session()
        if row is None:
            return self._no_session_payload()

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
