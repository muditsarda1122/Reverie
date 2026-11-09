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

CONFLICT_PROMPT = """Two layers of an engineering organisation write rules for coding agents. A HIGHER layer outranks a LOWER one.
LOWER-LAYER RULE: {low}

HIGHER-LAYER RULES:
{high}

Does the lower rule CONTRADICT any higher rule (an agent cannot follow both)? Merely being more specific is not a contradiction. Answer on one line: NONE, or the number of the contradicted higher rule, a colon, and a short reason."""


def _cfg_judge():
    from .config import get_config
    cfg = get_config()
    return cfg, staleness.default_judge(cfg)


def find_conflicts(root: str, judge: Callable[[str], str], *, state: dict | None = None
                   ) -> list[dict]:
    """Judge cross-layer rule pairs; returns the cached+new conflict records."""
    state = context.load_state() if state is None else state
    rules = [i for i in context.load_items(root) if i.kind == "rule"]
    known = {(c["a"], c["b"]) for c in state.get("conflicts") or []}
    checked: dict[str, str] = state.get("conflict_checked") or {}
    out = [c for c in state.get("conflicts") or []]
    live = {i.id for i in rules}
    out = [c for c in out if c.get("a") in live and c.get("b") in live]
    for low in rules:
        high = [h for h in rules if context.RANK[h.layer] < context.RANK[low.layer]]
        if not high:
            continue
        sig = "|".join(sorted(h.id for h in high))
        if checked.get(low.id) == sig:
            continue                                    # unchanged since the last check
        listing = "\n".join(f"{n}. [{h.layer}] {h.text}" for n, h in enumerate(high, 1))
        try:
            raw = judge(CONFLICT_PROMPT.format(low=low.text, high=listing)).strip()
        except Exception:                                # a judge error never flags
            continue
        checked[low.id] = sig
        m = re.match(r"\s*(\d+)\s*[:.)-]?\s*(.*)", raw)
        if not m or raw.upper().startswith("NONE"):
            continue
        n = int(m.group(1))
        if 1 <= n <= len(high) and (high[n - 1].id, low.id) not in known:
            out.append({"a": high[n - 1].id, "b": low.id, "reason": m.group(2)[:200]})
            known.add((high[n - 1].id, low.id))
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
