"""Install-fix tests — the installer must write an ABSOLUTE ``reverie-mcp``
path that actually starts a server, verify it at install time, and flag
paths that break later (moved/deleted venv).

Bug: ``server_command()`` returned the bare name ``reverie-mcp`` whenever the
package was pip-installed. Harnesses launch MCP servers outside the user's
virtualenv, so a bare name only works when it happens to be on the global
PATH — with a venv/pipx install the server never starts.

Run: .venv/bin/python -m pytest tests/test_phase11_mcp_command_path.py -v
All offline: fake stdio servers are tiny Python scripts; no real MCP
subprocess is launched except the explicitly sandboxed ones.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reverie import install

ROOT = Path(__file__).resolve().parent.parent

#: A minimal stdio MCP server: answers every request with a reverie
#: initialize-style response. Written with the test interpreter as shebang.
FAKE_SERVER = """\
#!{python}
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    sys.stdout.write(json.dumps({{
        "jsonrpc": "2.0", "id": msg.get("id"),
        "result": {{"serverInfo": {{"name": "reverie", "version": "0"}}}},
    }}) + "\\n")
    sys.stdout.flush()
"""


def write_fake_server(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_SERVER.format(python=sys.executable))
    path.chmod(0o755)
    return path


def run_install_code(home, reverie_home, **kwargs):
    """install.run() sandboxed; returns (exit_code, transcript)."""
    out: list[str] = []
    kwargs.setdefault("which", lambda _binary: None)
    kwargs.setdefault("input_fn", lambda _prompt="": "")
    kwargs.setdefault("environ", {})
    kwargs.setdefault("no_verify", True)
    code = install.run(home=home, reverie_home=reverie_home,
                       print_fn=out.append, **kwargs)
    return code, "\n".join(out)


# ---------------------------------------------------------------------------
# server_command resolution — venv, editable, pipx, which, module fallback
# ---------------------------------------------------------------------------

def test_server_command_venv_install_absolute(monkeypatch, tmp_path):
    monkeypatch.setattr(install, "_is_pip_installed", lambda: True)
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/usr/bin/env python\n")
    script = bin_dir / "reverie-mcp"
    script.write_text("#!/usr/bin/env python\n")
    command, args, env = install.server_command(executable=str(bin_dir / "python"))
    assert command == str(script)
    assert Path(command).is_absolute() and Path(command).is_file()
    assert args == [] and env == {}


def test_server_command_pipx_layout_absolute(monkeypatch, tmp_path):
    """pipx keeps the package in its own venv: ~/.local/share/pipx/venvs/
    <dist>/bin/. The running pipx shim executes that venv's interpreter, so
    the script resolves beside sys.executable."""
    monkeypatch.setattr(install, "_is_pip_installed", lambda: True)
    bin_dir = tmp_path / "pipx" / "venvs" / "reverie" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/usr/bin/env python\n")
    script = bin_dir / "reverie-mcp"
    script.write_text("#!/usr/bin/env python\n")
    command, args, env = install.server_command(executable=str(bin_dir / "python"))
    assert command == str(script)
    assert Path(command).is_absolute() and Path(command).is_file()
    assert "pipx" in command
    assert args == [] and env == {}


def test_server_command_editable_install_absolute():
    """This checkout is the editable install: the command must be the running
    venv's reverie-mcp script, absolute and existing."""
    if not install._is_pip_installed():
        pytest.skip("running outside an installed distribution")
    script = install.console_script_path()
    if script is None:
        pytest.skip("reverie-mcp console script unavailable in this environment")
    command, args, env = install.server_command()
    assert command == script
    assert Path(command).is_absolute() and Path(command).is_file()
    assert Path(command).name.startswith(install.MCP_CONSOLE_SCRIPT)
    assert args == [] and env == {}


def test_written_configs_carry_existing_absolute_script(tmp_path):
    """End-to-end write (editable/venv install): every harness config gets
    the absolute path, and that file exists."""
    if not install._is_pip_installed() or install.console_script_path() is None:
        pytest.skip("running outside an installed distribution")
    home = tmp_path / "home"
    home.mkdir()
    code, out = run_install_code(
        home, home / ".reverie", only=["opencode", "cursor", "codex", "gemini_cli"],
        check_fn=lambda *a, **k: (True, "check skipped (test)"))
    assert code == 0
    script = install.console_script_path()
    opencode = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text())
    assert opencode["mcp"]["reverie"]["command"] == [script]
    cursor = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert cursor["mcpServers"]["reverie"]["command"] == script
    import tomllib
    codex = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert codex["mcp_servers"]["reverie"]["command"] == script
    gemini = json.loads((home / ".gemini" / "settings.json").read_text())
    assert gemini["mcpServers"]["reverie"]["command"] == script
    assert Path(script).is_absolute() and Path(script).is_file()


def test_server_command_which_fallback(monkeypatch, tmp_path):
    """Layouts where the scripts dir is elsewhere (pip install --user):
    shutil.which finds the script and the resolved absolute path is used."""
    monkeypatch.setattr(install, "_is_pip_installed", lambda: True)
    exe_dir = tmp_path / "python" / "bin"
    exe_dir.mkdir(parents=True)
    exe = exe_dir / "python"
    exe.write_text("")
    found = tmp_path / "user-base" / "Scripts" / "reverie-mcp"
    found.parent.mkdir(parents=True)
    found.write_text("")
    command, args, env = install.server_command(
        executable=str(exe), which=lambda name: str(found))
    assert command == str(found)
    assert Path(command).is_absolute()
    assert args == [] and env == {}


def test_server_command_module_fallback(monkeypatch, tmp_path):
    """Script unfindable anywhere: the running interpreter + -m (it can
    import the package) is the last-resort command; the install-time check
    then verifies it for real."""
    monkeypatch.setattr(install, "_is_pip_installed", lambda: True)
    exe_dir = tmp_path / "bin"
    exe_dir.mkdir(parents=True)
    exe = exe_dir / "python"
    exe.write_text("")
    command, args, env = install.server_command(
        executable=str(exe), which=lambda name: None)
    assert command == str(exe)
    assert args == ["-m", "reverie.mcp_server"] and env == {}


# ---------------------------------------------------------------------------
# check_server — launch the exact command, MCP initialize, stop
# ---------------------------------------------------------------------------

def test_check_server_reports_started(tmp_path):
    script = write_fake_server(tmp_path / "bin" / "reverie-mcp")
    ok, detail = install.check_server(str(script), [], {}, timeout=10)
    assert ok is True
    assert "server started (reverie)" in detail


def test_check_server_missing_command_message(tmp_path):
    missing = str(tmp_path / "gone" / "reverie-mcp")
    ok, detail = install.check_server(missing, [], {}, timeout=10)
    assert ok is False
    assert "could not launch" in detail
    assert "No such file" in detail or "not found" in detail


def test_check_server_exit_without_response(tmp_path):
    script = tmp_path / "crasher.py"
    script.write_text("import sys; sys.stderr.write('boom: bad import\\n'); "
                      "sys.exit(3)\n")
    ok, detail = install.check_server(sys.executable, [str(script)], {},
                                      timeout=10)
    assert ok is False
    assert "exited without responding" in detail
    assert "boom: bad import" in detail


def test_check_server_timeout(tmp_path):
    script = tmp_path / "sleepy.py"
    script.write_text("import time; time.sleep(60)\n")
    started = time.monotonic()
    ok, detail = install.check_server(sys.executable, [str(script)], {},
                                      timeout=0.5)
    assert ok is False
    assert "no MCP response" in detail
    assert time.monotonic() - started < 15


# ---------------------------------------------------------------------------
# install-time verification — ✓/✗ per harness, ready only when all start
# ---------------------------------------------------------------------------

def test_install_prints_check_per_harness(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    code, out = run_install_code(
        home, home / ".reverie", all_agents=False, only=["claude_code", "cursor"],
        check_fn=lambda *a: (True, "server started (fake)"))
    assert code == 0
    assert "Verifying the MCP server starts..." in out
    assert "✓ Claude Code: server started (fake)" in out
    assert "✓ Cursor: server started (fake)" in out
    assert "Reverie is ready!" in out


def test_install_checks_each_distinct_command_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    calls: list[str] = []

    def check(command, args, env):
        calls.append(command)
        return True, "server started (fake)"

    code, _ = run_install_code(
        home, home / ".reverie", all_agents=False,
        only=["claude_code", "cursor", "opencode", "codex", "gemini_cli"],
        check_fn=check)
    assert code == 0
    # all five harnesses share one command: launched once, reported per harness
    assert len(calls) == 1


def test_failed_check_blocks_ready_and_prints_command(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seen: list[tuple] = []

    def bad_check(command, args, env):
        seen.append((command, list(args), dict(env)))
        return False, "could not launch: [Errno 2] No such file or directory"

    code, out = run_install_code(
        home, home / ".reverie", only=["cursor"], check_fn=bad_check)
    assert code == 1
    assert "✗ Cursor: could not launch" in out
    assert "No such file or directory" in out
    assert "command: " in out                       # the exact command shown
    assert seen[0][0] in out
    assert "Setup incomplete" in out
    assert "Reverie is ready!" not in out
    # the harness entry was still written — the fix is in the report
    assert (home / ".cursor" / "mcp.json").exists()


def test_install_real_check_reports_missing_command(monkeypatch, tmp_path):
    """The real check runs when no check_fn is injected: a config pointing at
    a non-existent script is ✗ with the command and the error, and the run
    must not claim success."""
    home = tmp_path / "home"
    home.mkdir()
    missing = str(tmp_path / "deleted-venv" / "bin" / "reverie-mcp")
    monkeypatch.setattr(install, "_is_pip_installed", lambda: True)
    monkeypatch.setattr(install, "console_script_path", lambda *a, **k: missing)
    code, out = run_install_code(home, home / ".reverie", only=["cursor"])
    assert code == 1
    assert "✗ Cursor:" in out
    assert "could not launch" in out
    assert missing in out
    assert "Setup incomplete" in out
    assert "Reverie is ready!" not in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang script")
def test_install_real_check_reports_started(monkeypatch, tmp_path):
    """The real check with a startable server: ✓ per harness, ready printed,
    and the config contains the absolute path of the fake pipx script."""
    pipx_bin = tmp_path / "pipx" / "venvs" / "reverie" / "bin"
    pipx_bin.mkdir(parents=True)
    (pipx_bin / "python").write_text("#!/usr/bin/env python\n")
    script = write_fake_server(pipx_bin / "reverie-mcp")
    monkeypatch.setattr(install, "_is_pip_installed", lambda: True)
    monkeypatch.setattr(install, "console_script_path",
                        lambda *a, **k: str(script))
    home = tmp_path / "home"
    home.mkdir()
    code, out = run_install_code(home, home / ".reverie",
                                 only=["cursor", "opencode"])
    assert code == 0
    assert "✓ Cursor: server started (reverie)" in out
    assert "✓ OpenCode: server started (reverie)" in out
    assert "Reverie is ready!" in out
    cursor = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert cursor["mcpServers"]["reverie"]["command"] == str(script)
    opencode = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text())
    assert opencode["mcp"]["reverie"]["command"] == [str(script)]


def test_dry_run_never_launches_the_check(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    calls: list = []
    code, out = run_install_code(
        home, home / ".reverie", only=["cursor"], dry_run=True,
        check_fn=lambda *a: calls.append(a) or (True, "unused"))
    assert code == 0
    assert calls == []
    assert "[dry-run] would start each harness's written server command" in out
    assert not (home / ".cursor" / "mcp.json").exists()


# ---------------------------------------------------------------------------
# configured_servers / reverie status — stale path detection
# ---------------------------------------------------------------------------

def _seed_claude(home: Path, command: str) -> None:
    home.mkdir(exist_ok=True)
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"reverie": {"type": "stdio", "command": command}},
    }))


def test_configured_servers_reads_every_harness(tmp_path):
    home = tmp_path / "home"
    _seed_claude(home, "/a/reverie-mcp")
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"reverie": {"command": "/b/reverie-mcp"}}}))
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text(json.dumps(
        {"mcp": {"reverie": {"type": "local",
                             "command": ["/c/reverie-mcp", "--flag"],
                             "enabled": True}}}))
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.reverie]\ncommand = "/d/reverie-mcp"\n'
        'args = ["-x"]\n')
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(json.dumps(
        {"mcpServers": {"reverie": {"command": "/e/reverie-mcp"}}}))
    servers = {s.agent: s for s in install.configured_servers(home)}
    assert set(servers) == {"claude_code", "cursor", "opencode", "codex",
                            "gemini_cli"}
    assert servers["claude_code"].command == "/a/reverie-mcp"
    assert servers["cursor"].command == "/b/reverie-mcp"
    assert servers["opencode"].command == "/c/reverie-mcp"
    assert servers["opencode"].args == ["--flag"]
    assert servers["codex"].command == "/d/reverie-mcp"
    assert servers["codex"].args == ["-x"]
    assert servers["gemini_cli"].command == "/e/reverie-mcp"


def test_status_flags_missing_server_command(tmp_path):
    home = tmp_path / "home"
    missing = str(tmp_path / "deleted-venv" / "bin" / "reverie-mcp")
    _seed_claude(home, missing)
    out: list[str] = []
    ok = install.report_configured_servers(print_fn=out.append, home=home)
    text = "\n".join(out)
    assert ok is False
    assert "✗ Claude Code:" in text
    assert missing in text
    assert "Run `reverie install` again" in text


def test_status_reports_found_command(tmp_path):
    home = tmp_path / "home"
    script = write_fake_server(tmp_path / "venv" / "bin" / "reverie-mcp")
    _seed_claude(home, str(script))
    out: list[str] = []
    ok = install.report_configured_servers(print_fn=out.append, home=home)
    assert ok is True
    assert f"✓ Claude Code: {script}" in "\n".join(out)


def test_status_bare_command_uses_which(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(json.dumps(
        {"mcpServers": {"reverie": {"command": "reverie-mcp"}}}))
    out: list[str] = []
    ok = install.report_configured_servers(
        print_fn=out.append, home=home,
        which=lambda name: "/usr/bin/reverie-mcp" if name == "reverie-mcp"
        else None)
    assert ok is True


def test_status_quiet_when_nothing_configured(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    out: list[str] = []
    assert install.report_configured_servers(print_fn=out.append, home=home)
    assert out == []


def test_status_subprocess_flags_stale_path(tmp_path):
    """`reverie status` end-to-end: a config whose absolute command vanished
    (venv moved/deleted) must say to run `reverie install` again."""
    fake_home = tmp_path / "home"
    _seed_claude(fake_home, str(fake_home / "gone" / "reverie-mcp"))
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["REVERIE_HOME"] = str(tmp_path / "reverie_home")
    env["PYTHONPATH"] = str(ROOT)
    env["HF_HUB_OFFLINE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "reverie.session", "status"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "MCP server command:" in proc.stdout
    assert "✗ Claude Code:" in proc.stdout
    assert "Run `reverie install` again" in proc.stdout
