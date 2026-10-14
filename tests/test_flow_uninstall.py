"""feat/flow tests — `ec uninstall` (review fix 10): undoes exactly what
`ec install` did, nothing more.

Run: .venv/bin/python -m pytest tests/test_flow_uninstall.py -v
All offline, everything under tmp_path (sandboxed home + EC_HOME).
"""

import json
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import zipfile
from pathlib import Path

import pytest

from ec import install, uninstall
from ec.brain import Brain
from ec.transport import TransportError

T0 = "2026-10-14T12:00:00+00:00"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("EC_HOME", str(h / ".ec"))
    return h


@pytest.fixture()
def ec_home(home):
    return home / ".ec"


def json_text(data: dict) -> str:
    """The exact serialization install/uninstall use (order preserved)."""
    return json.dumps(data, indent=2) + "\n"


def seed_files(home: Path) -> None:
    """Pre-existing harness files: OTHER MCP servers and instructions the
    user had before Reverie arrived. Every file ends with a newline (the
    normal text-file case) so install's idempotent append is reversible
    byte-for-byte."""
    (home / ".claude.json").write_text(json_text({
        "theme": "dark",
        "mcpServers": {"other-mcp": {"command": "other-server"}},
    }))
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("# My notes\n\nKeep this.\n")

    cursor_dir = home / ".cursor"
    cursor_dir.mkdir()
    (cursor_dir / "mcp.json").write_text(json_text({
        "mcpServers": {"other-cursor": {"command": "cursor-server"}},
    }))

    oc_dir = home / ".config" / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text(json_text({
        "$schema": "https://opencode.ai/config.json",
        "theme": "solarized",
        "mcp": {"other-oc": {"type": "local", "command": ["oc-server"]}},
    }))
    (oc_dir / "AGENTS.md").write_text("# OpenCode instructions\n")

    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        '# my codex config\nmodel = "gpt-5"\n\n'
        '[mcp_servers.other]\ncommand = "codex-server"\n')
    (codex_dir / "AGENTS.md").write_text("# Codex instructions\n")


def snapshot(home: Path) -> dict[str, bytes]:
    """Byte snapshot of every file under home (db/config excluded)."""
    return {
        str(p.relative_to(home)): p.read_bytes()
        for p in sorted(home.rglob("*"))
        if p.is_file() and p.suffix not in (".db", ".yaml")
    }


def do_install(home, ec_home, **kwargs):
    return install.run(
        home=home, ec_home=ec_home, all_agents=True, no_verify=True,
        which=lambda _b: None, input_fn=lambda _p="": "",
        print_fn=lambda s: None, environ={}, **kwargs)


def do_uninstall(home, ec_home, **kwargs):
    """Run the uninstaller sandboxed; returns (exit_code, transcript).
    The default confirmation answer is 'delete' (pass input_fn to
    override)."""
    out: list[str] = []
    kwargs.setdefault("input_fn", lambda _p="": "delete")
    code = uninstall.run(home=home, ec_home=ec_home, print_fn=out.append,
                         **kwargs)
    return code, "\n".join(out)


# ---------------------------------------------------------------------------
# install → uninstall is byte-identical for files install only merged into
# ---------------------------------------------------------------------------

def test_uninstall_restores_harness_files_exactly(home, ec_home):
    seed_files(home)
    before = snapshot(home)
    assert do_install(home, ec_home) == 0
    assert snapshot(home) != before             # install actually changed
    # things: merged entries + new files (ec.md, ~/.ec/**)

    assert do_uninstall(home, ec_home)[0] == 0
    after = snapshot(home)
    # every harness file is byte-identical to before install
    for rel, content in before.items():
        assert rel in after, f"{rel} was removed by uninstall"
        assert after[rel] == content, f"{rel} differs after uninstall"
    # and nothing new appeared outside ~/.ec
    leftovers = {rel for rel in after
                 if rel not in before and not rel.startswith(".ec/")}
    assert leftovers == set(), leftovers


def test_uninstall_removes_entries_it_created(home, ec_home):
    seed_files(home)                            # markers → agents detected
    do_install(home, ec_home)
    claude = json.loads((home / ".claude.json").read_text())
    assert "ec" in claude["mcpServers"]         # sanity: install merged it
    _, transcript = do_uninstall(home, ec_home)
    assert "Removed the ec MCP server" in transcript
    assert (home / ".cursor" / "rules" / "ec.md").exists() is False
    # command files live under ~/.ec (kept by default)
    assert (ec_home / "commands" / "ec-start.md").exists()


# ---------------------------------------------------------------------------
# invalid / edited files are skipped, not damaged
# ---------------------------------------------------------------------------

def test_uninstall_skips_invalid_and_edited_files(home, ec_home):
    seed_files(home)
    do_install(home, ec_home)
    (home / ".claude.json").write_text("{not valid json")
    (home / ".codex" / "config.toml").write_text("[mcp_servers.ec\nbroken")
    edited = home / ".config" / "opencode" / "AGENTS.md"
    text = edited.read_text()
    edited.write_text(text.replace(
        "engineering memory via ec_observe", "MY OWN EDIT LIVES HERE"))
    before = {
        p: p.read_bytes()
        for p in (home / ".claude.json", home / ".codex" / "config.toml",
                  edited)
    }
    _, transcript = do_uninstall(home, ec_home)
    assert "not valid JSON" in transcript
    assert "not valid TOML" in transcript
    assert "edited after install" in transcript
    for path, content in before.items():
        assert path.read_bytes() == content     # untouched, not damaged
    # the untouched files were still cleaned
    claude_md = home / ".claude" / "CLAUDE.md"
    assert "@~/.ec/AGENTS.md" not in claude_md.read_text()


# ---------------------------------------------------------------------------
# --delete-data: export first, then confirm, then remove
# ---------------------------------------------------------------------------

def seed_brain(ec_home: Path) -> None:
    brain = Brain(ec_home / "ec.db")
    brain.insert_ecu({
        "cognition": "a kept conclusion",
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:a"},
        "provenance": {"source_type": "debugging"},
        "confidence": 0.7,
    })
    brain.close()


def test_delete_data_exports_then_deletes(home, ec_home, tmp_path):
    ec_home.mkdir(parents=True, exist_ok=True)
    seed_brain(ec_home)
    code, transcript = do_uninstall(home, ec_home, delete_data=True,
                                    yes=True, export_dir=tmp_path)
    assert code == 0
    exports = list(tmp_path.glob("reverie-final-export-*.zip"))
    assert len(exports) == 1
    assert str(exports[0]) in transcript
    with zipfile.ZipFile(exports[0]) as zf:
        assert set(zf.namelist()) == {"brain.db", "manifest.json"}
    assert not ec_home.exists()                 # ~/.ec removed
    assert "Removed ~/.ec" in transcript


def test_delete_data_without_export_flag_skips_export(home, ec_home,
                                                      tmp_path):
    ec_home.mkdir(parents=True, exist_ok=True)
    seed_brain(ec_home)
    code, _ = do_uninstall(home, ec_home, delete_data=True,
                           no_export=True, yes=True, export_dir=tmp_path)
    assert code == 0
    assert not list(tmp_path.glob("reverie-final-export-*.zip"))
    assert not ec_home.exists()


def test_failed_export_removes_nothing(home, ec_home, tmp_path, monkeypatch):
    seed_files(home)
    ec_home.mkdir(parents=True, exist_ok=True)
    seed_brain(ec_home)
    assert do_install(home, ec_home) == 0
    monkeypatch.setattr("ec.transport.export_brain",
                        lambda *a, **k: (_ for _ in ()).throw(
                            TransportError("disk full")))
    code, transcript = do_uninstall(home, ec_home, delete_data=True,
                                    yes=True, export_dir=tmp_path)
    assert code == 1
    assert "Final export FAILED" in transcript
    assert ec_home.exists()                     # ~/.ec untouched
    claude = json.loads((home / ".claude.json").read_text())
    assert "ec" in claude["mcpServers"]         # harness entries untouched


def test_delete_data_requires_typing_delete(home, ec_home, tmp_path,
                                            monkeypatch):
    seed_files(home)
    ec_home.mkdir(parents=True, exist_ok=True)
    seed_brain(ec_home)
    assert do_install(home, ec_home) == 0
    code, transcript = do_uninstall(home, ec_home, delete_data=True,
                                    export_dir=tmp_path,
                                    input_fn=lambda _p="": "no")
    assert code == 1
    assert "your memory was NOT removed" in transcript
    assert ec_home.exists()                     # aborted — memory kept


# ---------------------------------------------------------------------------
# --dry-run and idempotence
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(home, ec_home):
    seed_files(home)
    before = snapshot(home)
    do_install(home, ec_home)
    code, transcript = do_uninstall(home, ec_home, dry_run=True,
                                    delete_data=True, yes=True)
    assert code == 0
    assert "[dry-run]" in transcript
    after = snapshot(home)
    # nothing changed by the dry run: every file install created/edited is
    # still exactly as install left it (and ~/.ec was not removed)
    assert after != before
    assert ec_home.exists()
    assert "would ask for confirmation" in transcript


def test_uninstall_twice_is_harmless(home, ec_home):
    seed_files(home)
    do_install(home, ec_home)
    assert do_uninstall(home, ec_home)[0] == 0
    code, second = do_uninstall(home, ec_home)
    assert code == 0
    assert "not present" in second
    assert "✓" not in second                    # nothing left to remove
