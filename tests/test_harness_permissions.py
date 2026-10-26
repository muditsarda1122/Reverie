"""Prompt-free MCP permissions for the Claude Code and Cursor CLI harnesses.

Both permissions are merged into the user's existing ``permissions.allow``
list, never duplicated, and removed on uninstall only when install recorded
that Reverie added them.
"""

from __future__ import annotations

import json
from pathlib import Path

from reverie import install
from reverie.integrations.claude import (
    _CLAUDE_PERMISSION,
    _session_start_handler,
    _settings_plan,
    _stop_handler,
    _uninstall_settings,
)
from reverie.integrations.cursor import (
    _CURSOR_PERMISSION,
    _cli_config_path,
    _cli_config_plan,
    _cli_config_removal,
)


def _settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_claude_permission_merges_without_duplicating(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write_json(_settings(home), {
        "theme": "dark",
        "permissions": {"allow": ["Bash(npm test)"]},
    })
    first = _settings_plan(home, _session_start_handler(), _stop_handler())
    assert first.owned_entries == (_CLAUDE_PERMISSION,)
    install._apply(first)
    data = json.loads(_settings(home).read_text())
    assert data["permissions"]["allow"] == [
        "Bash(npm test)", _CLAUDE_PERMISSION]
    assert data["theme"] == "dark"
    assert len(data["hooks"]["SessionStart"]) == 1

    second = _settings_plan(home, _session_start_handler(), _stop_handler())
    assert second.action == "unchanged"
    assert second.owned_entries == ()
    assert json.loads(_settings(home).read_text())["permissions"]["allow"] == [
        "Bash(npm test)", _CLAUDE_PERMISSION]


def test_claude_uninstall_removes_only_the_owned_permission(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write_json(_settings(home), {
        "permissions": {"allow": ["Bash(npm test)", _CLAUDE_PERMISSION]},
    })
    removal = _uninstall_settings(home, {}, {_CLAUDE_PERMISSION})
    assert removal.action == "remove-entry"
    after = json.loads(removal.after)
    assert after["permissions"]["allow"] == ["Bash(npm test)"]

    untouched = _uninstall_settings(home, {}, set())
    assert untouched.action == "unchanged"
    assert untouched.after is None


def test_cursor_permission_merges_and_preserves_other_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    path = _cli_config_path(home)
    _write_json(path, {
        "approvalMode": "allowlist",
        "permissions": {"allow": ["Shell(ls)"]},
        "theme": "dark",
    })
    plan = _cli_config_plan(home)
    assert plan.owned_entries == (_CURSOR_PERMISSION,)
    install._apply(plan)
    data = json.loads(path.read_text())
    assert data["permissions"]["allow"] == ["Shell(ls)", _CURSOR_PERMISSION]
    assert data["approvalMode"] == "allowlist"
    assert data["theme"] == "dark"

    second = _cli_config_plan(home)
    assert second.action == "unchanged"
    assert second.owned_entries == ()
    assert json.loads(path.read_text())["permissions"]["allow"] == [
        "Shell(ls)", _CURSOR_PERMISSION]


def test_cursor_uninstall_removes_only_the_owned_permission(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    path = _cli_config_path(home)
    _write_json(path, {
        "approvalMode": "allowlist",
        "permissions": {"allow": ["Shell(ls)", _CURSOR_PERMISSION]},
    })
    removal = _cli_config_removal(home, {_CURSOR_PERMISSION}, {})
    assert removal.action == "remove-entry"
    after = json.loads(removal.after)
    assert after["permissions"]["allow"] == ["Shell(ls)"]
    assert after["approvalMode"] == "allowlist"

    untouched = _cli_config_removal(home, set(), {})
    assert untouched.action == "unchanged"
    assert untouched.after is None


def test_install_manifest_records_owned_entries_idempotently(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    reverie_home = home / ".reverie"
    path = _cli_config_path(home)
    _write_json(path, {"permissions": {"allow": [_CURSOR_PERMISSION]}})
    resolved = str(path.resolve())
    install._record_install_manifest(
        reverie_home, [], {resolved: [_CURSOR_PERMISSION]})
    install._record_install_manifest(
        reverie_home, [], {resolved: [_CURSOR_PERMISSION]})
    assert install.load_owned_entries(reverie_home) == {
        resolved: (_CURSOR_PERMISSION,)}
