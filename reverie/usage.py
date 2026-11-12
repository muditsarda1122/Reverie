"""The closed loop: was a delivered memory item used, ignored or contradicted?

Without this Reverie accumulates but does not learn: nothing says whether the rule it
keeps handing an agent ever mattered. Two parts:

* ``log_delivery`` (STDLIB, called by the delivery code on every hook/MCP delivery)
  appends ``{t, channel, ids}`` to ``<spool>/deliveries.jsonl``;
* ``judge_exchange`` (offline, after an exchange has been captured) asks one batched
  judge call to label every item delivered around that exchange, using the user's
  prompt, the real diff and the agent's answer (behaviour evidence: this is about
  what the agent DID, not about creating memory), and adds the counts to
  ``item.meta.usage``. An item that agents keep contradicting is marked
  ``meta.challenged`` (delivery says so) so the user can fix or drop it.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import items, spool

LABELS = ("USED", "IGNORED", "IRRELEVANT", "CONTRADICTED", "OVERRIDDEN")
WINDOW_BEFORE = 900        # s before the prompt: session-start deliveries
WINDOW_AFTER = 1800        # s after it: point-of-action deliveries during the work
MAX_ITEMS = 12
CHALLENGE_AT = 2           # contradictions (and more than uses) before delivery warns


def _path(cwd: str | Path) -> Path:
    return spool.repo_dir(cwd) / "deliveries.jsonl"


def log_delivery(cwd: str | Path, channel: str, ids) -> None:
    """Append one delivery record. Never raises; no-op without ids."""
    try:
        ids = sorted({str(i) for i in ids})
        if not ids or os.environ.get("REVERIE_NO_USAGE_LOG"):
            return
        p = _path(cwd)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "channel": channel, "ids": ids}) + "\n")
    except Exception:
        pass


def delivered_between(cwd: str | Path, t0: float, t1: float) -> list[str]:
    seen: dict[str, None] = {}
    try:
        for line in _path(cwd).read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if t0 <= float(rec.get("t", 0)) <= t1:
                for i in rec.get("ids", []):
                    seen[i] = None
    except OSError:
        pass
    return list(seen)


PROMPT = """You audit whether a coding agent's memory items actually mattered in one piece of work.

MEMORY ITEMS delivered to the agent around this work:
{items}

THE USER ASKED:
{prompt}

THE REAL CHANGE (diff):
{diff}

THE AGENT'S REPLY:
{reply}

Label every item with exactly one of:
USED         the work follows or applies the item, or the agent relied on it
IGNORED      the item was relevant to this work but the work shows no sign of it (no violation either)
IRRELEVANT   the item has nothing to do with this work
CONTRADICTED the work does the opposite of the item although the user did not ask for that
OVERRIDDEN   the user explicitly asked for something that differs from the item, so not following it was right

Reply on one line, one label per item number: 1=USED 2=IRRELEVANT 3=CONTRADICTED ..."""


def parse_labels(raw: str, count: int) -> dict[int, str]:
    out: dict[int, str] = {}
    for m in re.finditer(r"(\d+)\s*=\s*(USED|IGNORED|IRRELEVANT|CONTRADICTED|OVERRIDDEN)",
                         (raw or "").upper()):
        n = int(m.group(1))
        if 1 <= n <= count and n not in out:
            out[n] = m.group(2)
    return out


def judge_exchange(cwd: str | Path, conn, *, prompt: str, reply: str, epoch: float,
                   judge, repo_key: str | None = None,
                   epoch_end: float | None = None) -> dict[str, str]:
    """Label the items delivered around one exchange. -> {item_id: label}.

    Fails safe: no judge / no deliveries / a judge error records nothing."""
    from . import provenance
    # an imported (past) exchange ends when its answer was written; only deliveries up to
    # then (plus a minute of skew) belong to it, not whatever was delivered hours later
    t1 = epoch_end + 60 if epoch_end else epoch + WINDOW_AFTER
    ids = delivered_between(cwd, epoch - WINDOW_BEFORE, t1)
    rows = [r for r in (items.get(conn, i) for i in ids) if r and r["status"] == "active"]
    rows = rows[:MAX_ITEMS]
    if not rows or judge is None:
        return {}
    text = "\n".join(f"{n}. [{r['kind']}] {r['text']}" for n, r in enumerate(rows, 1))
    try:
        raw = judge(PROMPT.format(items=text, prompt=prompt[:3000],
                                  diff=provenance.working_diff(cwd)[:6000] or "(no diff)",
                                  reply=reply[:2500]))
    except Exception:
        return {}
    labels = parse_labels(raw, len(rows))
    result: dict[str, str] = {}
    for n, label in labels.items():
        item = rows[n - 1]
        meta = item.get("meta") or {}
        usage = dict(meta.get("usage") or {})
        usage[label] = int(usage.get(label, 0)) + 1
        patch: dict = {"usage": usage}
        if usage.get("CONTRADICTED", 0) >= CHALLENGE_AT \
                and usage.get("CONTRADICTED", 0) > usage.get("USED", 0):
            patch["challenged"] = True
        items.set_meta(conn, item["id"], patch)
        result[item["id"]] = label
    try:                                      # audit trail: who judged what, when
        with open(spool.repo_dir(cwd) / "usage_judgements.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "epoch": epoch, "prompt": prompt[:80],
                                 "labels": result}) + "\n")
    except OSError:
        pass
    return result


def report(conn, repo_key: str | None) -> list[dict]:
    """Per-item usage, most-delivered first."""
    out = []
    for it in items.active(conn, repo_key):
        u = (it.get("meta") or {}).get("usage") or {}
        total = sum(int(v) for v in u.values())
        if total:
            out.append({"id": it["id"], "text": it["text"], "kind": it["kind"], "usage": u,
                        "total": total, "challenged": bool((it.get("meta") or {}).get("challenged"))})
    return sorted(out, key=lambda r: -r["total"])


_WORDS = {"USED": "used", "IGNORED": "ignored", "IRRELEVANT": "not relevant", "CONTRADICTED": "contradicted",
          "OVERRIDDEN": "overridden"}


def words(tally: dict) -> str:
    """A usage tally in plain words: "used 3 times, contradicted twice" ("" when every count is zero)."""
    def times(n: int) -> str:
        return {1: "once", 2: "twice"}.get(n, f"{n} times")
    return ", ".join(f"{_WORDS[k]} {times(int(tally[k]))}" for k in LABELS if tally.get(k))
