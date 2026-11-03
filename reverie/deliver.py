"""Deliver the right memory at the right moment. STDLIB ONLY (hooks call this).

Three delivery paths, by measured value (lab X7/X9):

1. Rules lane  — user-entered rules (reverie.rules), always loaded at start.
2. Headline index — one line per active item/conclusion (id, kind, title,
   anchor) at session start; the agent opens an item by id on demand
   (``reverie show <id>``). Scaled to >=200 entries at low cost.
3. Point-of-action — a hook that runs before edits. An item fires when the file
   is one of its anchors OR one of its anchor symbols appears in that file.
   (File anchors alone missed a rule that had no file anchor: 0/2.)

Every delivered item carries freshness: "verified at commit X; file changed
since: yes/no/unknown".
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from . import items as items_mod
from . import spool

MAX_POINT_ITEMS = 3
MAX_CONTEXT_CHARS = 6000          # harnesses cap hook context (10k chars)
INDEX_MAX_LINES = 80
INDEX_TITLE_WORDS = 14
MAX_FILE_BYTES = 1_000_000

# order in the index: what changed agent behaviour most comes first
_KIND_ORDER = {"rule": 0, "decision": 1, "rejected_approach": 2,
               "work_state": 3, "preference": 4, "environment": 5,
               "external_context": 6, "open_question": 7, "observation": 9}


def db_path() -> Path:
    return spool.reverie_home() / "reverie.db"


def repo_key(cwd: str | Path) -> str:
    """The same identity repos.common_dir() uses (git common dir)."""
    common = spool._git(["rev-parse", "--path-format=absolute",
                         "--git-common-dir"], cwd)
    return str(Path(common).resolve()) if common else spool.repo_root(cwd)


def open_db() -> sqlite3.Connection | None:
    path = db_path()
    if not path.exists():
        return None
    try:
        conn = items_mod.connect(path, readonly=True)
        conn.execute("SELECT 1 FROM repos LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def is_disabled(conn: sqlite3.Connection, key: str) -> bool:
    try:
        row = conn.execute("SELECT state FROM repos WHERE repo_key=?",
                           (key,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row["state"] == "disabled")


# ---------------------------------------------------------------------------
# unified view: new typed items + legacy canonical conclusions
# ---------------------------------------------------------------------------

def _legacy_kind(conclusion_type: str) -> str:
    return "decision" if conclusion_type in ("decision", "constraint",
                                             "trade-off") else "observation"


def _legacy_conclusions(conn: sqlite3.Connection, root: str) -> list[dict]:
    out: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT id, cognition, conclusion_type, grounding_json, created_at "
            "FROM ecus WHERE status IN ('active','challenged')").fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        try:
            g = json.loads(r["grounding_json"] or "{}")
        except ValueError:
            continue
        if g.get("repo_path") and os.path.realpath(g["repo_path"]) != \
                os.path.realpath(root):
            continue
        out.append({
            "id": r["id"], "text": r["cognition"], "origin": "conclusion",
            "kind": _legacy_kind(r["conclusion_type"]),
            "anchor": {"type": "file", "files": g.get("files") or [],
                       "symbols": g.get("symbols") or []},
            "source": "agent_inferred", "grounding_commit": g.get("commit_hash"),
            "level": "repo", "status": "active"})
    return out


def memory(conn: sqlite3.Connection, key: str, root: str) -> list[dict]:
    typed = items_mod.active(conn, key)
    for it in typed:
        it["origin"] = "item"
        it["grounding_commit"] = it.get("grounding_commit")
    return typed + _legacy_conclusions(conn, root)


# ---------------------------------------------------------------------------
# headline index
# ---------------------------------------------------------------------------

def title(text: str, words: int = INDEX_TITLE_WORDS) -> str:
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0]
    parts = first.split()
    return " ".join(parts[:words]) + (" …" if len(parts) > words else "")


def _anchor_label(it: dict) -> str:
    a = it.get("anchor") or {}
    if a.get("type") == "user" or it.get("repo_key") is None and \
            it.get("origin") == "item":
        return "user-wide"
    if a.get("files"):
        return ", ".join(a["files"][:2])
    if a.get("symbols"):
        return ", ".join(a["symbols"][:2])
    return "repo"


def headline_index(mem: list[dict], limit: int = INDEX_MAX_LINES) -> str:
    if not mem:
        return ""
    # Derivable code observations are worth ~nothing as headlines (lab H8) and
    # invite anchoring; they still surface at the edit-time hook via anchors.
    mem = [m for m in mem if m["kind"] != "observation"]
    if not mem:
        return ""
    ordered = sorted(mem, key=lambda i: (_KIND_ORDER.get(i["kind"], 8),
                                         i.get("created_at") or ""))
    lines = [
        "Reverie memory index. These are past decisions and findings, not "
        "facts: verify against the code. Open one with `reverie show <id>`.",
    ]
    for it in ordered[:limit]:
        lines.append(f"- [{it['id'][:8]}] {it['kind']}: {title(it['text'])} "
                     f"(anchor: {_anchor_label(it)})")
    if len(ordered) > limit:
        lines.append(f"(+{len(ordered) - limit} more; `reverie show` opens any "
                     "id, and the edit hook surfaces matching ones)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# point-of-action matching + freshness
# ---------------------------------------------------------------------------

def _norm(p: str) -> str:
    return p.replace("\\", "/").lstrip("./")


def file_anchor_match(it: dict, rel: str) -> bool:
    rel = _norm(rel)
    for f in (it.get("anchor") or {}).get("files") or []:
        f = _norm(f)
        if f == rel or rel.endswith("/" + f) or f.endswith("/" + rel):
            return True
    return False


def symbol_in_file(it: dict, text: str) -> str | None:
    for sym in (it.get("anchor") or {}).get("symbols") or []:
        for part in {sym, sym.split(".")[-1], sym.split("::")[-1]}:
            if len(part) >= 3 and re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(part)}(?![A-Za-z0-9_])", text):
                return sym
    return None


def match(mem: list[dict], rel: str, file_text: str) -> list[tuple[dict, str]]:
    """Items whose file anchor is this file, or whose symbol occurs in it.
    Returns (item, why) pairs, file-anchored first."""
    hits: list[tuple[dict, str]] = []
    for it in mem:
        if it["kind"] == "rule":
            continue                       # rules ride the always-on lane
        if file_anchor_match(it, rel):
            hits.append((it, "file anchor"))
        else:
            sym = symbol_in_file(it, file_text)
            if sym:
                hits.append((it, f"symbol `{sym}` appears in this file"))
    hits.sort(key=lambda h: (h[1] != "file anchor",
                             _KIND_ORDER.get(h[0]["kind"], 8)))
    return hits


def freshness(it: dict, rel: str, root: str) -> str:
    """'verified at commit X; file changed since: yes|no|unknown'."""
    commit = it.get("grounding_commit")
    if not commit:
        return "not tied to a commit"
    short = commit[:8]
    if not spool._git(["cat-file", "-t", commit], root):
        return f"verified at commit {short}; file changed since: unknown"
    diff = spool._git(["diff", "--name-only", commit, "--", rel], root)
    return (f"verified at commit {short}; file changed since: "
            f"{'yes' if diff.strip() else 'no'}")


_STANDING = ("rule", "decision", "rejected_approach")


def _is_standing(it: dict) -> bool:
    """A decision the user stated, or any rule / rejected approach, is a
    standing constraint, not a hint."""
    return it["kind"] in _STANDING and (
        it.get("source") == "user_said" or it["kind"] != "decision")


def render_point(hits: list[tuple[dict, str]], rel: str, root: str) -> str:
    shown = hits[:MAX_POINT_ITEMS]
    lines = [f"Reverie memory anchored to this edit of {rel}. Items marked "
             "STANDING are decisions the team already made: do NOT write code "
             "that contradicts one. If the task seems to require it, stop and "
             "ask the user first (name the item and offer alternatives). "
             "Everything else is past belief: verify against the code."]
    for it, why in shown:
        tag = "STANDING " if _is_standing(it) else ""
        lines.append(f"- [{it['id'][:8]}] {tag}{it['kind']} ({why}): "
                     f"{it['text'].strip()}")
        lines.append(f"  {freshness(it, rel, root)}")
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


# ---------------------------------------------------------------------------
# point of action (harness-neutral; a harness package wraps it in its schema)
# ---------------------------------------------------------------------------

def _delivered_file(cwd: str, session: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session or "nosession")
    return spool.repo_dir(cwd) / f"delivered-{safe}.json"


def _already(cwd: str, session: str) -> set[str]:
    try:
        return set(json.loads(_delivered_file(cwd, session).read_text()))
    except (OSError, ValueError):
        return set()


def _remember(cwd: str, session: str, ids: set[str]) -> None:
    try:
        path = _delivered_file(cwd, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(ids)))
    except OSError:
        pass


def point_of_action(cwd: str, session: str, target: str,
                    extra_text: str = "") -> str:
    """Plain-text memory for an imminent edit of ``target`` (a path), or ''.

    ``extra_text`` is the content about to be written, so a symbol the edit
    introduces also matches. Never raises."""
    try:
        root = spool.repo_root(cwd)
        absolute = target if os.path.isabs(target) else os.path.join(cwd, target)
        rel = os.path.relpath(os.path.realpath(absolute), os.path.realpath(root))
        if rel.startswith(".."):
            return ""
        conn = open_db()
        if conn is None:
            return ""
        try:
            key = repo_key(cwd)
            if is_disabled(conn, key):
                return ""
            mem = memory(conn, key, root)
        finally:
            conn.close()
        if not mem:
            return ""
        try:
            with open(absolute, "rb") as fh:
                text = fh.read(MAX_FILE_BYTES).decode("utf-8", "replace")
        except OSError:
            text = ""
        text += "\n" + extra_text
        seen = _already(cwd, session)
        hits = [h for h in match(mem, rel, text) if h[0]["id"] not in seen]
        if not hits:
            return ""
        shown = hits[:MAX_POINT_ITEMS]
        _remember(cwd, session, seen | {h[0]["id"] for h in shown})
        return render_point(shown, rel, root)
    except Exception:
        return ""


def show(conn: sqlite3.Connection, id_prefix: str, root: str) -> str:
    """Full text of one item or legacy conclusion, by id prefix."""
    like = id_prefix + "%"
    found: list[dict] = []
    try:
        rows = conn.execute("SELECT * FROM items WHERE id LIKE ?", (like,)).fetchall()
        found += [items_mod._row(r) | {"origin": "item"} for r in rows]
    except sqlite3.Error:
        pass
    if not found:
        found = [m for m in _legacy_conclusions(conn, root)
                 if m["id"].startswith(id_prefix)]
    if len(found) != 1:
        return (f"No item matches {id_prefix!r}." if not found else
                f"{len(found)} items match {id_prefix!r}; use more characters.")
    it = found[0]
    a = it.get("anchor") or {}
    lines = [f"[{it['id']}] {it['kind']} ({it.get('status', 'active')})",
             it["text"].strip(), ""]
    if a.get("files"):
        lines.append("files: " + ", ".join(a["files"]))
    if a.get("symbols"):
        lines.append("symbols: " + ", ".join(a["symbols"]))
    lines.append(f"source: {it.get('source')} | author: {it.get('author', 'agent')} "
                 f"| level: {it.get('level', 'repo')}")
    if it.get("evidence"):
        lines.append("evidence: " + it["evidence"])
    lines.append(freshness(it, (a.get("files") or ["."])[0], root))
    return "\n".join(lines)
