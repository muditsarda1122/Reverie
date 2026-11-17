"""`reverie ui`, the Ink design (design.md): the shared shell, Overview and Proposals, people shown by name,
and the rule that hiding a form is never the check: every action the page does not draw is still refused."""

import re
import subprocess
import threading
from pathlib import Path

import pytest
from test_ui_writes import _follow, _form, _http, _tree

from reverie import context, ui

OWNER = "lead@acme.com"


def _repo(path: Path, email: str, name: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for k, v in (("user.email", email), ("user.name", name)):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
    (path / "CODEOWNERS").write_text(f"/.reverie/context/ {OWNER}\n")
    return path


def _as(repo: Path, email: str, name: str) -> None:
    for k, v in (("user.email", email), ("user.name", name)):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    return tmp_path, _repo(tmp_path / "svc", "dev@acme.com", "Dana Dev")      # dev is NOT an owner


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


def _propose(repo: Path, text="Prefer small PRs") -> dict:
    context.add(str(repo), text, why="reviews are faster")
    return context.proposals(str(repo))[0]


def _tab_counts(body: str) -> dict:
    return {label: int(n) for label, n in re.findall(r'>(Overview|Proposals)<span class="n">(\d+)</span>', body)}


# --- an unknown project is a 404, never another project ---------------------------------------------------

def test_an_unknown_project_id_is_a_404_and_never_falls_back(env, serve):
    tmp_path, repo = env
    other = _repo(tmp_path / "other", "x@y.z", "X")
    (other / ".reverie").mkdir()
    (other / ".reverie" / "rules.md").write_text("- SECRET-OTHER-REPO-RULE\n")
    s = serve(repo)
    for url in ("/project?p=deadbeef", "/context?p=deadbeef", "/proposals?p=deadbeef", "/memory?p=deadbeef",
                "/start?p=deadbeef", "/add?p=deadbeef", "/search?q=x&p=deadbeef", f"/project?p={other}", "/project?p=../../etc"):
        status, body, _ = _http(s, "GET", url)
        assert status == 404 and "No project with that id." in body, url
        assert "SECRET-OTHER-REPO-RULE" not in body and 'class="tabs"' not in body
    assert _http(s, "GET", "/project")[0] == 200                       # no p: the project it was started in
    known = re.search(r'/project\?p=([0-9a-f]{8})', _http(s, "GET", "/project")[1]).group(1)
    assert _http(s, "GET", f"/project?p={known}")[0] == 200


# --- people: names, with the email in the title, "you" for the viewer -----------------------------------------

def test_names_come_from_proposals_and_commits_and_unknown_emails_stay_emails(env):
    tmp_path, repo = env
    _propose(repo)                                              # proposed-by: Dana Dev <dev@acme.com>
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Lee Lead", "-c", "user.email=" + OWNER, "commit",
                    "--allow-empty", "-q", "-m", "x"], check=True)
    names = ui.known_people(str(repo))
    assert names["dev@acme.com"] == "Dana Dev" and names[OWNER] == "Lee Lead"
    me = ui.Viewer(str(repo))
    assert ui.person_html(OWNER, names, me) == '<span title="lead@acme.com">Lee Lead</span>'
    assert ui.person_html("dev@acme.com", names, me) == '<span title="dev@acme.com">Dana (you)</span>'
    assert ui.person_html("Dana Dev <dev@acme.com>", names, me) == '<span title="dev@acme.com">Dana (you)</span>'
    assert ui.person_html("ghost@nowhere.io", names, me) == "ghost@nowhere.io"           # no name known: the email
    assert ui.people_html([OWNER, "dev@acme.com"], names, me) == (
        '<span title="lead@acme.com">Lee Lead</span>, <span title="dev@acme.com">Dana (you)</span>')
    assert ui.person_html("<b>x</b>@a.io", names, me) == "&lt;b&gt;x&lt;/b&gt;@a.io"     # hostile text stays escaped


def test_pages_show_names_not_emails_and_the_email_only_in_a_title(env, serve):
    tmp_path, repo = env
    _propose(repo)
    s = serve(repo)
    body = _http(s, "GET", "/proposals")[1]
    assert 'Waiting on an owner of <code>rules.md</code>: lead@acme.com.' in body        # no name is known for the owner
    assert '<span title="dev@acme.com">Dana (you)</span>' in body
    assert "&lt;dev@acme.com&gt;" not in body
    assert 'Acting as <span title="dev@acme.com">Dana Dev</span>, from your git identity.' in body


# --- Overview and Proposals share one definition of "waiting on you" -----------------------------------------

def test_tab_counts_are_what_waits_on_the_viewer_and_vanish_at_zero(env, serve):
    tmp_path, repo = env
    _propose(repo)
    s = serve(repo)
    over, props = _http(s, "GET", "/project")[1], _http(s, "GET", "/proposals")[1]
    assert _tab_counts(over) == {} and _tab_counts(props) == {}                         # dev decides nothing
    assert "Nothing waiting on you. 1 waiting on others." in props and "Nothing waiting on you. 1 waiting on others." in over
    assert "Waiting on you" not in over and "Waiting on you" not in props               # an empty section is omitted
    assert "Waiting on others" in over and ">View<" in over and ">Decide<" not in over
    _as(repo, OWNER, "Lee Lead")
    over, props = _http(s, "GET", "/project")[1], _http(s, "GET", "/proposals")[1]
    assert _tab_counts(over) == {"Overview": 1, "Proposals": 1} == _tab_counts(props)
    assert "1 waiting on you." in over and ">Decide<" in over and "Waiting on others" not in over
    assert 'Acting as <span title="lead@acme.com">Lee Lead</span>' in over


def test_overview_puts_conflicts_and_outdated_rules_in_one_list_with_a_link_each(env, serve):
    tmp_path, repo = env
    _as(repo, OWNER, "Lee Lead")
    ctxdir = repo / ".reverie" / "context"
    ctxdir.mkdir(parents=True)
    (ctxdir / "rules.md").write_text("## Rules\n\n- Use the helper in `app/retry.py` for every call\n")
    for team in ("platform", "payments"):                  # two sibling team folders tag one rule: an unresolved conflict
        (tmp_path / "orgdocs" / team).mkdir(parents=True)
        (tmp_path / "orgdocs" / team / "rules.md").write_text(
            f"## Rules\n\n- {'Retry three times' if team == 'platform' else 'Never retry'} {{id: retry}}\n")
    (repo / ".reverie" / "context.toml").write_text('[sources]\nteam = ["../orgdocs/platform", "../orgdocs/payments"]\n')
    res = context.resolve(str(repo))
    assert [c.status for c in res.conflicts] == ["unresolved"]
    st = context.load_state()
    helper = next(i for i in res.items if "helper" in i.text)
    st["stale"] = {helper.id: {"commit": "abcdef0123", "message": "", "reason": "the retry helper is gone", "file": ""}}
    context.save_state(st)
    s = serve(repo)
    over = _http(s, "GET", "/project")[1]
    assert over.count('class="label"') == 2 and ">Conflict<" in over and ">Possibly outdated<" in over
    assert "No CODEOWNERS entry covers either document, so anyone can settle it." in over
    assert "Possibly outdated" in over and "the retry helper is gone" in over
    assert re.search(r'Commit <code>abcdef0</code></span>', over)                       # no message: the hash alone
    assert re.search(r'<a href="/rule\?p=[0-9a-f]{8}&amp;id=[0-9a-f]+">Open rule</a>', over)
    assert 'class="rule"' in over and '<h3 class="rule"><a' not in over                 # rule text is never a link


# --- hiding a form is not a permission check ------------------------------------------------------------------

def test_a_viewer_who_cannot_decide_gets_no_form_and_a_direct_post_is_still_refused(env, serve):
    tmp_path, repo = env
    prop = _propose(repo)                                         # dev is not an owner
    s = serve(repo)
    page = _http(s, "GET", "/proposals")[1]
    assert "/do/accept" not in page and "/do/reject" not in page and prop["slug"] not in page.replace("#p-", "")
    assert "Waiting on an owner of" in page
    tok, pid = _form(s, "/add")                                   # the token still comes from a page that has a form
    before = _tree(tmp_path)
    out = _follow(s, _http(s, "POST", "/do/accept", {"token": tok, "p": pid, "slug": prop["slug"]}))
    assert "only the owners of rules.md can accept" in out
    out = _follow(s, _http(s, "POST", "/do/reject", {"token": tok, "p": pid, "slug": prop["slug"], "why": "no"}))
    assert "only the owners of rules.md can reject" in out
    assert _tree(tmp_path) == before and context.proposals(str(repo))


def test_an_owner_gets_the_forms_and_accepting_quotes_the_rule(env, serve):
    tmp_path, repo = env
    prop = _propose(repo, "Prefer small PRs")
    _as(repo, OWNER, "Lee Lead")
    s = serve(repo)
    page = _http(s, "GET", "/proposals")[1]
    assert 'action="/do/accept"' in page and 'action="/do/reject"' in page and 'name="slug"' in page
    tok, pid = _form(s, "/proposals")
    out = _follow(s, _http(s, "POST", "/do/accept", {"token": tok, "p": pid, "slug": prop["slug"]}))
    assert "Accepted into rules.md: “Prefer small PRs”" in out and '<span class="in">in force</span>' in out
