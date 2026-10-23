"""Gemini CLI integration.

Owns ``~/.gemini/settings.json``, the ``GEMINI.md`` reference, and the TOML
command files (Gemini commands are TOML with a required ``prompt`` string,
not markdown).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..base import Integration
from ..common import (
    _REVIEW_COMMAND_BODY,
    _STATUS_COMMAND_BODY,
    LEGACY_REFERENCE_BLOCK_MARKER,
    POLICY_BLOCK_MARKER,
    ConfiguredServer,
    PlannedRemoval,
    PlannedWrite,
    _agents_ref,
    _block_once,
    _display,
    _json_configured_server,
    _json_merge,
    _legacy_reference_block,
    _mcp_entry,
    _plan_block,
    _plan_json_entry,
    _plan_verbatim_file,
    _policy_block,
    _server_command,
    _verbatim_file,
)


def _gemini_command(body: str, description: str) -> str:
    lines = [f"# {description}", f'description = "{description}"',
             "prompt = '''"]
    lines.extend(body.rstrip("\n").splitlines())
    lines.append("'''")
    return "\n".join(lines) + "\n"


GEMINI_COMMANDS: dict[str, str] = {
    "reverie-review.toml": _gemini_command(
        _REVIEW_COMMAND_BODY, "Review pending Reverie conclusions"),
    "reverie-status.toml": _gemini_command(
        _STATUS_COMMAND_BODY, "Show the current Reverie status"),
}


def _settings_path(home: Path) -> Path:
    return Path(home) / ".gemini" / "settings.json"


def _reference_path(home: Path) -> Path:
    return Path(home) / ".gemini" / "GEMINI.md"


def _commands_dir(home: Path) -> Path:
    return Path(home) / ".gemini" / "commands"


class GeminiIntegration(Integration):
    key = "gemini_cli"
    client = "gemini"
    name = "Gemini CLI"
    binary = "gemini"
    marker = ".gemini/settings.json"
    next_step = ("Restart Gemini CLI; /reverie-review and /reverie-status "
                 "are available in the chat.")

    def install(self, home: Path, reverie_bin: str | None = None,
                dry_run: bool = False, *,
                reverie_home: Path | None = None,
                which=shutil.which) -> list[PlannedWrite]:
        home = Path(home)
        reverie_home = Path(reverie_home) if reverie_home else home / ".reverie"
        ref = _agents_ref(home, reverie_home)
        command, args, env = _server_command(reverie_bin,
                                             client=self.client_key)
        settings = _settings_path(home)
        writes = [_json_merge(
            settings, home, "mcpServers", _mcp_entry(command, args, env),
            f"Added MCP server to {_display(settings, home)}")]
        agents = _reference_path(home)
        writes.append(_block_once(
            agents, home, POLICY_BLOCK_MARKER, _policy_block(),
            f"Wrote {_display(agents, home)} (Reverie usage policy)",
            legacy_blocks=[(LEGACY_REFERENCE_BLOCK_MARKER,
                            _legacy_reference_block(ref))]))
        for name, body in GEMINI_COMMANDS.items():
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
                _settings_path(home), home, "mcpServers", self.key, False,
                manifest),
            _plan_block(
                _reference_path(home), home, _policy_block(),
                POLICY_BLOCK_MARKER, self.key, manifest,
                legacy_blocks=[(LEGACY_REFERENCE_BLOCK_MARKER,
                                _legacy_reference_block(ref))]),
        ]
        for name, body in GEMINI_COMMANDS.items():
            path = _commands_dir(home) / name
            removals.append(_plan_verbatim_file(path, home, body, path.stem))
        return removals

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        return _json_configured_server(
            self.key, _settings_path(home), ("mcpServers", "reverie"))


INTEGRATION = GeminiIntegration()
