"""Codex CLI integration.

Owns ``~/.codex/config.toml`` (a real TOML table, not JSON), the Codex
``AGENTS.md`` reference, and the user-level skills under
``~/.agents/skills/``. Codex 0.163 no longer surfaces custom prompts from
``~/.codex/prompts/``; older prompt files are removed only when the install
manifest proves Reverie created them.
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
    LEGACY_REFERENCE_BLOCK_MARKER,
    POLICY_BLOCK_MARKER,
    ConfiguredServer,
    InstallError,
    PlannedRemoval,
    PlannedWrite,
    _agents_ref,
    _block_once,
    _display,
    _legacy_reference_block,
    _owned_by_install,
    _plan_block,
    _policy_block,
    _server_command,
    _verbatim_file,
)

#: Retired user prompts (Codex 0.163 no longer shows them). Kept so install
#: and uninstall can clean up files an older Reverie created.
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

CODEX_SKILLS: dict[str, str] = {
    "reverie-review": (
        "---\n"
        "name: reverie-review\n"
        "description: Review pending Reverie conclusions. Use only when the "
        "user asks to review or check Reverie; never trigger for unrelated "
        "coding work.\n"
        "---\n"
        + _REVIEW_COMMAND_BODY
    ),
    "reverie-status": (
        "---\n"
        "name: reverie-status\n"
        "description: Show the current Reverie status. Use only when the "
        "user asks to review or check Reverie; never trigger for unrelated "
        "coding work.\n"
        "---\n"
        + _STATUS_COMMAND_BODY
    ),
}

CODEX_SKILL_OPENAI = "allow_implicit_invocation: false\n"


def _config_path(home: Path) -> Path:
    return Path(home) / ".codex" / "config.toml"


def _agents_path(home: Path) -> Path:
    return Path(home) / ".codex" / "AGENTS.md"


def _prompts_dir(home: Path) -> Path:
    return Path(home) / ".codex" / "prompts"


def _skills_root(home: Path) -> Path:
    return Path(home) / ".agents" / "skills"


def _skill_dir(home: Path, name: str) -> Path:
    return _skills_root(home) / name


def _skill_path(home: Path, name: str) -> Path:
    return _skill_dir(home, name) / "SKILL.md"


def _skill_openai_path(home: Path, name: str) -> Path:
    return _skill_dir(home, name) / "agents" / "openai.yaml"


def _manifest(reverie_home: Path) -> dict[str, str]:
    from ...install import load_install_manifest
    return load_install_manifest(reverie_home)


def _legacy_prompt_plans(home: Path,
                         manifest: dict[str, str]) -> list[PlannedWrite]:
    """Remove old Codex prompt files only when Reverie owns them."""
    plans = []
    for name in CODEX_PROMPTS:
        path = _prompts_dir(home) / name
        if not path.exists():
            continue
        disp = _display(path, home)
        if _owned_by_install(path, manifest):
            plans.append(PlannedWrite(
                path, "delete-file", None,
                f"Removed {disp} (old Codex prompt — Reverie now installs a "
                "skill)"))
        else:
            plans.append(PlannedWrite(
                path, "unchanged", None,
                f"Left {disp} (not created by Reverie — remove it by hand if "
                "unwanted)"))
    return plans


def _plan_owned_file(path: Path, home: Path,
                     manifest: dict[str, str] | None,
                     label: str) -> PlannedRemoval:
    disp = _display(path, home)
    if not path.exists():
        return PlannedRemoval(path, "unchanged", f"{disp}: not present")
    if _owned_by_install(path, manifest):
        return PlannedRemoval(
            path, "delete-file", f"Removed {disp} ({label})")
    return PlannedRemoval(
        path, "skipped",
        f"SKIPPED {disp}: not created by Reverie (or edited since install) — "
        "left in place")


def _table(reverie_bin: str | None = None,
           client: str | None = None) -> str:
    command, args, env = _server_command(reverie_bin, client=client)
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
    next_step = ("Restart Codex CLI; the reverie-review and reverie-status "
                 "skills are available (use /skills or $reverie-review).")

    def install(self, home: Path, reverie_bin: str | None = None,
                dry_run: bool = False, *,
                reverie_home: Path | None = None,
                which=shutil.which) -> list[PlannedWrite]:
        home = Path(home)
        reverie_home = Path(reverie_home) if reverie_home else home / ".reverie"
        ref = _agents_ref(home, reverie_home)
        writes = [_toml_plan(
            _config_path(home), home,
            _table(reverie_bin, client=self.client_key))]
        agents = _agents_path(home)
        writes.append(_block_once(
            agents, home, POLICY_BLOCK_MARKER, _policy_block(),
            f"Wrote {_display(agents, home)} (Reverie usage policy)",
            legacy_blocks=[(LEGACY_REFERENCE_BLOCK_MARKER,
                            _legacy_reference_block(ref))]))
        for name, body in CODEX_SKILLS.items():
            path = _skill_path(home, name)
            writes.append(_verbatim_file(
                path, home, body,
                f"Wrote {_display(path, home)} (Codex skill ${name})"))
            openai = _skill_openai_path(home, name)
            writes.append(_verbatim_file(
                openai, home, CODEX_SKILL_OPENAI,
                f"Wrote {_display(openai, home)} (explicit invocation only)"))
        writes.extend(_legacy_prompt_plans(home, _manifest(reverie_home)))
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
                _agents_path(home), home, _policy_block(),
                POLICY_BLOCK_MARKER, self.key, manifest,
                legacy_blocks=[(LEGACY_REFERENCE_BLOCK_MARKER,
                                _legacy_reference_block(ref))]),
        ]
        for name in CODEX_PROMPTS:
            removals.append(_plan_owned_file(
                _prompts_dir(home) / name, home, manifest, "old Codex prompt"))
        for name in CODEX_SKILLS:
            folder = _skill_dir(home, name)
            files = [(path, label) for path, label in (
                (_skill_path(home, name), "Codex skill"),
                (_skill_openai_path(home, name), "Codex skill config"),
            )]
            owned_any = False
            blocked = False
            for path, label in files:
                removal = _plan_owned_file(path, home, manifest, label)
                removals.append(removal)
                if removal.action == "delete-file":
                    owned_any = True
                elif removal.action == "skipped":
                    blocked = True
            if not folder.exists() or blocked:
                continue
            expected = {path for path, _label in files}
            extras = [path for path in folder.rglob("*")
                      if path.is_file() and path not in expected]
            disp = _display(folder, home)
            if extras:
                removals.append(PlannedRemoval(
                    folder, "skipped",
                    f"SKIPPED {disp}: contains files Reverie did not create — "
                    "left in place"))
            elif owned_any:
                removals.append(PlannedRemoval(
                    folder, "delete-dir",
                    f"Removed {disp} (Reverie-owned skill folder)"))
        return removals

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        from ..common import _toml_configured_server
        return _toml_configured_server(
            self.key, _config_path(home), ("mcp_servers", "reverie"))


INTEGRATION = CodexIntegration()
