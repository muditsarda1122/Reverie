"""opencode plugin shim: prompt log, idle import, session memory.

STDLIB ONLY at import time. The generated plugin (``plugin.js``) spawns this
module with a JSON payload on stdin. It NEVER raises and ALWAYS exits 0.

    prompt   chat.message  -> log the user's prompt (redacted) the moment it is sent
    idle     session.idle  -> import the finished session (answer + extraction)
    start    system prompt -> print the rules + memory block (read side)
"""

from __future__ import annotations

import json
import os
import sys

HARNESS = "opencode"


def _payload() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _unquote(text: str) -> str:
    """`opencode run "msg"` hands the plugin the message JSON-quoted."""
    t = text.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        try:
            v = json.loads(t)
            if isinstance(v, str):
                return v
        except ValueError:
            pass
    return t


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cmd = argv[0] if argv else ""
        p = _payload()
        cwd = str(p.get("cwd") or os.getcwd())
        if cmd == "prompt":
            from ... import spool
            text = p.get("text")
            if isinstance(text, str):
                spool.append(cwd, "prompt", _unquote(text),
                             session=str(p.get("session") or ""), harness=HARNESS)
        elif cmd == "idle":
            from ... import importers
            importers.lazy(cwd, finalize=True)
        elif cmd == "start":
            from ... import deliver
            sys.stdout.write(deliver.startup_text(cwd))
    except Exception:
        pass
    return 0


if __name__ == "__main__":        # pragma: no cover
    raise SystemExit(main())
