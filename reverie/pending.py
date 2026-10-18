"""Recovery for failed LLM work (feat/providers) — `reverie retry`.

Two failure states this recovers:

1. **Pending extractions.** When the extraction LLM call fails after
   retries, the interaction input is persisted to ``pending_extractions``
   (nothing is lost). Items are retried automatically at the next
   successful ``observe`` for the same (repo, branch), or manually:
   ``reverie retry`` / ``reverie retry --list`` / ``reverie retry --drop <id>``.
   A retry extracts the stored interaction into the *current* active
   session for that (repo, branch) — a closed session's brain is never
   written.

2. **Undiffused ECUs.** A review-gate accept whose Full-Diffuser pass
   failed leaves the ECU promoted but edgeless (ASSESSMENT.md:178). The
   review gate flags those in metadata (``diffusion_failed``);
   ``reverie retry --rediffuse`` re-runs the diffuser and clears the flag.
"""

from __future__ import annotations

import argparse
import sys

from .brain import Brain, BrainError
from .config import get_config
from .diffuser import DiffuserError, diffuse_ecu
from .extractor import ExtractorError, extract_and_store_session

# ---------------------------------------------------------------------------
# pending extractions
# ---------------------------------------------------------------------------

def save_pending_extraction(
    brain: Brain,
    session_row: dict,
    interaction: dict,
    error: str,
    origin_agent: str | None = None,
    commit_hash: str | None = None,
) -> str:
    """Persist a failed extraction's input under its (repo, branch)."""
    return brain.add_pending_extraction(
        repo_path=session_row["repo_path"],
        branch=session_row["branch"],
        interaction=interaction,
        session_id=session_row["id"],
        origin_agent=origin_agent,
        commit_hash=commit_hash,
        error=error,
    )


def retry_pending(
    brain: Brain,
    repo_path: str | None = None,
    branch: str | None = None,
    item_id: str | None = None,
    embedding_model=None,
    config=None,
) -> dict:
    """Retry pending extractions, storing recovered ECUs in the active session.

    An item whose (repo, branch) has no active session is skipped (one is
    created automatically on that repo's next tool call). Returns a summary
    dict; individual failures are recorded, never raised.
    """
    cfg = config or get_config()
    items = brain.list_pending_extractions(repo_path=repo_path, branch=branch)
    if item_id:
        items = [i for i in items if i["id"] == item_id]
    summary = {"pending": len(items), "recovered": 0, "failed": 0,
               "skipped": 0, "details": []}
    for item in items:
        active = brain.get_active_session(item["repo_path"], item["branch"])
        if active is None:
            summary["skipped"] += 1
            summary["details"].append({
                "id": item["id"], "outcome": "skipped",
                "reason": f"no active session for {item['repo_path']} "
                          f"({item['branch']}) — it is created on that "
                          "repo's next tool call",
            })
            continue
        try:
            _result, ids = extract_and_store_session(
                brain, active["id"], item["interaction"],
                embedding_model=embedding_model, config=cfg,
                origin_agent=item.get("origin_agent"),
                commit_hash=item.get("commit_hash"),
                repo_path=item.get("repo_path") or active["repo_path"],
            )
        except ExtractorError as exc:
            brain.mark_pending_extraction_attempt(item["id"], str(exc))
            summary["failed"] += 1
            summary["details"].append({"id": item["id"], "outcome": "failed",
                                       "reason": str(exc)})
            continue
        brain.delete_pending_extraction(item["id"])
        summary["recovered"] += 1
        summary["details"].append({
            "id": item["id"], "outcome": "recovered",
            "reason": f"{len(ids)} conclusion(s) extracted into session "
                      f"{active['id'][:8]}",
        })
    return summary


# ---------------------------------------------------------------------------
# re-diffusion for promoted-but-undiffused ECUs
# ---------------------------------------------------------------------------

def rediffuse(
    brain: Brain,
    ecu_id: str | None = None,
    embedding_model=None,
    config=None,
) -> dict:
    """Re-run the Full Diffuser for flagged ECUs; clear flags on success.

    ``ecu_id`` targets one ECU; otherwise every ECU carrying the
    ``diffusion_failed`` metadata flag is processed. Diffusion failures keep
    the flag (the ECU stays findable for a later retry).
    """
    cfg = config or get_config()
    targets = [ecu_id] if ecu_id else brain.list_undiffused_ecus()
    summary = {"targets": len(targets), "rediffused": 0, "failed": 0,
               "missing": 0, "details": []}
    for target_id in targets:
        if brain.get_ecu(target_id) is None:
            summary["missing"] += 1
            summary["details"].append({"ecu_id": target_id,
                                       "outcome": "missing"})
            continue
        try:
            diffuse_ecu(brain, target_id,
                        embedding_model=embedding_model, config=cfg)
        except DiffuserError as exc:
            summary["failed"] += 1
            summary["details"].append({"ecu_id": target_id,
                                       "outcome": "failed", "reason": str(exc)})
            continue
        brain.update_ecu_metadata(target_id, diffusion_failed=False)
        summary["rediffused"] += 1
        summary["details"].append({"ecu_id": target_id, "outcome": "rediffused"})
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_retry_summary(summary: dict, print_fn) -> None:
    print_fn(
        f"Pending extractions: {summary['pending']} "
        f"({summary['recovered']} recovered, {summary['failed']} failed, "
        f"{summary['skipped']} skipped)."
    )
    for d in summary["details"]:
        mark = {"recovered": "✓", "failed": "✗", "skipped": "·",
                "missing": "·"}.get(d["outcome"], "·")
        print_fn(f"  {mark} {d['id'][:8]} {d['outcome']}: {d.get('reason', '')}")


def _print_rediffuse_summary(summary: dict, print_fn) -> None:
    print_fn(
        f"Undiffused conclusions: {summary['targets']} "
        f"({summary['rediffused']} rediffused, {summary['failed']} failed)."
    )
    for d in summary["details"]:
        mark = "✓" if d["outcome"] == "rediffused" else "✗"
        print_fn(f"  {mark} {d['ecu_id'][:8]} {d['outcome']}: "
                 f"{d.get('reason', '')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reverie retry",
        description="Retry pending extractions and re-diffuse promoted-but-"
                    "undiffused conclusions.",
    )
    parser.add_argument("--list", action="store_true",
                        help="list pending extractions and exit")
    parser.add_argument("--id", metavar="ITEM_ID", default=None,
                        help="retry only this pending extraction id")
    parser.add_argument("--drop", metavar="ITEM_ID", default=None,
                        help="discard this pending extraction without retrying")
    parser.add_argument("--rediffuse", action="store_true",
                        help="re-run diffusion for undiffused conclusions instead of "
                             "retrying pending extractions")
    parser.add_argument("--ecu-id", metavar="ECU_ID", default=None,
                        help="with --rediffuse: only this conclusion id (ECU id)")
    parser.add_argument("--repo", default=None,
                        help="only items for this repo path (default: all)")
    parser.add_argument("--branch", default=None,
                        help="only items for this branch (default: all)")
    args = parser.parse_args(argv)

    try:
        brain = Brain()
    except BrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        if args.drop:
            brain.delete_pending_extraction(args.drop)
            print(f"Dropped pending extraction {args.drop[:8]}.")
            return 0
        if args.rediffuse:
            summary = rediffuse(brain, ecu_id=args.ecu_id)
            _print_rediffuse_summary(summary, print)
            return 0
        if args.list:
            items = brain.list_pending_extractions(repo_path=args.repo,
                                                   branch=args.branch)
            if not items:
                print("No pending extractions — nothing lost.")
            for item in items:
                print(f"  {item['id'][:8]}  {item['repo_path']} "
                      f"({item['branch']})  retries={item['retry_count']}  "
                      f"error={item['error']}")
            return 0
        if args.id:
            items = brain.list_pending_extractions(repo_path=args.repo,
                                                   branch=args.branch)
            if not any(i["id"] == args.id for i in items):
                print(f"No pending extraction with id {args.id}",
                      file=sys.stderr)
                return 1
        summary = retry_pending(brain, repo_path=args.repo,
                                branch=args.branch, item_id=args.id)
        _print_retry_summary(summary, print)
        return 0
    finally:
        brain.close()


if __name__ == "__main__":
    sys.exit(main())
