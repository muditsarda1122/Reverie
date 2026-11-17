"""Claude Code hook shim: prompt log, answer log, point-of-action memory.

STDLIB ONLY (plus reverie.spool / reverie.deliver, which are stdlib-only):
this runs on every prompt and every edit, so it must start in tens of
milliseconds and must never import the brain, numpy or torch. It NEVER raises
and ALWAYS exits 0 — a hook must not break the session.

    reverie-claude-hook prompt    UserPromptSubmit  -> append the prompt
    reverie-claude-hook answer    Stop              -> append the final answer
    reverie-claude-hook pretool   PreToolUse        -> memory for the edited file
"""

from __future__ import annotations

import json
import os
import sys

from ... import deliver, spool

HARNESS = "claude"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _payload() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _prompt(p: dict) -> None:
    text = p.get("prompt")
    if isinstance(text, str):
        spool.append(p.get("cwd") or os.getcwd(), "prompt", text,
                     session=str(p.get("session_id") or ""), harness=HARNESS)


def _answer(p: dict) -> None:
    text = p.get("last_assistant_message")
    if isinstance(text, str):
        spool.append(p.get("cwd") or os.getcwd(), "answer", text,
                     session=str(p.get("session_id") or ""), harness=HARNESS)


def _pretool(p: dict) -> str:
    if p.get("tool_name") not in EDIT_TOOLS:
        return ""
    ti = p.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("notebook_path")
    if not target:
        return ""
    extra = [str(ti.get("content") or ""), str(ti.get("new_string") or "")]
    extra += [str(e.get("new_string", "")) for e in ti.get("edits") or []]
    context = deliver.point_of_action(
        p.get("cwd") or os.getcwd(), str(p.get("session_id") or ""), target,
        "\n".join(extra))
    if not context:
        return ""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "additionalContext": context}})


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cmd = argv[0] if argv else ""
        if cmd == "prompt":
            _prompt(_payload())
        elif cmd == "answer":
            _answer(_payload())
        elif cmd == "pretool":
            out = _pretool(_payload())
            if out:
                sys.stdout.write(out)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
