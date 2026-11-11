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
* no telemetry of any kind: the server logs nothing and reports nothing.

Standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import context, deliver, redact, rules, spool

PAGE_SIZE = 100
CSP = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
MAX_BODY = 16 * 1024
WRITE_PATHS = ("/do/add", "/do/accept", "/do/reject")
LIMITS = {"text": 1000, "why": 300, "slug": 200, "token": 100, "p": 16, "kind": 16, "target": 120}

# Design: a team workspace. A slim sidebar of projects (the unit people think in), a top bar with
# the path you are on and search, a project header with tabs, and boards where the work is a set
# of things to triage (conflicts, outdated rules, proposals, memory by kind). One neutral family,
# one blue for the current tab, links and the primary action; amber/red/green only mean status.
CSS = """
:root{
--canvas:#eceef2;--bg:#fff;--side:#f6f7f9;--sunk:#f4f5f8;--line:#e6e8ee;--line2:#d3d7e0;
--fg:#12151c;--mut:#566070;--faint:#7a8494;
--accent:#1f5bff;--accent-d:#1747cf;--accent-bg:#e9efff;--accent-fg:#1a43b8;
--warn-bg:#fff4dc;--warn-fg:#8a4a00;--bad-bg:#fdeceb;--bad-fg:#a1231f;--ok-bg:#e6f5ec;--ok-fg:#17603a;
--ring:rgba(31,91,255,.35);--ease:cubic-bezier(.22,1,.36,1)}
@media (prefers-color-scheme:dark){:root{
--canvas:#08090c;--bg:#121419;--side:#0e1014;--sunk:#181b22;--line:#242933;--line2:#343b48;
--fg:#e9ebf0;--mut:#a2abb8;--faint:#7f8997;
--accent:#6f98ff;--accent-d:#8aaaff;--accent-bg:#18264a;--accent-fg:#adc3ff;
--warn-bg:#2b2310;--warn-fg:#f0bd62;--bad-bg:#2d1818;--bad-fg:#f49a95;--ok-bg:#12261a;--ok-fg:#7fd1a1;
--ring:rgba(111,152,255,.45)}}
*{box-sizing:border-box}
html{color-scheme:light dark}
body{margin:0;background:var(--canvas);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
::selection{background:var(--accent-bg);color:var(--accent-fg)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:8px}
*{scrollbar-width:thin;scrollbar-color:var(--line2) transparent}
a{color:inherit;text-decoration:none}
p a,.lede a,.note a{color:var(--accent);text-decoration:underline;text-underline-offset:3px}
code{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--sunk);
border:1px solid var(--line);border-radius:6px;padding:1px 6px;overflow-wrap:anywhere}
pre{font:13px/1.65 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;margin:0;padding:20px 22px;
white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;max-height:68vh}
svg{flex:none}

/* frame: the whole app sits in one inset window on the canvas */
.app{display:grid;grid-template-columns:264px minmax(0,1fr);min-height:100vh;max-width:1560px;margin:0 auto;
background:var(--bg)}
@media (min-width:1200px){body{padding:18px}.app{min-height:calc(100vh - 36px);border-radius:18px;overflow:hidden;
border:1px solid var(--line)}}
.side{background:var(--side);border-right:1px solid var(--line);padding:16px 12px;display:flex;flex-direction:column;
gap:6px;position:sticky;top:0;height:100vh;overflow:auto;align-self:start}
@media (min-width:1200px){.side{height:calc(100vh - 36px)}}
.brand{display:flex;align-items:center;gap:10px;padding:4px 8px 14px;font-weight:650;font-size:16px;letter-spacing:-.01em}
.logo{width:28px;height:28px;border-radius:8px;background:var(--fg);color:var(--bg);display:grid;place-items:center}
.sec{display:flex;align-items:center;justify-content:space-between;padding:14px 10px 6px;font-size:12px;
font-weight:600;color:var(--faint)}
.sec a{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;color:var(--mut);
transition:background-color .15s var(--ease)}
.sec a:hover{background:var(--line)}
.item{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:9px;color:var(--mut);font-weight:520;
transition:background-color .15s var(--ease),color .15s var(--ease);min-width:0}
.item span.t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item:hover{background:var(--sunk);color:var(--fg)}
.item[aria-current=page]{background:var(--line);color:var(--fg)}
.tile{width:24px;height:24px;border-radius:7px;display:grid;place-items:center;font-size:12px;font-weight:700;color:#fff;flex:none}
.tile.lg{width:44px;height:44px;border-radius:12px;font-size:19px}
.t0{background:#2f6bff}.t1{background:#16a085}.t2{background:#d9480f}.t3{background:#7c3aed}.t4{background:#0b7285}.t5{background:#c2255c}
.dot{margin-left:auto;min-width:19px;height:19px;border-radius:10px;padding:0 6px;font-size:11.5px;font-weight:650;
display:grid;place-items:center;background:var(--warn-bg);color:var(--warn-fg)}
.foot{margin-top:auto;padding:12px 10px 4px;color:var(--faint);font-size:12.5px;display:flex;gap:8px}

/* top bar */
.col{min-width:0;display:flex;flex-direction:column}
.top{display:flex;align-items:center;gap:16px;padding:12px 28px;border-bottom:1px solid var(--line);min-height:62px}
.crumbs{display:flex;align-items:center;gap:8px;color:var(--faint);min-width:0;white-space:nowrap;overflow:hidden}
.crumbs b{color:var(--fg);font-weight:600;overflow:hidden;text-overflow:ellipsis}
.crumbs a:hover{color:var(--fg)}
.search{margin-left:auto;display:flex;align-items:center;gap:8px;width:min(340px,40vw);height:38px;padding:0 12px;
border:1px solid var(--line);border-radius:999px;background:var(--bg);color:var(--faint);
transition:border-color .15s var(--ease),box-shadow .15s var(--ease)}
.search:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
.search input{all:unset;flex:1;min-width:0;color:var(--fg);font-size:14px}
.search input::placeholder{color:var(--faint)}
.ro{display:flex;align-items:center;gap:6px;height:30px;padding:0 11px;border-radius:999px;background:var(--sunk);
color:var(--mut);font-size:12.5px;font-weight:550;white-space:nowrap}

/* page head */
main{padding:28px 28px 64px;min-width:0}
.head{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.head .who{display:flex;gap:14px;align-items:center;min-width:0;flex:1 1 20rem}
.head .acts{display:flex;gap:10px;flex-wrap:wrap;margin-left:auto}
h1{font-size:1.7rem;line-height:1.2;letter-spacing:-.022em;margin:0;font-weight:670;text-wrap:balance}
.sub{color:var(--mut);margin:4px 0 0;max-width:68ch;overflow-wrap:anywhere}
.btn{display:inline-flex;align-items:center;gap:8px;height:38px;padding:0 15px;border-radius:999px;font-weight:580;
font-size:14px;border:1px solid var(--line2);background:var(--bg);color:var(--fg);white-space:nowrap;
transition:background-color .15s var(--ease),border-color .15s var(--ease)}
.btn:hover{background:var(--sunk);border-color:var(--faint)}
.btn.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.pri:hover{background:var(--accent-d);border-color:var(--accent-d)}
.tabs{display:flex;gap:4px;margin:22px 0 0;border-bottom:1px solid var(--line);overflow-x:auto}
.tabs a{display:flex;align-items:center;gap:8px;padding:10px 14px 12px;color:var(--mut);font-weight:550;white-space:nowrap;
border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .15s var(--ease)}
.tabs a:hover{color:var(--fg)}
.tabs a[aria-current=page]{color:var(--fg);border-bottom-color:var(--accent)}
.tabs a[aria-current=page] svg{color:var(--accent)}
.tabs .n{font-size:12px;font-weight:650;min-width:20px;height:20px;border-radius:10px;padding:0 6px;display:grid;
place-items:center;background:var(--sunk);color:var(--mut)}
.tabs .n.hot{background:var(--warn-bg);color:var(--warn-fg)}

/* boards */
.board{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(272px,1fr);gap:18px;margin-top:26px;overflow-x:auto;
padding-bottom:6px;align-items:start}
.lane{min-width:0}
.lane>h2{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:620;margin:0 0 12px;padding:0 2px}
.lane>h2 .c{min-width:22px;height:22px;border-radius:11px;padding:0 7px;display:grid;place-items:center;
font-size:12px;font-weight:650;background:var(--fg);color:var(--bg)}
.lane>h2 .c.w{background:var(--warn-bg);color:var(--warn-fg)}.lane>h2 .c.b{background:var(--bad-bg);color:var(--bad-fg)}
.lane>h2 .c.o{background:var(--ok-bg);color:var(--ok-fg)}.lane>h2 .c.a{background:var(--accent-bg);color:var(--accent-fg)}
.stack{display:flex;flex-direction:column;gap:12px}
.card{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:16px;min-width:0;overflow-wrap:anywhere;
transition:border-color .15s var(--ease),box-shadow .15s var(--ease)}
a.card:hover,.card:hover{border-color:var(--line2);box-shadow:0 4px 14px rgba(16,24,40,.07)}
.card h3{margin:0;font-size:15px;font-weight:620;line-height:1.35}
.card p{margin:6px 0 0;color:var(--mut);font-size:13.5px}
.card .q{color:var(--fg);font-size:14px}
.rule{height:1px;background:var(--line);margin:14px 0 12px;border:0}
.meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:var(--faint);font-size:12.5px}
.meta .sp{margin-left:auto}
.who1{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--mut);min-width:0}
.av{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:700;color:#fff;flex:none}
.chip{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 9px;border-radius:999px;font-size:12px;font-weight:600;
background:var(--sunk);color:var(--mut);white-space:nowrap}
.chip.warn{background:var(--warn-bg);color:var(--warn-fg)}.chip.bad{background:var(--bad-bg);color:var(--bad-fg)}
.chip.ok{background:var(--ok-bg);color:var(--ok-fg)}.chip.acc{background:var(--accent-bg);color:var(--accent-fg)}
details{margin-top:12px}
summary{cursor:pointer;color:var(--accent);font-weight:580;font-size:13px;list-style:none;display:inline-flex;gap:6px;
align-items:center;border-radius:6px}
summary::-webkit-details-marker{display:none}
summary::before{content:"";width:6px;height:6px;border-right:1.75px solid currentColor;border-bottom:1.75px solid currentColor;
transform:rotate(-45deg);transition:transform .15s var(--ease);margin-right:2px}
details[open] summary::before{transform:rotate(45deg)}
.cmd{display:block;margin-top:8px;padding:10px 12px;background:var(--sunk);border:1px solid var(--line);border-radius:10px;
font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere;user-select:all}
.cmd.big{font-size:13.5px;padding:13px 15px}
.empty{margin-top:26px;border:1px dashed var(--line2);border-radius:16px;padding:40px 24px;text-align:center;
display:flex;flex-direction:column;align-items:center;gap:8px}
.empty .ico{width:44px;height:44px;border-radius:12px;background:var(--sunk);display:grid;place-items:center;color:var(--faint)}
.empty b{font-size:1.05rem}.empty p{margin:0;color:var(--mut);max-width:50ch}
.lane .empty{margin:0;padding:22px 14px;border-radius:14px;font-size:13.5px;color:var(--faint)}

/* tables and docs */
.panel{margin-top:26px;border:1px solid var(--line);border-radius:14px;overflow:hidden;background:var(--bg)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-size:12px;font-weight:600;color:var(--faint);background:var(--side);padding:10px 16px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:14px 16px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:var(--side)}
td .s{display:block;color:var(--faint);font-size:12px;margin-top:2px}
.marks{display:flex;flex-wrap:wrap;gap:6px}
.pager{display:flex;align-items:center;gap:10px;color:var(--mut);margin:18px 0 0}
.pager a{padding:6px 14px;border:1px solid var(--line2);border-radius:999px;font-weight:580;color:var(--fg)}
.pager a:hover{background:var(--sunk)}
.steps{counter-reset:s;display:grid;gap:14px;margin:26px 0 0;padding:0;list-style:none;max-width:46rem}
.steps>li{counter-increment:s;display:grid;grid-template-columns:30px minmax(0,1fr);gap:4px 14px}
.steps>li::before{content:counter(s);width:28px;height:28px;border-radius:50%;background:var(--accent-bg);
color:var(--accent-fg);font-weight:700;font-size:13px;display:grid;place-items:center;grid-row:span 3}
.steps b{font-weight:620;align-self:center}.steps p{margin:0;color:var(--mut);font-size:14px}
.note{margin-top:44px;color:var(--faint);font-size:12.5px}
.hint{margin-top:18px;color:var(--mut);font-size:13.5px}
.statline{display:flex;gap:22px;flex-wrap:wrap;margin-top:22px;color:var(--mut);font-size:13.5px}
.statline b{color:var(--fg);font-size:15px;margin-right:5px}

/* forms and flash */
.flash{margin:20px 0 0;padding:12px 16px;border-radius:12px;font-weight:550;overflow-wrap:anywhere}
.flash.ok{background:var(--ok-bg);color:var(--ok-fg)}.flash.bad{background:var(--bad-bg);color:var(--bad-fg)}
form.f{display:grid;gap:12px;margin-top:12px}
form.f label{display:grid;gap:5px;font-size:13px;font-weight:600;color:var(--mut)}
form.f textarea,form.f input[type=text],form.f select{font:inherit;font-size:14px;font-weight:400;color:var(--fg);
background:var(--bg);border:1px solid var(--line2);border-radius:10px;padding:9px 11px;width:100%}
form.f textarea{min-height:84px;resize:vertical}
form.f textarea:focus,form.f input:focus,form.f select:focus{outline:2px solid var(--accent);outline-offset:1px}
.btn.bad{border-color:var(--bad-fg);color:var(--bad-fg);background:var(--bg)}
button.btn{cursor:pointer;font-family:inherit}
.who{color:var(--mut);font-size:13px}
.acts2{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
.acts2 form.f{margin:0;flex:1 1 220px}
@media (max-width:900px){
.app{grid-template-columns:minmax(0,1fr)}
.side{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);flex-direction:row;overflow-x:auto;
align-items:center;padding:10px 12px;gap:4px}
.brand{padding:0 8px 0 4px}.sec,.foot{display:none}
.item{white-space:nowrap}
.top{padding:10px 16px}.search{display:none}
main{padding:20px 16px 48px}.board{grid-auto-flow:row;grid-auto-columns:minmax(0,1fr)}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

# icons: authored, 24px grid, 1.75 stroke, one weight everywhere
_ICONS = {
    "grid": '<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/>'
            '<rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
    "users": '<circle cx="9" cy="8.5" r="3"/><path d="M3.5 19c.4-3 2.6-4.8 5.5-4.8s5.1 1.8 5.5 4.8"/>'
             '<path d="M15.5 5.8a3 3 0 0 1 0 5.4"/><path d="M17.5 14.6c1.8.6 2.7 2 3 4.4"/>',
    "inbox": '<path d="M3.5 13.5 6 6h12l2.5 7.5"/><path d="M3.5 13.5V18a1 1 0 0 0 1 1h15a1 1 0 0 0 1-1v-4.5H15a3 3 0 0 1-6 0Z"/>',
    "layers": '<path d="m12 4 8.5 4.5L12 13 3.5 8.5 12 4Z"/><path d="m3.5 12.5 8.5 4.5 8.5-4.5"/><path d="m3.5 16.5 8.5 4.5 8.5-4.5"/>',
    "list": '<path d="M9 7h11M9 12h11M9 17h11"/><path d="M4.5 7h.01M4.5 12h.01M4.5 17h.01"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12.3 2.8 2.8L16.2 9.5"/>',
    "alert": '<path d="M12 4 21 19.5H3L12 4Z"/><path d="M12 10v4.2"/><path d="M12 16.9v.1"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="9" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
    "off": '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6 18.4 18.4"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
    "plug": '<path d="M9 4v5M15 4v5"/><path d="M6.5 9h11v3a5.5 5.5 0 0 1-11 0V9Z"/><path d="M12 17.5V21"/>',
    "folder": '<path d="M3.5 7.5a1.5 1.5 0 0 1 1.5-1.5h4l2 2.2h8a1.5 1.5 0 0 1 1.5 1.5v7.8a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5V7.5Z"/>',
    "chev": '<path d="m9 6 6 6-6 6"/>',
    "logo": '<path d="M5 17.5V6.5l7 3.5 7-3.5v11l-7-3.5-7 3.5Z"/>',
}


def icon(name: str, size: int = 18) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f'{_ICONS[name]}</svg>')


def esc(value: object, *, redacted: bool = True) -> str:
    text = redact.redact(str(value)) if redacted else str(value)
    return html.escape(text, quote=True)


def _clip(text: str, n: int = 300) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= n else one[: n - 1] + "…"


def _hue(text: str) -> int:
    return hashlib.sha1(text.encode()).digest()[0] % 6


def _avatar(who: str) -> str:
    name = (who.split("<")[0].strip() or who.strip() or "?")
    return f'<span class="av t{_hue(name)}" title="{esc(who)}">{esc(name[:1].upper())}</span>'


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


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------

def _doc(title: str, side_proj: dict | None, plist: list[dict], active: str, crumbs: list[str],
         inner: str) -> str:
    projs = "".join(
        f'<a class=item href="{_u("/project", p)}"{" aria-current=page" if (side_proj and p["id"] == side_proj["id"]) else ""}>'
        f'<span class="tile t{_hue(p["name"])}">{esc(p["name"][:1].upper())}</span>'
        f'<span class=t>{esc(p["name"])}</span></a>' for p in plist[:40])
    pid = f'<input type=hidden name=p value="{side_proj["id"]}">' if side_proj else ""
    cr = " ".join(crumbs)
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} · Reverie</title><style>{CSS}</style></head><body><div class=app>"
        f"<aside class=side><div class=brand><span class=logo>{icon('logo', 16)}</span>Reverie</div>"
        f'<a class=item href="/"{" aria-current=page" if active == "/" else ""}>{icon("grid")}<span class=t>All projects</span></a>'
        f'<a class=item href="/new"{" aria-current=page" if active == "/new" else ""}>{icon("plus")}<span class=t>Add a project</span></a>'
        f'<div class=sec><span>Projects</span></div>{projs}'
        f"<div class=foot>{icon('lock', 15)}<span>Local only. It writes only when you press a button, with the CLI's own rules.</span></div></aside>"
        f'<div class=col><div class=top><div class=crumbs>{cr}</div>'
        f'<form class=search action="/search" method=get role=search>{icon("search", 16)}{pid}'
        f'<input name=q type=search placeholder="Search rules and memory" aria-label="Search rules and memory" '
        f'maxlength=120></form></div><main>{inner}'
        f'<p class=note>Everything here is also a command; the page calls the same functions and refuses what the CLI refuses.</p></main></div>'
        f"</div></body></html>")


def _crumb_link(label: str, href: str) -> str:
    return f'<a href="{href}">{esc(label)}</a><span>/</span>'


def shell(title: str, sub: str, body: str, active: str, *, plist: list[dict], actions: str = "") -> str:
    head = (f'<div class=head><div class=who><div><h1>{esc(title)}</h1>'
            + (f'<p class=sub>{sub}</p>' if sub else "") + f'</div></div><div class=acts>{actions}</div></div>')
    return _doc(title, None, plist, active, [f"<b>{esc(title)}</b>"], head + body)


TABS = (("/project", "Overview", "grid"), ("/context", "Rules", "users"), ("/memory", "Memory", "layers"),
        ("/start", "What agents are told", "eye"), ("/proposals", "Proposals", "inbox"),
        ("/connect", "Connect", "plug"))


def frame(proj: dict, plist: list[dict], d: dict | None, tab: str, body: str, *, title: str = "") -> str:
    d = d or {}
    n_att = _attention(d) if d else 0
    counts = {"/project": (n_att, True), "/proposals": (len(d.get("proposals", [])), True),
              "/context": (len(d.get("team_rules", [])), False), "/memory": (len(d.get("mem", [])), False)}
    tabs = ""
    for path, label, ico in TABS:
        n, hot = counts.get(path, (0, False))
        badge = f'<span class="n{" hot" if hot and n else ""}">{n}</span>' if n else ""
        cur = ' aria-current="page"' if path == tab else ""
        tabs += f'<a href="{_u(path, proj)}"{cur}>{icon(ico, 16)}{esc(label)}{badge}</a>'
    head = (f'<div class=head><div class=who><span class="tile lg t{_hue(proj["name"])}">'
            f'{esc(proj["name"][:1].upper())}</span><div><h1>{esc(proj["name"])}</h1>'
            f'<p class=sub>{esc(proj["path"])}</p></div></div><div class=acts>'
            f'<a class=btn href="{_u("/connect", proj)}">{icon("plug", 16)}Connect an agent</a>'
            f'<a class="btn pri" href="{_u("/add", proj)}">{icon("plus", 16)}Add a rule</a></div></div>'
            f'<nav class=tabs aria-label="Project">{tabs}</nav>')
    label = next((lab for p, lab, _ in TABS if p == tab), title)
    crumbs = [_crumb_link("Projects", "/"), _crumb_link(proj["name"], _u("/project", proj)),
              f"<b>{esc(title or label)}</b>"]
    return _doc(f'{proj["name"]} · {title or label}', proj, plist, "", crumbs, head + body)


def _empty(ico: str, title: str, text: str) -> str:
    return (f'<div class=empty><span class=ico>{icon(ico, 20)}</span><b>{esc(title)}</b><p>{text}</p></div>')


def _cmd(*commands: str, label: str = "How to settle it") -> str:
    lines = "".join(f"<code class=cmd>{c}</code>" for c in commands)
    return f"<details><summary>{esc(label)}</summary>{lines}</details>"


def _owner(item) -> str:
    return _safe(lambda: context.owner_text(item), "")


def _lane(title: str, tone: str, cards: list[str], empty: str = "") -> str:
    inner = "".join(cards) if cards else f'<div class=empty>{esc(empty)}</div>'
    return f'<section class=lane><h2>{esc(title)}<span class="c {tone}">{len(cards)}</span></h2><div class=stack>{inner}</div></section>'


def _disabled(proj: dict, plist: list[dict], tab: str) -> str:
    return frame(proj, plist, None, tab, _empty(
        "off", "Reverie is turned off for this repo",
        "Nothing is delivered to agents and nothing is shown here. Turn it back on with "
        "<code>reverie enable</code>."), title="Turned off")


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

def projects_page(root: str, query=None) -> str:
    _, _, plist = _target(root, query)
    lanes: dict[str, list[str]] = {"attn": [], "ok": [], "off": []}
    for p in plist:
        off = is_disabled(p["path"])
        d = None if off else collect(p["path"])
        link = _u("/project", p)
        head = (f'<div style="display:flex;gap:12px;align-items:center"><span class="tile lg t{_hue(p["name"])}">'
                f'{esc(p["name"][:1].upper())}</span><div style="min-width:0"><h3>{esc(p["name"])}</h3>'
                f'<p style="margin:2px 0 0">{esc(p["path"])}</p></div></div>')
        if off:
            lanes["off"].append(f'<a class=card href="{link}">{head}<hr class=rule><div class=meta>'
                                f'{icon("off", 15)}Reverie is turned off for this repo</div></a>')
            continue
        assert d is not None
        n = _attention(d)
        chips = []
        if d["unresolved"]:
            chips.append(f'<span class="chip bad">{len(d["unresolved"])} conflict{"s" if len(d["unresolved"]) != 1 else ""}</span>')
        out = len(d["stale_team"]) + len(d["stale_mem"])
        if out:
            chips.append(f'<span class="chip warn">{out} outdated</span>')
        if d["proposals"]:
            chips.append(f'<span class="chip acc">{len(d["proposals"])} proposal{"s" if len(d["proposals"]) != 1 else ""}</span>')
        in_force = len(d["team_rules"]) + len(d["personal"]) + len(d["mem_rules"])
        foot = (f'<hr class=rule><div class=meta><span>{in_force} rules in force</span><span>·</span>'
                f'<span>{len(d["mem"])} remembered</span></div>')
        card = (f'<a class=card href="{link}">{head}'
                + (f'<div class=marks style="margin-top:12px">{"".join(chips)}</div>' if chips else "")
                + foot + "</a>")
        lanes["attn" if n else "ok"].append(card)
    board = (_lane("Needs attention", "w", lanes["attn"], "Nothing waiting on anyone.")
             + _lane("All clear", "o", lanes["ok"], "No project is fully clear yet.")
             + (_lane("Turned off", "", lanes["off"]) if lanes["off"] else ""))
    return shell("Projects", "Every repo Reverie knows on this machine, sorted by what needs a person.",
                 f'<div class=board>{board}</div>', "/", plist=plist,
                 actions=f'<a class="btn pri" href="/new">{icon("plus", 16)}Add a project</a>')


def overview(root: str, proj: dict, plist: list[dict]) -> str:
    d = collect(root)
    in_force = len(d["team_rules"]) + len(d["personal"]) + len(d["mem_rules"])
    outdated = len(d["stale_team"]) + len(d["stale_mem"])
    statline = "".join(f"<span><b>{n}</b>{esc(label)}</span>" for label, n in (
        ("Rules in force", in_force), ("Conflicts to settle", len(d["unresolved"])),
        ("Possibly outdated", outdated), ("Proposals waiting", len(d["proposals"]))))
    conflicts, stale, props = [], [], []
    for c in d["unresolved"]:
        a, b = c.overridden[0], c.overridden[1]
        who = "; ".join(x for x in (_owner(a), _owner(b)) if x)
        conflicts.append(
            f'<article class=card><span class="chip bad">Conflict</span>'
            f"<h3 style='margin-top:10px'>Two rules disagree</h3>"
            f"<p class=q>{esc(a.label)}: “{esc(_clip(a.text, 160))}”</p>"
            f"<p class=q>{esc(b.label)}: “{esc(_clip(b.text, 160))}”</p>"
            f"<p>Agents are told to ask before acting on either.</p>"
            + (f"<hr class=rule><div class=meta>Ask {esc(who)}</div>" if who else "")
            + _cmd(f'reverie context resolve {esc(c.key)} "your decision"') + "</article>")
    for it in d["stale_team"]:
        st = d["res"].stale.get(it.id, {})
        stale.append(
            f'<article class=card><span class="chip warn">Possibly outdated</span>'
            f"<h3 style='margin-top:10px'>“{esc(_clip(it.text, 200))}”</h3>"
            f"<p>{esc(it.label)}. A later change to the code may contradict it: "
            f"{esc(_clip(str(st.get('reason', '')), 200))}</p>"
            + _cmd(f"Check the code, then edit or remove the rule in {esc(it.file)}", label="What to do")
            + "</article>")
    for m in d["stale_mem"]:
        stale.append(
            f'<article class=card><span class="chip warn">Possibly outdated</span>'
            f"<h3 style='margin-top:10px'>“{esc(_clip(m['text'], 200))}”</h3>"
            f"<p>Remembered from an earlier session.</p>" + _cmd("reverie stale list", label="What to do")
            + "</article>")
    for p in d["proposals"]:
        by = str(p.get("proposed-by", "someone"))
        props.append(
            f'<article class=card><span class="chip acc">Proposal</span>'
            f"<h3 style='margin-top:10px'>A proposal is waiting</h3>"
            f"<p class=q>“{esc(_clip(context.bullet_text(p['bullet']), 200))}”</p>"
            + (f"<p>Because: {esc(_clip(p.get('why', ''), 160))}</p>" if p.get("why") else "")
            + f"<hr class=rule><div class=meta><span class=who1>{_avatar(by)}{esc(by)}</span></div>"
            + _cmd(f"reverie context accept {esc(p['slug'])}",
                   f'reverie context reject {esc(p["slug"])} --why "…"', label="An owner decides")
            + "</article>")
    if conflicts or stale or props:
        body = (f'<div class=statline>{statline}</div><h2 style="font-size:1rem;margin:30px 0 0">Needs your action</h2>'
                f'<div class=board style="margin-top:14px">'
                + _lane("Conflicts", "b", conflicts, "No conflicts")
                + _lane("Possibly outdated", "w", stale, "Nothing outdated")
                + _lane("Proposals", "a", props, "No proposals") + "</div>")
    else:
        body = (f'<div class=statline>{statline}</div>'
                + _empty("check", "Nothing needs your action right now.",
                         "Conflicts, outdated rules and waiting proposals will show up here first."))
    return frame(proj, plist, d, "/project", body)


def start(root: str, proj: dict, plist: list[dict]) -> str:
    d = collect(root)
    text = _safe(lambda: deliver.startup_text(root, log=False), "")
    intro = ("<p class=hint>This is exactly what an agent is handed when a session starts in this repo. "
             "Viewing it here is not a delivery and is not counted as one.</p>")
    if text:
        body = (intro + f'<div class=panel><pre id=start>{esc(text, redacted=False)}</pre></div>'
                f'<p class=hint>{len(text)} characters.</p>')
    else:
        body = intro + _empty("eye", "Nothing.", "No rules or memory apply here yet, so an agent starts with a clean slate.")
    return frame(proj, plist, d, "/start", body)


def _chips(marks: list[str]) -> str:
    return "".join(f'<span class="chip {"bad" if m == "conflict" else "warn" if "outdated" in m else ""}">'
                   f'{esc(m)}</span>' for m in marks)


def context_page(root: str, proj: dict, plist: list[dict]) -> str:
    d = collect(root)
    res = d["res"]
    if not res.items:
        return frame(proj, plist, d, "/context", _empty(
            "users", "No team context applies here",
            "Put markdown in <code>.reverie/context/</code> (see <code>reverie context</code>) "
            "to share rules with a team."))
    rows = []
    lost = {o.id for c in res.conflicts for o in c.overridden}
    for it in res.items:
        marks = [m for m, on in (("locked", it.locked), ("conflict", it.id in lost),
                                 ("possibly outdated", it.id in res.stale),
                                 ("also a shared rule: hidden here", it.id in res.hidden),
                                 (f"only under {', '.join(it.paths)}", bool(it.paths))) if on]
        rows.append(f"<tr><td><span class=chip>{esc(it.label)}</span></td><td>{esc(it.kind)}</td>"
                    f"<td>{esc(_clip(it.text))}<span class=s>{esc(it.file)}</span></td>"
                    f"<td><div class=marks>{_chips(marks)}</div></td><td><code>{esc(it.id)}</code></td></tr>")
    conf = []
    for c in res.conflicts:
        names = "".join(f"<p class=q>{esc(x.label)}: “{esc(_clip(x.text, 120))}”</p>" for x in c.items)
        conf.append(f'<article class=card><span class="chip {"bad" if c.status == "unresolved" else "ok"}">'
                    f'{esc(c.status)}</span><h3 style="margin-top:10px"><code>{esc(c.key)}</code></h3>{names}'
                    + (f"<p>{esc(c.reason)}</p>" if c.reason else "") + "</article>")
    hint = context.contradiction_hint(res)
    body = (('<div class=board style="grid-auto-columns:minmax(272px,420px)">'
             + _lane("Conflicts", "b", conf) + "</div>") if conf else "")
    body += (f'<p class=hint>Hint shown to agents: {esc(hint)}</p>' if hint else "")
    body += ('<div class="panel scroll"><table><thead><tr><th>Applies to</th><th>Kind</th><th>Rule</th>'
             f'<th>Marks</th><th>Id</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    return frame(proj, plist, d, "/context", body)


def _decide_forms(proj: dict, st: State | None, p: dict, root: str) -> str:
    doc = p["path"].parent.parent / (p.get("document") or "rules.md")
    owners = _safe(lambda: context.owners_of(doc), [])
    mine = _safe(lambda: context.is_owner(doc), False)
    who = _safe(lambda: _who(doc.parent, root), "unknown")
    status = ('<span class="chip ok">You can decide this</span>' if mine else
              '<span class="chip bad">Not an owner: the CLI would refuse</span>')
    line = (f"<p class=who>You would act as <b>{esc(who)}</b> (git identity). Owners of {esc(doc.name)}: "
            f"{esc(', '.join(owners) or 'nobody (anyone may edit)')}. {status}</p>")
    slug = esc(p["slug"])
    return (line + '<div class=acts2>'
            + _form_open(proj, st, "/do/accept") + f'<input type=hidden name=slug value="{slug}">'
              '<button class="btn pri" type=submit>Accept</button></form>'
            + _form_open(proj, st, "/do/reject") + f'<input type=hidden name=slug value="{slug}">'
              f'<label>Reason for turning it down (the proposer will read it)'
              f'<input type=text name=why maxlength={LIMITS["why"]} required></label>'
              '<button class="btn bad" type=submit>Reject</button></form></div>')


def proposals_page(root: str, proj: dict, plist: list[dict], st: State | None = None,
                   flash: dict | None = None) -> str:
    d = collect(root)
    banner = _banner(flash)
    if not d["proposals"] and not d["rejected"]:
        return frame(proj, plist, d, "/proposals", banner + _empty(
            "inbox", "No proposals",
            "Anyone can suggest a rule with <code>reverie context add</code>, or turn one of "
            "their own into a proposal with <code>reverie context propose</code>."))
    wait = []
    for p in d["proposals"]:
        by = str(p.get("proposed-by", "?"))
        extra = " · ".join(f"{k}: {p[k]}" for k in ("from", "first-stated", "usage") if p.get(k))
        wait.append(
            f"<article class=card><p class=q style='margin:0'>“{esc(context.bullet_text(p['bullet']))}”</p>"
            + (f"<p>Why: {esc(p['why'])}</p>" if p.get("why") else "")
            + (f"<p>{esc(extra)}</p>" if extra else "")
            + f"<hr class=rule><div class=meta><span class=who1>{_avatar(by)}{esc(by)}</span>"
              f"<span class=sp>{esc(p.get('date', '?'))}</span></div>"
            + f"<div class=meta style='margin-top:8px'>for {esc(p.get('document', '?'))}</div>"
            + _decide_forms(proj, st, p, root) + "</article>")
    gone = []
    for r in d["rejected"]:
        by = str(r.get("rejected-by", "?"))
        gone.append(
            f"<article class=card><p class=q style='margin:0'>“{esc(context.bullet_text(r['bullet']))}”</p>"
            f"<p>{esc(r.get('reason', ''))}</p><hr class=rule><div class=meta>"
            f"<span class=who1>{_avatar(by)}{esc(by)}</span><span class=sp>{esc(r.get('rejected-on', '?'))}</span></div>"
            "</article>")
    body = (banner + '<p class=hint>Rules people suggested for the team. Only an owner of the document can '
            'accept or turn one down; turned-down ones keep their reason, so the proposer can see it.</p>'
            '<div class=board>'
            + _lane("Waiting", "a", wait, "Nothing waiting") + _lane("Turned down", "", gone, "Nothing turned down")
            + "</div>")
    return frame(proj, plist, d, "/proposals", body)


_KIND_ORDER = ("rule", "preference", "decision", "finding")


def _mcard(m: dict) -> str:
    meta = m.get("meta") or {}
    use = ", ".join(f"{k.lower()}={v}" for k, v in sorted((meta.get("usage") or {}).items()))
    flags = [f for f, on in (("possibly outdated", deliver.is_stale_flagged(m)),
                             ("challenged", bool(meta.get("challenged")))) if on]
    src = str(m.get("source") or m.get("origin") or "")
    return (f'<article class=card><p class=q style="margin:0">{esc(_clip(m.get("text", "")))}</p>'
            + (f'<div class=marks style="margin-top:10px">{_chips(flags)}</div>' if flags else "")
            + f'<hr class=rule><div class=meta><span class=chip>{esc(src or "unknown")}</span>'
              + (f'<span>{esc(use)}</span>' if use else "")
              + f'<span class=sp><code>{esc(str(m.get("id", ""))[:8])}</code></span></div></article>')


def memory_page(root: str, proj: dict, plist: list[dict], page: int = 1) -> str:
    d = collect(root)
    mem = d["mem"]
    if not mem:
        return frame(proj, plist, d, "/memory", _empty(
            "layers", "Nothing remembered for this repo yet",
            "Decisions and findings from agent sessions appear here once Reverie has captured some."))
    pages = max(1, -(-len(mem) // PAGE_SIZE))
    page = min(max(page, 1), pages)
    chunk = mem[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    kinds = [k for k in _KIND_ORDER if any(m.get("kind") == k for m in chunk)]
    kinds += sorted({str(m.get("kind")) for m in chunk} - set(kinds))
    lanes = "".join(_lane({"rule": "Rules", "preference": "Preferences", "decision": "Decisions",
                           "finding": "Findings"}.get(k, k.title()), "",
                          [_mcard(m) for m in chunk if str(m.get("kind")) == k]) for k in kinds)
    nav = ""
    if pages > 1:
        nav = (f'<div class=pager><span>Page {page} of {pages}</span>'
               + (f'<a href="{_u("/memory", proj, page=str(page - 1))}">Previous</a>' if page > 1 else "")
               + (f'<a href="{_u("/memory", proj, page=str(page + 1))}">Next</a>' if page < pages else "") + "</div>")
    body = (f'<p class=hint>{len(mem)} item(s) remembered on this machine for this repo. '
            f'Open one with <code>reverie show &lt;id&gt;</code>.</p><div class=board>{lanes}</div>{nav}')
    return frame(proj, plist, d, "/memory", body)


def connect_page(root: str, proj: dict, plist: list[dict]) -> str:
    off = is_disabled(root)
    state = ('<span class="chip warn">Reverie is turned off for this repo</span>' if off
             else '<span class="chip ok">Reverie is on for this repo</span>')
    p = esc(proj["path"])
    body = (f'<p class=hint>{state}</p><ol class=steps>'
            f'<li><b>Set up your agent once per machine</b><p>This adds Reverie to the agents you use '
            f'(Claude Code, Codex and others it finds).</p><code class="cmd big">reverie install</code></li>'
            f'<li><b>Turn it on for this project</b><p>Reverie is on by default. Use this to turn it back on '
            f'after <code>reverie disable</code>.</p><code class="cmd big">cd {p} &amp;&amp; reverie enable</code></li>'
            f'<li><b>Start a session and check</b><p>Open your agent in the project. What it receives is on '
            f'the “What agents are told” tab.</p><code class="cmd big">reverie status</code></li>'
            f'<li><b>Share rules with your team</b><p>Point the repo at a shared folder of markdown rules in '
            f'<code>.reverie/context.toml</code>; everyone who opens the project gets them.</p>'
            f'<code class="cmd big">reverie context</code></li></ol>')
    return frame(proj, plist, None, "/connect", body, title="Connect an agent")


def add_page(root: str, proj: dict, plist: list[dict], st: State | None = None,
             flash: dict | None = None) -> str:
    def way(title: str, text: str, cmd: str) -> str:
        return (f'<article class=card><h3>{esc(title)}</h3><p>{text}</p><code class="cmd big">{cmd}</code></article>')
    flash = flash or {}
    opts = "".join(f'<option value="{esc(v)}">{esc(lab)}</option>' for v, lab in _targets(root))
    ids = [f"{lab}: {_who(Path(f), root)}" for lab, f in
           [(f"{lay} {context.scope_name(fo)}", fo) for lay, fos in context.sources(root).items() for fo in fos]]
    who = (f"<p class=who>You would act as <b>{esc(_safe(lambda: _who(Path(root) / '.reverie' / 'context', root), '?'))}</b> "
           f"(this repo's git identity). If a CODEOWNERS entry covers the document and you are not an owner, "
           f"this becomes a proposal the owners review.</p>"
           + (f"<p class=who>Shared folders use their own git identity: {esc('; '.join(ids))}</p>" if ids else ""))
    form = (_form_open(proj, st, "/do/add")
            + f'<label>The rule<textarea name=text maxlength={LIMITS["text"]} required>{esc(flash.get("text", ""))}'
              f'</textarea></label>'
              '<label>Kind<select name=kind><option value=rule>Rule (always delivered)</option>'
              '<option value=guidance>Guidance (delivered when relevant)</option></select></label>'
              f'<label>Where<select name=target>{opts}</select></label>'
              f'<label>Why (optional)<input type=text name=why maxlength={LIMITS["why"]} '
              f'value="{esc(flash.get("why", ""))}"></label>'
              '<div><button class="btn pri" type=submit>Add</button></div></form>')
    cards = (f'<article class=card><h3>A rule or guidance for the team</h3>{who}{form}'
             '<p class=hint>The same as <code>reverie context add</code>. A rule that looks like a secret is refused '
             'and not kept.</p></article>'
             + way("A rule only you follow", "Stays on your machine and is delivered to your agents. "
                   "Still a command.", 'reverie rules add "Money is integer cents"')
             + way("Turn a rule you already use into a team proposal",
                   "Takes a rule from your own rules or memory and proposes it to the team. Still a command.",
                   'reverie context propose "Money is integer cents"'))
    return frame(proj, plist, None, "/add",
                 _banner(flash or None) + f'<div class=board style="grid-auto-columns:minmax(272px,1fr)">{cards}</div>',
                 title="Add a rule")


def new_page(root: str, query=None) -> str:
    _, _, plist = _target(root, query)
    body = ('<ol class=steps>'
            '<li><b>Open a terminal in the project</b><p>Any git repo works.</p><code class="cmd big">cd path/to/your/repo</code></li>'
            '<li><b>Turn Reverie on there</b><p>It then appears in the sidebar the next time this page '
            'is opened.</p><code class="cmd big">reverie enable</code></li>'
            '<li><b>Connect an agent</b><p>One time per machine.</p><code class="cmd big">reverie install</code></li>'
            '</ol>')
    return shell("Add a project", "Projects are repos. Reverie finds them once you use it in them.", body, "/new",
                 plist=plist)


def search_page(root: str, proj: dict, plist: list[dict], q: str) -> str:
    d = collect(root)
    q = " ".join(q.split())[:120]
    needle = q.lower()
    if not needle:
        return frame(proj, plist, d, "/search", _empty("search", "Type something to search",
                     "Searches the team rules and the remembered items of this project."), title="Search")
    rule_hits = [i for i in d["res"].items if needle in i.text.lower()]
    mem_hits = [m for m in d["mem"] if needle in str(m.get("text", "")).lower()]
    team = [f'<article class=card><span class=chip>{esc(i.label)}</span><p class=q style="margin-top:10px">'
            f'{esc(_clip(i.text))}</p><div class=meta style="margin-top:8px">{esc(i.file)}</div></article>'
            for i in rule_hits[:50]]
    mems = [_mcard(m) for m in mem_hits[:50]]
    body = (f'<p class=hint>Results for “{esc(q)}” in {esc(proj["name"])}.</p><div class=board>'
            + _lane("Team rules", "a", team, "No match") + _lane("Memory", "", mems, "No match") + "</div>")
    return frame(proj, plist, d, "/search", body, title="Search")


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


def _who(folder: Path, root: str) -> str:
    """The git identity an action in ``folder`` will use (what the CLI would use)."""
    cwd = folder if folder.exists() else Path(root)
    name = spool._git(["config", "user.name"], cwd).strip()
    mail = spool._git(["config", "user.email"], cwd).strip()
    if not name and not mail:
        return "no git identity is set here"
    return f"{name} <{mail}>" if mail else name


def _targets(root: str) -> list[tuple[str, str]]:
    out = [("repo", f"This project ({Path(root).name}/.reverie/context)")]
    for layer, folders in context.sources(root).items():
        out += [(f"{layer}|{context.scope_name(f)}", f"{layer}: {context.scope_name(f)}") for f in folders]
    return out


def _form_open(proj: dict, st: State | None, action: str) -> str:
    return (f'<form class=f method=post action="{action}"><input type=hidden name=token '
            f'value="{html.escape(st.token if st else "")}"><input type=hidden name=p value="{esc(proj["id"])}">')


def _banner(flash: dict | None) -> str:
    if not flash:
        return ""
    return f'<div class="flash {"ok" if flash["ok"] else "bad"}" role=status>{esc(flash["msg"])}</div>'


def do_write(root: str, st: State, path: str, form: dict[str, str]) -> str:
    """Run one write through the CLI's functions and return where to redirect. Security checks
    (Host, Origin, token, size) are done by the handler before this is called."""
    plist = projects(root)
    proj = next((x for x in plist if x["id"] == form.get("p", "")), None)
    if proj is None:
        return _u("/", None, done=st.put(False, "Refused: that project is not known to Reverie."))
    target = proj["path"]
    page = "/add" if path == "/do/add" else "/proposals"
    back = lambda ok, msg, **keep: _u(page, proj, done=st.put(ok, msg, **keep))   # noqa: E731
    if is_disabled(target):
        return back(False, "Refused: Reverie is turned off for this project. Turn it on first.")
    text, why = form.get("text", ""), form.get("why", "")
    with st.lock:
        try:
            if path == "/do/add":
                if redact.contains_secret(text) or redact.contains_secret(why):
                    return back(False, "Refused: this looks like it contains a secret, so nothing was saved. "
                                       "Remove the value and try again.")
                choice = form.get("target", "repo")
                if choice not in dict(_targets(target)):
                    return back(False, "Refused: that is not a place this project can add to.", text=text, why=why)
                layer, _, scope = choice.partition("|")
                added = context.add(target, text, layer=layer, scope=scope,
                                    rule=form.get("kind", "rule") != "guidance", why=why)
                if added.proposal:
                    return back(True, f"Written as a proposal ({added.path.name}). The owners "
                                      f"({', '.join(added.owners)}) review it; it is not in force yet.")
                return back(True, f"Added to {added.path.name}. It applies from the next session.")
            slug = form.get("slug", "")
            # exact name only: the CLI matches a prefix, and "" is a prefix of every proposal
            if slug not in {x["slug"] for x in context.proposals(target)}:
                return back(False, "Refused: that proposal is not waiting any more.")
            if path == "/do/accept":
                doc = context.accept(target, slug)
                return back(True, f"Accepted into {doc.name}. The proposal is gone and the rule is in force.")
            if redact.contains_secret(why):
                return back(False, "Refused: the reason looks like it contains a secret, so nothing was "
                                   "changed. Remove the value and try again.")
            dest = context.reject(target, slug, why)
            return back(True, f"Rejected. The reason is kept in proposals/rejected/{dest.name}.")
        except ValueError as exc:
            return back(False, f"Refused: {exc}", **({"text": text, "why": why} if path == "/do/add" else {}))
        except Exception:
            return back(False, "That did not work, and nothing was changed. The CLI still does.")


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

PROJECT_PAGES: dict[str, Callable[..., str]] = {"/project": overview, "/start": start, "/context": context_page,
                 "/proposals": proposals_page, "/connect": connect_page, "/add": add_page}


def render(root: str, path: str, query: dict[str, list[str]] | None = None,
           st: State | None = None) -> tuple[int, str]:
    try:
        target, proj, plist = _target(root, query)
        if path in ("/", "/repos"):
            return 200, projects_page(root, query)
        if path == "/new":
            return 200, new_page(root, query)
        if path == "/memory" or path == "/search" or path in PROJECT_PAGES:
            if is_disabled(target):
                tab = path if path in dict((p, 1) for p, _, _ in TABS) else "/project"
                return 200, _disabled(proj, plist, tab)
            if path == "/memory":
                try:
                    page = int((query or {}).get("page", ["1"])[0])
                except ValueError:
                    page = 1
                return 200, memory_page(target, proj, plist, page)
            if path == "/search":
                return 200, search_page(target, proj, plist, ((query or {}).get("q") or [""])[0])
            if path in ("/add", "/proposals"):
                flash = st.take(((query or {}).get("done") or [""])[0]) if st else None
                return 200, PROJECT_PAGES[path](target, proj, plist, st, flash)
            return 200, PROJECT_PAGES[path](target, proj, plist)
        return 404, shell("Not found", 'No such page. <a href="/">Back to all projects</a>', "", "", plist=plist)
    except Exception:
        return 500, shell("Something went wrong", "This page could not be built. The CLI still works.", "", "",
                          plist=[])


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
            code, body = render(root, parts.path, parse_qs(parts.query), st)
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
                    where = do_write(root, st, path, form)
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
