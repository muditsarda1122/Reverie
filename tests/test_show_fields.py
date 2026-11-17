"""`reverie show`: the fields the web UI's Rule page needs and the CLI did not print before."""

import subprocess

import pytest

from reverie import cli, context, deliver, items, usage


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REVERIE_NO_BACKGROUND", "1")
    r = tmp_path / "svc"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.email", "dev@acme.com"], check=True)
    monkeypatch.chdir(r)
    assert cli.main(["enable"]) == 0
    return r


def test_usage_words():
    assert usage.words({"USED": 3, "CONTRADICTED": 2}) == "used 3 times, contradicted twice"
    assert usage.words({"USED": 1, "IGNORED": 0}) == "used once" and usage.words({}) == ""
    assert usage.words({"IRRELEVANT": 4, "OVERRIDDEN": 1}) == "not relevant 4 times, overridden once"


def test_show_prints_when_remembered_verified_usage_and_challenge(repo):
    conn = items.connect(deliver.db_path())
    iid = items.add(conn, text="Refunds need approval", kind="decision", source="agent_inferred", evidence="policy",
                    repo_key=deliver.repo_key(str(repo)), meta={"usage": {"USED": 3, "CONTRADICTED": 2}, "challenged": True})
    conn.execute("UPDATE items SET last_verified=? WHERE id=?", ("2026-11-10T09:00:00+0000", iid))
    conn.commit()
    plain = items.add(conn, text="Plain note", kind="decision", source="user_said", repo_key=deliver.repo_key(str(repo)))
    conn.close()
    out = deliver.show(deliver.open_db(), iid[:8], str(repo))
    assert "remembered: 20" in out and "last verified: 2026-11-10" in out
    assert "used by agents: used 3 times, contradicted twice" in out and "challenged: agents contradicted it repeatedly" in out
    bare = deliver.show(deliver.open_db(), plain[:8], str(repo))
    assert "remembered: 20" in bare and "last verified" not in bare and "used by agents" not in bare and "challenged" not in bare


def test_context_show_prints_the_conflict_reason_the_winner_and_the_stale_message(repo, tmp_path):
    ctx = repo / ".reverie" / "context"
    ctx.mkdir(parents=True)
    (ctx / "rules.md").write_text("## Rules\n- Plain logs in dev {id: log}\n- Use the helper\n")
    (tmp_path / "co" / "org").mkdir(parents=True)
    (tmp_path / "co" / "org" / "rules.md").write_text("## Rules\n- All services log JSON {id: log} (locked)\n")
    (repo / ".reverie" / "context.toml").write_text('[sources]\norg = ["../co/org"]\n')
    res = context.resolve(str(repo))
    lo = next(i for i in res.items if "Plain logs" in i.text)
    out = context.show(str(repo), lo.id)
    assert "conflict log (locked):" in out and "winner: [org" in out
    helper = next(i for i in res.items if "helper" in i.text)
    st = context.load_state()
    st["stale"] = {helper.id: {"commit": "abcdef0123", "message": "drop the helper", "reason": "gone", "file": helper.file}}
    context.save_state(st)
    assert "possibly outdated: commit abcdef01 (drop the helper): gone" in context.show(str(repo), helper.id)
    st["stale"][helper.id]["message"] = ""
    context.save_state(st)
    assert "possibly outdated: commit abcdef01: gone" in context.show(str(repo), helper.id)
