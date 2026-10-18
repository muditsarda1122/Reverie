"""feat/session-start-hook — `reverie hook session-start` + installer wiring.

The hook is what a fresh Claude Code session runs at startup: it must print
the repo-state block (enabled counts + rules / the first-time question /
the off notice), stay read-only and quiet on every failure, and always exit
0. The installer merges exactly one SessionStart handler into
``~/.claude/settings.json``; uninstall reverses that merge surgically.

Run: .venv/bin/python -m pytest tests/test_flow_session_start_hook.py -v
All offline, everything under tmp_path (sandboxed home + REVERIE_HOME).
"""

import json
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import sqlite3
from pathlib import Path

import pytest

from reverie import cli, hooks, install, repos, uninstall
from reverie.brain import Brain

T0 = "2026-10-14T12:00:00+00:00"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("REVERIE_HOME", str(h / ".reverie"))
    return h


@pytest.fixture()
def reverie_home(home):
    return home / ".reverie"


@pytest.fixture()
def repo_env(tmp_path, monkeypatch):
    """A deterministic (repo, branch) without git: the env override form."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("REVERIE_REPO_PATH", str(repo))
    monkeypatch.setenv("REVERIE_BRANCH", "main")
    return str(repo.resolve())


def json_text(data: dict) -> str:
    """The exact serialization install/uninstall use (order preserved)."""
    return json.dumps(data, indent=2) + "\n"


def capture(print_fn=None):
    out: list[str] = []
    return out, (print_fn or out.append)


def run_hook(cwd=None) -> tuple[int, str]:
    out: list[str] = []
    code = hooks.session_start(cwd, print_fn=out.append)
    return code, "\n".join(out)


def do_install(home, reverie_home, **kwargs):
    kwargs.setdefault("check_fn", lambda *a, **k: (True, "check skipped (test)"))
    kwargs.setdefault("only", ["claude_code"])
    return install.run(
        home=home, reverie_home=reverie_home, no_verify=True,
        which=kwargs.pop("which", lambda _b: None),
        input_fn=lambda _p="": "", print_fn=lambda s: None, environ={},
        **kwargs)


def do_uninstall(home, reverie_home, **kwargs) -> tuple[int, str]:
    out: list[str] = []
    kwargs.setdefault("input_fn", lambda _p="": "y")
    code = uninstall.run(home=home, reverie_home=reverie_home,
                         print_fn=out.append, **kwargs)
    return code, "\n".join(out)


def enable_repo(reverie_home: Path) -> Brain:
    brain = Brain(reverie_home / "reverie.db")
    brain.upsert_repo("env-key", path="/tmp/other", state="enabled")
    return brain


# ---------------------------------------------------------------------------
# hook output — the three states, exact texts
# ---------------------------------------------------------------------------

def test_missing_brain_is_undecided_and_creates_nothing(home, repo_env):
    code, text = run_hook()
    assert code == 0
    assert repos.FIRST_TIME_QUESTION in text
    assert "reverie_set_repo_state" in text
    assert not (home / ".reverie").exists()      # the hook never bootstraps


def test_disabled_prints_the_off_notice_exactly(home, reverie_home,
                                                repo_env):
    brain = Brain(reverie_home / "reverie.db")
    brain.upsert_repo(repo_env, path=repo_env, state="disabled")
    brain.close()
    code, text = run_hook()
    assert code == 0
    assert text == hooks.DISABLED_TEXT


def test_enabled_prints_counts_and_rules_never_conclusion_text(
        home, reverie_home, repo_env):
    brain = Brain(reverie_home / "reverie.db")
    brain.upsert_repo(repo_env, path=repo_env, state="enabled")
    brain.insert_ecu({"cognition": "webhook handlers must be idempotent",
                      "conclusion_type": "invariant",
                      "scope": {"level": "repo", "path": f"repo:{repo_env}"}})
    sid = brain.create_session(repo_env, "main")
    brain.insert_session_ecu(sid, {
        "cognition": "secret pending conclusion text",
        "confidence": 0.6, "status": "active",
        "scope": {"level": "repo", "path": f"repo:{repo_env}"},
        "conclusion_type": "decision"})
    brain.close()
    code, text = run_hook()
    assert code == 0
    assert "1 conclusion in memory" in text
    assert "1 candidate awaiting review" in text
    assert "secret pending conclusion text" not in text
    assert "webhook handlers must be idempotent" not in text
    for rule in hooks.ENABLED_RULES:
        assert rule in text


def test_undecided_with_existing_brain_writes_no_repo_row(home,
                                                          reverie_home,
                                                          repo_env):
    brain = Brain(reverie_home / "reverie.db")
    brain.close()
    _, text = run_hook()
    assert repos.FIRST_TIME_QUESTION in text
    brain = Brain(reverie_home / "reverie.db")
    assert brain.list_repos() == []              # nothing was upserted
    brain.close()


def test_non_git_directory_is_quiet(tmp_path, home, monkeypatch):
    monkeypatch.delenv("REVERIE_REPO_PATH", raising=False)
    monkeypatch.delenv("REVERIE_BRANCH", raising=False)
    code, text = run_hook(cwd=tmp_path)          # not a repo — no git
    assert code == 0 and text == ""


def test_broken_brain_is_quiet_and_exits_zero(home, reverie_home,
                                              repo_env):
    reverie_home.mkdir(parents=True)
    (reverie_home / "reverie.db").write_bytes(b"this is not a database")
    code, text = run_hook()
    assert code == 0 and text == ""


def test_outdated_schema_is_quiet_and_never_migrated(home, reverie_home,
                                                     repo_env):
    brain = Brain(reverie_home / "reverie.db")
    brain._conn.execute("PRAGMA user_version = 0")
    brain._conn.commit()
    brain.close()
    code, text = run_hook()
    assert code == 0 and text == ""              # stay quiet, never migrate
    conn = sqlite3.connect(reverie_home / "reverie.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


def test_state_readonly_reports_url_match_without_rekeying(reverie_home):
    """A moved repo (new git common dir, same origin URL) reads its old
    state — but nothing is re-keyed or stamped (D58 rematch is a write)."""
    brain = Brain(reverie_home / "reverie.db")
    brain.upsert_repo("/old/.git", path="/old", url="git@x:y.git",
                      state="enabled")
    info = repos.RepoInfo(repo_path="/new", branch="main",
                          repo_key="/new/.git", url="git@x:y.git")
    assert repos.state_readonly(brain, info) == "enabled"
    assert brain.get_repo("/new/.git") is None   # no re-key
    assert brain.get_repo("/old/.git")["asked_at"] is None
    brain.close()


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_hook_session_start_routes_to_hooks(home, repo_env, capsys):
    assert cli.main(["hook", "session-start"]) == 0
    assert repos.FIRST_TIME_QUESTION in capsys.readouterr().out


def test_hook_parser_knows_the_session_start_flags():
    args = cli._build_parser().parse_args(
        ["hook", "session-start", "--repo", "/x"])
    assert args.command == "hook" and args.hook == "session-start"
    assert args.repo == "/x"


# ---------------------------------------------------------------------------
# hook_handler — pip install (exec form) vs dev checkout (shell form)
# ---------------------------------------------------------------------------

def test_hook_handler_pip_install_uses_absolute_script(monkeypatch,
                                                       tmp_path):
    monkeypatch.setattr(install, "_is_pip_installed", lambda: True)
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/usr/bin/env python\n")
    script = bin_dir / "reverie"
    script.write_text("#!/usr/bin/env python\n")
    handler = install.hook_handler(executable=str(bin_dir / "python"))
    assert handler == {"type": "command", "command": str(script),
                       "args": ["hook", "session-start"]}
    assert install._is_reverie_hook_handler(handler)


def test_hook_handler_dev_fallback_sets_pythonpath_shell_form(monkeypatch):
    monkeypatch.setattr(install, "_is_pip_installed", lambda: False)
    handler = install.hook_handler()
    assert handler["type"] == "command"
    assert "args" not in handler                 # shell form (no env field)
    assert "-m reverie.cli hook session-start" in handler["command"]
    assert handler["command"].startswith("PYTHONPATH=")
    assert install._is_reverie_hook_handler(handler)


# ---------------------------------------------------------------------------
# install merge — idempotent, siblings untouched
# ---------------------------------------------------------------------------

def test_install_creates_then_repeats_without_changes(home, reverie_home):
    assert do_install(home, reverie_home) == 0
    settings = home / ".claude" / "settings.json"
    first = settings.read_text()
    data = json.loads(first)
    groups = data["hooks"]["SessionStart"]
    assert len(groups) == 1 and groups[0]["matcher"] == "startup"
    assert install._is_reverie_hook_handler(groups[0]["hooks"][0])
    assert str(settings.resolve()) in json.loads(
        (reverie_home / "install_manifest.json").read_text())["files"]
    assert do_install(home, reverie_home) == 0
    assert settings.read_text() == first         # idempotent, byte-identical


def test_install_preserves_other_settings_and_hooks(home, reverie_home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = json_text({
        "theme": "dark",
        "hooks": {
            "PreToolUse": [{"matcher": "Bash",
                            "hooks": [{"type": "command",
                                       "command": "/usr/bin/true"}]}],
            "SessionStart": [{"matcher": "resume",
                              "hooks": [{"type": "command",
                                         "command": "/usr/bin/true"}]}],
        },
    })
    settings.write_text(original)
    assert do_install(home, reverie_home) == 0
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["hooks"]["PreToolUse"] == json.loads(original)["hooks"]["PreToolUse"]
    groups = data["hooks"]["SessionStart"]
    assert [g["matcher"] for g in groups] == ["resume", "startup"]
    assert do_uninstall(home, reverie_home)[0] == 0
    assert settings.read_text() == original      # byte-identical restore


def test_install_appends_to_existing_startup_group(home, reverie_home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json_text({"hooks": {"SessionStart": [
        {"matcher": "startup",
         "hooks": [{"type": "command", "command": "/usr/bin/true"}]},
    ]}}))
    assert do_install(home, reverie_home) == 0
    groups = json.loads(settings.read_text())["hooks"]["SessionStart"]
    assert len(groups) == 1                     # no new group
    assert len(groups[0]["hooks"]) == 2         # the user's + ours
    assert install._is_reverie_hook_handler(groups[0]["hooks"][1])


def test_install_replaces_a_stale_reverie_handler(home, reverie_home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json_text({"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [
            {"type": "command", "command": "/old/venv/bin/reverie",
             "args": ["hook", "session-start"]},
        ]},
    ]}}))
    assert do_install(home, reverie_home) == 0
    groups = json.loads(settings.read_text())["hooks"]["SessionStart"]
    handlers = groups[0]["hooks"]
    assert len(handlers) == 1                   # replaced, never duplicated
    assert handlers[0]["command"] != "/old/venv/bin/reverie"


def test_install_refuses_invalid_settings_json(home, reverie_home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")
    with pytest.raises(install.InstallError):
        do_install(home, reverie_home)
    assert settings.read_text() == "{not json"   # untouched


# ---------------------------------------------------------------------------
# uninstall — surgical reverse merge
# ---------------------------------------------------------------------------

def test_uninstall_removes_only_the_hook_and_keeps_shared_file(
        home, reverie_home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json_text({
        "theme": "dark",
        "hooks": {"SessionStart": [
            {"matcher": "startup", "hooks": [
                {"type": "command", "command": "/usr/bin/true"}]},
        ]},
    }))
    assert do_install(home, reverie_home) == 0
    code, transcript = do_uninstall(home, reverie_home)
    assert code == 0
    assert "Removed the Reverie SessionStart hook" in transcript
    assert settings.exists()                    # user file is never deleted
    assert json.loads(settings.read_text()) == {
        "theme": "dark",
        "hooks": {"SessionStart": [
            {"matcher": "startup", "hooks": [
                {"type": "command", "command": "/usr/bin/true"}]},
        ]},
    }


def test_uninstall_deletes_settings_it_created(home, reverie_home):
    assert do_install(home, reverie_home) == 0
    settings = home / ".claude" / "settings.json"
    assert settings.exists()
    assert do_uninstall(home, reverie_home)[0] == 0
    assert not settings.exists()                # owned + unedited → gone


def test_uninstall_skips_invalid_settings_json(home, reverie_home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{broken")
    code, transcript = do_uninstall(home, reverie_home)
    assert code == 0
    assert "SKIPPED" in transcript
    assert settings.read_text() == "{broken"     # never damaged


def test_uninstall_without_the_hook_is_unchanged(home, reverie_home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json_text({"theme": "dark"}))
    code, transcript = do_uninstall(home, reverie_home)
    assert code == 0
    assert "no Reverie SessionStart hook" in transcript
    assert settings.read_text() == json_text({"theme": "dark"})
