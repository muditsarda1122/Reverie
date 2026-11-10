"""`reverie ui`: a read-only local page. A command, not a daemon: it serves on 127.0.0.1 until
Ctrl-C, from the same functions the CLI uses, and changes nothing.

* only GET and HEAD answer (everything else is 405); nothing here writes a file; the brain is
  opened read-only;
* bound to 127.0.0.1; a Host header that is not the bound address is refused (DNS rebinding);
* every value is HTML-escaped (shared documents are written by other people) and passed through
  the existing redaction; a strict Content-Security-Policy; no script, no external resource
  (system fonts, inline SVG icons, native <details> for the "how to fix it" commands);
* no telemetry of any kind: the server logs nothing and reports nothing.

Standard library only.
"""

from __future__ import annotations

import html
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import context, deliver, redact, rules, spool

PAGE_SIZE = 100
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"

# Design: an operate-mode product surface. One neutral family (cool grey), a darker neutral for
# the sidebar, one cobalt accent used only for the current page, links and focus; amber, red and
# green are reserved for status. Flat 1px hairlines, one soft shadow, 150ms transitions.
CSS = """
:root{
--bg:#f5f6f8;--side:#eceef2;--surface:#fff;--sunk:#f1f3f6;--line:#e2e5ea;--line2:#cfd4dc;
--fg:#151a21;--mut:#5a6472;--faint:#7b8593;
--accent:#2457d6;--accent-bg:#e8eefc;--accent-fg:#1a43ad;
--warn-bg:#fff5df;--warn-line:#f0d9a0;--warn-fg:#8a4a00;
--bad-bg:#fdecec;--bad-line:#f3c4c2;--bad-fg:#a1231f;
--ok-bg:#e7f5ec;--ok-line:#bfe3cd;--ok-fg:#17603a;
--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 1px rgba(16,24,40,.04);
--ease:cubic-bezier(.22,1,.36,1)}
@media (prefers-color-scheme:dark){:root{
--bg:#0f1115;--side:#0a0c0f;--surface:#161a20;--sunk:#1b2027;--line:#262c35;--line2:#364050;
--fg:#e8eaee;--mut:#a0a9b6;--faint:#7e8895;
--accent:#7ea3ff;--accent-bg:#1b2745;--accent-fg:#a9c1ff;
--warn-bg:#2a2210;--warn-line:#5a4716;--warn-fg:#f0bd62;
--bad-bg:#2c1717;--bad-line:#5e2b2a;--bad-fg:#f49a95;
--ok-bg:#122519;--ok-line:#24503a;--ok-fg:#7fd1a1;
--shadow:0 1px 2px rgba(0,0,0,.4)}}
*{box-sizing:border-box}
html{color-scheme:light dark}
body{margin:0;background:var(--bg);color:var(--fg);font:14.5px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
::selection{background:var(--accent-bg);color:var(--accent-fg)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
*{scrollbar-width:thin;scrollbar-color:var(--line2) transparent}
a{color:var(--accent);text-underline-offset:3px;text-decoration-thickness:1px}
a:hover{text-decoration-thickness:2px}
code{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--sunk);
border:1px solid var(--line);border-radius:5px;padding:1px 5px;overflow-wrap:anywhere}
pre{font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;margin:0;padding:18px 20px;
white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;max-height:70vh}

.app{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100vh}
.side{background:var(--side);border-right:1px solid var(--line);padding:18px 12px;position:sticky;top:0;
height:100vh;display:flex;flex-direction:column;gap:18px;overflow:auto}
.brand{display:flex;align-items:center;gap:10px;padding:2px 8px;font-weight:650;font-size:15.5px;letter-spacing:-.01em}
.mark{width:26px;height:26px;border-radius:7px;background:var(--accent);color:#fff;display:grid;place-items:center}
.repo{margin:0 4px;padding:9px 11px;background:var(--surface);border:1px solid var(--line);border-radius:9px;
box-shadow:var(--shadow);min-width:0}
.repo small{display:block;color:var(--faint);font-size:11.5px;margin-bottom:1px}
.repo b{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
nav{display:flex;flex-direction:column;gap:2px}
.grp{font-size:11.5px;font-weight:600;color:var(--faint);padding:10px 10px 4px;text-transform:none}
nav a{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:8px;color:var(--mut);
text-decoration:none;font-weight:500;transition:background-color .15s var(--ease),color .15s var(--ease)}
nav a:hover{background:var(--sunk);color:var(--fg)}
nav a[aria-current=page]{background:var(--surface);color:var(--fg);box-shadow:var(--shadow);
outline:1px solid var(--line)}
nav a[aria-current=page] svg{color:var(--accent)}
nav svg{flex:none}
.count{margin-left:auto;min-width:20px;height:20px;padding:0 6px;border-radius:10px;font-size:11.5px;font-weight:650;
display:grid;place-items:center;background:var(--warn-bg);color:var(--warn-fg);border:1px solid var(--warn-line)}
.foot{margin-top:auto;padding:10px;color:var(--faint);font-size:12px;display:flex;gap:8px;align-items:flex-start}

main{padding:36px clamp(18px,4vw,56px) 64px;min-width:0}
.wrap{max-width:62rem;margin:0 auto}
.crumb{color:var(--faint);font-size:12.5px;margin:0 0 6px}
h1{font-size:1.65rem;line-height:1.2;letter-spacing:-.02em;margin:0;font-weight:680;text-wrap:balance}
.lede{color:var(--mut);margin:8px 0 0;max-width:62ch}
h2{font-size:1rem;margin:36px 0 12px;font-weight:650;letter-spacing:-.005em;display:flex;align-items:center;gap:8px}
h2 .n{font-weight:600;color:var(--mut);font-size:12.5px;background:var(--sunk);border:1px solid var(--line);
border-radius:10px;padding:0 8px}
.lede+.panel,.lede+.pager{margin-top:22px}
.mut{color:var(--mut)}.faint{color:var(--faint)}

.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
.status{display:flex;align-items:center;gap:12px;padding:14px 16px;border-radius:12px;border:1px solid;margin-top:22px}
.status b{font-weight:650}.status.attn{background:var(--warn-bg);border-color:var(--warn-line);color:var(--warn-fg)}
.status.clear{background:var(--ok-bg);border-color:var(--ok-line);color:var(--ok-fg)}
.status svg{flex:none}

.list{list-style:none;margin:0;padding:0}
.row{padding:16px 18px;border-top:1px solid var(--line);display:grid;grid-template-columns:8.75rem minmax(0,1fr);gap:4px 14px;
transition:background-color .15s var(--ease)}
.list>.row:first-child{border-top:0}
.row:hover{background:var(--sunk)}
.row .body{min-width:0;overflow-wrap:anywhere}
.row .title{font-weight:600}
.row .quote{margin:6px 0 0;color:var(--fg)}
.row .meta{margin:6px 0 0;color:var(--mut);font-size:13px}
.badge{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;border-radius:11px;font-size:12px;
font-weight:600;border:1px solid var(--line);background:var(--sunk);color:var(--mut);white-space:nowrap;align-self:start;justify-self:start}
.badge.warn{background:var(--warn-bg);border-color:var(--warn-line);color:var(--warn-fg)}
.badge.bad{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad-fg)}
.badge.ok{background:var(--ok-bg);border-color:var(--ok-line);color:var(--ok-fg)}
.badge.acc{background:var(--accent-bg);border-color:transparent;color:var(--accent-fg)}
details{margin-top:10px}
summary{cursor:pointer;color:var(--accent);font-weight:550;font-size:13px;list-style:none;display:inline-flex;gap:6px;
align-items:center;border-radius:6px}
summary::-webkit-details-marker{display:none}
summary::before{content:"";width:6px;height:6px;border-right:1.75px solid currentColor;border-bottom:1.75px solid currentColor;
transform:rotate(-45deg);transition:transform .15s var(--ease);margin-right:2px}
details[open] summary::before{transform:rotate(45deg)}
.cmd{display:block;margin-top:8px;padding:10px 12px;background:var(--sunk);border:1px solid var(--line);border-radius:8px;
font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere}
.cmd+.cmd{margin-top:6px}

.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}
.stat{display:flex;flex-direction:column;gap:2px;padding:16px 18px;color:var(--mut);text-decoration:none;font-size:13px;
border-left:1px solid var(--line);transition:background-color .15s var(--ease)}
.stat:first-child{border-left:0}
.stat:hover{background:var(--sunk)}
.stat b{font-size:1.7rem;line-height:1.15;color:var(--fg);font-weight:680;letter-spacing:-.02em;order:-1}
.stat.hot b{color:var(--warn-fg)}
.stat small{color:var(--faint);font-size:12px}
.links{display:block}
.link{display:flex;gap:12px;padding:14px 18px;text-decoration:none;color:var(--fg);border-top:1px solid var(--line);
transition:background-color .15s var(--ease)}
.link:first-child{border-top:0}
.link:hover{background:var(--sunk)}
.link svg{color:var(--faint);flex:none;margin-top:2px}
.link b{display:block;font-weight:600}.link span{color:var(--mut);font-size:13px}

.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{position:sticky;top:0;text-align:left;font-size:12px;font-weight:600;color:var(--mut);background:var(--sunk);
padding:9px 14px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
tbody tr:last-child td{border-bottom:0}
tbody tr{transition:background-color .15s var(--ease)}tbody tr:hover{background:var(--sunk)}
td.text{min-width:16rem}td .sub{display:block;color:var(--faint);font-size:12px;margin-top:2px}
.marks{display:flex;flex-wrap:wrap;gap:6px}
.empty{padding:34px 24px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:8px}
.empty .ico{width:40px;height:40px;border-radius:12px;background:var(--sunk);border:1px solid var(--line);
display:grid;place-items:center;color:var(--faint)}
.empty b{font-size:1rem}.empty p{margin:0;color:var(--mut);max-width:46ch}
.pager{display:flex;align-items:center;gap:12px;color:var(--mut);margin:14px 2px}
.pager a{padding:5px 11px;border:1px solid var(--line);border-radius:8px;background:var(--surface);text-decoration:none;
color:var(--fg);font-weight:550;box-shadow:var(--shadow)}
.pager a:hover{border-color:var(--line2)}
.note{margin-top:40px;color:var(--faint);font-size:12.5px}

@media (max-width:860px){
.app{grid-template-columns:minmax(0,1fr)}
.side{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);padding:12px;gap:10px}
.repo,.foot,.grp{display:none}
nav{flex-direction:row;overflow-x:auto;padding-bottom:2px}nav a{white-space:nowrap}
main{padding:24px 16px 48px}
.row{grid-template-columns:1fr}
.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.stat:nth-child(3){border-left:0}
.stat:nth-child(n+3){border-top:1px solid var(--line)}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

# icons: authored, 24px grid, 1.75 stroke, one weight everywhere
_ICONS = {
    "home": '<path d="M4 11.5 12 5l8 6.5"/><path d="M6 10.5V19h12v-8.5"/><path d="M10 19v-5h4v5"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
    "users": '<circle cx="9" cy="8.5" r="3"/><path d="M3.5 19c.4-3 2.6-4.8 5.5-4.8s5.1 1.8 5.5 4.8"/>'
             '<path d="M15.5 5.8a3 3 0 0 1 0 5.4"/><path d="M17.5 14.6c1.8.6 2.7 2 3 4.4"/>',
    "inbox": '<path d="M3.5 13.5 6 6h12l2.5 7.5"/><path d="M3.5 13.5V18a1 1 0 0 0 1 1h15a1 1 0 0 0 1-1v-4.5H15a3 3 0 0 1-6 0Z"/>',
    "layers": '<path d="m12 4 8.5 4.5L12 13 3.5 8.5 12 4Z"/><path d="m3.5 12.5 8.5 4.5 8.5-4.5"/><path d="m3.5 16.5 8.5 4.5 8.5-4.5"/>',
    "folder": '<path d="M3.5 7.5a1.5 1.5 0 0 1 1.5-1.5h4l2 2.2h8a1.5 1.5 0 0 1 1.5 1.5v7.8a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5V7.5Z"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12.3 2.8 2.8L16.2 9.5"/>',
    "alert": '<path d="M12 4 21 19.5H3L12 4Z"/><path d="M12 10v4.2"/><path d="M12 16.9v.1"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="9" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
    "off": '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6 18.4 18.4"/>',
    "shield": '<path d="M12 3.5 19 6v5.5c0 4.2-2.8 7.4-7 9-4.2-1.6-7-4.8-7-9V6l7-2.5Z"/><path d="m9 12 2.2 2.2L15.2 10"/>',
}


def icon(name: str, size: int = 18) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f'{_ICONS[name]}</svg>')


# (path, label, icon, group, one line that explains the page)
NAV = (
    ("/", "Overview", "home", "", "What needs attention and what is in force"),
    ("/start", "What agents are told", "eye", "Agents", "The exact text an agent gets at session start"),
    ("/context", "Team context", "users", "Team", "Shared rules, who they apply to, and where they clash"),
    ("/proposals", "Proposals", "inbox", "Team", "Rules people suggested, waiting for an owner"),
    ("/memory", "Memory", "layers", "This repo", "What Reverie remembered from earlier sessions"),
    ("/repos", "Repos", "folder", "This repo", "Every repo Reverie knows about on this machine"),
)


def esc(value: object, *, redacted: bool = True) -> str:
    text = redact.redact(str(value)) if redacted else str(value)
    return html.escape(text, quote=True)


def _clip(text: str, n: int = 300) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= n else one[: n - 1] + "…"


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
    mem_rules = [m for m in mem if m["kind"] in ("rule", "preference")
                 and not deliver.is_stale_flagged(m)]
    return {
        "res": res, "mem": mem, "stale_mem": stale_mem, "team_rules": team_rules,
        "personal": personal, "repo_rules": repo_rules, "mem_rules": mem_rules,
        "unresolved": [c for c in res.conflicts if c.status == "unresolved"],
        "stale_team": [i for i in res.items if i.id in res.stale],
        "proposals": _safe(lambda: context.proposals(root), []),
        "rejected": _safe(lambda: context.rejected(root), []),
    }


def _attention(d: dict) -> int:
    return len(d["unresolved"]) + len(d["stale_team"]) + len(d["stale_mem"]) + len(d["proposals"])


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------

def layout(title: str, body: str, active: str, *, root: str = "", lede: str = "",
           badges: dict[str, int] | None = None) -> str:
    badges = badges or {}
    out, last = [], None
    for url, name, ico, group, _ in NAV:
        if group != last and group:
            out.append(f'<div class=grp>{esc(group)}</div>')
        last = group
        n = badges.get(url, 0)
        cur = ' aria-current="page"' if url == active else ""
        count = f'<span class=count title="{n} to look at">{n}</span>' if n else ""
        out.append(f'<a href="{url}"{cur}>{icon(ico)}<span>{esc(name)}</span>{count}</a>')
    repo_name = _safe(lambda: spool.repo_root(root).rstrip("/").rsplit("/", 1)[-1], "") if root else ""
    repo = (f'<div class=repo><small>Repo</small><b title="{esc(spool.repo_root(root))}">{esc(repo_name)}</b></div>'
            if repo_name else "")
    sub = f'<p class=lede>{lede}</p>' if lede else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)} · Reverie</title><style>{CSS}</style></head><body><div class=app>"
            f"<aside class=side><div class=brand><span class=mark>{icon('shield', 16)}</span>Reverie</div>"
            f"{repo}<nav aria-label=Pages>{''.join(out)}</nav>"
            f"<div class=foot>{icon('lock', 15)}<span>Read-only view. Nothing here changes anything.</span></div>"
            f"</aside><main><div class=wrap><h1>{esc(title)}</h1>{sub}{body}"
            f"<p class=note>Each item shows the command that changes it. This page never does.</p>"
            f"</div></main></div></body></html>")


def _empty(ico: str, title: str, text: str) -> str:
    return (f'<div class="panel empty"><span class=ico>{icon(ico, 20)}</span><b>{esc(title)}</b>'
            f'<p>{text}</p></div>')


def _cmd(*commands: str, label: str = "How to settle it") -> str:
    lines = "".join(f"<code class=cmd>{c}</code>" for c in commands)
    return f"<details><summary>{esc(label)}</summary>{lines}</details>"


def _disabled_page(active: str, root: str = "") -> str:
    return layout("Reverie is turned off for this repo",
                  _empty("off", "Turned off here",
                         "Nothing is delivered to agents and nothing is shown here. "
                         "Turn it back on with <code>reverie enable</code>."),
                  active, root=root)


def _owner(item) -> str:
    return _safe(lambda: context.owner_text(item), "")


def _badges(d: dict) -> dict[str, int]:
    return {"/": _attention(d), "/proposals": len(d["proposals"])}


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

def _row(kind: str, tone: str, body: str) -> str:
    return f'<li class=row><span class="badge {tone}">{esc(kind)}</span><div class=body>{body}</div></li>'


def home(root: str) -> str:
    d = collect(root)
    in_force = len(d["team_rules"]) + len(d["personal"]) + len(d["mem_rules"])
    outdated = len(d["stale_team"]) + len(d["stale_mem"])
    nums = [("Rules in force", in_force, "agents follow these", "/start", False),
            ("Conflicts to settle", len(d["unresolved"]), "two rules disagree", "/context", True),
            ("Possibly outdated", outdated, "the code may have moved on", "/context", True),
            ("Proposals waiting", len(d["proposals"]), "waiting for an owner", "/proposals", True)]
    top = "".join(f'<a class="stat{" hot" if hot and n else ""}" href="{u}"><b>{n}</b>{esc(label)}'
                  f'<small>{esc(sub)}</small></a>' for label, n, sub, u, hot in nums)

    action: list[str] = []
    for c in d["unresolved"]:
        a, b = c.overridden[0], c.overridden[1]
        who = "; ".join(x for x in (_owner(a), _owner(b)) if x)
        action.append(_row("Conflict", "bad",
            f"<div class=title>Two rules disagree. {esc(a.label)} and {esc(b.label)} say different things.</div>"
            f"<p class=quote>{esc(a.label)}: “{esc(_clip(a.text, 160))}”<br>"
            f"{esc(b.label)}: “{esc(_clip(b.text, 160))}”</p>"
            f"<p class=meta>Agents are told to ask before acting on either."
            + (f" Ask: {esc(who)}." if who else "") + "</p>"
            + _cmd(f'reverie context resolve {esc(c.key)} "your decision"')))
    for it in d["stale_team"]:
        st = d["res"].stale.get(it.id, {})
        action.append(_row("Possibly outdated", "warn",
            f"<div class=title>“{esc(_clip(it.text, 200))}”</div>"
            f"<p class=meta>{esc(it.label)}. A later change to the code may contradict it: "
            f"{esc(_clip(str(st.get('reason', '')), 200))}</p>"
            + _cmd(f"Check the code, then edit or remove the rule in {esc(it.file)}",
                   label="What to do")))
    for m in d["stale_mem"]:
        action.append(_row("Possibly outdated", "warn",
            f"<div class=title>“{esc(_clip(m['text'], 200))}”</div>"
            f"<p class=meta>Remembered from an earlier session.</p>"
            + _cmd("reverie stale list", label="What to do")))
    for p in d["proposals"]:
        action.append(_row("Proposal", "acc",
            f"<div class=title>A proposal is waiting.</div>"
            f"<p class=quote>“{esc(_clip(context.bullet_text(p['bullet']), 200))}”</p>"
            f"<p class=meta>Proposed by {esc(p.get('proposed-by', 'someone'))}"
            + (f" because: {esc(_clip(p.get('why', ''), 160))}" if p.get("why") else "")
            + ".</p>"
            + _cmd(f"reverie context accept {esc(p['slug'])}",
                   f'reverie context reject {esc(p["slug"])} --why "…"',
                   label="An owner decides")))

    n = _attention(d)
    if action:
        status = (f'<div class="status attn">{icon("alert", 20)}<span><b>{n} thing{"s" if n != 1 else ""} '
                  f'need{"" if n != 1 else "s"} a look.</b> Nothing is broken; agents carry on '
                  f'with the safest reading.</span></div>')
        needs = f'<h2>Needs your action <span class=n>{n}</span></h2><ul class="panel list">{"".join(action)}</ul>'
    else:
        status = (f'<div class="status clear">{icon("check", 20)}<span><b>All clear.</b> '
                  f'Every rule is in force and nothing is waiting on anyone.</span></div>')
        needs = ("<h2>Needs your action</h2>"
                 + _empty("check", "Nothing needs your action right now.",
                          "Conflicts, outdated rules and waiting proposals will show up here first."))
    links = "".join(f'<a class=link href="{u}">{icon(ico)}<div><b>{esc(name)}</b>'
                    f'<span>{esc(desc)}</span></div></a>'
                    for u, name, ico, _, desc in NAV[1:])
    body = (status + f'<h2>At a glance</h2><div class="panel stats">{top}</div>'
            + needs + f'<h2>Look closer</h2><div class="panel links">{links}</div>')
    return layout("Overview", body, "/", root=root, badges=_badges(d),
                  lede=f"The state of the rules your agents follow in <code>{esc(spool.repo_root(root))}</code>.")


def start(root: str) -> str:
    d = collect(root)
    text = _safe(lambda: deliver.startup_text(root, log=False), "")
    if text:
        body = (f'<h2>The block <span class=n>{len(text)} characters</span></h2>'
                f'<div class=panel><pre id=start>{esc(text, redacted=False)}</pre></div>')
    else:
        body = _empty("eye", "Nothing.", "No rules or memory apply here yet, so an agent starts with a clean slate.")
    return layout("What agents are told", body, "/start", root=root, badges=_badges(d),
                  lede="This is exactly what an agent is handed when a session starts in this repo. "
                       "Viewing it here is not a delivery and is not counted as one.")


def _chips(marks: list[str]) -> str:
    return "".join(f'<span class="badge {"bad" if m == "conflict" else "warn" if "outdated" in m else ""}">'
                   f'{esc(m)}</span>' for m in marks)


def context_page(root: str) -> str:
    d = collect(root)
    res = d["res"]
    if not res.items:
        return layout("Team context", _empty("users", "No team context applies here",
                      "Put markdown in <code>.reverie/context/</code> (see <code>reverie context</code>) "
                      "to share rules with a team."), "/context", root=root, badges=_badges(d))
    rows = []
    lost = {o.id for c in res.conflicts for o in c.overridden}
    for it in res.items:
        marks = [m for m, on in (("locked", it.locked), ("conflict", it.id in lost),
                                 ("possibly outdated", it.id in res.stale),
                                 ("also a shared rule: hidden here", it.id in res.hidden),
                                 (f"only under {', '.join(it.paths)}", bool(it.paths))) if on]
        rows.append(f"<tr class=item><td><span class=badge>{esc(it.label)}</span></td><td>{esc(it.kind)}</td>"
                    f"<td class=text>{esc(_clip(it.text))}<span class=sub>{esc(it.file)}</span></td>"
                    f"<td><div class=marks>{_chips(marks)}</div></td>"
                    f"<td><code>{esc(it.id)}</code></td></tr>")
    conf = []
    for c in res.conflicts:
        names = "<br>".join(f"{esc(x.label)}: “{esc(_clip(x.text, 120))}”" for x in c.items)
        conf.append(_row(c.status, "bad" if c.status == "unresolved" else "ok",
                         f"<div class=title><code>{esc(c.key)}</code></div><p class=quote>{names}</p>"
                         + (f"<p class=meta>{esc(c.reason)}</p>" if c.reason else "")))
    hint = context.contradiction_hint(res)
    body = ((f'<h2>Conflicts <span class=n>{len(conf)}</span></h2><ul class="panel list">{"".join(conf)}</ul>'
             if conf else "")
            + (f'<p class=mut>Hint shown to agents: {esc(hint)}</p>' if hint else "")
            + f'<h2>Everything in force <span class=n>{len(rows)}</span></h2>'
              '<div class="panel scroll"><table><thead><tr><th>Scope</th><th>Kind</th><th>Text</th>'
              f'<th>Marks</th><th>Id</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    return layout("Team context", body, "/context", root=root, badges=_badges(d),
                  lede="Shared rules, the scope each one applies to, and anything that disagrees.")


def proposals_page(root: str) -> str:
    d = collect(root)
    if not d["proposals"] and not d["rejected"]:
        return layout("Proposals", _empty("inbox", "No proposals",
                      "Anyone can suggest a rule with <code>reverie context add</code>, or turn one of "
                      "their own into a proposal with <code>reverie context propose</code>."),
                      "/proposals", root=root, badges=_badges(d))
    out = []
    if d["proposals"]:
        rows = []
        for p in d["proposals"]:
            extra = " · ".join(f"{k}: {p[k]}" for k in ("from", "first-stated", "usage") if p.get(k))
            rows.append(_row("Waiting", "acc",
                f"<p class='quote title'>“{esc(context.bullet_text(p['bullet']))}”</p>"
                f"<p class=meta>By {esc(p.get('proposed-by', '?'))} on {esc(p.get('date', '?'))} for "
                f"{esc(p.get('document', '?'))}" + (f" · why: {esc(p['why'])}" if p.get("why") else "")
                + (f" · {esc(extra)}" if extra else "") + "</p>"
                + _cmd(f"reverie context accept {esc(p['slug'])}", label="An owner decides")))
        out.append(f'<h2>Waiting <span class=n>{len(rows)}</span></h2><ul class="panel list">{"".join(rows)}</ul>')
    if d["rejected"]:
        rows = [_row("Turned down", "", f"<p class='quote title'>“{esc(context.bullet_text(r['bullet']))}”</p>"
                     f"<p class=meta>Rejected {esc(r.get('rejected-on', '?'))} by {esc(r.get('rejected-by', '?'))}: "
                     f"{esc(r.get('reason', ''))}</p>") for r in d["rejected"]]
        out.append(f'<h2>Turned down <span class=n>{len(rows)}</span></h2><ul class="panel list">{"".join(rows)}</ul>')
    return layout("Proposals", "".join(out), "/proposals", root=root, badges=_badges(d),
                  lede="Rules people suggested for the team. Turned-down ones keep their reason so the proposer can see it.")


def memory_page(root: str, page: int = 1) -> str:
    d = collect(root)
    mem = d["mem"]
    if not mem:
        return layout("Memory", _empty("layers", "Nothing remembered for this repo yet",
                      "Decisions and findings from agent sessions appear here once Reverie has captured some."),
                      "/memory", root=root, badges=_badges(d))
    pages = max(1, -(-len(mem) // PAGE_SIZE))
    page = min(max(page, 1), pages)
    rows = []
    for m in mem[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]:
        meta = m.get("meta") or {}
        use = ", ".join(f"{k.lower()}={v}" for k, v in sorted((meta.get("usage") or {}).items()))
        flags = [f for f, on in (("possibly outdated", deliver.is_stale_flagged(m)),
                                 ("challenged", bool(meta.get("challenged")))) if on]
        rows.append(f"<tr class=item><td><span class=badge>{esc(m.get('kind', ''))}</span></td>"
                    f"<td class=text>{esc(_clip(m.get('text', '')))}</td>"
                    f"<td>{esc(m.get('source') or m.get('origin') or '')}</td><td>{esc(use)}</td>"
                    f"<td><div class=marks>{_chips(flags)}</div></td>"
                    f"<td><code>{esc(str(m.get('id', ''))[:8])}</code></td></tr>")
    nav = ""
    if pages > 1:
        nav = (f'<div class=pager><span>Page {page} of {pages}</span>'
               + (f'<a href="/memory?page={page - 1}">Previous</a>' if page > 1 else "")
               + (f'<a href="/memory?page={page + 1}">Next</a>' if page < pages else "") + "</div>")
    body = (f'{nav}<div class="panel scroll"><table><thead><tr><th>Kind</th><th>Text</th><th>Source</th>'
            f'<th>How agents used it</th><th>Flags</th><th>Id</th></tr></thead><tbody>{"".join(rows)}</tbody>'
            f'</table></div>{nav}')
    return layout("Memory", body, "/memory", root=root, badges=_badges(d),
                  lede=f"{len(mem)} item(s). Open one with <code>reverie show &lt;id&gt;</code>.")


def repos_page(root: str) -> str:
    def go() -> list[dict]:
        conn = deliver.open_db()
        if conn is None:
            return []
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM repos").fetchall()]
        finally:
            conn.close()
    rows = _safe(go, [])
    if not rows:
        return layout("Repos", _empty("folder", "No repo is known yet",
                      "Use Reverie from your agent in a repo once, or run <code>reverie enable</code> there."),
                      "/repos", root=root)
    trs = "".join(f"<tr class=item><td class=text>{esc(r.get('path') or r.get('repo_key'))}</td>"
                  f"<td><span class='badge {'ok' if str(r.get('state')) == 'enabled' else ''}'>"
                  f"{esc(r.get('state'))}</span></td><td>{esc(r.get('url') or '')}</td></tr>" for r in rows)
    return layout("Repos", f'<div class="panel scroll"><table><thead><tr><th>Repo</th><th>State</th>'
                  f'<th>Remote</th></tr></thead><tbody>{trs}</tbody></table></div>',
                  "/repos", root=root, lede="Every repo Reverie knows about on this machine.")


ROUTES = {"/": home, "/start": start, "/context": context_page, "/proposals": proposals_page,
          "/repos": repos_page}


def render(root: str, path: str, query: dict[str, list[str]] | None = None) -> tuple[int, str]:
    if path == "/memory":
        if is_disabled(root):
            return 200, _disabled_page(path, root)
        try:
            page = int((query or {}).get("page", ["1"])[0])
        except ValueError:
            page = 1
        return 200, memory_page(root, page)
    fn = ROUTES.get(path)
    if fn is None:
        return 404, layout("Not found", '<p class=lede>No such page. <a href="/">Back to the overview</a></p>',
                           "", root=root)
    if path != "/repos" and is_disabled(root):
        return 200, _disabled_page(path, root)
    try:
        return 200, fn(root)
    except Exception:
        return 500, layout("Something went wrong",
                           "<p class=lede>This page could not be built. The CLI still works.</p>", "")


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------

def make_handler(root: str, allowed_hosts: set[str]):
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
            code, body = render(root, parts.path, parse_qs(parts.query))
            self._reply(code, body, head)

        def do_GET(self) -> None:
            self._serve(False)

        def do_HEAD(self) -> None:
            self._serve(True)

        def _no(self) -> None:
            self._reply(405, "read-only: only GET and HEAD are served", extra={"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _no

    return Handler


def make_server(root: str, port: int = 0) -> ThreadingHTTPServer:
    """Bind 127.0.0.1 (never another interface). Raises OSError if the port is taken."""
    server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    bound = server.server_address[1]
    server.RequestHandlerClass = make_handler(root, {f"127.0.0.1:{bound}", f"localhost:{bound}"})
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
    print_fn(f"Reverie (read-only) at {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
