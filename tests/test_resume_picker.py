"""`reverie resume` picks the session on the current branch, or asks."""
from reverie import spool


def _rec(sid, branch, text, t, kind="prompt", harness="claude"):
    return {"t": t, "kind": kind, "session": sid, "branch": branch,
            "harness": harness, "text": text}


def test_single_session_resumes_without_asking():
    recs = [_rec("a", "main", "do x", "2026-01-01T10:00:00")]
    chosen, allsess = spool.pick_session(recs, "feat")
    assert chosen == "a" and len(allsess) == 1


def test_one_session_on_current_branch_wins_over_newer_elsewhere():
    recs = [_rec("a", "feat", "task A", "2026-01-01T10:00:00"),
            _rec("b", "main", "task B", "2026-01-02T10:00:00")]
    assert spool.pick_session(recs, "feat")[0] == "a"


def test_several_on_branch_is_ambiguous_and_lists():
    recs = [_rec("a", "main", "task A", "2026-01-01T10:00:00"),
            _rec("b", "main", "task B", "2026-01-02T10:00:00")]
    chosen, allsess = spool.pick_session(recs, "main")
    assert chosen is None
    text = spool.render_session_list(allsess, "main")
    assert "task A" in text and "task B" in text and "--session" in text
    assert text.index("task B") < text.index("task A")      # newest first


def test_none_on_branch_but_several_elsewhere_asks():
    recs = [_rec("a", "x", "A", "2026-01-01T10:00:00"),
            _rec("b", "y", "B", "2026-01-02T10:00:00")]
    assert spool.pick_session(recs, "main")[0] is None


def test_title_is_first_prompt_clipped_and_one_line():
    long = "line one\nline two " + "z" * 200
    info = spool.sessions([_rec("a", "m", long, "2026-01-01T10:00:00")])[0]
    assert "\n" not in info["title"] and len(info["title"]) <= spool.TITLE_CHARS


def test_render_resume_asks_then_latest_flag_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "r"
    repo.mkdir()
    spool.append(repo, "prompt", "first task", session="s1")
    spool.append(repo, "prompt", "second task", session="s2")
    asked = spool.render_resume(repo)
    assert "Several earlier sessions" in asked and "[prompt 1]" not in asked
    latest = spool.render_resume(repo, latest=True)
    assert "second task" in latest and "first task" not in latest
    assert "first task" in spool.render_resume(repo, session="s1")


def test_cutoff_hint_and_summary_resume_for_agents_without_a_shell(tmp_path, monkeypatch):
    """Regression (real run): codex's login shell had no `reverie` on PATH, so the policy's
    `reverie resume` silently failed. MCP `summary` now announces a cut-off session and can
    return the card."""
    import time

    from reverie import spool
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "r"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    spool.append(repo, "prompt", "add summarize_failures, only log when DEBUG is on",
                 session="c1", harness="claude", branch="main", t="2026-11-05T10:00:00+0000")
    base = time.mktime(time.strptime("2026-11-05T10:00:00", "%Y-%m-%dT%H:%M:%S"))
    assert spool.cutoff_hint(repo, now=base + 30) == ""              # too fresh: the live session
    hint = spool.cutoff_hint(repo, now=base + 600)
    assert "unanswered" in hint and "only log when DEBUG is on" in hint and "resume: true" in hint
    assert spool.cutoff_hint(repo, now=base + 3 * 86400) == ""       # a day old: history
    spool.append(repo, "answer", "done", session="c1", harness="claude", branch="main",
                 t="2026-11-05T10:01:00+0000")
    assert spool.cutoff_hint(repo, now=base + 600) == ""              # it was answered
