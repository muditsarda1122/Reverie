"""``reverie review`` — the terminal review UI (feat/flow, D57).

Review is independent of sessions: one queue across every branch and repo.
The TUI shows one candidate at a time — conclusion text prominent, the
metadata small and muted, one key-hint line at the bottom, no boxes.
Single keypresses, no Enter needed (POSIX cbreak; falls back to line input
when stdin is not a TTY, on other platforms, or in tests).

Decisions apply immediately through the review gate
(``review_queue.apply_decisions`` → ``review_gate.apply_review_decisions``
with close=False) so contradiction surfacing, pending updates, diffusion
and the audit trail are exactly the gate's. ``u`` (undo) reverses the last
decision of this run while it is still reversible — an accept has already
been promoted and diffused, so undoing one plainly says so. Rejects and
skips are reversed from a snapshot.

Non-interactive surfaces for scripts: ``--list [--json]`` and
``--decide <id>=<accept|reject|skip>``. ``--expired`` reviews expired
candidates: ``v`` revives one (back to pending, fresh expiry lease), a/r
revive-and-apply.

No new dependencies: the screen is one candidate plus a hint line —
rendering is plain text and single-key input needs only ``termios``/
``tty`` (stdlib). prompt_toolkit/rich would add weight for nothing this
screen needs (no layout engine, no history, no styling system).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from . import repos, review_queue
from .brain import Brain, BrainError
from .config import get_config
from .review_gate import diffusion_failure_warning


class Quit(Exception):
    """User pressed q — leave the TUI."""


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

def build_queue(brain: Brain, repo: str | None = None, branch: str | None = None,
                include_expired: bool = False, config=None,
                now: datetime | None = None) -> list[dict]:
    """The ordered candidate list (strongest first). Expired candidates are
    excluded unless ``include_expired``."""
    return review_queue.candidates(
        brain, repo=repo, branch=branch,
        include_expired=include_expired, config=config, now=now,
    )


def ordered_by_topic(queue: list[dict]) -> list[dict]:
    """Topic-grouped ordering option (§7.4 groups, queue order within)."""
    ordered: list[dict] = []
    for _label, members in review_queue.topic_groups(queue):
        ordered.extend(members)
    return ordered


def candidate_detail(brain: Brain, candidate: dict) -> dict:
    """Grounding/evidence for the muted lines (fetched one candidate at a
    time — queue rows are deliberately lean)."""
    row = brain.get_session_ecu_row(candidate["id"])
    if row is None:
        return {}
    doc = json.loads(row["document"]) if row.get("document") else {}
    grounding = doc.get("grounding") or {}
    return {
        "grounding_files": grounding.get("files") or [],
        "evidence": doc.get("evidence_pointers") or [],
        "grounding_symbols": grounding.get("symbols") or [],
        "evidence_status": (doc.get("metadata") or {}).get("evidence_status"),
        "grounding_dropped": (grounding.get("validation") or {}).get(
            "dropped_files") or [],
        "symbols_dropped": (grounding.get("validation") or {}).get(
            "dropped_symbols") or [],
    }


def contradiction_partner(brain: Brain, candidate: dict) -> dict | None:
    """The canonical conclusion this candidate contradicts, when the
    lightweight diffuser already recorded a cross-brain contradicts edge —
    both conclusions are shown at the gate (§28.7)."""
    for edge in brain.get_session_edges_for(candidate["id"]):
        if edge["type"] != "contradicts":
            continue
        if edge["target_type"] == "canonical_ecu":
            other_id = edge["target_id"]
        elif edge["source_id"] != candidate["id"] \
                and edge["target_type"] == "session_ecu":
            continue
        else:
            continue
        if other_id == candidate["id"]:
            continue
        partner = brain.get_ecu(other_id)
        if partner is not None:
            return partner
    return None


# ---------------------------------------------------------------------------
# rendering (pure — snapshot-tested)
# ---------------------------------------------------------------------------

def _muted(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "—"
    return str(value) if value not in (None, "") else "—"


def render_screen(candidate: dict, detail: dict, *, index: int, total: int,
                  progress: dict, partner: dict | None = None,
                  expired: bool = False) -> str:
    """One calm screen: conclusion prominent, metadata muted, one hint line.
    No boxes, no panels."""
    lines: list[str] = []
    counts = progress
    lines.append(
        f"Reverie Review — candidate {index} of {total} "
        f"({counts.get('accepted', 0)} accepted · "
        f"{counts.get('rejected', 0)} rejected · "
        f"{counts.get('skipped', 0)} skipped so far)"
    )
    lines.append("")
    lines.append(candidate["cognition"])
    lines.append("")
    if partner is not None:
        lines.append(
            f'    contradicts: "{partner["cognition"]}" '
            f"({partner['confidence']:.2f})"
        )
        lines.append("")
    lines.append(f"    type: {candidate.get('conclusion_type') or '—'}")
    lines.append(f"    scope: {candidate.get('scope_path') or '—'}")
    lines.append(f"    confidence: {candidate['confidence']:.2f}")
    lines.append(f"    grounded: {_muted(detail.get('grounding_files'))}"
                 f"  symbols: {_muted(detail.get('grounding_symbols'))}")
    lines.append(f"    evidence: {_muted(detail.get('evidence'))}")
    if detail.get("evidence_status") == "unsupported":
        lines.append("    ⚠ evidence unsupported — no file path, test name, "
                     "or commit reference")
    dropped = (detail.get("grounding_dropped") or []) \
        + (detail.get("symbols_dropped") or [])
    if dropped:
        lines.append(f"    ⚠ unverifiable references dropped: "
                     f"{', '.join(str(d) for d in dropped)}")
    lines.append(
        f"    repo: {candidate.get('repo_path') or '—'} "
        f"(branch {candidate.get('branch') or '—'})"
    )
    if expired:
        lines.append("    status: expired")
        lines.append("")
        lines.append("v revive (back to pending) · a accept · r reject · "
                     "q quit")
    else:
        lines.append("")
        lines.append("a accept · r reject · s skip · e edit · g accept group "
                     "· u undo · q quit")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# decision application (the TUI's thin slice over the queue)
# ---------------------------------------------------------------------------

def decide_candidate(
    brain: Brain,
    candidate: dict,
    action: str,
    *,
    edited_cognition: str | None = None,
    channel: str = "tui",
    config=None,
    now: datetime | None = None,
) -> dict:
    """Apply one decision through the review gate. Returns
    ``{"id", "action", "diffused": bool}`` (an accept always diffuses —
    undo says so; a reject/skip is reversible while the run lasts)."""
    if action not in ("accept", "reject", "skip"):
        raise ValueError(f"unknown review action {action!r}")
    out = review_queue.apply_decisions(
        brain,
        {candidate["id"]: {"action": action,
                           "edited_cognition": edited_cognition}},
        channel=channel, config=config, now=now,
    )
    result = out["results"][0] if out["results"] else None
    if not out["decided"]:
        raise ValueError(
            f"candidate {candidate['id']} could not be decided: "
            f"{result.errors if result else 'no effect'}"
        )
    return {
        "id": candidate["id"],
        "action": action,
        "diffused": action != "accept" or (
            result is not None and candidate["id"] not in result.undiffused),
        "warning": diffusion_failure_warning(result)
        if action == "accept" else None,
    }


# ---------------------------------------------------------------------------
# key input
# ---------------------------------------------------------------------------

class KeyReader:
    """Single-keypress reader when possible, line fallback otherwise.

    The raw (cbreak) mode is entered ONCE for the whole run — before the
    first screen — and restored at the end. Setting it per-key would race
    with type-ahead: a keypress landing between screen render and
    ``setcbreak`` would sit in canonical buffering (awaiting a newline)
    and the TUI would wait forever. Raises ``Quit`` on q/quit/EOF."""

    def __init__(self, input_fn=input, reader_fn=None):
        self._input_fn = input_fn
        self._reader_fn = reader_fn     # injected reader: raw mode never used
        self._saved = None

    @property
    def _raw_capable(self) -> bool:
        if self._reader_fn is not None:
            return False
        try:
            return sys.stdin.isatty() and os.name == "posix"
        except Exception:
            return False

    def setup(self) -> None:
        if self._raw_capable:
            import termios
            import tty
            fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)          # one keypress, no Enter needed

    def teardown(self) -> None:
        if self._saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN,
                              self._saved)
            self._saved = None

    def __call__(self) -> str:
        if self._reader_fn is not None:
            return self._reader_fn()
        if self._saved is not None:
            return sys.stdin.read(1)
        try:
            answer = self._input_fn("> ").strip().lower()
        except EOFError:
            raise Quit() from None
        if answer in ("q", "quit", "exit"):
            raise Quit()
        return answer


def make_key_reader(input_fn=input) -> KeyReader:
    """The reader for run_tui (raw when possible, line fallback otherwise —
    the fallback maps full words and single letters alike)."""
    return KeyReader(input_fn)


# ---------------------------------------------------------------------------
# edit (§7.3 — the Extractor's text stays in metadata.review_edit, D19)
# ---------------------------------------------------------------------------

def edit_cognition(current: str, *, editor_cmd: str | None = None,
                   input_fn=input, print_fn=print) -> str | None:
    """Open $EDITOR on the text (fallback: inline prompt). None = no edit."""
    editor = editor_cmd or os.environ.get("EDITOR") or "vi"
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(current)
            path = fh.name
        try:
            proc = subprocess.run(
                [editor, path],
                check=False,
            )
            if proc.returncode == 0:
                with open(path, encoding="utf-8") as fh:
                    edited = fh.read().strip()
                if edited and edited != current:
                    return edited
                return None
        finally:
            os.unlink(path)
    except (OSError, subprocess.SubprocessError) as exc:
        print_fn(f"(could not open editor {editor!r}: {exc})")
    if not sys.stdin.isatty():
        return None
    try:
        answer = input_fn("Edited conclusion (Enter to keep): ").strip()
    except EOFError:
        return None
    return answer or None


# ---------------------------------------------------------------------------
# the TUI loop
# ---------------------------------------------------------------------------

class ReviewRun:
    """State of one interactive review run (decision logic, no rendering):
    the undo stack and the progress counters."""

    def __init__(self, brain: Brain, channel: str = "tui", config=None,
                 now: datetime | None = None):
        self.brain = brain
        self.channel = channel
        self.config = config or get_config()
        self.now = now or datetime.now(timezone.utc)
        self.undo_stack: list[list[tuple[str, str, dict | None]]] = []
        self.progress = {"accepted": 0, "rejected": 0, "skipped": 0,
                         "revived": 0}

    def snapshot(self, candidate_id: str) -> dict | None:
        return self.brain.get_session_ecu_row(candidate_id)

    def apply(self, candidate: dict, action: str,
              edited: str | None = None) -> dict:
        """Apply one decision through the gate and record its undo entry
        (snapshot BEFORE the apply — a reject deletes the row; an accept's
        entry is recorded too, and undo refuses it plainly)."""
        snapshot = self.snapshot(candidate["id"]) if action == "reject" \
            else None
        outcome = decide_candidate(
            self.brain, candidate, action,
            edited_cognition=edited, channel=self.channel,
            config=self.config, now=self.now,
        )
        self.progress[{"accept": "accepted", "reject": "rejected",
                       "skip": "skipped"}[action]] += 1
        self.record([(candidate["id"], action, snapshot)])
        return outcome

    def record(self, entries: list[tuple[str, str, dict | None]]) -> None:
        """Push one undo entry (a single decision or a whole group)."""
        if entries:
            self.undo_stack.append(entries)

    def undo(self) -> str:
        """Reverse the last decision of this run, when still possible.

        An accept has already been promoted and diffused into the Canonical
        Brain — undo says so plainly. Rejects and skips are reversed (a
        rejected row is restored from its snapshot)."""
        if not self.undo_stack:
            return "Nothing to undo in this review run."
        entry = self.undo_stack.pop()
        if any(decision == "accept" for _id, decision, _snap in entry):
            return ("The last decision was an accept — it already diffused "
                    "into the Canonical Brain and cannot be undone.")
        for candidate_id, decision, snapshot in reversed(entry):
            if decision == "reject":
                assert snapshot is not None
                self.brain.restore_session_ecu(snapshot)
            else:                       # skip (or revived skip)
                self.brain.update_session_ecu_review_status(
                    candidate_id, "pending")
            self.brain.add_review_decision(
                candidate_id, "undo", self.channel,
                session_id=snapshot["session_id"] if snapshot else None,
                decided_at=self.now.isoformat())
            key = {"reject": "rejected", "skip": "skipped"}.get(
                decision, decision)
            if key in self.progress:
                self.progress[key] = max(0, self.progress[key] - 1)
        return "Undone: " + ", ".join(
            f"{cid[:8]} ({decision})" for cid, decision, _ in entry)


def run_tui(
    brain: Brain,
    queue: list[dict],
    *,
    channel: str = "tui",
    config=None,
    now: datetime | None = None,
    key_reader=None,
    print_fn=print,
    edit_fn=None,
    page_size: int | None = None,
) -> dict:
    """The interactive loop: one candidate at a time, pages of
    ``review.page_size`` with 'more' available — never a filter."""
    cfg = config or get_config()
    run = ReviewRun(brain, channel=channel, config=cfg, now=now)
    if key_reader is not None and not hasattr(key_reader, "setup"):
        key_reader = KeyReader(reader_fn=key_reader)   # injected reader
    key_reader = key_reader or make_key_reader()
    edit_fn = edit_fn or (lambda text: edit_cognition(text, print_fn=print_fn))
    size = page_size or int(cfg.review.page_size)
    expired_mode = bool(queue) and queue[0].get("review_status") == "expired"

    key_reader.setup()               # cbreak on BEFORE the first screen
    try:
        return _run_loop(
            run, queue, key_reader=key_reader, print_fn=print_fn,
            edit_fn=edit_fn, page_size=size, expired_mode=expired_mode,
            config=cfg, now=now)
    finally:
        key_reader.teardown()


def _run_loop(run: ReviewRun, queue: list[dict], *, key_reader, print_fn,
              edit_fn, page_size: int, expired_mode: bool, config,
              now: datetime | None) -> dict:
    brain = run.brain
    size = page_size
    remaining = list(queue)
    shown = 0
    while remaining:
        page = remaining[:size]
        remaining = remaining[size:]
        for candidate in page:
            fresh = brain.get_queue_candidate(candidate["id"])
            if fresh is None or fresh["review_status"] not in (
                    "pending", "skipped", "expired"):
                continue               # already decided (e.g. by 'g') — skip
            shown += 1
            detail = candidate_detail(brain, candidate)
            partner = contradiction_partner(brain, candidate)
            print_fn("\n" + render_screen(
                candidate, detail, index=shown,
                total=len(queue), progress=run.progress, partner=partner,
                expired=expired_mode,
            ))
            try:
                key = key_reader().strip().lower()
            except Quit:
                return {"progress": run.progress, "remaining":
                        len(remaining) + len(page) - 1}
            _handle_key(
                run, candidate, key, page, remaining, brain=brain,
                print_fn=print_fn, edit_fn=edit_fn,
                expired_mode=expired_mode, key_reader=key_reader,
            )
        if remaining:
            print_fn(f"\n--- more available: {len(remaining)} candidate(s) "
                     "remain (q quits, decisions so far are kept) ---")
    _end_screen(run, key_reader, print_fn)
    return {"progress": run.progress, "remaining": 0}


def _end_screen(run: ReviewRun, key_reader, print_fn) -> None:
    """After the last candidate: a calm close-out. Undo stays available —
    the last decision of the run must be reversible too."""
    p = run.progress
    print_fn(f"\n--- all {p['accepted'] + p['rejected'] + p['skipped'] + p['revived']} "
             f"candidate(s) decided ({p['accepted']} accepted · "
             f"{p['rejected']} rejected · {p['skipped']} skipped) — "
             "u undo last · q done ---")
    while True:
        try:
            key = key_reader().strip().lower()
        except Quit:
            return
        if key == "u":
            print_fn(f"  · {run.undo()}")
            continue
        return


def _handle_key(run: ReviewRun, candidate: dict, key: str, page: list[dict],
                remaining: list[dict], *, brain: Brain, print_fn, edit_fn,
                expired_mode: bool, key_reader) -> None:
    """One key's effect. Decision logic lives in ReviewRun; this is wiring."""
    if expired_mode and key in ("v", "a", "r"):
        if key == "v":
            record = review_queue.revive(
                brain, candidate["id"], channel=run.channel, now=run.now)
            run.progress["revived"] += 1
            run.record([(candidate["id"], "revive", None)])
            print_fn(f"  · {record['message']}")
            return
        review_queue.revive(brain, candidate["id"], channel=run.channel,
                            now=run.now)
        action = {"a": "accept", "r": "reject"}[key]
        try:
            outcome = run.apply(candidate, action)
        except ValueError as exc:
            print_fn(f"  ! {exc}")
            return
        print_fn(f"  · revived and {action}ed through the review gate")
        if outcome.get("warning"):
            print_fn(f"  ⚠️ {outcome['warning']}")
        return

    if key in ("a", "r", "s"):
        action = {"a": "accept", "r": "reject", "s": "skip"}[key]
        try:
            outcome = run.apply(candidate, action)
        except ValueError as exc:
            print_fn(f"  ! {exc}")
            return
        print_fn(f"  · {action}{'ped' if action == 'skip' else 'ed'}")
        if outcome.get("warning"):
            print_fn(f"  ⚠️ {outcome['warning']}")
        return
    if key == "e":
        edited = edit_fn(candidate["cognition"])
        if edited:
            try:
                outcome = run.apply(candidate, "accept", edited=edited)
            except ValueError as exc:
                print_fn(f"  ! {exc}")
                return
            print_fn("  · accepted with your edit")
            if outcome.get("warning"):
                print_fn(f"  ⚠️ {outcome['warning']}")
        else:
            print_fn("  · (no edit — still open)")
        return
    if key == "g":
        label = None
        for group_label, members in review_queue.topic_groups(
                list(page) + list(remaining)):
            if any(m["id"] == candidate["id"] for m in members):
                label, group = group_label, members
                break
        try:
            accepted_here = 0
            for member in group:          # the group includes this one
                fresh = brain.get_queue_candidate(member["id"])
                if fresh is None or fresh["review_status"] not in (
                        "pending", "skipped"):
                    continue
                outcome = run.apply(member, "accept")
                accepted_here += 1
                if outcome.get("warning"):
                    print_fn(f"  ⚠️ {outcome['warning']}")
        except ValueError as exc:
            print_fn(f"  ! {exc}")
            return
        print_fn(f"  · accepted {accepted_here} candidate(s) in group "
                 f"{label}")
        return
    if key == "u":
        print_fn(f"  · {run.undo()}")
        return
    if key in ("q", "quit", "exit"):
        raise Quit()
    print_fn("  · (a accept · r reject · s skip · e edit · g accept group · "
             "u undo · q quit)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_decide_flags(items: list[str]) -> dict[str, str]:
    """``--decide <id>=<accept|reject|skip>`` (repeatable)."""
    decisions: dict[str, str] = {}
    for item in items:
        id_raw, _, action = item.partition("=")
        action = action.strip().lower()
        if not id_raw.strip() or action not in ("accept", "reject", "skip"):
            raise SystemExit(
                f"--decide expects <id>=<accept|reject|skip>, got {item!r}")
        decisions[id_raw.strip()] = action
    return decisions


def _scope_args(args) -> tuple[str | None, str | None, dict | None]:
    """Default scope: the current repo; outside a repo, everything."""
    repo, branch = args.repo, args.branch
    if repo is None and branch is None:
        try:
            info = repos.resolve_repo()
            return info.repo_path, info.branch, None
        except Exception:
            return None, None, None
    return repo, branch, None


def _archive_stale(brain: Brain) -> None:
    """Best-effort archive-on-lookup (D56) for the current repo."""
    try:
        info = repos.resolve_repo()
    except Exception:
        return
    if info.via_git:
        try:
            repos.archive_missing_branch_sessions(
                brain, info.repo_path, skip_branch=info.branch)
        except Exception:
            pass


def _resolve_open_questions(brain: Brain, policy: str) -> None:
    """§17.5 open questions resolve BEFORE the review flow (D41)."""
    from .review_gate import resolve_open_questions, resolve_open_questions_archive_all
    interactive = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False
    if policy == "archive":
        for record in resolve_open_questions_archive_all(brain):
            print(f"  ✓ {record['message']}")
    elif policy == "auto" and interactive:
        resolve_open_questions(brain)


def build_parser() -> argparse.ArgumentParser:
    """The `reverie review` parser (introspectable — docs/CLI.md's
    completeness test walks it)."""
    parser = argparse.ArgumentParser(
        prog="reverie review",
        description="Review pending Reverie candidates — one queue across "
                    "all branches and repos, independent of sessions.",
    )
    parser.add_argument("--repo", default=None,
                        help="only this repo's candidates (default: current "
                             "repo, or everything outside a repo)")
    parser.add_argument("--branch", default=None, help="only this branch")
    parser.add_argument("--all", action="store_true",
                        help="every repo's candidates")
    parser.add_argument("--by-topic", action="store_true",
                        help="group by topic instead of strongest-first")
    parser.add_argument("--expired", action="store_true",
                        help="review expired candidates (revive them)")
    parser.add_argument("--list", action="store_true",
                        help="list candidates non-interactively and exit")
    parser.add_argument("--json", action="store_true",
                        help="with --list: emit JSON")
    parser.add_argument("--decide", metavar="ID=ACTION", default=None,
                        nargs="+",
                        help="apply decisions non-interactively, e.g. "
                             "--decide <id>=accept <id2>=reject")
    parser.add_argument("--resolve-open-questions", default="auto",
                        choices=("auto", "skip", "archive"),
                        help="§17.5/D41: auto = prompt when interactive, "
                             "skip otherwise; skip = leave open questions "
                             "parked; archive = archive all without "
                             "prompting (they are review concerns, not "
                             "session concerns)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        brain = Brain()
    except BrainError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        _archive_stale(brain)
        _resolve_open_questions(brain, args.resolve_open_questions)
        repo = None if args.all else (args.repo if args.repo is not None
                                      else _scope_args(args)[0])
        queue = build_queue(brain, repo=repo, branch=args.branch,
                            include_expired=args.expired)
        if args.by_topic and not args.expired:
            queue = ordered_by_topic(queue)

        if args.list or args.decide is not None:
            if args.list:
                _print_listing(queue, json_out=args.json)
            if args.decide is not None:
                return _apply_flag_decisions(brain, queue,
                                             _parse_decide_flags(args.decide))
            return 0

        if not queue:
            mode = "expired" if args.expired else "pending"
            print(f"No {mode} candidates — nothing to review.")
            return 0
        outcome = run_tui(brain, queue)
        p = outcome["progress"]
        print(f"\nReview run done: {p['accepted']} accepted, "
              f"{p['rejected']} rejected, {p['skipped']} skipped"
              + (f", {p['revived']} revived" if p.get("revived") else "")
              + (f", {outcome['remaining']} left for next time"
                 if outcome["remaining"] else "") + ".")
        return 0
    finally:
        brain.close()


def _print_listing(queue: list[dict], json_out: bool) -> None:
    if json_out:
        print(json.dumps(queue, indent=2, default=str))
        return
    if not queue:
        print("No candidates in this scope.")
        return
    print(f"{len(queue)} candidate(s):")
    for c in queue:
        print(f"  [{c['confidence']:.2f}] {c['cognition']}")
        print(f"      id: {c['id']} | {c['repo_path']} ({c['branch']}) | "
              f"{c['review_status']}")


def _apply_flag_decisions(brain: Brain, queue: list[dict],
                          decisions: dict[str, str]) -> int:
    known = {c["id"] for c in queue}
    unknown = [cid for cid in decisions if cid not in known]
    if unknown:
        print(f"Unknown candidate id(s) in --decide: {unknown}",
              file=sys.stderr)
        return 1
    out = review_queue.apply_decisions(brain, decisions, channel="cli")
    applied = out["decided"]
    missing = [cid for cid in decisions if cid not in applied]
    if out["unknown"]:
        print(f"Not found in the queue: {out['unknown']}", file=sys.stderr)
    print(f"Applied {len(applied)} decision(s).")
    for result in out["results"]:
        if result.errors:
            print(f"  ! {'; '.join(result.errors)}")
        warning = diffusion_failure_warning(result)
        if warning:
            print(warning)
    # diffusion failures are soft (promotion stands; `reverie retry --rediffuse`
    # recovers) — only undecidable candidates fail the command.
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
