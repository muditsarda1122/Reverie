"""Delivery defects found by running Reverie as a product on a real repo (httpx):

* hook-less harnesses (codex, opencode) were told "0 conclusions" and broke a
  standing rule -> MCP summary/query now carry the typed memory;
* a stale-flagged rule stayed in the always-apply list; transient to-dos
  outlived the code they described; the start block outgrew the harness cap.
"""

import json
import subprocess

import pytest
from fake_embed import FakeEmbed

from reverie import core_api, deliver, items
from reverie.brain import Brain


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "pkg" / "store.py").write_text("class Storage:\n    pass\n")
    (repo / "pkg" / "other.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    brain = Brain(tmp_path / "home" / "reverie.db")
    yield brain, repo
    brain.close()


def add(brain, repo, text, kind="rule", files=(), symbols=(), source="user_said",
        emb=None, **kw):
    return items.add(
        brain._conn, text=text, kind=kind, source=source, evidence="e",
        repo_key=deliver.repo_key(repo), anchor_=items.anchor(files, symbols),
        grounding_commit=git(repo, "rev-parse", "HEAD"), author="user",
        embedding=emb, **kw)


def flag_stale(brain, item_id):
    items.set_meta(brain._conn, item_id, {"stale": {
        "commit": "deadbeef1234", "message": "changed it", "reason": "now allows X"}})



# ------------------------------------------------------------ startup block

def test_stale_rule_is_set_apart_and_nothing_is_listed_twice(env):
    brain, repo = env
    ok = add(brain, repo, "Never retry a ReadTimeout.")
    old = add(brain, repo, "Do not retry on 429.")
    flag_stale(brain, old)
    text = deliver.startup_text(str(repo))
    head, _, tail = text.partition("Possibly outdated")
    assert ok[:8] in head and old[:8] not in head          # not always-apply
    assert old[:8] in tail and "now allows X" in tail      # with the reason
    assert text.count(ok[:8]) == 1 and text.count(old[:8]) == 1


def test_transient_items_die_with_the_code_they_described(env):
    brain, repo = env
    todo = add(brain, repo, "Implement the store next.", kind="work_state",
               files=["pkg/store.py"])
    keep = add(brain, repo, "Waiting on the user about other.py.", kind="open_question",
               files=["pkg/other.py"])
    flagged = add(brain, repo, "Not started yet.", kind="work_state")
    flag_stale(brain, flagged)
    assert todo[:8] in deliver.startup_text(str(repo))      # code unchanged: live
    (repo / "pkg" / "store.py").write_text("class Storage:\n    def close(self): ...\n")
    git(repo, "commit", "-qam", "implement store")
    text = deliver.startup_text(str(repo))
    assert todo[:8] not in text                             # its file changed
    assert keep[:8] in text                                 # other file untouched
    assert flagged[:8] not in text                          # flagged: dead
    assert deliver.is_live(brain._conn.execute(
        "SELECT * FROM items WHERE id=?", (keep,)).fetchone() and
        items.get(brain._conn, keep) | {"origin": "item"}, str(repo))


def test_standing_rules_never_die_from_age_or_file_changes(env):
    brain, repo = env
    rule = add(brain, repo, "Never log headers.", files=["pkg/store.py"])
    (repo / "pkg" / "store.py").write_text("changed\n")
    git(repo, "commit", "-qam", "edit")
    assert rule[:8] in deliver.startup_text(str(repo))


def test_startup_block_stays_under_the_harness_cap_at_scale(env):
    brain, repo = env
    for i in range(60):
        add(brain, repo, f"Rule number {i}: module m{i} must keep invariant {i} "
            f"because of incident {i * 7} " + "x" * 300)
    text = deliver.startup_text(str(repo))
    assert len(text) <= deliver.MAX_STARTUP_CHARS
    assert "more" in text and "reverie show" in text       # says what it cut


def test_edit_time_delivers_anchored_rules_as_standing_and_flags_stale(env):
    from reverie.integrations.claude import shim
    brain, repo = env
    rule = add(brain, repo, "Never log headers.", files=["pkg/store.py"])
    old = add(brain, repo, "Do not retry on 429.", files=["pkg/store.py"])
    flag_stale(brain, old)

    def edit(session):
        out = shim._pretool({"tool_name": "Edit", "cwd": str(repo), "session_id": session,
                             "tool_input": {"file_path": str(repo / "pkg/store.py")}})
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]
    ctx = edit("s1")
    assert f"[{rule[:8]}] STANDING rule" in ctx
    assert f"[{old[:8]}] rule" in ctx and f"[{old[:8]}] STANDING" not in ctx
    assert "WARNING: may be stale" in ctx


# ------------------------------------------------------------ MCP harnesses

def _server(brain):
    from reverie.mcp_server import ReverieServer
    from reverie.session import SessionManager
    return ReverieServer(brain=brain, manager=SessionManager(brain),
                         embedding_model=FakeEmbed())


def test_mcp_summary_carries_the_rules_and_index(env):
    brain, repo = env
    rule = add(brain, repo, "Never retry a ReadTimeout, not even for GET.")
    out = core_api.summary(str(repo), _server=_server(brain))
    assert rule[:8] in out["memory"] and "Never retry a ReadTimeout" in out["message"]


def test_mcp_query_returns_matching_typed_items_and_standing_rules(env):
    brain, repo = env
    emb = FakeEmbed()
    rt = add(brain, repo, "Never retry a ReadTimeout, not even for GET.",
             files=["pkg/store.py"], symbols=["RetryTransport"],
             emb=emb.encode_one("Never retry a ReadTimeout, not even for GET.").tobytes())
    other = add(brain, repo, "Docs are built with scripts/docs.")
    server = _server(brain)
    res = server._tool_query({"query": "add retry_on so ReadTimeout can be retried in RetryTransport",
                              "repo_path": str(repo)})
    assert res["status"] == "ok"
    assert f"[{rt[:8]}] STANDING rule" in res["memory"]
    assert other[:8] in res["memory"]                      # other standing rules listed
    assert "typed memory below applies" in res["message"]  # no more "brain is empty"
    assert res["memory"] in res["formatted"]


def test_disabled_repo_gets_nothing_from_mcp(env):
    from reverie import repos
    brain, repo = env
    add(brain, repo, "Never retry a ReadTimeout.")
    info = repos.resolve_repo(cwd=str(repo))
    repos.set_repo_state(brain, info, "disabled")
    assert deliver.startup_text(str(repo)) == ""
    assert deliver.query_context(str(repo), "retry timeout") == ""


