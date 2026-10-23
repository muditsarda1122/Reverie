"""``reverie hook`` — lifecycle hooks for coding agents (feat/session-start-hook).

The session-start hook is installed by a harness integration: its plain-text
stdout is injected into the conversation context at startup, so the agent
begins every session knowing Reverie's state — without relying on a first
tool call (the failure this fixes: a fresh session that never called Reverie
even though the instructions were loaded).

Contract — the hook must never break a session:

- **read-only:** no LLM call, no embeddings, no session creation, no writes
  of any kind (``repos.state_readonly``; a missing brain is never created);
- **quiet on failure:** not a git repository, unreadable/broken brain,
  malformed config → print nothing, exit 0;
- **fast:** two indexed SQLite counts, no model loading (well under a
  second even on a large brain).

Output states:

- enabled  — the canonical summary message: counts and the pending-review
  count (never conclusion text);
- undecided — the first-time question (verbatim) and how to record it;
- disabled — one line: off means off.

Harness hook schemas live in the integration packages; this module only
renders the context block and stays neutral.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import core_api, repos
from .brain import Brain
from .config import SCHEMA_VERSION, default_db_path

#: Exact first-time question (shared with the MCP gate — repos.FIRST_TIME_QUESTION
#: is the same string; imported here so the hook and the gate can never drift).
UNDECIDED_TEXT = repos.FIRST_TIME_QUESTION

#: Disabled repos are fully off: nothing captured, retrieved or injected.
DISABLED_TEXT = "Reverie is off for this repo. Do not capture or retrieve here."

def enabled_block(summary: dict) -> str:
    """The enabled-repo block: the canonical summary message only.

    The stats line is ``core_api.summary``'s own ``message`` — the hook must
    not grow a parallel formatting path that can drift from the MCP tool.
    Counts only — never conclusion text (anti-anchoring, §11.3)."""
    return summary["message"]


def undecided_block(repo_path: str) -> str:
    """The undecided-repo block: the first-time question (verbatim) plus the
    instruction to ask once and record the answer."""
    return (
        f"{UNDECIDED_TEXT}\n"
        "Ask the user this once — at most once per conversation — then "
        "record the answer with reverie_set_repo_state "
        f"(repo_path: {repo_path})."
    )


def _db_ready(db: Path) -> bool:
    """Whether ``db`` is an existing brain at this build's schema version.

    Probed with a READ-ONLY connection: an older-schema brain would be
    migrated on open, and the hook must not write. A missing, unreadable or
    outdated brain is simply "not ready" — the hook stays quiet (the next
    real tool call opens and migrates it normally)."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return False
    return int(version) == SCHEMA_VERSION


def session_start(cwd: str | Path | None = None, *, print_fn=print) -> int:
    """Print the session-start block for the repo at ``cwd`` (default: the
    current directory). Always returns 0 and never raises.

    The brain is read only when its database already exists at the current
    schema version — the hook never creates or migrates one. A missing brain
    reads as undecided (Reverie has no state for this repo yet)."""
    try:
        info = repos.resolve_repo(cwd=cwd)
        db = default_db_path()
        if not db.exists():
            print_fn(undecided_block(info.repo_path))
            return 0
        if not _db_ready(db):
            return 0                  # broken or outdated brain — stay quiet
        brain = Brain(db)
        try:
            from .mcp_server import ReverieServer
            payload = core_api.summary(
                info.repo_path, branch=info.branch, read_only=True,
                _server=ReverieServer(brain=brain))
        finally:
            brain.close()
    except Exception:
        return 0                      # never break a session — print nothing
    if payload.get("status") == "ok":
        print_fn(enabled_block(payload))
    elif payload.get("status") == "disabled":
        print_fn(DISABLED_TEXT)
    elif payload.get("status") == "needs_consent":
        print_fn(undecided_block(info.repo_path))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reverie hook",
        description="Agent lifecycle hooks, invoked by the harness itself "
                    "(installed by `reverie install`).",
    )
    sub = parser.add_subparsers(dest="hook", required=True)
    p = sub.add_parser(
        "session-start",
        help="print the session-start context block (repo state + usage "
             "rules); read-only, quiet on error, exit 0 always",
    )
    p.add_argument("--repo", default=None,
                   help="workspace path (default: current directory)")
    args = parser.parse_args(argv)
    if args.hook == "session-start":
        return session_start(args.repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
