"""Undo and the occasional digest — the replacement for the review gate.

Items are accepted automatically. To keep that safe the user can (a) undo any
item, restoring is just as easy, and (b) read a digest of what was added since
they last looked. STDLIB ONLY (the session-start hook uses ``nudge``).
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from . import items, spool


def _state_path():
    return spool.reverie_home() / "digest.json"


def _state() -> dict:
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def last_digest() -> str | None:
    return _state().get("last")


def watermark(repo_key: str | None) -> int:
    """Rowid up to which this repo's items (or all repos' for None) were seen."""
    marks = _state().get("marks") or {}
    return max(int(marks.get("*", 0)), int(marks.get(repo_key or "*", 0)))


def mark_digest(conn: sqlite3.Connection, repo_key: str | None,
                now: str | None = None) -> None:
    """Everything added so far counts as seen, for this repo (or all)."""
    state = _state()
    marks = dict(state.get("marks") or {})
    marks[repo_key or "*"] = items.max_rowid(conn)
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last": now or items._now(), "marks": marks}))


def undo(conn: sqlite3.Connection, id_prefix: str, restore: bool = False) -> str:
    """Undo (or restore) one item. Returns a human message."""
    it = items.get(conn, id_prefix)
    if it is None:
        return f"No single item matches {id_prefix!r}."
    if restore:
        if it["status"] != "undone":
            return f"[{it['id'][:8]}] is {it['status']}, not undone."
        items.set_status(conn, it["id"], "active")
        return f"Restored [{it['id'][:8]}]: {it['text'][:80]}"
    if it["status"] == "undone":
        return f"[{it['id'][:8]}] is already undone."
    items.set_status(conn, it["id"], "undone")
    return (f"Undone [{it['id'][:8]}]: {it['text'][:80]}\n"
            f"  restore with: reverie undo --restore {it['id'][:8]}")


def _who(it: dict) -> str:
    return "you said" if it["source"] == "user_said" else "inferred by the agent"


def render(its: list[dict], since: str | None) -> str:
    if not its:
        return "Nothing new" + (f" since {since[:16]}." if since else ".")
    lines = [f"{len(its)} item(s) added since "
             f"{since[:16] if since else 'the beginning'}:", ""]
    for it in its:
        lines.append(f"[{it['id'][:8]}] {it['kind']} ({_who(it)})")
        lines.append(f"    {it['text'].strip()}")
        a = it.get("anchor") or {}
        where = ", ".join((a.get("files") or []) + (a.get("symbols") or []))
        if where:
            lines.append(f"    anchored to: {where}")
    lines += ["", "Undo any of them with `reverie undo <id>`."]
    return "\n".join(lines)


def _pending_stale(conn: sqlite3.Connection, repo_key: str | None) -> str:
    q = "SELECT * FROM items WHERE status='candidate'"
    args: list = []
    if repo_key is not None:
        q += " AND repo_key = ?"
        args.append(repo_key)
    rows = [items._row(r) for r in conn.execute(q, args)]
    if not rows:
        return ""
    lines = ["", f"{len(rows)} memory(ies) may have gone stale after code changes:"]
    for c in rows:
        lines.append(f"  [{c['id'][:8]}] proposed update: {c['text'][:160]}")
        lines.append(f"      reverie stale accept {c['id'][:8]}  |  "
                     f"reverie stale keep {c['meta'].get('candidate_for', '')[:8]}")
    return "\n".join(lines)


def digest(conn: sqlite3.Connection, repo_key: str | None,
           mark: bool = True) -> str:
    its = items.recent(conn, watermark(repo_key), repo_key)
    out = render(its, last_digest())
    out += _pending_stale(conn, repo_key)
    if mark:
        mark_digest(conn, repo_key)
    return out


def nudge(conn: sqlite3.Connection, repo_key: str | None,
          min_new: int = 5, min_days: float = 3.0) -> str:
    """One line for session start when enough has piled up unseen, else ''."""
    last = last_digest()
    its = items.recent(conn, watermark(repo_key), repo_key)
    if not its:
        return ""
    old = True
    if last:
        try:
            then = datetime.strptime(last[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
            old = (time.time() - then) / 86400 >= min_days
        except ValueError:
            pass
    if len(its) >= min_new or (old and last):
        return (f"Reverie remembered {len(its)} new item(s) since you last "
                "looked. Run `reverie digest` to see them (undo any with "
                "`reverie undo <id>`).")
    return ""
