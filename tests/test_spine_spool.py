"""Step 1: crash-safe prompt log, rules files, `reverie resume`."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reverie import cli, rules, spool


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "proj"
    repo.mkdir()
    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True)
    return repo


def test_prompts_are_logged_verbatim_in_order(env):
    p1 = "Decision (1): exit codes 0/1/2.\n\nUnicode ✓ and 'quotes' \"too\""
    assert spool.append(env, "prompt", p1, session="s1")
    assert spool.append(env, "answer", "ok", session="s1")
    assert spool.append(env, "prompt", "second", session="s1")
    recs = spool.read(env)
    assert [r["kind"] for r in recs] == ["prompt", "answer", "prompt"]
    assert recs[0]["text"] == p1


def test_torn_final_line_is_skipped(env):
    spool.append(env, "prompt", "intact", session="s")
    log = spool.repo_dir(env) / "log.jsonl"
    with open(log, "ab") as fh:
        fh.write(b'{"t": "x", "kind": "prompt", "text": "tor')   # crash mid-write
    assert [r["text"] for r in spool.read(env)] == ["intact"]
    spool.append(env, "prompt", "after", session="s")
    assert [r["text"] for r in spool.read(env)] == ["intact", "after"]


def test_empty_and_bad_kinds_are_ignored(env):
    assert not spool.append(env, "prompt", "   ")
    assert not spool.append(env, "thought", "x")
    assert spool.read(env) == []


def test_worktrees_share_one_log(env, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "other"],
                   cwd=env, check=True)
    spool.append(env, "prompt", "from main checkout", session="s")
    assert [r["text"] for r in spool.read(wt)] == ["from main checkout"]


def test_resume_uses_latest_session_and_includes_git_state(env):
    spool.append(env, "prompt", "old session prompt", session="old")
    spool.append(env, "prompt", "PLAN: CLI uses TDB_PATH", session="new")
    spool.append(env, "prompt", "do the CLI now", session="new")
    (env / "a.py").write_text("x = 2\n")
    (env / "new.py").write_text("")
    out = spool.render_resume(env)
    assert "old session prompt" not in out
    assert out.index("PLAN: CLI uses TDB_PATH") < out.index("do the CLI now")
    assert "branch: main" in out and "a.py" in out and "new.py" in out
    assert "old session prompt" in spool.render_resume(env, all_sessions=True)


def test_resume_compactor_seam(env):
    for i in range(5):
        spool.append(env, "prompt", f"p{i}", session="s")
    out = spool.render_resume(env, compactor=lambda recs: recs[-1:])
    assert "p4" in out and "p0" not in out


def test_resume_with_no_log_still_prints_git_state(env):
    out = spool.render_resume(env)
    assert "No prompt log" in out and "branch: main" in out


def test_rules_add_list_verbatim_and_idempotent(env):
    repo_file = rules.add("Never use atexit; see incident 12", level="repo", cwd=env)
    assert repo_file == env / ".reverie" / "rules.md"
    rules.add("Never use atexit; see incident 12", level="repo", cwd=env)
    rules.add("Prefer minimal diffs", level="user", cwd=env)
    assert rules.bullets(repo_file.read_text()) == ["Never use atexit; see incident 12"]
    block = rules.render(env)
    assert block.index("personal") < block.index("team")
    assert "Prefer minimal diffs" in block and "incident 12" in block
    with pytest.raises(ValueError):
        rules.add("  ", level="repo", cwd=env)


def test_resume_prints_rules(env):
    rules.add("Zero new dependencies", level="repo", cwd=env)
    assert "Zero new dependencies" in _run_cli(["resume", "--repo", str(env)])


def _run_cli(argv, capsys=None):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main(argv) == 0
    return buf.getvalue()


def test_cli_rules_add_and_list(env):
    _run_cli(["rules", "add", "--repo", str(env), "Tests", "go", "in", "tests/"])
    out = _run_cli(["rules", "list", "--repo", str(env)])
    assert "Tests go in tests/" in out


def test_hook_shim_is_stdlib_only_and_never_fails(env):
    """The prompt hook must not import the brain / numpy / torch, must exit 0
    on garbage, and must log the prompt."""
    code = ("import sys; import reverie.integrations.claude.shim as s; s.main(['prompt']);"
            "bad=[m for m in ('numpy','torch','reverie.brain') if m in sys.modules];"
            "print('HEAVY', bad)")
    payload = json.dumps({"prompt": "hello from a hook", "session_id": "S",
                          "cwd": str(env)})
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-c", code], input=payload,
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(root)})
    assert r.returncode == 0, r.stderr
    assert "HEAVY []" in r.stdout
    assert spool.read(env)[-1]["text"] == "hello from a hook"
    r = subprocess.run([sys.executable, "-m", "reverie.integrations.claude.shim", "prompt"],
                       input="not json", capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(root)})
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# Claude Code installs the spool hooks
# ---------------------------------------------------------------------------

def _install_claude(home):
    from reverie.integrations.claude import INTEGRATION
    for w in INTEGRATION.install(home, None):
        if w.path.name == "settings.json" and w.content:
            w.path.parent.mkdir(parents=True, exist_ok=True)
            w.path.write_text(w.content)
    return json.loads((home / ".claude" / "settings.json").read_text())


def test_install_registers_prompt_and_answer_spool_hooks(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    settings = _install_claude(home)
    ups = [h for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert len(ups) == 1 and "shim" in json.dumps(ups[0]) and \
        ups[0].get("async") is not True
    stop = [h for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert any("capture-turn" in json.dumps(h) for h in stop)
    assert any("shim" in json.dumps(h) for h in stop)
    pre = settings["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    assert "pretool" in json.dumps(pre[0]["hooks"][0])
    # idempotent
    again = _install_claude(home)
    assert again == settings


def test_uninstall_removes_only_reverie_hooks(tmp_path):
    from reverie.integrations.claude import INTEGRATION
    home = tmp_path / "h"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}}))
    _install_claude(home)
    for r in INTEGRATION.uninstall(home):
        if r.path.name == "settings.json" and r.after is not None:
            r.path.write_text(r.after)
    left = json.loads((home / ".claude" / "settings.json").read_text())
    left.pop("permissions", None)      # owned-entry bookkeeping is not under test
    assert left == {"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "mine.sh"}]}]}}
