"""OpenCode integration.

Owns ``~/.config/opencode/opencode.json``, the OpenCode ``AGENTS.md``
reference, and the user-level command files (markdown + YAML frontmatter).
OpenCode names the MCP environment key ``environment`` and marks the entry
``{"type": "local", "enabled": true}``.
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
    _agents_ref,
    _block_once,
    _display,
    _json_configured_server,
    _json_merge,
    _mcp_entry,
    _plan_block,
    _plan_json_entry,
    _plan_verbatim_file,
    _reference_block,
    _server_command,
    _verbatim_file,
)

_OPENCODE_COMMANDS: dict[str, str] = {}
for _name, _body in (("reverie-review.md", _REVIEW_COMMAND_BODY),
                     ("reverie-status.md", _STATUS_COMMAND_BODY)):
    _OPENCODE_COMMANDS[_name] = (
        "---\n"
        f"description: "
        f"{'Review pending Reverie conclusions' if 'review' in _name else 'Show the current Reverie status'}\n"
        "---\n" + _body
    )
OPENCODE_COMMANDS: dict[str, str] = _OPENCODE_COMMANDS


def _config_path(home: Path) -> Path:
    return Path(home) / ".config" / "opencode" / "opencode.json"


def _agents_path(home: Path) -> Path:
    return Path(home) / ".config" / "opencode" / "AGENTS.md"


def _commands_dir(home: Path) -> Path:
    return Path(home) / ".config" / "opencode" / "commands"


def _entry(command: str, args: list[str], env: dict) -> dict:
    entry = _mcp_entry([command, *args], [], env,
                       {"type": "local", "enabled": True})
    if "env" in entry:
        entry["environment"] = entry.pop("env")
    return entry


class OpenCodeIntegration(Integration):
    key = "opencode"
    name = "OpenCode"
    binary = "opencode"
    marker = ".config/opencode/opencode.json"
    next_step = ("Restart OpenCode; /reverie-review and /reverie-status are "
                 "available in the chat.")

    def install(self, home: Path, reverie_bin: str | None = None,
                dry_run: bool = False, *,
                reverie_home: Path | None = None,
                which=shutil.which) -> list[PlannedWrite]:
        home = Path(home)
        reverie_home = Path(reverie_home) if reverie_home else home / ".reverie"
        ref = _agents_ref(home, reverie_home)
        command, args, env = _server_command(reverie_bin)
        cfg = _config_path(home)
        agents = _agents_path(home)
        writes = [
            _json_merge(
                cfg, home, "mcp", _entry(command, args, env),
                f"Added MCP server to {_display(cfg, home)}",
                schema="https://opencode.ai/config.json"),
            _block_once(
                agents, home, ref, _reference_block(ref),
                f"Wrote {_display(agents, home)} (references {ref})"),
        ]
        for name, body in OPENCODE_COMMANDS.items():
            path = _commands_dir(home) / name
            writes.append(_verbatim_file(
                path, home, body,
                f"Wrote {_display(path, home)} (/{path.stem})"))
        return writes

    def uninstall(self, home: Path, dry_run: bool = False, *,
                  reverie_home: Path | None = None,
                  manifest: dict[str, str] | None = None
                  ) -> list[PlannedRemoval]:
        home = Path(home)
        reverie_home = Path(reverie_home) if reverie_home else home / ".reverie"
        ref = _agents_ref(home, reverie_home)
        removals = [
            _plan_json_entry(
                _config_path(home), home, "mcp", self.key, True, manifest),
            _plan_block(
                _agents_path(home), home, _reference_block(ref), ref,
                self.key, manifest),
        ]
        for name, body in OPENCODE_COMMANDS.items():
            path = _commands_dir(home) / name
            removals.append(_plan_verbatim_file(path, home, body, path.stem))
        return removals

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        return _json_configured_server(
            self.key, _config_path(home), ("mcp", "reverie"))


INTEGRATION = OpenCodeIntegration()
