"""Codex CLI integration.

Owns ``~/.codex/config.toml`` (a real TOML table, not JSON), the Codex
``AGENTS.md`` reference, and the deprecated-but-working user prompts under
``~/.codex/prompts/``.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

from ..base import Integration
from ..common import (
    _REVIEW_COMMAND_BODY,
    _STATUS_COMMAND_BODY,
    REFERENCE_BLOCK_MARKER,
    ConfiguredServer,
    InstallError,
    PlannedRemoval,
    PlannedWrite,
    _agents_ref,
    _block_once,
    _display,
    _owned_by_install,
    _plan_block,
    _plan_verbatim_file,
    _reference_block,
    _server_command,
    _verbatim_file,
)

CODEX_PROMPTS: dict[str, str] = {
    "reverie-review.md": (
        "---\ndescription: Review pending Reverie conclusions\n---\n"
        + _REVIEW_COMMAND_BODY
    ),
    "reverie-status.md": (
        "---\ndescription: Show the current Reverie status\n---\n"
        + _STATUS_COMMAND_BODY
    ),
}


def _config_path(home: Path) -> Path:
    return Path(home) / ".codex" / "config.toml"


def _agents_path(home: Path) -> Path:
    return Path(home) / ".codex" / "AGENTS.md"


def _prompts_dir(home: Path) -> Path:
    return Path(home) / ".codex" / "prompts"


def _table(reverie_bin: str | None = None) -> str:
    command, args, env = _server_command(reverie_bin)
    lines = ["[mcp_servers.reverie]", f"command = {json.dumps(command)}"]
    if args:
        lines.append(f"args = {json.dumps(args)}")
    if env:
        lines.append(
            f"env = {{ PYTHONPATH = {json.dumps(env['PYTHONPATH'])} }}")
    return "\n".join(lines)


def _toml_plan(path: Path, home: Path, table: str) -> PlannedWrite:
    disp = _display(path, home)
    if not path.exists():
        return PlannedWrite(
            path, "create", table + "\n",
            f"Added [mcp_servers.reverie] to {disp}")
    text = path.read_text(encoding="utf-8")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(
            f"{disp} is not valid TOML ({exc}). Fix or remove it, then "
            "re-run `reverie install`. No changes were made to it."
        ) from exc
    lines = text.splitlines()
    start = next(
        (i for i, table_line in enumerate(lines)
         if table_line.strip() == "[mcp_servers.reverie]"),
        None,
    )
    if start is None:
        new = text if text.endswith("\n") else text + "\n"
        if text.strip():
            new += "\n"
        new += table + "\n"
    else:
        end = next(
            (i for i in range(start + 1, len(lines))
             if lines[i].strip().startswith("[")),
            len(lines),
        )
        replacement = table.splitlines()
        if end < len(lines):
            replacement.append("")
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        new = "\n".join(lines[:start] + replacement + lines[end:]) + "\n"
    if new == text:
        return PlannedWrite(
            path, "unchanged", None,
            f"{disp} already has [mcp_servers.reverie] — unchanged")
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(
            f"refusing to write {disp}: merged TOML would not parse ({exc}). "
            "No changes were made to it."
        ) from exc
    return PlannedWrite(
        path, "merge" if path.exists() else "create", new,
        f"Added [mcp_servers.reverie] to {disp}")


def _uninstall_toml(path: Path, home: Path,
                    manifest: dict[str, str] | None) -> PlannedRemoval:
    disp = _display(path, home)
    if not path.exists():
        return PlannedRemoval(path, "unchanged", f"{disp}: not present")
    text = path.read_text(encoding="utf-8")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: not valid TOML ({exc}) — fix it by hand if "
            "you want the reverie entry gone.")
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines)
         if line.strip() == "[mcp_servers.reverie]"),
        None,
    )
    if start is None:
        return PlannedRemoval(path, "unchanged", f"{disp}: no reverie table")
    end = next(
        (i for i in range(start + 1, len(lines))
         if lines[i].strip().startswith("[")),
        len(lines),
    )
    new_lines = lines[:start] + lines[end:]
    after = "\n".join(new_lines).rstrip("\n")
    if not after.strip():
        if _owned_by_install(path, manifest):
            return PlannedRemoval(
                path, "delete-file",
                f"Removed {disp} (Reverie created it; only its table was in it)")
        return PlannedRemoval(
            path, "remove-entry",
            f"Removed [mcp_servers.reverie] from {disp} (empty file kept — "
            "it existed before install)",
            after="", removed="[mcp_servers.reverie]")
    after += "\n"
    try:
        tomllib.loads(after)
    except tomllib.TOMLDecodeError as exc:
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: removing the table would not parse ({exc}).")
    return PlannedRemoval(
        path, "remove-entry",
        f"Removed [mcp_servers.reverie] from {disp} (everything else kept)",
        after=after, removed="[mcp_servers.reverie]")


class CodexIntegration(Integration):
    key = "codex"
    name = "Codex"
    binary = "codex"
    marker = ".codex/config.toml"
    next_step = ("Restart Codex CLI; /prompts:reverie-review and "
                 "/prompts:reverie-status are available (custom prompts are "
                 "deprecated upstream but work).")

    def install(self, home: Path, reverie_bin: str | None = None,
                dry_run: bool = False, *,
                reverie_home: Path | None = None,
                which=shutil.which) -> list[PlannedWrite]:
        home = Path(home)
        reverie_home = Path(reverie_home) if reverie_home else home / ".reverie"
        ref = _agents_ref(home, reverie_home)
        writes = [_toml_plan(_config_path(home), home, _table(reverie_bin))]
        agents = _agents_path(home)
        writes.append(_block_once(
            agents, home, REFERENCE_BLOCK_MARKER, _reference_block(ref),
            f"Wrote {_display(agents, home)} (references {ref})"))
        for name, body in CODEX_PROMPTS.items():
            path = _prompts_dir(home) / name
            writes.append(_verbatim_file(
                path, home, body,
                f"Wrote {_display(path, home)} (/prompts:{path.stem})"))
        return writes

    def uninstall(self, home: Path, dry_run: bool = False, *,
                  reverie_home: Path | None = None,
                  manifest: dict[str, str] | None = None
                  ) -> list[PlannedRemoval]:
        home = Path(home)
        reverie_home = Path(reverie_home) if reverie_home else home / ".reverie"
        ref = _agents_ref(home, reverie_home)
        removals = [
            _uninstall_toml(_config_path(home), home, manifest),
            _plan_block(
                _agents_path(home), home, _reference_block(ref),
                REFERENCE_BLOCK_MARKER, self.key, manifest),
        ]
        for name, body in CODEX_PROMPTS.items():
            path = _prompts_dir(home) / name
            removals.append(_plan_verbatim_file(path, home, body, path.stem))
        return removals

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        from ..common import _toml_configured_server
        return _toml_configured_server(
            self.key, _config_path(home), ("mcp_servers", "reverie"))


INTEGRATION = CodexIntegration()
