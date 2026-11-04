"""opencode adapter: SQLite ``opencode.db`` (read-only; only the session,
message and part tables are touched, never the credential tables)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Iterable

from . import Source, Turn, iso, under

name = "opencode"


def db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(os.environ.get("OPENCODE_DB") or Path(base) / "opencode" / "opencode.db")


def _connect() -> sqlite3.Connection | None:
    p = db_path()
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1 FROM session LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def _unquote(text: str) -> str:
    t = text.strip()
    # `opencode run "msg"` stores the message JSON-quoted
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        try:
            v = json.loads(t)
            if isinstance(v, str):
                return v
        except ValueError:
            pass
    return t


class _Adapter:
    name = name

    def sources(self, paths: list[str], since: float) -> Iterable[Source]:
        conn = _connect()
        if conn is None:
            return
        try:
            rows = conn.execute(
                "SELECT id, directory, time_updated FROM session "
                "WHERE parent_id IS NULL AND time_updated >= ?", (int(since * 1000),)
            ).fetchall()
        except sqlite3.Error:
            return
        finally:
            conn.close()
        for sid, directory, updated in rows:
            if directory and under(directory, paths):
                yield Source(key=f"opencode:{sid}", path=str(db_path()),
                             stamp=updated / 1000.0, harness=name, session=sid)

    def read(self, src: Source, state: dict) -> tuple[list[Turn], dict]:
        conn = _connect()
        if conn is None:
            return [], dict(state)
        after = int(state.get("offset", 0))
        turns: list[Turn] = []
        last = after
        try:
            rows = conn.execute(
                "SELECT p.time_created, json_extract(m.data,'$.role'), p.data "
                "FROM part p JOIN message m ON m.id = p.message_id "
                "WHERE p.session_id = ? AND p.time_created > ? "
                "ORDER BY p.time_created, p.id", (src.session, after)).fetchall()
        except sqlite3.Error:
            return [], dict(state)
        finally:
            conn.close()
        for created, role, data in rows:
            last = max(last, int(created))
            try:
                part = json.loads(data)
            except ValueError:
                continue
            if part.get("type") != "text" or part.get("synthetic") or part.get("ignored"):
                continue
            text = str(part.get("text") or "")
            if role == "user":
                turns.append(Turn("prompt", _unquote(text), src.session,
                                  t=iso(created / 1000.0)))
            elif role == "assistant" and text.strip():
                turns.append(Turn("answer", text.strip(), src.session,
                                  t=iso(created / 1000.0)))
        new = dict(state)
        new["offset"] = last
        return turns, new


ADAPTER = _Adapter()
