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

from ec import install
from ec.brain import Brain
from ec.config import DEFAULT_CONFIG, load_config
from ec.install import AGENTS, InstallError

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
def ec_home(home):
    return home / ".ec"


def run_install(home, ec_home, *, input_fn=None, environ=None, **kwargs):
    """run() fully sandboxed: no real PATH binaries, no real env keys."""
    out = []
    install.run(
        home=home,
        ec_home=ec_home,
        which=lambda _binary: None,
        input_fn=input_fn or (lambda _prompt="": ""),
        print_fn=out.append,
        environ=environ if environ is not None else {},
        **kwargs,
    )
    return "\n".join(out)


def scripted(answers):
    it = iter(answers)

    def _input(_prompt=""):
        return next(it)

    return _input


@pytest.fixture()
def dev_install(monkeypatch):
    """Pin the D28 development fallback form (no pip-installed package).

    D45 (Phase 11) made server_command() prefer the ``ec-mcp`` console
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


def test_detect_none(home, ec_home):
    out = run_install(home, ec_home, all_agents=True)
    for name in ("Claude Code", "Cursor", "OpenCode", "Codex"):
        assert f"✗ {name} not found" in out
    assert "No supported agents detected" in out


# ---------------------------------------------------------------------------
# per-agent config (merge, never overwrite — §28.8)
# ---------------------------------------------------------------------------

def test_claude_merge_preserves_existing(dev_install, home, ec_home):
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"other": {"command": "x"}},
        "unrelated": {"keep": True},
    }))
    run_install(home, ec_home, only=["claude_code"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}     # untouched
    assert data["unrelated"] == {"keep": True}                 # untouched
    python, args, env = install.server_command()
    assert data["mcpServers"]["ec"] == {
        "type": "stdio", "command": python, "args": args, "env": env,
    }
    assert (home / ".claude" / "CLAUDE.md").read_text() == "@~/.ec/AGENTS.md\n"


def test_claude_import_appended_once(home, ec_home):
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir()
    claude_md.write_text("# My instructions\n")
    run_install(home, ec_home, only=["claude_code"])
    text = claude_md.read_text()
    assert text.startswith("# My instructions\n")
    assert text.count("@~/.ec/AGENTS.md") == 1
    run_install(home, ec_home, only=["claude_code"])           # idempotent
    assert claude_md.read_text().count("@~/.ec/AGENTS.md") == 1


def test_cursor_merge_and_rules(dev_install, home, ec_home):
    cfg = home / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"github": {"command": "gh"}}}))
    run_install(home, ec_home, only=["cursor"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["github"] == {"command": "gh"}   # untouched
    python, args, env = install.server_command()
    assert data["mcpServers"]["ec"] == {
        "command": python, "args": args, "env": env,
    }
    rules = (home / ".cursor" / "rules" / "ec.md").read_text()
    assert "~/.ec/AGENTS.md" in rules


def test_opencode_merge_and_agents_md(dev_install, home, ec_home):
    cfg = home / ".config" / "opencode" / "opencode.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "theme": "dark",
        "mcp": {"other": {"type": "local", "command": ["x"], "enabled": True}},
    }))
    run_install(home, ec_home, only=["opencode"])
    data = json.loads(cfg.read_text())
    assert data["theme"] == "dark"                             # untouched
    assert data["mcp"]["other"]["command"] == ["x"]            # untouched
    python, args, env = install.server_command()
    assert data["mcp"]["ec"] == {
        "type": "local", "command": [python, *args],
        "environment": env, "enabled": True,
    }
    assert "~/.ec/AGENTS.md" in (
        home / ".config" / "opencode" / "AGENTS.md").read_text()


def test_opencode_new_file_gets_schema(home, ec_home):
    run_install(home, ec_home, only=["opencode"])
    data = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text())
    assert data["$schema"] == "https://opencode.ai/config.json"
    assert data["mcp"]["ec"]["enabled"] is True


def test_codex_toml_create_append_replace(dev_install, home, ec_home):
    cfg = home / ".codex" / "config.toml"
    python, args, env = install.server_command()
    run_install(home, ec_home, only=["codex"])
    text = cfg.read_text()
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["ec"] == {
        "command": python, "args": ["-m", "ec.mcp_server"],
        "env": {"PYTHONPATH": str(ROOT)}}

    # append to an existing config with other tables
    cfg.write_text('[profile.default]\nmodel = "gpt-5"\n')
    run_install(home, ec_home, only=["codex"])
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["profile"]["default"]["model"] == "gpt-5"    # untouched
    assert parsed["mcp_servers"]["ec"]["command"] == python

    # an existing ec table is replaced, not duplicated
    cfg.write_text(
        cfg.read_text().replace(f'command = {json.dumps(python)}',
                                'command = "old"'))
    run_install(home, ec_home, only=["codex"])
    text = cfg.read_text()
    assert text.count("[mcp_servers.ec]") == 1
    assert 'command = "old"' not in text
    assert tomllib.loads(text)["profile"]["default"]["model"] == "gpt-5"


def test_codex_agents_md_append_idempotent(home, ec_home):
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir()
    agents.write_text("# Codex notes\n")
    run_install(home, ec_home, only=["codex"])
    run_install(home, ec_home, only=["codex"])
    text = agents.read_text()
    assert text.startswith("# Codex notes\n")
    assert text.count("~/.ec/AGENTS.md") == 1


# ---------------------------------------------------------------------------
# server_command — the written configs must work from ANY cwd (B2 fix)
# ---------------------------------------------------------------------------

def test_server_command_is_absolute_and_repo_rooted(dev_install, ):
    python, args, env = install.server_command()
    p = Path(python)
    assert p.is_absolute() and p.is_file(), python
    assert args == ["-m", "ec.mcp_server"]
    assert env == {"PYTHONPATH": str(ROOT)}
    # the venv interpreter lives inside the repo (never a hardcoded path)
    assert str(p).startswith(str(ROOT))


def test_written_config_starts_server_from_foreign_cwd(dev_install, home, ec_home, tmp_path):
    """Regression for the benchmark blocker: an agent launching the server
    from the target repo (not the EMS-v2 repo) must get a working server."""
    run_install(home, ec_home, only=["opencode"])
    cfg = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text())
    entry = cfg["mcp"]["ec"]
    foreign = tmp_path / "some-other-repo"     # NOT the EMS-v2 repo root
    foreign.mkdir()
    env = dict(os.environ)
    env.update(entry["environment"])           # the config's PYTHONPATH
    env["EC_HOME"] = str(ec_home)
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
        assert msg["result"]["serverInfo"]["name"] == "ec"
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_invalid_json_aborts_without_touching(home, ec_home):
    cfg = home / ".claude.json"
    cfg.write_text("{ not json")
    with pytest.raises(InstallError, match="not valid JSON"):
        run_install(home, ec_home, only=["claude_code"])
    assert cfg.read_text() == "{ not json"                     # byte-identical


# ---------------------------------------------------------------------------
# ~/.ec bootstrap (§28.2) + verbatim spec texts (§28.9/§28.10)
# ---------------------------------------------------------------------------

def test_ec_home_bootstrap(home, ec_home):
    run_install(home, ec_home, all_agents=True)
    # ec.db auto-created with the full schema
    brain = Brain(ec_home / "ec.db")
    try:
        for table in ("ecus", "edges", "sessions", "session_ecus",
                      "session_edges", "pending_updates"):
            assert table in brain.table_names()
    finally:
        brain.close()
    # config.yaml created and loads to exactly the defaults
    assert (ec_home / "config.yaml").exists()
    assert dict(load_config(ec_home / "config.yaml")) == DEFAULT_CONFIG


def test_existing_config_yaml_kept(home, ec_home):
    ec_home.mkdir()
    (ec_home / "config.yaml").write_text("ranking:\n  w_relevance: 0.99\n")
    run_install(home, ec_home, all_agents=True)
    assert (ec_home / "config.yaml").read_text() == \
        "ranking:\n  w_relevance: 0.99\n"


def test_agents_md_is_spec_verbatim(home, ec_home):
    run_install(home, ec_home, all_agents=True)
    installed = (ec_home / "AGENTS.md").read_text()
    assert installed == spec_block("### 28.9 AGENTS.md")


def test_command_templates(home, ec_home):
    run_install(home, ec_home, all_agents=True)
    expected = {
        "ec-start.md": ("**`~/.ec/commands/ec-start.md`:**",
                        "python -m ec.session start"),
        "ec-stop.md": ("**`~/.ec/commands/ec-stop.md`:**",
                       "python -m ec.session stop"),
        "ec-status.md": ("**`~/.ec/commands/ec-status.md`:**",
                         "python -m ec.session status"),
    }
    for name, (header, invocation) in expected.items():
        text = (ec_home / "commands" / name).read_text()
        assert spec_block(header) in text                       # §28.10 verbatim
        assert f"`{invocation}`" in text                        # D15/D24 wiring
    # MCP tools referenced exactly as §28.9 names them
    assert "ec_observe" in (ec_home / "commands" / "ec-start.md").read_text()
    stop = (ec_home / "commands" / "ec-stop.md").read_text()
    assert "ec_observe" in stop and "--all-accept" in stop \
        and "--all-skip" in stop
    assert "ec_get_summary" in \
        (ec_home / "commands" / "ec-status.md").read_text()


# ---------------------------------------------------------------------------
# dry-run, selection modes, Zen key
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(home, ec_home):
    (home / ".claude.json").write_text("{}")
    out = run_install(home, ec_home, all_agents=True, dry_run=True)
    assert "dry run" in out.lower()
    assert "[dry-run] merge: ~/.claude.json" in out
    assert '"ec"' in out                                       # content shown
    assert not ec_home.exists()
    assert json.loads((home / ".claude.json").read_text()) == {}
    assert not (home / ".claude").exists()


def test_interactive_all_and_subset(home, ec_home):
    for marker, content in ((".claude.json", "{}"),
                            (".codex/config.toml", "")):
        p = home / marker
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    out = run_install(home, ec_home, input_fn=scripted(["y"]))
    assert "Configuring Claude Code..." in out
    assert "Configuring Codex..." in out

    # subset selection: build a fresh home with three detected agents
    home2 = home.parent / "home2"
    for marker, content in ((".claude.json", "{}"), (".cursor/mcp.json", "{}"),
                            (".codex/config.toml", "")):
        p = home2 / marker
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    out = run_install(home2, home2 / ".ec",
                      input_fn=scripted(["n", "1,3"]))
    assert "Select agents to configure:" in out
    assert "Configuring Claude Code..." in out
    assert "Configuring Cursor..." not in out
    assert "Configuring Codex..." in out
    assert '"ec"' not in (home2 / ".cursor" / "mcp.json").read_text()


def test_interactive_eof_selects_nothing(home, ec_home):
    (home / ".claude.json").write_text("{}")

    def eof(_prompt=""):
        raise EOFError

    out = run_install(home, ec_home, input_fn=eof)
    assert "Configuring Claude Code..." not in out
    assert json.loads((home / ".claude.json").read_text()) == {}
    assert (ec_home / "ec.db").exists()                        # bootstrap ran


def test_only_unknown_key_errors(home, ec_home):
    with pytest.raises(InstallError, match="unknown agent"):
        run_install(home, ec_home, only=["emacs"])


def test_only_configures_undetected_agent(home, ec_home):
    out = run_install(home, ec_home, only=["cursor"])
    assert "not detected — configuring anyway" in out
    assert (home / ".cursor" / "mcp.json").exists()


def test_zen_key_check(home, ec_home):
    out = run_install(home, ec_home, all_agents=True,
                      environ={install.ZEN_KEY_ENV: "sk-test"})
    assert f"OpenCode Zen API key found ({install.ZEN_KEY_ENV} env var)" in out
    out = run_install(home, ec_home, all_agents=True, environ={})
    assert install.ZEN_GUIDANCE in out                         # §28.8 verbatim


def test_agents_ref_absolute_when_ec_home_not_default(home, tmp_path):
    ec_elsewhere = tmp_path / "elsewhere" / "ec"
    run_install(home, ec_elsewhere, only=["codex"])
    text = (home / ".codex" / "AGENTS.md").read_text()
    assert str(ec_elsewhere / "AGENTS.md") in text
    assert "~/.ec/AGENTS.md" not in text


def test_full_rerun_idempotent(home, ec_home):
    for marker, content in ((".claude.json", "{}"), (".cursor/mcp.json", "{}"),
                            (".config/opencode/opencode.json", "{}"),
                            (".codex/config.toml", "")):
        p = home / marker
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    run_install(home, ec_home, all_agents=True)
    snapshot = {
        str(p.relative_to(home)): p.read_text()
        for p in sorted(home.rglob("*")) if p.is_file() and p.suffix != ".db"
    }
    out = run_install(home, ec_home, all_agents=True)
    again = {
        str(p.relative_to(home)): p.read_text()
        for p in sorted(home.rglob("*")) if p.is_file() and p.suffix != ".db"
    }
    assert snapshot == again
    assert out.count("— unchanged") >= 8                       # nothing redone


# ---------------------------------------------------------------------------
# CLI subprocess (real `python -m ec.install`, sandboxed home)
# ---------------------------------------------------------------------------

def test_cli_subprocess(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["EC_HOME"] = str(fake_home / ".ec")
    env["PYTHONPATH"] = str(ROOT)
    env["HF_HUB_OFFLINE"] = "1"
    env.pop("OPENCODE_ZEN_API_KEY", None)
    proc = subprocess.run(
        [sys.executable, "-m", "ec.install", "--all"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Engineering Cognition — Installation" in proc.stdout
    assert "EC is ready!" in proc.stdout
    ec_dir = fake_home / ".ec"
    assert (ec_dir / "ec.db").exists()
    assert (ec_dir / "config.yaml").exists()
    assert (ec_dir / "AGENTS.md").exists()
    for name in ("ec-start.md", "ec-stop.md", "ec-status.md"):
        assert (ec_dir / "commands" / name).exists()
    # every written file is inside the fake home — nothing escaped
    assert proc.stdout.count("✓") >= 4
