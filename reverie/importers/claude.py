"""Claude Code adapter: ``<config dir>/projects/<slug>/<session>.jsonl``."""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Iterable

from . import Source, Turn, read_new_lines, slug_of

name = "claude"
_NOISE = ("<command-name>", "<local-command", "Caveat:", "[Request interrupted",
          "<command-message>")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def config_dirs() -> list[Path]:
    dirs = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        dirs.append(Path(env).expanduser())
    dirs += [Path(p) for p in sorted(glob.glob(str(Path.home() / ".claude*")))]
    seen, out = set(), []
    for d in dirs:
        if (d / "projects").is_dir() and str(d.resolve()) not in seen:
            seen.add(str(d.resolve()))
            out.append(d)
    return out


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(c["text"] for c in content
                     if isinstance(c, dict) and c.get("type") == "text"
                     and isinstance(c.get("text"), str))


class _Adapter:
    name = name

    def sources(self, paths: list[str], since: float) -> Iterable[Source]:
        for cfg in config_dirs():
            for p in paths:
                d = cfg / "projects" / slug_of(p)
                for f in glob.glob(str(d / "*.jsonl")):
                    try:
                        m = os.path.getmtime(f)
                    except OSError:
                        continue
                    if m >= since:
                        yield Source(key=f, path=f, stamp=m,
                                     session=Path(f).stem, harness=name)

    def read(self, src: Source, state: dict) -> tuple[list[Turn], dict]:
        lines, off = read_new_lines(src.path, int(state.get("offset", 0)))
        turns: list[Turn] = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isMeta") or rec.get("isSidechain"):
                continue
            kind = rec.get("type")
            if kind not in ("user", "assistant"):
                continue
            inner = rec.get("message")
            msg: dict = inner if isinstance(inner, dict) else rec
            text = _REMINDER.sub("", _text(msg.get("content"))).strip()
            if not text or text.startswith(_NOISE):
                continue
            turns.append(Turn("prompt" if kind == "user" else "answer", text,
                              str(rec.get("sessionId") or src.session),
                              t=_local(rec.get("timestamp")),
                              branch=str(rec.get("gitBranch") or ""),
                              cwd=str(rec.get("cwd") or "")))
        new = dict(state)
        new["offset"] = off
        return turns, new


def _local(ts: object) -> str:
    from . import iso
    if not isinstance(ts, str) or len(ts) < 19:
        return ""
    import calendar
    import time
    try:
        base = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        return iso(calendar.timegm(base))
    except ValueError:
        return ""


ADAPTER = _Adapter()
