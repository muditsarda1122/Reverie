"""`deliver.startup_report`: the start text, plus what became of every rule in it (told, or cut and which limit
cut it). The text is the one `startup_text` returns; the report only watches."""

import subprocess
from pathlib import Path

import pytest

from reverie import cli, deliver, items, rules


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


def _add_rules(repo: Path, texts: list[str]) -> None:
    conn = items.connect(deliver.db_path())
    key = deliver.repo_key(str(repo))
    for t in texts:
        items.add(conn, text=t, kind="rule", source="user_said", repo_key=key)
    conn.close()


def _mem_cut(trace):
    return [r for r in trace.rules if r["source"] == "memory" and not r["told"]]


def test_the_report_text_is_exactly_the_start_text(repo):
    _add_rules(repo, [f"Repo rule number {n:02d} about thing {n}" for n in range(5)])
    text, trace = deliver.startup_report(str(repo))
    assert text == deliver.startup_text(str(repo), log=False)
    assert [r["told"] for r in trace.rules] == [True] * 5 and trace.index == 0


def test_rules_cut_by_the_line_limit_say_so(repo):
    _add_rules(repo, [f"Repo rule number {n:02d} about thing {n}" for n in range(35)])
    text, trace = deliver.startup_report(str(repo))
    cut = _mem_cut(trace)
    assert len(cut) == 5 and sum(1 for r in trace.rules if r["told"]) == 30
    assert {r["limit"] for r in cut} == {"lines"}
    assert {r["reason"] for r in cut} == {"agents are told at most 30 remembered rules for this repo"}
    assert "(+5 more" in text and all(r["text"] not in text for r in cut)       # what the report calls cut is not in the text
    assert all(r["text"] in text for r in trace.rules if r["told"])


def test_rules_cut_by_the_character_budget_say_so(repo):
    long = "x" * 280
    _add_rules(repo, [f"Rule {n:02d} {long}" for n in range(25)])                  # 25 lines fit the line cap, not the budget
    text, trace = deliver.startup_report(str(repo))
    told = [r for r in trace.rules if r["told"]]
    cut = _mem_cut(trace)
    assert 0 < len(told) < 25 and len(told) + len(cut) == 25
    assert {r["limit"] for r in cut} == {"characters"}
    assert {r["reason"] for r in cut} == {"agents are told at most about 4,500 characters of remembered rules for this repo"}
    assert all(r["text"] not in text for r in cut) and all(r["text"] in text for r in told)


def test_user_wide_rules_have_their_own_limit(repo):
    conn = items.connect(deliver.db_path())
    for n in range(20):
        items.add(conn, text=f"Wide preference {n:02d} about habit {n}", kind="preference", source="user_said", repo_key=None)
    conn.close()
    _, trace = deliver.startup_report(str(repo))
    cut = _mem_cut(trace)
    assert len(cut) == 5 and {r["reason"] for r in cut} == {"agents are told at most 15 of your rules that apply in every repo"}


def test_team_rules_and_rules_file_bullets_are_reported_too(repo):
    ctx = repo / ".reverie" / "context"
    ctx.mkdir(parents=True)
    (ctx / "rules.md").write_text("## Rules\n- Money is integer cents\n")
    rules.add("Squash merge only", level="repo", cwd=str(repo))
    rules.add("Prefer rg over grep", level="user")
    text, trace = deliver.startup_report(str(repo))
    sources = {r["source"] for r in trace.rules}
    assert sources == {"team", "rules_file_repo", "rules_file_user"} and all(r["told"] for r in trace.rules)
    team = next(r for r in trace.rules if r["source"] == "team")
    assert team["key"] in text and team["text"] == "Money is integer cents"


def test_a_rule_that_falls_off_the_end_of_the_whole_text_is_reported_as_cut(repo, monkeypatch):
    monkeypatch.setenv("REVERIE_STARTUP_CHARS", "1500")            # the smallest cap there is
    for n in range(25):                                            # ~2,300 characters of rules-file bullets: under their own 2,400 limit
        rules.add(f"Personal rule {n:02d} " + "z" * 70, level="user")
    text, trace = deliver.startup_report(str(repo))
    assert "(…truncated" in text and len(text) <= 1500
    told = [r for r in trace.rules if r["told"]]
    cut = [r for r in trace.rules if not r["told"]]
    assert told and cut and len(told) + len(cut) == 25
    assert {r["limit"] for r in cut} == {"characters"}
    assert {r["reason"] for r in cut} == {"the whole start text stops at 1,500 characters"}
    assert all(r["text"] in text for r in told) and all(r["text"] not in text for r in cut)


def test_index_count_is_the_index_lines_in_the_text(repo):
    conn = items.connect(deliver.db_path())
    key = deliver.repo_key(str(repo))
    for t in ("We chose the outbox", "Rejected caching fees"):
        items.add(conn, text=t, kind="decision", source="user_said", repo_key=key)
    conn.close()
    text, trace = deliver.startup_report(str(repo))
    assert trace.index == 2 == sum(1 for ln in text.splitlines() if ln.startswith("- ["))
