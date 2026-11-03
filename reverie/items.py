"""Typed memory items — storage, STDLIB ONLY (sqlite3), safe for hooks.

See the ``items`` table in schema.sql. Writes are plain INSERT/UPDATE on a WAL
database with a busy timeout: append-only in spirit (an item is never edited;
a changed fact is a NEW item that supersedes the old one), no explicit lock.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

KINDS = ("rule", "decision", "rejected_approach", "work_state", "preference",
         "environment", "external_context", "open_question")
SOURCES = ("user_said", "agent_inferred")
LEVELS = ("user", "repo", "team")
STATUSES = ("active", "stale", "superseded", "undone")

DDL = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY, repo_key TEXT, text TEXT NOT NULL, kind TEXT NOT NULL,
    anchor_json TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'agent', level TEXT NOT NULL DEFAULT 'user',
    grounding_commit TEXT, last_verified TEXT, status TEXT NOT NULL DEFAULT 'active',
    evidence TEXT, supersedes TEXT, created_at TEXT NOT NULL, embedding BLOB,
    meta_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_items_repo ON items(repo_key);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
"""


def connect(db: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(str(db), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript(DDL)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def anchor(files=(), symbols=(), type_: str | None = None) -> dict:
    files, symbols = list(files), list(symbols)
    kind = type_ or ("symbol" if symbols else "file" if files else "repo")
    return {"type": kind, "files": files, "symbols": symbols}


def add(conn: sqlite3.Connection, *, text: str, kind: str, source: str,
        repo_key: str | None, anchor_: dict | None = None, author: str = "agent",
        level: str = "user", grounding_commit: str | None = None,
        evidence: str | None = None, supersedes: str | None = None,
        embedding: bytes | None = None, meta: dict | None = None) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    if not text.strip():
        raise ValueError("empty text")
    if source == "agent_inferred" and not (evidence or "").strip():
        raise ValueError("agent-inferred items need evidence")
    item_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO items (id, repo_key, text, kind, anchor_json, source, author,"
        " level, grounding_commit, last_verified, status, evidence, supersedes,"
        " created_at, embedding, meta_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, repo_key, text if source == "user_said" else text.strip(), kind,
         json.dumps(anchor_ or anchor()), source, author, level, grounding_commit,
         _now() if grounding_commit else None, "active", evidence, supersedes,
         _now(), embedding, json.dumps(meta or {})))
    if supersedes:
        conn.execute("UPDATE items SET status='superseded' WHERE id=? "
                     "AND status='active'", (supersedes,))
    conn.commit()
    return item_id


def set_status(conn: sqlite3.Connection, item_id: str, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(status)
    cur = conn.execute("UPDATE items SET status=? WHERE id=?", (status, item_id))
    conn.commit()
    return cur.rowcount > 0


def get(conn: sqlite3.Connection, id_or_prefix: str) -> dict | None:
    rows = conn.execute("SELECT * FROM items WHERE id LIKE ?",
                        (id_or_prefix + "%",)).fetchall()
    return _row(rows[0]) if len(rows) == 1 else None


def active(conn: sqlite3.Connection, repo_key: str | None) -> list[dict]:
    """Active items for this repo plus user-wide ones (repo_key NULL)."""
    try:
        rows = conn.execute(
            "SELECT * FROM items WHERE status='active' AND "
            "(repo_key = ? OR repo_key IS NULL) ORDER BY created_at",
            (repo_key,)).fetchall()
    except sqlite3.OperationalError:       # table missing: brain pre-dates it
        return []
    return [_row(r) for r in rows]


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["anchor"] = json.loads(d.pop("anchor_json") or "{}")
    d["meta"] = json.loads(d.pop("meta_json") or "{}")
    d.pop("embedding", None)
    return d
