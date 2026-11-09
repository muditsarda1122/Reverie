"""Layered team context: org > team > repo > personal. STDLIB ONLY (hooks load this).

Plain markdown documents are the source; nothing is copied into the database.
Each bullet is read as an item with a stable id (hash of layer + file + text),
so usage and staleness data stick to it. See docs/ROADMAP.md "Team layer v1".

    <repo>/.reverie/context/*.md        repo layer
    [sources] org = "..", team = ".."   in <repo>/.reverie/context.toml or
                                        ~/.reverie/context.toml: folders of .md
    ~/.reverie/context/*.md             personal layer (documents; the personal
                                        rules.md keeps working as before)

Format (what a product manager writes): sections headed Rules / Always / Never /
Must hold rules, one per bullet; every other bullet is guidance (a pointer).
A bullet ending ``(locked)`` cannot be overridden by a lower layer; an optional
trailing ``{id: name}`` lets another layer answer the same rule. A first line
``applies-to: repo-a, repo-b`` limits a document to those repos (default: all
repos that point at its folder).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import spool

LAYERS = ("org", "team", "repo", "personal")          # highest first
RANK = {name: i for i, name in enumerate(LAYERS)}
_RULE_HEADING = re.compile(r"\b(rules?|always|never|must|non-negotiables?|required)\b", re.I)
_TAG = re.compile(r"\s*\{id:\s*([\w.-]+)\s*\}\s*")
_LOCKED = re.compile(r"\s*\(locked\)\s*$", re.I)
_BULLET = re.compile(r"^(\s*)[-*]\s+(.*\S)\s*$")
MAX_DOC_BYTES = 200_000


@dataclass
class CtxItem:
    id: str
    layer: str
    kind: str                  # "rule" | "guidance"
    text: str
    file: str                  # path as given to the reader (display)
    section: str = ""
    locked: bool = False
    tag: str = ""              # the {id: ...} name, "" when none
    applies_to: tuple[str, ...] = ()
    meta: dict = field(default_factory=dict)


@dataclass
class Conflict:
    key: str                   # the shared {id} or "judge:<hash>"
    winner: CtxItem
    overridden: list[CtxItem]
    locked: bool               # the winner is locked: the lower attempt is a violation
    reason: str = ""


@dataclass
class Resolved:
    items: list[CtxItem]                 # everything that applies here, highest layer first
    effective: list[CtxItem]             # items not overridden
    conflicts: list[Conflict]
    stale: dict[str, dict]               # item id -> {"commit","reason"}


# ---------------------------------------------------------------------------
# reading documents
# ---------------------------------------------------------------------------

def item_id(layer: str, file: str, text: str) -> str:
    return hashlib.sha1(f"{layer}\0{Path(file).name}\0{text}".encode()).hexdigest()[:10]


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_DOC_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def parse_document(text: str, layer: str, file: str) -> list[CtxItem]:
    """Bullets of one markdown document as items. Never raises."""
    applies: tuple[str, ...] = ()
    lines = text.splitlines()
    out: list[CtxItem] = []
    section, in_rules = "", False
    cur: list[str] | None = None
    cur_indent = 0

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        body = " ".join(" ".join(cur).split())
        cur = None
        locked = bool(_LOCKED.search(body))
        body = _LOCKED.sub("", body).strip()
        m = _TAG.search(body)
        tag = m.group(1) if m else ""
        body = _TAG.sub(" ", body).strip()
        body = _LOCKED.sub("", body).strip()
        locked = locked or bool(re.search(r"\(locked\)", body, re.I))
        body = re.sub(r"\s*\(locked\)\s*", " ", body, flags=re.I).strip()
        if not body:
            return
        out.append(CtxItem(item_id(layer, file, body), layer,
                           "rule" if in_rules else "guidance", body, file,
                           section, locked, tag, applies))

    seen_content = False
    for raw in lines:
        s = raw.strip()
        if not seen_content and s.lower().startswith("applies-to:"):
            applies = tuple(x.strip().lower() for x in s.split(":", 1)[1].split(",")
                            if x.strip())
            continue
        if s:
            seen_content = True
        head = re.match(r"^#{1,6}\s+(.*)$", s)
        if head:
            flush()
            section = head.group(1).strip()
            in_rules = bool(_RULE_HEADING.search(section))
            continue
        b = _BULLET.match(raw)
        if b:
            flush()
            cur, cur_indent = [b.group(2)], len(b.group(1))
        elif cur is not None and s and (len(raw) - len(raw.lstrip())) > cur_indent:
            cur.append(s)                      # wrapped continuation line
        elif not s:
            flush()
        else:
            flush()                            # prose line: not an item
    flush()
    # the applies-to header belongs to every item of the document
    return [CtxItem(**{**it.__dict__, "applies_to": applies}) for it in out]


def repo_names(root: str) -> set[str]:
    """Names this repo answers to for ``applies-to``: folder + remote name."""
    names = {Path(root).name.lower()}
    url = spool._git(["config", "--get", "remote.origin.url"], root)
    if url:
        base = url.strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        names.add(re.sub(r"\.git$", "", base).lower())
    return names


def _sources(root: str) -> dict[str, list[Path]]:
    """layer -> folders, from the repo's and the personal context.toml
    (personal file wins on a clash: it is the user's own machine)."""
    found: dict[str, list[Path]] = {}
    for toml_path, base in ((Path(root) / ".reverie" / "context.toml", Path(root)),
                            (spool.reverie_home() / "context.toml", Path.home())):
        try:
            data = tomllib.loads(_read(toml_path) or "")
        except tomllib.TOMLDecodeError:
            continue
        for layer, raw in (data.get("sources") or {}).items():
            if layer not in ("org", "team") or not isinstance(raw, str):
                continue
            p = Path(os.path.expanduser(raw))
            p = p if p.is_absolute() else (base / p)
            found.setdefault(layer, [])
            if p not in found[layer]:
                found[layer].append(p)
    return found


def _md_files(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.glob("*.md") if p.is_file())
    except OSError:
        return []


def documents(root: str) -> list[tuple[str, Path]]:
    """[(layer, path)] of every context document that may apply to this repo."""
    docs: list[tuple[str, Path]] = []
    for layer, folders in _sources(root).items():
        for folder in folders:
            docs += [(layer, p) for p in _md_files(folder)]
    docs += [("repo", p) for p in _md_files(Path(root) / ".reverie" / "context")]
    docs += [("personal", p) for p in _md_files(spool.reverie_home() / "context")]
    return docs


def load_items(root: str) -> list[CtxItem]:
    names = repo_names(root)
    items: list[CtxItem] = []
    for layer, path in documents(root):
        for it in parse_document(_read(path), layer, str(path)):
            if it.applies_to and "*" not in it.applies_to and not (set(it.applies_to) & names):
                continue
            items.append(it)
    items.sort(key=lambda i: RANK[i.layer])
    return items


# ---------------------------------------------------------------------------
# sidecar state (judge conflicts, stale flags): never written into documents
# ---------------------------------------------------------------------------

def state_path() -> Path:
    return spool.reverie_home() / "context_state.json"


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# precedence and conflicts
# ---------------------------------------------------------------------------

def resolve(root: str, items: list[CtxItem] | None = None,
            state: dict | None = None) -> Resolved:
    """Apply precedence. Higher layer wins; a locked rule beats everything.
    Overridden items are kept (listed as conflicts), never dropped."""
    items = load_items(root) if items is None else items
    state = load_state() if state is None else state
    by_id = {i.id: i for i in items}
    conflicts: list[Conflict] = []
    overridden: set[str] = set()

    groups: dict[str, list[CtxItem]] = {}
    for it in items:
        if it.tag and it.kind == "rule":
            groups.setdefault(it.tag, []).append(it)
    for tag, grp in groups.items():
        layers = {g.layer for g in grp}
        if len(layers) < 2:
            continue
        grp = sorted(grp, key=lambda g: (RANK[g.layer], not g.locked))
        win, rest = grp[0], [g for g in grp[1:] if g.layer != grp[0].layer]
        if not rest:
            continue
        conflicts.append(Conflict(tag, win, rest, win.locked))
        overridden.update(r.id for r in rest)

    for c in state.get("conflicts") or []:            # judge-found, cached by id
        a, b = by_id.get(c.get("a", "")), by_id.get(c.get("b", ""))
        if not a or not b or a.layer == b.layer:
            continue
        win, lose = (a, b) if (RANK[a.layer], not a.locked) <= (RANK[b.layer], not b.locked) else (b, a)
        if lose.id in overridden:
            continue
        conflicts.append(Conflict("judge:" + win.id, win, [lose], win.locked,
                                  str(c.get("reason", ""))))
        overridden.add(lose.id)

    stale = {k: v for k, v in (state.get("stale") or {}).items() if k in by_id}
    return Resolved(items, [i for i in items if i.id not in overridden], conflicts, stale)


# ---------------------------------------------------------------------------
# delivery text
# ---------------------------------------------------------------------------

def _one(text: str, limit: int) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def render(root: str, *, budget: int = 3600, rule_chars: int = 300) -> str:
    """The team-context block for a session start: rules always (by layer,
    bounded), conflicts shown, guidance as pointers. '' when nothing applies."""
    try:
        res = resolve(root)
    except Exception:
        return ""
    if not res.items:
        return ""
    lines: list[str] = []
    rules = [i for i in res.effective if i.kind == "rule"
             and i.id not in res.stale]
    if rules:
        lines.append("Team context rules (higher layer wins; always apply):")
        for layer in LAYERS:
            for it in rules:
                if it.layer == layer:
                    lock = " (locked: cannot be overridden)" if it.locked else ""
                    lines.append(f"- [{layer} {it.id}] {_one(it.text, rule_chars)}{lock}")
    shown = [c for c in res.conflicts]
    if shown:
        lines.append("Conflicts between layers (both shown; the higher layer applies):")
        for c in shown:
            for lo in c.overridden:
                tail = "; a locked rule, do not follow the lower one" if c.locked else ""
                why = f" ({c.reason})" if c.reason else ""
                lines.append(f"- [{c.winner.layer} {c.winner.id}] \"{_one(c.winner.text, 120)}\" "
                             f"overrides [{lo.layer} {lo.id}] \"{_one(lo.text, 120)}\"{why}{tail}")
    outdated = [i for i in res.items if i.id in res.stale]
    if outdated:
        lines.append("Possibly outdated team context (a later commit may contradict it; verify):")
        for it in outdated:
            st = res.stale[it.id]
            lines.append(f"- [{it.layer} {it.id}] {_one(it.text, 160)}  [commit "
                         f"{str(st.get('commit', ''))[:8]}: {_one(str(st.get('reason', '')), 140)}]")
    guide = [i for i in res.effective if i.kind == "guidance" and i.id not in res.stale]
    if guide:
        lines.append("Team guidance (open one with `reverie show <id>`):")
        for it in guide:
            lines.append(f"- [{it.layer} {it.id}] {_one(it.section + ': ' if it.section else '', 40)}"
                         f"{_one(it.text, 90)}")
    out: list[str] = []
    used = 0
    for ln in lines:
        if used + len(ln) + 1 > budget:
            out.append(f"(…{len(lines) - len(out)} more lines; `reverie context list`)")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def find(root: str, id_prefix: str) -> CtxItem | None:
    hits = [i for i in load_items(root) if i.id.startswith(id_prefix)]
    return hits[0] if len(hits) == 1 else None


def show(root: str, id_prefix: str) -> str:
    it = find(root, id_prefix)
    if it is None:
        return ""
    res = resolve(root)
    lines = [f"[{it.id}] {it.layer} {it.kind}" + (" (locked)" if it.locked else ""),
             it.text, "", f"document: {it.file}"]
    if it.section:
        lines.append(f"section: {it.section}")
    if it.applies_to:
        lines.append("applies to: " + ", ".join(it.applies_to))
    for c in res.conflicts:
        if it.id == c.winner.id or it.id in {o.id for o in c.overridden}:
            lines.append(f"conflict: [{c.winner.layer}] wins over "
                         f"{', '.join('[' + o.layer + '] ' + o.id for o in c.overridden)}")
    if it.id in res.stale:
        st = res.stale[it.id]
        lines.append(f"possibly outdated: commit {st.get('commit', '')[:8]}: {st.get('reason', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# adding a bullet (the PM path: no code knowledge, no format to remember)
# ---------------------------------------------------------------------------

def target_folder(root: str, layer: str) -> Path:
    if layer == "repo":
        return Path(root) / ".reverie" / "context"
    if layer == "personal":
        return spool.reverie_home() / "context"
    folders = _sources(root).get(layer)
    if not folders:
        raise ValueError(f"no {layer} source configured: add [sources] {layer} = \"<folder>\" "
                         f"to .reverie/context.toml")
    return folders[0]


def add(root: str, text: str, *, layer: str = "repo", rule: bool = True,
        locked: bool = False, tag: str = "", doc: str = "") -> Path:
    """Append a bullet to ``<folder>/<doc>.md`` (default ``rules.md`` or ``guidance.md``)."""
    if layer not in LAYERS:
        raise ValueError(f"layer must be one of {', '.join(LAYERS)}")
    text = " ".join(text.split())
    if not text:
        raise ValueError("text is empty")
    folder = target_folder(root, layer)
    name = (doc or ("rules" if rule else "guidance")).removesuffix(".md")
    if not re.fullmatch(r"[\w.-]+", name):
        raise ValueError("document name must be a plain file name")
    path = folder / f"{name}.md"
    heading = "## Rules" if rule else "## Guidance"
    body = _read(path)
    bullet = "- " + text + (f" {{id: {tag}}}" if tag else "") + (" (locked)" if locked else "")
    if bullet in body.splitlines():
        return path
    folder.mkdir(parents=True, exist_ok=True)
    heads = re.findall(r"(?m)^#{1,6}\s+(.*)$", body)
    last_is_rules = bool(heads) and bool(_RULE_HEADING.search(heads[-1]))
    if not body.strip():
        body = f"# {name.replace('-', ' ').title()}\n\n{heading}\n\n"
    elif not heads or last_is_rules != rule:
        body = body.rstrip("\n") + f"\n\n{heading}\n\n"
    elif not body.endswith("\n"):
        body += "\n"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(body + bullet + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path
