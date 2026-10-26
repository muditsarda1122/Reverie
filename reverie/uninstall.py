"""``reverie uninstall`` — remove exactly what ``reverie install`` did, nothing more.

The installer's safety contract is merge-only: existing agent config is
merged, never overwritten, and only the ``reverie`` entry is added. Uninstall
applies the same merge logic in reverse:

- For every supported harness, only Reverie's own entries are removed:
  the ``reverie`` MCP server entry in ``~/.claude.json``,
  ``~/.cursor/mcp.json``, ``~/.config/opencode/opencode.json`` and
  ``~/.codex/config.toml``; the ``@~/.reverie/AGENTS.md`` line in
  ``~/.claude/CLAUDE.md``; the Reverie SessionStart and Stop hook handlers
  in ``~/.claude/settings.json`` (emptied matcher groups and keys are
  dropped with them); the reference blocks in the OpenCode/Codex
  ``AGENTS.md`` files; the user-level slash-command files (only while their
  content is still Reverie's verbatim text). Every other key, line and file stays
  byte-identical. A file that is invalid JSON/TOML, or was edited so that
  Reverie's entry can't be found unambiguously, is skipped and reported —
  never guessed at.
- **The plan comes first.** The full plan (harness entries; whether data
  will be exported and deleted) is printed and ONE confirmation covers it
  (``--yes`` skips). Nothing is touched before the confirmation; aborting
  leaves everything unchanged.
- **Your memory (``~/.reverie``)** is KEPT by default, so a reinstall picks up
  where you left off. With ``--delete-data``: a final export is written
  outside ``~/.reverie`` first (default: the current directory) and its path
  printed; only then is ``~/.reverie`` removed. ``--no-export`` skips the
  export (the confirmation still covers the deletion). ``--yes`` skips the
  confirmation for scripts but NEVER skips the export unless ``--no-export``
  is also given.
- ``--dry-run`` prints every planned removal with the exact before/after
  for each file and changes nothing.

``reverie uninstall`` cannot reliably remove its own Python package while it is
running; as the last step it prints the exact command to run by hand
(pip, or the pipx/uv equivalent when that is how it was installed).
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import transport
from .brain import Brain
from .config import reverie_home as _default_reverie_home
from .install import DIST_NAME, load_install_manifest
from .integrations import AGENTS
from .integrations.common import PlannedRemoval, _display


def _plan_harnesses(home: Path, reverie_home: Path,
                    manifest: dict[str, str] | None = None
                    ) -> list[tuple[str, PlannedRemoval]]:
    """Every planned harness removal, for ALL supported agents — uninstall
    does not depend on detection: it cleans wherever Reverie may have
    written. Each integration computes its own reverse plan."""
    plans: list[tuple[str, PlannedRemoval]] = []
    for integration in AGENTS:
        for removal in integration.uninstall(
                home, True, reverie_home=reverie_home, manifest=manifest):
            plans.append((integration.name, removal))
    return plans


def _apply(removal: PlannedRemoval) -> None:
    if removal.action in ("unchanged", "skipped"):
        return
    if removal.action == "delete-file":
        removal.path.unlink(missing_ok=True)
        return
    if removal.action == "delete-dir":
        if removal.path.is_dir():
            shutil.rmtree(removal.path)
        return
    removal.path.write_text(removal.after or "", encoding="utf-8")


def _future_detail(detail: str) -> str:
    """Item 41: dry-run lines speak in the future tense ("Removed …" reads
    as done; a dry run must say "would remove …")."""
    for past, future in (("Removed ", "would remove "),
                         ("SKIPPED ", "would skip: ")):
        if detail.startswith(past):
            return future + detail[len(past):]
    return detail


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
        print_fn(f"  [dry-run] {mark}: {_future_detail(removal.detail)}")
        if removal.after is not None:
            print_fn("      after:")
            for line in removal.after.rstrip("\n").splitlines() or [""]:
                print_fn(f"        {line}")
        elif removal.action == "delete-file":
            print_fn("      (file would be removed)")
        elif removal.action == "delete-dir":
            print_fn("      (folder would be removed)")
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

def run(*, home: Path | None = None, reverie_home: Path | None = None,
        delete_data: bool = False, no_export: bool = False, yes: bool = False,
        dry_run: bool = False, export_dir: Path | None = None,
        print_fn=print, input_fn=input) -> int:
    """`reverie uninstall`. Returns 0 on success (nothing fatal), 1 otherwise.

    The FULL plan is always shown first — the harness entries to remove and
    whether data will be exported and deleted — and ONE confirmation covers
    the whole plan (``--yes`` skips it). Nothing is touched before the
    confirmation; aborting leaves everything unchanged. The export-before-
    delete rule holds regardless of the flag: with ``--delete-data`` a final
    export is written outside ``~/.reverie`` first (``--no-export`` skips
    it) and a failed export aborts the whole uninstall."""
    home = Path(home) if home else Path.home()
    reverie_home = Path(reverie_home) if reverie_home else _default_reverie_home()

    print_fn("\nReverie — Uninstall\n")
    if dry_run:
        print_fn("(dry run — nothing will be changed; the plan below is "
                 "printed once, in future tense)\n")

    # -- 1. the plan, computed pure and printed BEFORE anything changes ---
    harness_plans = _plan_harnesses(home, reverie_home,
                                    load_install_manifest(reverie_home))
    export_path: Path | None = None
    will_delete = delete_data and reverie_home.exists()
    if delete_data and reverie_home.exists():
        target_base = Path(export_dir) if export_dir else Path.cwd()
        export_path = target_base / (
            "reverie-final-export-"
            + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            + ".zip")

    if dry_run:
        # Item 41: ONE pass over the plan, all future tense; no separate
        # summary block (that printed every line twice), no walk-through.
        print_fn("Would remove:")
        for agent_name, removal in harness_plans:
            _emit(removal, home, dry_run=True, print_fn=print_fn,
                  mark=agent_name)
        memory = _display(reverie_home, home)
        if delete_data:
            if not reverie_home.exists():
                print_fn(f"  · {memory}: not present — nothing to remove")
            elif no_export:
                print_fn(f"  · Your memory ({memory}): would be REMOVED "
                         "(no final export — --no-export)")
            else:
                print_fn(f"  · Your memory ({memory}): would be REMOVED "
                         f"after a final export to {export_path}")
        else:
            print_fn(f"  · Your memory ({memory}): would be KEPT (default) "
                     "— a reinstall picks up where you left off. Pass "
                     "--delete-data to remove it (a final export is taken "
                     "first).")
        if will_delete and not no_export:
            print_fn(f"\n[dry-run] would write a final export to "
                     f"{export_path}")
        print_fn("\nUninstall report:")
        print_fn("  Dry run — nothing was removed.")
        if not delete_data:
            print_fn(f"  Kept: {memory} (your memory)")
        print_fn("\nLast step, run by hand:")
        print_fn(f"  {package_removal_command()}")
        print_fn(f"  (Reverie's published package name is {DIST_NAME}; the "
                 "running package cannot remove itself)")
        return 0

    print_fn("The plan:")
    for agent_name, removal in harness_plans:
        print_fn(f"  - {agent_name}: {removal.detail}"
                 if removal.action != "unchanged"
                 else f"  - {agent_name}: {removal.detail}")
    if delete_data:
        if not reverie_home.exists():
            print_fn(f"  - {_display(reverie_home, home)}: not present — "
                     "nothing to remove")
        else:
            print_fn(f"  - {_display(reverie_home, home)}: "
                     + ("REMOVED after a final export to "
                        f"{export_path}" if not no_export else
                        "REMOVED (no final export — --no-export)"))
    else:
        print_fn(f"  - {_display(reverie_home, home)}: KEPT (default) — "
                 "a reinstall picks up where you left off. Pass "
                 "--delete-data to remove it (a final export is taken "
                 "first).")

    # -- 2. ONE confirmation covers the whole plan -------------------------
    if not dry_run and not yes:
        try:
            answer = input_fn("\nProceed with this plan? (y/n): ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print_fn("\nAborted — nothing was changed.")
            return 1

    # -- 3. the export happens FIRST: a failed export aborts the whole
    #       uninstall so nothing is removed without a backup ---------------
    if will_delete and not no_export:
        try:
            brain = Brain(reverie_home / "reverie.db")
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

    # -- 4. harness entries (merge logic in reverse) -----------------------
    print_fn("\nRemoving Reverie from your coding agents...")
    for agent_name, removal in harness_plans:
        _emit(removal, home, dry_run=False, print_fn=print_fn,
              mark=agent_name)

    # -- 5. your memory (~/.reverie) ---------------------------------------
    print_fn(f"\nYour memory ({_display(reverie_home, home)})...")
    if not reverie_home.exists():
        print_fn("  · not present — nothing to remove")
    elif not delete_data:
        print_fn("  · KEPT (default) — a reinstall picks up where you "
                 "left off. Run `reverie uninstall --delete-data` to remove "
                 "it (a final export is taken first).")
    else:
        shutil.rmtree(reverie_home)
        print_fn(f"  ✓ Removed {_display(reverie_home, home)}")

    # -- 6. report ---------------------------------------------------------
    print_fn("\nUninstall report:")
    print_fn("  Removed: Reverie's MCP entries, instruction references "
             "and command files from the agents above.")
    if not delete_data:
        print_fn(f"  Kept: {_display(reverie_home, home)} (your memory)")
    else:
        print_fn(f"  Removed: {_display(reverie_home, home)} (your memory)")
    if delete_data and export_path is not None and not no_export:
        print_fn(f"  Final export: {export_path}")
    print_fn("\nLast step, run by hand:")
    print_fn(f"  {package_removal_command()}")
    print_fn(f"  (Reverie's published package name is {DIST_NAME}; the "
             "running package cannot remove itself)")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="reverie uninstall",
        description="Remove exactly what `reverie install` did, and nothing "
                    "more: Reverie's MCP entries, instruction references "
                    "and command files in every supported agent. The full "
                    "plan is shown first; one confirmation covers it. Your "
                    "memory (~/.reverie) is kept unless --delete-data is "
                    "given.",
    )
    parser.add_argument("--delete-data", action="store_true",
                        help="also remove ~/.reverie (a final export is "
                             "written first)")
    parser.add_argument("--no-export", action="store_true",
                        help="with --delete-data: skip the final export "
                             "(the confirmation still covers the deletion)")
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
