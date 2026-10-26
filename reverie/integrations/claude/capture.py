"""Claude Code Stop-hook entry point: capture one completed exchange.

Claude's Stop hook sends JSON on stdin:

    {"session_id": ..., "transcript_path": ..., "cwd": ...,
     "hook_event_name": "Stop", "last_assistant_message": ...}

We read the last user request and the final assistant reply from the
transcript (falling back to ``last_assistant_message``), then hand them to
``core_api.capture_turn``. The hook is fire-and-forget: this function NEVER
raises and ALWAYS exits 0 — a broken transcript, a disabled repo, a missing
provider key or an extraction timeout must not block Claude. A timed-out or
failed extraction is left in the pending queue for ``reverie retry``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ... import core_api
from ...repos import resolve_repo


@dataclass
class Exchange:
    user_request: str
    assistant_reply: str


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("tool_result", "tool_use", "thinking"):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def read_exchange(path: str | Path,
                  last_assistant_message: str | None = None) -> Exchange | None:
    """The last user request and final assistant text in a Claude JSONL log.

    Sidechain (subagent) messages and meta entries are ignored. Returns None
    when there is no usable user request — the hook then does nothing.
    """
    last_user = ""
    last_assistant = ""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict) or record.get("isMeta") or \
                record.get("isSidechain"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            message = record
        role = message.get("role") or record.get("type")
        content = message.get("content")
        if role == "user":
            text = _text_from_content(content)
            if text.strip():
                last_user = text
        elif role == "assistant":
            text = _text_from_content(content)
            if text.strip():
                last_assistant = text
    if isinstance(last_assistant_message, str) and last_assistant_message.strip():
        last_assistant = last_assistant_message
    if not last_user.strip():
        return None
    return Exchange(last_user, last_assistant)


def _hook_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _branch_for(repo: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        return resolve_repo(repo).branch
    except Exception:
        return None


def _run(args: argparse.Namespace) -> int:
    payload = _hook_payload()
    transcript = args.transcript or payload.get("transcript_path")
    if not transcript:
        return 0
    exchange = read_exchange(transcript, payload.get("last_assistant_message"))
    if exchange is None:
        return 0
    repo = (args.repo or payload.get("cwd") or os.getcwd())
    branch = _branch_for(repo, args.branch or payload.get("gitBranch"))
    core_api.capture_turn(repo, branch, exchange.user_request,
                          exchange.assistant_reply, timeout=args.timeout)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reverie capture-turn")
    parser.add_argument("--transcript", help="Claude JSONL transcript path")
    parser.add_argument("--repo", help="Repository path (default: hook cwd)")
    parser.add_argument("--branch", help="Branch (default: detected)")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="Extraction time limit in seconds (default: 20)")
    try:
        return _run(parser.parse_args(argv))
    except SystemExit:
        raise
    except Exception:
        return 0


if __name__ == "__main__":          # pragma: no cover
    raise SystemExit(main())
