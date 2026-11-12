"""`reverie ui`, stage 3 of the design port: Add, the board, Add a project, Connect, Search, Turned off, Not found and
Something went wrong. Every refusal is reached by a direct POST (hiding a form is never the check)."""

import html
import re
import shlex
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from test_ui_writes import KEY, _follow, _form, _http, _tree

from reverie import cli, context, deliver, install, items, ui

OWNER = "lead@acme.com"


def _repo(path: Path, email="dev@acme.com", name="Dana Dev", owners="/.reverie/context/ lead@acme.com") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for k, v in (("user.email", email), ("user.name", name)):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
    if owners:
        (path / "CODEOWNERS").write_text(owners + "\n")
    return path


def _as(repo: Path, email: str, name: str) -> None:
    for k, v in (("user.email", email), ("user.name", name)):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    repo = _repo(tmp_path / "svc")                                  # dev is NOT an owner
    monkeypatch.chdir(repo)
    assert cli.main(["enable"]) == 0
    return tmp_path, repo


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


def get(s, path):
    return _http(s, "GET", path)


def _post_add(s, text="Prefer small PRs", **over):
    tok, pid = _form(s, "/add")
    fields = {"token": tok, "p": pid, "text": text, "kind": "rule", "target": "repo", "why": "because", **over}
    return _http(s, "POST", "/do/add", fields)


def _main(body: str) -> str:
    return body[body.index("<main"):body.index("</main>")]


# --- Add: what the page says will happen is what context.add does ----------------------------------------------

def test_add_tells_a_non_owner_it_becomes_a_proposal_and_an_owner_it_does_not(env, serve):
    tmp_path, repo = env
    s = serve(repo)
    page = get(s, "/add")[1]
    assert '<h1 class="ask">What should agents know about this repo?</h1>' in page
    assert ('A rule waits until an owner of <code>rules.md</code> accepts it (lead@acme.com); '
            'guidance waits for an owner of <code>guidance.md</code> (lead@acme.com).') in page          # CODEOWNERS covers the folder
    assert "Reverie edits the file and does not commit it." in page
    assert page.count('name="target"') == 1 and page.count('name="kind"') == 2
    _as(repo, OWNER, "Lee Lead")
    page = get(s, "/add")[1]
    assert re.search(r"No review: you are an owner of <code>rules\.md</code> \(<span title=\"lead@acme.com\">Lee \(you\)</span>\)", page)
    assert "waits until an owner" not in page


def test_the_why_field_shows_only_when_an_option_can_lead_to_a_proposal(env, serve):
    tmp_path, repo = env
    s = serve(repo)
    page = get(s, "/add")[1]                                                              # a non-owner: a proposal is possible
    assert 'name="why"' in page and "Only a proposal keeps this" in page
    _as(repo, OWNER, "Lee Lead")
    page = get(s, "/add")[1]                                                              # an owner: every option is direct
    assert 'name="why"' not in page and "Only a proposal keeps this" not in page and "Why (optional)" not in page
    tok, pid = _form(s, "/add")                                                           # hiding the field is not the check: a posted why is still dropped
    _follow(s, _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": "Direct rule", "kind": "rule", "target": "repo", "why": "ignored"}))
    assert "ignored" not in (repo / ".reverie/context/rules.md").read_text()


def test_the_terminal_examples_put_why_only_where_it_is_kept(env, serve):
    s = serve(env[1])
    page = html.unescape(get(s, "/add")[1])
    assert 'reverie context add "Money is stored as integer cents"</pre>' in page
    assert 'reverie context propose "Money is stored as integer cents" --why "rounding drift"</pre>' in page
    assert "context add \"Money is stored as integer cents\" --why" not in page


def test_when_only_rules_md_is_owned_guidance_is_added_straight_away_and_the_page_says_so(env, serve):
    tmp_path, repo = env
    (repo / "CODEOWNERS").write_text("/.reverie/context/rules.md lead@acme.com\n")
    s = serve(repo)
    assert "A rule waits until an owner of <code>rules.md</code> accepts it (lead@acme.com); guidance is added straight away." in get(s, "/add")[1]
    page = _follow(s, _post_add(s, "Docs live in /docs", kind="guidance"))              # a non-owner's guidance: direct, as promised
    assert "Added to <code>guidance.md</code>" in page
    assert "Docs live in /docs" in (repo / ".reverie/context/guidance.md").read_text()
    page = _follow(s, _post_add(s, "Money is integer cents"))                             # and a rule: a proposal
    assert "Proposed: " in page and not (repo / ".reverie/context/rules.md").exists()


def test_a_non_owners_add_is_a_proposal_with_a_link_to_it(env, serve):
    tmp_path, repo = env
    s = serve(repo)
    page = _follow(s, _post_add(s, "Run `ruff` before every commit", why="the linter catches what reviews miss"))
    flash = re.search(r'<p class="flash" role="status">(.*?)</p>', page, re.S).group(1)
    assert flash.startswith("Proposed: “Run <code>ruff</code> before every commit” It waits for an owner of <code>rules.md</code>: lead@acme.com. It is not in force yet. ")
    link = re.search(r'<a href="(/proposals\?p=[0-9a-f]{8})">See the proposal</a>', flash)
    assert link and not (repo / ".reverie/context/rules.md").exists()
    props = get(s, link.group(1))[1]
    assert "Run <code>ruff</code> before every commit" in props and "Why: the linter catches what reviews miss" in props
    assert "why: the linter catches what reviews miss" in next((repo / ".reverie/context/proposals").glob("*.md")).read_text()   # a proposal keeps it
    assert "Proposed:" not in get(s, "/add")[1]                                            # one-shot


def test_an_owners_add_goes_straight_in_and_the_why_is_not_kept(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    s = serve(repo)
    page = _follow(s, _post_add(s, "Run `ruff` before every commit", why="a very specific reason"))
    flash = re.search(r'<p class="flash" role="status">(.*?)</p>', page, re.S).group(1)
    assert flash.startswith("Added to <code>rules.md</code>: “Run <code>ruff</code> before every commit” It applies from the next session.")
    assert "reverie context check</code>" in flash
    link = re.search(r'<a href="(/context\?p=[0-9a-f]{8})">See it in Rules</a>', flash)
    assert link and "Run <code>ruff</code> before every commit" in get(s, link.group(1))[1]
    stored = (repo / ".reverie/context/rules.md").read_text()
    assert "- Run `ruff` before every commit" in stored and "a very specific reason" not in stored    # the page said so: a direct add keeps no why
    assert 'name="why"' not in get(s, "/add")[1]                                           # every option is a direct add: no Why field


def test_a_team_folder_is_offered_with_its_own_note_and_written_to(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    team = tmp_path / "co" / "team"
    team.mkdir(parents=True)
    (repo / ".reverie").mkdir(exist_ok=True)
    (repo / ".reverie" / "context.toml").write_text('[sources]\nteam = ["../co/team"]\n')
    s = serve(repo)
    page = get(s, "/add")[1]
    assert '<input type="radio" name="target" value="team|co/team">' in page
    assert ("Everyone whose repos use this folder, from the next session. No CODEOWNERS entry covers it, so there is no review. "
            "Reverie edits the file there and does not commit it.") in page
    out = _follow(s, _post_add(s, "Use the shared logger", target="team|co/team"))
    assert "Added to <code>rules.md</code>" in out and "Use the shared logger" in (team / "rules.md").read_text()


# --- Add: every refusal, by direct POST, writes nothing -----------------------------------------------------------

def test_empty_and_blank_text_is_refused_with_nothing_written(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")                          # an owner: a non-empty rule would have been written
    s = serve(repo)
    tok, pid = _form(s, "/add")
    before = _tree(tmp_path)
    for text in ("", "   ", "\n\t \n"):
        page = _follow(s, _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": text, "kind": "rule", "target": "repo", "why": "w"}))
        assert '<p class="err" id="add-error" role="alert">Nothing was saved: write the rule first.</p>' in page
        assert re.search(r'<textarea[^>]*required aria-invalid="true" aria-describedby="add-error"></textarea>', page)
        assert 'name="why" maxlength="300" value="w"' in page                          # what was typed is kept
    assert _tree(tmp_path) == before


def test_a_secret_is_refused_cleared_and_never_echoed_anywhere(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    s = serve(repo)
    tok, pid = _form(s, "/add")
    before = _tree(tmp_path)
    for fields in ({"text": f"Use token {KEY} for CI", "why": "fine"}, {"text": "Fine rule", "why": f"key {KEY}"}):
        resp = _http(s, "POST", "/do/add", {"token": tok, "p": pid, "kind": "rule", "target": "repo", **fields})
        assert KEY not in resp[1] and KEY not in str(resp[2]) and "sk-ant" not in str(resp[2])
        page = _follow(s, resp)
        assert "Nothing was saved: this looks like it contains a secret. Remove the value and try again. The text was cleared on purpose." in page
        assert KEY not in page and "sk-ant" not in page and "Use token" not in page and "Fine rule" not in page
        assert re.search(r'<textarea[^>]*aria-invalid="true"[^>]*></textarea>', page)
        assert 'name="why" maxlength="300" value=""' in page
        assert "sk-ant" not in get(s, "/add")[1] and "sk-ant" not in get(s, "/context")[1]
    assert _tree(tmp_path) == before


def test_an_unknown_destination_is_refused_keeps_the_text_and_marks_the_choices(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    s = serve(repo)
    tok, pid = _form(s, "/add")
    before = _tree(tmp_path)
    for target in ("team|nowhere/at-all", "personal", "../../etc", "org|x", ""):
        page = _follow(s, _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": "Keep me", "kind": "rule", "target": target, "why": "kept why"}))
        assert 'role="alert">Nothing was saved: this project cannot add to that place. Pick another option.</p>' in page, target
        assert ">Keep me</textarea>" in page and 'value="kept why"' in page
        assert 'name="target" value="repo" checked aria-invalid="true" aria-describedby="add-error"' in page
    assert _tree(tmp_path) == before


def test_a_turned_off_project_refuses_the_add_and_shows_the_off_page(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    s = serve(repo)
    tok, pid = _form(s, "/add")
    assert cli.main(["disable"]) == 0
    before = _tree(tmp_path)
    resp = _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": "Never push to main", "kind": "rule", "target": "repo", "why": "w"})
    assert resp[0] == 303 and "Never push" not in str(resp[2])
    page = _http(s, "GET", resp[2]["Location"])[1]
    assert "Reverie is turned off for svc." in page and "<form" not in _main(page).replace('<form class="find"', "")
    assert _tree(tmp_path) == before and not (repo / ".reverie/context/rules.md").exists()
    assert "Nothing was saved" not in get(s, "/project")[1]                                # the refusal was spent on that page


def test_a_bad_or_missing_token_is_refused_and_nothing_is_saved(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    s = serve(repo)
    tok, pid = _form(s, "/add")
    before = _tree(tmp_path)
    for token in (None, "", "wrong", tok[:-1] + ("A" if tok[-1] != "A" else "B")):
        fields = {"p": pid, "text": "Never push to main", "kind": "rule", "target": "repo"}
        if token is not None:
            fields["token"] = token
        status, body, _ = _http(s, "POST", "/do/add", fields)
        assert status == 403 and "Never push" not in body
    assert _tree(tmp_path) == before


def test_an_unknown_project_is_refused_and_the_board_says_so(env, serve):
    tmp_path, repo = env
    s = serve(repo)
    tok, _ = _form(s, "/add")
    before = _tree(tmp_path)
    resp = _http(s, "POST", "/do/add", {"token": tok, "p": "deadbeef", "text": "R", "kind": "rule", "target": "repo"})
    assert resp[0] == 303 and urlsplit(resp[2]["Location"]).path == "/"
    page = _http(s, "GET", resp[2]["Location"])[1]
    assert '<p class="err" role="alert">Nothing was saved: that project is not known to Reverie.</p>' in page
    assert _tree(tmp_path) == before


def test_hostile_text_in_a_result_message_is_escaped(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    s = serve(repo)
    page = _follow(s, _post_add(s, '<script>alert(1)</script> <img src=x onerror=alert(2)> "q" & more'))
    assert "<script" not in page.lower() and "<img src=x" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in re.search(r'<p class="flash".*?</p>', page, re.S).group(0)


# --- the board ---------------------------------------------------------------------------------------------------

def test_the_board_lists_only_what_waits_on_the_viewer_and_parks_turned_off_projects_at_the_bottom(env, serve, monkeypatch):
    tmp_path, repo = env
    clear, off = _repo(tmp_path / "notifications"), _repo(tmp_path / "legacy-billing")
    for r in (clear, off):
        monkeypatch.chdir(r)
        assert cli.main(["enable"]) == 0
    monkeypatch.chdir(off)
    assert cli.main(["disable"]) == 0
    monkeypatch.chdir(repo)
    context.add(str(repo), "Prefer small PRs", why="faster")                              # dev proposes; lead owns the folder
    context.add(str(repo), "Use the helper", why="shared")
    s = serve(repo)
    page = _main(get(s, "/")[1])
    assert '<p class="tally">Nothing is waiting on you in any project.</p>' in page         # dev owns nothing
    assert "Waiting on you" not in page and 'class="proj"' not in page
    assert page.index('id="g-clear"') < page.index('id="g-off"')
    _as(repo, OWNER, "Lee Lead")
    page = _main(get(s, "/")[1])
    assert '<p class="tally">1 project has something waiting on you.</p>' in page
    assert '<h2 id="g-wait">Waiting on you</h2>' in page and "2 waiting on you: 2 proposals." in page
    assert re.search(r'<p class="proj"><a href="/project\?p=[0-9a-f]{8}">svc</a></p>', page)
    assert page.index('id="g-wait"') < page.index('id="g-clear"') < page.index('id="g-off"')
    assert re.search(r'<ul class="plain"><li><a href="/project\?p=[0-9a-f]{8}">notifications</a></li></ul>', page)
    assert re.search(r'<h2 id="g-off" class="dim">Turned off</h2><ul class="plain dim"><li><a class="dim" href="/project\?p=[0-9a-f]{8}">legacy-billing</a></li></ul>', page)
    assert "<article" not in page and "card" not in page                                   # no per-project cards


def test_the_boards_breakdown_uses_the_same_words_and_counts_as_the_tabs(env, serve):
    tmp_path, repo = env
    context.add(str(repo), "A", why="")                                                   # dev proposes ...
    _as(repo, OWNER, "Lee Lead")                                                          # ... lead owns the folder
    s = serve(repo)
    d = ui.collect(str(repo))
    n, words = ui.waiting_words(ui.waiting(str(repo), d))
    assert n == ui.waiting(str(repo), d)["counts"]["/project"] == 1 and words == "1 waiting on you: 1 proposal."
    assert f'<a href="/project?p={ui._pid(str(repo))}">svc</a></p><p class="why">{words}</p>' in get(s, "/")[1]


# --- Add a project, Connect ----------------------------------------------------------------------------------------

def _steps(body: str) -> list[str]:
    return re.findall(r"<li><h2>(.*?)</h2>", body)


def test_add_a_project_puts_install_first_and_describes_how_a_repo_appears(env, serve):
    s = serve(env[1])
    body = _main(get(s, "/new")[1])
    assert '<h1 class="ask">Add a project</h1>' in body
    assert "Projects are repos. A repo appears here once Reverie has seen it." in body
    assert _steps(body)[1] == "Use Reverie in the repo" and len(_steps(body)) == 2
    assert body.index("reverie install") < body.index("reverie enable")
    assert "Reverie is on by default, so either one makes the repo appear here the next time this page is opened." in body
    assert "<title>Add a project, Reverie</title>" in get(s, "/new")[1]


def test_connect_shows_only_the_steps_that_apply(env, serve):
    tmp_path, repo = env
    s = serve(repo)
    body = _main(get(s, "/connect")[1])
    assert '<p class="tally">Reverie is on for this repo.</p>' in body
    assert _steps(body)[1:] == ["Start a session and check", "Share rules with your team"]
    assert "Turn it on" not in body and "reverie enable" not in body
    assert cli.main(["disable"]) == 0
    body = _main(get(s, "/connect")[1])
    assert '<p class="tally">Reverie is turned off for this repo. Step 1 turns it on.</p>' in body
    assert _steps(body)[0] == "Turn it on for this project" and _steps(body)[2:] == ["Start a session and check", "Share rules with your team"]
    assert "this turns it back on after <code>reverie disable</code>" in body
    assert "Connect an agent to svc, Reverie" in get(s, "/connect")[1]                    # Connect is the one tab that works while off


def test_connect_names_the_agents_install_has_configured_and_none_when_there_are_none(env, serve, monkeypatch):
    s = serve(env[1])
    monkeypatch.setattr(install, "configured_servers", lambda home=None: [])
    assert "No agent is set up on this machine yet." in get(s, "/connect")[1]
    keys = [a.key for a in install.AGENTS][:2]
    monkeypatch.setattr(install, "configured_servers", lambda home=None: [SimpleNamespace(agent=k) for k in keys])
    names = [a.name for a in install.AGENTS][:2]
    assert f"Set up on this machine: {', '.join(names)}." in get(s, "/connect")[1]
    assert install.configured_agent_names() == names                                       # the CLI's own names


def test_connect_step_one_reads_as_not_undone_when_an_agent_is_set_up_and_names_only_what_install_supports(env, serve, monkeypatch):
    s = serve(env[1])
    supported = install.supported_agent_names()
    assert supported == [a.name for a in install.AGENTS] and len(supported) >= 4
    listing = ", ".join(supported[:-1]) + " and " + supported[-1]
    monkeypatch.setattr(install, "configured_servers", lambda home=None: [])
    body = _main(get(s, "/connect")[1])
    assert _steps(body)[0] == "Set up your agent once per machine"
    assert f"This adds Reverie to the agents you use that it finds on this machine ({listing})." in body
    assert "others it finds" not in body and "already set up" not in body
    keys = [a.key for a in install.AGENTS][:2]
    monkeypatch.setattr(install, "configured_servers", lambda home=None: [SimpleNamespace(agent=k) for k in keys])
    body = _main(get(s, "/connect")[1])
    names = [a.name for a in install.AGENTS][:2]
    assert _steps(body)[0] == "Set up another agent"
    assert f"{names[0]} and {names[1]} are already set up. Run this again after you install another agent" in body
    assert f"({listing})" in body and "Set up your agent once per machine" not in body
    monkeypatch.setattr(install, "configured_servers", lambda home=None: [SimpleNamespace(agent=keys[0])])
    assert f"{names[0]} is already set up." in _main(get(s, "/connect")[1])
    assert f"({listing})" in _main(get(s, "/new")[1]) and "Set up another agent" in _main(get(s, "/new")[1])


def test_the_repo_path_is_shell_quoted_even_with_spaces_and_quotes(tmp_path, monkeypatch, serve):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    repo = _repo(tmp_path / "my \"repo's\" $HOME & co")
    monkeypatch.chdir(repo)
    assert cli.main(["enable"]) == 0
    s = serve(repo)
    real = ui.projects(str(repo))[0]["path"]
    assert cli.main(["disable"]) == 0
    for url in ("/connect", "/project"):                                                   # the off page and Connect both show it
        body = get(s, url)[1]
        cmd = html.unescape(re.search(r'<pre class="block-cmd">(cd .*? &amp;&amp; reverie enable)</pre>', body).group(1))
        assert shlex.split(cmd) == ["cd", real, "&&", "reverie", "enable"], url
        assert "'" in cmd and "$HOME" in cmd                                                # quoted, not expanded
    assert "<script" not in body


# --- Search --------------------------------------------------------------------------------------------------------

def _search_env(env):
    tmp_path, repo = env
    ctx = repo / ".reverie" / "context"
    ctx.mkdir(parents=True)
    (ctx / "rules.md").write_text("## Rules\n- Ledger rows are append-only\n- Money is integer cents\n- Never render <b>bold</b> in logs\n")
    conn = items.connect(deliver.db_path())
    key = deliver.repo_key(str(repo))
    items.add(conn, repo_key=key, text="Refunds need approval above 10,000", kind="decision", source="user_said")
    items.add(conn, repo_key=key, text="Never run migrations on the replica", kind="rule", source="user_said")
    conn.close()
    return repo


def test_search_groups_rules_and_memory_keeps_the_query_and_links_to_pages(env, serve):
    repo = _search_env(env)
    s = serve(repo)
    page = get(s, "/search?q=ledger")[1]
    assert '<p class="tally">1 result for “ledger”.</p>' in page and 'value="ledger"' in page      # kept in the box
    assert '<h2 id="g-r">Rules</h2>' in page and 'id="g-m"' not in page
    link = re.search(r'<a class="open" href="(/rule\?p=[0-9a-f]{8}&amp;id=[0-9a-f]+)">Open rule</a>', page).group(1)
    assert "Ledger rows are append-only" in get(s, html.unescape(link))[1]
    page = get(s, "/search?q=refund")[1]
    assert '<p class="tally">1 result for “refund”.</p>' in page and '<h2 id="g-m">Memory</h2>' in page and 'id="g-r"' not in page
    assert '<p class="label">Decision</p>' in page
    mlink = re.search(r'<a class="open" href="(/rule\?p=[0-9a-f]{8}&amp;id=[0-9a-f]{8})">Open</a>', page).group(1)
    assert get(s, html.unescape(mlink))[0] == 200
    page = get(s, "/search?q=never")[1]                                                       # a team rule and a remembered rule
    assert "2 results for “never”." in page and page.count('class="open"') == 2 and '<h2 id="g-r">Rules</h2>' in page
    assert "<title>Search, Reverie</title>" in page and 'aria-current="page"' not in _main(page)


def test_search_empty_and_blank_states(env, serve):
    repo = _search_env(env)
    s = serve(repo)
    page = _main(get(s, "/search?q=kubernetes")[1])
    assert '<p class="tally">No rules or memory items match “kubernetes”.</p>' in page
    assert "Search looks for those words in the text of team rules and remembered items." in page
    for url in ("/search?q=", "/search", "/search?q=%20%20"):
        page = _main(get(s, url)[1])
        assert '<p class="tally">Type something to search.</p>' in page
        assert "Searches the team rules and the remembered items of this project." in page


HOSTILE = ['<script>alert(1)</script>', '"><img src=x onerror=alert(2)>', "<b>bold</b>", "a & b ' \" <"]


@pytest.mark.parametrize("q", HOSTILE)
def test_html_in_a_query_is_escaped_everywhere_it_appears(env, serve, q):
    repo = _search_env(env)
    s = serve(repo)
    from urllib.parse import quote
    status, page, _ = get(s, f"/search?q={quote(q)}")
    assert status == 200
    assert "<script" not in page.lower() and "<img src=x" not in page and "<b>bold</b>" not in page
    safe = html.escape(q, quote=True)
    assert page.count(safe) >= 2                                                           # the box and the sentence under it
    assert re.search(r'<input name="q"[^>]* value="' + re.escape(safe) + '">', page)
    if q == "<b>bold</b>":                                                                 # a rule whose own text contains it: matched, shown escaped
        assert "Never render &lt;b&gt;bold&lt;/b&gt; in logs" in page


def test_a_long_query_is_cut_to_the_boxs_limit(env, serve):
    repo = _search_env(env)
    s = serve(repo)
    page = get(s, "/search?q=" + "x" * 500)[1]
    assert "x" * 120 in page and "x" * 121 not in page


# --- Turned off ---------------------------------------------------------------------------------------------------

def test_every_project_tab_but_connect_shows_the_off_page_and_leaks_nothing(env, serve):
    repo = _search_env(env)
    s = serve(repo)
    pid = ui._pid(str(repo))
    assert cli.main(["disable"]) == 0
    cmd = f"cd {shlex.quote(ui.projects(str(repo))[0]['path'])} &amp;&amp; reverie enable"
    for path in ("/project", "/context", "/memory", "/start", "/proposals", "/add", "/search?q=ledger", "/rule?id=abc123"):
        sep = "&" if "?" in path else "?"
        status, page, _ = get(s, f"{path}{sep}p={pid}")
        main = _main(page)
        assert status == 200 and "Reverie is turned off for svc." in main, path
        assert "Nothing is told to agents and nothing is shown here." in main and cmd in main, path
        assert "Ledger rows" not in page and "Refunds need" not in page and "<form class=\"addf\"" not in page, path
        assert "<title>svc, Reverie</title>" in page and '<h1 class="visually-hidden">Turned off for svc</h1>' in page
        assert '<span class="n">' not in page                                              # no counts: nothing is read
        assert "Add a rule" not in page and 'class="acts"' not in page                     # no way to start a write from here
    assert "Reverie is turned off for this repo. Step 1 turns it on." in get(s, f"/connect?p={pid}")[1]
    tabs = re.findall(r'<nav class="tabs".*?</nav>', get(s, f"/context?p={pid}")[1], re.S)[0]
    assert 'aria-current="page">Rules' in tabs                                              # the tab you are on stays marked


def test_add_and_search_of_a_turned_off_project_read_nothing_from_it(env, serve, monkeypatch):
    repo = _search_env(env)
    s = serve(repo)
    pid = ui._pid(str(repo))
    assert cli.main(["disable"]) == 0

    def boom(*a, **k):
        raise AssertionError("read the project")
    for name in ("collect", "waiting", "known_people", "rule_records", "add_targets", "search_page", "add_page"):
        monkeypatch.setattr(ui, name, boom)
    monkeypatch.setattr(ui.context, "resolve", boom)
    monkeypatch.setattr(ui.deliver, "memory", boom)
    for url in (f"/add?p={pid}", f"/search?p={pid}&q=ledger", f"/search?p={pid}&q=<b>"):
        status, page, _ = get(s, url)
        assert status == 200 and "Reverie is turned off for svc." in page, url
        assert "<form class=\"addf\"" not in page and "<textarea" not in page and "result" not in _main(page), url
        assert 'name="q" type="search"' in page and "ledger" not in _main(page)
    # Connect, the one tab that works while off, also offers no Add
    status, page, _ = get(s, f"/connect?p={pid}")
    assert status == 200 and "Add a rule" not in page


# --- Not found, Something went wrong ----------------------------------------------------------------------------------

def test_the_board_and_not_found_have_a_hidden_h1(env, serve):
    s = serve(env[1])
    board, nf = get(s, "/")[1], get(s, "/nosuchpage")[1]
    assert board.count("<h1") == 1 and '<h1 class="visually-hidden">All projects</h1>' in board
    assert nf.count("<h1") == 1 and '<h1 class="visually-hidden">Not found</h1>' in nf
    new = get(s, "/new")[1]                                                                # Add a project already has its visible h1
    assert new.count("<h1") == 1 and '<h1 class="ask">Add a project</h1>' in new


def test_not_found_is_its_own_page_with_a_way_back(env, serve):
    s = serve(env[1])
    for path in ("/nosuchpage", "/project/extra", "/do/add", "/rules"):
        status, page, _ = get(s, path)
        assert status == 404 and '<p class="tally">That page does not exist.</p><p class="go"><a href="/">Back to all projects</a></p>' in page
        assert "<title>Not found, Reverie</title>" in page and "svc" in page                # the project switcher still works


BOOM = "SECRET-DETAIL /Users/someone/private/module.py line 12 sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA"


WRITE_WORDS = "Your change may or may not have been saved. Check Rules and Proposals before trying again."


def _assert_a_plain_500(status, body, tmp_path, writing=False):
    assert status == 500
    assert "<p class=\"tally\">Something went wrong.</p>" in body
    if writing:
        assert f'<p class="who">{WRITE_WORDS}</p>' in body and "could not be built" not in body
    else:
        assert '<p class="who">This page could not be built.</p>' in body and "may or may not" not in body
    assert "The CLI still works" not in body and "<hr>" not in body                         # no projects above it: no divider
    assert '<pre class="block-cmd">reverie status</pre>' in body and '<a href="/">Back to all projects</a>' in body
    for leak in ("SECRET-DETAIL", "Traceback", "RuntimeError", "module.py", "/Users/someone", "sk-ant", "ui.py", str(tmp_path), "line 12"):
        assert leak not in body, leak
    assert "<title>Something went wrong, Reverie</title>" in body


def test_an_exception_in_a_page_handler_gives_a_500_with_no_trace_and_no_exception_text(env, serve, monkeypatch):
    tmp_path, repo = env
    s = serve(repo)
    pid = ui._pid(str(repo))

    def boom(*a, **k):
        raise RuntimeError(BOOM)
    for path in ("/context", "/connect", "/project", "/add"):
        monkeypatch.setitem(ui.PROJECT_PAGES, path, boom)
        status, body, headers = get(s, f"{path}?p={pid}")
        _assert_a_plain_500(status, body, tmp_path)
        assert "default-src 'none'" in headers["Content-Security-Policy"]
    monkeypatch.undo()
    monkeypatch.setattr(ui, "collect", boom)                                               # deeper: the data layer
    for path in ("/", "/memory", "/start", "/proposals", "/search?q=x"):
        sep = "&" if "?" in path else "?"
        _assert_a_plain_500(*get(s, f"{path}{sep}p={pid}")[:2], tmp_path)
    status, body, _ = get(s, "/")
    _assert_a_plain_500(status, body, tmp_path)


def test_an_exception_while_writing_is_a_500_page_too_and_changes_nothing(env, serve, monkeypatch):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    s = serve(repo)
    tok, pid = _form(s, "/add")
    before = _tree(tmp_path)

    def boom(*a, **k):
        raise RuntimeError(BOOM)
    monkeypatch.setattr(ui, "do_write", boom)
    status, body, _ = _http(s, "POST", "/do/add", {"token": tok, "p": pid, "text": "R", "kind": "rule", "target": "repo"})
    _assert_a_plain_500(status, body, tmp_path, writing=True)
    assert _tree(tmp_path) == before


def test_the_menu_has_a_divider_only_when_projects_are_listed_above_it(env):
    tmp_path, repo = env
    none = ui.layout(title="t", proj=None, plist=[], body="")
    assert "<hr>" not in none and '<ul class="menu"><li><a href="/">All projects</a></li>' in none
    for page in (ui.error_page(), ui.error_page(writing=True)):
        assert "<hr>" not in page
    some = ui.layout(title="t", proj=None, plist=ui.projects(str(repo)), body="")
    assert some.count("<hr>") == 1 and some.index("svc</a></li>") < some.index("<hr>") < some.index("All projects</a></li>")
    assert "<hr>" in ui.render(str(repo), "/", {})[1]


# --- every internal link on every ported page resolves --------------------------------------------------------------

def test_every_internal_link_on_every_ported_page_resolves(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    _search_env(env)
    context.add(str(repo), "Proposal to read", why="reasons")
    s = serve(repo)
    pid = ui._pid(str(repo))
    pages = ["/", "/new", *(f"{p}?p={pid}" for p in ("/project", "/context", "/memory", "/start", "/proposals", "/connect", "/add")),
             f"/search?p={pid}&q=never", f"/search?p={pid}&q=refund", f"/search?p={pid}&q=", "/nosuchpage"]
    seen: dict[str, int] = {}
    for page in pages:
        for href in re.findall(r'href="(/[^"#]*)', get(s, page)[1]):
            href = html.unescape(href)
            if href not in seen:
                seen[href] = get(s, href)[0]
    assert seen and all(code == 200 for code in seen.values()), {h: c for h, c in seen.items() if c != 200}
    assert {re.sub(r"\?.*", "", h) for h in seen} >= {"/", "/new", "/project", "/context", "/memory", "/start", "/proposals",
                                                      "/connect", "/add", "/rule"}
