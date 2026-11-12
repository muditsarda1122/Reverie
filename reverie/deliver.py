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

The same memory reaches harnesses with no hooks through the MCP tools
(``startup_text`` in ``summary``, ``query_context`` in ``query``): measured on
httpx, a codex/opencode agent was told "the brain holds 0 conclusions" while a
standing rule existed, and broke it. Nothing here names a harness.

Liveness: a to-do or an open question describes the repo at one moment. Once
the code it was about has changed (or a stale check flagged it) it is not
delivered at all; a stale-flagged rule or decision is delivered in its own
"possibly outdated" group, never in the always-apply list.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from . import collapse, spool, usage
from . import items as items_mod
from .redact import redact

MAX_POINT_ITEMS = 3
MAX_CONTEXT_CHARS = 6000          # harnesses cap hook context (10k chars)
MAX_STARTUP_CHARS = 8000          # default size of the session-start block
MIN_STARTUP_CHARS = 1500          # below this the rules themselves would not fit
MAX_RULE_LINES = 30
MAX_WIDE_LINES = 15
MAX_OUTDATED_LINES = 8
MAX_LINE_CHARS = 320
MAX_QUERY_HITS = 6
QUERY_MIN_SIM = 0.30
#: kinds that describe the repo at one moment, not a lasting fact
TRANSIENT_KINDS = ("work_state", "open_question")
INDEX_MAX_LINES = 80
INDEX_TITLE_WORDS = 14
MAX_FILE_BYTES = 1_000_000

# order in the index: what changed agent behaviour most comes first
_KIND_ORDER = {"rule": 0, "decision": 1, "rejected_approach": 2,
               "work_state": 3, "preference": 4, "environment": 5,
               "external_context": 6, "open_question": 7, "observation": 9}


def startup_cap() -> int:
    """Size cap of the session-start block: env ``REVERIE_STARTUP_CHARS``, else
    ``deliver.startup_chars`` in config.yaml (read with a regex: this module must
    not import yaml), else the default. Clamped to a sane range."""
    raw = os.environ.get("REVERIE_STARTUP_CHARS")
    if not raw:
        try:
            text = (spool.reverie_home() / "config.yaml").read_text(encoding="utf-8")
            m = re.search(r"(?m)^deliver:\s*\n(?:[ \t]+.*\n)*?[ \t]+startup_chars:\s*(\d+)", text)
            raw = m.group(1) if m else ""
        except OSError:
            raw = ""
    try:
        return max(MIN_STARTUP_CHARS, min(int(raw), 9500)) if raw else MAX_STARTUP_CHARS
    except ValueError:
        return MAX_STARTUP_CHARS


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
            "SELECT id, cognition, conclusion_type, grounding_json, created_at, "
            "metadata_json "
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
            "level": "repo", "status": "active",
            "meta": json.loads(r["metadata_json"] or "{}")})
    return out


def memory(conn: sqlite3.Connection, key: str, root: str) -> list[dict]:
    typed = items_mod.active(conn, key)
    for it in typed:
        it["origin"] = "item"
        it["grounding_commit"] = it.get("grounding_commit")
    typed = collapse.collapse(typed)          # near-duplicates are shown once
    return typed + _legacy_conclusions(conn, root)


# ---------------------------------------------------------------------------
# liveness + the shared startup / query text
# ---------------------------------------------------------------------------

def is_stale_flagged(it: dict) -> bool:
    return bool((it.get("meta") or {}).get("stale"))


def is_live(it: dict, root: str) -> bool:
    """May this item still be delivered?

    A to-do ("implement X next") or an open question describes the repo at one
    moment. Once the code it was about changed, or a stale check flagged it,
    it is dead weight that misleads (measured: a fresh agent was handed "the
    transports are not written yet" three commits after they shipped)."""
    if it.get("kind") not in TRANSIENT_KINDS:
        return True
    if is_stale_flagged(it):
        return False
    commit = it.get("grounding_commit")
    if not commit or not spool._git(["cat-file", "-t", commit], root):
        return True                       # cannot tell: keep it
    files = (it.get("anchor") or {}).get("files") or []
    if files:
        return not spool._git(["diff", "--name-only", commit, "--", *files],
                              root).strip()
    return spool._git(["rev-list", "--count", f"{commit}..HEAD"],
                      root) in ("", "0")


def _clip(text: str, limit: int = MAX_LINE_CHARS) -> str:
    one = " ".join(redact(text or "").split())      # render-time pass
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _line(it: dict) -> str:
    tag = "" if it["kind"] == "rule" else f"{it['kind']}: "
    seen = f" (seen {it['seen_n']} times)" if int(it.get("seen_n") or 1) > 1 else ""
    warn = (" [agents contradicted this repeatedly: confirm or fix it]"
            if (it.get("meta") or {}).get("challenged") else "")
    return f"- [{it['id'][:8]}] {tag}{_clip(it['text'])}{seen}{warn}"


def _capped_ex(lines: list[str], cap: int, more: str, budget: int = 10**9) -> tuple[list[str], int, str]:
    """At most ``cap`` lines and ``budget`` characters; a cut is always said. Returns the lines, how many of the
    given lines were kept, and what stopped the list: "lines" (the line cap), "characters" (the budget) or ""."""
    out: list[str] = []
    used = 0
    why = ""
    for ln in lines[:cap]:
        if used + len(ln) + 1 > budget:
            why = "characters"
            break
        out.append(ln)
        used += len(ln) + 1
    kept = len(out)
    if kept < len(lines):
        why = why or "lines"
        out.append(f"(+{len(lines) - kept} more {more})")
    return out, kept, why


def _capped(lines: list[str], cap: int, more: str, budget: int = 10**9) -> list[str]:
    return _capped_ex(lines, cap, more, budget)[0]


class StartTrace:
    """What the start text did with every rule: told, or cut and why. Filled by ``startup_text`` when a trace is
    passed (``startup_report``); nothing else changes. ``rules``: dicts with source, key, text, told, reason, limit
    (limit is "lines" or "characters"). ``index``: how many memory items the text lists in its index."""

    def __init__(self) -> None:
        self.rules: list[dict] = []
        self.index = 0
        self.cap = startup_cap()

    def add(self, source: str, key: str, text: str, told: bool, reason: str = "", limit: str = "") -> None:
        self.rules.append({"source": source, "key": key, "text": text, "told": told, "reason": reason, "limit": limit})


def _n(n: int) -> str:
    return f"{n:,}"


def _rules_files_text(cwd: str) -> str:
    """The user's rules.md files, header boilerplate stripped."""
    from . import rules
    out = []
    loaded = rules.load(cwd, hide_shared=True)
    for key, label in (("user", "Your personal rules"),
                       ("repo", "This repo's rules (team)")):
        body = "\n".join(
            ln for ln in loaded[key].splitlines()
            if ln.strip() and not ln.startswith("# Reverie rules")
            and not ln.startswith("One rule per bullet"))
        if body.strip():
            out.append(f"### {label}\n{body.strip()}")
    return "\n\n".join(out)


def startup_text(cwd: str, *, log: bool = True, mem: list[dict] | None = None) -> str:
    """The session-start text (see ``_start``)."""
    return _start(cwd, log, mem, None)


def startup_report(cwd: str, *, log: bool = False, mem: list[dict] | None = None) -> tuple[str, StartTrace]:
    """The same text, plus what happened to every rule in it (told or cut, and which limit cut it)."""
    trace = StartTrace()
    return _start(cwd, log, mem, trace), trace


def _start(cwd: str, log: bool, mem: list[dict] | None, trace: StartTrace | None) -> str:
    """Everything an agent should know at the start of a session, within
    the configured cap (``deliver.startup_chars``, default 8000): the user's rules, rules stated in earlier sessions
    (stale ones set apart), and the headline index of everything else.
    '' when disabled or empty. Never raises; read-only. ``log=False`` is for viewers (the
    UI): looking at the block is not a delivery and must not enter the usage log. ``mem`` is
    the viewer's already-loaded ``memory()`` result, to spare the (slow) collapse of a second load."""
    try:
        cap = startup_cap()
        root = spool.repo_root(cwd)
        files_text = _rules_files_text(cwd)
        loaded: list[dict] = []
        conn = open_db()
        if conn is not None:
            try:
                key = repo_key(cwd)
                if is_disabled(conn, key):
                    return ""
                loaded = mem if mem is not None else memory(conn, key, root)
            finally:
                conn.close()
        mem = [m for m in loaded if is_live(m, root)]
        from . import context as _ctx
        shared = _ctx.shared_rule_norms(root)
        if shared:                       # a rule already in the team document: show it once
            mem = [m for m in mem if not (m.get("kind") in ("rule", "preference")
                                          and _ctx.norm(m.get("text") or "") in shared)]
        flagged = [m for m in mem if is_stale_flagged(m)]
        fresh = [m for m in mem if not is_stale_flagged(m)]
        wide = [m for m in fresh if m.get("repo_key") is None
                and m.get("origin") == "item"
                and m["kind"] in ("rule", "preference")]
        wide_ids = {m["id"] for m in wide}
        repo_rules = [m for m in fresh if m["kind"] == "rule"
                      and m["id"] not in wide_ids]
        listed = wide_ids | {m["id"] for m in repo_rules}
        rest = [m for m in fresh if m["id"] not in listed]

        parts: list[str] = []
        left = cap - 700          # keep room for the index + notes
        bullets_files: list[tuple[str, str]] = []
        if trace is not None:
            from . import rules as _rules
            ld = _rules.load(cwd, hide_shared=True)
            bullets_files = [(k, b) for k in ("user", "repo") for b in _rules.bullets(ld[k])]
        if files_text:
            if len(files_text) > 2400:
                files_text = files_text[:2400].rsplit("\n", 1)[0] + (
                    "\n(…more in the rules files; `reverie rules list`)")
            parts.append("Reverie rules (the user's own words; always apply):\n"
                         + files_text)
            left -= len(parts[-1])
        from . import context as team_context
        team_text = team_context.render(root, budget=min(3600, max(left // 2, 800)), trace=trace)
        if team_text:
            parts.insert(0, team_text)       # org/team rules outrank the personal ones
            left -= len(team_text)
        if wide:
            wb = min(1500, max(left, 0))
            lines_w, kept, why = _capped_ex([_line(m) for m in wide], MAX_WIDE_LINES, "— `reverie rules list`", wb)
            if trace is not None:
                for i, m in enumerate(wide):
                    trace.add("memory", m["id"], m.get("text") or "", i < kept,
                              "" if i < kept else (f"agents are told at most {MAX_WIDE_LINES} of your rules that apply in every repo"
                                                   if why == "lines" else
                                                   f"agents are told at most about {_n(wb)} characters of your rules that apply in every repo"), why)
            body = "\n".join(lines_w)
            parts.append("Your preferences and rules across all repos:\n" + body)
            left -= len(parts[-1])
        if repo_rules:
            rb = min(4500, max(left - 900, 300))
            lines_r, kept, why = _capped_ex([_line(m) for m in repo_rules], MAX_RULE_LINES,
                                            "— open any with `reverie show <id>`", rb)
            if trace is not None:
                for i, m in enumerate(repo_rules):
                    trace.add("memory", m["id"], m.get("text") or "", i < kept,
                              "" if i < kept else (f"agents are told at most {MAX_RULE_LINES} remembered rules for this repo"
                                                   if why == "lines" else
                                                   f"agents are told at most about {_n(rb)} characters of remembered rules for this repo"), why)
            body = "\n".join(lines_r)
            parts.append("Rules you stated in earlier sessions:\n" + body)
            left -= len(parts[-1])
        if flagged:
            lines = []
            for m in flagged:
                st = (m.get("meta") or {}).get("stale") or {}
                lines.append(_line(m) + f"  [a later commit {str(st.get('commit', ''))[:8]} "
                             f"may contradict this: {_clip(str(st.get('reason', '')), 160)}]")
            body = "\n".join(_capped(lines, MAX_OUTDATED_LINES, "possibly outdated",
                                     min(1800, max(left, 300))))
            parts.append("Possibly outdated (a later commit may contradict these; verify "
                         "against the code before relying on them):\n" + body)
        used = sum(len(p) for p in parts) + 2 * len(parts)
        room = max(0, cap - used - 220)
        limit = max(0, min(INDEX_MAX_LINES, room // 150))
        index = headline_index(rest, limit=limit) if rest and limit else ""
        if index:
            parts.append(index)
            if trace is not None:
                trace.index = sum(1 for ln in index.splitlines() if ln.startswith("- ["))
        text = "\n\n".join(parts)
        if trace is not None:
            kept_files = "\n\n".join(parts)
            for kind, b in bullets_files:
                told = b in kept_files
                trace.add("rules_file_" + kind, b, b, told, "" if told else "agents are told at most 2,400 characters from your rules files",
                          "" if told else "characters")
        if log:
            usage.log_delivery(cwd, "start", [m["id"] for m in wide + repo_rules + flagged]
                               + [m["id"] for m in rest[:limit]])
        if len(text) > cap:
            text = text[: cap - 80].rstrip() + (
                "\n(…truncated; `reverie rules list` and `reverie digest` show the rest)")
            if trace is not None:               # a rule that was told above but fell off the end of the whole text
                for r in trace.rules:
                    if r["told"]:
                        marker = f"[{r['key'][:8]}]" if r["source"] == "memory" else (f"{r['key']}]" if r["source"] == "team" else r["text"])
                        if marker not in text:
                            r.update(told=False, limit="characters",
                                     reason=f"the whole start text stops at {_n(cap)} characters")
        return text
    except Exception:
        return ""


def _mentions(it: dict, query_lc: str) -> str | None:
    a = it.get("anchor") or {}
    for sym in a.get("symbols") or []:
        for part in {sym, sym.split(".")[-1], sym.split("::")[-1]}:
            if len(part) >= 3 and re.search(
                    rf"(?<![a-z0-9_]){re.escape(part.lower())}(?![a-z0-9_])", query_lc):
                return f"symbol `{sym}`"
    for f in a.get("files") or []:
        base = _norm(f).rsplit("/", 1)[-1].lower()
        if _norm(f).lower() in query_lc or (len(base) >= 4 and base in query_lc):
            return f"file `{f}`"
    return None


def query_context(cwd: str, query: str, embed=None) -> str:
    """Typed memory for an agent's question (the MCP ``query`` path): every
    standing rule that applies repo-wide, plus items that match the question
    by anchor or by embedding similarity, each with its freshness. '' when
    there is nothing. ``embed`` is a callable text -> vector (optional).
    Never raises; read-only."""
    try:
        import numpy as np

        from .embeddings import from_blob
        root = spool.repo_root(cwd)
        conn = open_db()
        if conn is None:
            return ""
        try:
            key = repo_key(cwd)
            if is_disabled(conn, key):
                return ""
            rows = items_mod.active(conn, key, with_embedding=True)
        finally:
            conn.close()
        rows = [r for r in rows if is_live(r, root)]
        for r in rows:
            r["origin"] = "item"
        q = (query or "").lower()
        qv = None
        if embed is not None:
            try:
                v = np.asarray(embed(query), dtype=np.float32)
                qv = v / (float(np.linalg.norm(v)) or 1.0)
            except Exception:
                qv = None
        scored: list[tuple[float, dict, str]] = []
        for r in rows:
            why = _mentions(r, q)
            sim = 0.0
            if qv is not None and r.get("embedding"):
                iv = np.asarray(from_blob(r["embedding"]), dtype=np.float32)
                sim = float(iv @ qv / (float(np.linalg.norm(iv)) or 1.0))
            if why is None and sim >= QUERY_MIN_SIM:
                why = f"similar to your question ({sim:.2f})"
            if why:
                scored.append((1.0 + sim if why.startswith(("symbol", "file")) else sim, r, why))
        scored.sort(key=lambda t: -t[0])
        hits = scored[:MAX_QUERY_HITS]
        hit_ids = {r["id"] for _, r, _ in hits}
        standing = [r for r in rows if r["kind"] == "rule" and r["id"] not in hit_ids
                    and not is_stale_flagged(r)]
        if not hits and not standing:
            return ""
        lines = ["Reverie typed memory (the user's decisions and findings; past beliefs "
                 "to verify against the code). Items marked STANDING are decisions "
                 "already made: do not write code that contradicts one; if the task "
                 "seems to need it, stop and ask the user first."]
        for _, r, why in hits:
            tag = "STANDING " if _is_standing(r) and not is_stale_flagged(r) else ""
            lines.append(f"- [{r['id'][:8]}] {tag}{r['kind']} ({why}): {_clip(r['text'], 600)}")
            anchor_file = ((r.get("anchor") or {}).get("files") or ["."])[0]
            lines.append(f"  {freshness(r, anchor_file, root)}")
            note = stale_note(r)
            if note:
                lines.append(note)
        if standing:
            lines.append("Other standing rules for this repo:")
            lines += _capped([_line(r) for r in standing], MAX_RULE_LINES,
                             "— open any with `reverie show <id>`")
        usage.log_delivery(cwd, "query", [r["id"] for _, r, _ in hits] + [r["id"] for r in standing])
        return "\n".join(lines)[:MAX_CONTEXT_CHARS]
    except Exception:
        return ""


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
        flag = " [MAY BE STALE]" if (it.get("meta") or {}).get("stale") else ""
        lines.append(f"- [{it['id'][:8]}] {it['kind']}: {title(it['text'])} "
                     f"(anchor: {_anchor_label(it)}){flag}")
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


def stale_note(it: dict) -> str:
    st = (it.get("meta") or {}).get("stale")
    if not st:
        return ""
    return (f"  WARNING: may be stale: commit {st.get('commit')} "
            f"({st.get('message', '')}) contradicts it: {st.get('reason', '')}")


def render_point(hits: list[tuple[dict, str]], rel: str, root: str) -> str:
    shown = hits[:MAX_POINT_ITEMS]
    lines = [f"Reverie memory anchored to this edit of {rel}. Items marked "
             "STANDING are decisions the team already made: do NOT write code "
             "that contradicts one. If the task seems to require it, stop and "
             "ask the user first (name the item and offer alternatives). "
             "Everything else is past belief: verify against the code."]
    for it, why in shown:
        tag = "STANDING " if _is_standing(it) and not is_stale_flagged(it) else ""
        lines.append(f"- [{it['id'][:8]}] {tag}{it['kind']} ({why}): "
                     f"{it['text'].strip()}")
        lines.append(f"  {freshness(it, rel, root)}")
        if stale_note(it):
            lines.append(stale_note(it))
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
    introduces also matches. Path-scoped team rules for the file come first.
    Never raises."""
    try:
        from . import context as team_context
        root = spool.repo_root(cwd)
        absolute = target if os.path.isabs(target) else os.path.join(cwd, target)
        rel = os.path.relpath(os.path.realpath(absolute), os.path.realpath(root))
        team, ids = ("", set()) if rel.startswith("..") or _repo_disabled(cwd) else \
            team_context.point_text(root, rel, _already(cwd, session))
        if ids:
            _remember(cwd, session, _already(cwd, session) | ids)
        mem_text = _memory_point(cwd, session, target, extra_text)
        return "\n\n".join(t for t in (team, mem_text) if t)
    except Exception:
        return ""


def _repo_disabled(cwd: str) -> bool:
    conn = open_db()
    if conn is None:
        return False
    try:
        return is_disabled(conn, repo_key(cwd))
    finally:
        conn.close()


def _memory_point(cwd: str, session: str, target: str, extra_text: str) -> str:
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
        mem = [m for m in mem if is_live(m, root)]
        hits = [h for h in match(mem, rel, text) if h[0]["id"] not in seen]
        if not hits:
            return ""
        shown = hits[:MAX_POINT_ITEMS]
        _remember(cwd, session, seen | {h[0]["id"] for h in shown})
        usage.log_delivery(cwd, "edit", [h[0]["id"] for h in shown])
        return render_point(shown, rel, root)
    except Exception:
        return ""


def show(conn: sqlite3.Connection, id_prefix: str, root: str) -> str:
    """Full text of one item or legacy conclusion, by id prefix."""
    from . import context as team_context
    ctx = team_context.show(root, id_prefix)
    if ctx:
        return ctx
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
             redact(it["text"].strip()), ""]
    if a.get("files"):
        lines.append("files: " + ", ".join(a["files"]))
    if a.get("symbols"):
        lines.append("symbols: " + ", ".join(a["symbols"]))
    lines.append(f"source: {it.get('source')} | author: {it.get('author', 'agent')} "
                 f"| level: {it.get('level', 'repo')}")
    if it.get("evidence"):
        lines.append("evidence: " + redact(it["evidence"]))
    if it.get("created_at"):
        lines.append(f"remembered: {str(it['created_at'])[:10]}")
    if it.get("last_verified"):
        lines.append(f"last verified: {str(it['last_verified'])[:10]}")
    meta = it.get("meta") or {}
    tally = usage.words(meta.get("usage") or {})
    if tally:
        lines.append(f"used by agents: {tally}")
    if meta.get("challenged"):
        lines.append("challenged: agents contradicted it repeatedly; agents are told to confirm or fix it before relying on it")
    lines.append(freshness(it, (a.get("files") or ["."])[0], root))
    if stale_note(it):
        lines.append(stale_note(it).strip())
    return "\n".join(lines)
