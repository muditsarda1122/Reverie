"""`reverie ui`, reading: every GET/HEAD is free of side effects, escaped, bounded, local, silent.
The write run (add, accept, reject) lives in test_ui_writes.py."""

import hashlib
import html
import http.client
import re
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from reverie import cli, context, deliver, hooks, items, rules, ui

PAGES = ["/", "/project", "/start", "/context", "/proposals", "/memory", "/connect", "/add", "/new", "/search?q=squash"]


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=t", *a],
                   check=True, capture_output=True)


def _repo(path: Path, email="dev@acme.com") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "config", "user.email", email)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    return tmp_path, _repo(tmp_path / "svc")


@pytest.fixture
def serve():
    started = []

    def go(root):
        server = ui.make_server(str(root), 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server
    yield go
    for s in started:
        s.shutdown()
        s.server_close()


def req(server, path, method="GET", host=None):
    port = server.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{port}")
    c.endheaders()
    r = c.getresponse()
    body = r.read().decode("utf-8")
    out = (r.status, body, dict(r.getheaders()))
    c.close()
    return out


def _tree(*roots) -> dict:
    snap = {}
    for root in roots:
        for p in sorted(Path(root).rglob("*")):
            # SQLite's own -shm/-wal housekeeping appears when ANY reader opens a WAL database,
            # even read-only; the database file itself (reverie.db) is still compared byte for byte.
            if p.is_file() and ".git/" not in str(p) and not p.name.endswith(("-shm", "-wal")):
                snap[str(p)] = hashlib.sha1(p.read_bytes()).hexdigest()
    return snap


def _brain(repo, monkeypatch):
    """A real brain (with its repos table) so memory is readable, via `reverie enable`."""
    monkeypatch.chdir(repo)
    assert cli.main(["enable"]) == 0
    return items.connect(deliver.db_path())


def _seed(tmp_path, repo):
    """Two sibling teams that conflict, a stale team rule, a waiting proposal, memory."""
    for d in ("co/a", "co/b"):
        (tmp_path / d).mkdir(parents=True)
    (repo / ".reverie").mkdir(exist_ok=True)
    (repo / ".reverie" / "context.toml").write_text('[sources]\nteam = ["../co/a", "../co/b"]\n')
    (tmp_path / "co/a/r.md").write_text("## Rules\n- Always squash merge {id: merge}\n")
    (tmp_path / "co/b/r.md").write_text("## Rules\n- Never squash merge {id: merge}\n"
                                        "- Use `old_helper` everywhere\n")
    (repo / "CODEOWNERS").write_text("/.reverie/context/ lead@acme.com\n")
    context.add(str(repo), "Prefer small PRs", why="reviews are faster")      # proposal
    res = context.resolve(str(repo))
    stale_item = next(i for i in res.items if "old_helper" in i.text)
    st = context.load_state()
    st["stale"] = {stale_item.id: {"commit": "abcdef123456", "reason": "old_helper was removed",
                                   "file": stale_item.file}}
    context.save_state(st)
    rules.add("Money is integer cents", level="user")


# --- the home page: plain language, action first ----------------------------------------

def test_home_is_plain_language_with_action_items_first(env, serve):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    s = serve(repo)
    status, body, _ = req(s, "/project")
    assert status == 200
    for phrase in ("Rules in force", "Conflicts to settle", "Possibly outdated", "Proposals waiting"):
        assert phrase in body
    assert re.search(r"<b>1</b>Conflicts to settle", body)
    assert re.search(r"<b>1</b>Possibly outdated", body)
    assert re.search(r"<b>1</b>Proposals waiting", body)
    assert body.index("Needs your action") < body.index("Two rules disagree")
    assert body.index("<b>1</b>Rules in force") < body.index("Needs your action")
    assert "A proposal is waiting" in body and "reverie context accept" in body
    assert ">Possibly outdated</span>" in body and "old_helper was removed" in body
    assert "Nothing needs your action" not in body


def test_home_when_nothing_needs_action(env, serve):
    _, repo = env
    rules.add("Money is integer cents", level="user")
    _, body, _ = req(serve(repo), "/project")
    assert "Nothing needs your action right now." in body and re.search(r"<b>1</b>Rules in force", body)


# --- hostile and large data ------------------------------------------------------------------

HOSTILE = "<script>alert(1)</script> <img src=x onerror=alert(2)> \"quoted\" & more"


def test_hostile_text_is_escaped_on_every_page(env, serve, monkeypatch):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    (tmp_path / "co/a/hostile.md").write_text(f"## Rules\n- {HOSTILE}\n")
    context.add(str(repo), HOSTILE, why=HOSTILE)
    rules.add(HOSTILE, level="user")
    conn = _brain(repo, monkeypatch)
    items.add(conn, text=HOSTILE, kind="rule", source="user_said", repo_key=deliver.repo_key(str(repo)))
    conn.close()
    s = serve(repo)
    saw = 0
    for page in PAGES:
        status, body, hdr = req(s, page)
        assert status == 200
        assert "<script" not in body.lower() and "<img" not in body.lower(), page
        assert "onerror=alert" not in body.replace("&", "") or "&lt;img" in body
        assert hdr["Content-Security-Policy"].startswith("default-src 'none'")
        saw += "&lt;script&gt;" in body
    assert saw >= 3                                  # it is shown, as text


def test_memory_500_items_is_paged_and_bounded(env, serve, monkeypatch):
    _, repo = env
    conn = _brain(repo, monkeypatch)
    key = deliver.repo_key(str(repo))
    for n in range(500):
        items.add(conn, text=f"Convention number {n}: " + "x" * 400, kind="rule",
                  source="user_said", repo_key=key)
    conn.close()
    s = serve(repo)
    _, body, _ = req(s, "/memory")
    assert "500 item(s)" in body and "Page 1 of 5" in body
    assert body.count("<article class=card>") == ui.PAGE_SIZE and len(body) < 120_000
    assert "Page 5 of 5" in req(s, "/memory?page=5")[1]
    assert "Page 5 of 5" in req(s, "/memory?page=999")[1]        # clamped
    assert "Page 1 of 5" in req(s, "/memory?page=abc")[1]        # junk ignored
    assert "Page 1 of 5" in req(s, "/memory?page=-3")[1]


def test_every_page_has_an_empty_state(env, serve):
    _, repo = env                                    # no .reverie dir, no brain, no rules
    assert not (repo / ".reverie").exists()
    s = serve(repo)
    want = {"/project": "Nothing needs your action", "/start": "Nothing.", "/context": "No team context",
            "/proposals": "No proposals", "/memory": "Nothing remembered", "/": "Projects",
            "/connect": "reverie install", "/add": "reverie context add", "/new": "reverie enable",
            "/search?q=": "Type something to search"}
    for page, text in want.items():
        status, body, _ = req(s, page)
        assert status == 200 and text in body, page
    assert req(s, "/nope")[0] == 404


def test_disabled_repo_shows_only_that_it_is_off(env, serve, monkeypatch):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    monkeypatch.chdir(repo)
    assert cli.main(["disable"]) == 0
    s = serve(repo)
    for page in ("/", "/project", "/start", "/context", "/proposals", "/memory", "/search?q=squash"):
        _, body, _ = req(s, page)
        assert "turned off for this repo" in body, page
        assert "squash merge" not in body and "integer cents" not in body and "Prefer small PRs" not in body


def test_port_in_use_is_reported_not_raised(env):
    _, repo = env
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    out = []
    try:
        assert ui.serve(str(repo), busy.getsockname()[1], open_browser=False, print_fn=out.append) == 2
    finally:
        busy.close()
    assert "cannot listen" in out[0] and "--port" in out[0]


# --- the read-only run: reading changes nothing (the write run is test_ui_writes.py) --------------------------------------------------------------

def test_read_only_run_crawl_changes_nothing_not_even_the_write_urls(env, serve, capsys, monkeypatch):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    conn = _brain(repo, monkeypatch)
    items.add(conn, text="A remembered rule", kind="rule", source="user_said",
              repo_key=deliver.repo_key(str(repo)))
    conn.close()
    before = _tree(tmp_path / "home", repo, tmp_path / "co")
    s = serve(repo)
    for page in PAGES + ["/memory?page=2", "/nope", "/do/add", "/do/accept", "/do/reject",
                         "/add?done=forged", "/proposals?done=forged"]:
        req(s, page)
        assert req(s, page, "HEAD")[1] == ""
    for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):      # POST only answers on /do/*
        status, _, hdr = req(s, "/", m)
        assert status == 405 and hdr["Allow"] == "GET, HEAD", m
    after = _tree(tmp_path / "home", repo, tmp_path / "co")
    assert sorted(set(after) - set(before)) == [] and after == before
    assert capsys.readouterr().err == ""             # no access log


def test_wrong_host_is_refused_and_bind_is_loopback(env, serve):
    _, repo = env
    s = serve(repo)
    assert s.server_address[0] == "127.0.0.1"
    for host in ("evil.example", "127.0.0.1:1", "localhost"):
        assert req(s, "/", host=host)[0] == 403
    assert req(s, "/", host=f"localhost:{s.server_address[1]}")[0] == 200


def test_no_external_resources_or_telemetry(env, serve):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    s = serve(repo)
    for page in PAGES:
        _, body, hdr = req(s, page)
        assert not re.search(r"(?i)(src|href|action)\s*=\s*['\"]?(https?:)?//", body), page
        assert "<script" not in body.lower() and "<link" not in body.lower()
        assert "cookie" not in {k.lower() for k in hdr}
    assert ui.make_handler.__doc__ is None


# --- the start page is exactly what the CLI delivers ------------------------------------------

def test_start_page_is_exactly_the_delivered_text(env, serve):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    rules.add("Quote \"this\" & <that>", level="user")
    s = serve(repo)
    page = req(s, "/start")[1]
    shown = html.unescape(re.search(r'<pre id=start>(.*?)</pre>', page, re.S).group(1))
    delivered = deliver.startup_text(str(repo), log=False)
    assert shown == delivered and delivered
    printed: list[str] = []
    hooks.session_start(str(repo), print_fn=printed.append)    # what an agent actually gets
    assert delivered in "\n".join(printed)


def test_viewing_the_start_block_is_not_a_delivery(env, serve, monkeypatch):
    """Regression (found by the snapshot test): rendering /start logged a delivery that
    never happened, which would feed the usage judge phantom deliveries."""
    from reverie import usage
    tmp_path, repo = env
    conn = _brain(repo, monkeypatch)
    items.add(conn, text="Always paginate", kind="rule", source="user_said",
              repo_key=deliver.repo_key(str(repo)))
    conn.close()
    log = usage._path(str(repo))
    s = serve(repo)
    for _ in range(3):
        req(s, "/start")
    assert not log.exists()
    deliver.startup_text(str(repo))                  # a real delivery does log
    assert log.exists()


def test_only_known_projects_open_and_never_by_path(env, serve, monkeypatch):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    other = _repo(tmp_path / "other")
    (other / ".reverie").mkdir()
    (other / ".reverie" / "rules.md").write_text("- SECRET-OTHER-REPO-RULE\n")
    s = serve(repo)
    for url in (f"/project?p={other}", "/project?p=../../etc", f"/memory?p={other}", "/context?p=deadbeef"):
        status, body, _ = req(s, url)
        assert status == 200 and "SECRET-OTHER-REPO-RULE" not in body
        assert "svc" in body                              # fell back to the repo it was started in
    ids = re.findall(r'href="/project\?p=([0-9a-f]{8})"', req(s, "/")[1])
    assert ids and len(set(ids)) == 1                      # one known project, never the other repo


# --- copy and count fixes (one test each) -----------------------------------------------------

def test_memory_hint_says_this_machine_not_everyone(env, serve, monkeypatch):
    _, repo = env
    conn = _brain(repo, monkeypatch)
    items.add(conn, text="Always paginate", kind="rule", source="user_said",
              repo_key=deliver.repo_key(str(repo)))
    conn.close()
    body = req(serve(repo), "/memory")[1]
    assert "1 item(s) remembered on this machine for this repo." in body
    assert "shared by everyone" not in body


def test_add_page_personal_rule_command_has_user_flag(env, serve):
    """Without --user, `reverie rules add` writes the git-shared <repo>/.reverie/rules.md."""
    _, repo = env
    body = req(serve(repo), "/add")[1]
    assert 'reverie rules add --user "Money is integer cents"' in body
    assert 'reverie rules add "' not in body


def test_repo_rules_file_is_shown_labelled_and_counted(env, serve):
    """<repo>/.reverie/rules.md reaches agents as "This repo's rules (team)"; the page must show and count it."""
    _, repo = env
    rules.add("Money is integer cents", level="repo", cwd=str(repo))
    rules.add("Prefer small PRs", level="repo", cwd=str(repo))
    s = serve(repo)
    ctx = req(s, "/context")[1]
    assert "Shared rules file: no owners, no review" in ctx
    assert "Money is integer cents" in ctx and "Prefer small PRs" in ctx
    assert "No team context applies here" not in ctx            # not the empty state any more
    assert re.search(r"<b>2</b>Rules in force", req(s, "/project")[1])
    assert "2 rules in force" in req(s, "/")[1]
    assert "This repo's rules (team)" in html.unescape(req(s, "/start")[1])   # same bullets, as delivered


def test_repo_rules_file_sits_beside_team_context(env, serve):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    rules.add("Repo file rule", level="repo", cwd=str(repo))
    body = req(serve(repo), "/context")[1]
    assert "Shared rules file: no owners, no review" in body and "Repo file rule" in body
    assert "Always squash merge" in body                          # the team table is still there


def test_rules_in_force_matches_what_the_start_block_gives(env, serve, monkeypatch):
    """A remembered rule that repeats a team rule is hidden from agents, so it is not counted; a repo-level
    preference is only an index line, so it is not counted either."""
    tmp_path, repo = env
    (repo / ".reverie" / "context").mkdir(parents=True)
    (repo / ".reverie" / "context" / "rules.md").write_text("## Rules\n- Money is integer cents\n")
    conn = _brain(repo, monkeypatch)
    key = deliver.repo_key(str(repo))
    items.add(conn, text="money is integer cents.", kind="rule", source="user_said", repo_key=key)   # repeat
    items.add(conn, text="Always paginate list endpoints", kind="rule", source="user_said", repo_key=key)
    items.add(conn, text="Keep handlers short", kind="preference", source="user_said", repo_key=key)
    items.add(conn, text="Use UTC everywhere", kind="preference", source="user_said", repo_key=None)
    conn.close()
    s = serve(repo)
    start = req(s, "/start")[1]
    assert "Always paginate list endpoints" in start and "Use UTC everywhere" in start
    given = deliver.startup_text(str(repo), log=False)
    assert given.count("Money is integer cents") == 1            # shown once: the team rule
    # team rule 1 + remembered "paginate" 1 + user-wide preference 1
    assert re.search(r"<b>3</b>Rules in force", req(s, "/project")[1])
    assert "3 rules in force" in req(s, "/")[1]


def test_cli_help_for_ui_no_longer_says_read_only():
    sub = next(a for a in cli._build_parser()._actions if a.dest == "command")
    helps = {c.dest: c.help for c in sub._choices_actions}
    assert "read-only" not in helps["ui"].lower()
    assert "add, accept or reject" in helps["ui"]


def test_overview_proposal_links_to_its_card_on_proposals(env, serve):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    context.add(str(repo), "Second proposal", why="because")          # a second one waiting
    s = serve(repo)
    over = req(s, "/project")[1]
    links = re.findall(r'<a href="(/proposals\?p=[0-9a-f]{8})#(p-[0-9a-f]{10})">A proposal is waiting</a>', over)
    assert len(links) == 2 and len({frag for _, frag in links}) == 2
    page = req(s, links[0][0])[1]
    for _, frag in links:
        assert f'<article class=card id="{frag}">' in page      # every link lands on a card
    assert page.count('<article class=card id="p-') == 2


def test_memory_lanes_use_human_names(env, serve, monkeypatch):
    _, repo = env
    conn = _brain(repo, monkeypatch)
    key = deliver.repo_key(str(repo))
    for kind, text in (("rule", "r1"), ("preference", "p1"), ("decision", "d1"),
                       ("rejected_approach", "Do not cache fees"), ("environment", "Needs docker"),
                       ("open_question", "Which queue?"), ("external_context", "Vendor API v2")):
        items.add(conn, text=text, kind=kind, source="user_said", repo_key=key)
    conn.close()
    body = req(serve(repo), "/memory")[1]
    lanes = re.findall(r"<section class=lane><h2>([^<]+)<", body)
    assert lanes == ["Rules", "Preferences", "Decisions", "Environment", "External context",
                     "Open questions", "Rejected approaches"]       # known first, the rest alphabetical
    assert "Findings" not in body and "Rejected_Approach" not in body and "_" not in " ".join(lanes)
    assert ui._kind_name("observation") == "Observations" and ui._kind_name("some_new_kind") == "Some new kind"


def test_connect_tab_of_a_disabled_project_shows_how_to_turn_it_on(env, serve, monkeypatch):
    tmp_path, repo = env
    _seed(tmp_path, repo)
    monkeypatch.chdir(repo)
    assert cli.main(["disable"]) == 0
    s = serve(repo)
    status, body, _ = req(s, "/connect")
    assert status == 200
    assert "Reverie is turned off for this repo" in body
    assert "reverie enable" in body and "Turn it on for this project" in body
    assert "reverie install" in body
    assert "squash merge" not in body and "Prefer small PRs" not in body      # still shows no project data
    for page in ("/project", "/context", "/proposals", "/memory", "/start", "/add"):   # the rest stay "off"
        assert "Nothing is delivered to agents and nothing is shown here" in req(s, page)[1], page
