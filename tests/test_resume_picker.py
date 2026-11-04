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
