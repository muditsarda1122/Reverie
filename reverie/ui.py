"""`reverie ui`: a read-only local page. A command, not a daemon: it serves on 127.0.0.1 until
Ctrl-C, from the same functions the CLI uses, and changes nothing.

* only GET and HEAD answer (everything else is 405); nothing here writes a file; the brain is
  opened read-only;
* bound to 127.0.0.1; a Host header that is not the bound address is refused (DNS rebinding);
* every value is HTML-escaped (shared documents are written by other people) and passed through
  the existing redaction; a strict Content-Security-Policy; no script, no external resource;
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

CSS = """
:root{--bg:#fff;--fg:#1c1f23;--mut:#5b6470;--line:#dfe3e8;--card:#f6f7f9;--warn:#9a5b00;--warnbg:#fff4e0;--ok:#1f6f43;--link:#0b5cad}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8eaed;--mut:#9aa3ae;--line:#2c3138;--card:#1c2025;--warn:#f0b35a;--warnbg:#2a2113;--ok:#6cc08f;--link:#7db4f0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main,header{max-width:60rem;margin:0 auto;padding:0 16px}header{padding-top:16px}
a{color:var(--link)}nav a{margin-right:14px;text-decoration:none}nav a.on{font-weight:700;text-decoration:underline}
h1{font-size:1.5rem;margin:.8em 0 .3em}h2{font-size:1.1rem;margin:1.6em 0 .4em}p.mut,.mut{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:8px 0;overflow-wrap:anywhere}
.warn{background:var(--warnbg);border-color:var(--warn)}.warn b{color:var(--warn)}
code,pre{font:13px/1.45 ui-monospace,monospace}pre{background:var(--card);border:1px solid var(--line);padding:10px;overflow:auto;white-space:pre-wrap}
code{background:var(--card);padding:1px 4px;border-radius:4px}
.nums{display:flex;flex-wrap:wrap;gap:10px}.num{flex:1 1 9rem;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.num b{font-size:1.6rem;display:block}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:5px 6px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
"""

NAV = (("/", "Home"), ("/start", "What agents are told"), ("/context", "Team context"),
       ("/proposals", "Proposals"), ("/memory", "Memory"), ("/repos", "Repos"))


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


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

def layout(title: str, body: str, active: str) -> str:
    links = "".join(f'<a href="{u}"{" class=on" if u == active else ""}>{esc(n)}</a>' for u, n in NAV)
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)} · Reverie</title><style>{CSS}</style></head><body>"
            f"<header><nav>{links}</nav></header><main><h1>{esc(title)}</h1>{body}"
            f"<p class=mut>Read-only view. Nothing here changes anything; each item shows the "
            f"command that does.</p></main></body></html>")


def _disabled_page(active: str) -> str:
    return layout("Reverie is turned off for this repo",
                  "<p>Nothing is delivered to agents and nothing is shown here. "
                  "Turn it back on with <code>reverie enable</code>.</p>", active)


def _owner(item) -> str:
    return _safe(lambda: context.owner_text(item), "")


def home(root: str) -> str:
    d = collect(root)
    in_force = len(d["team_rules"]) + len(d["personal"]) + len(d["mem_rules"])
    outdated = len(d["stale_team"]) + len(d["stale_mem"])
    nums = [("Rules in force", in_force, "agents follow these"),
            ("Conflicts to settle", len(d["unresolved"]), "two rules disagree"),
            ("Possibly outdated", outdated, "the code may have moved on"),
            ("Proposals waiting", len(d["proposals"]), "waiting for an owner")]
    top = "".join(f'<div class=num><b>{n}</b>{esc(label)}<br><span class=mut>{esc(sub)}</span></div>'
                  for label, n, sub in nums)
    action: list[str] = []
    for c in d["unresolved"]:
        a, b = c.overridden[0], c.overridden[1]
        who = "; ".join(x for x in (_owner(a), _owner(b)) if x)
        action.append(
            f"<div class='card warn'><b>Two rules disagree.</b> {esc(a.label)} says "
            f"“{esc(_clip(a.text, 160))}”, but {esc(b.label)} says “{esc(_clip(b.text, 160))}”. "
            f"Agents are told to ask before acting on either."
            + (f" Ask: {esc(who)}." if who else "")
            + f"<br><span class=mut>To settle it: <code>reverie context resolve {esc(c.key)} "
              f"\"your decision\"</code></span></div>")
    for it in d["stale_team"]:
        st = d["res"].stale.get(it.id, {})
        action.append(
            f"<div class='card warn'><b>Possibly outdated.</b> “{esc(_clip(it.text, 200))}” "
            f"({esc(it.label)}). A later change to the code may contradict it: "
            f"{esc(_clip(str(st.get('reason', '')), 200))}<br><span class=mut>Check the code, then "
            f"edit or remove the rule in <code>{esc(it.file)}</code>.</span></div>")
    for m in d["stale_mem"]:
        action.append(
            f"<div class='card warn'><b>Possibly outdated.</b> “{esc(_clip(m['text'], 200))}” "
            f"(remembered from an earlier session).<br><span class=mut><code>reverie stale list</code>"
            f"</span></div>")
    for p in d["proposals"]:
        action.append(
            f"<div class='card warn'><b>A proposal is waiting.</b> “{esc(_clip(context.bullet_text(p['bullet']), 200))}” "
            f"proposed by {esc(p.get('proposed-by', 'someone'))}"
            + (f" because: {esc(_clip(p.get('why', ''), 160))}" if p.get("why") else "")
            + f".<br><span class=mut>An owner runs <code>reverie context accept {esc(p['slug'])}</code> "
              f"or <code>reverie context reject {esc(p['slug'])} --why \"…\"</code></span></div>")
    needs = ("".join(action) if action else "<p class=card>Nothing needs your action right now.</p>")
    more = ('<p><a href="/start">What agents are told</a> · <a href="/context">Team context</a> · '
            '<a href="/proposals">Proposals</a> · <a href="/memory">Memory</a> · '
            '<a href="/repos">Repos</a></p>')
    body = (f"<p class=mut>{esc(spool.repo_root(root))}</p><div class=nums>{top}</div>"
            f"<h2>Needs your action</h2>{needs}<h2>Look closer</h2>{more}")
    return layout("Reverie at a glance", body, "/")


def start(root: str) -> str:
    text = _safe(lambda: deliver.startup_text(root, log=False), "")
    body = (f"<p>This is exactly what an agent is handed when a session starts in this repo "
            f"({len(text)} characters).</p>"
            + (f"<pre id=start>{esc(text, redacted=False)}</pre>" if text else
               "<p class=card>Nothing. No rules or memory apply here yet.</p>"))
    return layout("What agents are told", body, "/start")


def context_page(root: str) -> str:
    d = collect(root)
    res = d["res"]
    if not res.items:
        return layout("Team context", "<p class=card>No team context applies here. Put markdown "
                      "in <code>.reverie/context/</code> (see <code>reverie context</code>).</p>",
                      "/context")
    rows = []
    lost = {o.id for c in res.conflicts for o in c.overridden}
    for it in res.items:
        marks = [m for m, on in (("locked", it.locked), ("conflict", it.id in lost),
                                 ("possibly outdated", it.id in res.stale),
                                 ("also a shared rule: hidden here", it.id in res.hidden),
                                 (f"only under {', '.join(it.paths)}", bool(it.paths))) if on]
        rows.append(f"<tr><td>{esc(it.label)}</td><td>{esc(it.kind)}</td><td>{esc(_clip(it.text))}"
                    f"<br><span class=mut>{esc(it.file)}</span></td><td>{esc(', '.join(marks))}</td>"
                    f"<td><code>{esc(it.id)}</code></td></tr>")
    conf = []
    for c in res.conflicts:
        names = " vs ".join(f"{x.label}: {_clip(x.text, 120)}" for x in c.items)
        conf.append(f"<div class=card><b>{esc(c.status)}</b> · <code>{esc(c.key)}</code><br>"
                    f"{esc(names)}" + (f"<br><span class=mut>{esc(c.reason)}</span>" if c.reason else "")
                    + "</div>")
    hint = context.contradiction_hint(res)
    body = ((f"<h2>Conflicts</h2>{''.join(conf)}" if conf else "")
            + (f"<p class=mut>Hint shown to agents: {esc(hint)}</p>" if hint else "")
            + "<h2>Everything in force</h2><table><tr><th>Scope</th><th>Kind</th><th>Text</th>"
              f"<th>Marks</th><th>Id</th></tr>{''.join(rows)}</table>")
    return layout("Team context", body, "/context")


def proposals_page(root: str) -> str:
    d = collect(root)
    if not d["proposals"] and not d["rejected"]:
        return layout("Proposals", "<p class=card>No proposals. Anyone can suggest a rule with "
                      "<code>reverie context add</code>, or turn one of their own into a "
                      "proposal with <code>reverie context propose</code>.</p>", "/proposals")
    out = ["<h2>Waiting</h2>" if d["proposals"] else ""]
    for p in d["proposals"]:
        extra = " · ".join(f"{k}: {p[k]}" for k in ("from", "first-stated", "usage") if p.get(k))
        out.append(f"<div class=card>“{esc(context.bullet_text(p['bullet']))}”<br><span class=mut>"
                   f"by {esc(p.get('proposed-by', '?'))} on {esc(p.get('date', '?'))} for "
                   f"{esc(p.get('document', '?'))}" + (f" · why: {esc(p['why'])}" if p.get("why") else "")
                   + (f" · {esc(extra)}" if extra else "") + f"<br><code>reverie context accept "
                   f"{esc(p['slug'])}</code></span></div>")
    if d["rejected"]:
        out.append("<h2>Turned down</h2>")
    for r in d["rejected"]:
        out.append(f"<div class=card>“{esc(context.bullet_text(r['bullet']))}”<br><span class=mut>"
                   f"rejected {esc(r.get('rejected-on', '?'))} by {esc(r.get('rejected-by', '?'))}: "
                   f"{esc(r.get('reason', ''))}</span></div>")
    return layout("Proposals", "".join(out), "/proposals")


def memory_page(root: str, page: int = 1) -> str:
    mem = memory_items(root)
    if not mem:
        return layout("Memory", "<p class=card>Nothing remembered for this repo yet.</p>", "/memory")
    pages = max(1, -(-len(mem) // PAGE_SIZE))
    page = min(max(page, 1), pages)
    rows = []
    for m in mem[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]:
        meta = m.get("meta") or {}
        use = ", ".join(f"{k.lower()}={v}" for k, v in sorted((meta.get("usage") or {}).items()))
        flags = [f for f, on in (("possibly outdated", deliver.is_stale_flagged(m)),
                                 ("challenged", bool(meta.get("challenged")))) if on]
        rows.append(f"<tr><td>{esc(m.get('kind', ''))}</td><td>{esc(_clip(m.get('text', '')))}</td>"
                    f"<td>{esc(m.get('source') or m.get('origin') or '')}</td><td>{esc(use)}</td>"
                    f"<td>{esc(', '.join(flags))}</td><td><code>{esc(str(m.get('id', ''))[:8])}</code></td></tr>")
    nav = ""
    if pages > 1:
        nav = (f"<p>Page {page} of {pages}: "
               + (f'<a href="/memory?page={page - 1}">previous</a> ' if page > 1 else "")
               + (f'<a href="/memory?page={page + 1}">next</a>' if page < pages else "") + "</p>")
    body = (f"<p>{len(mem)} item(s). Open one with <code>reverie show &lt;id&gt;</code>.</p>{nav}"
            "<table><tr><th>Kind</th><th>Text</th><th>Source</th><th>Agents followed it</th>"
            f"<th>Flags</th><th>Id</th></tr>{''.join(rows)}</table>{nav}")
    return layout("Memory", body, "/memory")


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
        return layout("Repos", "<p class=card>No repo is known yet. Use Reverie from your agent in "
                      "a repo once, or run <code>reverie enable</code> there.</p>", "/repos")
    trs = "".join(f"<tr><td>{esc(r.get('path') or r.get('repo_key'))}</td><td>{esc(r.get('state'))}</td>"
                  f"<td>{esc(r.get('url') or '')}</td></tr>" for r in rows)
    return layout("Repos", f"<table><tr><th>Repo</th><th>State</th><th>Remote</th></tr>{trs}</table>",
                  "/repos")


ROUTES = {"/": home, "/start": start, "/context": context_page, "/proposals": proposals_page,
          "/repos": repos_page}


def render(root: str, path: str, query: dict[str, list[str]] | None = None) -> tuple[int, str]:
    if path == "/memory":
        if is_disabled(root):
            return 200, _disabled_page(path)
        try:
            page = int((query or {}).get("page", ["1"])[0])
        except ValueError:
            page = 1
        return 200, memory_page(root, page)
    fn = ROUTES.get(path)
    if fn is None:
        return 404, layout("Not found", "<p>No such page. <a href=\"/\">Home</a></p>", "")
    if path != "/repos" and is_disabled(root):
        return 200, _disabled_page(path)
    try:
        return 200, fn(root)
    except Exception:
        return 500, layout("Something went wrong",
                           "<p>This page could not be built. The CLI still works.</p>", "")


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
