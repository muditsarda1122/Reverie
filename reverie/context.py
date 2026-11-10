"""Scoped team context. STDLIB ONLY (hooks load this).

A document declares WHERE it applies (an org, a team, the repo, paths inside
the repo, or the person); the only precedence is containment. See docs/ROADMAP.md
"Team layer v1, REVISION". Plain markdown is the source; nothing is copied into
the database. Each bullet is an item with a stable id (hash of scope + file +
text), so usage and staleness data stick to it.

    <repo>/.reverie/context/*.md      repo scope (``applies-to-paths:`` narrows it)
    [sources] org = [..], team = [..] folders of .md in <repo>/.reverie/context.toml
                                      or ~/.reverie/context.toml (a folder = a scope)
    ~/.reverie/context/*.md           the person's own scope

Document format (what anyone writes): sections headed Rules / Always / Never /
Must hold rules, one per bullet; every other bullet is guidance (a pointer).
A bullet may end with ``{id: name}`` (so another scope can answer it),
``{resolves: key}`` (a recorded decision settling a conflict) and ``(locked)``.
Optional header lines at the top: ``applies-to: repo-a, repo-b``,
``applies-to-paths: services/billing/**``, ``owner: whom to ask``,
``inside: acme`` (a team declaring which org it belongs to).

Containment (the default preset, widest first): org > team > repo > person.
A path glob is narrower than the same document without one. The narrower scope
answers the wider unless the wider is locked. Sibling scopes (two teams, two
orgs) have no order: a contradiction is UNRESOLVED and the agent is told to ask.
Reverie has no role list: authority is CODEOWNERS plus git identity, and the
optional ``authority`` file only attaches display labels to names.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import spool

LAYERS = ("org", "team", "repo", "personal")          # widest first (default preset)
RANK = {name: i for i, name in enumerate(LAYERS)}
_RULE_HEADING = re.compile(r"\b(rules?|always|never|must|non-negotiables?|required)\b", re.I)
_TAGS = re.compile(r"\{(id|resolves):\s*([\w.:-]+)\s*\}")
_LOCKED = re.compile(r"\(locked\)", re.I)
_BULLET = re.compile(r"^(\s*)[-*]\s+(.*\S)\s*$")
_HEADER = re.compile(r"^(applies-to-paths|applies-to|owner|inside):\s*(.*)$", re.I)
MAX_DOC_BYTES = 200_000


@dataclass
class CtxItem:
    id: str
    layer: str                 # origin scope kind: org | team | repo | personal
    kind: str                  # "rule" | "guidance"
    text: str
    file: str
    section: str = ""
    locked: bool = False
    tag: str = ""              # {id: ...}
    resolves: str = ""         # {resolves: ...}
    applies_to: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    owner: str = ""            # the document's `owner:` line (whom to ask)
    scope: str = ""            # scope name (folder for org/team)
    inside: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        where = f" {self.scope}" if self.layer in ("org", "team") and self.scope else ""
        return f"{self.layer}{where}"


@dataclass
class Conflict:
    key: str
    status: str                # overridden | locked | unresolved
    winner: CtxItem | None     # None when unresolved
    overridden: list[CtxItem]  # the losers, or both parties when unresolved
    reason: str = ""

    @property
    def locked(self) -> bool:
        return self.status == "locked"

    @property
    def items(self) -> list[CtxItem]:
        return ([self.winner] if self.winner else []) + self.overridden


@dataclass
class Resolved:
    items: list[CtxItem]
    effective: list[CtxItem]
    conflicts: list[Conflict]
    stale: dict[str, dict]
    hidden: set[str] = field(default_factory=set)   # personal duplicates of a team rule


def norm(text: str) -> str:
    """Comparison form of a rule's text: case, spacing and punctuation ignored."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


# ---------------------------------------------------------------------------
# globs (CODEOWNERS-style) and paths
# ---------------------------------------------------------------------------

def _glob_regex(pattern: str) -> re.Pattern[str]:
    p = pattern.strip()
    anchored = p.startswith("/") or "/" in p.strip("/")
    p = p.lstrip("/")
    out, i = [], 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    body = "".join(out).rstrip("/")
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile(f"^{prefix}{body}(?:/.*)?$")


def glob_match(pattern: str, rel: str) -> bool:
    return bool(_glob_regex(pattern).match(rel.removeprefix("./")))


def _literal_prefix(glob: str) -> str:
    parts = []
    for seg in glob.strip("/").split("/"):
        if any(c in seg for c in "*?["):
            break
        parts.append(seg)
    return "/".join(parts)


def _globs_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Could one file match a glob of ``a`` and a glob of ``b``? (conservative)"""
    for x in a:
        for y in b:
            px, py = _literal_prefix(x), _literal_prefix(y)
            if px == py or px == "" or py == "" or px.startswith(py + "/") \
                    or py.startswith(px + "/"):
                return True
    return False


# ---------------------------------------------------------------------------
# reading documents
# ---------------------------------------------------------------------------

def item_id(layer: str, file: str, text: str, scope: str = "") -> str:
    return hashlib.sha1(f"{layer}\0{scope}\0{Path(file).name}\0{text}".encode()
                        ).hexdigest()[:10]


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_DOC_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in value.split(",") if x.strip())


def parse_document(text: str, layer: str, file: str, scope: str = "") -> list[CtxItem]:
    """Bullets of one markdown document as items. Never raises."""
    applies: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    owner = inside = ""
    out: list[CtxItem] = []
    section, in_rules = "", False
    cur: list[str] | None = None
    cur_indent = 0
    seen_content = False

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        body = " ".join(" ".join(cur).split())
        cur = None
        locked = bool(_LOCKED.search(body))
        tags = {k.lower(): v for k, v in _TAGS.findall(body)}
        body = _LOCKED.sub(" ", _TAGS.sub(" ", body))
        body = " ".join(body.split())
        if not body:
            return
        out.append(CtxItem(item_id(layer, file, body, scope), layer,
                           "rule" if in_rules else "guidance", body, file, section,
                           locked, tags.get("id", ""), tags.get("resolves", ""),
                           scope=scope))

    for raw in text.splitlines():
        s = raw.strip()
        h = _HEADER.match(s) if not seen_content else None
        if h:
            key, val = h.group(1).lower(), h.group(2).strip()
            if key == "applies-to":
                applies = tuple(x.lower() for x in _split_csv(val))
            elif key == "applies-to-paths":
                paths = _split_csv(val)
            elif key == "owner":
                owner = val
            else:
                inside = val.lower()
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
            cur.append(s)
        else:
            flush()
    flush()
    return [CtxItem(**{**it.__dict__, "applies_to": applies, "paths": paths,
                       "owner": owner, "inside": inside}) for it in out]


def repo_names(root: str) -> set[str]:
    """Names this repo answers to for ``applies-to``: folder + remote name."""
    names = {Path(root).name.lower()}
    url = spool._git(["config", "--get", "remote.origin.url"], root)
    if url:
        base = url.strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        names.add(re.sub(r"\.git$", "", base).lower())
    return names


def scope_name(folder: Path) -> str:
    """Two path components, so ``acme/org`` and ``globex/org`` are two scopes."""
    return "/".join(folder.parts[-2:]) if len(folder.parts) > 1 else folder.name


def sources(root: str) -> dict[str, list[Path]]:
    """kind -> folders, from the repo's and the personal context.toml. A value is
    one path or a list of paths (several orgs and teams may share one repo)."""
    found: dict[str, list[Path]] = {}
    for toml_path, base in ((Path(root) / ".reverie" / "context.toml", Path(root)),
                            (spool.reverie_home() / "context.toml", Path.home())):
        try:
            data = tomllib.loads(_read(toml_path) or "")
        except tomllib.TOMLDecodeError:
            continue
        for layer, raw in (data.get("sources") or {}).items():
            if layer not in ("org", "team"):
                continue
            for entry in ([raw] if isinstance(raw, str) else
                          [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []):
                p = Path(os.path.expanduser(entry))
                p = Path(os.path.normpath(p if p.is_absolute() else base / p))
                found.setdefault(layer, [])
                if p not in found[layer]:
                    found[layer].append(p)
    return found


def _md_files(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.glob("*.md") if p.is_file())
    except OSError:
        return []


def documents(root: str) -> list[tuple[str, str, Path]]:
    """[(kind, scope name, path)] of every context document that may apply here."""
    docs: list[tuple[str, str, Path]] = []
    for layer, folders in sources(root).items():
        for folder in folders:
            docs += [(layer, scope_name(folder), p) for p in _md_files(folder)]
    docs += [("repo", Path(root).name, p) for p in _md_files(Path(root) / ".reverie" / "context")]
    docs += [("personal", "me", p) for p in _md_files(spool.reverie_home() / "context")]
    return docs


def doc_signature(root: str) -> str:
    """A hash of every applicable document's path and content: changes exactly when
    a document is added, removed or edited (the key for "run the judge again")."""
    h = hashlib.sha256()
    for layer, name, path in documents(root):
        h.update(f"{layer}|{name}|{path}\n".encode())
        h.update((_read(path) or "").encode("utf-8", "replace"))
    return h.hexdigest()[:16]


CONTRADICTION_HINT = "If two rules above seem to contradict, ask before acting."


def contradiction_hint(res: "Resolved") -> str:
    """A MITIGATION, not detection: when more than one org or team source applies,
    one fixed line nudging the agent to ask. It says nothing about whether a
    conflict exists; detection is ``context_check.find_conflicts``."""
    scopes = {(i.layer, i.scope) for i in res.items if i.layer in ("org", "team")}
    return CONTRADICTION_HINT if len(scopes) > 1 else ""


def load_items(root: str) -> list[CtxItem]:
    names = repo_names(root)
    items: list[CtxItem] = []
    for layer, name, path in documents(root):
        for it in parse_document(_read(path), layer, str(path), name):
            if it.applies_to and "*" not in it.applies_to and not (set(it.applies_to) & names):
                continue
            items.append(it)
    items.sort(key=lambda i: RANK[i.layer])
    return items


# ---------------------------------------------------------------------------
# authority: CODEOWNERS + git identity (no roles anywhere)
# ---------------------------------------------------------------------------

def _git_root(path: Path) -> Path | None:
    for p in [path, *path.parents]:
        if (p / ".git").exists():
            return p
    return None


def owners_of(doc: Path) -> list[str]:
    """CODEOWNERS entries covering ``doc`` ([] = nobody owns it: edit freely)."""
    doc = doc.absolute()
    top = _git_root(doc.parent)
    if top is None:
        return []
    for cand in (top / ".github" / "CODEOWNERS", top / "CODEOWNERS", top / "docs" / "CODEOWNERS"):
        text = _read(cand)
        if text:
            break
    else:
        return []
    try:
        rel = doc.relative_to(top).as_posix()
    except ValueError:
        return []
    owners: list[str] = []
    for ln in text.splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        parts = ln.split()
        if len(parts) >= 2 and glob_match(parts[0], rel):
            owners = [o.lower() for o in parts[1:]]       # last matching line wins
    return owners


def identity(folder: Path) -> set[str]:
    """Who the git config says is writing: email, name, handle (lower case)."""
    cwd = folder if folder.exists() else (_git_root(folder) or Path("."))
    out = set()
    for key in ("user.email", "user.name", "github.user", "reverie.handle"):
        v = spool._git(["config", key], cwd).strip().lower()
        if v:
            out.add(v)
            if key in ("github.user", "reverie.handle"):
                out.add("@" + v.lstrip("@"))
    return out


def is_owner(doc: Path, who: set[str] | None = None) -> bool:
    owners = owners_of(doc)
    return not owners or bool(set(owners) & (identity(doc.parent) if who is None else who))


def labels(folder: Path) -> dict[str, str]:
    """Display labels from an optional ``authority`` file (``legal = @a, @b``).
    Only read for display: nothing branches on a label."""
    out: dict[str, str] = {}
    top = _git_root(folder)
    for cand in (folder / "authority", top / ".reverie" / "authority" if top else folder / "authority"):
        for ln in _read(cand).splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                label, names = ln.split("=", 1)
                for n in _split_csv(names):
                    out.setdefault(n.lower(), label.strip())
    return out


def owner_text(it: CtxItem) -> str:
    """Whom to ask about this item, for conflict lines."""
    if it.owner:
        return it.owner
    path = Path(it.file)
    own = owners_of(path)
    lab = labels(path.parent)
    return ", ".join(f"{lab[o]} ({o})" if o in lab else o for o in own)


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
# containment, conflicts
# ---------------------------------------------------------------------------

def _origin_relation(a: CtxItem, b: CtxItem) -> str:
    """same | a_narrower | b_narrower | sibling, by where the documents come from."""
    if a.layer == b.layer:
        if a.layer in ("org", "team") and a.scope != b.scope:
            return "sibling"
        return "same"
    wide_first, narrow = (a, b) if RANK[a.layer] < RANK[b.layer] else (b, a)
    # a team that declares `inside: X` is not inside any other org
    team = next((x for x in (a, b) if x.layer == "team" and x.inside), None)
    org = next((x for x in (a, b) if x.layer == "org"), None)
    if team and org and team.inside not in (org.scope.lower(), org.scope.lower().split("/")[0],
                                            org.scope.lower().split("/")[-1]):
        return "sibling"
    return "a_narrower" if narrow is a else "b_narrower"


def relation(a: CtxItem, b: CtxItem) -> str:
    """same | disjoint | sibling | a_narrower | b_narrower."""
    if a.paths and b.paths and not _globs_overlap(a.paths, b.paths):
        return "disjoint"
    o = _origin_relation(a, b)
    if o == "sibling":
        return "sibling"
    if bool(a.paths) != bool(b.paths):                   # a path scope is narrower
        return "a_narrower" if a.paths else "b_narrower"
    return o


def pair_key(a: CtxItem, b: CtxItem) -> str:
    return "c-" + hashlib.sha1("|".join(sorted((a.id, b.id))).encode()).hexdigest()[:8]


def _decide(key: str, a: CtxItem, b: CtxItem, rel: str, reason: str,
            effective: list[CtxItem]) -> Conflict | None:
    if rel in ("same", "disjoint"):
        return None
    for r in effective:                                   # a recorded resolution settles it
        if r.resolves == key and r.id not in (a.id, b.id) \
                and all(relation(r, x) in ("a_narrower", "same") for x in (a, b)):
            return Conflict(key, "overridden", r, [a, b],
                            (reason + "; " if reason else "") + "resolved by " +
                            (owner_text(r) or r.label))
    if rel == "sibling":
        why = "two locks contradict" if a.locked and b.locked else \
            "sibling scopes have no order"
        return Conflict(key, "unresolved", None, [a, b], f"{why}; {reason}".strip("; "))
    narrow, wide = (a, b) if rel == "a_narrower" else (b, a)
    if wide.locked and narrow.locked:
        return Conflict(key, "unresolved", None, [a, b],
                        ("two locks contradict; " + reason).strip("; "))
    if wide.locked:
        return Conflict(key, "locked", wide, [narrow], reason)
    return Conflict(key, "overridden", narrow, [wide], reason)


def resolve(root: str, items: list[CtxItem] | None = None, state: dict | None = None,
            path: str | None = None) -> Resolved:
    """Apply containment. ``path`` (relative to the repo) filters path-scoped items
    to the ones that apply to it; None keeps them all (session start: no file yet).
    Overridden items are kept in the conflict list, never dropped."""
    items = load_items(root) if items is None else items
    state = load_state() if state is None else state
    if path is not None:
        items = [i for i in items if not i.paths or any(glob_match(g, path) for g in i.paths)]
    by_id = {i.id: i for i in items}
    rules = [i for i in items if i.kind == "rule"]
    pairs: dict[tuple[str, str], tuple[str, str]] = {}    # ids -> (key, reason)
    groups: dict[str, list[CtxItem]] = {}
    for it in rules:
        if it.tag:
            groups.setdefault(it.tag, []).append(it)
    for tag, grp in groups.items():
        for i, a in enumerate(grp):
            for b in grp[i + 1:]:
                pairs[(a.id, b.id)] = (tag, "")
    for c in state.get("conflicts") or []:
        ca, cb = by_id.get(c.get("a", "")), by_id.get(c.get("b", ""))
        if ca and cb and (ca.id, cb.id) not in pairs and (cb.id, ca.id) not in pairs:
            pairs[(ca.id, cb.id)] = (pair_key(ca, cb), str(c.get("reason", "")))

    conflicts: list[Conflict] = []
    out_ids: set[str] = set()
    for (ia, ib), (key, reason) in pairs.items():
        a, b = by_id[ia], by_id[ib]
        # `effective` for resolution lookup = resolution rules in scope
        c = _decide(key, a, b, relation(a, b), reason, [r for r in rules if r.resolves])
        if c is None:
            continue
        conflicts.append(c)
        out_ids.update(o.id for o in c.overridden)
    stale = {k: v for k, v in (state.get("stale") or {}).items() if k in by_id}
    effective = [i for i in items if i.id not in out_ids]
    # A personal rule that repeats a team rule is hidden at DELIVERY time only (never
    # deleted). It comes back as soon as the shared rule is removed, overridden or stale.
    shared = {norm(i.text) for i in effective
              if i.kind == "rule" and i.layer != "personal" and i.id not in stale}
    hidden = {i.id for i in effective
              if i.kind == "rule" and i.layer == "personal" and norm(i.text) in shared}
    return Resolved(items, [i for i in effective if i.id not in hidden], conflicts, stale,
                    hidden)


def shared_rule_norms(root: str) -> set[str]:
    """Normalised text of the team/org/repo rules in force here (what a personal copy
    of the same rule would repeat). Empty on any error."""
    try:
        res = resolve(root)
    except Exception:
        return set()
    return {norm(i.text) for i in res.effective
            if i.kind == "rule" and i.layer != "personal" and i.id not in res.stale}


# ---------------------------------------------------------------------------
# delivery text
# ---------------------------------------------------------------------------

def _one(text: str, limit: int) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _tag(it: CtxItem) -> str:
    where = f" · {','.join(it.paths)}" if it.paths else ""
    return f"[{it.label}{where} {it.id}]"


def _conflict_lines(res: Resolved) -> list[str]:
    lines: list[str] = []
    for c in res.conflicts:
        if c.status == "unresolved":
            a, b = c.overridden[0], c.overridden[1]
            who = "; ".join(f"{x.label}: {owner_text(x)}" for x in (a, b) if owner_text(x))
            lines.append(f"- UNRESOLVED {c.key}: {_tag(a)} \"{_one(a.text, 110)}\" vs "
                         f"{_tag(b)} \"{_one(b.text, 110)}\" ({c.reason}). Neither applies yet: "
                         f"ask the user which one to follow before acting on either"
                         + (f" (owners: {who})" if who else "")
                         + f"; it is settled with `reverie context resolve {c.key} \"<decision>\"`.")
        else:
            assert c.winner is not None
            for lo in c.overridden:
                tail = ("; a locked rule, do not follow the lower one" if c.status == "locked"
                        else "")
                why = f" ({c.reason})" if c.reason else ""
                lines.append(f"- {c.status} {_tag(c.winner)} \"{_one(c.winner.text, 110)}\" over "
                             f"{_tag(lo)} \"{_one(lo.text, 110)}\"{why}{tail}")
    return lines


def render(root: str, *, budget: int = 3600, rule_chars: int = 300) -> str:
    """The team-context block for a session start: rules always (bounded),
    conflicts shown, guidance as pointers. '' when nothing applies."""
    try:
        res = resolve(root)
    except Exception:
        return ""
    if not res.items:
        return ""
    lines: list[str] = []
    rules = [i for i in res.effective if i.kind == "rule" and i.id not in res.stale]
    wide = [i for i in rules if not i.paths]
    scoped = [i for i in rules if i.paths]
    if wide:
        lines.append("Team context rules (the narrower scope answers the wider unless locked; "
                     "always apply):")
        for it in wide:
            lock = " (locked: cannot be overridden inside its scope)" if it.locked else ""
            lines.append(f"- {_tag(it)} {_one(it.text, rule_chars)}{lock}")
    if scoped:
        lines.append("Rules for specific paths (apply ONLY to files under the path):")
        for it in scoped:
            lock = " (locked)" if it.locked else ""
            lines.append(f"- {_tag(it)} {_one(it.text, rule_chars)}{lock}")
    cl = _conflict_lines(res)
    if cl:
        lines.append("Conflicts between scopes (never silently dropped):")
        lines += cl
    hint = contradiction_hint(res)
    if hint:
        lines.append(hint)
    outdated = [i for i in res.items if i.id in res.stale]
    if outdated:
        lines.append("Possibly outdated team context (a later commit may contradict it; verify):")
        for it in outdated:
            st = res.stale[it.id]
            lines.append(f"- {_tag(it)} {_one(it.text, 160)}  [commit "
                         f"{str(st.get('commit', ''))[:8]}: {_one(str(st.get('reason', '')), 140)}]")
    guide = [i for i in res.effective if i.kind == "guidance" and i.id not in res.stale]
    if guide:
        lines.append("Team guidance (open one with `reverie show <id>`):")
        for it in guide:
            lines.append(f"- {_tag(it)} {_one(it.section + ': ' if it.section else '', 40)}"
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


def point_text(root: str, rel: str, seen: set[str] | None = None) -> tuple[str, set[str]]:
    """Path-scoped rules that apply to ``rel`` (for the edit hook), minus ``seen``."""
    try:
        res = resolve(root, path=rel)
    except Exception:
        return "", set()
    seen = seen or set()
    hits = [i for i in res.effective if i.paths and i.kind == "rule" and i.id not in seen
            and i.id not in res.stale]
    unresolved = [c for c in res.conflicts if c.status == "unresolved"
                  and any(i.paths for i in c.overridden) and c.key not in seen]
    if not hits and not unresolved:
        return "", set()
    lines = [f"Team rules for {rel} (path-scoped; they apply to this file):"]
    lines += [f"- {_tag(i)} {_one(i.text, 300)}" + (" (locked)" if i.locked else "") for i in hits]
    if unresolved:
        tmp = Resolved(res.items, res.effective, unresolved, res.stale)
        lines += _conflict_lines(tmp)
    return "\n".join(lines), {i.id for i in hits} | {c.key for c in unresolved}


def find(root: str, id_prefix: str) -> CtxItem | None:
    hits = [i for i in load_items(root) if i.id.startswith(id_prefix)]
    return hits[0] if len(hits) == 1 else None


def show(root: str, id_prefix: str) -> str:
    it = find(root, id_prefix)
    if it is None:
        return ""
    res = resolve(root)
    lines = [f"[{it.id}] {it.label} {it.kind}" + (" (locked)" if it.locked else ""),
             it.text, "", f"document: {it.file}"]
    if it.section:
        lines.append(f"section: {it.section}")
    if it.applies_to:
        lines.append("applies to: " + ", ".join(it.applies_to))
    if it.paths:
        lines.append("only under: " + ", ".join(it.paths))
    own = owner_text(it)
    if own:
        lines.append("owner: " + own)
    for c in res.conflicts:
        if it.id in {x.id for x in c.items}:
            lines.append(f"conflict {c.key} ({c.status}): "
                         + " vs ".join(f"{_tag(x)}" for x in c.items))
    if it.id in res.stale:
        st = res.stale[it.id]
        lines.append(f"possibly outdated: commit {st.get('commit', '')[:8]}: {st.get('reason', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# contributing: anyone may write; owners' folders turn it into a proposal
# ---------------------------------------------------------------------------

@dataclass
class AddResult:
    path: Path
    proposal: bool
    owners: list[str]


def target_folder(root: str, layer: str, scope: str = "") -> Path:
    if layer == "repo":
        return Path(root) / ".reverie" / "context"
    if layer == "personal":
        return spool.reverie_home() / "context"
    folders = sources(root).get(layer) or []
    if not folders:
        raise ValueError(f"no {layer} source configured: add [sources] {layer} = [\"<folder>\"] "
                         f"to .reverie/context.toml")
    if scope:
        match = [f for f in folders if scope in (scope_name(f), f.name)]
        if len(match) != 1:
            raise ValueError(f"no single {layer} folder called {scope!r}; have: "
                             + ", ".join(scope_name(f) for f in folders))
        return match[0]
    if len(folders) > 1:
        raise ValueError(f"several {layer} folders ({', '.join(scope_name(f) for f in folders)}): "
                         f"say which with --scope")
    return folders[0]


def _bullet(text: str, locked: bool, tag: str, resolves: str) -> str:
    return ("- " + text + (f" {{id: {tag}}}" if tag else "")
            + (f" {{resolves: {resolves}}}" if resolves else "")
            + (" (locked)" if locked else ""))


def _append(path: Path, bullet: str, rule: bool, paths: tuple[str, ...] = (),
            owner: str = "") -> None:
    name = path.stem
    heading = "## Rules" if rule else "## Guidance"
    body = _read(path)
    if bullet in body.splitlines():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    heads = re.findall(r"(?m)^#{1,6}\s+(.*)$", body)
    last_is_rules = bool(heads) and bool(_RULE_HEADING.search(heads[-1]))
    if not body.strip():
        head = (f"applies-to-paths: {', '.join(paths)}\n" if paths else "") + \
               (f"owner: {owner}\n" if owner else "")
        title = name.replace('-', ' ').title()
        body = f"{head}# {'Team context' if title in ('Rules', 'Guidance') else title}\n\n{heading}\n\n"
    elif not heads or last_is_rules != rule:
        body = body.rstrip("\n") + f"\n\n{heading}\n\n"
    elif not body.endswith("\n"):
        body += "\n"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(body + bullet + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _slug(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "proposal"
    return f"{base}-{hashlib.sha1(text.encode()).hexdigest()[:6]}"


def add(root: str, text: str, *, layer: str = "repo", rule: bool = True,
        locked: bool = False, tag: str = "", doc: str = "", scope: str = "",
        paths: tuple[str, ...] = (), resolves: str = "", why: str = "",
        provenance: dict[str, str] | None = None) -> AddResult:
    """Write a bullet. A direct edit when nobody owns the target document or the
    writer is an owner (CODEOWNERS + git identity); otherwise a PROPOSAL file the
    owners can review like any pull request. Reverie never asks for a job title."""
    if layer not in LAYERS:
        raise ValueError(f"layer must be one of {', '.join(LAYERS)}")
    text = " ".join(text.split())
    if not text:
        raise ValueError("text is empty")
    folder = target_folder(root, layer, scope)
    stem = (doc or ("rules" if rule else "guidance")).removesuffix(".md")
    if paths and not doc:
        stem = "paths-" + re.sub(r"[^a-z0-9]+", "-", paths[0].lower()).strip("-")
    if not re.fullmatch(r"[\w.-]+", stem):
        raise ValueError("document name must be a plain file name")
    path = folder / f"{stem}.md"
    bullet = _bullet(text, locked, tag, resolves)
    owners = owners_of(path)
    if owners and not is_owner(path):
        pdir = folder / "proposals"
        pdir.mkdir(parents=True, exist_ok=True)
        who = spool._git(["config", "user.name"], folder if folder.exists() else Path(root)) or "unknown"
        mail = spool._git(["config", "user.email"], folder if folder.exists() else Path(root))
        prop = pdir / f"{_slug(bullet)}.md"
        prop.write_text(
            "# Proposal\n\n" f"document: {stem}.md\n" f"kind: {'rule' if rule else 'guidance'}\n"
            f"paths: {', '.join(paths)}\n" f"proposed-by: {who} <{mail}>\n"
            f"date: {time.strftime('%Y-%m-%d')}\n" f"why: {why}\n"
            + "".join(f"{k}: {' '.join(str(v).split())}\n" for k, v in (provenance or {}).items())
            + f"\n{bullet}\n", encoding="utf-8")
        return AddResult(prop, True, owners)
    _append(path, bullet, rule, paths)
    return AddResult(path, False, owners)


def _proposal_dirs(root: str) -> list[Path]:
    dirs = [Path(root) / ".reverie" / "context" / "proposals"]
    for folders in sources(root).values():
        dirs += [f / "proposals" for f in folders]
    return [d for d in dirs if d.is_dir()]


def proposals(root: str) -> list[dict]:
    out = []
    for d in _proposal_dirs(root):
        for p in sorted(d.glob("*.md")):
            fields: dict[str, str] = {}
            bullet = ""
            for ln in _read(p).splitlines():
                if ln.startswith("- "):
                    bullet = ln
                elif ":" in ln and not ln.startswith("#"):
                    k, v = ln.split(":", 1)
                    fields.setdefault(k.strip().lower(), v.strip())
            out.append({"slug": p.stem, "path": p, "bullet": bullet, **fields})
    return out


def accept(root: str, slug: str) -> Path:
    """An owner moves a proposal into its document and deletes the proposal."""
    hits = [p for p in proposals(root) if p["slug"].startswith(slug)]
    if len(hits) != 1:
        raise ValueError(f"no single proposal matches {slug!r}")
    prop = hits[0]
    folder = prop["path"].parent.parent
    doc = folder / (prop.get("document") or "rules.md")
    if not is_owner(doc):
        raise ValueError(f"only the owners of {doc.name} can accept: " + ", ".join(owners_of(doc)))
    paths = tuple(x.strip() for x in (prop.get("paths") or "").split(",") if x.strip())
    _append(doc, prop["bullet"], prop.get("kind", "rule") == "rule", paths)
    prop["path"].unlink()
    return doc


def rejected(root: str) -> list[dict]:
    """Proposals an owner declined, with the reason. They live in ``proposals/rejected/``
    next to the document (so they are in git history and the proposer can read them) and
    are never delivered to an agent."""
    out = []
    for d in _proposal_dirs(root):
        for p in sorted((d / "rejected").glob("*.md")):
            fields: dict[str, str] = {}
            bullet = ""
            for ln in _read(p).splitlines():
                if ln.startswith("- "):
                    bullet = ln
                elif ":" in ln and not ln.startswith("#"):
                    k, v = ln.split(":", 1)
                    fields.setdefault(k.strip().lower(), v.strip())
            out.append({"slug": p.stem, "path": p, "bullet": bullet, **fields})
    return out


def bullet_text(bullet: str) -> str:
    """The rule text of a bullet line, without ``{id}``/``{resolves}``/``(locked)`` marks."""
    t = re.sub(r"\{(?:id|resolves):[^}]*\}", "", bullet.lstrip("- ").strip())
    return re.sub(r"\s*\(locked\)\s*$", "", t).strip()


def reject(root: str, slug: str, why: str) -> Path:
    """An owner declines a proposal. The reason is kept in ``proposals/rejected/``."""
    if not " ".join(why.split()):
        raise ValueError("say why (--why): the proposer will read it")
    hits = [p for p in proposals(root) if p["slug"].startswith(slug)]
    if len(hits) != 1:
        raise ValueError(f"no single proposal matches {slug!r}")
    prop = hits[0]
    doc = prop["path"].parent.parent / (prop.get("document") or "rules.md")
    if not is_owner(doc):
        raise ValueError(f"only the owners of {doc.name} can reject: " + ", ".join(owners_of(doc)))
    who = spool._git(["config", "user.name"], doc.parent) or "unknown"
    mail = spool._git(["config", "user.email"], doc.parent)
    dest = prop["path"].parent / "rejected" / prop["path"].name
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = _read(prop["path"]).rstrip("\n")
    dest.write_text(f"{body}\n\nrejected-by: {who} <{mail}>\nrejected-on: "
                    f"{time.strftime('%Y-%m-%d')}\nreason: {' '.join(why.split())}\n",
                    encoding="utf-8")
    prop["path"].unlink()
    return dest
