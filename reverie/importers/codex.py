"""Codex CLI adapter: ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``."""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Iterable

from . import Source, Turn, read_new_lines, under

name = "codex"
# context Codex injects as "user" messages; none of it was typed by the user
_INJECTED = ("# AGENTS.md instructions", "<environment_context", "<user_instructions",
             "<recommended_plugins", "<permissions", "<skills_instructions",
             "<turn_aborted", "<collaboration_mode", "<multi_agent", "<user_shell_command")


def home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _parts(content: object, kind: str) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(c["text"] for c in content if isinstance(c, dict)
                     and c.get("type") == kind and isinstance(c.get("text"), str))


class _Adapter:
    name = name

    def sources(self, paths: list[str], since: float) -> Iterable[Source]:
        root = home() / "sessions"
        for f in glob.glob(str(root / "*" / "*" / "*" / "rollout-*.jsonl")):
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            if m < since:
                continue
            cwd = _first_cwd(f)
            if cwd and under(cwd, paths):
                yield Source(key=f, path=f, stamp=m, harness=name,
                             session=Path(f).stem)

    def read(self, src: Source, state: dict) -> tuple[list[Turn], dict]:
        lines, off = read_new_lines(src.path, int(state.get("offset", 0)))
        session = state.get("session") or ""
        branch = state.get("branch") or ""
        turns: list[Turn] = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
                continue
            p: dict = rec["payload"]
            if rec.get("type") == "session_meta":
                session = str(p.get("id") or p.get("session_id") or session)
                git = p.get("git")
                branch = str((git.get("branch") if isinstance(git, dict) else "") or branch)
                continue
            if rec.get("type") != "response_item" or p.get("type") != "message":
                continue
            role = p.get("role")
            t = _ts(rec.get("timestamp"))
            if role == "user":
                text = _parts(p.get("content"), "input_text").strip()
                if not text or text.startswith(_INJECTED):
                    continue
                turns.append(Turn("prompt", text, session or src.session, t=t, branch=branch))
            elif role == "assistant":
                text = _parts(p.get("content"), "output_text").strip()
                if text:
                    turns.append(Turn("answer", text, session or src.session, t=t,
                                      branch=branch))
        new = dict(state)
        new.update(offset=off, session=session, branch=branch)
        return turns, new


def _first_cwd(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            rec = json.loads(fh.readline())
        return str(rec.get("payload", {}).get("cwd") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _ts(ts: object) -> str:
    import calendar
    import time

    from . import iso
    if not isinstance(ts, str) or len(ts) < 19:
        return ""
    try:
        return iso(calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return ""


ADAPTER = _Adapter()
