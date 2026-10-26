"""Cursor integration.

Owns ``~/.cursor/mcp.json``, the CLI permission in
``~/.cursor/cli-config.json``, and the user-level command files. Cursor
surfaces the MCP server's ``instructions`` field and tool descriptions, so
Reverie deliberately writes no global rules file and never touches the
user's repos — the MCP usage contract reaches the model directly.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..base import Integration
from ..common import (
    _REVIEW_COMMAND_BODY,
    _STATUS_COMMAND_BODY,
    ConfiguredServer,
    InstallError,
    PlannedRemoval,
    PlannedWrite,
    _add_permission_entry,
    _display,
    _json_configured_server,
    _json_merge,
    _mcp_entry,
    _owned_by_install,
    _plan_json_entry,
    _plan_verbatim_file,
    _remove_permission_entry,
    _server_command,
    _verbatim_file,
)

CURSOR_COMMANDS: dict[str, str] = {
    "reverie-review.md": _REVIEW_COMMAND_BODY,
    "reverie-status.md": _STATUS_COMMAND_BODY,
}

#: Cursor CLI permission rule for Reverie's MCP tools.
_CURSOR_PERMISSION = "Mcp(reverie:*)"


def _mcp_path(home: Path) -> Path:
    return Path(home) / ".cursor" / "mcp.json"


def _commands_dir(home: Path) -> Path:
    return Path(home) / ".cursor" / "commands"


def _cli_config_path(home: Path) -> Path:
    return Path(home) / ".cursor" / "cli-config.json"


def _cli_config_plan(home: Path) -> PlannedWrite:
    path = _cli_config_path(home)
    disp = _display(path, home)
    existed = path.exists()
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(
                f"{disp}: not valid JSON ({exc}). Fix or remove it, then "
                "re-run `reverie install`. No changes were made to it.")
        if not isinstance(data, dict):
            raise InstallError(
                f"{disp}: does not contain a JSON object. Fix or remove it, "
                "then re-run `reverie install`. No changes were made to it.")
        original = json.dumps(data, sort_keys=True)
    else:
        data = {}
        original = ""
    added = _add_permission_entry(data, _CURSOR_PERMISSION, disp)
    if original and json.dumps(data, sort_keys=True) == original:
        return PlannedWrite(
            path, "unchanged", None,
            f"{disp} already allows Reverie tools — unchanged")
    detail = (
        f"Updated {disp} (permissions.allow)" if existed else
        f"Created {disp} with permissions.allow")
    return PlannedWrite(
        path, "merge" if existed else "create",
        json.dumps(data, indent=2) + "\n", detail,
        owned_entries=(_CURSOR_PERMISSION,) if added else ())


def _cli_config_removal(home: Path, owned_entries: set[str],
                        manifest: dict[str, str] | None) -> PlannedRemoval:
    path = _cli_config_path(home)
    disp = _display(path, home)
    if not path.exists():
        return PlannedRemoval(path, "unchanged", f"{disp}: not present")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: not valid JSON ({exc}) — fix it by hand if you "
            "want the Reverie permission gone.")
    if not isinstance(data, dict):
        return PlannedRemoval(
            path, "skipped", f"SKIPPED {disp}: does not contain a JSON object.")
    if _CURSOR_PERMISSION not in owned_entries:
        return PlannedRemoval(path, "unchanged", f"{disp}: no Reverie permissions")
    if not _remove_permission_entry(data, _CURSOR_PERMISSION):
        return PlannedRemoval(path, "unchanged", f"{disp}: no Reverie permissions")
    if not data:
        if _owned_by_install(path, manifest):
            return PlannedRemoval(
                path, "delete-file",
                f"Removed {disp} (Reverie created it; only its Reverie "
                "entries were in it)")
        return PlannedRemoval(
            path, "remove-entry",
            f"Removed Reverie permissions from {disp} (empty file kept — it "
            "existed before install)",
            after="{}\n", removed="permissions.allow")
    return PlannedRemoval(
        path, "remove-entry", f"Removed Reverie permissions from {disp}",
        after=json.dumps(data, indent=2) + "\n", removed="permissions.allow")


class CursorIntegration(Integration):
    key = "cursor"
    name = "Cursor"
    binary = "cursor"
    marker = ".cursor/mcp.json"
    next_step = ("Restart Cursor; the tools load in every project. "
                 "/reverie-review and /reverie-status are available in the "
                 "chat (commands are beta).")

    def install(self, home: Path, reverie_bin: str | None = None,
                dry_run: bool = False, *,
                reverie_home: Path | None = None,
                which=shutil.which) -> list[PlannedWrite]:
        home = Path(home)
        command, args, env = _server_command(reverie_bin,
                                             client=self.client_key)
        path = _mcp_path(home)
        writes = [
            _json_merge(
                path, home, "mcpServers", _mcp_entry(command, args, env),
                f"Added MCP server to {_display(path, home)}"),
            _cli_config_plan(home),
        ]
        for name, body in CURSOR_COMMANDS.items():
            target = _commands_dir(home) / name
            writes.append(_verbatim_file(
                target, home, body,
                f"Wrote {_display(target, home)} (/{target.stem})"))
        return writes

    def uninstall(self, home: Path, dry_run: bool = False, *,
                  reverie_home: Path | None = None,
                  manifest: dict[str, str] | None = None
                  ) -> list[PlannedRemoval]:
        home = Path(home)
        reverie_home = Path(reverie_home) if reverie_home else home / ".reverie"
        from ...install import load_owned_entries
        owned = set(load_owned_entries(reverie_home).get(
            str(_cli_config_path(home).resolve()), ()))
        removals = [
            _plan_json_entry(
                _mcp_path(home), home, "mcpServers", self.key, False, manifest),
            _cli_config_removal(home, owned, manifest),
        ]
        for name, body in CURSOR_COMMANDS.items():
            target = _commands_dir(home) / name
            removals.append(_plan_verbatim_file(
                target, home, body, target.stem))
        return removals

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        return _json_configured_server(
            self.key, _mcp_path(home), ("mcpServers", "reverie"))


INTEGRATION = CursorIntegration()
