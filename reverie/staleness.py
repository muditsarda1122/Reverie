"""Staleness worker — keep memory true as the code moves.

Git is only a TRIGGER here (history is not mined for memories). For each new
commit since the last check:

  1. file/symbol trigger: only memories whose anchors the commit touched are
     rechecked (the lab: 98 judge calls instead of 396, same recall);
  2. tests first: the suite is run once at HEAD and a failure is passed to the
     judge as evidence (tests independently caught 4 of 5 invalidating
     mutations for free);
  3. a judge reads the diff and says STILL_TRUE / CONTRADICTED / UNRELATED.

A CONTRADICTED memory is never silently rewritten or retired. It is flagged
(`meta.stale`, shown with a warning wherever it is delivered) and a
SUPERSEDING CANDIDATE is stored (status 'candidate', not delivered). The user
resolves it: `reverie stale accept <candidate>` or `reverie stale keep <id>`.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field

from . import deliver, items, spool

log = logging.getLogger("reverie.staleness")

PROMPT = """A coding agent's long-term memory contains this entry about a codebase:
MEMORY: {mem}

A new commit just landed:
COMMIT: {msg}
{tests}DIFF:
{diff}

Is the memory still true after this commit? Answer on the first line with one word: STILL_TRUE, CONTRADICTED (the commit makes the memory's claim false or its stated rationale/convention obsolete), or UNRELATED (the commit does not touch what the memory is about). Then a colon and a short reason.
If and only if CONTRADICTED, add a second line: UPDATED: <one sentence stating what is true now, or UNKNOWN>."""


@dataclass
class Flag:
    memory_id: str
    commit: str
    message: str
    reason: str
    updated: str | None = None
    candidate_id: str | None = None


@dataclass
class Report:
    commits: int = 0
    triggered: int = 0            # (commit, memory) pairs reaching the judge
    possible: int = 0             # commits x memories (what a naive worker pays)
    flags: list[Flag] = field(default_factory=list)
    tests: str = "skipped"


def _commit_files(root: str, sha: str) -> set[str]:
    out = spool._git(["show", "--name-only", "--format=", sha], root)
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def _commit_diff(root: str, sha: str) -> str:
    return spool._git(["show", "--format=", "-U2", sha], root, timeout=30)


def _touches(it: dict, files: set[str], diff: str) -> bool:
    a = it.get("anchor") or {}
    for f in a.get("files") or []:
        f = deliver._norm(f)
        if any(f == g or g.endswith("/" + f) or f.endswith("/" + g) for g in files):
            return True
    changed = "\n".join(ln for ln in diff.splitlines()
                        if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
    for sym in a.get("symbols") or []:
        part = sym.split(".")[-1]
        if len(part) >= 3 and re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(part)}(?![A-Za-z0-9_])", changed):
            return True
    return False


def run_tests(root: str, cfg) -> str:
    """'passed' | 'failed: <tail>' | 'skipped'. Runs once, at HEAD."""
    s = cfg.stale
    if not s.run_tests:
        return "skipped"
    cmd = s.test_command.split() if s.test_command else (
        [sys.executable, "-m", "pytest", "-q", "-x"]
        if os.path.isdir(os.path.join(root, "tests")) else [])
    if not cmd:
        return "skipped"
    if spool._git(["status", "--porcelain"], root):
        return "skipped (uncommitted changes: tests would not reflect a commit)"
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                           timeout=int(s.test_timeout))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"skipped ({type(exc).__name__})"
    if p.returncode == 0:
        return "passed"
    tail = (p.stdout or p.stderr).strip().splitlines()[-3:]
    return "failed: " + " | ".join(tail)[:300]


def default_judge(cfg):
    """The product judge for diffs: classification model, enough tokens for a
    reason plus the UPDATED line."""
    from .dedupe import llm_judge
    return llm_judge(cfg, max_tokens=300)


def commits_since(root: str, last: str | None) -> list[tuple[str, str]]:
    """[(sha, subject)] oldest first, after ``last`` (or only HEAD if never
    checked: a fresh repo is baselined, not replayed)."""
    rng = f"{last}..HEAD" if last and spool._git(["cat-file", "-t", last], root) \
        else "HEAD~0..HEAD"
    if rng == "HEAD~0..HEAD":
        return []                      # first run: baseline at HEAD, check nothing
    out = spool._git(["log", "--reverse", "--format=%H %s", rng], root, timeout=30)
    return [tuple(ln.split(" ", 1)) if " " in ln else (ln, "")  # type: ignore[misc]
            for ln in out.splitlines() if ln.strip()]


def head(root: str) -> str:
    return spool._git(["rev-parse", "HEAD"], root)


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT last_commit FROM stale_state WHERE repo_key=?",
                       (key,)).fetchone()
    return row["last_commit"] if row else None


def set_state(conn: sqlite3.Connection, key: str, commit: str) -> None:
    conn.execute("INSERT INTO stale_state (repo_key,last_commit,checked_at) VALUES (?,?,?) "
                 "ON CONFLICT(repo_key) DO UPDATE SET last_commit=excluded.last_commit,"
                 "checked_at=excluded.checked_at", (key, commit, items._now()))
    conn.commit()


def parse_verdict(raw: str) -> tuple[str, str, str | None]:
    first = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    word = re.match(r"\W*(STILL_TRUE|CONTRADICTED|UNRELATED)", first.upper())
    verdict = word.group(1) if word else "UNRELATED"
    reason = first.split(":", 1)[1].strip() if ":" in first else first
    upd = re.search(r"^\s*UPDATED:\s*(.+)$", raw or "", re.M | re.I)
    updated = upd.group(1).strip() if upd and upd.group(1).strip().upper() != "UNKNOWN" else None
    return verdict, reason[:300], updated


def check(conn: sqlite3.Connection, root: str, cfg, judge, *,
          since: str | None = None, baseline_only: bool = False,
          tests: str | None = None) -> Report:
    """Recheck memory against the commits since the last check."""
    key = deliver.repo_key(root)
    report = Report()
    last = since or get_state(conn, key)
    if last is None or baseline_only:
        set_state(conn, key, head(root))
        return report
    mem = [m for m in deliver.memory(conn, key, root)
           if m.get("status", "active") == "active"
           and not (m.get("meta") or {}).get("stale")]
    commits = commits_since(root, last)
    report.commits = len(commits)
    if not commits:
        return report
    report.tests = run_tests(root, cfg) if tests is None else tests
    jobs = []
    for sha, subject in commits:
        files, diff = _commit_files(root, sha), _commit_diff(root, sha)
        report.possible += len(mem)
        for m in mem:
            if _touches(m, files, diff):
                jobs.append((sha, subject, diff[: int(cfg.stale.diff_chars)], m))
    report.triggered = len(jobs)
    test_note = ("TESTS AT HEAD: " + report.tests + "\n"
                 if report.tests.startswith("failed") else "")

    def run(job):
        sha, subject, diff, m = job
        try:
            raw = judge(PROMPT.format(mem=m["text"], msg=subject, diff=diff,
                                      tests=test_note))
        except Exception as exc:                 # a judge error never flags
            log.warning("stale judge failed for %s: %s", m["id"][:8], exc)
            return job, ("UNRELATED", "judge error", None)
        return job, parse_verdict(raw)

    with cf.ThreadPoolExecutor(max(1, int(cfg.stale.judge_workers))) as ex:
        results = list(ex.map(run, jobs))
    flagged: set[str] = set()
    for (sha, subject, _diff, m), (verdict, reason, updated) in results:
        if verdict != "CONTRADICTED" or m["id"] in flagged:
            continue
        flagged.add(m["id"])
        flag = Flag(m["id"], sha, subject, reason, updated)
        flag.candidate_id = _store_candidate(conn, m, flag, key)
        if m["origin"] == "item":
            items.set_meta(conn, m["id"], {"stale": {
                "commit": sha[:12], "message": subject, "reason": reason,
                "candidate": flag.candidate_id}})
        else:
            _flag_conclusion(conn, m["id"], flag)
        report.flags.append(flag)
    set_state(conn, key, head(root))
    return report


def _store_candidate(conn, m: dict, flag: Flag, key: str) -> str:
    """A superseding CANDIDATE: status 'candidate', never delivered."""
    text = flag.updated or (f"(needs rewrite) {m['text']}  [contradicted by "
                            f"commit {flag.commit[:8]}: {flag.reason}]")
    return items.add(
        conn, text=text, kind=m["kind"] if m["kind"] in items.KINDS else "decision",
        source="agent_inferred", repo_key=key,
        anchor_=m.get("anchor") or items.anchor(),
        author="agent:staleness-worker", level="repo",
        grounding_commit=flag.commit, status="candidate",
        evidence=f"commit {flag.commit[:12]}: {flag.message}",
        meta={"candidate_for": m["id"], "reason": flag.reason,
              "candidate_for_origin": m["origin"]})


def _flag_conclusion(conn, ecu_id: str, flag: Flag) -> None:
    """A legacy conclusion keeps its row; the flag goes in its own metadata."""
    row = conn.execute("SELECT metadata_json FROM ecus WHERE id=?", (ecu_id,)).fetchone()
    if row is None:
        return
    meta = json.loads(row["metadata_json"] or "{}")
    meta["stale"] = {"commit": flag.commit[:12], "message": flag.message,
                     "reason": flag.reason, "candidate": flag.candidate_id}
    conn.execute("UPDATE ecus SET metadata_json=? WHERE id=?", (json.dumps(meta), ecu_id))
    conn.commit()


def _mem_by_id(conn, mem_id: str) -> tuple[str, dict] | None:
    """('item'|'conclusion', row-ish) for an id prefix."""
    it = items.get(conn, mem_id)
    if it:
        return "item", it
    rows = conn.execute("SELECT id, metadata_json FROM ecus WHERE id LIKE ?",
                        (mem_id + "%",)).fetchall()
    if len(rows) == 1:
        return "conclusion", {"id": rows[0]["id"],
                              "meta": json.loads(rows[0]["metadata_json"] or "{}")}
    return None


def accept(conn, candidate_id: str) -> str:
    """The candidate becomes the active memory; the old one is superseded."""
    cand = items.get(conn, candidate_id)
    if not cand or cand["status"] != "candidate":
        return f"No pending candidate matches {candidate_id!r}."
    old_id = cand["meta"].get("candidate_for")
    items.set_status(conn, cand["id"], "active")
    found = _mem_by_id(conn, old_id) if old_id else None
    if found and found[0] == "item":
        items.set_status(conn, old_id, "superseded")
        items.set_meta(conn, cand["id"], {"supersedes": old_id})
    elif found:
        conn.execute("UPDATE ecus SET status='superseded' WHERE id=?", (old_id,))
        conn.commit()
    return f"Accepted [{cand['id'][:8]}]; the older memory [{(old_id or '')[:8]}] is superseded."


def keep(conn, memory_id: str) -> str:
    """Keep the old memory: clear its stale flag and drop its candidate."""
    found = _mem_by_id(conn, memory_id)
    if not found:
        return f"No memory matches {memory_id!r}."
    kind, row = found
    cand_id = (row["meta"].get("stale") or {}).get("candidate")
    if kind == "item":
        items.set_meta(conn, row["id"], {"stale": None})
    else:
        meta = dict(row["meta"])
        meta.pop("stale", None)
        conn.execute("UPDATE ecus SET metadata_json=? WHERE id=?", (json.dumps(meta), row["id"]))
        conn.commit()
    if cand_id:
        items.set_status(conn, cand_id, "undone")
    return f"Kept [{row['id'][:8]}]; stale flag cleared."


def pending(conn, key: str | None = None) -> list[dict]:
    q = "SELECT * FROM items WHERE status='candidate'"
    args: list = []
    if key is not None:
        q += " AND repo_key = ?"
        args.append(key)
    return [items._row(r) for r in conn.execute(q + " ORDER BY rowid", args)]
