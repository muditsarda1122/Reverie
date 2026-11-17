import io
import json
import sys
import tomllib

from reverie import spool
from reverie.integrations.codex import HOOKS_START, INTEGRATION, shim

SECRET = "sk-ant-api03-Qw8ErTy2UiOp4AsDf6GhJk9LzXc1VbNm"


def _feed(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _apply(writes):
    for w in writes:
        if w.content is not None:
            w.path.parent.mkdir(parents=True, exist_ok=True)
            w.path.write_text(w.content)


def test_install_adds_three_hooks_and_keeps_the_mcp_table(tmp_path):
    home = tmp_path / "h"
    _apply(INTEGRATION.install(home, reverie_home=tmp_path / "rh"))
    cfg = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert cfg["features"]["hooks"] is True
    assert "reverie" in cfg["mcp_servers"]
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        cmd = cfg["hooks"][event][0]["hooks"][0]["command"]
        assert "codex" in cmd and cfg["hooks"][event][0]["hooks"][0]["type"] == "command"
    # idempotent
    again = INTEGRATION.install(home, reverie_home=tmp_path / "rh")
    assert all(w.action == "unchanged" for w in again if w.path.name == "config.toml")


def test_existing_config_and_features_table_are_respected(tmp_path):
    home = tmp_path / "h"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        'model = "x"\n[features]\nfoo = true\n[mcp_servers.other]\ncommand = "y"\n')
    _apply(INTEGRATION.install(home, reverie_home=tmp_path / "rh"))
    text = (home / ".codex" / "config.toml").read_text()
    cfg = tomllib.loads(text)                       # still valid TOML (no duplicate [features])
    assert cfg["model"] == "x" and cfg["features"]["foo"] is True
    assert "other" in cfg["mcp_servers"] and "reverie" in cfg["mcp_servers"]
    assert "existing [features] table" in text      # tells the user to set hooks = true


def test_uninstall_removes_hooks_and_table_but_keeps_user_config(tmp_path):
    home = tmp_path / "h"
    (home / ".codex").mkdir(parents=True)
    path = home / ".codex" / "config.toml"
    path.write_text('model = "x"\n')
    _apply(INTEGRATION.install(home, reverie_home=tmp_path / "rh"))
    assert HOOKS_START in path.read_text()
    for r in INTEGRATION.uninstall(home, reverie_home=tmp_path / "rh"):
        if r.path == path and r.after is not None:
            path.write_text(r.after)
    left = tomllib.loads(path.read_text())
    assert left == {"model": "x"} and HOOKS_START not in path.read_text()


def test_shim_prompt_stop_start(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "rules.md").write_text("- never retry ReadTimeout\n")
    monkeypatch.setenv("REVERIE_HOME", str(home))
    repo = tmp_path / "r"
    repo.mkdir()
    _feed(monkeypatch, {"cwd": str(repo), "session_id": "s1",
                        "prompt": f"keep going, key {SECRET}"})
    assert shim.main(["prompt"]) == 0
    rec = spool.read(repo)[0]
    assert rec["harness"] == "codex" and rec["session"] == "s1" and SECRET not in rec["text"]
    _feed(monkeypatch, {"cwd": str(repo)})
    assert shim.main(["start"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "never retry ReadTimeout" in out["hookSpecificOutput"]["additionalContext"]
    _feed(monkeypatch, {"cwd": str(repo)})
    assert shim.main(["stop"]) == 0                 # REVERIE_NO_IMPORT: a no-op here, never raises
    monkeypatch.setattr(sys, "stdin", io.StringIO("garbage"))
    assert shim.main(["prompt"]) == 0 and shim.main(["zzz"]) == 0
