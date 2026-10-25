"""feat/flow tests — `reverie uninstall` (review fix 10): undoes exactly what
`reverie install` did, nothing more.

Run: .venv/bin/python -m pytest tests/test_flow_uninstall.py -v
All offline, everything under tmp_path (sandboxed home + REVERIE_HOME).
"""

import json
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import zipfile
from pathlib import Path

import pytest

from reverie import install, uninstall
from reverie.brain import Brain
from reverie.transport import TransportError

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


def do_install(home, reverie_home, which=None, **kwargs):
    kwargs.setdefault("check_fn", lambda *a, **k: (True, "check skipped (test)"))
    return install.run(
        home=home, reverie_home=reverie_home, all_agents=True, no_verify=True,
        which=which or (lambda _b: None), input_fn=lambda _p="": "",
        print_fn=lambda s: None, environ={}, **kwargs)


def do_uninstall(home, reverie_home, **kwargs):
    """Run the uninstaller sandboxed; returns (exit_code, transcript).
    The default confirmation answer is 'y' (pass input_fn to override)."""
    out: list[str] = []
    kwargs.setdefault("input_fn", lambda _p="": "y")
    code = uninstall.run(home=home, reverie_home=reverie_home, print_fn=out.append,
                         **kwargs)
    return code, "\n".join(out)


def opencode_which(binary):
    """Detects only OpenCode (so a fresh home has exactly one agent)."""
    return "/usr/bin/opencode" if binary == "opencode" else None


# ---------------------------------------------------------------------------
# install → uninstall is byte-identical for files install only merged into
# ---------------------------------------------------------------------------

def test_uninstall_restores_harness_files_exactly(home, reverie_home):
    seed_files(home)
    before = snapshot(home)
    assert do_install(home, reverie_home) == 0
    assert snapshot(home) != before             # install actually changed
    # things: merged entries + new files (ec.md, ~/.reverie/**)

    assert do_uninstall(home, reverie_home)[0] == 0
    after = snapshot(home)
    # every harness file is byte-identical to before install
    for rel, content in before.items():
        assert rel in after, f"{rel} was removed by uninstall"
        assert after[rel] == content, f"{rel} differs after uninstall"
    # and nothing new appeared outside ~/.reverie
    leftovers = {rel for rel in after
                 if rel not in before and not rel.startswith(".reverie/")}
    assert leftovers == set(), leftovers


def test_uninstall_removes_entries_it_created(home, reverie_home):
    seed_files(home)                            # markers → agents detected
    do_install(home, reverie_home)
    claude = json.loads((home / ".claude.json").read_text())
    assert "reverie" in claude["mcpServers"]    # sanity: install merged it
    _, transcript = do_uninstall(home, reverie_home)
    assert "Removed the reverie MCP server" in transcript
    # the user-level command files are gone
    assert not (home / ".claude" / "commands" / "reverie-review.md").exists()
    assert not (home / ".cursor" / "commands" / "reverie-status.md").exists()
    assert not (home / ".config" / "opencode" / "commands"
                / "reverie-review.md").exists()
    assert not (home / ".codex" / "prompts" / "reverie-review.md").exists()
    assert not (home / ".agents" / "skills" / "reverie-review").exists()
    assert not (home / ".agents" / "skills" / "reverie-status").exists()


def test_uninstall_removes_absolute_path_entries(home, reverie_home):
    """Install writes the ABSOLUTE reverie-mcp path into every harness
    config; uninstall matches by entry name, never by command value. A
    hand-written file that carried ONLY the entry survives (review fix
    39): install did not create it, so it is never deleted."""
    absolute = str(home / "venv" / "bin" / "reverie-mcp")
    (home / ".claude.json").write_text(json_text({
        "mcpServers": {
            "other": {"command": "other-server"},
            "reverie": {"type": "stdio", "command": absolute},
        },
    }))
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(json_text({
        "mcpServers": {"reverie": {"command": absolute}},
    }))
    code, transcript = do_uninstall(home, reverie_home)
    assert code == 0
    assert "Removed the reverie MCP server" in transcript
    claude = json.loads((home / ".claude.json").read_text())
    assert "reverie" not in claude["mcpServers"]
    assert claude["mcpServers"]["other"] == {"command": "other-server"}
    # cursor held only the (hand-written) reverie entry -> kept, reset to
    # an empty object instead of deleting a file the installer never made
    cursor = home / ".cursor" / "mcp.json"
    assert cursor.exists()
    assert json.loads(cursor.read_text()) == {}
    assert "existed before install" in transcript


def test_uninstall_removes_files_the_installer_created(home, reverie_home):
    """Review fix 39: a file install itself created (recorded in the
    install manifest with a matching hash) is removed whole."""
    which = opencode_which
    assert do_install(home, reverie_home, which=which) == 0
    oc = home / ".config" / "opencode" / "opencode.json"
    assert "reverie" in json.loads(oc.read_text())["mcp"]
    manifest = json.loads(
        (reverie_home / "install_manifest.json").read_text())
    assert str(oc.resolve()) in manifest["files"]        # recorded as ours

    code, transcript = do_uninstall(home, reverie_home)
    assert code == 0
    assert not oc.exists()
    assert "Reverie created it" in transcript


def test_schema_only_handwritten_file_survives(home, reverie_home):
    """Review fix 39's exact acceptance case: a hand-written file holding
    only ``$schema`` plus reverie's entry must survive with a clean
    ``$schema`` object — never be deleted."""
    schema = {"$schema": "https://opencode.ai/config.json"}
    oc = home / ".config" / "opencode" / "opencode.json"
    oc.parent.mkdir(parents=True)
    oc.write_text(json_text(schema))
    which = opencode_which
    assert do_install(home, reverie_home, which=which) == 0
    assert "reverie" in json.loads(oc.read_text())["mcp"]
    manifest = json.loads(
        (reverie_home / "install_manifest.json").read_text())
    assert str(oc.resolve()) not in manifest["files"]    # it was here first

    code, transcript = do_uninstall(home, reverie_home)
    assert code == 0
    assert oc.exists()
    assert json.loads(oc.read_text()) == schema
    assert "existed before install" in transcript


def test_edited_installer_created_file_survives(home, reverie_home):
    """The manifest is not a licence to delete user edits: a file install
    created but the user then changed keeps everything except the reverie
    entry."""
    which = opencode_which
    assert do_install(home, reverie_home, which=which) == 0
    oc = home / ".config" / "opencode" / "opencode.json"
    data = json.loads(oc.read_text())
    data["theme"] = "dark"
    oc.write_text(json_text(data))

    code, _ = do_uninstall(home, reverie_home)
    assert code == 0
    assert oc.exists()
    kept = json.loads(oc.read_text())
    assert "reverie" not in kept.get("mcp", {})
    assert kept["theme"] == "dark"


# ---------------------------------------------------------------------------
# invalid / edited files are skipped, not damaged
# ---------------------------------------------------------------------------

def test_uninstall_skips_invalid_and_edited_files(home, reverie_home):
    seed_files(home)
    do_install(home, reverie_home)
    (home / ".claude.json").write_text("{not valid json")
    (home / ".codex" / "config.toml").write_text("[mcp_servers.reverie\nbroken")
    edited = home / ".config" / "opencode" / "AGENTS.md"
    text = edited.read_text()
    edited.write_text(text.replace(
        "Relay only decisions the user states", "MY OWN EDIT LIVES HERE"))
    before = {
        p: p.read_bytes()
        for p in (home / ".claude.json", home / ".codex" / "config.toml",
                  edited)
    }
    _, transcript = do_uninstall(home, reverie_home)
    assert "not valid JSON" in transcript
    assert "not valid TOML" in transcript
    assert "edited after install" in transcript
    for path, content in before.items():
        assert path.read_bytes() == content     # untouched, not damaged
    # the untouched files were still cleaned
    claude_md = home / ".claude" / "CLAUDE.md"
    assert "@~/.reverie/AGENTS.md" not in claude_md.read_text()


# ---------------------------------------------------------------------------
# --delete-data: export first, then confirm, then remove
# ---------------------------------------------------------------------------

def seed_brain(reverie_home: Path) -> None:
    brain = Brain(reverie_home / "reverie.db")
    brain.insert_ecu({
        "cognition": "a kept conclusion",
        "conclusion_type": "invariant",
        "scope": {"level": "repo", "path": "repo:a"},
        "provenance": {"source_type": "debugging"},
        "confidence": 0.7,
    })
    brain.close()


def test_delete_data_exports_then_deletes(home, reverie_home, tmp_path):
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed_brain(reverie_home)
    code, transcript = do_uninstall(home, reverie_home, delete_data=True,
                                    yes=True, export_dir=tmp_path)
    assert code == 0
    exports = list(tmp_path.glob("reverie-final-export-*.zip"))
    assert len(exports) == 1
    assert str(exports[0]) in transcript
    with zipfile.ZipFile(exports[0]) as zf:
        assert set(zf.namelist()) == {"brain.db", "manifest.json"}
    assert not reverie_home.exists()                 # ~/.reverie removed
    assert "Removed ~/.reverie" in transcript


def test_delete_data_without_export_flag_skips_export(home, reverie_home,
                                                      tmp_path):
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed_brain(reverie_home)
    code, _ = do_uninstall(home, reverie_home, delete_data=True,
                           no_export=True, yes=True, export_dir=tmp_path)
    assert code == 0
    assert not list(tmp_path.glob("reverie-final-export-*.zip"))
    assert not reverie_home.exists()


def test_failed_export_removes_nothing(home, reverie_home, tmp_path, monkeypatch):
    seed_files(home)
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed_brain(reverie_home)
    assert do_install(home, reverie_home) == 0
    monkeypatch.setattr("reverie.transport.export_brain",
                        lambda *a, **k: (_ for _ in ()).throw(
                            TransportError("disk full")))
    code, transcript = do_uninstall(home, reverie_home, delete_data=True,
                                    yes=True, export_dir=tmp_path)
    assert code == 1
    assert "Final export FAILED" in transcript
    assert reverie_home.exists()                     # ~/.reverie untouched
    claude = json.loads((home / ".claude.json").read_text())
    assert "reverie" in claude["mcpServers"]    # harness entries untouched


def test_abort_leaves_everything_unchanged(home, reverie_home, tmp_path):
    """Answering 'no' to the single confirmation aborts BEFORE anything is
    touched: no export, no harness edits, memory intact."""
    seed_files(home)
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed_brain(reverie_home)
    assert do_install(home, reverie_home) == 0
    before = snapshot(home)
    code, transcript = do_uninstall(home, reverie_home, delete_data=True,
                                    export_dir=tmp_path,
                                    input_fn=lambda _p="": "no")
    assert code == 1
    assert "Aborted" in transcript
    assert not list(tmp_path.glob("reverie-final-export-*.zip"))
    for rel, content in before.items():          # byte-identical everything
        assert (home / rel).read_bytes() == content, rel


def test_confirmation_covers_whole_plan(home, reverie_home, tmp_path):
    """The plan (harness entries + data) is printed BEFORE the single
    confirmation, and one 'y' applies all of it."""
    seed_files(home)
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed_brain(reverie_home)
    assert do_install(home, reverie_home) == 0
    out: list[str] = []
    plan_at: list[int] = []

    def scripted(prompt=""):
        plan_at.append(len(out))                 # lines printed before ask
        return "y"

    code = uninstall.run(home=home, reverie_home=reverie_home,
                         delete_data=True, export_dir=tmp_path,
                         print_fn=out.append, input_fn=scripted)
    assert code == 0
    assert len(plan_at) == 1                     # exactly one question
    # the plan preceded the prompt and named the harnesses + the data step
    plan = "\n".join(out[:plan_at[0]])
    assert "The plan:" in plan
    assert "Claude Code" in plan and "Codex" in plan
    assert "final export" in plan
    assert reverie_home.exists() is False        # applied in full
    assert len(list(tmp_path.glob("reverie-final-export-*.zip"))) == 1


def test_yes_skips_confirmation_but_not_the_export(home, reverie_home,
                                                   tmp_path):
    seed_files(home)
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed_brain(reverie_home)
    assert do_install(home, reverie_home) == 0
    code, transcript = do_uninstall(home, reverie_home, delete_data=True,
                                    yes=True, export_dir=tmp_path,
                                    input_fn=lambda _p="": (_ for _ in ()).throw(
                                        AssertionError("no prompt expected")))
    assert code == 0
    assert not reverie_home.exists()
    assert len(list(tmp_path.glob("reverie-final-export-*.zip"))) == 1


def test_dry_run_shows_plan_and_changes_nothing(home, reverie_home, tmp_path):
    seed_files(home)
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed_brain(reverie_home)
    assert do_install(home, reverie_home) == 0
    before = snapshot(home)
    code, transcript = do_uninstall(home, reverie_home, delete_data=True,
                                    yes=True, dry_run=True,
                                    export_dir=tmp_path)
    assert code == 0
    assert "dry run" in transcript.lower()
    assert not list(tmp_path.glob("reverie-final-export-*.zip"))
    for rel, content in before.items():
        assert (home / rel).read_bytes() == content, rel

def test_dry_run_writes_nothing(home, reverie_home):
    seed_files(home)
    before = snapshot(home)
    do_install(home, reverie_home)
    code, transcript = do_uninstall(home, reverie_home, dry_run=True,
                                    delete_data=True, yes=True)
    assert code == 0
    assert "[dry-run]" in transcript
    after = snapshot(home)
    # nothing changed by the dry run: every file install created/edited is
    # still exactly as install left it (and ~/.reverie was not removed)
    assert after != before
    assert reverie_home.exists()
    assert "REMOVED after a final export" in transcript


def test_dry_run_speaks_future_and_prints_the_plan_once(home, reverie_home):
    """Items 41/42: the dry run says "would remove …" (no past-tense
    "Removed …" lines as if it had run) and the plan appears exactly once
    (no summary block + walk-through duplication)."""
    seed_files(home)
    reverie_home.mkdir(parents=True, exist_ok=True)
    seed_brain(reverie_home)
    assert do_install(home, reverie_home) == 0
    code, transcript = do_uninstall(home, reverie_home, dry_run=True,
                                    yes=True, delete_data=True)
    assert code == 0
    assert "Would remove:" in transcript
    assert "would remove the reverie MCP server" in transcript
    assert "Removed the reverie MCP server" not in transcript
    assert "The plan:" not in transcript            # one pass, not two
    assert "Removing Reverie from your coding agents" not in transcript
    # every JSON harness entry is described once — nothing is repeated
    assert (transcript.count("would remove the reverie MCP server")
            == transcript.count("the reverie MCP server from") > 0)
    # the report says what a dry run means, consistently
    assert "Dry run — nothing was removed." in transcript


def test_uninstall_twice_is_harmless(home, reverie_home):
    seed_files(home)
    do_install(home, reverie_home)
    assert do_uninstall(home, reverie_home)[0] == 0
    code, second = do_uninstall(home, reverie_home)
    assert code == 0
    assert "not present" in second
    assert "✓" not in second                    # nothing left to remove
