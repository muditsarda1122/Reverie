"""``ec uninstall`` — remove exactly what ``ec install`` did, nothing more.

The installer's safety contract is merge-only: existing agent config is
merged, never overwritten, and only the ``ec`` entry is added. Uninstall
applies the same merge logic in reverse:

- For every supported harness, only Reverie's own entries are removed:
  the ``ec`` MCP server entry in ``~/.claude.json``,
  ``~/.cursor/mcp.json``, ``~/.config/opencode/opencode.json`` and
  ``~/.codex/config.toml``; the ``@~/.ec/AGENTS.md`` line in
  ``~/.claude/CLAUDE.md``; the reference blocks in the OpenCode and Codex
  ``AGENTS.md`` files; ``~/.cursor/rules/ec.md`` (only when Reverie's
  verbatim content is still intact). Every other key, line and file stays
  byte-identical. A file that is invalid JSON/TOML, or was edited so that
  Reverie's entry can't be found unambiguously, is skipped and reported —
  never guessed at.
- **Your memory (``~/.ec``)** is KEPT by default, so a reinstall picks up
  where you left off. With ``--delete-data``: a final export is written
  outside ``~/.ec`` first (default: the current directory) and its path
  printed; then the user must type ``delete``; only then is ``~/.ec``
  removed. ``--no-export`` skips the export (confirmation still required).
  ``--yes`` skips the confirmation for scripts but NEVER skips the export
  unless ``--no-export`` is also given.
- ``--dry-run`` prints every planned removal with the exact before/after
  for each file and changes nothing.

``ec uninstall`` cannot reliably remove its own Python package while it is
running; as the last step it prints the exact command to run by hand
(pip, or the pipx/uv equivalent when that is how it was installed).
"""

from __future__ import annotations

import json
import shutil
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import transport
from .brain import Brain
from .config import ec_home as _default_ec_home
from .install import AGENTS, DIST_NAME, _agents_ref, _reference_block


@dataclass
class PlannedRemoval:
    """One planned uninstall step, computed pure, applied (or printed)
    separately — the mirror of install.PlannedWrite."""
    path: Path
    action: str            # remove-entry | remove-line | remove-block |
                           # delete-file | unchanged | skipped
    detail: str
    after: str | None = None    # resulting content (None for delete/unchanged)
    removed: str | None = None  # what would be removed (for dry-run display)


def _display(path: Path, home: Path) -> str:
    """`~`-relative display for paths under the user's home."""
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# harness reverse-merge plans
# ---------------------------------------------------------------------------

def _plan_json_entry(path: Path, home: Path, section: str,
                     label: str, schema_only_ok: bool = False
                     ) -> PlannedRemoval:
    """Remove ``data[section]["ec"]`` — the reverse of install._json_merge.

    When nothing meaningful remains (the file was created by the
    installer), the file itself is removed. Invalid JSON is skipped,
    never touched."""
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
            "you want the ec entry gone.")
    if not isinstance(data, dict):
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: does not contain a JSON object.")
    node = data.get(section)
    if not isinstance(node, dict) or "ec" not in node:
        return PlannedRemoval(
            path, "unchanged", f"{disp}: no ec entry in '{section}'")
    node = dict(node)
    node.pop("ec")
    remaining = dict(data)
    remaining[section] = node
    if isinstance(remaining[section], dict) and not remaining[section]:
        del remaining[section]           # the section held only the ec entry
    meaningful = {k: v for k, v in remaining.items()
                  if not (schema_only_ok and k == "$schema")}
    if not meaningful:
        return PlannedRemoval(
            path, "delete-file",
            f"Removed {disp} (only Reverie's entry was in it)")
    after = json.dumps(remaining, indent=2) + "\n"
    return PlannedRemoval(
        path, "remove-entry",
        f"Removed the ec MCP server from {disp} ('{section}' preserved)",
        after=after, removed=f"[{section}].ec")


def _plan_line(path: Path, home: Path, line: str, label: str
               ) -> PlannedRemoval:
    """Remove the exact import line — the reverse of install._line_once."""
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
        return PlannedRemoval(
            path, "delete-file",
            f"Removed {disp} (only the EC import line was in it)")
    # The installer appends `<text>\n<line>\n`; restore exactly one
    # trailing newline on the original text.
    after += "\n"
    return PlannedRemoval(
        path, "remove-line", f"Removed the {line} import from {disp}",
        after=after, removed=line)


def _plan_block(path: Path, home: Path, block: str, marker: str, label: str
                ) -> PlannedRemoval:
    """Remove the appended reference block — the reverse of
    install._block_once. The block is only removed when it is found
    verbatim; a block that was edited afterwards is skipped and reported
    (Reverie never guesses)."""
    disp = _display(path, home)
    if not path.exists():
        return PlannedRemoval(path, "unchanged", f"{disp}: not present")
    text = path.read_text(encoding="utf-8")
    if marker not in text:
        return PlannedRemoval(
            path, "unchanged", f"{disp}: no EC reference block")
    if block not in text:
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: the EC reference block was edited after "
            "install — remove it by hand if you want it gone.")
    before = text[:text.index(block)]
    remainder = before + text[text.index(block) + len(block):]
    # The installer appends `<text>\n\n<block>`; restore exactly one
    # trailing newline on the original text. (A file that did not end with
    # a newline before install is restored with one — the append itself is
    # not reversible byte-wise.)
    if not remainder.strip():
        return PlannedRemoval(
            path, "delete-file",
            f"Removed {disp} (only the EC reference block was in it)")
    remainder = remainder.rstrip("\n") + "\n"
    return PlannedRemoval(
        path, "remove-block", f"Removed the EC reference block from {disp}",
        after=remainder, removed=block.strip().splitlines()[0] + " …")


def _plan_verbatim_file(path: Path, home: Path, content: str, label: str
                        ) -> PlannedRemoval:
    """Delete a spec-owned file (e.g. ~/.cursor/rules/ec.md) — but only
    when its content is still exactly what Reverie wrote."""
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


def _plan_toml_table(path: Path, home: Path, label: str) -> PlannedRemoval:
    """Remove the ``[mcp_servers.ec]`` table — the reverse of
    install._codex_toml. Invalid TOML is skipped, never touched."""
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
            "you want the ec entry gone.")
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines)
         if line.strip() == "[mcp_servers.ec]"),
        None,
    )
    if start is None:
        return PlannedRemoval(path, "unchanged", f"{disp}: no ec table")
    end = next(
        (i for i in range(start + 1, len(lines))
         if lines[i].strip().startswith("[")),
        len(lines),
    )
    new_lines = lines[:start] + lines[end:]
    after = "\n".join(new_lines).rstrip("\n")
    if not after.strip():
        return PlannedRemoval(
            path, "delete-file",
            f"Removed {disp} (only Reverie's table was in it)")
    # The installer appends `<text>\n<table>\n`; restore exactly one
    # trailing newline on the original text.
    after += "\n"
    try:
        tomllib.loads(after)
    except tomllib.TOMLDecodeError as exc:
        return PlannedRemoval(
            path, "skipped",
            f"SKIPPED {disp}: removing the table would not parse ({exc}).")
    return PlannedRemoval(
        path, "remove-entry",
        f"Removed [mcp_servers.ec] from {disp} (everything else kept)",
        after=after, removed="[mcp_servers.ec]")


def _plan_harnesses(home: Path, ec_home: Path) -> list[tuple[str,
                                                             PlannedRemoval]]:
    """Every planned harness removal, for ALL supported agents — uninstall
    does not depend on detection: it cleans wherever Reverie may have
    written. Returns [(agent_name, removal), ...]."""
    ref = _agents_ref(home, ec_home)
    block = _reference_block(ref)
    marker = "## Engineering Cognition (EC)"
    plans: list[tuple[str, PlannedRemoval]] = []
    for agent in AGENTS:
        if agent.key == "claude_code":
            entries = [
                _plan_json_entry(home / ".claude.json", home, "mcpServers",
                                 "claude_code"),
                _plan_line(home / ".claude" / "CLAUDE.md", home,
                           f"@{ref}", "claude_code"),
            ]
        elif agent.key == "cursor":
            entries = [
                _plan_json_entry(home / ".cursor" / "mcp.json", home,
                                 "mcpServers", "cursor"),
                _plan_verbatim_file(home / ".cursor" / "rules" / "ec.md",
                                    home, block, "cursor"),
            ]
        elif agent.key == "opencode":
            entries = [
                _plan_json_entry(home / ".config" / "opencode"
                                 / "opencode.json", home, "mcp",
                                 "opencode", schema_only_ok=True),
                _plan_block(home / ".config" / "opencode" / "AGENTS.md",
                            home, block, marker, "opencode"),
            ]
        elif agent.key == "codex":
            entries = [
                _plan_toml_table(home / ".codex" / "config.toml", home,
                                 "codex"),
                _plan_block(home / ".codex" / "AGENTS.md", home, block,
                            marker, "codex"),
            ]
        else:                                   # future agents: nothing yet
            continue
        for entry in entries:
            plans.append((agent.name, entry))
    return plans


def _apply(removal: PlannedRemoval) -> None:
    if removal.action in ("unchanged", "skipped"):
        return
    if removal.action == "delete-file":
        removal.path.unlink(missing_ok=True)
        return
    removal.path.write_text(removal.after or "", encoding="utf-8")


def _emit(removal: PlannedRemoval, home: Path, dry_run: bool, print_fn,
          mark: str) -> None:
    """Print a planned removal (with exact before/after in dry-run), then
    apply it."""
    if removal.action == "unchanged":
        print_fn(f"  · {mark}: {removal.detail}")
        return
    if removal.action == "skipped":
        print_fn(f"  ! {removal.detail}")
        return
    if dry_run:
        print_fn(f"  [dry-run] {removal.action}: "
                 f"{_display(removal.path, home)} — {removal.detail}")
        if removal.removed is not None:
            print_fn(f"      removed: {removal.removed}")
        if removal.after is not None:
            print_fn("      after:")
            for line in removal.after.rstrip("\n").splitlines() or [""]:
                print_fn(f"        {line}")
        elif removal.action == "delete-file":
            print_fn("      (file would be removed)")
        return
    _apply(removal)
    print_fn(f"  ✓ {mark}: {removal.detail}")


# ---------------------------------------------------------------------------
# the package step (last: the running package can't remove itself)
# ---------------------------------------------------------------------------

def package_removal_command() -> str:
    """The exact command the user still has to run by hand, detecting a
    pipx/uv tools install where possible."""
    prefix = sys.prefix
    if "pipx" in prefix:
        return f"pipx uninstall {DIST_NAME}"
    if "uv/tools" in prefix or "uv-tool" in prefix:
        return f"uv tool uninstall {DIST_NAME}"
    return f"pip uninstall {DIST_NAME}"


# ---------------------------------------------------------------------------
# the uninstaller
# ---------------------------------------------------------------------------

def run(*, home: Path | None = None, ec_home: Path | None = None,
        delete_data: bool = False, no_export: bool = False, yes: bool = False,
        dry_run: bool = False, export_dir: Path | None = None,
        print_fn=print, input_fn=input) -> int:
    """§ uninstall. Returns 0 on success (nothing fatal), 1 otherwise."""
    home = Path(home) if home else Path.home()
    ec_home = Path(ec_home) if ec_home else _default_ec_home()

    print_fn("\nEngineering Cognition — Uninstall\n")
    if dry_run:
        print_fn("(dry run — showing exactly what would be removed; "
                 "no files will be changed)\n")

    # -- 1. the final export happens FIRST: a failed export aborts the
    #       whole uninstall so nothing is removed without a backup --------
    export_path: Path | None = None
    if delete_data and not no_export and ec_home.exists():
        target_base = Path(export_dir) if export_dir else Path.cwd()
        export_path = target_base / (
            "reverie-final-export-"
            + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".zip")
        if dry_run:
            print_fn(f"[dry-run] would write a final export to "
                     f"{export_path}")
        else:
            try:
                brain = Brain(ec_home / "ec.db")
                try:
                    transport.export_brain(brain, export_path)
                finally:
                    brain.close()
            except Exception as exc:
                print_fn(f"  ✗ Final export FAILED ({exc}) — nothing was "
                         "removed. Free space or choose another location "
                         "and re-run.")
                return 1
            print_fn(f"  ✓ Final export written to {export_path}")

    # -- 2. harness entries (merge logic in reverse) -----------------------
    print_fn("\nRemoving Reverie from your coding agents...")
    for agent_name, removal in _plan_harnesses(home, ec_home):
        _emit(removal, home, dry_run, print_fn, agent_name)

    # -- 3. your memory (~/.ec) -------------------------------------------
    print_fn(f"\nYour memory ({_display(ec_home, home)})...")
    if not ec_home.exists():
        print_fn("  · not present — nothing to remove")
    elif not delete_data:
        print_fn("  · KEPT (default) — a reinstall picks up where you "
                 "left off. Run `ec uninstall --delete-data` to remove it "
                 "(a final export is taken first).")
    elif dry_run:
        print_fn(f"  [dry-run] would ask for confirmation, then remove "
                 f"{_display(ec_home, home)} entirely")
    else:
        if not yes:
            try:
                answer = input_fn(
                    f"This permanently removes {_display(ec_home, home)} "
                    "(the brain, config and agent instructions).\n"
                    "Type 'delete' to confirm: ").strip()
            except EOFError:
                answer = ""
            if answer != "delete":
                print_fn("  Aborted — your memory was NOT removed. The "
                         "harness entries above are already gone.")
                return 1
        shutil.rmtree(ec_home)
        print_fn(f"  ✓ Removed {_display(ec_home, home)}")

    # -- 4. report ---------------------------------------------------------
    print_fn("\nUninstall report:")
    if not dry_run:
        print_fn("  Removed: Reverie's MCP entries, instruction references "
                 "and command files from the agents above.")
    else:
        print_fn("  (dry run — nothing above was actually removed)")
    if not delete_data:
        print_fn(f"  Kept: {_display(ec_home, home)} (your memory)")
    elif not dry_run:
        print_fn(f"  Removed: {_display(ec_home, home)} (your memory)")
    if delete_data and export_path is not None and not dry_run:
        print_fn(f"  Final export: {export_path}")
    print_fn("\nLast step, run by hand:")
    print_fn(f"  {package_removal_command()}")
    print_fn("  (Reverie's published package name is "
             f"{DIST_NAME}; the running package cannot remove itself)")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="ec uninstall",
        description="Remove exactly what `ec install` did, and nothing "
                    "more: Reverie's MCP entries and instruction "
                    "references in every supported agent. Your memory "
                    "(~/.ec) is kept unless --delete-data is given.",
    )
    parser.add_argument("--delete-data", action="store_true",
                        help="also remove ~/.ec (a final export is written "
                             "first, and you must confirm)")
    parser.add_argument("--no-export", action="store_true",
                        help="with --delete-data: skip the final export "
                             "(confirmation is still required)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt (scripts); the "
                             "export still happens unless --no-export")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every planned removal with the exact "
                             "before/after; change nothing")
    args = parser.parse_args(argv)
    return run(delete_data=args.delete_data, no_export=args.no_export,
               yes=args.yes, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
