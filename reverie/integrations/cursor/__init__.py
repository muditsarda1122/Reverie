"""Cursor integration.

Owns ``~/.cursor/mcp.json`` and the user-level command files. Cursor
surfaces the MCP server's ``instructions`` field and tool descriptions, so
Reverie deliberately writes no global rules file and never touches the
user's repos — the MCP usage contract reaches the model directly.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..base import Integration
from ..common import (
    _REVIEW_COMMAND_BODY,
    _STATUS_COMMAND_BODY,
    ConfiguredServer,
    PlannedRemoval,
    PlannedWrite,
    _display,
    _json_configured_server,
    _json_merge,
    _mcp_entry,
    _plan_json_entry,
    _plan_verbatim_file,
    _server_command,
    _verbatim_file,
)

CURSOR_COMMANDS: dict[str, str] = {
    "reverie-review.md": _REVIEW_COMMAND_BODY,
    "reverie-status.md": _STATUS_COMMAND_BODY,
}


def _mcp_path(home: Path) -> Path:
    return Path(home) / ".cursor" / "mcp.json"


def _commands_dir(home: Path) -> Path:
    return Path(home) / ".cursor" / "commands"


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
        command, args, env = _server_command(reverie_bin)
        path = _mcp_path(home)
        writes = [_json_merge(
            path, home, "mcpServers", _mcp_entry(command, args, env),
            f"Added MCP server to {_display(path, home)}")]
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
        removals = [_plan_json_entry(
            _mcp_path(home), home, "mcpServers", self.key, False, manifest)]
        for name, body in CURSOR_COMMANDS.items():
            target = _commands_dir(home) / name
            removals.append(_plan_verbatim_file(
                target, home, body, target.stem))
        return removals

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        return _json_configured_server(
            self.key, _mcp_path(home), ("mcpServers", "reverie"))


INTEGRATION = CursorIntegration()
