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
from .client import set_client


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


def _cmd_rules(args) -> int:
    from . import rules
    cwd = args.repo or "."
    if args.action == "add":
        text = " ".join(args.text)
        try:
            path = rules.add(text, level="user" if args.user else "repo",
                             cwd=cwd)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Added to {path}")
        return 0
    loaded = rules.load(cwd)
    for label, key, path in (("personal", "user", rules.personal_path()),
                             ("repo", "repo", rules.repo_path(cwd))):
        items = rules.bullets(loaded[key])
        print(f"{label} ({path}): {len(items)} rule(s)")
        for item in items:
            print(f"  - {item}")
    return 0


def _cmd_context(args) -> int:
    from . import context, spool
    root = spool.repo_root(args.repo or ".")
    if args.action in ("add", "resolve"):
        text = " ".join(args.text[1:] if args.action == "resolve" else args.text)
        paths = tuple(x.strip() for x in (args.paths or "").split(",") if x.strip())
        try:
            added = context.add(root, text, layer=args.layer, rule=not args.guidance,
                              locked=args.locked, tag=args.tag or "", doc=args.doc or "",
                              scope=args.scope or "", paths=paths, why=args.why or "",
                              resolves=args.text[0] if args.action == "resolve" else "")
        except (ValueError, IndexError) as exc:
            print(str(exc) or "usage: reverie context resolve <conflict-key> <decision>",
                  file=sys.stderr)
            return 2
        if added.proposal:
            print(f"Written as a proposal: {added.path}\n"
                  f"The owners ({', '.join(added.owners)}) review it like any pull request; "
                  f"an owner runs `reverie context accept <name>`.")
        else:
            print(f"Added to {added.path}")
        return 0
    if args.action == "proposals":
        found = context.proposals(root)
        if not found:
            print("No open proposals.")
        for p in found:
            print(f"{p['slug']}: {p['bullet']}  (by {p.get('proposed-by', '?')}, "
                  f"for {p.get('document', '?')})")
        return 0
    if args.action == "accept":
        try:
            doc = context.accept(root, args.text[0] if args.text else "")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Accepted into {doc}")
        return 0
    if args.action == "check":
        from . import context_check
        cfg, judge = context_check._cfg_judge()
        conflicts = context_check.find_conflicts(root, judge)
        flags = context_check.check_stale(root, judge, since=args.since,
                                          diff_chars=int(cfg.stale.diff_chars))
        print(f"{len(conflicts)} conflict(s) between scopes, {len(flags)} newly outdated.")
        for f in flags:
            print(f"  [{f['id']}] {f['file']}: contradicted since commit "
                  f"{f['commit'][:8]} ({f['reason']})")
        return 0
    res = context.resolve(root)
    if not res.items:
        print("No team context applies here. Put markdown in .reverie/context/ "
              "(see docs/CLI.md, `reverie context`).")
        return 0
    lost = {o.id for c in res.conflicts for o in c.overridden}
    for layer in context.LAYERS:
        mine = [i for i in res.items if i.layer == layer]
        if not mine:
            continue
        print(f"{layer} ({len({i.file for i in mine})} document(s)):")
        for it in mine:
            marks = [f for f, on in (("locked", it.locked), ("conflict", it.id in lost),
                                     ("possibly outdated", it.id in res.stale)) if on]
            tail = f"  [{', '.join(marks)}]" if marks else ""
            where = f" (only under {', '.join(it.paths)})" if it.paths else ""
            who = f" <{it.scope}>" if layer in ("org", "team") else ""
            print(f"  [{it.id}]{who} {it.kind}{where}: {it.text}{tail}")
    for c in res.conflicts:
        names = " vs ".join(f"[{x.label}] {x.id}" for x in c.items)
        print(f"conflict {c.key} ({c.status}): {names}" + (f" - {c.reason}" if c.reason else ""))
    return 0


def _cmd_stale(args) -> int:
    from . import deliver, items, spool, staleness
    from .config import get_config
    root = spool.repo_root(args.repo or ".")
    if not deliver.db_path().exists():
        print("No Reverie memory yet.", file=sys.stderr)
        return 1
    conn = items.connect(deliver.db_path())
    try:
        key = deliver.repo_key(root)
        if args.action == "check":
            cfg = get_config()
            if args.tests:
                cfg.stale["run_tests"] = True
            rep = staleness.check(conn, root, cfg, staleness.default_judge(cfg),
                                  since=args.since)
            print(f"Checked {rep.commits} commit(s); {rep.triggered} memory "
                  f"recheck(s) of {rep.possible} possible; tests: {rep.tests}.")
            for f in rep.flags:
                print(f"  STALE [{f.memory_id[:8]}] after {f.commit[:8]} "
                      f"({f.message}): {f.reason}")
                print(f"    candidate [{(f.candidate_id or '')[:8]}]: "
                      f"reverie stale accept {(f.candidate_id or '')[:8]}  |  "
                      f"reverie stale keep {f.memory_id[:8]}")
            return 0
        if args.action == "list":
            cands = staleness.pending(conn, key)
            for c in cands:
                print(f"[{c['id'][:8]}] replaces [{c['meta'].get('candidate_for','')[:8]}]: "
                      f"{c['text']}\n    why: {c['meta'].get('reason', '')}")
            if not cands:
                print("No pending staleness candidates.")
            return 0
        if not args.id:
            print("an id is required", file=sys.stderr)
            return 2
        fn = staleness.accept if args.action == "accept" else staleness.keep
        print(fn(conn, args.id))
        return 0
    finally:
        conn.close()


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
        return 0
    finally:
        brain.close()


def _cmd_install(args) -> int:
    from . import install
    if getattr(args, "list_profiles", False):
        from . import profiles
        for key, plist in profiles.discover().items():
            print(f"{key}:" + ("" if plist else "  (none found)"))
            for p in plist:
                print(f"  {p.label()}")
        return 0
    try:
        return install.run(
            profile_args=getattr(args, "profile", None),
            trust_codex_hooks=getattr(args, "trust_codex_hooks", True),
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
    parser.add_argument("--client", default=None,
                        help="client key that launched this command (recorded "
                             "for logs; no behaviour change)")
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
    p.add_argument("--profile", action="append", default=None, metavar="KEY=PATH",
                   help="install into this config directory of a harness that can have "
                        "several (claude_code=PATH, codex=PATH); repeatable")
    p.add_argument("--trust-codex-hooks", action=argparse.BooleanOptionalAction, default=True,
                   help="approve Reverie's own Codex hooks through Codex's API, as /hooks "
                        "would (default); --no-trust-codex-hooks leaves that to you")
    p.add_argument("--list-profiles", action="store_true",
                   help="list every config directory found for each agent and exit")
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
    p = sub.add_parser("transcripts", help="import new prompts/answers from the "
                       "harnesses' own transcripts (Claude, Codex, opencode, "
                       "Cursor) into the redacted prompt log")
    p.add_argument("--repo", default=None,
                   help="repo path (default: current directory)")
    p = sub.add_parser("resume", help="print rules + the verbatim prompt log "
                       "+ git state, to continue a cut-off session in any "
                       "harness")
    p.add_argument("--repo", default=None,
                   help="repo path (default: current directory)")
    p.add_argument("--session", default=None, help="a specific session id "
                   "(default: the most recent one)")
    p.add_argument("--all", action="store_true", dest="all_sessions",
                   help="every recorded prompt, all sessions")
    p.add_argument("--latest", action="store_true",
                   help="the most recent session, without asking which")
    p.add_argument("--prompt", type=int, default=None, metavar="N",
                   help="print prompt N of the session in full (the resume card "
                        "trims long prompts)")
    p = sub.add_parser("undo", help="undo a remembered item (reversible)")
    p.add_argument("id")
    p.add_argument("--restore", action="store_true",
                   help="bring an undone item back")
    p = sub.add_parser("digest", help="what Reverie remembered since you "
                       "last looked")
    p.add_argument("--repo", default=None,
                   help="repo path (default: current directory)")
    p.add_argument("--all", action="store_true", dest="all_repos",
                   help="every repo, not just this one")
    p.add_argument("--no-mark", action="store_true",
                   help="do not mark everything as seen")
    p = sub.add_parser("usage", help="which memory items agents actually used, ignored "
                       "or contradicted")
    p.add_argument("--repo", default=None,
                   help="repo path (default: current directory)")
    p = sub.add_parser("merge", help="merge near-duplicate memory items "
                       "(periodic; also run lazily at session start)")
    p.add_argument("--repo", default=None,
                   help="repo path (default: current directory)")
    p = sub.add_parser("stale", help="keep memory true as the code changes")
    p.add_argument("action", choices=["check", "list", "accept", "keep"])
    p.add_argument("id", nargs="?", default=None,
                   help="accept: candidate id; keep: memory id")
    p.add_argument("--repo", default=None,
                   help="repo path (default: current directory)")
    p.add_argument("--scope", default=None,
                   help="which org/team folder to write to when several are configured (add)")
    p.add_argument("--paths", default=None,
                   help="comma-separated globs: the rule applies only under these paths (add)")
    p.add_argument("--why", default=None,
                   help="why, written into the proposal for the owners (add)")
    p.add_argument("--since", default=None, metavar="REV",
                   help="check: recheck commits after REV instead of the "
                        "last checked commit")
    p.add_argument("--tests", action="store_true",
                   help="check: also run the project's tests first and give a "
                        "failure to the judge as extra evidence (off by default)")
    p = sub.add_parser("promote", help="make a memory item apply in every "
                       "repo (always/never), or take it back")
    p.add_argument("id")
    p.add_argument("--demote", action="store_true",
                   help="return a promoted item to the repo it came from")
    p = sub.add_parser("show", help="print one memory item by id (prefix ok)")
    p.add_argument("id")
    p.add_argument("--repo", default=None,
                   help="repo path (default: current directory)")
    p = sub.add_parser("rules", help="your always-on rules (markdown files)")
    p.add_argument("action", choices=["add", "list"])
    p.add_argument("text", nargs="*", help="rule text (add)")
    p.add_argument("--user", action="store_true",
                   help="personal rule (~/.reverie/rules.md, every repo); "
                        "default is this repo's .reverie/rules.md")
    p.add_argument("--repo", default=None,
                   help="repo path (default: current directory)")
    p = sub.add_parser("context", help="layered team context (org > team > repo "
                       "> personal), plain markdown in the repo")
    p.add_argument("action", choices=["list", "add", "check", "proposals", "accept", "resolve"])
    p.add_argument("text", nargs="*", help="the rule or guidance text (add)")
    p.add_argument("--layer", choices=["org", "team", "repo", "personal"], default="repo",
                   help="which layer's folder to write to (add; default repo)")
    p.add_argument("--guidance", action="store_true",
                   help="add as guidance (delivered as a pointer) instead of a rule (add)")
    p.add_argument("--locked", action="store_true",
                   help="lower layers cannot override this rule (add)")
    p.add_argument("--tag", default=None,
                   help="name the rule so another layer can answer it, {id: NAME} (add)")
    p.add_argument("--since", default=None,
                   help="check: recheck commits since this revision (default: last check)")
    p.add_argument("--doc", default=None,
                   help="document file name, without .md (add; default rules or guidance)")
    p.add_argument("--repo", default=None,
                   help="repo path (default: current directory)")
    sub.add_parser("capture-turn", help="capture the completed exchange "
                   "(invoked by the installed hook; reads hook JSON on "
                   "stdin)")
    return parser


#: Every first-class subcommand (order shown by `reverie --help`).
SUBCOMMANDS = ("enable", "disable", "repos", "review", "status", "export",
               "import", "reset", "uninstall", "retry", "repair", "hook",
               "capture-turn", "install", "resume", "transcripts", "merge", "usage", "rules", "show", "undo", "digest", "stale", "promote", "context")


def _unknown_command(word: str) -> int:
    """A mistyped first word is an error with a 'did you mean' hint."""
    import difflib
    close = difflib.get_close_matches(word, SUBCOMMANDS, n=1)
    hint = (f"Did you mean 'reverie {close[0]}'?" if close
            else "Run `reverie --help` for the command list.")
    print(f"Unknown command: {word!r}. {hint}", file=sys.stderr)
    return 2


def _extract_client(argv: list[str]) -> list[str] | None:
    """Strip ``--client <key>`` from anywhere in argv and store the key.

    Accepting it at any position keeps old launch forms and new ones
    compatible; the value is recorded only (logs / future wording).
    """
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--client":
            if i + 1 >= len(argv):
                print("--client requires a value", file=sys.stderr)
                return None
            set_client(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--client="):
            set_client(arg.split("=", 1)[1])
            i += 1
            continue
        rest.append(arg)
        i += 1
    return rest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = _extract_client(argv)
    if argv is None:
        return 2
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

    if args.command == "transcripts":
        from . import importers
        r = importers.run(args.repo or ".")
        print(f"imported {r.prompts} prompt(s) and {r.answers} answer(s) from "
              f"{r.sources} changed transcript(s) {r.by_harness or ''}; "
              f"{r.queued} exchange(s) sent to extraction.")
        return 0
    if args.command == "resume":
        from . import importers, rules, spool
        cwd = args.repo or "."
        importers.lazy(cwd)
        print(spool.render_resume(cwd, session=args.session,
                                  all_sessions=args.all_sessions,
                                  latest=args.latest, full_prompt=args.prompt,
                                  rules_text=rules.render(cwd)))
        return 0
    if args.command == "rules":
        return _cmd_rules(args)
    if args.command == "context":
        return _cmd_context(args)
    if args.command == "usage":
        from . import deliver, items, spool, usage
        root = spool.repo_root(args.repo or ".")
        if not deliver.db_path().exists():
            print("No Reverie memory yet.", file=sys.stderr)
            return 1
        conn = items.connect(deliver.db_path())
        try:
            rows = usage.report(conn, deliver.repo_key(root))
        finally:
            conn.close()
        if not rows:
            print("No usage recorded yet (it is judged after each captured exchange).")
        for row in rows:
            u = row["usage"]
            tally = " ".join(f"{k.lower()}={u[k]}" for k in usage.LABELS if u.get(k))
            flag = "  <- CHALLENGED: agents keep contradicting it" if row["challenged"] else ""
            print(f"[{row['id'][:8]}] {row['kind']}: {row['text'][:80]}\n    {tally}{flag}")
        return 0
    if args.command == "merge":
        from . import deliver, items, merge, spool
        from .config import get_config
        root = spool.repo_root(args.repo or ".")
        if not deliver.db_path().exists():
            print("No Reverie memory yet.", file=sys.stderr)
            return 1
        conn = items.connect(deliver.db_path())
        try:
            from .dedupe import llm_judge
            cfg = get_config()
            rep = merge.run(conn, deliver.repo_key(root), cfg,
                            judge=llm_judge(cfg, max_tokens=200))
        finally:
            conn.close()
        for gone, keep in rep.merged:
            print(f"merged [{gone[:8]}] into [{keep[:8]}]")
        print(f"{len(rep.merged)} near-duplicate item(s) merged.")
        return 0
    if args.command == "stale":
        return _cmd_stale(args)
    if args.command == "promote":
        from . import deliver, items
        if not deliver.db_path().exists():
            print("No Reverie memory yet.", file=sys.stderr)
            return 1
        conn = items.connect(deliver.db_path())
        try:
            it = items.get(conn, args.id)
            if it is None:
                print(f"No single item matches {args.id!r}.", file=sys.stderr)
                return 1
            fn = items.demote if args.demote else items.promote
            ok = fn(conn, it["id"])
            word = "back to its repo" if args.demote else "user-wide"
            print(f"[{it['id'][:8]}] is now {word}." if ok else
                  f"[{it['id'][:8]}] was not changed (already {word}).")
            return 0
        finally:
            conn.close()
    if args.command in ("undo", "digest"):
        from . import deliver, items, spool
        from . import digest as digest_mod
        if not deliver.db_path().exists():
            print("No Reverie memory yet.", file=sys.stderr)
            return 1
        conn = items.connect(deliver.db_path())
        try:
            if args.command == "undo":
                print(digest_mod.undo(conn, args.id, args.restore))
            else:
                key = None if args.all_repos else deliver.repo_key(
                    spool.repo_root(args.repo or "."))
                print(digest_mod.digest(conn, key, mark=not args.no_mark))
        finally:
            conn.close()
        return 0
    if args.command == "show":
        from . import deliver, spool
        ro = deliver.open_db()
        if ro is None:
            from . import context as team_context
            text = team_context.show(spool.repo_root(args.repo or "."), args.id)
            if text:
                print(text)
                return 0
            print("No Reverie memory yet.", file=sys.stderr)
            return 1
        try:
            print(deliver.show(ro, args.id, spool.repo_root(args.repo or ".")))
        finally:
            ro.close()
        return 0
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
