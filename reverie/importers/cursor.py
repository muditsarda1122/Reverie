"""Cursor adapter: ``~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl``.

Lines look like ``{"role": "user"|"assistant", "message": {"content": [...]}}``;
the user's own words sit inside ``<user_query>`` after a ``<timestamp>`` tag."""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Iterable

from . import Source, Turn, read_new_lines, slug_of

name = "cursor"
_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def home() -> Path:
    return Path(os.environ.get("CURSOR_HOME") or Path.home() / ".cursor").expanduser()


class _Adapter:
    name = name

    def sources(self, paths: list[str], since: float) -> Iterable[Source]:
        for p in paths:
            pattern = str(home() / "projects" / slug_of(p).lstrip("-") /
                          "agent-transcripts" / "*" / "*.jsonl")
            for f in glob.glob(pattern):
                try:
                    m = os.path.getmtime(f)
                except OSError:
                    continue
                if m >= since:
                    yield Source(key=f, path=f, stamp=m, harness=name,
                                 session=Path(f).stem)

    def read(self, src: Source, state: dict) -> tuple[list[Turn], dict]:
        lines, off = read_new_lines(src.path, int(state.get("offset", 0)))
        turns: list[Turn] = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            msg = rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            text = "\n".join(c["text"] for c in content if isinstance(c, dict)
                             and c.get("type") == "text"
                             and isinstance(c.get("text"), str)) \
                if isinstance(content, list) else ""
            if rec.get("role") == "user":
                m = _QUERY.search(text)
                if m and m.group(1).strip():
                    turns.append(Turn("prompt", m.group(1).strip(), src.session))
            elif rec.get("role") == "assistant" and text.strip():
                turns.append(Turn("answer", text.strip(), src.session))
        new = dict(state)
        new["offset"] = off
        return turns, new


ADAPTER = _Adapter()
