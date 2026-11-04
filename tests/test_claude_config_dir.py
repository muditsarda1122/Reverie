"""Claude Code honours $CLAUDE_CONFIG_DIR; so must the installer.

Found by installing for real: with CLAUDE_CONFIG_DIR=~/.claude-work the
installer wrote hooks to ~/.claude, which Claude never reads, so no hook ever
fired and the prompt log stayed empty."""

import json

from reverie.integrations.claude import ClaudeIntegration


def _apply(writes):
    for w in writes:
        if w.content is None:
            continue
        w.path.parent.mkdir(parents=True, exist_ok=True)
        w.path.write_text(w.content, encoding="utf-8")


def test_default_layout_is_unchanged(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _apply(ClaudeIntegration().install(home, reverie_home=home / ".reverie"))
    assert (home / ".claude" / "settings.json").exists()
    assert "reverie" in json.loads((home / ".claude.json").read_text())["mcpServers"]


def test_config_dir_env_moves_everything(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = tmp_path / "elsewhere"
    home.mkdir()
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    integ = ClaudeIntegration()
    _apply(integ.install(home, reverie_home=home / ".reverie"))
    hooks = json.loads((cfg / "settings.json").read_text())["hooks"]
    assert {"SessionStart", "Stop", "UserPromptSubmit", "PreToolUse"} <= set(hooks)
    assert (cfg / "CLAUDE.md").exists() and (cfg / "commands").is_dir()
    assert "reverie" in json.loads((cfg / ".claude.json").read_text())["mcpServers"]
    assert not (home / ".claude").exists() and not (home / ".claude.json").exists()
    assert integ.configured_server(home) is not None
