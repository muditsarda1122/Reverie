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
import re
import secrets
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import context, deliver, redact, rules, spool, usage
from .ui_style import CSS as DESIGN_CSS

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
              "/context": (rules_in_force(d)[0] if d else 0, False), "/memory": (len(d.get("mem", [])), False)}
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


def _pslug(slug: str) -> str:
    """A safe fragment id for a proposal card, the same on Overview (link) and Proposals (target)."""
    return "p-" + hashlib.sha1(slug.encode()).hexdigest()[:10]


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
        in_force, cut = rules_in_force(d)
        cut_note = f'<span>{cut} cut by the start limit</span><span>·</span>' if cut else ""
        foot = (f'<hr class=rule><div class=meta><span>{in_force} rules in force</span><span>·</span>{cut_note}'
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


def _chips(marks: list[str]) -> str:
    return "".join(f'<span class="chip {"bad" if m == "conflict" else "warn" if "outdated" in m else ""}">'
                   f'{esc(m)}</span>' for m in marks)


_KIND_ORDER = ("rule", "preference", "decision")
KIND_NAMES = {"rule": "Rules", "preference": "Preferences", "decision": "Decisions",
              "rejected_approach": "Rejected approaches", "environment": "Environment",
              "observation": "Observations", "open_question": "Open questions",
              "work_state": "Work in progress", "external_context": "External context"}


def _kind_name(kind: str) -> str:
    return KIND_NAMES.get(kind) or kind.replace("_", " ").capitalize()


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
                   "Still a command.", 'reverie rules add --user "Money is integer cents"')
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


_TITLE_WORD = {"Overview": "of", "Rules": "of", "Memory": "of", "What agents are told": "in"}


def _frame(root: str, proj: dict, plist: list[dict], d: dict, w: dict, tab: str, body: str, title: str, h1: str,
           full_title: str = "") -> str:
    return layout(title=full_title or f"{title} {_TITLE_WORD.get(title, 'for')} {proj['name']}, Reverie", proj=proj, plist=plist,
                  body=body, tab=tab, counts=w["counts"], h1=h1)


def _flash_html(flash: dict | None) -> str:
    if not flash:
        return ""
    return f'<p class="flash" role="status">{esc(flash["msg"]).replace("in force", "<span class=\"in\">in force</span>")}</p>'


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


def memory_page(root: str, proj: dict, plist: list[dict], page: int = 1) -> str:
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


CHECK_LATER = (" It has not been checked for conflicts yet: that happens at the next session start, "
               "or now with `reverie context check`.")


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
                return back(True, f"Added to {added.path.name}. It applies from the next session." + CHECK_LATER)
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
            return back(False, f"Refused: {exc}", **({"text": text, "why": why} if path == "/do/add" else {}))
        except Exception:
            return back(False, "That did not work, and nothing was changed. The CLI still does.")


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

PROJECT_PAGES: dict[str, Callable[..., str]] = {"/project": overview, "/start": start, "/context": rules_page,
                 "/proposals": proposals_page, "/connect": connect_page, "/add": add_page}


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
            return 200, projects_page(root, query)
        if path == "/new":
            return 200, new_page(root, query)
        if path in ("/memory", "/search", "/rule") or path in PROJECT_PAGES:
            if path != "/connect" and is_disabled(target):    # Connect is how you turn it back on
                tab = path if path in dict((p, 1) for p, _, _ in TABS) else "/project"
                return 200, _disabled(proj, plist, tab)
            if path == "/rule":
                rid = ((query or {}).get("id") or [""])[0]
                return rule_page(target, proj, plist, rid if ID_OK.match(rid) else "")
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
