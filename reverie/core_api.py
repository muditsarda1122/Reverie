"""Harness-neutral core interface.

This is the boundary every integration (and the CLI) may import. It exposes
the same operations the MCP tools expose, plus the two extra paths harness
hooks need:

- ``summary(..., read_only=True)`` — brain stats with no writes (SessionStart)
- ``capture_turn(...)`` — extract one completed exchange (Stop hook)

No harness names live here. Harness packages live under
``reverie.integrations`` and call these functions; the core never imports a
harness package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import repos
from .config import default_db_path
from .session import brain_summary

#: One lazily-created server per process. Reusing it keeps the review-token
#: table stable across successive wrapper calls (review_list → review_decide)
#: and avoids reopening the DB for each call. MCP handlers pass their own
#: server explicitly. Tests reset this module attribute between homes.
_DEFAULT_SERVER = None


def _server_for(server):
    global _DEFAULT_SERVER
    if server is not None:
        return server
    if _DEFAULT_SERVER is None:
        from .mcp_server import ReverieServer
        _DEFAULT_SERVER = ReverieServer()
    return _DEFAULT_SERVER


def _summary_payload(server, info, row, branch: str | None) -> dict:
    brain = server._ensure_brain()
    payload = {"status": "ok", **brain_summary(brain, row, branch=branch)}
    from . import review_queue
    nudge = review_queue.nudge_line(
        brain, config=server.config,
        repo_path=info.repo_path if info is not None else None)
    if nudge:
        payload["review_nudge"] = nudge
        payload["message"] = payload["message"] + " " + nudge
    return payload


def _repo_and_error(server, repo_path: str | None):
    return server._resolve_repo({"repo_path": repo_path} if repo_path else {})


def summary(repo_path: str | None = None, branch: str | None = None, *,
            read_only: bool = False, _server=None) -> dict:
    """Brain statistics (and the review nudge), never individual conclusions.

    Default matches the MCP ``summary`` tool exactly: gated on enablement,
    resolves/creates the session for the detected branch, includes the
    review nudge. ``read_only=True`` performs no writes at all (no session
    creation, no asked-at stamp, no branch archiving) and is what the
    latency-sensitive SessionStart hook uses.
    """
    server = _server_for(_server)
    if read_only:
        info, error = _repo_and_error(server, repo_path)
        if error is not None:
            return error
        brain = server._ensure_brain()
        wanted_branch = branch or info.branch
        state = repos.state_readonly(brain, info)
        if state == "disabled":
            return {
                "status": "disabled", "state": "disabled",
                "repo": info.repo_path,
                "error": "Reverie is disabled for this repo. Nothing was "
                         "captured or retrieved.",
            }
        if state != "enabled":
            return {
                "status": "needs_consent", "state": "undecided",
                "repo": info.repo_path,
                "question": repos.FIRST_TIME_QUESTION,
                "error": repos.FIRST_TIME_QUESTION,
            }
        row = server._ensure_manager().current_session(
            info.repo_path, wanted_branch)
        return _summary_payload(server, info, row, wanted_branch)

    gate, ctx = server._gate({"repo_path": repo_path} if repo_path else {},
                             require_repo=False)
    if gate is not None:
        return gate
    row = None
    wanted_branch = branch
    if ctx is not None:
        info = ctx["info"]
        if branch:
            row = server._ensure_manager().ensure_session(info.repo_path,
                                                          branch)
        else:
            row = server._enabled_session(info)
        wanted_branch = branch or info.branch
    return _summary_payload(server, info if ctx is not None else None, row,
                            wanted_branch)


def observe(user_prompt: str, reasoning_trace: str,
            final_output: str | None = None, repo_path: str | None = None,
            *, _server=None) -> dict:
    """Extract conclusions from agent reasoning into the Session Brain."""
    args: dict[str, Any] = {
        "user_prompt": user_prompt,
        "reasoning_trace": reasoning_trace,
    }
    if final_output is not None:
        args["final_output"] = final_output
    if repo_path:
        args["repo_path"] = repo_path
    return _server_for(_server)._tool_observe(args)


def query(query: str, scope: str | None = None, mode: str | None = None,
          repo_path: str | None = None, *, _server=None) -> dict:
    """Demand-driven retrieval over both brains."""
    args: dict[str, Any] = {"query": query}
    if scope is not None:
        args["scope"] = scope
    if mode is not None:
        args["mode"] = mode
    if repo_path:
        args["repo_path"] = repo_path
    return _server_for(_server)._tool_query(args)


def set_repo_state(state: str, repo_path: str | None = None, *,
                   _server=None) -> dict:
    """Record the user's per-repo enablement answer."""
    args: dict[str, Any] = {"state": state}
    if repo_path:
        args["repo_path"] = repo_path
    return _server_for(_server)._tool_set_repo_state(args)


def review_list(page: int = 1, by_topic: bool = False,
                all_repos: bool = False, repo_path: str | None = None, *,
                _server=None) -> dict:
    """The pending-review page, for the agent to relay to the user."""
    args: dict[str, Any] = {"page": page, "by_topic": by_topic,
                            "all": all_repos}
    if repo_path:
        args["repo_path"] = repo_path
    return _server_for(_server)._tool_review_list(args)


def review_decide(review_token: str, decisions: list[dict], *,
                  repo_path: str | None = None, _server=None) -> dict:
    """Apply only the decisions the user stated."""
    args: dict[str, Any] = {"review_token": review_token,
                            "decisions": decisions}
    if repo_path:
        args["repo_path"] = repo_path
    return _server_for(_server)._tool_review_decide(args)


def _no_capture(state: str, repo_path: str | None, **extra) -> dict:
    payload = {
        "status": "disabled" if state == "disabled" else "needs_consent",
        "state": state,
        "repo": repo_path,
        "captured": False,
        "ecus_extracted": 0,
    }
    payload.update(extra)
    return payload


def capture_turn(repo_path: str | None, branch: str | None,
                 user_request: str, assistant_reply: str, *,
                 timeout: float = 20.0, _server=None) -> dict:
    """Capture one completed exchange into the Session Brain.

    Never raises. Only an enabled repo is captured. The exchange is saved to
    the pending queue before extraction, so a timeout, provider failure or
    crash leaves it recoverable via ``reverie retry``; a successful capture
    removes the pending row. ``timeout`` bounds the extraction (SIGALRM on
    POSIX main threads; a no-op where unavailable).
    """
    try:
        if not isinstance(user_request, str) or not user_request.strip() \
                or not isinstance(assistant_reply, str) \
                or not assistant_reply.strip():
            return {"status": "skipped", "captured": False,
                    "ecus_extracted": 0,
                    "message": "No completed user/assistant exchange."}
        server = _server_for(_server)
        info, error = _repo_and_error(server, repo_path)
        if error is not None:
            return {**error, "captured": False, "ecus_extracted": 0}
        if server._brain is None and not Path(default_db_path()).exists():
            return _no_capture("undecided", info.repo_path,
                               question=repos.FIRST_TIME_QUESTION)
        brain = server._ensure_brain()
        state = repos.state_readonly(brain, info)
        if state == "disabled":
            return _no_capture(
                "disabled", info.repo_path,
                message="Reverie is disabled for this repo; nothing captured.")
        if state != "enabled":
            return _no_capture("undecided", info.repo_path,
                               question=repos.FIRST_TIME_QUESTION)
        row = server._ensure_manager().ensure_session(
            info.repo_path, branch or info.branch)
        interaction = {
            "prompt": user_request,
            "output": assistant_reply,
            "session_context": {
                "session_id": row["id"],
                "repo": row["repo_path"],
                "branch": row["branch"],
            },
        }
        result = server._extract_interaction(
            row, interaction, prequeue=True, timeout=timeout)
        return {**result, "captured": result.get("status") == "ok",
                "repo": info.repo_path}
    except Exception as exc:               # a hook must never break the agent
        return {"status": "error", "captured": False, "ecus_extracted": 0,
                "error": str(exc)}
