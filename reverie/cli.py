"""``reverie`` — the command line.

One entry point, subcommands:

    reverie install          configure agents + ~/.reverie
    reverie enable           turn Reverie on for the current repo
    reverie disable          turn Reverie off for the current repo (fully
                             off: no capture, no retrieval — data is kept)
    reverie repos            list every known repo with its state and counts
    reverie review           review pending candidates (terminal UI)
    reverie status           session + brain status
    reverie export           export the whole brain to a ZIP
    reverie import           import a brain export (--replace for non-empty)
    reverie reset            wipe the brain (or one repo), with auto-backup
    reverie retry            retry failed extractions / re-diffuse conclusions
    reverie repair           brain integrity check + safe recovery
    reverie hook             lifecycle hooks invoked by the agent harness
                             itself (installed by `reverie install`)
    reverie capture-turn     capture the completed exchange (Stop hook)
    reverie uninstall        remove Reverie's harness entries (+ data with
                             --delete-data)

Bare ``reverie`` prints this help; a mistyped first word is an error with a
"did you mean" hint. There are no other console scripts: the MCP server is
``reverie-mcp``.
"""

from __future__ import annotations

import argparse
import sys

from . import repos, transport
from .brain import Brain, BrainError


def _brain_or_die():
    try:
        return Brain()
    except BrainError as exc:
        print(str(exc), file=sys.stderr)
        return None


def _resolve_or_die(path: str | None) -> repos.RepoInfo | None:
    """Resolve the repo at ``path`` (default: cwd); None with a printed
    error when it isn't a git repository."""
    try:
        return repos.resolve_repo(cwd=path)
    except Exception as exc:
        print(f"Not a git repository: {exc}", file=sys.stderr)
        return None


def _cmd_enable(args) -> int:
    info = _resolve_or_die(args.path)
    if info is None:
        return 1
    brain = _brain_or_die()
    if brain is None:
        return 1
    try:
        repos.set_repo_state(brain, info, "enabled")
        print(f"Reverie is on for {info.repo_path} (branch {info.branch}).")
        print("Sessions are automatic per branch — just code. Review "
              "candidates with `reverie review` or through your agent.")
        return 0
    finally:
        brain.close()


def _cmd_disable(args) -> int:
    info = _resolve_or_die(args.path)
    if info is None:
        return 1
    brain = _brain_or_die()
    if brain is None:
        return 1
    try:
        repos.set_repo_state(brain, info, "disabled")
        print(f"Reverie is off for {info.repo_path} (branch {info.branch}).")
        print("Fully off: nothing is captured, retrieved, or injected "
              "here. Existing memory is kept — re-enable any time with "
              "`reverie enable` and everything is intact.")
        return 0
    finally:
        brain.close()


def _strip_git(repo_key: str) -> str:
    return repo_key[: -len("/.git")] if repo_key.endswith("/.git") \
        else repo_key


def _cmd_repos(_args) -> int:
    brain = _brain_or_die()
    if brain is None:
        return 1
    try:
        rows = brain.list_repos()
        if not rows:
            print("No repos known yet — run `reverie enable` inside a repo, "
                  "or just use Reverie from your agent once.")
            return 0
        print(f"{len(rows)} known repo(s):")
        for row in rows:
            path = row["path"] or _strip_git(row["repo_key"])
            sessions = brain.list_sessions(repo_path=path)
            active = sum(1 for s in sessions if s["status"] == "active")
            candidates = len(brain.list_queue_candidates(
                repo_path=path, statuses=("pending", "skipped")))
            conclusions = len(brain.list_ecus_for_repo(path))
            print(f"  {path}")
            print(f"    state: {row['state']} | active sessions: {active} "
                  f"| candidates: {candidates} | conclusions grounded "
                  f"here: {conclusions}")
            if row["url"]:
                print(f"    remote: {row['url']}")
            if row["state"] == "undecided" and row["asked_at"]:
                print("    (asked once — still undecided)")
        return 0
    finally:
        brain.close()


def _cmd_install(args) -> int:
    from . import install
    try:
        return install.run(
            all_agents=getattr(args, "all", False),
            only=[k.strip() for k in args.only.split(",") if k.strip()]
            if getattr(args, "only", None) else None,
            dry_run=getattr(args, "dry_run", False),
            provider=getattr(args, "provider", None),
            no_verify=getattr(args, "no_verify", False),
            import_file=getattr(args, "import_file", None),
        )
    except install.InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reverie",
        description="Reverie — engineering memory for coding agents.",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("enable", help="turn Reverie on for a repo")
    p.add_argument("path", nargs="?", default=None,
                   help="repo path (default: current directory)")
    p = sub.add_parser("disable", help="turn Reverie off for a repo")
    p.add_argument("path", nargs="?", default=None,
                   help="repo path (default: current directory)")
    sub.add_parser("repos", help="list known repos with states and counts")
    sub.add_parser("review", help="review pending candidates (terminal UI)")

    p = sub.add_parser("status", help="session + brain status")
    p.add_argument("--repo", default=None,
                   help="repo path (default: git detect)")
    p.add_argument("--branch", default=None,
                   help="branch (default: git detect)")
    p.add_argument("--all", action="store_true",
                   help="every enabled repo's active sessions")

    p = sub.add_parser("export", help="export the brain to one ZIP file")
    p.add_argument("file", nargs="?",
                   help="output ZIP path (default: ./reverie-export-<date>.zip)")

    p = sub.add_parser("import", help="import a brain export")
    p.add_argument("file", help="export ZIP path")
    p.add_argument("--replace", action="store_true",
                   help="replace the non-empty local brain (automatic "
                        "backup taken first). Merging two brains is not "
                        "supported.")

    p = sub.add_parser("reset", help="wipe the brain (automatic backup first)")
    p.add_argument("--repo", default=None, metavar="PATH",
                   help="reset only this repo's sessions and candidates; "
                        "long-term memory is kept")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt (the automatic "
                        "backup is still taken)")

    p = sub.add_parser("install", help="configure agents + ~/.reverie")
    p.add_argument("--import", dest="import_file", metavar="FILE",
                   default=None, help="import a brain export during setup "
                   "on a new machine")
    p.add_argument("--all", action="store_true")
    p.add_argument("--only", default=None)
    p.add_argument("--provider", default=None)
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("uninstall", help="remove what `reverie install` did "
                       "(your memory ~/.reverie is kept unless --delete-data)")
    p.add_argument("--delete-data", action="store_true",
                   help="also remove ~/.reverie — a final export is written "
                        "first and you must confirm")
    p.add_argument("--no-export", action="store_true",
                   help="with --delete-data: skip the final export "
                        "(confirmation still required)")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt (the export still "
                        "happens unless --no-export)")
    p.add_argument("--dry-run", action="store_true",
                   help="print every planned removal with the exact "
                        "before/after; change nothing")

    p = sub.add_parser("retry", help="retry failed extractions / re-diffuse")
    p.add_argument("--list", action="store_true",
                   help="list pending extractions and exit")
    p.add_argument("--id", metavar="ITEM_ID", default=None,
                   help="retry only this pending extraction id")
    p.add_argument("--drop", metavar="ITEM_ID", default=None,
                   help="discard this pending extraction without retrying")
    p.add_argument("--rediffuse", action="store_true",
                   help="re-run diffusion for undiffused conclusions instead of "
                        "retrying pending extractions")
    p.add_argument("--ecu-id", metavar="ECU_ID", default=None,
                   help="with --rediffuse: only this conclusion id")
    p.add_argument("--repo", default=None,
                   help="only items for this repo path (default: all)")
    p.add_argument("--branch", default=None,
                   help="only items for this branch (default: all)")

    p = sub.add_parser("repair", help="check brain integrity; safe recovery")
    p.add_argument("--dry-run", action="store_true",
                   help="report problems without applying any fixes")
    p.add_argument("--db", default=None,
                   help="brain path (default: $REVERIE_HOME/reverie.db)")

    p = sub.add_parser("hook", help="agent lifecycle hooks (invoked by the "
                       "harness itself, installed by `reverie install`)")
    hook_sub = p.add_subparsers(dest="hook")
    hs = hook_sub.add_parser("session-start", help="print the session-start "
                             "context block: repo state only "
                             "(read-only; quiet on error; always exit 0)")
    hs.add_argument("--repo", default=None,
                    help="workspace path (default: current directory)")
    sub.add_parser("capture-turn", help="capture the completed exchange "
                   "(invoked by the installed hook; reads hook JSON on "
                   "stdin)")
    return parser


#: Every first-class subcommand (order shown by `reverie --help`).
SUBCOMMANDS = ("enable", "disable", "repos", "review", "status", "export",
               "import", "reset", "uninstall", "retry", "repair", "hook",
               "capture-turn", "install")


def _unknown_command(word: str) -> int:
    """A mistyped first word is an error with a 'did you mean' hint."""
    import difflib
    close = difflib.get_close_matches(word, SUBCOMMANDS, n=1)
    hint = (f"Did you mean 'reverie {close[0]}'?" if close
            else "Run `reverie --help` for the command list.")
    print(f"Unknown command: {word!r}. {hint}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("--help", "-h"):
        _build_parser().print_help()    # the multi-command help
        return 0
    if argv[0] not in SUBCOMMANDS:
        return _unknown_command(argv[0])
    # review parses its own flags; the generic pass would reject them
    if argv[0] == "review":
        from . import review_cli
        return review_cli.main(argv[1:])
    if argv[0] == "status":
        from . import session as session_mod
        return session_mod.main(["status", *argv[1:]])
    if argv[0] == "uninstall":
        from . import uninstall
        return uninstall.main(argv[1:])
    if argv[0] == "retry":
        from . import pending
        return pending.main(argv[1:])
    if argv[0] == "repair":
        from . import repair
        return repair.main(argv[1:])
    # hook parses its own nested subcommand (session-start)
    if argv[0] == "hook":
        from . import hooks
        return hooks.main(argv[1:])
    if argv[0] == "capture-turn":
        from . import integrations
        return integrations.capture_turn_main(argv[1:])
    args = _build_parser().parse_args(argv)

    if args.command == "enable":
        return _cmd_enable(args)
    if args.command == "disable":
        return _cmd_disable(args)
    if args.command == "repos":
        return _cmd_repos(args)
    if args.command == "export":
        return transport.main_export(argv[1:])
    if args.command == "import":
        return transport.main_import(argv[1:])
    if args.command == "reset":
        return transport.main_reset(argv[1:])
    return _cmd_install(args)


if __name__ == "__main__":
    sys.exit(main())
