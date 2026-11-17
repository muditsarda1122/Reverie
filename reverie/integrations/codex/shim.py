"""Codex hook shim: prompt log, turn import, session memory.

STDLIB ONLY at import time. Codex runs this as a command hook with the event
JSON on stdin (verified on 0.163, headless ``exec``: SessionStart,
UserPromptSubmit, Stop all carry ``session_id``, ``cwd``, ``transcript_path``;
UserPromptSubmit has ``prompt``). It NEVER raises and ALWAYS exits 0.

    prompt   UserPromptSubmit -> log the redacted prompt at once (a killed turn keeps it)
    stop     Stop             -> import the finished turn and queue extraction
    start    SessionStart     -> rules + memory block as additionalContext (read side)

Codex only runs a hook after the user approved it (``/hooks`` in the TUI); an
unapproved hook is skipped silently, and the transcript importer still covers
the session.
"""

from __future__ import annotations

import json
import os
import sys

HARNESS = "codex"


def _payload() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cmd = argv[0] if argv else ""
        p = _payload()
        cwd = str(p.get("cwd") or os.getcwd())
        if cmd == "prompt":
            from ... import spool
            text = p.get("prompt")
            if isinstance(text, str):
                spool.append(cwd, "prompt", text,
                             session=str(p.get("session_id") or ""), harness=HARNESS)
        elif cmd == "stop":
            from ... import importers
            importers.lazy(cwd, finalize=True)
        elif cmd == "start":
            from ... import deliver
            block = deliver.startup_text(cwd)
            if block.strip():
                sys.stdout.write(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "SessionStart", "additionalContext": block}}))
    except Exception:
        pass
    return 0


if __name__ == "__main__":        # pragma: no cover
    raise SystemExit(main())
