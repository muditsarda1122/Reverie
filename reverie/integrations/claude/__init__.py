"""Claude Code integration.

Owns every Claude-specific file, key and hook schema:

- ``~/.claude.json`` → ``mcpServers.reverie``
- ``~/.claude/settings.json`` → SessionStart + Stop command hooks
- ``~/.claude/CLAUDE.md`` → ``@~/.reverie/AGENTS.md``
- ``~/.claude/commands/reverie-{review,status}.md``

The command *behaviour* is harness-neutral (``reverie/hooks.py`` renders
SessionStart context, ``reverie capture-turn`` captures a completed
exchange); this package owns Claude's handler shape and its config format.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ... import launch
from ..base import Integration
from ..common import (
    _STATUS_COMMAND_BODY,
    ConfiguredServer,
    InstallError,
    PlannedRemoval,
    PlannedWrite,
    _add_permission_entry,
    _agents_ref,
    _display,
    _json_configured_server,
    _json_merge,
    _line_once,
    _mcp_entry,
    _owned_by_install,
    _plan_json_entry,
    _plan_line,
    _plan_verbatim_file,
    _remove_permission_entry,
    _server_command,
    _verbatim_file,
)

#: Permission rule that lets Claude call Reverie's MCP tools without prompting.
_CLAUDE_PERMISSION = "mcp__reverie__*"

#: The retired deferred-ToolSearch line. Install no longer writes it, but
#: uninstall still removes it from homes written by older versions.
_LEGACY_DEFERRED_TOOLS_MARKER = (
    "Reverie's MCP tools may be deferred in this session.")
_LEGACY_DEFERRED_TOOLS_LINE = (
    _LEGACY_DEFERRED_TOOLS_MARKER
    + " Before your first decision in a Reverie-enabled repo, load them "
    "with ToolSearch: select:mcp__reverie__query,mcp__reverie__observe,"
    "mcp__reverie__summary,mcp__reverie__reconsolidate,"
    "mcp__reverie__review_list,mcp__reverie__review_decide,"
    "mcp__reverie__set_repo_state"
)

#: Keys Reverie owns inside ``mcpServers.reverie``; anything else in that
#: entry belongs to the user and survives an upgrade.
_CLAUDE_MCP_OWNED_KEYS = frozenset(
    {"type", "alwaysLoad", "command", "args", "env"})

_CLAUDE_REVIEW_COMMAND_BODY = """Review your pending Reverie conclusions (the human decides).

Steps:
1. Call review_list to get the pending candidates
   (pass the workspace path as repo_path).
2. Print the review tool result's `message` field verbatim — exactly as
   returned, unchanged. Preserve all line breaks, spacing, candidate text,
   and the options line for every candidate. Do not rewrite, reformat, or
   summarise it. Do not reorder or paraphrase it; do not omit any part of
   it.
3. After printing the listing, ask what the user wants to do with each
   candidate (accept / reject / skip).
4. Relay ONLY the decisions the user states, via review_decide
   (pass the review_token from the listing).
5. Never decide on the user's behalf; never bulk-accept unless the user
   explicitly asked for that group.
"""

CLAUDE_CODE_COMMANDS: dict[str, str] = {
    "reverie-review.md": _CLAUDE_REVIEW_COMMAND_BODY,
    "reverie-status.md": _STATUS_COMMAND_BODY,
}

_STOP_TIMEOUT_SECONDS = 20


def _claude_dir(home: Path) -> Path:
    return Path(home) / ".claude"


def _settings_path(home: Path) -> Path:
    return _claude_dir(home) / "settings.json"


def _reference_path(home: Path) -> Path:
    return _claude_dir(home) / "CLAUDE.md"


def _commands_dir(home: Path) -> Path:
    return _claude_dir(home) / "commands"


def _reference_removal(home: Path, ref: str,
                       manifest: dict[str, str] | None) -> PlannedRemoval:
    """Remove the import line and any legacy deferred-tools reminder."""
    path = _reference_path(home)
    disp = _display(path, home)
    ref_line = f"@{ref}"
    import_removal = _plan_line(path, home, ref_line, "claude", manifest)
    tools_removal = _plan_line(
        path, home, _LEGACY_DEFERRED_TOOLS_LINE, "claude", manifest)
    if tools_removal.action == "unchanged":
        return import_removal
    if import_removal.action == "unchanged":
        return tools_removal
    text = path.read_text(encoding="utf-8")
    gone = (ref_line, _LEGACY_DEFERRED_TOOLS_LINE)
    keep = [line for line in text.splitlines(keepends=True)
            if line.strip() not in gone]
    after = "".join(keep).rstrip("\n")
    removed = f"{ref_line}, legacy deferred-tools reminder"
    if not after:
        if _owned_by_install(path, manifest):
            return PlannedRemoval(
                path, "delete-file",
                f"Removed {disp} (Reverie created it; only its lines were in it)")
        return PlannedRemoval(
            path, "remove-line",
            f"Removed the Reverie lines from {disp} (empty file kept — it "
            "existed before install)", after="", removed=removed)
    return PlannedRemoval(
        path, "remove-line", f"Removed the Reverie lines from {disp}",
        after=after + "\n", removed=removed)


def _command_handler(tail: list[str],
                     reverie_bin: str | None = None) -> dict:
    """Build Claude's command-handler shape around a ``reverie <tail>`` launch.

    ``args`` present means exec form; absent means shell form (Claude has no
    env field, so the development fallback carries PYTHONPATH in the command).
    """
    if reverie_bin:
        return {"type": "command",
                "command": " ".join([reverie_bin, *tail])}
    command, args = launch.reverie_command(tail)
    handler = {"type": "command", "command": command}
    if args:
        handler["args"] = args
    return handler


def _session_start_handler(reverie_bin: str | None = None) -> dict:
    return _command_handler(list(launch.HOOK_ARGS_TAIL), reverie_bin)


def _stop_handler(reverie_bin: str | None = None) -> dict:
    """The Stop handler: the same launch form as session-start, with the
    capture command tail."""
    handler = _command_handler(["capture-turn"], reverie_bin)
    handler["timeout"] = _STOP_TIMEOUT_SECONDS
    handler["async"] = True
    return handler


def _is_reverie_hook(hook: object, event: str) -> bool:
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    args = hook.get("args")
    command = str(hook.get("command", ""))
    if event == "SessionStart":
        return (isinstance(args, list)
                and args[-2:] == ["hook", "session-start"]) or \
            command.endswith(" hook session-start")
    if event == "Stop":
        return (isinstance(args, list) and args[-1:] == ["capture-turn"]) or \
            command.endswith(" capture-turn")
    return False


def _upsert_handler(groups: list, handler: dict, event: str,
                    matcher: str | None) -> None:
    for group in groups:
        if not isinstance(group, dict):
            continue
        if matcher is not None and group.get("matcher") != matcher:
            continue
        if matcher is None and "matcher" in group:
            continue
        hooks = group.setdefault("hooks", [])
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if _is_reverie_hook(hook, event):
                hook.clear()
                hook.update(handler)
                return
        hooks.append(handler)
        return
    group: dict = {"hooks": [handler]}
    if matcher is not None:
        group["matcher"] = matcher
    groups.append(group)


def _settings_plan(home: Path, session_handler: dict, stop_handler: dict
                   ) -> PlannedWrite:
    path = _settings_path(home)
    disp = _display(path, home)
    existed = path.exists()
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(
                f"{disp} is not valid JSON ({exc}). Fix or remove it, then "
                "re-run `reverie install`. No changes were made to it."
            ) from exc
        if not isinstance(data, dict):
            raise InstallError(
                f"{disp} must contain a JSON object. Fix or remove it, then "
                "re-run `reverie install`. No changes were made to it."
            )
        original = json.dumps(data, sort_keys=True)
    else:
        data, original = {}, ""
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(
            f"{disp}: `hooks` is not a JSON object. Fix or remove it, then "
            "re-run `reverie install`. No changes were made to it."
        )
    session_groups = hooks.setdefault("SessionStart", [])
    stop_groups = hooks.setdefault("Stop", [])
    if not isinstance(session_groups, list) or not isinstance(stop_groups, list):
        raise InstallError(
            f"{disp}: `hooks.SessionStart` and `hooks.Stop` must be lists. "
            "Fix or remove it, then re-run `reverie install`. No changes were "
            "made to it."
        )
    _upsert_handler(session_groups, session_handler, "SessionStart", "startup")
    _upsert_handler(stop_groups, stop_handler, "Stop", None)
    added_permission = _add_permission_entry(data, _CLAUDE_PERMISSION, disp)
    if original and json.dumps(data, sort_keys=True) == original:
        return PlannedWrite(
            path, "unchanged", None,
            f"{disp} already has Reverie hooks — unchanged")
    detail = (
        f"Updated Reverie settings in {disp} (SessionStart + Stop hooks, "
        "permissions.allow)" if existed else
        f"Created {disp} with Reverie SessionStart + Stop hooks and "
        "permissions.allow")
    return PlannedWrite(
        path, "merge" if existed else "create",
        json.dumps(data, indent=2) + "\n", detail,
        owned_entries=(_CLAUDE_PERMISSION,) if added_permission else ())


def _uninstall_settings(home: Path,
                        manifest: dict[str, str] | None,
                        owned_entries: set[str] | None = None
                        ) -> PlannedRemoval:
    path = _settings_path(home)
    disp = _display(path, home)
    if not path.exists():
        return PlannedRemoval(path, "unchanged", f"{disp}: not present")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: not valid JSON ({exc}) — fix it by hand if you "
            "want the Reverie hooks gone.")
    if not isinstance(data, dict):
        return PlannedRemoval(
            path, "skipped", f"SKIPPED {disp}: does not contain a JSON object.")
    changed = False
    removed_labels = []
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in ("SessionStart", "Stop"):
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            kept_groups = []
            for group in groups:
                if not isinstance(group, dict):
                    kept_groups.append(group)
                    continue
                entries = group.get("hooks")
                if not isinstance(entries, list):
                    kept_groups.append(group)
                    continue
                kept = [hook for hook in entries
                        if not _is_reverie_hook(hook, event)]
                if len(kept) != len(entries):
                    changed = True
                    removed_labels.append(event)
                if kept:
                    group = dict(group)
                    group["hooks"] = kept
                    kept_groups.append(group)
            if kept_groups:
                hooks[event] = kept_groups
            else:
                hooks.pop(event, None)
        if not hooks:
            data.pop("hooks", None)
    if _CLAUDE_PERMISSION in (owned_entries or set()):
        if _remove_permission_entry(data, _CLAUDE_PERMISSION):
            changed = True
            removed_labels.append("permissions.allow")
    if not changed:
        return PlannedRemoval(path, "unchanged", f"{disp}: no Reverie hooks")
    if not data:
        if _owned_by_install(path, manifest):
            return PlannedRemoval(
                path, "delete-file",
                f"Removed {disp} (Reverie created it; only its Reverie "
                "entries were in it)")
        return PlannedRemoval(
            path, "remove-entry",
            f"Removed Reverie hooks/settings from {disp} (empty file kept — "
            "it existed before install)",
            after="{}\n", removed=", ".join(removed_labels))
    return PlannedRemoval(
        path, "remove-entry", f"Removed Reverie hooks/settings from {disp}",
        after=json.dumps(data, indent=2) + "\n",
        removed=", ".join(removed_labels))


class ClaudeIntegration(Integration):
    key = "claude_code"
    name = "Claude Code"
    binary = "claude"
    marker = ".claude.json"
    next_step = "Restart Claude Code, then run /reverie-status in a repo."

    def install(self, home: Path, reverie_bin: str | None = None,
                dry_run: bool = False, *,
                reverie_home: Path | None = None,
                which=shutil.which) -> list[PlannedWrite]:
        home = Path(home)
        reverie_home = Path(reverie_home) if reverie_home else home / ".reverie"
        ref = _agents_ref(home, reverie_home)
        writes: list[PlannedWrite] = []
        command, args, env = _server_command(reverie_bin,
                                             client=self.client_key)
        mcp_path = home / ".claude.json"
        writes.append(_json_merge(
            mcp_path, home, "mcpServers",
            _mcp_entry(command, args, env,
                       {"type": "stdio", "alwaysLoad": True}),
            f"Added MCP server to {_display(mcp_path, home)} (user scope)",
            owned_keys=_CLAUDE_MCP_OWNED_KEYS))
        writes.append(_line_once(
            _reference_path(home), home, f"@{ref}",
            f"Added @{ref} import to "
            f"{_display(_reference_path(home), home)}"))
        for name, body in CLAUDE_CODE_COMMANDS.items():
            path = _commands_dir(home) / name
            writes.append(_verbatim_file(
                path, home, body,
                f"Wrote {_display(path, home)} (/{path.stem})"))
        writes.append(_settings_plan(
            home, _session_start_handler(reverie_bin),
            _stop_handler(reverie_bin)))
        return writes

    def uninstall(self, home: Path, dry_run: bool = False, *,
                  reverie_home: Path | None = None,
                  manifest: dict[str, str] | None = None
                  ) -> list[PlannedRemoval]:
        home = Path(home)
        reverie_home = Path(reverie_home) if reverie_home else home / ".reverie"
        ref = _agents_ref(home, reverie_home)
        from ...install import load_owned_entries
        owned = set(load_owned_entries(reverie_home).get(
            str(_settings_path(home).resolve()), ()))
        removals: list[PlannedRemoval] = []
        removals.append(_plan_json_entry(
            home / ".claude.json", home, "mcpServers", "claude", False,
            manifest))
        removals.append(_reference_removal(home, ref, manifest))
        removals.append(_uninstall_settings(home, manifest, owned))
        for name, body in CLAUDE_CODE_COMMANDS.items():
            path = _commands_dir(home) / name
            removals.append(_plan_verbatim_file(
                path, home, body, path.stem))
        return removals

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        return _json_configured_server(
            self.key, Path(home) / ".claude.json", ("mcpServers", "reverie"))


INTEGRATION = ClaudeIntegration()
