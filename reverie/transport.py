"""Brain transport (feat/flow, D58) — export, import, reset.

A Reverie export is a single ZIP with two members:

- ``brain.db``   — a consistent snapshot of the whole global brain
                   (``VACUUM INTO`` on the open connection), carrying the
                   repos table (enablement states + remote URLs) with it.
- ``manifest.json`` — Reverie version, schema version (``PRAGMA
  user_version``), embedding model + dimensions, row counts, export date.

Import rules:
- allowed directly when the local brain is empty;
- when it isn't, ``--replace`` is required — an automatic backup export is
  taken first and its path is printed;
- **merging two brains is not supported** (embedding spaces, ECU ids and
  edge graphs are not reconcilable without re-diffusing everything) —
  the help text says so plainly;
- refused when the export's embedding model or dimensions differ from the
  local configuration, or when its schema is NEWER than this Reverie.

Machine-specific data (D58): repo paths are absolute and differ between
machines. Strategy: repo *states* travel by remote URL — the repos table
records each repo's origin URL, and ``reverie/repos.resolve_repo_state``
re-matches a never-seen path against exactly one known URL on first use
(ambiguous or URL-less repos start fresh and are resolved by ``ec
enable``). Sessions are tied to worktree paths on purpose (§28.5): rows
pointing at paths that don't exist on this machine stay inert and are
archived/ignored; the canonical memory is what meaningfully migrates.

``reverie reset`` always takes an automatic backup export first — even with
``--yes`` — and prints its path. ``--repo`` limits the wipe to one repo's
sessions + candidates (long-term memory and the enablement row stay).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, repos
from .brain import Brain, BrainError
from .config import SCHEMA_VERSION, reverie_home, get_config

#: Export format revision (manifest["format"]); bump on breaking changes.
EXPORT_FORMAT = 1

MANIFEST_NAME = "manifest.json"
BRAIN_NAME = "brain.db"


class TransportError(RuntimeError):
    """Export/import failure with an actionable message."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_export_path(prefix: str = "reverie-export") -> Path:
    ts = _utcnow().strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / f"{prefix}-{ts}.zip"


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def export_brain(brain: Brain, file: str | Path | None = None,
                 config=None) -> dict:
    """Write a consistent snapshot + manifest as one ZIP; returns the
    manifest. Safe while other processes hold the DB open (WAL)."""
    cfg = config or get_config()
    target = Path(file) if file else default_export_path()
    if target.exists():
        raise TransportError(
            f"{target} already exists — choose another file name.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / BRAIN_NAME
        brain._conn.execute("VACUUM INTO ?", (str(snapshot),))
        manifest = {
            "format": EXPORT_FORMAT,
            "reverie_version": __version__,
            "schema_version": int(
                brain._conn.execute("PRAGMA user_version").fetchone()[0]),
            "embedding_model": cfg.embedding.model,
            "embedding_dimensions": cfg.embedding.dimensions,
            "exported_at": _utcnow().isoformat(),
            "counts": {
                "ecus": brain.count_ecus(),
                "edges": len(brain.list_all_edges()),
                "sessions": len(brain.list_sessions()),
                "session_ecus": sum(
                    brain.count_session_ecus(s["id"])
                    for s in brain.list_sessions()),
                "repos": len(brain.list_repos()),
            },
        }
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, BRAIN_NAME)
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
    return manifest


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def _brain_is_empty(brain: Brain) -> bool:
    return brain.count_ecus() == 0 and not brain.list_sessions() \
        and not brain.list_repos()


def _check_versions(manifest: dict, cfg) -> None:
    mine_model = cfg.embedding.model
    mine_dims = cfg.embedding.dimensions
    if manifest.get("embedding_model") != mine_model:
        raise TransportError(
            f"Cannot import: this export uses embedding model "
            f"\"{manifest.get('embedding_model')}\" but this Reverie is "
            f"configured for \"{mine_model}\". Embeddings from different "
            "models are not comparable — import the export with the "
            "Reverie version that produced it, or reconfigure "
            "~/.reverie/config.yaml to match.")
    if int(manifest.get("embedding_dimensions") or 0) != mine_dims:
        raise TransportError(
            f"Cannot import: this export's embedding dimensions "
            f"({manifest.get('embedding_dimensions')}) do not match this "
            f"Reverie's ({mine_dims}).")
    exported_schema = int(manifest.get("schema_version") or 0)
    if exported_schema > SCHEMA_VERSION:
        raise TransportError(
            f"Cannot import: the export's schema (v{exported_schema}) is "
            f"newer than this Reverie (v{SCHEMA_VERSION}). Upgrade Reverie "
            "first, then import.")


def import_brain(
    brain: Brain,
    file: str | Path,
    *,
    replace: bool = False,
    config=None,
    print_fn=print,
) -> dict:
    """Replace the local brain with an export's contents.

    Direct when the local brain is empty; otherwise ``replace=True``
    (``--replace``) is required and an automatic backup is taken first.
    Returns ``{"manifest", "counts", "backup"}``."""
    cfg = config or get_config()
    path = Path(file).expanduser()
    if not path.exists():
        raise TransportError(f"No such file: {path}")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                if MANIFEST_NAME not in names or BRAIN_NAME not in names:
                    raise TransportError(
                        f"{path} is not a Reverie export (expected "
                        f"{BRAIN_NAME} + {MANIFEST_NAME} inside).")
                zf.extract(MANIFEST_NAME, tmp)
                zf.extract(BRAIN_NAME, tmp)
        except zipfile.BadZipFile as exc:
            raise TransportError(
                f"{path} is not a readable ZIP export ({exc}).") from exc
        manifest = json.loads((Path(tmp) / MANIFEST_NAME).read_text())
        _check_versions(manifest, cfg)

        empty = _brain_is_empty(brain)
        backup = None
        if not empty:
            if not replace:
                raise TransportError(
                    "The local brain is not empty ("
                    f"{brain.count_ecus()} ECUs). Import would replace it — "
                    "merging two brains is not supported. Re-run with "
                    "`--replace` (an automatic backup is taken first), or "
                    "import into an empty brain.")
            backup_path = reverie_home() / "backups" / (
                "pre-import-" + _utcnow().strftime("%Y%m%d-%H%M%S") + ".zip")
            export_brain(brain, backup_path, config=cfg)
            backup = str(backup_path)
            print_fn(f"Automatic backup written to {backup}")

        # (feat/flow review fix) upgrade the extracted snapshot through the
        # normal Brain schema/migration path BEFORE attaching: an export
        # made by an older Reverie (its schema may lack later tables or
        # columns) is brought up to the current schema in place.
        snapshot = Path(tmp) / BRAIN_NAME
        try:
            snapshot_brain = Brain(snapshot)
            snapshot_brain.close()
        except BrainError as exc:
            raise TransportError(
                f"The export's database could not be read: {exc}") from exc

        # (feat/flow review fix) the delete and the insert run in ONE
        # transaction — a failure anywhere rolls the whole import back and
        # the local brain is left exactly as it was.
        brain._conn.execute("ATTACH ? AS imported", (str(snapshot),))
        try:
            try:
                counts = brain.replace_all_from_attached("imported")
            except (BrainError, sqlite3.Error) as exc:
                raise TransportError(
                    "Import failed — the local brain was left exactly as "
                    f"it was. ({exc})") from exc
        finally:
            brain._conn.execute("DETACH imported")
    skipped = counts.pop("skipped_tables", [])
    if skipped:
        print_fn("Skipped — these tables are absent from the export: "
                 + ", ".join(skipped))
    print_fn(
        "Imported: {ecus} ECUs, {sessions} sessions, {repos} repo "
        "state(s).".format(ecus=counts["ecus"],
                           sessions=counts["sessions"],
                           repos=counts["repos"]))
    print_fn(
        "Note: repo paths in the export are absolute paths from the "
        "exporting machine. Repos are re-matched by git remote URL on "
        "first use; when ambiguous (or URL-less), run `reverie enable` inside "
        "the repo. Sessions tied to paths that don't exist on this "
        "machine stay inert.")
    return {"manifest": manifest, "counts": counts, "backup": backup}


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

def reset_brain(brain: Brain, *, repo: str | None = None, config=None,
                print_fn=print) -> str:
    """Wipe data (whole brain, or one repo's sessions + candidates) —
    ALWAYS after taking an automatic backup export. Returns the backup
    path."""
    cfg = config or get_config()
    backup_path = reverie_home() / "backups" / (
        "pre-reset-" + _utcnow().strftime("%Y%m%d-%H%M%S") + ".zip")
    export_brain(brain, backup_path, config=cfg)
    print_fn(f"Automatic backup written to {backup_path}")
    if repo:
        # Sessions are keyed by worktree path while repo identity is the
        # git common dir (§29.2/D58) — resolve the argument to its identity
        # and wipe every worktree belonging to it (feat/flow review fix 7).
        repo_arg = str(Path(repo).expanduser().resolve())
        worktrees = repos.worktree_paths(repo_arg)
        counts = brain.delete_repo_data(worktrees)
        print_fn(
            f"Reset repo {repo} (repo identity: "
            f"{repos.common_dir(repo_arg)}): {counts['sessions']} "
            f"session(s), {counts['session_ecus']} candidate(s), "
            f"{counts['pending_extractions']} pending extraction(s) "
            "removed. Long-term memory and the repo's enablement state "
            "were kept.")
        print_fn("Included worktree paths: " + ", ".join(worktrees))
    else:
        brain.clear_all_data()
        print_fn("Reverie brain reset — all sessions, candidates and "
                 "long-term memory removed.")
    return str(backup_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main_export(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ec export",
        description="Export the whole Reverie brain (consistent snapshot + "
                    "manifest) as one ZIP file.",
    )
    parser.add_argument("file", nargs="?",
                        help="output ZIP path "
                             "(default: ./reverie-export-<date>.zip)")
    args = parser.parse_args(argv)
    try:
        brain = Brain()
    except BrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        manifest = export_brain(brain, args.file)
        target = args.file or default_export_path()
        print(f"Exported {manifest['counts']['ecus']} ECUs to {target}")
        return 0
    except TransportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        brain.close()


def main_import(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ec import",
        description="Import a Reverie export. Allowed directly when the "
                    "local brain is empty; otherwise pass --replace (an "
                    "automatic backup is taken first). Merging two brains "
                    "is not supported.",
    )
    parser.add_argument("file", help="export ZIP path")
    parser.add_argument("--replace", action="store_true",
                        help="replace the non-empty local brain (automatic "
                             "backup taken first); merging is not supported")
    args = parser.parse_args(argv)
    try:
        brain = Brain()
    except BrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        import_brain(brain, args.file, replace=args.replace)
        return 0
    except TransportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        brain.close()


def main_reset(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ec reset",
        description="Reset the Reverie brain. Always takes an automatic "
                    "backup export first and prints its path.",
    )
    parser.add_argument("--repo", default=None, metavar="PATH",
                        help="reset only this repo's sessions and "
                             "candidates; long-term memory is kept")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt (the automatic "
                             "backup is still taken)")
    args = parser.parse_args(argv)

    try:
        brain = Brain()
    except BrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        if not args.yes:
            scope = f"the repo {args.repo}" if args.repo \
                else "the ENTIRE Reverie brain (all repos, all memory)"
            answer = input(
                f"This will permanently reset {scope}. "
                "An automatic backup will be taken first.\n"
                "Type 'reset' to confirm: ").strip()
            if answer != "reset":
                print("Aborted — nothing was changed.")
                return 1
        reset_brain(brain, repo=args.repo)
        return 0
    except TransportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        brain.close()
