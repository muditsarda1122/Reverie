"""Write-path dedupe: one cheap check, no LLM.

A candidate that is almost the same as an existing item (cosine >= ``high``)
is dropped (and the existing item's "seen" counter goes up: repetition is an
importance signal). Everything else is kept. Telling a near-duplicate from a
new fact about the same topic needs judgement, and an LLM call on every write
was slow, costly and over-rejected (lab H14: no threshold separates them);
so near-duplicates that survive are collapsed when memory is DELIVERED
(reverie.collapse) and merged by a periodic job (reverie.merge).

The one judge that stays is ``replaces``: a user instruction that reverses an
older rule ("forget the earlier rule") supersedes it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np

log = logging.getLogger("reverie.dedupe")

JudgeFn = Callable[[str], str]


@dataclass
class Screen:
    keep: bool
    stage: str                      # empty | low | band | high
    sim: float = 0.0
    nearest_id: str | None = None   # most similar existing item (for corroboration)
    raw: str = ""
    neighbors: list[tuple[str, float]] = field(default_factory=list)


def _unit(v) -> np.ndarray:
    a = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(a))
    return a / n if n else a


def nearest(text: str, vec, existing: Sequence[tuple[str, str, object]],
            k: int) -> list[tuple[str, float]]:
    """The ``k`` most similar existing items as ``[(id, sim)]``, best first."""
    if not existing:
        return []
    q = _unit(vec)
    sims = np.array([float(_unit(v) @ q) for _, _, v in existing])
    return [(existing[int(i)][0], float(sims[int(i)]))
            for i in np.argsort(-sims)[:k]]


def screen(text: str, vec, existing: Sequence[tuple[str, str, object]], cfg) -> Screen:
    """Keep or drop one candidate. ``existing`` is ``[(id, text, vector)]``."""
    d = cfg.dedupe
    if not existing:
        return Screen(True, "empty")
    q = _unit(vec)
    sims = np.array([float(_unit(v) @ q) for _, _, v in existing])
    order = [int(i) for i in np.argsort(-sims)[: int(d.neighbors)]]
    best = order[0]
    sim = float(sims[best])
    nbrs = [(existing[i][0], float(sims[i])) for i in order]

    def mk(keep: bool, stage: str) -> Screen:
        return Screen(keep, stage, sim=sim, nearest_id=existing[best][0], neighbors=nbrs)
    if sim >= float(d.high):
        return mk(False, "high")
    return mk(True, "low" if sim < float(d.low) else "band")


REPLACE_PROMPT = """A user gave a coding agent a NEW instruction. Below are EXISTING instructions or decisions it may have changed.

Existing:
{nbrs}

NEW instruction:
{cand}

Does the NEW instruction reverse, replace or directly contradict any existing item, so that the old one must no longer be followed? Only answer with item numbers if the new instruction really changes what the old one demands (a new instruction that merely adds detail, or is about a different thing, does NOT replace it).
Reply in exactly this format on one line: REPLACES=<comma-separated item numbers, or NONE>"""

_NUM = re.compile(r"\d+")


def parse_replaces(raw: str, count: int) -> list[int]:
    """-> zero-based indexes of the existing items the reply says are replaced."""
    m = re.search(r"REPLACES\s*=\s*([^\n]*)", raw or "", re.I)
    if not m or re.match(r"\s*NONE", m.group(1), re.I):
        return []
    return sorted({int(n) - 1 for n in _NUM.findall(m.group(1))
                   if 1 <= int(n) <= count})


def replaces(text: str, neighbors: Sequence[tuple[str, str, float]],
             judge: JudgeFn | None) -> list[str]:
    """Ids among ``neighbors`` ``[(id, text, sim)]`` that the user's new
    instruction ``text`` reverses. Fails SAFE: no judge or an error -> nothing
    is replaced (a wrong supersession hides a standing rule)."""
    if not neighbors or judge is None:
        return []
    prompt = REPLACE_PROMPT.format(
        nbrs="\n".join(f"{i}. {t}" for i, (_, t, _) in enumerate(neighbors, 1)),
        cand=text)
    try:
        raw = judge(prompt)
    except Exception as exc:
        log.warning("replace judge failed (%s); replacing nothing", exc)
        return []
    return [neighbors[i][0] for i in parse_replaces(raw, len(neighbors))]


def llm_judge(config, max_tokens: int = 60) -> JudgeFn:
    """The product judge: the configured LLM, classification model, temp 0."""
    from .llm import call_llm, model_for

    def judge(prompt: str) -> str:
        return call_llm(prompt, model=model_for(config, "classification"),
                        temperature=0.0, max_tokens=max_tokens, config=config)
    return judge
