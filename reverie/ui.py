"""`reverie ui`: a local workspace for a team's projects. A command, not a daemon: it serves on
127.0.0.1 until Ctrl-C, from the same functions the CLI uses.

Reading is free; writing is narrow. Two writes exist, both calling the CLI's own functions (never a
write path of the UI's own), and both refusing exactly where the CLI refuses:
  POST /do/add      -> context.add     (a rule or guidance; a proposal if the writer is not an owner)
  POST /do/accept   -> context.accept  (owners only)
  POST /do/reject   -> context.reject  (owners only, a reason is required)

* GET and HEAD never change anything (the snapshot test crawls every page, write paths included);
* every POST needs: a bound Host, a same-origin Origin (or Sec-Fetch-Site: same-origin; neither
  header means refused), a form body of bounded size, and the per-process random token that the
  forms carry (no cookie); every write ends in a 303 redirect (post/redirect/get);
* only the repos Reverie already knows about (plus the one it was started in) can be opened, by an
  opaque id, never by a path from the URL or the form;
* every value is HTML-escaped (shared documents are written by other people) and passed through
  the existing redaction; a strict Content-Security-Policy (form-action 'self', no framing); no
  script, no external resource; a submitted secret is refused and never echoed back;
* the UI never runs the conflict judge; the detached session-start worker and the daily job do;
* a page or a write that fails is a plain 500 page ("Something went wrong") with no trace and no exception text;
* no telemetry of any kind: the server logs nothing and reports nothing.

Standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
import shlex
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import context, deliver, redact, rules, spool, usage
from .ui_style import CSS as DESIGN_CSS

CSP = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
MAX_BODY = 16 * 1024
WRITE_PATHS = ("/do/add", "/do/accept", "/do/reject")
LIMITS = {"text": 1000, "why": 300, "slug": 200, "token": 100, "p": 16, "kind": 16, "target": 120}

def esc(value: object, *, redacted: bool = True) -> str:
    text = redact.redact(str(value)) if redacted else str(value)
    return html.escape(text, quote=True)


# ---------------------------------------------------------------------------
# data: everything read-only, every call guarded (a broken file never breaks a page)
# ---------------------------------------------------------------------------

def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def is_disabled(root: str) -> bool:
    def go() -> bool:
        conn = deliver.open_db()
        if conn is None:
            return False
        try:
            return deliver.is_disabled(conn, deliver.repo_key(root))
        finally:
            conn.close()
    return bool(_safe(go, False))


def memory_items(root: str) -> list[dict]:
    def go() -> list[dict]:
        conn = deliver.open_db()
        if conn is None:
            return []
        try:
            return [m for m in deliver.memory(conn, deliver.repo_key(root), root)
                    if deliver.is_live(m, root)]
        finally:
            conn.close()
    return _safe(go, [])


def collect(root: str) -> dict:
    res = _safe(lambda: context.resolve(root), context.Resolved([], [], [], {}))
    mem = memory_items(root)
    stale_mem = [m for m in mem if deliver.is_stale_flagged(m)]
    team_rules = [i for i in res.effective if i.kind == "rule" and i.id not in res.stale]
    personal = rules.bullets(_safe(lambda: rules.load(root, hide_shared=True)["user"], ""))
    repo_rules = rules.bullets(_safe(lambda: rules.load(root)["repo"], ""))
    # Exactly the remembered rules deliver.startup_text hands over in full: not stale, not a repeat of a
    # team rule (the start block hides those), and either a rule or a user-wide preference.
    shared = {context.norm(i.text) for i in res.effective
              if i.kind == "rule" and i.layer != "personal" and i.id not in res.stale}
    mem_rules = [m for m in mem if not deliver.is_stale_flagged(m)
                 and context.norm(m.get("text") or "") not in shared
                 and (m["kind"] == "rule"
                      or (m["kind"] == "preference" and m.get("repo_key") is None
                          and m.get("origin") == "item"))]
    d = {
        "res": res, "mem": mem, "stale_mem": stale_mem, "team_rules": team_rules,
        "personal": personal, "repo_rules": repo_rules, "mem_rules": mem_rules,
        "unresolved": [c for c in res.conflicts if c.status == "unresolved"],
        "stale_team": [i for i in res.items if i.id in res.stale],
        "proposals": _safe(lambda: context.proposals(root), []),
        "rejected": _safe(lambda: context.rejected(root), []),
    }
    d["start"], d["trace"] = _safe(lambda: deliver.startup_report(root, log=False, mem=mem), ("", deliver.StartTrace()))
    d["records"] = _safe(lambda: rule_records(root, d), [])
    return d


def rules_in_force(d: dict) -> tuple[int, int]:
    """(told, cut): the rules agents are handed in the start text, and rules that apply but were cut by one of its
    limits. Counted from the start-text trace, the same numbers Rules and What agents are told show."""
    return told_counts(d)


# ---------------------------------------------------------------------------
# projects: only repos Reverie already knows (plus the one it was started in), by opaque id
# ---------------------------------------------------------------------------

def _pid(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:8]


def projects(root: str) -> list[dict]:
    def known() -> list[str]:
        conn = deliver.open_db()
        if conn is None:
            return []
        try:
            return [str(r["path"]) for r in conn.execute("SELECT path FROM repos").fetchall() if r["path"]]
        finally:
            conn.close()
    cur = _safe(lambda: spool.repo_root(root), root)
    out, seen = [], set()
    for path in [cur] + _safe(known, []):
        if path in seen:
            continue
        seen.add(path)
        out.append({"id": _pid(path), "path": path, "name": path.rstrip("/").rsplit("/", 1)[-1] or path,
                    "current": path == cur})
    return out


def _target(root: str, query: dict[str, list[str]] | None) -> tuple[str, dict, list[dict]]:
    plist = projects(root)
    want = ((query or {}).get("p") or [""])[0]
    proj = next((p for p in plist if p["id"] == want), plist[0])
    return proj["path"], proj, plist


def _u(path: str, proj: dict | None = None, **q: str) -> str:
    args = ([("p", proj["id"])] if proj else []) + list(q.items())
    return path + ("?" + "&".join(f"{k}={quote(str(v))}" for k, v in args) if args else "")


def _pslug(slug: str) -> str:
    """A safe fragment id for a proposal card, the same on Overview (link) and Proposals (target)."""
    return "p-" + hashlib.sha1(slug.encode()).hexdigest()[:10]


KIND_NAMES = {"rule": "Rules", "preference": "Preferences", "decision": "Decisions",
              "rejected_approach": "Rejected approaches", "environment": "Environment",
              "observation": "Observations", "open_question": "Open questions",
              "work_state": "Work in progress", "external_context": "External context"}


def _kind_name(kind: str) -> str:
    return KIND_NAMES.get(kind) or kind.replace("_", " ").capitalize()


# ---------------------------------------------------------------------------
# the Ink design (design.md): the shared shell, people, and the Overview and Proposals pages
# ---------------------------------------------------------------------------

_PERSON = re.compile(r"^\s*(.*?)\s*<([^<>\s]+@[^<>\s]+)>\s*$")


def _split_person(text: str) -> tuple[str, str]:
    """"Name <email>" -> (name, email); anything else -> ("", text)."""
    m = _PERSON.match(text or "")
    return (m.group(1), m.group(2)) if m else ("", (text or "").strip())


def known_people(root: str) -> dict[str, str]:
    """email (lower case) -> name, from what the repo already says: the git identity here, the people named
    on proposals and rejections, and the authors of recent commits. CODEOWNERS only has emails, so this is
    where names come from; an email with no name stays an email."""
    out: dict[str, str] = {}

    def add(name: str, email: str) -> None:
        name, email = (name or "").strip(), (email or "").strip().lower()
        if name and "@" in email:
            out.setdefault(email, name)
    add(spool._git(["config", "user.name"], Path(root)), spool._git(["config", "user.email"], Path(root)))
    for p in _safe(lambda: context.proposals(root) + context.rejected(root), []):
        for key in ("proposed-by", "rejected-by"):
            add(*_split_person(str(p.get(key, ""))))
    for line in _safe(lambda: spool._git(["log", "-n", "300", "--format=%aN\t%aE"], Path(root)), "").splitlines():
        name, _, email = line.partition("\t")
        add(name, email)
    return out


class Viewer:
    """Who the git config says is looking (the same identity the CLI would act as)."""

    def __init__(self, root: str) -> None:
        folder = Path(root) / ".reverie" / "context"
        self.ids = _safe(lambda: context.identity(folder), set())
        self.name = spool._git(["config", "user.name"], Path(root)).strip()
        self.email = spool._git(["config", "user.email"], Path(root)).strip()
        self.first = (self.name.split() or ["You"])[0]


def acting_html(me: "Viewer") -> str:
    """The viewer's own name (full), the email in the title: "Acting as Mudit Sarda, from your git identity."."""
    if not (me.name or me.email):
        return '<p class="who">No git identity is set here.</p>'
    who = f'<span title="{esc(me.email)}">{esc(me.name)}</span>' if me.name else esc(me.email)
    return f'<p class="who">Acting as {who}, from your git identity.</p>'


def person_html(who: str, names: dict[str, str], me: Viewer) -> str:
    """One person: the name with the email in the title attribute, "First (you)" for the viewer, the bare text
    when no name is known."""
    name, email = _split_person(who)
    key = (email or who).strip().lower()
    if key in me.ids or key == me.email.lower() or (name and name.lower() in me.ids):
        return f'<span title="{esc(email or me.email)}">{esc(me.first)} (you)</span>'
    name = name or names.get(key, "")
    if name:
        return f'<span title="{esc(email)}">{esc(name)}</span>'
    return esc(who)


def people_html(whos: list[str], names: dict[str, str], me: Viewer, sep: str = ", ") -> str:
    return sep.join(person_html(w, names, me) for w in whos)


def rule_html(text: str) -> str:
    """Rule text, escaped; `backticked` spans are code."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(text))


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _doc_path(path: Path, root: str) -> str:
    """The path shown for a document: relative to the repo, or folder/name for a shared folder."""
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        pass
    mine = _under_home(path)
    return mine if mine else f"{context.scope_name(path.parent)}/{path.name}"


def _under_home(path: Path) -> str:
    """A path inside the personal Reverie folder, shown as ~/.reverie/... whatever the folder is really called."""
    try:
        rel = path.resolve().relative_to(spool.reverie_home().resolve()).as_posix()
    except ValueError:
        return ""
    return f"~/.reverie/{rel}" if rel != "." else "~/.reverie"


def _doc_span(path: Path, root: str) -> str:
    """A document: its name in mono, its path as the title."""
    shown = _doc_path(path, root)
    return f'<span title="{esc(shown)}"><code>{esc(path.name)}</code></span>'


def _source_label(it) -> str:
    where = f" {it.scope}" if it.scope else ""
    return {"org": f"Organisation folder{where}", "team": f"Team folder{where}", "repo": "This repo",
            "personal": "Yours"}.get(it.layer, it.layer)


def _tabs_html(proj: dict, counts: dict, active: str) -> str:
    items = [("/project", "Overview"), ("/context", "Rules"), ("/memory", "Memory"),
             ("/start", "What agents are told"), ("/proposals", "Proposals"), ("/connect", "Connect")]
    out = []
    for path, label in items:
        cur = ' aria-current="page"' if path == active else ""
        n = counts.get(path)
        out.append(f'<a href="{_u(path, proj)}"{cur}>{label}' + (f'<span class="n">{n}</span>' if n else "") + "</a>")
    return '<nav class="tabs" aria-label="Project">' + "".join(out) + "</nav>"


def layout(*, title: str, proj: dict | None, plist: list[dict], body: str, tab: str = "", counts: dict | None = None,
         h1: str | None = None, desc: str = "", q: str = "") -> str:
    """The shared shell: top bar (wordmark, project switcher, search), the tab row, the page."""
    menu = "".join(
        f'<li><a href="{_u("/project", p)}"' + (' aria-current="page"' if proj and p["id"] == proj["id"] else "")
        + f">{esc(p['name'])}</a></li>" for p in plist[:40])
    pid = f'<input type="hidden" name="p" value="{esc(proj["id"])}">' if proj else ""
    value = f' value="{esc(q)}"' if q else ""
    tabrow = (f'<div class="tabrow">{_tabs_html(proj, counts or {}, tab)}<div class="acts">'
              f'<a href="{_u("/add", proj)}">Add a rule</a></div></div>') if proj else ""
    hidden = f'<h1 class="visually-hidden">{esc(h1)} for {esc(proj["name"])}</h1>' if (h1 and proj) else ""
    return (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>{esc(title)}</title>\n'
            + (f'<meta name="description" content="{esc(desc)}">\n' if desc else "")
            + f"<style>\n{DESIGN_CSS}</style>\n</head>\n<body>\n"
            f'<header class="bar"><div class="page">\n<a class="mark" href="/" aria-label="Reverie, all projects">Reverie</a>\n'
            f'<details class="switch"><summary>{esc(proj["name"] if proj else "All projects")}</summary>\n'
            f'<ul class="menu">{menu}<li><hr></li><li><a href="/">All projects</a></li><li><a href="/new">Add a project</a></li></ul></details>\n'
            f'<form class="find" action="/search" method="get" role="search">{pid}<input name="q" type="search" '
            f'placeholder="Search rules and memory" aria-label="Search rules and memory" maxlength="120"{value}></form>\n'
            f'</div></header>\n<div class="page">\n{tabrow}\n<main class="main">\n{hidden}\n{body}\n</main>\n</div>\n</body>\n</html>\n')


def _proposal_doc(p: dict) -> Path:
    return p["path"].parent.parent / (p.get("document") or "rules.md")


def waiting(root: str, d: dict) -> dict:
    """Everything that waits on someone, split by whether it waits on the viewer. A proposal waits on you when you
    are an owner of its document (or none is); a conflict or a possibly outdated rule when you own one of its
    documents or no CODEOWNERS entry covers it. One definition for the tab counts, Overview and Proposals."""
    res = d["res"]
    props = []
    for p in d["proposals"]:
        doc = _proposal_doc(p)
        props.append({"p": p, "doc": doc, "owners": _safe(lambda: context.owners_of(doc), []),
                      "mine": _safe(lambda: context.is_owner(doc), False)})
    conflicts = []
    for c in d["unresolved"]:
        files = [Path(i.file) for i in c.items]
        owners: list[str] = []
        for f in files:
            owners += [o for o in _safe(lambda: context.owners_of(f), []) if o not in owners]
        conflicts.append({"c": c, "owners": owners, "mine": any(_safe(lambda: context.is_owner(f), False) for f in files)})
    outdated = []
    for it in d["stale_team"]:
        f = Path(it.file)
        outdated.append({"kind": "team", "it": it, "st": res.stale.get(it.id, {}), "doc": f,
                         "owners": _safe(lambda: context.owners_of(f), []), "mine": _safe(lambda: context.is_owner(f), False)})
    for m in d["stale_mem"]:
        outdated.append({"kind": "memory", "m": m, "mine": True})
    you = (sum(1 for x in props if x["mine"]) + sum(1 for x in conflicts if x["mine"]) + sum(1 for x in outdated if x["mine"]))
    return {"props": props, "conflicts": conflicts, "outdated": outdated,
            "counts": {"/project": you, "/proposals": sum(1 for x in props if x["mine"])}}


_TITLE_WORD = {"Overview": "of", "Rules": "of", "Memory": "of", "What agents are told": "in", "Add a rule": "to"}


def _frame(root: str, proj: dict, plist: list[dict], d: dict, w: dict, tab: str, body: str, title: str, h1: str,
           full_title: str = "") -> str:
    return layout(title=full_title or f"{title} {_TITLE_WORD.get(title, 'for')} {proj['name']}, Reverie", proj=proj, plist=plist,
                  body=body, tab=tab, counts=w["counts"], h1=h1)


def _flash_html(flash: dict | None) -> str:
    if not flash:
        return ""
    if "html" in flash:                          # built from escaped parts by do_write (a rule quoted, names, a result link)
        return f'<p class="flash" role="status">{flash["html"]}</p>'
    msg = _backtick_cmds(esc(flash["msg"])).replace("in force", "<span class=\"in\">in force</span>")
    return f'<p class="flash" role="status">{msg}</p>'


def _backtick_cmds(text: str) -> str:
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def _prop_item(x: dict, ctx: dict, link: bool = True) -> str:
    p = x["p"]
    names, me, root, pid = ctx["names"], ctx["me"], ctx["root"], ctx["pid"]
    meta = [person_html(str(p.get("proposed-by", "")), names, me), f'<span>{esc(p.get("date", ""))}</span>',
            _doc_span(x["doc"], root)]
    if p.get("paths"):
        meta.append(f'<span>only under <code>{esc(p["paths"])}</code></span>')
    why = f'<p class="why">Why: {rule_html(p["why"])}</p>' if p.get("why") else ""
    href = f'/proposals?p={pid}#{_pslug(p["slug"])}'
    if x["mine"]:
        tail = f'<p class="go"><a href="{href}">Decide</a></p>'
    else:
        tail = (f'<p class="waiting-on">Waiting on an owner of <code>{esc(x["doc"].name)}</code>: '
                f'{people_html(x["owners"], names, me)}.</p><p class="go"><a href="{href}">View</a></p>')
    return (f'<li><article class="entry"><p class="label">Proposal</p><h3 class="rule">{rule_html(context.bullet_text(p["bullet"]))}</h3>'
            f'{why}<p class="meta">{"".join(meta)}</p>{tail}</article></li>')


def _conflict_item(x: dict, ctx: dict) -> str:
    c, names, me, root, pid = x["c"], ctx["names"], ctx["me"], ctx["root"], ctx["pid"]
    sides = "".join(
        f'<div class="side"><p class="rule">{rule_html(it.text)}</p><p class="meta"><span>{esc(_source_label(it))}</span>'
        f'{_doc_span(Path(it.file), root)}<a href="/rule?p={pid}&amp;id={esc(it.id)}">Open rule</a></p></div>' for it in c.items)
    if not x["owners"]:
        who = "No CODEOWNERS entry covers either document, so anyone can settle it."
    elif x["mine"]:
        who = "You own one of the documents, so you can settle it."
    else:
        who = f'Waiting on an owner: {people_html(x["owners"], names, me)}.'
    return (f'<li><article class="entry"><p class="label">Conflict</p>{sides}'
            f'<p class="why">Agents are told to ask before acting on either. {who}</p>'
            f'<p class="cmdline"><code>reverie context resolve {esc(c.key)} "your decision"</code></p></article></li>')


def _outdated_item(x: dict, ctx: dict) -> str:
    names, me, root, pid = ctx["names"], ctx["me"], ctx["root"], ctx["pid"]
    if x["kind"] == "memory":
        m = x["m"]
        st = (m.get("meta") or {}).get("stale") or {}
        text, ident, reason = str(m.get("text", "")), str(m.get("id", ""))[:8], str(st.get("reason", ""))
        meta = ['<span>Remembered</span>']
        commit, message = str(st.get("commit", "")), str(st.get("message", ""))
        tail = (f'<p class="todo">Check the code, then keep it with <code>reverie stale keep {esc(ident)}</code> or '
                f'forget it with <code>reverie undo {esc(ident)}</code>.</p>')
    else:
        it, st = x["it"], x["st"]
        text, ident, reason = it.text, it.id, str(st.get("reason", ""))
        commit, message = str(st.get("commit", "")), str(st.get("message", ""))
        meta = [_doc_span(x["doc"], root)]
        if x["mine"]:
            tail = (f'<p class="todo">Check the code, then edit or remove the rule in <code>{esc(_doc_path(x["doc"], root))}</code>.</p>')
        else:
            tail = (f'<p class="waiting-on">Waiting on an owner of <code>{esc(x["doc"].name)}</code>: '
                    f'{people_html(x["owners"], names, me)}.</p>')
    if commit:
        meta.append(f'<span>Commit <code>{esc(commit[:7])}</code>{" " + esc(message) if message else ""}</span>')
    return (f'<li><article class="entry"><p class="label">Possibly outdated</p><h3 class="rule">{rule_html(text)}</h3>'
            f'<p class="why">A later change to the code may contradict it: {esc(reason)}.</p>'
            f'<p class="meta">{"".join(meta)}</p>{tail}'
            f'<p class="go"><a href="/rule?p={pid}&amp;id={esc(ident)}">Open rule</a></p></article></li>')


def _ctx(root: str, proj: dict) -> dict:
    return {"root": root, "pid": proj["id"], "names": known_people(root), "me": Viewer(root)}


def overview(root: str, proj: dict, plist: list[dict]) -> str:
    d = collect(root)
    w = waiting(root, d)
    ctx = _ctx(root, proj)
    you = ([_prop_item(x, ctx) for x in w["props"] if x["mine"]] + [_conflict_item(x, ctx) for x in w["conflicts"] if x["mine"]]
           + [_outdated_item(x, ctx) for x in w["outdated"] if x["mine"]])
    others = ([_prop_item(x, ctx) for x in w["props"] if not x["mine"]]
              + [_conflict_item(x, ctx) for x in w["conflicts"] if not x["mine"]]
              + [_outdated_item(x, ctx) for x in w["outdated"] if not x["mine"]])
    if you and others:
        tally = f"{len(you)} waiting on you, {len(others)} waiting on others."
    elif you:
        tally = f"{len(you)} waiting on you."
    elif others:
        tally = f"Nothing waiting on you. {len(others)} waiting on others."
    else:
        tally = "Nothing needs you."
    who = acting_html(ctx["me"])
    parts = [who, f'<p class="tally">{tally}</p>']
    cut = rules_in_force(d)[1]
    if cut:
        parts.append(f'<p class="cut">{cut} more {"rule applies but was" if cut == 1 else "rules apply but were"} cut by the '
                     f'start limit. <a href="{_u("/start", proj)}">See what agents are told.</a></p>')
    for key, label, items in (("you", "Waiting on you", you), ("others", "Waiting on others", others)):
        if items:
            parts.append(f'<section class="group" aria-labelledby="h-{key}"><h2 id="h-{key}">{label}</h2>'
                         f'<ol class="items">{"".join(items)}</ol></section>')
    return _frame(root, proj, plist, d, w, "/project", "".join(parts), "Overview", "Overview")


def _prop_entry(x: dict, ctx: dict, st: State | None) -> str:
    p = x["p"]
    names, me, root, pid = ctx["names"], ctx["me"], ctx["root"], ctx["pid"]
    meta = [person_html(str(p.get("proposed-by", "")), names, me), f'<span>{esc(p.get("date", ""))}</span>', _doc_span(x["doc"], root)]
    if p.get("paths"):
        meta.append(f'<span>only under <code>{esc(p["paths"])}</code></span>')
    why = f'<p class="why">Why: {rule_html(p["why"])}</p>' if p.get("why") else ""
    slug = esc(p["slug"])
    if x["mine"]:
        hidden = (f'<input type="hidden" name="token" value="{html.escape(st.token if st else "")}">'
                  f'<input type="hidden" name="p" value="{esc(pid)}"><input type="hidden" name="slug" value="{slug}">')
        act = (f'<form method="post" action="/do/accept">{hidden}<button class="btn" type="submit">Accept</button></form>'
               f'<details class="reject"><summary>Reject</summary><form method="post" action="/do/reject">{hidden}'
               f'<label>Reason for turning it down (the proposer will read it)'
               f'<input type="text" name="why" maxlength="{LIMITS["why"]}" required></label>'
               f'<button class="text-btn" type="submit">Turn down with this reason</button></form></details>'
               f'<details class="cmd"><summary>As commands</summary>'
               f'<p class="why">Accept it.</p><pre>reverie context accept {slug}</pre>'
               f'<p class="why">Or turn it down, with the reason the proposer will read.</p>'
               f'<pre>reverie context reject {slug} --why "..."</pre></details>')
        act = f'<div class="act">{act}</div>'
    else:
        act = (f'<p class="waiting-on">Waiting on an owner of <code>{esc(x["doc"].name)}</code>: '
               f'{people_html(x["owners"], names, me)}.</p>')
    return (f'<article class="entry" id="{_pslug(p["slug"])}"><h3 class="rule">{rule_html(context.bullet_text(p["bullet"]))}</h3>'
            f'{why}<p class="meta">{"".join(meta)}</p>{act}</article>')


def proposals_page(root: str, proj: dict, plist: list[dict], st: State | None = None, flash: dict | None = None) -> str:
    d = collect(root)
    w = waiting(root, d)
    ctx = _ctx(root, proj)
    names, me = ctx["names"], ctx["me"]
    banner = _flash_html(flash)
    if not d["proposals"] and not d["rejected"]:
        body = (banner + '<div class="empty"><p class="lead">Nothing waiting. Suggest a rule with '
                f'<a href="{_u("/add", proj)}">Add a rule</a>.</p><div class="cli"><p>The same from the command line:</p>'
                '<pre>reverie context add "Money is integer cents" --why "reason"</pre>'
                '<p>To turn a rule you already use into a proposal:</p>'
                '<pre>reverie context propose "Money is integer cents"</pre></div></div>')
        return _frame(root, proj, plist, d, w, "/proposals", body, "Proposals", "Proposals")
    on_me = [x for x in w["props"] if x["mine"]]
    other = [x for x in w["props"] if not x["mine"]]
    if on_me and other:
        tally = f"{len(on_me)} waiting on you, {len(other)} waiting on others."
    elif on_me:
        tally = f"{len(on_me)} waiting on you."
    elif other:
        tally = f"Nothing waiting on you. {len(other)} waiting on others."
    else:
        tally = "Nothing waiting."
    who = acting_html(me)
    parts = [banner, who, f'<p class="tally">{tally}</p>']
    for key, label, xs in (("you", "Waiting on you", on_me), ("others", "Waiting on others", other)):
        if xs:
            parts.append(f'<section class="group" aria-labelledby="h-{key}"><h2 id="h-{key}">{label}</h2>'
                         + "".join(_prop_entry(x, ctx, st) for x in xs) + "</section>")
    if d["rejected"]:
        rows = ""
        for r in d["rejected"]:
            rows += (f'<article class="entry"><h3 class="rule">{rule_html(context.bullet_text(r["bullet"]))}</h3>'
                     f'<p class="why">Reason: {rule_html(str(r.get("reason", "")))}</p><p class="meta">'
                     + (f'<span>Proposed by {person_html(str(r["proposed-by"]), names, me)}</span>' if r.get("proposed-by") else "")
                     + f'<span>Turned down by {person_html(str(r.get("rejected-by", "?")), names, me)}</span>'
                     f'<span>{esc(r.get("rejected-on", "?"))}</span></p></article>')
        parts.append(f'<details class="history"><summary>Turned down, {len(d["rejected"])}</summary>{rows}</details>')
    return _frame(root, proj, plist, d, w, "/proposals", "".join(parts), "Proposals", "Proposals")


# ---------------------------------------------------------------------------
# stage 2: Rules, the Rule page, What agents are told, Memory (design.md; one record per rule feeds all of them)
# ---------------------------------------------------------------------------

CUT_FALLBACK = "it did not fit in what agents are told"
ID_OK = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
SAID = {"user_said": "You said it", "agent_inferred": "An agent inferred it"}


def bullet_id(text: str) -> str:
    """Bullets in a rules file have no id of their own: a stable one is the hash of their text."""
    return hashlib.sha1(" ".join(text.split()).lower().encode()).hexdigest()[:10]


def usage_words(u: dict) -> str:
    return usage.words(u)


def _clip1(text: str, n: int = 64) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def _quote(text: str) -> str:
    return f"“{rule_html(_clip1(text))}”"


def plain_conflict_reason(reason: str) -> str:
    """The judge's or the resolver's reason, in plain words."""
    low = (reason or "").lower()
    if "sibling scopes have no order" in low:
        return "both come from team folders at the same level, so neither one wins."
    if "two locks contradict" in low:
        return "both are locked, so neither one wins."
    return (reason or "the two rules say different things.").rstrip(".") + "."


def _overridden_why(c) -> str:
    if c.status == "locked":
        return "it is locked, so it wins everywhere"
    if "resolved by" in (c.reason or ""):
        return c.reason.split(";")[-1].strip()
    return "it is more specific, so it answers this one"


def _where_label(it) -> str:
    return it.scope or "this repo" if it.layer in ("org", "team") else "this repo"


def _conflict_who(owners: list[str], mine: bool, names: dict, me: "Viewer") -> str:
    if not owners:
        return "No CODEOWNERS entry covers either document, so anyone can settle it."
    if mine:
        return "You own one of the documents, so you can settle it."
    return f"Waiting on an owner: {people_html(owners, names, me)}."


def _memory_state(m: dict, shared: set[str], fate: dict) -> tuple[str, str]:
    """(status, reason) of a remembered item: outdated / hidden / cut / challenged / force."""
    meta = m.get("meta") or {}
    if deliver.is_stale_flagged(m):
        return "outdated", str((meta.get("stale") or {}).get("reason", ""))
    if m.get("kind") == "rule" and context.norm(m.get("text") or "") in shared:
        return "hidden", ""
    f = fate.get(("memory", m.get("id")))
    if f is not None and not f["told"]:
        return "cut", f["reason"] or CUT_FALLBACK
    return ("challenged" if meta.get("challenged") else "force"), ""


def rule_records(root: str, d: dict) -> list[dict]:
    """Every rule that applies here, one record each, with its state: in force (told to agents), possibly
    outdated, in an unresolved conflict, overridden, hidden (a copy of a team rule) or cut. Rules, the Rule page,
    What agents are told and Search all read these; the told/cut state comes from the start-text trace."""
    res, trace = d["res"], d["trace"]
    fate = {(r["source"], r["key"]): r for r in trace.rules}
    conf: dict[str, tuple] = {}
    for c in res.conflicts:
        if c.status == "unresolved":
            for it in c.overridden:
                conf[it.id] = ("conflict", c, next((x for x in c.overridden if x.id != it.id), None))
        else:
            for lo in c.overridden:
                conf[lo.id] = ("overridden", c, c.winner)
            if c.winner is not None and c.overridden:
                conf.setdefault(c.winner.id, ("beats", c, c.overridden[0]))
    recs: list[dict] = []
    for it in res.items:
        f = Path(it.file)
        rec = {"id": it.id, "text": it.text, "kind": "team", "item": it, "group": "your" if it.layer == "personal" else "team",
               "sub": _source_label(it) if it.layer != "personal" else "", "doc": f.name, "path": _doc_path(f, root),
               "paths": ", ".join(it.paths), "locked": it.locked, "guidance": it.kind != "rule", "status": "force", "reason": "",
               "other": None, "note": "", "layer": it.layer}
        if it.id in res.stale:
            rec["status"], rec["reason"] = "outdated", str(res.stale[it.id].get("reason", ""))
        elif it.id in conf:
            role, c, other = conf[it.id]
            rec["conflict"] = c
            rec["other"] = other
            if role == "conflict":
                rec["status"] = "conflict"
            elif role == "overridden":
                rec["status"], rec["reason"] = "overridden", _overridden_why(c)
            else:
                rec["beats"] = other
                rec["reason"] = _overridden_why(c)
        if rec["status"] == "force" and it.id in res.hidden:
            rec["status"] = "hidden"
        elif rec["status"] == "force" and not rec["guidance"]:
            fa = fate.get(("team", it.id))
            if fa is not None and not fa["told"]:
                rec["status"], rec["reason"] = "cut", fa["reason"] or CUT_FALLBACK
        recs.append(rec)
    for src, group, path, bullets in (("rules_file_user", "your", "~/.reverie/rules.md", d["personal"]),
                                      ("rules_file_repo", "shared", ".reverie/rules.md", d["repo_rules"])):
        for b in bullets:
            fa = fate.get((src, b))
            cut = fa is not None and not fa["told"]
            recs.append({"id": bullet_id(b), "text": b, "kind": "file", "item": None, "group": group, "sub": "",
                         "doc": "rules.md", "path": path, "paths": "", "locked": False, "guidance": False,
                         "status": "cut" if cut else "force", "reason": ((fa or {}).get("reason") or CUT_FALLBACK) if cut else "",
                         "other": None, "note": "", "layer": "personal" if group == "your" else "repo"})
    shared = {context.norm(i.text) for i in res.effective if i.kind == "rule" and i.layer != "personal" and i.id not in res.stale}
    for m in d["mem"]:
        if m.get("kind") != "rule":
            continue
        status, reason = _memory_state(m, shared, fate)
        recs.append({"id": str(m.get("id", ""))[:8], "text": str(m.get("text", "")), "kind": "memory", "item": m,
                     "group": "remembered", "sub": "", "doc": "", "path": "", "paths": "", "locked": False, "guidance": False,
                     "status": status, "reason": reason, "other": None, "note": "", "layer": "memory"})
    return recs


def _split_ids(recs: list[dict]) -> dict[str, dict]:
    return {r["id"]: r for r in recs}


def told_counts(d: dict) -> tuple[int, int]:
    """(told, cut): the rules agents are handed, and the rules that apply but were cut. One count, from the start-text trace."""
    rs = d["trace"].rules
    told = sum(1 for r in rs if r["told"])
    return told, len(rs) - told


def _st_label(rec: dict) -> str:
    return {"outdated": "Possibly outdated", "conflict": "Conflict", "overridden": "Overridden",
            "hidden": "Hidden: same as a team rule", "cut": "Cut by the start limit", "challenged": "Challenged"}.get(rec["status"], "")


def rule_li(r: dict, ctx: dict) -> str:
    names, me = ctx["names"], ctx["me"]
    pid = ctx["pid"]
    st, extra = r["status"], ""
    label = _st_label(r)
    if st == "force" and r.get("beats") is not None:
        b = r["beats"]
        extra = f'<p class="note">Overrides {_quote(b.text)} in {esc(_where_label(b))}: {esc(r["reason"])}.</p>'
    elif st == "outdated":
        extra = f'<p class="note">A later change to the code may contradict it: {esc(r["reason"])}.</p>'
    elif st == "conflict":
        o, c = r["other"], r["conflict"]
        owners = ctx["owners_of_conflict"](c)
        extra = (f'<p class="note">Disagrees with {_quote(o.text) if o else "another rule"} in {esc(_where_label(o) if o else "")}. '
                 f'Agents are told to ask before acting on either. {_conflict_who(owners[0], owners[1], names, me)}</p>')
    elif st == "overridden":
        o = r["other"]
        extra = f'<p class="note">Overridden by {_quote(o.text)} in {esc(_where_label(o))}: {esc(r["reason"])}.</p>'
    elif st == "hidden":
        extra = ('<p class="note">Agents are told the team rule, not this copy. The copy comes back if the team rule '
                 'is removed.</p>')
    elif st == "cut":
        extra = f'<p class="note">It applies here but was cut, because {esc(r["reason"])}.</p>'
    elif st == "challenged":
        extra = ('<p class="note">Agents contradicted it repeatedly, so agents are now told to confirm or fix it '
                 'before relying on it.</p>')
    meta = [f'<span class="st">{label}</span>'] if label else []
    if r["kind"] == "memory":
        meta.append(f'<span>{esc(SAID.get(str((r["item"] or {}).get("source", "")), "Remembered"))}</span>')
    elif r["doc"]:
        meta.append(f'<span title="{esc(r["path"])}"><code>{esc(r["doc"])}</code></span>')
    if r["guidance"]:
        meta.append("<span>Guidance</span>")
    if r["paths"]:
        meta.append(f'<span>only under <code>{esc(r["paths"])}</code></span>')
    if r["locked"]:
        meta.append("<span>locked</span>")
    meta.append(f'<a class="open" href="/rule?p={esc(pid)}&amp;id={quote(r["id"])}">Open rule</a>')
    return (f'<li class="entry"><p class="rule">{rule_html(r["text"])}</p><p class="meta">{"".join(meta)}</p>{extra}</li>')


def _rule_list(rs: list[dict], ctx: dict) -> str:
    if len(rs) <= 10:
        return f'<ol class="items">{"".join(rule_li(r, ctx) for r in rs)}</ol>'
    top = [r for r in rs if r["status"] != "force"]          # exceptions stay visible above the fold
    rest = [r for r in rs if r["status"] == "force"]
    out = f'<ol class="items">{"".join(rule_li(r, ctx) for r in top)}</ol>' if top else ""
    if rest:
        out += (f'<details class="fold"><summary>{len(rest)} {"more " if top else "rules "}in force</summary>'
                f'<ol class="items">{"".join(rule_li(r, ctx) for r in rest)}</ol></details>')
    return out


def _team_order(r: dict) -> int:
    it = r["item"]
    return {"org": 0, "team": 1}.get(it.layer, 2)          # org, team, then this repo; the order inside is the loader's


def rules_page(root: str, proj: dict, plist: list[dict]) -> str:
    d = collect(root)
    w = waiting(root, d)
    ctx = _ctx(root, proj)
    ctx["owners_of_conflict"] = _owner_fn(w, ctx)
    recs = d["records"]
    told, cut = told_counts(d)
    tally = (f'{told} {"rule" if told == 1 else "rules"} <span class="in">in force</span>' if told
             else 'No rules <span class="in">in force</span>')
    tally += (f', {cut} more cut by the start limit. <a href="{_u("/start", proj)}">See what agents are told.</a>' if cut else ".")
    out = [f'<p class="tally">{tally}</p>']

    def section(key: str, title: str, gnote: str, inner: str) -> None:
        out.append(f'<section class="group" aria-labelledby="g-{key}"><h2 id="g-{key}">{title}</h2>'
                   + (f'<p class="gnote">{gnote}</p>' if gnote else "") + inner + "</section>")
    team = sorted((r for r in recs if r["group"] == "team"), key=_team_order)
    if team:
        subs: dict[str, list[dict]] = {}
        for r in team:
            subs.setdefault(r["sub"], []).append(r)
        inner = ""
        for k, rs in subs.items():
            if k == "This repo":
                label = "This repo, <code>.reverie/context</code>"
            else:
                head, _, folder = k.partition(" folder ")
                label = f'{esc(head)} folder <code>{esc(folder)}</code>'
            inner += f'<h3 class="sub">{label}</h3>{_rule_list(rs, ctx)}'
        section("team", "Team rules", "", inner)
    for key, title, gnote in (("shared", "Shared rules file", "Through git, with no owners and no review."),
                              ("your", "Your rules", "Only your agents are told these, in every repo."),
                              ("remembered", "Remembered", "Rules remembered on this machine for this repo.")):
        g = [r for r in recs if r["group"] == key]
        if key == "your":
            g.sort(key=lambda r: r["kind"] != "file")                  # the rules file's bullets first, then personal documents
        if g:
            section(key, title, gnote, _rule_list(g, ctx))
    if not recs:
        out.append('<p class="who">No rules apply here yet. Put markdown in <code>.reverie/context/</code> to share rules '
                   'with a team, or use Add a rule.</p>')
    return _frame(root, proj, plist, d, w, "/context", "".join(out), "Rules", "Rules")


def _owner_fn(w: dict, ctx: dict):
    """A function from a Conflict to (owners of its documents, whether the viewer may settle it), from waiting()."""
    by_key = {x["c"].key: x for x in w["conflicts"]}
    return lambda c: ((by_key[c.key]["owners"], by_key[c.key]["mine"]) if c.key in by_key else ([], True))


# ---- the Rule page ------------------------------------------------------------------------------------------

def _fact(dt: str, dd: str) -> str:
    return f"<dt>{dt}</dt><dd>{dd}</dd>"


LAYER_WORD = {"org": "Organisation", "team": "Team", "repo": "This repo", "personal": "Yours", "memory": "Remembered on this machine"}


def _other_block(it, pid: str, root: str, lead: str) -> str:
    where = (f'<span>{esc(_source_label(it))}</span>{_doc_span(Path(it.file), root)}'
             + (f'<span>only under <code>{esc(", ".join(it.paths))}</code></span>' if it.paths else ""))
    return (f'<div class="other"><p class="why">{lead}</p><p class="rule">{rule_html(it.text)}</p><p class="meta">{where}'
            f'<a class="open" href="/rule?p={esc(pid)}&amp;id={quote(it.id)}">Open rule</a></p></div>')


def _memory_facts(m: dict, ctx: dict) -> str:
    meta = m.get("meta") or {}
    anchor = m.get("anchor") or {}
    rows = [_fact("Said by", esc(SAID.get(str(m.get("source", "")), "Remembered")))]
    if m.get("evidence"):
        rows.append(_fact("Evidence", f'<span class="quote">“{rule_html(str(m["evidence"]))}”</span>'))
    if m.get("created_at"):
        rows.append(_fact("Remembered", esc(str(m["created_at"])[:10])))
    anchors = [*(anchor.get("files") or []), *(f"{s}" for s in (anchor.get("symbols") or []))]
    if anchors:
        rows.append(_fact("Anchored to", ", ".join(f'<code>{esc(a)}</code>' for a in anchors)))
    if m.get("grounding_commit"):
        rows.append(_fact("Grounded on commit", f'<code>{esc(str(m["grounding_commit"])[:7])}</code>'))
    if m.get("last_verified"):
        rows.append(_fact("Last verified", esc(str(m["last_verified"])[:10])))
    words = usage_words(meta.get("usage") or {})
    if words:
        rows.append(_fact("Used by agents", esc(words[0].upper() + words[1:] + ".")))
    st = meta.get("stale") or {}
    if st:
        rows.append(_fact("Possibly outdated", f'A later change to the code may contradict it: {esc(str(st.get("reason", "")))}.'))
        if st.get("commit"):
            rows.append(_fact("Commit", f'<code>{esc(str(st["commit"])[:7])}</code>'
                                        + (f' {esc(str(st["message"]))}' if st.get("message") else "")))
    return "".join(rows)


def rule_page(root: str, proj: dict, plist: list[dict], rid: str) -> tuple[int, str]:
    """One rule, or one remembered item, by id. Read-only; commands are shown, never run."""
    d = collect(root)
    w = waiting(root, d)
    ctx = _ctx(root, proj)
    pid = proj["id"]
    recs = d["records"]
    rec = _split_ids(recs).get(rid)
    mem = None
    if rec is None:                                             # a remembered item that is not a rule
        mem = next((m for m in d["mem"] if str(m.get("id", ""))[:8] == rid), None)
        if mem is None:
            body = (f'<p class="back"><a href="{_u("/context", proj)}">Back to Rules</a></p>'
                    f'<p class="tally">No rule with that id in {esc(proj["name"])}.</p>')
            return 404, _frame(root, proj, plist, d, w, "/context", body, "Rule not found", "", "Rule not found, Reverie")
    ctx["res"], ctx["proj"] = d["res"], proj
    if rec is not None and rec["kind"] == "memory":
        mem = rec["item"]
    if mem is not None:
        is_rule = mem.get("kind") == "rule"
        tab, home = ("/context", ("Rules", "/context")) if is_rule else ("/memory", ("Memory", "/memory"))
        status = ""
        if rec is not None:
            status = _st_label(rec)
        else:
            meta = mem.get("meta") or {}
            status = "Possibly outdated" if deliver.is_stale_flagged(mem) else ("Challenged" if meta.get("challenged") else "")
        heading = "Remembered rule" if is_rule else ("Remembered decision" if mem.get("kind") == "decision" else "Remembered item")
        sec = f'<section class="sec"><h2>{heading}</h2><dl class="facts">{_memory_facts(mem, ctx)}</dl>'
        if (mem.get("meta") or {}).get("challenged"):
            sec += ('<p class="why">Challenged: agents contradicted it repeatedly, so agents are now told to confirm or fix it '
                    'before relying on it. See the tally with <code>reverie usage</code>.</p>')
        sec += "</section>"
        i = str(mem.get("id", ""))[:8]
        cmds = [("Print this item in a terminal.", f"reverie show {i}"),
                (f"Forget it. Bring it back with <code>reverie undo {i} --restore</code>.", f"reverie undo {i}"),
                (f"Make it apply in every repo, not only this one. Take it back with <code>reverie promote {i} --demote</code>.",
                 f"reverie promote {i}")]
        if deliver.is_stale_flagged(mem):
            cmds.append(("Keep it as it is and clear the possibly-outdated flag.", f"reverie stale keep {i}"))
        sec += ('<section class="sec"><h2>Commands</h2>' + "".join(
            f'<p class="why">{t if "<code>" in t else esc(t)}</p><pre class="block-cmd">{esc(c)}</pre>' for t, c in cmds) + "</section>")
        text, ident = str(mem.get("text", "")), i
    else:
        assert rec is not None
        tab, home = "/context", ("Rules", "/context")
        text, ident = rec["text"], rec["id"]
        status = _st_label(rec)
        sec = _team_sections(rec, ctx, w, root)
    body = (f'<p class="back"><a href="{home[1]}?p={esc(pid)}">Back to {home[0]}</a></p>'
            f'<h1 class="bigrule">{rule_html(text)}</h1>' + (f'<p class="status">{esc(status)}</p>' if status else "") + sec
            + f'<p class="idline">Id <code>{esc(ident)}</code>. Open it from a terminal with <code>reverie show {esc(ident)}</code>.</p>')
    word = ("Remembered decision " if (mem is not None and mem.get("kind") == "decision") else "Rule ")
    return 200, _frame(root, proj, plist, d, w, tab, body, "Rule", "", f"{word}{ident}, Reverie")


def _team_sections(rec: dict, ctx: dict, w: dict, root: str) -> str:
    names, me, pid = ctx["names"], ctx["me"], ctx["pid"]
    it = rec["item"]
    rows = []
    if rec["kind"] == "team":
        owners = _safe(lambda: context.owners_of(Path(it.file)), [])
        folder = (".reverie/context" if it.layer == "repo" else
                  (_under_home(Path(it.file).parent) or it.scope or "") if it.layer == "personal" else (it.scope or ""))
        rows = [_fact("Source", esc(LAYER_WORD.get(it.layer, it.layer))), _fact("Folder", f'<code>{esc(folder)}</code>'),
                _fact("Document", f'<code>{esc(rec["path"])}</code>')]
        if it.section:
            rows.append(_fact("Section", esc(it.section)))
        rows.append(_fact("Owners", people_html(owners, names, me) if owners else "No CODEOWNERS entry covers it."))
    else:
        rows = [_fact("Source", "Yours" if rec["group"] == "your" else "This repo (the shared rules file)"),
                _fact("Document", f'<code>{esc(rec["path"])}</code>'),
                _fact("Owners", "None: a file shared through git has no owners and no review." if rec["group"] == "shared"
                      else "Only you: it applies to your agents in every repo.")]
    if rec["paths"]:
        rows.append(_fact("Applies to", f'only under <code>{esc(rec["paths"])}</code>'))
    if rec["locked"]:
        rows.append(_fact("Locked", "It cannot be overridden inside its scope."))
    out = f'<section class="sec"><h2>Where it comes from</h2><dl class="facts">{"".join(rows)}</dl></section>'
    st = rec["status"]
    if st == "conflict":
        c, o = rec["conflict"], rec["other"]
        owners, mine = _owner_fn(w, ctx)(c)
        out += ('<section class="sec"><h2>Conflict</h2>'
                + _other_block(o, pid, root, f'Disagrees with this rule. Conflict <code>{esc(c.key)}</code>: {esc(plain_conflict_reason(c.reason))}')
                + f'<p class="why">Agents are told to ask before acting on either. {_conflict_who(owners, mine, names, me)}</p>'
                f'<pre class="block-cmd">reverie context resolve {esc(c.key)} "your decision"</pre></section>')
    elif st == "overridden":
        c, o = rec["conflict"], rec["other"]
        out += ('<section class="sec"><h2>Overridden</h2>' + _other_block(o, pid, root, "Overridden by this rule.")
                + f'<p class="why">Why it wins: {esc(rec["reason"])}.</p>'
                f'<p class="absent">The conflict is <code>{esc(c.key)}</code>. An overridden rule is kept in its document and is not told to agents.</p></section>')
    elif rec.get("beats") is not None:
        c, o = rec["conflict"], rec["beats"]
        out += ('<section class="sec"><h2>Overrides</h2>' + _other_block(o, pid, root, "This rule overrides:")
                + f'<p class="why">Why it wins: {esc(rec["reason"])}.</p></section>')
    elif st == "outdated":
        stale = ctx["res"].stale.get(it.id, {}) if it is not None else {}
        rows2 = [_fact("Reason", f'A later change to the code may contradict it: {esc(rec["reason"])}.')]
        if stale.get("commit"):
            rows2.append(_fact("Commit", f'<code>{esc(str(stale["commit"])[:7])}</code>'
                                         + (f' {esc(str(stale["message"]))}' if stale.get("message") else "")))
        rows2.append(_fact("What to do", f'Check the code, then edit or remove the rule in <code>{esc(rec["path"])}</code>.'))
        out += f'<section class="sec"><h2>Possibly outdated</h2><dl class="facts">{"".join(rows2)}</dl></section>'
    elif st == "cut":
        out += (f'<section class="sec"><h2>Cut by the start limit</h2><p class="why">It applies here but was cut, because '
                f'{esc(rec["reason"])}. <a href="{_u("/start", ctx["proj"])}">See what agents are told.</a></p></section>')
    elif st == "hidden":
        out += ('<section class="sec"><h2>Hidden</h2><p class="why">Agents are told the team rule, not this copy. The copy '
                'comes back if the team rule is removed.</p></section>')
    return out


# ---- What agents are told -------------------------------------------------------------------------------------

def start(root: str, proj: dict, plist: list[dict]) -> str:
    d = collect(root)
    w = waiting(root, d)
    trace = d["trace"]
    text = d["start"]
    told, cut = told_counts(d)
    tally = f"{told} {'rule is' if told == 1 else 'rules are'} told to agents"
    if trace.index:
        tally += f", plus an index of {trace.index} remembered decisions and findings"
    tally += "."
    cut_rules = [r for r in trace.rules if not r["told"]]
    reasons = sorted({r["reason"] for r in cut_rules})
    if cut_rules:
        tally += f" {cut} more {'rule applies but was' if cut == 1 else 'rules apply but were'} cut"
        tally += f", because {reasons[0]}." if len(reasons) == 1 else "."
    out = [f'<p class="tally">{tally}</p>',
           '<p class="who">This is what your agents are told. Your rules are only told to your own agents.</p>']
    pid = proj["id"]
    if cut_rules:
        li = ""
        for r in cut_rules:
            rid = r["key"] if r["source"] == "team" else (str(r["key"])[:8] if r["source"] == "memory" else bullet_id(r["text"]))
            mem_item = next((m for m in d["mem"] if m.get("id") == r["key"]), None) if r["source"] == "memory" else None
            src = (SAID.get(str((mem_item or {}).get("source", "")), "Remembered") if r["source"] == "memory"
                   else ("Team rule" if r["source"] == "team" else "Rules file"))
            why = f'<span>Cut because {esc(r["reason"])}</span>' if len(reasons) > 1 else ""
            li += (f'<li class="entry"><p class="rule">{rule_html(r["text"])}</p><p class="meta"><span>{src}</span>{why}'
                   f'<a class="open" href="/rule?p={esc(pid)}&amp;id={quote(rid)}">Open rule</a></p></li>')
        out.append('<section class="group" aria-labelledby="g-cut"><h2 id="g-cut">Cut by the start limit</h2>'
                   f'<ol class="items">{li}</ol>'
                   f'<p class="gnote">The start text lists at most {deliver.MAX_RULE_LINES} remembered rules for this repo, '
                   f'{deliver.MAX_WIDE_LINES} of your rules that apply in every repo and {deliver.MAX_OUTDATED_LINES} possibly outdated '
                   f'ones, and stops at {trace.cap:,} characters. <code>reverie rules list</code> shows every rule that applies.</p></section>')
    if d["unresolved"]:
        li = "".join(
            f'<li class="entry"><p><a href="/rule?p={esc(pid)}&amp;id={quote(c.overridden[0].id)}"><code>{esc(c.key)}</code></a></p>'
            f'<p class="why">{esc(_where_label(c.overridden[0]))} and {esc(_where_label(c.overridden[1]))} disagree. '
            'Agents are told to ask before acting on either.</p></li>' for c in d["unresolved"] if len(c.overridden) > 1)
        if li:
            out.append(f'<section class="group" aria-labelledby="g-ask"><h2 id="g-ask">Agents are told to ask first</h2><ol class="items">{li}</ol></section>')
    if text:
        out.append('<section class="group" aria-labelledby="g-text"><h2 id="g-text">What agents read</h2>'
                   f'<pre class="doc" id="start">{esc(text, redacted=False)}</pre><p class="gnote">{len(text):,} of {trace.cap:,} characters.</p></section>')
    else:
        out.append('<p class="why">Nothing is told to agents here. They start with a clean slate.</p>')
    return _frame(root, proj, plist, d, w, "/start", "".join(out), "What agents are told", "What agents are told")


# ---- Memory ---------------------------------------------------------------------------------------------------

MEM_KINDS = [("decision", "Decisions", "Decision"), ("rejected_approach", "Rejected approaches", "Rejected approach"),
             ("preference", "Preferences", "Preference"), ("environment", "Environment", "Environment")]


def _mem_look(m: dict) -> tuple[str, str]:
    """(why it needs a look, the one command that fixes it), or ("", "")."""
    meta = m.get("meta") or {}
    i = esc(str(m.get("id", ""))[:8])
    if deliver.is_stale_flagged(m):
        st = meta.get("stale") or {}
        msg = f" {esc(str(st['message']))}" if st.get("message") else ""
        commit = f' Commit <code>{esc(str(st["commit"])[:7])}</code>{msg}.' if st.get("commit") else ""
        return (f'A later change to the code may contradict it: {esc(str(st.get("reason", "")))}.{commit}',
                f'If it is still true, keep it with <code>reverie stale keep {i}</code>. If not, forget it with <code>reverie undo {i}</code>.')
    if meta.get("challenged"):
        return ("Agents contradicted it repeatedly, so agents are now told to confirm or fix it before relying on it.",
                f'If it is wrong, forget it with <code>reverie undo {i}</code>. It can be restored.')
    return "", ""


def _mem_li(m: dict, pid: str, label: str = "") -> str:
    why, fix = _mem_look(m)
    meta_d = m.get("meta") or {}
    anchor = m.get("anchor") or {}
    bits = [f'<span>{esc(SAID.get(str(m.get("source", "")), "Remembered"))}</span>']
    files = [*(anchor.get("files") or []), *(anchor.get("symbols") or [])]
    if files:
        bits.append("<span>" + ", ".join(f'<code title="{esc(a)}">{esc(a)}</code>' for a in files) + "</span>")
    if m.get("created_at"):
        bits.append(f'<span>{esc(str(m["created_at"])[:10])}</span>')
    words = usage_words(meta_d.get("usage") or {})
    if words:
        bits.append(f"<span>{esc(words)}</span>")
    bits.append(f'<a class="open" href="/rule?p={esc(pid)}&amp;id={quote(str(m.get("id", ""))[:8])}">Open</a>')
    return ('<li class="entry">' + (f'<p class="label">{esc(label)}</p>' if label else "")
            + f'<p class="rule">{rule_html(str(m.get("text", "")))}</p>' + (f'<p class="why">{why}</p>' if why else "")
            + f'<p class="meta">{"".join(bits)}</p>' + (f'<p class="todo">{fix}</p>' if fix else "") + "</li>")


def memory_page(root: str, proj: dict, plist: list[dict]) -> str:
    """Every remembered item except rules (those are on Rules: each item is listed on exactly one tab)."""
    d = collect(root)
    w = waiting(root, d)
    pid = proj["id"]
    allm = d["mem"]
    rules_n = sum(1 for m in allm if m.get("kind") == "rule")
    ms = [m for m in allm if m.get("kind") != "rule"]
    if not ms and not rules_n:
        body = ('<p class="tally">Nothing remembered for this repo yet.</p>'
                '<p class="who">Decisions and findings from agent sessions appear here once Reverie has captured some.</p>')
        return _frame(root, proj, plist, d, w, "/memory", body, "Memory", "Memory")
    look = [m for m in ms if _mem_look(m)[0]]
    rest = [m for m in ms if not _mem_look(m)[0]]
    n = len(ms)
    if n:
        told = min(d["trace"].index, n)
        tally = f"{n} remembered {'item' if n == 1 else 'items'}."
        if told:
            tally += f" {told} {'is' if told == 1 else 'are'} told to agents in an index."
        if look:
            tally += f" {len(look)} {'needs' if len(look) == 1 else 'need'} a look."
    else:
        tally = "No decisions or findings remembered yet."
    out = [f'<p class="tally">{tally}</p>']
    if rules_n:
        out.append(f'<p class="who">{rules_n} {"rule you stated is" if rules_n == 1 else "rules you stated are"} listed under '
                   f'<a href="{_u("/context", proj)}">Rules</a>.</p>')
    if look:
        out.append('<section class="group" aria-labelledby="g-look"><h2 id="g-look">Needs a look</h2><ol class="items">'
                   + "".join(_mem_li(m, pid, _kind_label(m)) for m in look) + "</ol></section>")
    known = {k for k, _t, _l in MEM_KINDS}
    groups = list(MEM_KINDS) + [(k, _kind_name(k), _kind_name(k).rstrip("s")) for k in sorted({str(m.get("kind")) for m in rest} - known)]
    for kind, title, _lab in groups:
        g = [m for m in rest if str(m.get("kind")) == kind]
        if not g:
            continue
        items = "".join(_mem_li(m, pid) for m in g)
        inner = (f'<details class="fold"><summary>{len(g)} items</summary><ol class="items">{items}</ol></details>'
                 if len(g) > 10 else f'<ol class="items">{items}</ol>')
        out.append(f'<section class="group" aria-labelledby="g-{esc(kind)}"><h2 id="g-{esc(kind)}">{esc(title)}</h2>{inner}</section>')
    return _frame(root, proj, plist, d, w, "/memory", "".join(out), "Memory", "Memory")


def _kind_label(m: dict) -> str:
    k = str(m.get("kind"))
    return {kk: ll for kk, _t, ll in MEM_KINDS}.get(k) or _kind_name(k).rstrip("s")


# ---------------------------------------------------------------------------
# stage 3: the board, Add a project, Connect, Add a rule, Search, Turned off, Not found, Something went wrong
# ---------------------------------------------------------------------------

def _or_list(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " or " + parts[-1]


def _block(cmd: str) -> str:
    return f'<pre class="block-cmd">{esc(cmd)}</pre>'


def _enable_cmd(proj: dict) -> str:
    return f"cd {shlex.quote(proj['path'])} && reverie enable"


def waiting_words(w: dict) -> tuple[int, str]:
    """(how many wait on the viewer, 'N waiting on you: 2 proposals, 1 conflict, ...') from the same waiting() as the tab counts."""
    props = sum(1 for x in w["props"] if x["mine"])
    conflicts = sum(1 for x in w["conflicts"] if x["mine"])
    outdated = sum(1 for x in w["outdated"] if x["mine"])
    parts = [_n(k, one, many) for k, one, many in ((props, "proposal", "proposals"), (conflicts, "conflict", "conflicts"),
                                                   (outdated, "possibly outdated rule", "possibly outdated rules")) if k]
    total = props + conflicts + outdated
    return total, f"{total} waiting on you: {', '.join(parts)}." if total else ""


def projects_page(root: str, query=None, st: State | None = None) -> str:
    """The board: only what waits on the viewer, per project; projects with Reverie turned off sit quietly at the bottom."""
    plist = projects(root)
    flash = st.take(((query or {}).get("done") or [""])[0]) if st else None
    waits: list[tuple[dict, int, str]] = []
    clear: list[dict] = []
    off: list[dict] = []
    for p in plist:
        if is_disabled(p["path"]):
            off.append(p)
            continue
        n, words = waiting_words(waiting(p["path"], collect(p["path"])))
        (waits.append((p, n, words)) if n else clear.append(p))
    waits.sort(key=lambda t: -t[1])
    if waits:
        tally = (f"{len(waits)} project has something waiting on you." if len(waits) == 1
                 else f"{len(waits)} projects have something waiting on you.")
    else:
        tally = "Nothing is waiting on you in any project."
    link = lambda p, cls="": f'<a{cls} href="{_u("/project", p)}">{esc(p["name"])}</a>'      # noqa: E731
    out = ['<p class="topacts"><a href="/new">Add a project</a></p>']
    if flash and not flash.get("ok", True):
        out.append(f'<p class="err" role="alert">{esc(flash["msg"])}</p>')
    out.append(f'<p class="tally">{tally}</p>')
    if waits:
        out.append('<section class="group" aria-labelledby="g-wait"><h2 id="g-wait">Waiting on you</h2><ol class="items">'
                   + "".join(f'<li class="entry"><p class="proj">{link(p)}</p><p class="why">{esc(words)}</p></li>' for p, _n_, words in waits)
                   + "</ol></section>")
    if clear:
        out.append('<section class="group" aria-labelledby="g-clear"><h2 id="g-clear">Nothing waiting</h2><ul class="plain">'
                   + "".join(f"<li>{link(p)}</li>" for p in clear) + "</ul></section>")
    if off:
        out.append('<section class="group" aria-labelledby="g-off"><h2 id="g-off" class="dim">Turned off</h2><ul class="plain dim">'
                   + "".join(f'<li>{link(p, " class=" + chr(34) + "dim" + chr(34))}</li>' for p in off) + "</ul></section>")
    return layout(title="Projects, Reverie", proj=None, plist=plist, body="".join(out))


def _step(title: str, text: str, cmd: str) -> str:
    return f"<li><h2>{title}</h2><p>{text}</p>{_block(cmd)}</li>"


STEP_INSTALL = ("Set up your agent once per machine", "This adds Reverie to the agents you use (Claude Code, Codex and others it finds).",
                "reverie install")


def new_page(root: str, query=None) -> str:
    plist = projects(root)
    body = ('<h1 class="ask">Add a project</h1><p class="who">Projects are repos. A repo appears here once Reverie has seen it.</p>'
            '<ol class="steps">' + _step(*STEP_INSTALL)
            + _step("Use Reverie in the repo", "Open your agent in the repo, or run this there. Reverie is on by default, so either one "
                    "makes the repo appear here the next time this page is opened.", "cd path/to/your/repo && reverie enable")
            + "</ol>")
    return layout(title="Add a project, Reverie", proj=None, plist=plist, body=body)


def connect_page(root: str, proj: dict, plist: list[dict]) -> str:
    """Only the steps that apply: turning Reverie on is step 1 when it is off, and absent when it is on."""
    from . import install
    off = is_disabled(root)
    agents = _safe(install.configured_agent_names, [])
    steps = [_step("Turn it on for this project", "Reverie is on by default; this turns it back on after <code>reverie disable</code>.",
                   _enable_cmd(proj))] if off else []
    steps += [_step(*STEP_INSTALL),
              _step("Start a session and check", "Open your agent in the project. What it receives is on the “What agents are told” tab.",
                    "reverie status"),
              _step("Share rules with your team", "Point the repo at a shared folder of markdown rules in "
                    "<code>.reverie/context.toml</code>; everyone who opens the project gets them.", "reverie context")]
    tally = "Reverie is turned off for this repo. Step 1 turns it on." if off else "Reverie is on for this repo."
    who = (f"Set up on this machine: {esc(', '.join(agents))}." if agents else "No agent is set up on this machine yet.")
    body = f'<p class="tally">{tally}</p><p class="who">{who}</p><ol class="steps">{"".join(steps)}</ol>'
    counts: dict = {}
    if not off:
        counts = waiting(root, collect(root))["counts"]
    return layout(title=f"Connect an agent to {proj['name']}, Reverie", proj=proj, plist=plist, body=body, tab="/connect",
                  counts=counts, h1="Connect")


def off_page(proj: dict, plist: list[dict], tab: str) -> str:
    """Every project tab but Connect, for a repo Reverie is turned off in."""
    body = (f'<p class="tally">Reverie is turned off for {esc(proj["name"])}.</p>'
            '<p class="who">Nothing is told to agents and nothing is shown here.</p>'
            f'<p class="why">Turn it back on from a terminal:</p>{_block(_enable_cmd(proj))}')
    return layout(title=f"{proj['name']}, Reverie", proj=proj, plist=plist, body=body, tab=tab, counts={}, h1="Turned off")


def not_found_page(plist: list[dict]) -> str:
    return layout(title="Not found, Reverie", proj=None, plist=plist, body=(
        '<p class="tally">That page does not exist.</p><p class="go"><a href="/">Back to all projects</a></p>'))


def error_page() -> str:
    """A 500: no trace, no exception text, nothing read from disk. Only what to do next."""
    return layout(title="Something went wrong, Reverie", proj=None, plist=[], body=(
        '<p class="tally">Something went wrong.</p><p class="who">This page could not be built. The CLI still works.</p>'
        f'<p class="why">Reload the page. If it keeps failing, run this in a terminal:</p>{_block("reverie status")}'
        '<p class="go"><a href="/">Back to all projects</a></p>'))


# ---- Search ---------------------------------------------------------------------------------------------------

SEARCH_CAP = 100


def search_page(root: str, proj: dict, plist: list[dict], q: str) -> str:
    d = collect(root)
    w = waiting(root, d)
    q = " ".join(q.split())[:120]
    needle = q.lower()

    def page(body: str) -> str:
        return layout(title="Search, Reverie", proj=proj, plist=plist, body=body, counts=w["counts"], h1="Search", q=q)
    if not needle:
        return page('<p class="tally">Type something to search.</p>'
                    '<p class="who">Searches the team rules and the remembered items of this project.</p>')
    rules_hit = [r for r in d["records"] if needle in r["text"].lower()]
    mem_hit = [m for m in d["mem"] if m.get("kind") != "rule" and needle in str(m.get("text", "")).lower()]
    total = len(rules_hit) + len(mem_hit)
    if not total:
        return page(f'<p class="tally">No rules or memory items match “{esc(q)}”.</p>'
                    '<p class="who">Search looks for those words in the text of team rules and remembered items.</p>')
    ctx = _ctx(root, proj)
    ctx["owners_of_conflict"] = _owner_fn(w, ctx)
    out = [f'<p class="tally">{_n(total, "result", "results")} for “{esc(q)}”.</p>']
    if rules_hit:
        out.append('<section class="group" aria-labelledby="g-r"><h2 id="g-r">Rules</h2><ol class="items">'
                   + "".join(rule_li(r, ctx) for r in rules_hit[:SEARCH_CAP]) + "</ol></section>")
    if mem_hit:
        out.append('<section class="group" aria-labelledby="g-m"><h2 id="g-m">Memory</h2><ol class="items">'
                   + "".join(_mem_li(m, proj["id"], _kind_label(m)) for m in mem_hit[:SEARCH_CAP]) + "</ol></section>")
    if len(rules_hit) > SEARCH_CAP or len(mem_hit) > SEARCH_CAP:
        out.append(f'<p class="gnote">Showing the first {SEARCH_CAP} in each group. Search for more words to narrow it.</p>')
    return page("".join(out))


# ---- Add a rule -----------------------------------------------------------------------------------------------

FOLDER_WORD = {"org": "Organisation folder", "team": "Team folder"}


def add_targets(root: str) -> list[dict]:
    """Every place a rule can be added: this repo, then each configured shared folder (the CLI's own target_folder)."""
    out = [{"value": "repo", "folder": context.target_folder(root, "repo"), "repo": True, "title": "This repo"}]
    for layer, folders in context.sources(root).items():
        for f in folders:
            scope = context.scope_name(f)
            out.append({"value": f"{layer}|{scope}", "folder": f, "repo": False,
                        "title": f'{FOLDER_WORD.get(layer, layer.capitalize() + " folder")} <code>{esc(scope)}</code>'})
    return out


def _review_of(doc: Path) -> tuple[str, list[str]]:
    """What context.add would do for this document: 'open' (nobody owns it), 'direct' (you own it) or 'waits' (a proposal)."""
    owners = _safe(lambda: context.owners_of(doc), [])
    if not owners:
        return "open", []
    return ("direct" if _safe(lambda: context.is_owner(doc), False) else "waits"), owners


def _leads_to_proposal(t: dict) -> bool:
    """Whether adding here, as this viewer, can end as a proposal (a rule or guidance waiting for an owner): context.add's test."""
    folder = Path(t["folder"])
    return any(_review_of(folder / doc)[0] == "waits" for doc in ("rules.md", "guidance.md"))


def _add_note(t: dict, names: dict, me: "Viewer") -> str:
    """The true consequence of adding here as this viewer: from the same ownership test context.add applies."""
    rule_doc, guide_doc = Path(t["folder"]) / "rules.md", Path(t["folder"]) / "guidance.md"
    r, r_owners = _review_of(rule_doc)
    g, g_owners = _review_of(guide_doc)
    lead = "Everyone on this repo" if t["repo"] else "Everyone whose repos use this folder"
    who = lambda owners, sep=", ": (people_html(owners, names, me) if sep == ", " else _or_list([person_html(o, names, me) for o in owners]))  # noqa: E731
    if r == "open":
        note = f"{lead}, from the next session. No CODEOWNERS entry covers it, so there is no review."
    elif r == "direct":
        note = f"{lead}, from the next session. No review: you are an owner of <code>rules.md</code> ({who(r_owners)})."
    else:
        note = (f"{lead}. A rule waits until an owner of <code>rules.md</code> accepts it ({who(r_owners, ' or ')}); "
                + ("guidance is added straight away." if g != "waits" else
                   f"guidance waits for an owner of <code>guidance.md</code> ({who(g_owners, ' or ')})."))
    if r != "waits" and g == "waits":
        note += f" Guidance waits for an owner of <code>guidance.md</code> ({who(g_owners, ' or ')})."
    return note + f" Reverie edits the file{'' if t['repo'] else ' there'} and does not commit it."


def add_page(root: str, proj: dict, plist: list[dict], st: State | None = None, flash: dict | None = None) -> str:
    d = collect(root)
    w = waiting(root, d)
    ctx = _ctx(root, proj)
    names, me = ctx["names"], ctx["me"]
    flash = flash or {}
    bad_flash = bool(flash) and not flash.get("ok", True)
    err = (f'<p class="err" id="add-error" role="alert">{esc(flash["msg"])}</p>' if bad_flash else "")
    bad = flash.get("bad", "") if bad_flash else ""
    invalid = ' aria-invalid="true" aria-describedby="add-error"'
    targets = add_targets(root)
    chosen = flash.get("target") if flash.get("target") in {t["value"] for t in targets} else "repo"
    opts = "".join(
        f'<label class="opt"><input type="radio" name="target" value="{esc(t["value"])}"'
        f'{" checked" if t["value"] == chosen else ""}{invalid if bad == "target" else ""}><span><b>{t["title"]}</b>'
        f'<span class="d">{_add_note(t, names, me)}</span></span></label>' for t in targets)
    kinds = "".join(
        f'<label class="opt"><input type="radio" name="kind" value="{v}"{" checked" if v == (flash.get("kind") or "rule") else ""}>'
        f'<span><b>{title}</b><span class="d">{note}</span></span></label>'
        for v, title, note in (("rule", "A rule", "Agents are told it at the start of every session."),
                               ("guidance", "Guidance", "Agents see a pointer to it and open it when it is relevant.")))
    why_field = (f'<label class="why-in">Why (optional)<input type="text" name="why" maxlength="{LIMITS["why"]}" '
                 f'value="{esc(flash.get("why", ""))}"><span class="d">Only a proposal keeps this: the owners read it next to the rule. '
                 f'A direct add does not save it.</span></label>') if any(_leads_to_proposal(t) for t in targets) else ""
    form = ('<form class="addf" method="post" action="/do/add">'
            f'<input type="hidden" name="token" value="{html.escape(st.token if st else "")}">'
            f'<input type="hidden" name="p" value="{esc(proj["id"])}">{err}'
            f'<div><label class="visually-hidden" for="text">The rule</label><textarea id="text" name="text" '
            f'maxlength="{LIMITS["text"]}" required{invalid if bad == "text" else ""}>{esc(flash.get("text", ""))}</textarea></div>'
            f'<fieldset><legend>Who is told</legend>{opts}</fieldset><fieldset><legend>How agents get it</legend>{kinds}</fieldset>'
            + why_field + '<div><button class="btn" type="submit">Add rule</button></div></form>')
    terminal = ('<section class="subtle"><details class="cmd"><summary>From a terminal</summary><p>The same as this form:</p>'
                + _block('reverie context add "Money is stored as integer cents"')
                + "<p>Only for your own agents, in every repo:</p>"
                + _block('reverie rules add --user "Prefer rg over grep in examples"')
                + "<p>Turn a rule you already use into a team proposal. A proposal keeps the reason:</p>"
                + _block('reverie context propose "Money is stored as integer cents" --why "rounding drift"')
                + "</details><p>A rule that looks like a secret is refused and not kept.</p></section>")
    banner = _flash_html(flash) if flash and not bad_flash else ""
    body = (banner + '<h1 class="ask">What should agents know about this repo?</h1>' + acting_html(me) + form + terminal)
    return _frame(root, proj, plist, d, w, "/add", body, "Add a rule", "")


# ---------------------------------------------------------------------------
# writes: the CSRF token, one-shot messages, and the three actions (all through the CLI's functions)
# ---------------------------------------------------------------------------

class State:
    """Per-process: the CSRF token and the one-shot messages shown after a write."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()             # one write at a time
        self._flash: dict[str, dict] = {}

    def put(self, ok: bool, msg: str, **keep: str) -> str:
        fid = secrets.token_urlsafe(8)
        with self.lock:
            while len(self._flash) >= 50:
                self._flash.pop(next(iter(self._flash)))
            self._flash[fid] = {"ok": ok, "msg": msg, **keep}
        return fid

    def take(self, fid: str) -> dict | None:
        return self._flash.pop(fid, None)


CHECK_LATER = (" It has not been checked for conflicts yet: that happens at the next session start, "
               "or now with `reverie context check`.")
NOT_SAVED = "Nothing was saved: "


def _do_add(root: str, proj: dict, form: dict[str, str], back) -> str:
    """The Add form, through context.add (the CLI's function). Every refusal starts with "Nothing was saved:", is reached by
    a direct POST exactly as by the form, and never carries a secret back to the page."""
    text, why = " ".join(form.get("text", "").split()), form.get("why", "")
    choice, kind = form.get("target", "repo"), form.get("kind", "rule")
    keep = {"text": text, "why": why, "target": choice, "kind": kind}
    if not text:
        return back(False, NOT_SAVED + "write the rule first.", bad="text", why=why, target=choice, kind=kind)
    if redact.contains_secret(text) or redact.contains_secret(why):
        return back(False, NOT_SAVED + "this looks like it contains a secret. Remove the value and try again. "
                                       "The text was cleared on purpose.", bad="text")
    if choice not in {t["value"] for t in add_targets(root)}:
        return back(False, NOT_SAVED + "this project cannot add to that place. Pick another option.", bad="target", **keep)
    layer, _, scope = choice.partition("|")
    rule = kind != "guidance"
    try:
        added = context.add(root, text, layer=layer, scope=scope, rule=rule, why=why)
    except ValueError as exc:
        return back(False, NOT_SAVED + str(exc), bad="text", **keep)
    except Exception:
        return back(False, NOT_SAVED + "that did not work. The CLI still does.", bad="text", **keep)
    names, me = known_people(root), Viewer(root)
    quoted = f"“{rule_html(text)}”"
    check = _backtick_cmds(esc(CHECK_LATER))
    if added.proposal:
        doc = "rules.md" if rule else "guidance.md"
        owners = _or_list([person_html(o, names, me) for o in added.owners])
        h = (f'Proposed: {quoted} It waits for an owner of <code>{esc(doc)}</code>: {owners}. It is not in force yet. '
             f'<a href="{esc(_u("/proposals", proj))}">See the proposal</a>')
        return back(True, f"Proposed: {text}", html=h)
    h = (f'Added to <code>{esc(added.path.name)}</code>: {quoted} It applies from the next session.{check} '
         f'<a href="{esc(_u("/context", proj))}">See it in Rules</a>')
    return back(True, f"Added to {added.path.name}: {text}", html=h)


def do_write(root: str, st: State, path: str, form: dict[str, str]) -> str:
    """Run one write through the CLI's functions and return where to redirect. Security checks
    (Host, Origin, token, size) are done by the handler before this is called."""
    plist = projects(root)
    proj = next((x for x in plist if x["id"] == form.get("p", "")), None)
    adding = path == "/do/add"
    no = NOT_SAVED if adding else "Refused: "
    if proj is None:
        return _u("/", None, done=st.put(False, f"{no}that project is not known to Reverie."))
    target = proj["path"]
    page = "/add" if adding else "/proposals"
    back = lambda ok, msg, **keep: _u(page, proj, done=st.put(ok, msg, **keep))   # noqa: E731
    if is_disabled(target):
        return back(False, f"{no}Reverie is turned off for this project. Turn it on first.")
    why = form.get("why", "")
    with st.lock:
        try:
            if adding:
                return _do_add(target, proj, form, back)
            slug = form.get("slug", "")
            # exact name only: the CLI matches a prefix, and "" is a prefix of every proposal
            if slug not in {x["slug"] for x in context.proposals(target)}:
                return back(False, "Refused: that proposal is not waiting any more.")
            if path == "/do/accept":
                rule = next((context.bullet_text(x["bullet"]) for x in context.proposals(target) if x["slug"] == slug), "")
                doc = context.accept(target, slug)
                return back(True, f"Accepted into {doc.name}: “{rule}” The proposal is gone and the rule is in force."
                                + CHECK_LATER)
            if redact.contains_secret(why):
                return back(False, "Refused: the reason looks like it contains a secret, so nothing was "
                                   "changed. Remove the value and try again.")
            dest = context.reject(target, slug, why)
            return back(True, f"Rejected. The reason is kept in proposals/rejected/{dest.name}.")
        except ValueError as exc:
            return back(False, f"Refused: {exc}")
        except Exception:
            return back(False, "That did not work, and nothing was changed. The CLI still does.")


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

PROJECT_PAGES: dict[str, Callable[..., str]] = {"/project": overview, "/start": start, "/context": rules_page,
                 "/proposals": proposals_page, "/connect": connect_page, "/add": add_page, "/memory": memory_page}
TAB_PATHS = ("/project", "/context", "/memory", "/start", "/proposals", "/connect")


def render(root: str, path: str, query: dict[str, list[str]] | None = None,
           st: State | None = None) -> tuple[int, str]:
    try:
        target, proj, plist = _target(root, query)
        want = ((query or {}).get("p") or [""])[0]
        if want and path != "/new" and path not in ("/", "/repos") and not any(p["id"] == want for p in plist):
            # an unknown project id is its own 404: never another project's page
            return 404, layout(title="No such project, Reverie", proj=None, plist=plist, body=(
                '<p class="tally">No project with that id.</p><p class="go"><a href="/">Back to all projects</a></p>'))
        if path in ("/", "/repos"):
            return 200, projects_page(root, query, st)
        if path == "/new":
            return 200, new_page(root, query)
        if path in ("/search", "/rule") or path in PROJECT_PAGES:
            if path != "/connect" and is_disabled(target):    # Connect is how you turn it back on
                if st:                                        # a refusal waiting for this page is shown nowhere else
                    st.take(((query or {}).get("done") or [""])[0])
                return 200, off_page(proj, plist, path if path in TAB_PATHS else "/project")
            if path == "/rule":
                rid = ((query or {}).get("id") or [""])[0]
                return rule_page(target, proj, plist, rid if ID_OK.match(rid) else "")
            if path == "/search":
                return 200, search_page(target, proj, plist, ((query or {}).get("q") or [""])[0])
            if path in ("/add", "/proposals"):
                flash = st.take(((query or {}).get("done") or [""])[0]) if st else None
                return 200, PROJECT_PAGES[path](target, proj, plist, st, flash)
            return 200, PROJECT_PAGES[path](target, proj, plist)
        return 404, not_found_page(plist)
    except Exception:
        return 500, error_page()


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------

def make_handler(root: str, allowed_hosts: set[str], state: State | None = None):
    st = state or State()
    origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "reverie-ui"
        sys_version = ""

        def log_message(self, *args, **kwargs) -> None:     # no access log, no telemetry
            return

        def _reply(self, code: int, body: str, head: bool = False, extra: dict | None = None) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _serve(self, head: bool) -> None:
            if (self.headers.get("Host") or "").lower() not in allowed_hosts:
                self._reply(403, "forbidden", head)
                return
            parts = urlsplit(self.path)
            try:
                code, body = render(root, parts.path, parse_qs(parts.query), st)
            except Exception:                         # render guards itself; this keeps even a parse error off the wire
                code, body = 500, error_page()
            self._reply(code, body, head)

        def do_GET(self) -> None:
            self._serve(False)

        def do_HEAD(self) -> None:
            self._serve(True)

        def _no(self) -> None:
            self._reply(405, "only GET and HEAD are served here", extra={"Allow": "GET, HEAD"})

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if origin is not None:
                return origin.lower() in origins and site in ("", "same-origin")
            return site == "same-origin"              # neither header: refused

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                n = -1
            body = b""
            if 0 <= n <= MAX_BODY:                    # read first, so an early refusal is not a reset
                body = self.rfile.read(n)
            if (self.headers.get("Host") or "").lower() not in allowed_hosts:
                self._reply(403, "forbidden")
            elif path not in WRITE_PATHS:
                self._no()
            elif not self._same_origin():
                self._reply(403, "forbidden: not a same-origin request")
            elif n < 0:
                self._reply(411, "Content-Length required")
            elif n > MAX_BODY:
                self._reply(413, "too large")
            elif (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() \
                    != "application/x-www-form-urlencoded":
                self._reply(415, "unsupported content type")
            else:
                raw = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
                form = {k: (v[0] if v else "")[: LIMITS[k]] for k, v in raw.items() if k in LIMITS}
                if not hmac.compare_digest(form.get("token", "").encode(), st.token.encode()):
                    self._reply(403, "forbidden: missing or wrong token")
                else:
                    try:
                        where = do_write(root, st, path, form)
                    except Exception:
                        self._reply(500, error_page())
                        return
                    self._reply(303, "see other", extra={"Location": where})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _no

    return Handler


def make_server(root: str, port: int = 0) -> ThreadingHTTPServer:
    """Bind 127.0.0.1 (never another interface). Raises OSError if the port is taken."""
    server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    bound = server.server_address[1]
    state = State()
    server.ui_state = state                           # type: ignore[attr-defined]
    server.RequestHandlerClass = make_handler(root, {f"127.0.0.1:{bound}", f"localhost:{bound}"}, state)
    server.daemon_threads = True
    return server


def serve(root: str, port: int = 0, *, open_browser: bool = True, print_fn=print) -> int:
    try:
        server = make_server(root, port)
    except OSError as exc:
        print_fn(f"cannot listen on 127.0.0.1:{port}: {exc.strerror or exc}. "
                 f"Try another --port, or leave it out to pick a free one.")
        return 2
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print_fn(f"Reverie at {url}  (Ctrl-C to stop; it writes only when you press a button)")
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
