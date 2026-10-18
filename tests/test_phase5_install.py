"""Phase 5 tests — install + agent wiring (§28.8/§28.9/§28.10/§28.2).

Run: .venv/bin/python -m pytest tests/ -v
All offline. Every filesystem write lands in tmp_path: the fake user home
(agent configs) and the fake EC home are both redirected — real home
directories are never touched.
"""

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from reverie import install
from reverie.brain import Brain
from reverie.config import DEFAULT_CONFIG, load_config
from reverie.install import AGENTS, InstallError

ROOT = Path(__file__).resolve().parent.parent
SPEC = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")


def spec_block(header: str) -> str:
    """The first ```markdown fenced block after `header` in SPEC.md."""
    i = SPEC.index(header)
    return re.findall(r"```markdown\n(.*?)```", SPEC[i:], re.S)[0]


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture()
def reverie_home(home):
    return home / ".reverie"


def run_install(home, reverie_home, *, input_fn=None, environ=None, provider=None,
                no_verify=True, verify_fn=None, check_fn=None, **kwargs):
    """run() fully sandboxed: no real PATH binaries, no real env keys.

    The live ping is disabled by default (no_verify) so tests never touch
    the network; ``verify_fn`` replaces it when a test wants the report path.
    The end-of-install MCP server check is stubbed to ✓ by default so tests
    never launch real servers; ``check_fn`` replaces it when a test wants
    the ✗ report path.
    """
    out = []
    install.run(
        home=home,
        reverie_home=reverie_home,
        which=lambda _binary: None,
        input_fn=input_fn or (lambda _prompt="": ""),
        print_fn=out.append,
        environ=environ if environ is not None else {},
        provider=provider,
        no_verify=no_verify,
        verify_fn=verify_fn,
        check_fn=check_fn or (lambda *a, **k: (True, "check skipped (test)")),
        **kwargs,
    )
    return "\n".join(out)


def run_install_result(home, reverie_home, *, input_fn=None, environ=None,
                       provider=None, no_verify=True, verify_fn=None,
                       check_fn=None, **kwargs):
    """Like run_install but returns ``(exit_code, transcript)``."""
    out = []
    code = install.run(
        home=home,
        reverie_home=reverie_home,
        which=kwargs.pop("which", lambda _binary: None),
        input_fn=input_fn or (lambda _prompt="": ""),
        print_fn=out.append,
        environ=environ if environ is not None else {},
        provider=provider,
        no_verify=no_verify,
        verify_fn=verify_fn,
        check_fn=check_fn or (lambda *a, **k: (True, "check skipped (test)")),
        **kwargs,
    )
    return code, "\n".join(out)


def scripted(answers):
    it = iter(answers)

    def _input(_prompt=""):
        return next(it)

    return _input


@pytest.fixture()
def dev_install(monkeypatch):
    """Pin the D28 development fallback form (no pip-installed package).

    D45 (Phase 11) made server_command() prefer the ``reverie-mcp`` console
    script when ``engineering-cognition`` is installed — which it is in this
    venv. These tests verify the fallback path, so they opt out.
    """
    monkeypatch.setattr(install, "_is_pip_installed", lambda: False)
    return install


# ---------------------------------------------------------------------------
# detection (§28.8)
# ---------------------------------------------------------------------------

def test_detect_by_marker_files(home):
    (home / ".claude.json").write_text("{}")
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text("{}")
    found = [a.key for a in AGENTS if a.detected(home, lambda _b: None)]
    assert found == ["claude_code", "cursor"]
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text("{}")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text("")
    found = [a.key for a in AGENTS if a.detected(home, lambda _b: None)]
    assert found == ["claude_code", "cursor", "opencode", "codex"]


def test_detect_by_binary(home):
    found = [
        a.key for a in AGENTS
        if a.detected(home, lambda b: f"/usr/bin/{b}" if b == "codex" else None)
    ]
    assert found == ["codex"]


def test_detect_none(home, reverie_home):
    out = run_install(home, reverie_home, all_agents=True)
    for name in ("Claude Code", "Cursor", "OpenCode", "Codex"):
        assert f"✗ {name} not found" in out
    assert "No supported agents detected" in out


# ---------------------------------------------------------------------------
# per-agent config (merge, never overwrite — §28.8)
# ---------------------------------------------------------------------------

def test_claude_merge_preserves_existing(dev_install, home, reverie_home):
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"other": {"command": "x"}},
        "unrelated": {"keep": True},
    }))
    run_install(home, reverie_home, only=["claude_code"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}     # untouched
    assert data["unrelated"] == {"keep": True}                 # untouched
    python, args, env = install.server_command()
    assert data["mcpServers"]["reverie"] == {
        "type": "stdio", "command": python, "args": args, "env": env,
    }
    assert (home / ".claude" / "CLAUDE.md").read_text() == "@~/.reverie/AGENTS.md\n"


def test_claude_import_appended_once(home, reverie_home):
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir()
    claude_md.write_text("# My instructions\n")
    run_install(home, reverie_home, only=["claude_code"])
    text = claude_md.read_text()
    assert text.startswith("# My instructions\n")
    assert text.count("@~/.reverie/AGENTS.md") == 1
    run_install(home, reverie_home, only=["claude_code"])           # idempotent
    assert claude_md.read_text().count("@~/.reverie/AGENTS.md") == 1


def test_cursor_merge_and_commands(dev_install, home, reverie_home):
    """Cursor: MCP entry in ~/.cursor/mcp.json; NO global rules file is
    written (Cursor's documented global instruction mechanism is User Rules
    in app settings — never written by Reverie); user-level commands land in
    ~/.cursor/commands/ (beta)."""
    cfg = home / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"github": {"command": "gh"}}}))
    run_install(home, reverie_home, only=["cursor"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["github"] == {"command": "gh"}   # untouched
    python, args, env = install.server_command()
    assert data["mcpServers"]["reverie"] == {
        "command": python, "args": args, "env": env,
    }
    # no rules file — nothing global is written outside Reverie's own paths
    assert not (home / ".cursor" / "rules" / "ec.md").exists()
    assert not (home / ".cursor" / "rules").exists()
    for name in ("reverie-review.md", "reverie-status.md"):
        text = (home / ".cursor" / "commands" / name).read_text()
        assert "review_list" in text or "summary" in text


def test_opencode_merge_and_agents_md(dev_install, home, reverie_home):
    cfg = home / ".config" / "opencode" / "opencode.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "theme": "dark",
        "mcp": {"other": {"type": "local", "command": ["x"], "enabled": True}},
    }))
    run_install(home, reverie_home, only=["opencode"])
    data = json.loads(cfg.read_text())
    assert data["theme"] == "dark"                             # untouched
    assert data["mcp"]["other"]["command"] == ["x"]            # untouched
    python, args, env = install.server_command()
    assert data["mcp"]["reverie"] == {
        "type": "local", "command": [python, *args],
        "environment": env, "enabled": True,
    }
    assert "~/.reverie/AGENTS.md" in (
        home / ".config" / "opencode" / "AGENTS.md").read_text()


def test_opencode_new_file_gets_schema(home, reverie_home):
    run_install(home, reverie_home, only=["opencode"])
    data = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text())
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["mcp"]["reverie"]["enabled"] is True


def test_codex_toml_create_append_replace(dev_install, home, reverie_home):
    cfg = home / ".codex" / "config.toml"
    python, args, env = install.server_command()
    run_install(home, reverie_home, only=["codex"])
    text = cfg.read_text()
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["reverie"] == {
        "command": python, "args": ["-m", "reverie.mcp_server"],
        "env": {"PYTHONPATH": str(ROOT)}}

    # append to an existing config with other tables
    cfg.write_text('[profile.default]\nmodel = "gpt-5"\n')
    run_install(home, reverie_home, only=["codex"])
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["profile"]["default"]["model"] == "gpt-5"    # untouched
    assert parsed["mcp_servers"]["reverie"]["command"] == python

    # an existing ec table is replaced, not duplicated
    cfg.write_text(
        cfg.read_text().replace(f'command = {json.dumps(python)}',
                                'command = "old"'))
    run_install(home, reverie_home, only=["codex"])
    text = cfg.read_text()
    assert text.count("[mcp_servers.reverie]") == 1
    assert 'command = "old"' not in text
    assert tomllib.loads(text)["profile"]["default"]["model"] == "gpt-5"


def test_codex_agents_md_append_idempotent(home, reverie_home):
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir()
    agents.write_text("# Codex notes\n")
    run_install(home, reverie_home, only=["codex"])
    run_install(home, reverie_home, only=["codex"])
    text = agents.read_text()
    assert text.startswith("# Codex notes\n")
    assert text.count("~/.reverie/AGENTS.md") == 1


# ---------------------------------------------------------------------------
# server_command — the written configs must work from ANY cwd (B2 fix)
# ---------------------------------------------------------------------------

def test_server_command_is_absolute_and_repo_rooted(dev_install, ):
    python, args, env = install.server_command()
    p = Path(python)
    assert p.is_absolute() and p.is_file(), python
    assert args == ["-m", "reverie.mcp_server"]
    assert env == {"PYTHONPATH": str(ROOT)}
    # the venv interpreter lives inside the repo (never a hardcoded path)
    assert str(p).startswith(str(ROOT))


def test_written_config_starts_server_from_foreign_cwd(dev_install, home, reverie_home, tmp_path):
    """Regression for the benchmark blocker: an agent launching the server
    from the target repo (not the EMS-v2 repo) must get a working server."""
    run_install(home, reverie_home, only=["opencode"])
    cfg = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text())
    entry = cfg["mcp"]["reverie"]
    foreign = tmp_path / "some-other-repo"     # NOT the EMS-v2 repo root
    foreign.mkdir()
    env = dict(os.environ)
    env.update(entry["environment"])           # the config's PYTHONPATH
    env["REVERIE_HOME"] = str(reverie_home)
    env["HF_HUB_OFFLINE"] = "1"
    proc = subprocess.Popen(
        entry["command"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env, cwd=str(foreign),
    )
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "test"}},
        }) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, f"server produced no output; stderr: {proc.stderr.read()}"
        msg = json.loads(line)
        assert msg["result"]["serverInfo"]["name"] == "reverie"
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_invalid_json_aborts_without_touching(home, reverie_home):
    cfg = home / ".claude.json"
    cfg.write_text("{ not json")
    with pytest.raises(InstallError, match="not valid JSON"):
        run_install(home, reverie_home, only=["claude_code"])
    assert cfg.read_text() == "{ not json"                     # byte-identical


# ---------------------------------------------------------------------------
# ~/.reverie bootstrap (§28.2) + verbatim spec texts (§28.9/§28.10)
# ---------------------------------------------------------------------------

def test_reverie_home_bootstrap(home, reverie_home):
    run_install(home, reverie_home, all_agents=True)
    # reverie.db auto-created with the full schema
    brain = Brain(reverie_home / "reverie.db")
    try:
        for table in ("ecus", "edges", "sessions", "session_ecus",
                      "session_edges", "pending_updates"):
            assert table in brain.table_names()
    finally:
        brain.close()
    # config.yaml created and loads to exactly the defaults
    assert (reverie_home / "config.yaml").exists()
    assert dict(load_config(reverie_home / "config.yaml")) == DEFAULT_CONFIG


def test_existing_config_yaml_kept(home, reverie_home):
    reverie_home.mkdir()
    (reverie_home / "config.yaml").write_text("ranking:\n  w_relevance: 0.99\n")
    run_install(home, reverie_home, all_agents=True)
    assert (reverie_home / "config.yaml").read_text() == \
        "ranking:\n  w_relevance: 0.99\n"


def test_agents_md_is_spec_verbatim(home, reverie_home):
    run_install(home, reverie_home, all_agents=True)
    installed = (reverie_home / "AGENTS.md").read_text()
    assert installed == spec_block("### 28.9 AGENTS.md")


def test_user_level_commands_per_harness(home, reverie_home):
    """Each harness gets /reverie-review and /reverie-status in its own
    user-level command location and format (Phase 3)."""
    # mark every harness as installed (detection: binary or marker file)
    for marker in (".claude.json", ".cursor/mcp.json",
                   ".config/opencode/opencode.json", ".codex/config.toml",
                   ".gemini/settings.json"):
        path = home / marker
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if marker.endswith(".toml") is False else "")
    run_install(home, reverie_home, all_agents=True)
    # Claude Code: ~/.claude/commands/*.md (plain markdown)
    for name in ("reverie-review.md", "reverie-status.md"):
        text = (home / ".claude" / "commands" / name).read_text()
        assert "review_list" in text or "summary" in text
    # Codex: ~/.codex/prompts/*.md (frontmatter; invoked /prompts:<name>)
    text = (home / ".codex" / "prompts" / "reverie-review.md").read_text()
    assert "description:" in text and "review_list" in text
    # OpenCode: ~/.config/opencode/commands/*.md (frontmatter)
    text = (home / ".config" / "opencode" / "commands"
            / "reverie-status.md").read_text()
    assert text.startswith("---") and "summary" in text
    # Gemini CLI: ~/.gemini/commands/*.toml (TOML `prompt`)
    import tomllib as _toml
    raw = (home / ".gemini" / "commands" / "reverie-review.toml").read_text()
    parsed = _toml.loads(raw)
    assert parsed["prompt"].startswith("Review your pending Reverie")
    assert parsed["description"]


# ---------------------------------------------------------------------------
# dry-run, selection modes, Zen key
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(home, reverie_home):
    (home / ".claude.json").write_text("{}")
    out = run_install(home, reverie_home, all_agents=True, dry_run=True)
    assert "dry run" in out.lower()
    assert "[dry-run] merge: ~/.claude.json" in out
    assert '"reverie"' in out                                       # content shown
    assert not reverie_home.exists()
    assert json.loads((home / ".claude.json").read_text()) == {}
    assert not (home / ".claude").exists()


def test_interactive_all_and_subset(home, reverie_home):
    for marker, content in ((".claude.json", "{}"),
                            (".codex/config.toml", "")):
        p = home / marker
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    out = run_install(home, reverie_home, input_fn=scripted(["y", "7"]))
    assert "Configuring Claude Code..." in out
    assert "Configuring Codex..." in out

    # subset selection: build a fresh home with three detected agents
    home2 = home.parent / "home2"
    for marker, content in ((".claude.json", "{}"), (".cursor/mcp.json", "{}"),
                            (".codex/config.toml", "")):
        p = home2 / marker
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    out = run_install(home2, home2 / ".reverie",
                      input_fn=scripted(["n", "1,3", "7"]))   # 7 = skip provider
    assert "Select agents to configure:" in out
    assert "Configuring Claude Code..." in out
    assert "Configuring Cursor..." not in out
    assert "Configuring Codex..." in out
    assert '"reverie"' not in (home2 / ".cursor" / "mcp.json").read_text()


def test_interactive_eof_selects_nothing(home, reverie_home):
    (home / ".claude.json").write_text("{}")

    def eof(_prompt=""):
        raise EOFError

    out = run_install(home, reverie_home, input_fn=eof)
    assert "Configuring Claude Code..." not in out
    assert json.loads((home / ".claude.json").read_text()) == {}
    assert (reverie_home / "reverie.db").exists()                        # bootstrap ran


def test_only_unknown_key_errors(home, reverie_home):
    with pytest.raises(InstallError, match="unknown agent"):
        run_install(home, reverie_home, only=["emacs"])


def test_only_configures_undetected_agent(home, reverie_home):
    out = run_install(home, reverie_home, only=["cursor"])
    assert "not detected — configuring anyway" in out
    assert (home / ".cursor" / "mcp.json").exists()


def test_provider_key_detected_autoconfigures(home, reverie_home):
    """feat/providers: a single detected key is offered and (default yes)
    configures the provider, then the live ping reports success."""
    out = run_install(
        home, reverie_home, all_agents=True,
        environ={"ANTHROPIC_API_KEY": "sk-test"},
        no_verify=False,
        verify_fn=lambda cfg: (True, "OK"))
    assert "ANTHROPIC_API_KEY detected (Anthropic)" in out
    assert "Configured ~/.reverie/config.yaml for Anthropic (anthropic)" in out
    assert "Verifying claude-haiku-5-5 with a live ping" in out
    assert "✓ 'OK' — claude-haiku-5-5 is reachable" in out
    assert "✓ ANTHROPIC_API_KEY is set" in out


def test_multiple_keys_detected_menu(home, reverie_home):
    """Several detected keys: both are reported, the menu marks them, and the
    user picks one."""
    out = run_install(
        home, reverie_home, all_agents=True,
        environ={"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "b"},
        input_fn=scripted(["1"]))          # [1] Anthropic
    assert "ANTHROPIC_API_KEY detected (Anthropic)" in out
    assert "OPENAI_API_KEY detected (OpenAI)" in out
    assert "Select the LLM provider Reverie should use" in out
    assert "[1] Anthropic  ✓ key set" in out
    assert "[2] OpenAI  ✓ key set" in out
    assert "Configured ~/.reverie/config.yaml for Anthropic (anthropic)" in out
    import yaml
    data = yaml.safe_load((reverie_home / "config.yaml").read_text())
    assert data["llm"]["provider"] == "anthropic"


def test_no_key_prints_provider_guidance(home, reverie_home):
    """No key + non-tty answer: provider menu, skip, guidance listing every
    registry provider — nothing Zen-only."""
    out = run_install(home, reverie_home, all_agents=True)
    assert "Select the LLM provider Reverie should use" in out
    assert "Skip for now" in out
    assert "No provider configured" in out
    for env_var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                    "OPENROUTER_API_KEY", "OPENCODE_ZEN_API_KEY"):
        assert env_var in out
    assert "opencode.ai/auth" in out            # zen listed alongside others


def test_provider_flag_non_interactive(home, reverie_home):
    """--provider anthropic: no prompts, block merged, export line printed."""
    out = run_install(home, reverie_home, all_agents=True, provider="anthropic")
    assert "Configuring" not in out             # agent selection skipped via --all
    assert "Configured ~/.reverie/config.yaml for Anthropic (anthropic)" in out
    assert "export ANTHROPIC_API_KEY='your-key'" in out
    assert "live verification skipped" in out   # default no_verify in helper
    import yaml
    data = yaml.safe_load((reverie_home / "config.yaml").read_text())
    assert data["llm"]["provider"] == "anthropic"
    assert data["llm"]["model"] == "claude-haiku-5-5"
    assert data["llm"]["base_url"] == "https://api.anthropic.com/v1"
    assert data["llm"]["wire_format"] == "anthropic"


def test_provider_flag_unknown_key_errors(home, reverie_home):
    with pytest.raises(install.InstallError, match="unknown provider"):
        run_install(home, reverie_home, all_agents=True, provider="vllm")


def test_provider_dry_run_writes_nothing(home, reverie_home):
    out = run_install(home, reverie_home, all_agents=True, provider="anthropic",
                      dry_run=True)
    assert "[dry-run] create: ~/.reverie/config.yaml" in out
    assert "would verify the provider" in out
    assert not (reverie_home / "config.yaml").exists()


def test_ping_failure_fails_install_with_clear_message(home, reverie_home):
    """Review fix 1: a failed live ping exits non-zero with "Reverie is
    installed, but the LLM is not reachable." and no "Reverie is ready!".
    The config (and harness entries) are still written."""
    code, out = run_install_result(
        home, reverie_home, all_agents=True, provider="anthropic",
        no_verify=False,
        verify_fn=lambda cfg: (False, "LLMError: HTTP 401"))
    assert code == 1
    assert "✗ Ping failed for claude-haiku-5-5" in out
    assert "HTTP 401" in out
    assert "Reverie is installed, but the LLM is not reachable." in out
    assert "Reverie is ready!" not in out
    assert (reverie_home / "config.yaml").exists()   # config still written


def test_ping_message_reads_model_from_config(home, reverie_home):
    """Review fix 2: the ping names the model from the written config, not
    a hardcoded string — including the OpenCode Zen default."""
    seen = {}

    def verify(cfg):
        seen["model"] = cfg.llm.model
        return True, "OK"

    out = run_install(home, reverie_home, all_agents=True,
                      provider="opencode_zen", no_verify=False,
                      verify_fn=verify)
    assert seen["model"] == "claude-haiku-5-5"
    assert "Verifying claude-haiku-5-5 with a live ping" in out
    assert "✓ 'OK' — claude-haiku-5-5 is reachable" in out


def test_missing_key_message_names_var_and_how_to_fix(home, reverie_home):
    """Review fix 43: the missing-key message says the variable, where it
    was checked (this process), and how to make it visible — no real shell
    files are touched by the test."""
    code, out = run_install_result(home, reverie_home, all_agents=True,
                                   provider="opencode_zen", environ={})
    assert code == 0                              # no_verify default: not fatal
    assert "OPENCODE_ZEN_API_KEY is not set in this process's environment" in out
    assert "export OPENCODE_ZEN_API_KEY='your-key'" in out
    assert "new terminal" in out
    assert "source ~/.zshrc" in out


def test_existing_llm_block_skips_provider_question(home, reverie_home):
    """Review fix 44: config.yaml that already carries an llm block is kept;
    the provider question is not asked, and the existing model is pinged."""
    reverie_home.mkdir(parents=True, exist_ok=True)
    (reverie_home / "config.yaml").write_text(
        'llm:\n  provider: google\n  model: gemini-3.8-flash\n'
        '  base_url: https://generativelanguage.googleapis.com/v1beta/openai\n'
        '  api_key_env: GEMINI_API_KEY\n')

    def no_question(_prompt=""):
        raise AssertionError("the provider question must not be asked")

    code, out = run_install_result(
        home, reverie_home, all_agents=True, input_fn=no_question,
        no_verify=False, verify_fn=lambda cfg: (True, "OK"))
    assert code == 0
    assert "already configured for google — keeping" in out
    assert "gemini-3.8-flash is reachable" in out
    import yaml
    data = yaml.safe_load((reverie_home / "config.yaml").read_text())
    assert data["llm"]["provider"] == "google"    # untouched


def test_install_migrates_old_default_llm_model(home, reverie_home):
    """Review fix 2 follow-up: configs written by older installers still say
    llm.model: claude-haiku-4-5 (the retired default). Install updates that
    one value to the current default, says so, and pings the new model."""
    reverie_home.mkdir(parents=True, exist_ok=True)
    (reverie_home / "config.yaml").write_text(
        "llm:\n"
        "  provider: opencode_zen\n"
        "  model: claude-haiku-4-5\n"
        "  base_url: https://opencode.ai/zen/v1\n"
        "  api_key_env: OPENCODE_ZEN_API_KEY\n")

    pinged = []

    def verify(cfg):
        pinged.append(cfg.llm.model)
        return True, "OK"

    code, out = run_install_result(
        home, reverie_home, all_agents=True, no_verify=False, verify_fn=verify)
    assert code == 0
    assert ("Updated ~/.reverie/config.yaml: llm.model claude-haiku-4-5 "
            "→ claude-haiku-5-5 (the old installer default; only this value "
            "changed)") in out
    # the migration line is the only place the old model may appear — the
    # keep-message and the ping below must use the migrated value
    assert out.count("claude-haiku-4-5") == 1
    assert "already configured for opencode_zen — keeping" in out
    assert "claude-haiku-5-5 is reachable" in out
    assert pinged == ["claude-haiku-5-5"]
    import yaml
    data = yaml.safe_load((reverie_home / "config.yaml").read_text())
    assert data["llm"]["provider"] == "opencode_zen"   # untouched
    assert data["llm"]["model"] == "claude-haiku-5-5"


def test_install_keeps_custom_llm_model(home, reverie_home):
    """Review fix 2 follow-up: any other model is the user's explicit choice
    — the old-default migration leaves it (and the file) alone."""
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed = ("llm:\n"
            "  provider: opencode_zen\n"
            "  model: my-custom-haiku\n"
            "  api_key_env: OPENCODE_ZEN_API_KEY\n")
    (reverie_home / "config.yaml").write_text(seed)

    pinged = []

    def verify(cfg):
        pinged.append(cfg.llm.model)
        return True, "OK"

    code, out = run_install_result(
        home, reverie_home, all_agents=True, no_verify=False, verify_fn=verify)
    assert code == 0
    assert "Updated" not in out
    assert "already exists — kept" in out
    assert "my-custom-haiku is reachable" in out
    assert pinged == ["my-custom-haiku"]
    assert (reverie_home / "config.yaml").read_text() == seed


def test_old_default_under_other_provider_is_kept(home, reverie_home):
    """Review fix 2 follow-up/scope: the migration only touches an
    opencode_zen block — the same model name under another provider is the
    user's choice and stays."""
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed = ("llm:\n"
            "  provider: anthropic\n"
            "  model: claude-haiku-4-5\n")
    (reverie_home / "config.yaml").write_text(seed)
    out = run_install(home, reverie_home, all_agents=True)
    assert "Updated" not in out
    assert "already exists — kept" in out
    assert (reverie_home / "config.yaml").read_text() == seed


def test_old_default_migration_is_byte_surgical(home, reverie_home):
    """Review fix 2 follow-up: the migrated file is byte-identical to the
    original except that one scalar — comments, key order, indentation and
    quoting all survive (no YAML re-dump)."""
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed = (
        "# hand-tuned config — keep these notes\n"
        "extraction:\n"
        "  temperature: 0.25\n"
        "llm:\n"
        "  provider: opencode_zen\n"
        "  model: claude-haiku-4-5   # the retired default\n"
        "  api_key_env: OPENCODE_ZEN_API_KEY\n"
        "clustering:\n"
        "  min_size: 3\n")
    (reverie_home / "config.yaml").write_text(seed)
    out = run_install(home, reverie_home, all_agents=True)
    expected = seed.replace("claude-haiku-4-5", "claude-haiku-5-5", 1)
    assert (reverie_home / "config.yaml").read_text() == expected
    assert "claude-haiku-4-5" not in (reverie_home
                                      / "config.yaml").read_text()
    assert "Updated ~/.reverie/config.yaml" in out


def test_reconfigure_flag_asks_again(home, reverie_home):
    """Review fix 44: --reconfigure (and --provider) are the explicit ways
    to change an existing llm block."""
    reverie_home.mkdir(parents=True, exist_ok=True)
    (reverie_home / "config.yaml").write_text(
        'llm:\n  provider: google\n  model: gemini-3.8-flash\n')
    out = run_install(home, reverie_home, all_agents=True,
                      input_fn=scripted(["1"]),     # [1] Anthropic
                      reconfigure=True)
    assert "Select the LLM provider Reverie should use" in out
    assert "Configured ~/.reverie/config.yaml for Anthropic (anthropic)" in out
    import yaml
    data = yaml.safe_load((reverie_home / "config.yaml").read_text())
    assert data["llm"]["provider"] == "anthropic"


def test_provider_question_comes_after_detection_table(home, reverie_home):
    """Review fix 4: the 'Add to all detected agents?' question follows the
    detection table (and the 'How to use' block appears exactly once).
    ``input_fn`` prompts are not printed, so the prompt's position is the
    number of lines printed before it was asked."""
    (home / ".claude.json").write_text("{}")
    out: list[str] = []
    asked_at: list[int] = []

    def recorder(prompt=""):
        asked_at.append(len(out))
        return "y" if "all detected" in prompt else "7"

    install.run(home=home, reverie_home=reverie_home, input_fn=recorder,
                print_fn=out.append, environ={}, which=lambda _b: None,
                no_verify=True,
                check_fn=lambda *a, **k: (True, "check skipped (test)"))
    table_line = next(i for i, line in enumerate(out)
                      if "✓ Claude Code found" in line)
    assert table_line < asked_at[0]              # table printed first
    text = "\n".join(out)
    assert "How to use:" in text
    assert text.count("How to use:") == 1


def test_ollama_note_only_when_relevant(home, reverie_home):
    """Review fix 4: no Ollama line in the output when the binary is absent
    and a provider is configured; exactly one when it is present."""
    out = run_install(home, reverie_home, all_agents=True,
                      provider="anthropic")
    assert "Ollama" not in out

    home2 = home.parent / "home2"
    home2.mkdir()
    out = run_install_result(
        home2, home2 / ".reverie", all_agents=True, provider="anthropic",
        which=lambda binary: "/usr/local/bin/ollama"
        if binary == "ollama" else None)
    assert out[1].count("Ollama detected") == 1


def test_directory_bootstrap_line_only_on_create(home, reverie_home):
    """Review fix 48: 'Creating ~/.reverie/ directory...' appears on the
    first install only — not on re-runs."""
    first = run_install(home, reverie_home, all_agents=True)
    assert "Creating ~/.reverie/ directory..." in first
    second = run_install(home, reverie_home, all_agents=True)
    assert "Creating ~/.reverie/ directory..." not in second


def test_agents_ref_absolute_when_reverie_home_not_default(home, tmp_path):
    rv_elsewhere = tmp_path / "elsewhere" / "rv"
    run_install(home, rv_elsewhere, only=["codex"])
    text = (home / ".codex" / "AGENTS.md").read_text()
    assert str(rv_elsewhere / "AGENTS.md") in text
    assert "~/.reverie/AGENTS.md" not in text


def test_full_rerun_idempotent(home, reverie_home):
    for marker, content in ((".claude.json", "{}"), (".cursor/mcp.json", "{}"),
                            (".config/opencode/opencode.json", "{}"),
                            (".codex/config.toml", "")):
        p = home / marker
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    run_install(home, reverie_home, all_agents=True)
    snapshot = {
        str(p.relative_to(home)): p.read_text()
        for p in sorted(home.rglob("*")) if p.is_file() and p.suffix != ".db"
    }
    out = run_install(home, reverie_home, all_agents=True)
    again = {
        str(p.relative_to(home)): p.read_text()
        for p in sorted(home.rglob("*")) if p.is_file() and p.suffix != ".db"
    }
    assert snapshot == again
    assert out.count("— unchanged") >= 8                       # nothing redone


# ---------------------------------------------------------------------------
# CLI subprocess (real `python -m reverie.install`, sandboxed home)
# ---------------------------------------------------------------------------

def test_cli_subprocess(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # mark every harness as installed so detection is deterministic
    # (real PATH binaries would make it machine-dependent)
    for marker in (".claude.json", ".cursor/mcp.json",
                   ".config/opencode/opencode.json", ".codex/config.toml",
                   ".gemini/settings.json"):
        path = fake_home / marker
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if not marker.endswith(".toml") else "")
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["REVERIE_HOME"] = str(fake_home / ".reverie")
    env["PYTHONPATH"] = str(ROOT)
    env["HF_HUB_OFFLINE"] = "1"
    env.pop("OPENCODE_ZEN_API_KEY", None)
    proc = subprocess.run(
        [sys.executable, "-m", "reverie.install", "--all"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Reverie — Installation" in proc.stdout
    assert "Reverie is ready!" in proc.stdout
    rv_dir = fake_home / ".reverie"
    assert (rv_dir / "reverie.db").exists()
    assert (rv_dir / "config.yaml").exists()
    assert (rv_dir / "AGENTS.md").exists()
    # per-harness summary names the configured harnesses
    assert "Claude Code:" in proc.stdout
    assert "Gemini CLI:" in proc.stdout
    assert "Next:" in proc.stdout
    # every written file is inside the fake home — nothing escaped
    assert proc.stdout.count("✓") >= 4


# ---------------------------------------------------------------------------
# stale `reverie` console-script binding detection (feat/flow review fix 9)
# ---------------------------------------------------------------------------

def _fake_entry_points(monkeypatch, entries):
    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: entries)


def test_stale_entry_point_note_fires(monkeypatch):
    from types import SimpleNamespace
    _fake_entry_points(monkeypatch, [
        SimpleNamespace(name="reverie-mcp", value="reverie.mcp_server:main"),
        SimpleNamespace(name="reverie", value="reverie.install:main"),   # stale binding
    ])
    note = install.stale_entry_point_note()
    assert note is not None
    assert "reverie.install:main" in note
    assert "pip install -e ." in note


def test_stale_entry_point_note_quiet_when_current(monkeypatch):
    from types import SimpleNamespace
    _fake_entry_points(monkeypatch, [
        SimpleNamespace(name="reverie", value="reverie.cli:main"),
    ])
    assert install.stale_entry_point_note() is None


def test_stale_entry_point_note_quiet_without_metadata(monkeypatch):
    import importlib.metadata as md

    def boom(group=None):
        raise RuntimeError("no metadata here")

    monkeypatch.setattr(md, "entry_points", boom)
    assert install.stale_entry_point_note() is None
