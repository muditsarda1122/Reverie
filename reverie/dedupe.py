"""Two-stage dedupe for memory candidates.

Embeddings alone cannot separate duplicates from new facts about the same
topic (lab H14: no threshold works), and an LLM judge on every candidate is
slow and costs a call each. Two stages:

  1. max cosine similarity of the candidate to existing items
       sim <  low   -> KEEP  (no judge; clearly different)
       sim >= high  -> DROP  (no judge; clearly the same)
       otherwise    -> stage 2
  2. the binary NEW_INFO judge, shown the nearest ``neighbors`` items.
     Framed as a yes/no question ("does it add a claim not already stated?"):
     asking a judge to "reject duplicates" made it over-reject badly.

Thresholds (config ``dedupe``) were tuned on one repo's 49 items; they are
starting points. The judge fails OPEN: if it errors, the candidate is kept.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np

log = logging.getLogger("reverie.dedupe")

PROMPT = """You maintain a coding agent's project memory. Below are the most similar EXISTING memories, then a NEW candidate.

Existing memories:
{nbrs}

NEW candidate:
{cand}

Question 1: Does the NEW candidate contain at least one specific claim, mechanism, consequence, rule or detail that is NOT already stated in the existing memories? (Sharing a topic or component with an existing memory does not count as already stated.) Answer NEW_INFO=YES or NEW_INFO=NO.
{q2}Reply in exactly this format on one line: {fmt}"""

_Q2 = ("Question 2: Is the candidate only about how to run tools in this "
       "sandbox (test-runner flags, missing plugins), with no insight about "
       "the project's design? Answer SANDBOX_TRIVIA=YES or "
       "SANDBOX_TRIVIA=NO.\n")

JudgeFn = Callable[[str], str]


@dataclass
class Screen:
    keep: bool
    stage: str                      # empty | low | high | judge | judge_error | judge_off
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


def parse_verdict(raw: str, trivia_check: bool) -> bool:
    up = (raw or "").upper()
    keep = "NEW_INFO=YES" in up
    if trivia_check:
        keep = keep and "SANDBOX_TRIVIA=NO" in up
    return keep


def screen(text: str, vec, existing: Sequence[tuple[str, str, object]], cfg,
           judge: JudgeFn | None) -> Screen:
    """Decide keep/drop for one candidate.

    ``existing`` is ``[(id, text, vector)]`` in arrival order. ``judge`` takes
    a prompt and returns the model's reply (or raises)."""
    d = cfg.dedupe
    if not existing:
        return Screen(True, "empty")
    q = _unit(vec)
    sims = np.array([float(_unit(v) @ q) for _, _, v in existing])
    order = [int(i) for i in np.argsort(-sims)[: int(d.neighbors)]]
    best = order[0]
    sim = float(sims[best])
    nearest = existing[best][0]
    nbrs = [(existing[i][0], float(sims[i])) for i in order]

    def mk(keep: bool, stage: str, raw: str = "") -> Screen:
        return Screen(keep, stage, sim=sim, nearest_id=nearest, raw=raw,
                      neighbors=nbrs)
    if sims[best] < float(d.low):
        return mk(True, "low")
    if sims[best] >= float(d.high):
        return mk(False, "high")
    if not d.judge or judge is None:
        return mk(True, "judge_off")
    trivia = bool(d.sandbox_trivia_check)
    prompt = PROMPT.format(
        nbrs="\n".join(f"- ({sims[i]:.2f}) {existing[i][1]}" for i in order),
        cand=text, q2=_Q2 if trivia else "",
        fmt="NEW_INFO=<YES|NO> SANDBOX_TRIVIA=<YES|NO>" if trivia
        else "NEW_INFO=<YES|NO>")
    try:
        raw = judge(prompt)
    except Exception as exc:                       # fail open
        log.warning("dedupe judge failed (%s); keeping the candidate", exc)
        return mk(True, "judge_error", str(exc)[:120])
    return mk(parse_verdict(raw, trivia), "judge", raw[:120])


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
