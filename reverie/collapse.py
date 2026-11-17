"""Collapse near-duplicate memory items at delivery time. STDLIB ONLY.

Hooks must start fast and never import numpy/torch, so delivery cannot use
embeddings. Items that say the same thing in nearly the same words are shown
once, with how many times it was seen ("seen N times": repetition is an
importance signal). The merge job (reverie.merge) does the durable,
embedding-based version of this offline.

Never collapse opposites: "never retry 429" and "retry 429" share almost every
word. Two items are only duplicates when their polarity (negations) and their
numbers/codes agree, and neither has been superseded.
"""

from __future__ import annotations

import re

SIM = 0.72                 # word-set Jaccard at/above which two items are duplicates
_WORD = re.compile(r"[a-z0-9_]+")
_NEG = {"not", "never", "no", "dont", "don", "without", "cannot", "cant", "stop",
        "avoid", "disable", "disabled", "forbid", "forbidden", "instead"}
_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
         "be", "this", "that", "it", "we", "you", "i", "with", "when", "use", "using"}


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("n't", "nt"))


def signature(text: str) -> tuple[frozenset, bool, frozenset]:
    toks = _tokens(text)
    neg = any(t in _NEG for t in toks)
    nums = frozenset(t for t in toks if any(c.isdigit() for c in t))
    words = frozenset(t for t in toks if t not in _STOP and t not in _NEG)
    return words, neg, nums


def similar(a: str, b: str, threshold: float = SIM) -> bool:
    wa, na, ua = signature(a)
    wb, nb, ub = signature(b)
    if na != nb or ua != ub or not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


def collapse(entries: list[dict]) -> list[dict]:
    """Keep one entry per group of near-duplicates (same kind). The keeper is
    the user's own words first, then the most-seen, then the oldest. It gets
    ``seen_n`` = the sum of the group's ``meta.seen``. Order of the keepers
    follows the input order."""
    groups: list[list[dict]] = []
    for it in entries:
        for g in groups:
            if g[0].get("kind") == it.get("kind") and similar(g[0]["text"], it["text"]):
                g.append(it)
                break
        else:
            groups.append([it])
    keepers: dict[int, dict] = {}
    for g in groups:
        best = min(g, key=lambda x: (x.get("source") != "user_said",
                                     -int((x.get("meta") or {}).get("seen", 1) or 1),
                                     x.get("created_at") or ""))
        best["seen_n"] = sum(int((x.get("meta") or {}).get("seen", 1) or 1) for x in g)
        keepers[id(best)] = best
    return [it for it in entries if id(it) in keepers]
