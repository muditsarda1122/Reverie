"""Periodic merge of near-duplicate memory items (embedding-based, offline).

Runs outside any hook (it may import numpy). Two active items of the same repo
and kind merge when their embeddings are close (``dedupe.merge_sim``) AND they
agree on polarity and numbers (reverie.collapse.similar's guards, so "never
retry 429" is never merged with "retry 429"). The keeper is the user's own
words, then the most-seen, then the oldest; it absorbs the others' ``seen``
counts. The others become ``superseded`` with ``meta.merged_into`` naming the
keeper. Nothing is deleted.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from . import collapse, items


@dataclass
class MergeReport:
    merged: list[tuple[str, str]] = field(default_factory=list)   # (dropped, keeper)


def _signature_agrees(a: str, b: str) -> bool:
    _, na, ua = collapse.signature(a)
    _, nb, ub = collapse.signature(b)
    return na == nb and ua == ub


GROUP_PROMPT = """These memory items of a coding agent may repeat one another. Group the items that state the SAME instruction or fact: the same requirement with the same meaning, only worded differently. Items that merely share a topic, or that say opposite things (do X / do not do X), must NOT be grouped. Leave an item out of every group if it has no duplicate.

{items}

Reply on one line: GROUPS=<groups separated by ';', each a comma-separated list of item numbers>, or GROUPS=NONE. Example: GROUPS=1,3;2,5"""

_NUM = re.compile(r"\d+")


def parse_groups(raw: str, count: int) -> list[list[int]]:
    """-> groups of zero-based indexes (each of size >= 2, no index twice)."""
    m = re.search(r"GROUPS\s*=\s*([^\n]*)", raw or "", re.I)
    if not m or re.match(r"\s*NONE", m.group(1), re.I):
        return []
    used: set[int] = set()
    out: list[list[int]] = []
    for part in m.group(1).split(";"):
        idx = sorted({int(n) - 1 for n in _NUM.findall(part) if 1 <= int(n) <= count} - used)
        if len(idx) >= 2:
            used.update(idx)
            out.append(idx)
    return out


def _components(ids: list[str], vecs: dict, floor: float) -> list[list[str]]:
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for ai, a in enumerate(ids):
        for b in ids[ai + 1:]:
            if float(vecs[a] @ vecs[b]) >= floor:
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def _keeper_order(x: dict) -> tuple:
    return (x.get("source") != "user_said", -int((x.get("meta") or {}).get("seen", 1) or 1),
            x.get("created_at") or "")


def _merge_group(conn, members: list[dict], report: MergeReport) -> list[str]:
    """Merge ``members`` into one keeper; -> ids that were superseded."""
    members = sorted(members, key=_keeper_order)
    keeper, rest = members[0], members[1:]
    absorbed = 0
    for other in rest:
        items.set_status(conn, other["id"], "superseded")
        items.set_meta(conn, other["id"], {"merged_into": keeper["id"]})
        absorbed += int((other.get("meta") or {}).get("seen", 1) or 1)
        report.merged.append((other["id"], keeper["id"]))
    seen = int((keeper.get("meta") or {}).get("seen", 1) or 1) + absorbed
    items.set_meta(conn, keeper["id"], {"seen": seen})
    return [o["id"] for o in rest]


def run(conn: sqlite3.Connection, repo_key: str | None, cfg, judge=None) -> MergeReport:
    """Merge near-duplicates. Two paths:

    * fast, no LLM: cosine >= ``merge_sim`` AND same polarity/numbers;
    * paraphrases (cosine is NOT enough: "do not add dependencies without asking"
      and "ask before adding dependencies" sit where real opposites sit): items of
      one kind whose cosine is >= ``merge_candidate_sim`` form a candidate cluster
      and ``judge`` groups the true restatements, one call per cluster. No judge,
      or a judge error, merges nothing (fails safe)."""
    import numpy as np

    from .embeddings import from_blob
    report = MergeReport()
    rows = [i for i in items.active(conn, repo_key, with_embedding=True)
            if i.get("embedding") and i.get("repo_key") == repo_key]
    by_id = {r["id"]: r for r in rows}
    vecs = {}
    for i in rows:
        v = np.array(from_blob(i["embedding"]), dtype=np.float32)     # a writable copy
        n = float(np.linalg.norm(v))
        vecs[i["id"]] = v / n if n else v
    threshold = float(cfg.dedupe.merge_sim)
    gone: set[str] = set()
    for kind in sorted({r["kind"] for r in rows}):
        ids = [r["id"] for r in rows if r["kind"] == kind]
        # fast path
        for ai, a in enumerate(ids):
            if a in gone:
                continue
            near = [b for b in ids[ai + 1:] if b not in gone
                    and float(vecs[a] @ vecs[b]) >= threshold
                    and _signature_agrees(by_id[a]["text"], by_id[b]["text"])]
            if near:
                gone.update(_merge_group(conn, [by_id[a]] + [by_id[b] for b in near], report))
        # judged paraphrase clusters
        live = [i for i in ids if i not in gone
                and (items.get(conn, i) or {}).get("status") == "active"]
        if judge is None:
            continue
        for comp in _components(live, vecs, float(cfg.dedupe.merge_candidate_sim)):
            comp = comp[:12]
            prompt = GROUP_PROMPT.format(items="\n".join(
                f"{n}. {by_id[i]['text']}" for n, i in enumerate(comp, 1)))
            try:
                raw = judge(prompt)
            except Exception:
                continue
            for grp in parse_groups(raw, len(comp)):
                gone.update(_merge_group(conn, [by_id[comp[k]] for k in grp], report))
    return report
