"""`reverie install --refresh-instructions` and the status warning: installed instruction text
that went out of date is refreshed, and nothing else is touched or created."""

import hashlib
from pathlib import Path

import pytest

from reverie import cli, install
from reverie.integrations import common as integration_common

NEW_POLICY = "## When to call each command\n\n- A brand-new trigger.\n"


def _install(home: Path, key: str) -> None:
    code = install.run(home=home, reverie_home=home / ".reverie", only=[key], no_verify=True,
                       environ={}, which=lambda _b: None, input_fn=lambda _p="": "",
                       print_fn=lambda _s: None)
    assert code == 0


def _snapshot(home: Path) -> dict[str, str]:
    return {str(p): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(home.rglob("*")) if p.is_file() and p.name != "reverie.db"}


@pytest.fixture
def stale(tmp_path, monkeypatch):
    """codex installed, then the usage policy changes (a newer Reverie)."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "AGENTS.md").write_text("# My notes\n\nKeep PRs small.\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _install(home, "codex")
    monkeypatch.setattr(integration_common, "USAGE_POLICY", NEW_POLICY)
    monkeypatch.setattr(install, "AGENTS_MD", "# Reverie — Agent Instructions\n\n" + NEW_POLICY)
    return home


def test_up_to_date_install_reports_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _install(home, "codex")
    out: list[str] = []
    assert install.report_instruction_staleness(out.append, home, home / ".reverie") is None
    assert out == []
    msgs: list[str] = []
    assert install.refresh_instructions(home, home / ".reverie", msgs.append) == 0
    assert msgs == ["Reverie's installed instructions are up to date."]


def test_status_warns_about_a_stale_block_and_a_stale_owned_file(stale):
    home = stale
    out: list[str] = []
    assert install.report_instruction_staleness(out.append, home, home / ".reverie") is False
    joined = "\n".join(out)
    assert ".codex/AGENTS.md" in joined and ".reverie/AGENTS.md" in joined
    assert "--refresh-instructions" in joined


def test_refresh_rewrites_only_the_stale_text(stale):
    home = stale
    codex_md = home / ".codex" / "AGENTS.md"
    before = codex_md.read_text()
    start = before.index(integration_common.POLICY_BLOCK_START)
    end = before.index(integration_common.POLICY_BLOCK_END) + len(integration_common.POLICY_BLOCK_END) + 1
    outside = before[:start] + before[end:]
    others = {k: v for k, v in _snapshot(home).items()
              if not k.endswith(("/.codex/AGENTS.md", "/.reverie/AGENTS.md"))}
    assert install.refresh_instructions(home, home / ".reverie", lambda _s: None) == 0
    after = codex_md.read_text()
    assert NEW_POLICY in after and after.count(integration_common.POLICY_BLOCK_START) == 1
    s2 = after.index(integration_common.POLICY_BLOCK_START)
    e2 = after.index(integration_common.POLICY_BLOCK_END) + len(integration_common.POLICY_BLOCK_END) + 1
    assert after[:s2] + after[e2:] == outside                      # the user's own text survives
    assert "Keep PRs small." in after
    assert NEW_POLICY in (home / ".reverie" / "AGENTS.md").read_text()
    now = {k: v for k, v in _snapshot(home).items()
           if not k.endswith(("/.codex/AGENTS.md", "/.reverie/AGENTS.md"))}
    assert now == others                                            # MCP config, hooks, config.yaml: untouched
    assert install.report_instruction_staleness(lambda _s: None, home, home / ".reverie") is None


def test_refresh_never_creates_a_block_that_was_not_installed(stale):
    home = stale
    assert not (home / ".config" / "opencode" / "AGENTS.md").exists()
    install.refresh_instructions(home, home / ".reverie", lambda _s: None)
    assert not (home / ".config" / "opencode").exists()
    assert not (home / ".claude").exists()


def test_dry_run_changes_nothing(stale):
    home = stale
    before = _snapshot(home)
    out: list[str] = []
    install.refresh_instructions(home, home / ".reverie", out.append, dry_run=True)
    assert _snapshot(home) == before and any("[dry-run]" in ln for ln in out)


def test_cli_flag_runs_the_refresh(stale, monkeypatch, capsys):
    home = stale
    monkeypatch.setattr(install, "_default_reverie_home", lambda: home / ".reverie")
    assert cli.main(["install", "--refresh-instructions"]) == 0
    assert NEW_POLICY in (home / ".codex" / "AGENTS.md").read_text()
