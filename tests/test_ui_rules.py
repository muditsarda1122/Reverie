"""`reverie ui`, stage 2 of the design port: Rules, the Rule page (a real route with its 404), What agents are told and
Memory. One record per rule feeds them all, and the told/cut state comes from the start-text trace, so the pages cannot
disagree about how many rules agents are given."""

import html
import re
import subprocess
import threading
from pathlib import Path

import pytest
from test_ui_writes import _http

from reverie import cli, context, deliver, items, rules, ui


def _repo(path: Path, email="lead@acme.com", name="Lee Lead") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for k, v in (("user.email", email), ("user.name", name)):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    repo = _repo(tmp_path / "svc")
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


def _mem(repo, **kw):
    conn = items.connect(deliver.db_path())
    iid = items.add(conn, repo_key=deliver.repo_key(str(repo)), **kw)
    conn.close()
    return iid


def _ctx_docs(tmp_path, repo):
    """A repo document, two sibling team folders that conflict, and a CODEOWNERS entry for the repo's documents."""
    (repo / "CODEOWNERS").write_text("/.reverie/context/ lead@acme.com\n")
    ctx = repo / ".reverie" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "rules.md").write_text("## Rules\n- Money is integer cents\n- Use `app/retry.py` for every retry\n")
    (ctx / "paths-ledger.md").write_text("applies-to-paths: services/ledger/**\n## Rules\n- Ledger rows are append-only (locked)\n")
    for team in ("platform", "payments"):
        (tmp_path / "co" / team).mkdir(parents=True, exist_ok=True)
        (tmp_path / "co" / team / "rules.md").write_text(
            f"## Rules\n- {'Retry three times' if team == 'platform' else 'Never retry'} {{id: retry}}\n")
    (repo / ".reverie" / "context.toml").write_text('[sources]\nteam = ["../co/platform", "../co/payments"]\n')


def _stale(repo, text_part, **extra):
    res = context.resolve(str(repo))
    it = next(i for i in res.items if text_part in i.text)
    st = context.load_state()
    st["stale"] = {it.id: {"commit": "abcdef0123", "message": "retry: drop the helper", "reason": "the helper is gone",
                           "file": it.file, **extra}}
    context.save_state(st)
    return it


def _rule_links(body: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="(/rule\?[^"]+)"', body)]


# --- Rules and What agents are told count the same rules -------------------------------------------------------

def test_rules_and_start_give_the_same_numbers_for_the_capped_seed(env, serve):
    _, repo = env
    for n in range(35):
        _mem(repo, text=f"Repo rule number {n:02d} about thing {n}", kind="rule", source="user_said")
    rules.add("Prefer rg over grep", level="user")
    s = serve(repo)
    told, cut = ui.told_counts(ui.collect(str(repo)))
    assert (told, cut) == (31, 5)                                  # 30 remembered rules + 1 personal; 5 cut by the 30-line limit
    rules_page, start = get(s, "/context")[1], get(s, "/start")[1]
    assert f'{told} rules <span class="in">in force</span>, {cut} more cut by the start limit.' in rules_page
    assert f"{told} rules are told to agents." in start and f"{cut} more rules apply but were cut, because " in start
    assert "agents are told at most 30 remembered rules for this repo" in start               # the real reason, from the trace
    assert "agents are told at most 30 remembered rules for this repo" in rules_page         # the same reason on every cut rule
    assert rules_page.count("Cut by the start limit</span>") == cut                          # exactly the cut rules are marked
    for r in deliver.startup_report(str(repo))[1].rules:
        assert r["told"] == (r["text"] in deliver.startup_text(str(repo), log=False))        # the page says what the text says


def test_a_character_cut_is_reported_as_characters_not_lines(env, serve):
    _, repo = env
    for n in range(25):
        _mem(repo, text=f"Rule {n:02d} " + "x" * 280, kind="rule", source="user_said")
    s = serve(repo)
    start = get(s, "/start")[1]
    assert "characters of remembered rules for this repo" in start and "at most 30 remembered rules" not in start.split("<pre")[0].split("Cut by the start limit")[0]
    assert "Cut by the start limit</h2>" in start


def test_start_page_numbers_the_characters_with_the_configured_limit(env, serve, monkeypatch):
    _, repo = env
    _mem(repo, text="Always paginate list endpoints", kind="rule", source="user_said")
    monkeypatch.setenv("REVERIE_STARTUP_CHARS", "6000")
    start = get(serve(repo), "/start")[1]
    assert re.search(r"[\d,]+ of 6,000 characters\.", start)


# --- Rules: groups, states, the one place each rule is listed ---------------------------------------------------

def test_rules_groups_states_and_unmarked_in_force(env, serve):
    tmp_path, repo = env
    _ctx_docs(tmp_path, repo)
    _stale(repo, "app/retry.py")
    rules.add("Squash merge only", level="repo", cwd=str(repo))
    rules.add("Prefer rg over grep", level="user")
    _mem(repo, text="Never run migrations on the replica", kind="rule", source="user_said")
    s = serve(repo)
    page = get(s, "/context")[1]
    for h in ("Team rules", "Shared rules file", "Your rules", "Remembered"):
        assert f">{h}</h2>" in page
    assert page.index("Team folder <code>co/platform</code>") < page.index("This repo, <code>.reverie/context</code>")
    assert page.count(">Conflict</span>") == 2 and page.count(">Possibly outdated</span>") == 1
    assert "A later change to the code may contradict it: the helper is gone." in page
    assert "Disagrees with" in page and "Agents are told to ask before acting on either." in page
    assert "in force</span>" in page and page.count('class="in"') == 1                    # the teal word is in the summary only
    assert "Money is integer cents" in page and "only under <code>services/ledger/**</code>" in page and "<span>locked</span>" in page
    assert 'class="n"' not in page.split("</nav>")[0].split("Rules")[1][:30]              # no count on the Rules tab
    assert "No rules apply here" not in page


def test_a_personal_copy_of_a_team_rule_is_marked_hidden_and_a_narrower_rule_overrides(env, serve):
    tmp_path, repo = env
    (repo / ".reverie").mkdir(exist_ok=True)
    (repo / ".reverie" / "context").mkdir(parents=True)
    (repo / ".reverie" / "context" / "rules.md").write_text("## Rules\n- Money is integer cents\n- Log in JSON {id: log}\n")
    (tmp_path / "co" / "team").mkdir(parents=True)
    (tmp_path / "co" / "team" / "rules.md").write_text("## Rules\n- Plain text logs are fine {id: log}\n")
    (repo / ".reverie" / "context.toml").write_text('[sources]\nteam = ["../co/team"]\n')
    context.add(str(repo), "Money is integer cents", layer="personal")                    # an exact copy, in the personal layer
    s = serve(repo)
    page = get(s, "/context")[1]
    assert page.count("Hidden: same as a team rule</span>") == 1 and "Agents are told the team rule, not this copy." in page
    assert ">Overridden</span>" in page and "Overridden by “Log in JSON”" in page and "it is more specific, so it answers this one" in page
    assert "Overrides “Plain text logs are fine”" in page                                  # the winner says what it beats


# --- the Rule page --------------------------------------------------------------------------------------------

def test_every_rule_link_on_every_ported_page_resolves(env, serve):
    tmp_path, repo = env
    _ctx_docs(tmp_path, repo)
    _stale(repo, "app/retry.py")
    context.add(str(repo), "Prefer small PRs", why="x")                                   # lead owns it: direct add
    _mem(repo, text="Keep handlers short", kind="preference", source="user_said")
    _mem(repo, text="We chose the outbox", kind="decision", source="user_said", anchor_={"type": "file", "files": ["app/o.py"], "symbols": []})
    iid = _mem(repo, text="Refunds need approval", kind="decision", source="agent_inferred", evidence="policy.py",
               meta={"usage": {"USED": 3, "CONTRADICTED": 2}, "challenged": True})
    rules.add("Squash merge only", level="repo", cwd=str(repo))
    rules.add("Prefer rg over grep", level="user")
    s = serve(repo)
    links = set()
    for page in ("/project", "/context", "/start", "/memory"):
        body = get(s, page)[1]
        links |= set(_rule_links(body))
    assert len(links) >= 10
    for link in sorted(links):
        status, body, _ = get(s, link)
        assert status == 200 and "No rule with that id" not in body, link
        assert re.fullmatch(r"/rule\?p=[0-9a-f]{8}&id=[0-9A-Za-z_-]+", link), link         # safe in a URL, nothing to escape
    assert any(iid[:8] in link for link in links)


def test_an_unknown_or_hostile_rule_id_is_a_404_that_echoes_nothing(env, serve):
    tmp_path, repo = env
    _ctx_docs(tmp_path, repo)
    s = serve(repo)
    pid = re.search(r"p=([0-9a-f]{8})", get(s, "/context")[1]).group(1)
    for rid in ("nosuchrule", "", "<script>alert(1)</script>", '"><img src=x onerror=alert(1)>', "a" * 200, "../../etc/passwd",
                "%3Cscript%3E", "id;rm -rf", "é中"):
        from urllib.parse import quote
        status, body, _ = get(s, f"/rule?p={pid}&id={quote(rid)}")
        assert status == 404 and "No rule with that id in svc." in body and "Back to Rules" in body, rid
        assert "alert(1)" not in body and "onerror" not in body and "passwd" not in body
        assert rid == "" or rid not in html.unescape(body)                                  # the id is never echoed back
    assert get(s, f"/rule?p={pid}")[0] == 404                                              # no id at all
    assert get(s, "/rule?p=deadbeef&id=x")[0] == 404                                       # an unknown project is its own 404


def test_the_team_rule_page_says_where_it_comes_from(env, serve):
    tmp_path, repo = env
    _ctx_docs(tmp_path, repo)
    s = serve(repo)
    res = context.resolve(str(repo))
    it = next(i for i in res.items if "append-only" in i.text)
    body = get(s, f"/rule?p={ui._pid(str(repo))}&id={it.id}")[1]
    assert '<h1 class="bigrule">Ledger rows are append-only' in body and 'class="status"' not in body      # in force: no status
    assert "Back to Rules</a>" in body and '<a href="/context?p=' in body
    for want in ("This repo", ".reverie/context", ".reverie/context/paths-ledger.md", "Rules", "lead@acme.com",
                 "only under <code>services/ledger/**</code>", "It cannot be overridden inside its scope."):
        assert want in body, want
    assert body.count("<h1") == 1 and 'aria-current="page">Rules' in body
    assert f"reverie show {it.id}" in body


def test_a_conflict_page_shows_the_other_rule_the_reason_and_the_resolve_command(env, serve):
    tmp_path, repo = env
    _ctx_docs(tmp_path, repo)
    s = serve(repo)
    res = context.resolve(str(repo))
    it = next(i for i in res.items if i.text == "Retry three times")
    body = get(s, f"/rule?p={ui._pid(str(repo))}&id={it.id}")[1]
    assert 'class="status">Conflict' in body and "Never retry" in body
    assert "Conflict <code>retry</code>: both come from team folders at the same level, so neither one wins." in body
    assert "Agents are told to ask before acting on either." in body and 'reverie context resolve retry "your decision"' in body
    assert "Team folder co/payments" in body


def test_an_outdated_rule_page_shows_reason_commit_message_and_what_to_do(env, serve):
    tmp_path, repo = env
    _ctx_docs(tmp_path, repo)
    it = _stale(repo, "app/retry.py")
    body = get(serve(repo), f"/rule?p={ui._pid(str(repo))}&id={it.id}")[1]
    assert 'class="status">Possibly outdated' in body and "the helper is gone" in body
    assert "<code>abcdef0</code> retry: drop the helper" in body                              # the commit message, from the stale entry
    assert "Check the code, then edit or remove the rule in <code>.reverie/context/rules.md</code>." in body
    st = context.load_state()
    st["stale"][it.id]["message"] = ""                                                         # no message stored: the hash alone
    context.save_state(st)
    body = get(serve(repo), f"/rule?p={ui._pid(str(repo))}&id={it.id}")[1]
    assert "<code>abcdef0</code></dd>" in body


def test_a_locked_rule_overridden_page_names_the_winner_and_why(env, serve):
    tmp_path, repo = env
    (repo / ".reverie" / "context").mkdir(parents=True)
    (repo / ".reverie" / "context" / "rules.md").write_text("## Rules\n- Plain logs in dev {id: log}\n")
    (tmp_path / "co" / "org").mkdir(parents=True)
    (tmp_path / "co" / "org" / "rules.md").write_text("## Rules\n- All services log JSON {id: log} (locked)\n")
    (repo / ".reverie" / "context.toml").write_text('[sources]\norg = ["../co/org"]\n')
    s = serve(repo)
    res = context.resolve(str(repo))
    lo = next(i for i in res.items if "Plain logs" in i.text)
    body = get(s, f"/rule?p={ui._pid(str(repo))}&id={lo.id}")[1]
    assert 'class="status">Overridden' in body and "Overridden by this rule." in body and "All services log JSON" in body
    assert "Why it wins: it is locked, so it wins everywhere." in body
    win = next(i for i in res.items if "All services" in i.text)
    body = get(s, f"/rule?p={ui._pid(str(repo))}&id={win.id}")[1]
    assert "<h2>Overrides</h2>" in body and "Plain logs in dev" in body and "Locked</dt>" in body


def test_remembered_rule_decision_and_outdated_pages(env, serve):
    tmp_path, repo = env
    rid = _mem(repo, text="Never run migrations on the replica", kind="rule", source="user_said")
    did = _mem(repo, text="Refunds need approval", kind="decision", source="agent_inferred", evidence="approval gate in `policy.py`",
               anchor_={"type": "file", "files": ["app/refunds/policy.py"], "symbols": []}, grounding_commit="1234567890abcdef",
               meta={"usage": {"USED": 3, "CONTRADICTED": 2}, "challenged": True})
    oid = _mem(repo, text="Events go through a queue", kind="decision", source="user_said",
               meta={"stale": {"commit": "fedcba9876", "message": "events: use the outbox", "reason": "the queue is gone"}})
    conn = items.connect(deliver.db_path())
    conn.execute("UPDATE items SET last_verified=? WHERE id=?", ("2026-11-10T09:00:00+0000", did))
    conn.commit()
    conn.close()
    s = serve(repo)
    pid = ui._pid(str(repo))
    body = get(s, f"/rule?p={pid}&id={rid[:8]}")[1]                                            # a remembered rule: Rules is active
    assert "Back to Rules</a>" in body and 'aria-current="page">Rules' in body and "<h2>Remembered rule</h2>" in body
    assert "You said it" in body and f"reverie undo {rid[:8]} --restore" in body and f"reverie promote {rid[:8]} --demote" in body
    body = get(s, f"/rule?p={pid}&id={did[:8]}")[1]                                            # a decision: Memory is active
    assert "Back to Memory</a>" in body and '<a href="/memory?p=' in body and 'aria-current="page">Memory' in body
    assert "<h2>Remembered decision</h2>" in body and 'class="status">Challenged' in body
    assert "An agent inferred it" in body and '<span class="quote">“approval gate in <code>policy.py</code>”</span>' in body
    assert "<code>app/refunds/policy.py</code>" in body and "<code>1234567</code>" in body          # anchors and the grounding commit
    assert "Last verified</dt><dd>2026-11-10" in body and "Used by agents</dt><dd>Used 3 times, contradicted twice." in body
    assert "Challenged: agents contradicted it repeatedly" in body and "reverie usage" in body
    assert "Remembered</dt><dd>20" in body                                                         # created_at, as a date
    body = get(s, f"/rule?p={pid}&id={oid[:8]}")[1]                                            # possibly outdated
    assert 'class="status">Possibly outdated' in body and "the queue is gone" in body and f"reverie stale keep {oid[:8]}" in body
    assert "<code>fedcba9</code> events: use the outbox" in body


def test_rules_file_bullets_have_a_stable_id_and_their_own_page(env, serve):
    _, repo = env
    rules.add("Squash merge only", level="repo", cwd=str(repo))
    rules.add("Prefer rg over grep", level="user")
    s = serve(repo)
    pid = ui._pid(str(repo))
    assert ui.bullet_id("Squash  merge only") == ui.bullet_id("squash merge only") and len(ui.bullet_id("x")) == 10
    for text, doc, who in (("Squash merge only", ".reverie/rules.md", "no owners and no review"),
                           ("Prefer rg over grep", "~/.reverie/rules.md", "applies to your agents in every repo")):
        status, body, _ = get(s, f"/rule?p={pid}&id={ui.bullet_id(text)}")
        assert status == 200 and f"<code>{doc}</code>" in body and who in body


# --- Memory ---------------------------------------------------------------------------------------------------

def test_memory_lists_everything_but_rules_needs_a_look_first_and_is_not_paged(env, serve):
    tmp_path, repo = env
    _mem(repo, text="A stated rule", kind="rule", source="user_said")
    bad = _mem(repo, text="Refunds need approval", kind="decision", source="agent_inferred", evidence="e",
               meta={"usage": {"USED": 3, "CONTRADICTED": 2}, "challenged": True})
    old = _mem(repo, text="Events use a queue", kind="decision", source="user_said",
               meta={"stale": {"commit": "fedcba9876", "message": "", "reason": "queue removed"}})
    for n in range(120):
        _mem(repo, text=f"Environment fact number {n:03d} about thing {n}", kind="environment", source="user_said")
    s = serve(repo)
    page = get(s, "/memory")[1]
    assert page.count('class="entry"') == 122 and "Environment fact number 119" in page and "Page 1 of" not in page      # all of them, unpaged
    assert page.index(">Needs a look</h2>") < page.index(">Environment</h2>")
    assert "2 need a look." in page and "1 rule you stated is listed under <a href=\"/context?p=" in page
    assert "A stated rule" not in page                                                         # each item is on exactly one tab
    assert f"If it is wrong, forget it with <code>reverie undo {bad[:8]}</code>. It can be restored." in page
    assert (f"If it is still true, keep it with <code>reverie stale keep {old[:8]}</code>. If not, forget it with "
            f"<code>reverie undo {old[:8]}</code>.") in page
    assert "used 3 times, contradicted twice" in page and page.count("<summary>120 items</summary>") == 1
    assert ">Rules you stated<" not in page


def test_memory_empty_state_and_rules_only_state(env, serve):
    _, repo = env
    s = serve(repo)
    assert "Nothing remembered for this repo yet." in get(s, "/memory")[1]
    _mem(repo, text="A stated rule", kind="rule", source="user_said")
    page = get(s, "/memory")[1]
    assert "No decisions or findings remembered yet." in page and "1 rule you stated is listed under" in page


# --- the read-only crawl and security headers still hold for the new pages ------------------------------------------

def test_new_pages_are_get_only_and_keep_the_csp(env, serve):
    tmp_path, repo = env
    _ctx_docs(tmp_path, repo)
    s = serve(repo)
    pid = ui._pid(str(repo))
    for path in (f"/context?p={pid}", f"/rule?p={pid}&id=nosuchrule", f"/memory?p={pid}", f"/start?p={pid}"):
        status, body, hdr = get(s, path)
        assert "default-src 'none'" in hdr["Content-Security-Policy"] and "script" not in body.lower().replace("<style", "")
        assert _http(s, "POST", path.split("?")[0], {"x": "1"})[0] in (403, 404, 405)
