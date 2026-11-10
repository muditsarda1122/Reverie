"""Sessions linked into one chain across tools: finished in another tool via `reverie resume`."""

import json
import subprocess

import pytest

from reverie import spool


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    r = tmp_path / "proj"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    return r


def _log(repo, rows):
    d = spool.repo_dir(str(repo))
    d.mkdir(parents=True, exist_ok=True)
    (d / "log.jsonl").write_text("".join(json.dumps(
        {"t": t, "kind": k, "session": s, "branch": "main", "harness": h, "text": txt}) + "\n"
        for t, k, s, h, txt in rows))


ASK = "My earlier session got cut off. Run `reverie resume` and pick up where it stopped."

CHAIN = [
    ("2026-11-01T09:00:00", "prompt", "A", "claude_code", "Add retry logic to the fetcher; never use print()."),
    ("2026-11-01T09:05:00", "answer", "A", "claude_code", "Added retry."),
    ("2026-11-01T09:10:00", "prompt", "A", "claude_code", "Now add backoff, cap at 30 s."),
    ("2026-11-01T11:00:00", "prompt", "B", "codex", ASK),
    ("2026-11-01T11:01:00", "prompt", "B", "codex", "Also write the tests for the backoff."),
    ("2026-11-01T11:05:00", "answer", "B", "codex", "Tests written."),
]


def test_a_continuation_is_linked_to_the_session_it_continued(repo):
    _log(repo, CHAIN)
    recs = spool.read(str(repo))
    assert spool.predecessor(recs, "B") == "A"
    assert spool.predecessor(recs, "A") is None                 # no resume ask: never invented
    assert spool.chain_of(recs, "B") == ["A", "B"]


def test_the_card_shows_the_whole_chain_across_tools(repo):
    _log(repo, CHAIN)
    card = spool.render_resume(str(repo), session="B")
    assert "2 sessions, possibly in different tools" in card
    assert "claude_code (2 prompt(s)) -> codex (1 prompt(s))" in card
    a, b, c = (card.index(x) for x in ("Add retry logic", "Now add backoff", "write the tests"))
    assert a < b < c                                             # in order, one card
    assert "got cut off" not in card                            # the ask is the link, not work
    assert "[prompt 3 · codex] (LAST: may not have been handled) Also write the tests" in card
    assert "[prompt 1 · claude_code]" in card


def test_the_default_card_is_unambiguous_when_two_sessions_form_a_chain(repo):
    _log(repo, CHAIN)
    card = spool.render_resume(str(repo))                        # no --session: no list to choose from
    assert "Several earlier sessions" not in card and "Now add backoff" in card
    assert "write the tests" in card


def test_a_session_resumed_by_a_later_tool_is_still_listed_alone_when_asked(repo):
    _log(repo, CHAIN)
    card = spool.render_resume(str(repo), session="A")           # explicitly the first one only
    assert "write the tests" not in card and "possibly in different tools" not in card


def test_two_unrelated_sessions_stay_ambiguous(repo):
    rows = [r for r in CHAIN if r[2] == "A"] + [
        ("2026-11-01T11:00:00", "prompt", "C", "codex", "Refactor the cache layer.")]
    _log(repo, rows)
    assert "Several earlier sessions" in spool.render_resume(str(repo))


def test_chain_of_three_and_a_limit(repo):
    rows = CHAIN + [
        ("2026-11-01T14:00:00", "prompt", "C", "opencode", "Previous session got interrupted, continue it."),
        ("2026-11-01T14:02:00", "prompt", "C", "opencode", "Now also document the backoff."),
        ("2026-11-01T14:09:00", "answer", "C", "opencode", "Documented."),
    ]
    _log(repo, rows)
    recs = spool.read(str(repo))
    assert spool.chain_of(recs, "C") == ["A", "B", "C"]
    assert spool.chain_of(recs, "C", limit=2) == ["B", "C"]
    card = spool.render_resume(str(repo), session="C")
    assert "3 sessions" in card and "claude_code" in card and "codex" in card and "opencode" in card


def test_a_continuation_that_started_before_the_other_ended_is_not_linked(repo):
    rows = [
        ("2026-11-01T09:00:00", "prompt", "A", "claude_code", "Add retry logic to the fetcher."),
        ("2026-11-01T11:30:00", "answer", "A", "claude_code", "Done."),
        ("2026-11-01T11:00:00", "prompt", "B", "codex", ASK),        # began while A was still running
        ("2026-11-01T11:01:00", "prompt", "B", "codex", "Also write tests."),
    ]
    _log(repo, rows)
    assert spool.predecessor(spool.read(str(repo)), "B") is None


def test_the_chain_card_is_still_redacted_and_bounded(repo):
    rows = list(CHAIN)
    rows[0] = ("2026-11-01T09:00:00", "prompt", "A", "claude_code",
               "Use key AKIAIOSFODNN7EXAMPLE for the fetcher. " + "detail " * 400)
    _log(repo, rows)
    card = spool.render_resume(str(repo), session="B")
    assert "AKIAIOSFODNN7EXAMPLE" not in card and len(card) < 12_000
