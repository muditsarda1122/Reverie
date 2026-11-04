"""Step 2: rules lane, headline index, point-of-action hook, freshness."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reverie import deliver, hooks, items, rules
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
    (repo / "pkg" / "store.py").write_text(
        "class Storage:\n    def close(self):\n        pass\n")
    (repo / "pkg" / "other.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    Brain(tmp_path / "home" / "reverie.db").close()      # creates the brain
    return repo


def conn_for():
    return items.connect(deliver.db_path())


def add(env, text, kind="decision", files=(), symbols=(), **kw):
    c = conn_for()
    try:
        return items.add(c, text=text, kind=kind, source="user_said",
                         repo_key=deliver.repo_key(env),
                         anchor_=items.anchor(files, symbols),
                         grounding_commit=git(env, "rev-parse", "HEAD"), **kw)
    finally:
        c.close()


def pre(env, file, session="s1", **extra):
    payload = {"tool_name": "Edit", "cwd": str(env), "session_id": session,
               "tool_input": {"file_path": str(env / file), **extra}}
    from reverie.integrations.claude import shim
    out = shim._pretool(payload)
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_file_anchor_fires_and_unrelated_file_does_not(env):
    add(env, "Do not cache reads here; tried and abandoned.", "rejected_approach",
        files=["pkg/store.py"])
    assert "abandoned" in pre(env, "pkg/store.py")
    assert pre(env, "pkg/other.py") == ""


def test_symbol_in_file_fires_without_a_file_anchor(env):
    """The atexit lesson: a rule with no file anchor must still fire where its
    symbol lives (file-only anchors scored 0/2 in the lab)."""
    add(env, "Never use atexit for cleanup; see the 2025 incident.", "decision",
        symbols=["Storage.close"])
    assert "atexit" in pre(env, "pkg/store.py")      # `close` is in the file
    assert pre(env, "pkg/other.py") == ""


def test_symbol_in_the_new_content_counts_too(env):
    add(env, "Use sync_all() when touching the journal.", "decision",
        symbols=["sync_all"])
    assert pre(env, "pkg/other.py") == ""
    assert "sync_all" in pre(env, "pkg/other.py", new_string="sync_all()", session="s2")


def test_delivered_once_per_session(env):
    add(env, "Decision A", files=["pkg/store.py"])
    assert pre(env, "pkg/store.py", session="a")
    assert pre(env, "pkg/store.py", session="a") == ""
    assert pre(env, "pkg/store.py", session="b")


def test_freshness_unchanged_vs_changed_vs_unknown(env):
    add(env, "Decision B", files=["pkg/store.py"])
    assert "file changed since: no" in pre(env, "pkg/store.py", session="1")
    (env / "pkg" / "store.py").write_text("class Storage:\n    pass\n")
    git(env, "commit", "-qam", "edit")
    assert "file changed since: yes" in pre(env, "pkg/store.py", session="2")
    c = conn_for()
    c.execute("UPDATE items SET grounding_commit=?", ("0" * 40,))
    c.commit()
    c.close()
    assert "file changed since: unknown" in pre(env, "pkg/store.py", session="3")


def test_anchored_rules_are_point_delivered_as_standing_and_disabled_repo_is_silent(env):
    """Rules used to ride only the session-start lane. Measured on a real repo,
    the edit-time reminder is what makes an agent stop, so an anchored rule now
    fires there too (once per session)."""
    add(env, "A rule item", kind="rule", files=["pkg/store.py"])
    first = pre(env, "pkg/store.py")
    assert "STANDING rule" in first and "A rule item" in first
    assert pre(env, "pkg/store.py") == ""                    # once per session
    add(env, "Decision C", files=["pkg/store.py"])
    assert pre(env, "pkg/store.py", session="x")
    b = Brain(deliver.db_path())
    b.upsert_repo(deliver.repo_key(env), path=str(env), state="disabled")
    b.close()
    assert pre(env, "pkg/store.py", session="y") == ""


def test_other_repos_items_do_not_fire_but_user_wide_do(env, tmp_path):
    c = conn_for()
    items.add(c, text="Elsewhere only", kind="decision", source="user_said",
              repo_key="/some/other/.git", anchor_=items.anchor(["pkg/store.py"]))
    items.add(c, text="Everywhere", kind="preference", source="user_said",
              repo_key=None, anchor_=items.anchor(["pkg/store.py"], type_="user"))
    c.close()
    ctx = pre(env, "pkg/store.py")
    assert "Everywhere" in ctx and "Elsewhere" not in ctx


def test_non_edit_tools_and_outside_files_are_ignored(env):
    add(env, "Decision D", files=["pkg/store.py"])
    from reverie.integrations.claude import shim
    assert shim._pretool({"tool_name": "Read", "cwd": str(env),
                          "tool_input": {"file_path": str(env / "pkg/store.py")}}) == ""
    assert shim._pretool({"tool_name": "Edit", "cwd": str(env),
                          "tool_input": {"file_path": "/etc/hosts"}}) == ""


def test_session_start_prints_rules_and_index(env, capsys):
    rules.add("Never add dependencies", level="repo", cwd=env)
    add(env, "Cache design was rejected because of stale handles", "rejected_approach",
        files=["pkg/store.py"])
    out = []
    hooks.session_start(env, print_fn=out.append)
    text = "\n".join(out)
    assert "Never add dependencies" in text
    assert "rejected_approach: Cache design was rejected" in text
    assert "reverie show" in text


def test_rules_show_even_with_no_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "nohome"))
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    rules.add("Be terse", level="repo", cwd=repo)
    out = []
    hooks.session_start(repo, print_fn=out.append)
    assert "Be terse" in "\n".join(out)


def test_show_prints_item_with_freshness(env):
    item_id = add(env, "Full text of the decision", files=["pkg/store.py"])
    c = deliver.open_db()
    text = deliver.show(c, item_id[:8], str(env))
    c.close()
    assert "Full text of the decision" in text and "verified at commit" in text


def test_hook_process_is_fast_stdlib_and_always_exits_zero(env):
    add(env, "Decision E", files=["pkg/store.py"])
    root = Path(__file__).resolve().parent.parent
    payload = json.dumps({"tool_name": "Edit", "cwd": str(env), "session_id": "p",
                          "tool_input": {"file_path": str(env / "pkg/store.py")}})
    code = ("import sys, reverie.integrations.claude.shim as s; s.main(['pretool']);"
            "print('\\nHEAVY', [m for m in ('numpy','torch','reverie.brain') "
            "if m in sys.modules])")
    r = subprocess.run([sys.executable, "-c", code], input=payload,
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(root)})
    assert r.returncode == 0, r.stderr
    assert "Decision E" in r.stdout and "HEAVY []" in r.stdout
    bad = subprocess.run([sys.executable, "-m", "reverie.integrations.claude.shim", "pretool"],
                         input="garbage", capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(root)})
    assert bad.returncode == 0 and bad.stdout == ""


def test_headline_index_orders_rules_first_and_caps(env):
    for i in range(5):
        add(env, f"observation number {i} " + "w " * 40, kind="environment")
    add(env, "Decision first please", kind="decision")
    c = deliver.open_db()
    mem = deliver.memory(c, deliver.repo_key(env), str(env))
    c.close()
    idx = deliver.headline_index(mem, limit=3)
    lines = idx.splitlines()
    assert "Decision first please" in lines[1]
    assert lines[-1].startswith("(+3 more")
    assert " …" in deliver.title("w " * 40)
