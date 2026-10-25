"""Shared plumbing for harness integrations.

Everything here is harness-neutral: planned writes/removals, merge helpers,
the instruction-reference block, and reading a configured MCP entry back.
A concrete integration lives in its own package (``claude``, ``opencode``,
``codex``, ``cursor``) and owns every file path, key name and
hook schema for that harness.

The core modules import this package only through the registry; nothing in
the core names a harness.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..config import reverie_home as _default_reverie_home
from ..policy import USAGE_POLICY


class InstallError(RuntimeError):
    """Installation failure with actionable guidance (§28.4 message style)."""


# ---------------------------------------------------------------------------
# planned writes — computed pure, applied (or printed) separately
# ---------------------------------------------------------------------------

@dataclass
class PlannedWrite:
    path: Path
    action: str                # create | merge | append | update | delete-file | unchanged | create-db
    content: str | None        # full resulting content (None for unchanged/create-db)
    detail: str                # transcript-style note for the ✓ line
    owned_entries: tuple[str, ...] = ()   # config entries this write adds


@dataclass
class PlannedRemoval:
    """One planned uninstall step, computed pure, applied separately — the
    mirror of PlannedWrite."""
    path: Path
    action: str            # remove-entry | remove-line | remove-block |
                           # delete-file | delete-dir | unchanged | skipped
    detail: str
    after: str | None = None    # resulting content (None for delete/unchanged)
    removed: str | None = None  # what would be removed (for dry-run display)


def _display(path: Path, home: Path) -> str:
    """`~`-relative display for paths under the user's home."""
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def _agents_ref(home: Path, reverie_home: Path) -> str:
    """How agent configs refer to the Reverie AGENTS.md (spec form when default)."""
    return ("~/.reverie/AGENTS.md" if Path(reverie_home) == Path(home) / ".reverie"
            else str(Path(reverie_home) / "AGENTS.md"))


# ---------------------------------------------------------------------------
# forward merges
# ---------------------------------------------------------------------------

def _json_merge(path: Path, home: Path, section: str, entry: dict,
                detail: str, schema: str | None = None,
                owned_keys: frozenset[str] | None = None) -> PlannedWrite:
    """Merge ``entry`` into ``data[section]["reverie"]`` — never overwriting siblings.

    When ``owned_keys`` is given, an existing reverie entry is updated
    key-by-key: Reverie's own keys are replaced (or dropped when the new
    entry no longer sets them) while any other keys the user added to that
    entry are kept.
    """
    disp = _display(path, home)
    existed = path.exists()
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(
                f"{disp} is not valid JSON ({exc}). Fix or remove it, then "
                "re-run `python -m reverie.install`. No changes were made to it."
            ) from exc
        if not isinstance(data, dict):
            raise InstallError(
                f"{disp} must contain a JSON object. Fix or remove it, then "
                "re-run `python -m reverie.install`. No changes were made to it."
            )
    else:
        data = {}
        if schema is not None:
            data["$schema"] = schema
    node = data.setdefault(section, {})
    if not isinstance(node, dict):
        raise InstallError(
            f"{disp}: `{section}` is not a JSON object. Fix or remove it, "
            "then re-run `python -m reverie.install`. No changes were made to it."
        )
    existing = node.get("reverie")
    if owned_keys is not None and isinstance(existing, dict):
        merged = {key: value for key, value in existing.items()
                  if key not in owned_keys}
        merged.update(entry)
    else:
        merged = entry
    if existing == merged:
        return PlannedWrite(path, "unchanged", None,
                            f"{disp} already has the reverie MCP server — unchanged")
    node["reverie"] = merged
    action = "merge" if existed else "create"
    return PlannedWrite(path, action, json.dumps(data, indent=2) + "\n", detail)


def _add_permission_entry(data: dict, entry: str, disp: str) -> bool:
    """Add ``entry`` to ``permissions.allow`` once; return whether it was new."""
    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise InstallError(
            f"{disp}: `permissions` is not a JSON object. Fix or remove it, "
            "then re-run `reverie install`. No changes were made to it."
        )
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        raise InstallError(
            f"{disp}: `permissions.allow` must be a JSON list. Fix or remove "
            "it, then re-run `reverie install`. No changes were made to it."
        )
    if entry in allow:
        return False
    allow.append(entry)
    return True


def _remove_permission_entry(data: dict, entry: str) -> bool:
    """Remove one occurrence of ``entry``; drop now-empty owners."""
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return False
    allow = permissions.get("allow")
    if not isinstance(allow, list) or entry not in allow:
        return False
    index = allow.index(entry)
    kept = allow[:index] + allow[index + 1:]
    if kept:
        permissions["allow"] = kept
    else:
        permissions.pop("allow", None)
        if not permissions:
            data.pop("permissions", None)
    return True


def _line_once(path: Path, home: Path, line: str, detail: str) -> PlannedWrite:
    """Append ``line`` to a text file, creating it if absent (idempotent)."""
    disp = _display(path, home)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if line in (existing.strip() for existing in text.splitlines()):
            return PlannedWrite(path, "unchanged", None,
                                f"{disp} already references {line} — unchanged")
        new = text if text.endswith("\n") else text + "\n"
        if text.strip():
            new += "\n"
        return PlannedWrite(path, "append", new + line + "\n", detail)
    return PlannedWrite(path, "create", line + "\n", detail)


def _legacy_block_span(text: str, marker: str) -> tuple[int, int] | None:
    """Byte span of a retired ``marker`` section: its heading to the next
    level-2 heading (or EOF). The heading must begin its own line."""
    start = None
    offset = 0
    for line in text.splitlines(keepends=True):
        if start is None:
            if line.rstrip("\r\n").rstrip() == marker:
                start = offset
        elif line.startswith("## "):
            return start, offset
        offset += len(line)
    if start is not None:
        return start, len(text)
    return None


def _inline_block_span(text: str) -> tuple[int, int] | None:
    """Byte span of the marker-delimited block: from the opening marker
    through the closing marker and its trailing newline — or None when the
    markers do not delimit a complete block."""
    start = text.find(POLICY_BLOCK_START)
    if start == -1:
        return None
    end = text.find(POLICY_BLOCK_END, start + len(POLICY_BLOCK_START))
    if end == -1:
        return None
    end += len(POLICY_BLOCK_END)
    if text[end:end + 1] == "\n":
        end += 1
    return start, end


def _block_once(path: Path, home: Path, marker: str, block: str,
                detail: str,
                legacy_blocks: list[tuple[str, str]] | None = None
                ) -> PlannedWrite:
    """Append a markdown block containing ``marker`` (idempotent).

    ``legacy_blocks`` migrates older blocks in place: each entry is
    ``(marker, block)``. When the current block is already present the file
    is unchanged (a stale legacy section is removed without duplicating the
    current one); otherwise the legacy section — from its marker heading to
    the next level-2 heading or EOF — is replaced by ``block``, even when
    the legacy text was edited. Bytes outside that span are left untouched.

    When the markers are present but the inlined text is not exactly
    ``block`` (a block written by an older policy), the text between the
    markers is replaced by ``block`` and reported as an update; bytes
    outside the markers stay untouched.
    """
    disp = _display(path, home)
    legacy_blocks = list(legacy_blocks or [])
    if path.exists():
        text = path.read_text(encoding="utf-8")
        spans = [span for legacy_marker, _legacy in legacy_blocks
                 if (span := _legacy_block_span(text, legacy_marker))
                 is not None]
        if marker in text:
            span = _inline_block_span(text)
            stale = span is not None and text[span[0]:span[1]] != block
            if stale or spans:
                inline = ([(span[0], span[1], block)]
                          if stale and span is not None else [])
                edits = inline + [
                    (start, end, "") for start, end in spans]
                edits.sort()
                out: list[str] = []
                offset = 0
                for start, end, replacement in edits:
                    out += [text[offset:start], replacement]
                    offset = end
                out.append(text[offset:])
                new_text = "".join(out)
                if stale:
                    refreshed = (f"{disp}: refreshed the Reverie block "
                                 "(usage policy changed)")
                    if spans:
                        refreshed += "; removed the old pointer block"
                    return PlannedWrite(path, "update", new_text, refreshed)
                return PlannedWrite(
                    path, "update", new_text,
                    f"{disp}: removed the old Reverie block (current "
                    "block already present)")
            return PlannedWrite(path, "unchanged", None,
                                f"{disp} already references {marker} — unchanged")
        if spans:
            start, end = spans[0]
            return PlannedWrite(
                path, "update", text[:start] + block + text[end:], detail)
        if any(old_marker in text for old_marker, _ in legacy_blocks):
            return PlannedWrite(
                path, "unchanged", None,
                f"{disp}: an older Reverie block was edited — left untouched; "
                "remove it by hand to install the current block")
        new = text if text.endswith("\n") else text + "\n"
        if text.strip():
            new += "\n"
        return PlannedWrite(path, "append", new + block, detail)
    return PlannedWrite(path, "create", block, detail)


def _verbatim_file(path: Path, home: Path, content: str,
                   detail: str) -> PlannedWrite:
    """Spec-owned file (D25): always written verbatim; unchanged if equal."""
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == content:
        return PlannedWrite(
            path, "unchanged", None,
            f"{_display(path, home)} already up to date — unchanged")
    return PlannedWrite(
        path, "update" if path.exists() else "create", content, detail)


def _mcp_entry(command: "str | list[str]", args: list[str], env: dict,
               extra: dict | None = None) -> dict:
    """JSON MCP-server entry; empty args/env keys are omitted (D45 writes the
    absolute console-script path as just {"command": "/abs/path/reverie-mcp"})."""
    entry = dict(extra or {})
    entry["command"] = command
    if args:
        entry["args"] = list(args)
    if env:
        entry["env"] = dict(env)
    return entry


def _server_command(reverie_bin: str | None = None,
                    client: str | None = None) -> tuple[str, list[str], dict]:
    """The MCP server command for the harness config.

    ``reverie_bin`` (the integration contract's second argument) is accepted
    for callers that already resolved a Reverie launcher; the MCP entry still
    uses the core resolver, which knows whether the package is pip-installed.
    ``client`` is the harness key recorded in the launch args (logs and
    future per-client wording only).
    """
    from ..install import server_command
    command, args, env = server_command(client=client)
    if reverie_bin:
        return str(reverie_bin), (["--client", client] if client else []), {}
    return command, args, env


def _default_reverie_home_or(reverie_home: Path | None) -> Path:
    return Path(reverie_home) if reverie_home else _default_reverie_home()


# ---------------------------------------------------------------------------
# reading a configured server entry back (install verification + status)
# ---------------------------------------------------------------------------

@dataclass
class ConfiguredServer:
    """One harness config's reverie MCP server entry, read back from disk —
    the exact command that harness will launch."""
    agent: str                  # integration key
    config_path: Path
    command: str
    args: list[str]
    env: dict[str, str]


def _load_json_object(path: Path) -> dict | None:
    """Parse a JSON config file; None when absent, unreadable or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _server_from_entry(agent: str, path: Path,
                       entry: object) -> ConfiguredServer | None:
    """Normalize a JSON/TOML MCP entry (``command`` string or argv list) into
    a ConfiguredServer; None when the shape is not a launchable command."""
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    args: list[str] = []
    if isinstance(command, list):
        if not command or not all(isinstance(token, str) for token in command):
            return None
        command, args = command[0], list(command[1:])
    if not isinstance(command, str) or not command:
        return None
    raw_args = entry.get("args")
    if isinstance(raw_args, list) and all(isinstance(a, str) for a in raw_args):
        args = [*args, *raw_args]
    raw_env = entry.get("env") or entry.get("environment") or {}
    env = ({k: v for k, v in raw_env.items()
            if isinstance(k, str) and isinstance(v, str)}
           if isinstance(raw_env, dict) else {})
    return ConfiguredServer(agent, path, command, args, env)


def _json_configured_server(agent: str, path: Path,
                            keys: tuple[str, ...]) -> ConfiguredServer | None:
    node: object = _load_json_object(path)
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return _server_from_entry(agent, path, node)


def _toml_configured_server(agent: str, path: Path,
                            keys: tuple[str, ...]) -> ConfiguredServer | None:
    try:
        node: object = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return _server_from_entry(agent, path, node)


# ---------------------------------------------------------------------------
# reference block (shared instruction file fragment)
# ---------------------------------------------------------------------------

def _policy_block() -> str:
    """The inlined usage policy, delimited by machine-readable markers."""
    return f"{POLICY_BLOCK_START}\n{USAGE_POLICY}{POLICY_BLOCK_END}\n"


def _legacy_reference_block(agents_ref: str) -> str:
    """The pre-inlining pointer block, kept only for migration/uninstall."""
    return (
        f"## Reverie\n\n"
        f"Follow the Reverie agent instructions at {agents_ref}. Reverie is "
        f"an MCP server (`reverie`) providing engineering memory via "
        f"observe, query, summary, "
        f"reconsolidate, set_repo_state, "
        f"review_list, and review_decide. Sessions are "
        f"automatic per branch — never suggest starting or stopping a "
        f"session. Pass the workspace path as repo_path on every call. The "
        f"user reviews candidates via `reverie review` or by asking the "
        f"agent to show them; the agent only relays decisions the user "
        f"states.\n"
    )


def _reference_block(agents_ref: str | None = None) -> str:
    """Back-compat name for the current inlined policy block.

    ``agents_ref`` is accepted because older callers passed the AGENTS.md
    path; the current block no longer points at a file.
    """
    return _policy_block()


#: Opening marker of the inlined usage-policy block in AGENTS.md-style files.
POLICY_BLOCK_START = "<!-- reverie:start -->"
#: Closing marker of the inlined usage-policy block.
POLICY_BLOCK_END = "<!-- reverie:end -->"
#: Marker identifying the inlined usage-policy block.
POLICY_BLOCK_MARKER = POLICY_BLOCK_START
#: Marker line identifying Reverie's current block (install appends it;
#: uninstall removes only exact blocks).
REFERENCE_BLOCK_MARKER = POLICY_BLOCK_START
#: Marker of the retired pointer block, retained for migration/uninstall.
LEGACY_REFERENCE_BLOCK_MARKER = "## Reverie"


# ---------------------------------------------------------------------------
# user-level command bodies (harness packages wrap these in their own format)
# ---------------------------------------------------------------------------

_REVIEW_COMMAND_BODY = """Review your pending Reverie conclusions (the human decides).

Steps:
1. Call review_list to get the pending candidates
   (pass the workspace path as repo_path).
2. Show EVERY candidate's full conclusion text, one at a time.
3. Ask what the user wants to do with each (accept / reject / skip).
4. Relay ONLY the decisions the user states, via review_decide
   (pass the review_token from the listing).
5. Never decide on the user's behalf; never bulk-accept unless the user
   explicitly asked for that group.
"""

_STATUS_COMMAND_BODY = """Show the current Reverie status.

Steps:
1. Call summary (pass the workspace path as repo_path).
2. Report: brain statistics, session state for the current (repo, branch),
   and the pending-review count. Statistics only — never candidate
   contents unless the user asks for a review (use /reverie-review).
"""


# ---------------------------------------------------------------------------
# reverse merges (uninstall)
# ---------------------------------------------------------------------------

def _owned_by_install(path: Path, manifest: dict[str, str] | None) -> bool:
    """True only when install created this exact file and it is unedited
    (current sha256 == the hash recorded at install time). The manifest is
    the uninstaller's licence to delete a whole file; without a match the
    file is kept and only Reverie's entry is removed (review fix 39)."""
    if not manifest:
        return False
    expected = manifest.get(str(Path(path).resolve()))
    if expected is None:
        return False
    try:
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return False
    return actual == expected


def _plan_json_entry(path: Path, home: Path, section: str,
                     label: str, schema_only_ok: bool = False,
                     manifest: dict[str, str] | None = None
                     ) -> PlannedRemoval:
    """Remove ``data[section]["reverie"]`` — the reverse of _json_merge.

    The file itself is removed only when install created it and the content
    still matches the install-time hash (the manifest); a file that existed
    before install is always kept (reset to whatever remains — down to an
    empty object). Invalid JSON is skipped, never touched."""
    disp = _display(path, home)
    if not path.exists():
        return PlannedRemoval(path, "unchanged", f"{disp}: not present")
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: not valid JSON ({exc}) — fix it by hand if "
            "you want the reverie entry gone.")
    if not isinstance(data, dict):
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: does not contain a JSON object.")
    node = data.get(section)
    if not isinstance(node, dict) or "reverie" not in node:
        return PlannedRemoval(
            path, "unchanged", f"{disp}: no reverie entry in '{section}'")
    node = dict(node)
    node.pop("reverie")
    remaining = dict(data)
    remaining[section] = node
    if isinstance(remaining[section], dict) and not remaining[section]:
        del remaining[section]           # the section held only the reverie entry
    meaningful = {k: v for k, v in remaining.items()
                  if not (schema_only_ok and k == "$schema")}
    if not meaningful:
        if _owned_by_install(path, manifest):
            return PlannedRemoval(
                path, "delete-file",
                f"Removed {disp} (Reverie created it; only its entry was in it)")
        after = json.dumps(remaining, indent=2) + "\n"
        return PlannedRemoval(
            path, "remove-entry",
            f"Removed the reverie MCP server from {disp} (file kept — it "
            "existed before install)",
            after=after, removed=f"[{section}].reverie")
    after = json.dumps(remaining, indent=2) + "\n"
    return PlannedRemoval(
        path, "remove-entry",
        f"Removed the reverie MCP server from {disp} ('{section}' preserved)",
        after=after, removed=f"[{section}].reverie")


def _plan_line(path: Path, home: Path, line: str, label: str,
               manifest: dict[str, str] | None = None) -> PlannedRemoval:
    """Remove the exact import line — the reverse of _line_once."""
    disp = _display(path, home)
    if not path.exists():
        return PlannedRemoval(path, "unchanged", f"{disp}: not present")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    keep = [ln for ln in lines if ln.strip() != line]
    if len(keep) == len(lines):
        return PlannedRemoval(
            path, "unchanged", f"{disp}: no {line!r} line")
    after = "".join(keep).rstrip("\n")
    if not after:
        if _owned_by_install(path, manifest):
            return PlannedRemoval(
                path, "delete-file",
                f"Removed {disp} (Reverie created it; only the import line "
                "was in it)")
        return PlannedRemoval(
            path, "remove-line",
            f"Removed the {line} import from {disp} (empty file kept — it "
            "existed before install)",
            after="", removed=line)
    after += "\n"
    return PlannedRemoval(
        path, "remove-line", f"Removed the {line} import from {disp}",
        after=after, removed=line)


def _plan_block(path: Path, home: Path, block: str, marker: str, label: str,
                manifest: dict[str, str] | None = None,
                legacy_blocks: list[tuple[str, str]] | None = None
                ) -> PlannedRemoval:
    """Remove the appended reference block — the reverse of _block_once.

    ``legacy_blocks`` are the retired pointer blocks (``(marker, block)``):
    uninstall removes every exact block present, so an old install and a
    migrated one are both cleaned. A block that was edited afterwards is
    skipped and reported (Reverie never guesses)."""
    disp = _display(path, home)
    legacy_blocks = list(legacy_blocks or [])
    if not path.exists():
        return PlannedRemoval(path, "unchanged", f"{disp}: not present")
    text = path.read_text(encoding="utf-8")
    candidates = [(marker, block), *legacy_blocks]
    present = [(m, b) for m, b in candidates if m in text]
    if not present:
        return PlannedRemoval(
            path, "unchanged", f"{disp}: no Reverie reference block")
    exact = [(m, b) for m, b in candidates if b in text]
    if len(exact) != len(present):
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: the Reverie reference block was edited after "
            "install — remove it by hand if you want it gone.")
    remainder = text
    for _m, candidate in exact:
        remainder = remainder.replace(candidate, "", 1)
    if not remainder.strip():
        if _owned_by_install(path, manifest):
            return PlannedRemoval(
                path, "delete-file",
                f"Removed {disp} (Reverie created it; only its reference "
                "block was in it)")
        return PlannedRemoval(
            path, "remove-block",
            f"Removed the Reverie reference block from {disp} (empty file kept — "
            "it existed before install)",
            after="", removed=block.strip().splitlines()[0] + " …")
    remainder = remainder.rstrip("\n") + "\n"
    return PlannedRemoval(
        path, "remove-block", f"Removed the Reverie reference block from {disp}",
        after=remainder, removed=block.strip().splitlines()[0] + " …")


def _plan_verbatim_file(path: Path, home: Path, content: str, label: str
                        ) -> PlannedRemoval:
    """Delete a spec-owned file — but only when its content is still exactly
    what Reverie wrote."""
    disp = _display(path, home)
    if not path.exists():
        return PlannedRemoval(path, "unchanged", f"{disp}: not present")
    if path.read_text(encoding="utf-8") != content:
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: edited after install — not deleting your "
            "changes; remove it by hand if you want it gone.")
    return PlannedRemoval(
        path, "delete-file", f"Removed {disp} (Reverie-owned file)")
