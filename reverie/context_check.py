"""Judge-backed checks on shared team documents (never run inside a hook).

* conflicts: a rule at a lower layer that contradicts a higher-layer rule
  (one judge call per lower rule, cached by content so unchanged text is free);
* staleness: backticked files/symbols in a bullet are its anchors; a commit that
  touches them is judged with the same diff prompt as personal memory
  ("this paragraph contradicts x.py since commit Y").

Results go to the sidecar (``context.save_state``), never into the documents.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from . import anchors, context, spool, staleness

CONFLICT_PROMPT = """Different groups of an engineering organisation (teams, orgs, repo owners) write rules for the same coding agents.
RULE: {low}

OTHER GROUPS' RULES:
{high}

Does the RULE CONTRADICT any other rule (an agent working on the same code cannot follow both)? Merely being more specific is not a contradiction. Answer on one line: NONE, or the number of the contradicted rule, a colon, and a short reason."""


def _cfg_judge():
    from .config import get_config
    cfg = get_config()
    return cfg, staleness.default_judge(cfg)


def find_conflicts(root: str, judge: Callable[[str], str], *, state: dict | None = None
                   ) -> list[dict]:
    """Judge rule pairs from different scopes that could apply to the same work
    (nested or sibling, not disjoint paths). One call per rule, cached by content;
    the record says nothing about who wins: ``context.resolve`` decides that."""
    state = context.load_state() if state is None else state
    rules = [i for i in context.load_items(root) if i.kind == "rule"]
    checked: dict[str, str] = state.get("conflict_checked") or {}
    live = {i.id for i in rules}
    out = [c for c in state.get("conflicts") or [] if c.get("a") in live and c.get("b") in live]
    known = {frozenset((c["a"], c["b"])) for c in out}
    for low in rules:
        others = [h for h in rules if h.id != low.id
                  and context.relation(low, h) not in ("same", "disjoint")]
        if not others:
            continue
        sig = "|".join(sorted(h.id for h in others))
        if checked.get(low.id) == sig:
            continue                                    # unchanged since the last check
        listing = "\n".join(f"{n}. [{h.label}] {h.text}" for n, h in enumerate(others, 1))
        try:
            raw = judge(CONFLICT_PROMPT.format(low=low.text, high=listing)).strip()
        except Exception:                                # a judge error never flags
            continue
        checked[low.id] = sig
        m = re.match(r"\s*(\d+)\s*[:.)-]?\s*(.*)", raw)
        if not m or raw.upper().startswith("NONE"):
            continue
        n = int(m.group(1))
        if 1 <= n <= len(others) and frozenset((others[n - 1].id, low.id)) not in known:
            out.append({"a": others[n - 1].id, "b": low.id, "reason": m.group(2)[:200]})
            known.add(frozenset((others[n - 1].id, low.id)))
    state["conflicts"], state["conflict_checked"] = out, checked
    context.save_state(state)
    return out


def anchors_of(root: str, text: str, tracked: set[str]) -> dict:
    files, idents = anchors.candidates(text)
    real = [f for f in files if f in tracked or any(t.endswith("/" + f) for t in tracked)]
    return {"files": real, "symbols": idents[:6]}


def check_stale(root: str, judge: Callable[[str], str], *, diff_chars: int = 6000,
                since: str | None = None, state: dict | None = None) -> list[dict]:
    """Recheck shared documents against commits since the last check."""
    state = context.load_state() if state is None else state
    marks: dict[str, str] = state.setdefault("stale_commit", {})
    last = since or marks.get(root)
    head = staleness.head(root)
    if last is None:
        marks[root] = head
        context.save_state(state)
        return []
    tracked = set(spool._git(["ls-files"], root, timeout=10.0).splitlines())
    items = [i for i in context.load_items(root)
             if i.id not in (state.get("stale") or {})]
    anch = {i.id: anchors_of(root, i.text, tracked) for i in items}
    flags: list[dict] = []
    stale = state.setdefault("stale", {})
    for sha, subject in staleness.commits_since(root, last):
        files, diff = staleness._commit_files(root, sha), staleness._commit_diff(root, sha)
        for it in items:
            if it.id in stale or not (anch[it.id]["files"] or anch[it.id]["symbols"]):
                continue
            if not staleness._touches({"anchor": anch[it.id]}, files, diff):
                continue
            try:
                raw = judge(staleness.PROMPT.format(mem=it.text, msg=subject, tests="",
                                                    diff=diff[:diff_chars]))
            except Exception:
                continue
            verdict, reason, _upd = staleness.parse_verdict(raw)
            if verdict == "CONTRADICTED":
                stale[it.id] = {"commit": sha[:12], "message": subject, "reason": reason,
                                "file": it.file}
                flags.append({"id": it.id, **stale[it.id]})
    marks[root] = head
    context.save_state(state)
    return flags


def auto_check(root: str, judge: Callable[[str], str] | None = None, *,
               diff_chars: int | None = None, force: bool = False) -> dict | None:
    """Detection, run where a model call is allowed (``context add``/``accept``, the
    detached session-start worker, the daily merge job; never a hook). Conflicts are
    judged only when the documents changed since the last run (keyed by
    ``context.doc_signature``); staleness only on commits since the last check. A
    failing or missing judge is swallowed: nothing is recorded as checked, so the
    next run tries again. Returns a summary or None when nothing needed doing."""
    try:
        state = context.load_state()
        sig = context.doc_signature(root)
        sigs: dict[str, str] = state.setdefault("doc_sig", {})
        new_docs = force or sigs.get(root) != sig
        new_commits = staleness.head(root) != (state.get("stale_commit") or {}).get(root)
        if not (new_docs or new_commits):
            return None
        errors = 0
        if judge is None:
            cfg, judge = _cfg_judge()
            diff_chars = diff_chars or int(cfg.stale.diff_chars)

        def guarded(prompt: str) -> str:
            nonlocal errors
            try:
                return judge(prompt)          # type: ignore[misc]
            except Exception:
                errors += 1
                raise

        out: dict = {"conflicts": None, "stale": None}
        old_mark = (state.get("stale_commit") or {}).get(root)
        if new_docs:
            out["conflicts"] = len(find_conflicts(root, guarded, state=state))
        if new_commits:
            out["stale"] = len(check_stale(root, guarded, diff_chars=diff_chars or 6000,
                                           state=state))
        if errors == 0:
            state.setdefault("doc_sig", {})[root] = sig
        elif old_mark is not None:
            state["stale_commit"][root] = old_mark   # a failed judge must not skip commits
        context.save_state(state)
        out["judge_errors"] = errors
        return out
    except Exception:
        return None
