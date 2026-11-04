"""Crash-safe per-repo prompt log (the "spool") and ``reverie resume``.

STDLIB ONLY. Hooks call this module on every user prompt, so it must start
fast and must never import the brain, numpy or torch.

What it stores, per repo, under ``<reverie home>/spool/<slug>-<hash>/``:

    log.jsonl   append-only, one JSON object per line:
                {"t": iso-time, "kind": "prompt"|"answer", "session": id,
                 "branch": name, "harness": name, "text": "..."}

User prompts are kept verbatim and never rewritten or summarised. Final
answers are kept (capped) as a secondary signal. Each record is one
``os.write`` on an ``O_APPEND`` descriptor followed by ``fsync``, so a crash
leaves at most a torn final line, which readers skip.

``reverie resume`` prints rules + the prompt log + git state, for any harness.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

from .redact import PLACEHOLDER, redact

ANSWER_CAP = 8000          # chars of a final answer kept in the log
PROMPT_CAP = 200_000       # sanity cap only; prompts are otherwise verbatim
RESUME_BUDGET_CHARS = 40_000

#: Compaction seam. A compactor takes the prompt records of one session and
#: returns the records to print. The default keeps everything: the log was
#: validated to 16 turns (3.3 KB) and user words are the highest-value signal.
#: A future compactor (e.g. summarise only the oldest turns) plugs in here.
Compactor = Callable[[list[dict]], list[dict]]


def reverie_home() -> Path:
    env = os.environ.get("REVERIE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".reverie"


def _git(args: list[str], cwd: str | Path, timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_root(cwd: str | Path) -> str:
    """Worktree toplevel for ``cwd`` (falls back to ``cwd``)."""
    return _git(["rev-parse", "--show-toplevel"], cwd) or str(Path(cwd).resolve())


def repo_dir(cwd: str | Path) -> Path:
    """Spool directory for the repo containing ``cwd``.

    Keyed on the git common dir so worktrees of one repo share a log."""
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    key = str(Path(common).resolve()) if common else repo_root(cwd)
    base = Path(key).parent.name if key.endswith(".git") else Path(key).name
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", base)[:40] or "repo"
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return reverie_home() / "spool" / f"{slug}-{digest}"


def append(cwd: str | Path, kind: str, text: str, *, session: str = "",
           branch: str | None = None, harness: str = "") -> bool:
    """Durably append one record. Never raises; returns success."""
    try:
        if kind not in ("prompt", "answer") or not (text or "").strip():
            return False
        cap = ANSWER_CAP if kind == "answer" else PROMPT_CAP
        text = redact(text)        # before the cap, so a secret is never cut in half
        directory = repo_dir(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "kind": kind,
            "session": session or "",
            "branch": branch if branch is not None
            else _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd),
            "harness": harness,
            "text": text[:cap],
        }
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        path = directory / "log.jsonl"
        try:                       # a crash can leave a torn last line;
            with open(path, "rb") as fh:   # start on a fresh line after it
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    line = b"\n" + line
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def read(cwd: str | Path) -> list[dict]:
    """All intact records, oldest first. Torn/garbled lines are skipped."""
    path = repo_dir(cwd) / "log.jsonl"
    out: list[dict] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("kind") in ("prompt", "answer"):
            out.append(rec)
    return out


def last_session(records: Iterable[dict]) -> str:
    """Session id of the most recent prompt ('' when ids are absent)."""
    last = ""
    for rec in records:
        if rec["kind"] == "prompt":
            last = rec.get("session", "")
    return last


def select(records: list[dict], *, session: str | None = None,
           all_sessions: bool = False) -> list[dict]:
    if all_sessions:
        return records
    wanted = last_session(records) if session is None else session
    return [r for r in records if r.get("session", "") == wanted]


# ---------------------------------------------------------------------------
# which session to resume
# ---------------------------------------------------------------------------

TITLE_CHARS = 90


def sessions(records: Iterable[dict]) -> list[dict]:
    """One summary per session, most recent first.

    ``[{session, branch, harness, first, last, prompts, title}]``. The title is
    the session's first prompt, one line, clipped. Records with no session id
    are grouped under ''."""
    by: dict[str, dict] = {}
    for rec in records:
        sid = rec.get("session", "")
        info = by.get(sid)
        if info is None:
            info = by[sid] = {"session": sid, "branch": rec.get("branch", ""),
                              "harness": rec.get("harness", ""),
                              "first": rec.get("t", ""), "last": rec.get("t", ""),
                              "prompts": 0, "title": ""}
        info["last"] = rec.get("t", info["last"])
        if rec.get("branch"):
            info["branch"] = rec["branch"]
        if rec["kind"] == "prompt":
            info["prompts"] += 1
            if not info["title"]:
                one = " ".join(rec.get("text", "").split())
                info["title"] = (one[:TITLE_CHARS - 1] + "…"
                                 if len(one) > TITLE_CHARS else one)
    return sorted((i for i in by.values() if i["prompts"]),
                  key=lambda i: i["last"], reverse=True)


def pick_session(records: list[dict], branch: str) -> tuple[str | None, list[dict]]:
    """-> (session id to resume or None when the user must choose, all sessions).

    One session in the repo, or exactly one on the current branch, is
    unambiguous. Several on the branch (or none on it but several elsewhere)
    is ambiguous: the caller lists them and asks."""
    allsess = sessions(records)
    if len(allsess) <= 1:
        return (allsess[0]["session"] if allsess else None), allsess
    on_branch = [i for i in allsess if i["branch"] == branch]
    if len(on_branch) == 1:
        return on_branch[0]["session"], allsess
    return None, allsess


def render_session_list(allsess: list[dict], branch: str) -> str:
    out = ["# Several earlier sessions exist for this repo", "",
           f"Current branch: {branch or '(unknown)'}. Ask the user which one "
           "to continue, then run `reverie resume --session <id>` (or "
           "`reverie resume --latest` for the most recent).", ""]
    for i in allsess[:10]:
        mark = "*" if i["branch"] == branch else " "
        out.append(f"{mark} {i['session'] or '(no id)'}  {i['last'][:16]}  "
                   f"{i['branch'] or '?'}  {i['harness'] or '?'}  "
                   f"{i['prompts']} prompt(s)  {i['title']}")
    if len(allsess) > 10:
        out.append(f"  (+{len(allsess) - 10} older)")
    out += ["", "* = on the current branch"]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# resume rendering
# ---------------------------------------------------------------------------

def git_state(cwd: str | Path) -> str:
    root = repo_root(cwd)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root) or "(unknown)"
    head = _git(["log", "-1", "--format=%h %s"], root)
    status = _git(["status", "--short"], root)
    stat = _git(["diff", "HEAD", "--stat"], root)
    recent = _git(["log", "-5", "--format=%h %s"], root)
    parts = [f"branch: {branch}", f"HEAD: {head or '(no commits)'}"]
    parts.append("working tree:\n" + (status if status else "  clean"))
    if stat:
        parts.append("uncommitted diff (git diff HEAD --stat):\n" + stat)
    if recent:
        parts.append("recent commits:\n" + recent)
    return "\n".join(parts)


def render_resume(cwd: str | Path, *, session: str | None = None,
                  all_sessions: bool = False, compactor: Compactor | None = None,
                  rules_text: str = "", latest: bool = False) -> str:
    everything = read(cwd)
    if session is None and not all_sessions and not latest:
        chosen, allsess = pick_session(
            everything, _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd))
        if chosen is None and len(allsess) > 1:
            return render_session_list(
                allsess, _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd))
        session = chosen
    records = select(everything, session=session, all_sessions=all_sessions)
    records = [{**r, "text": redact(r.get("text", ""))} for r in records]  # render pass
    prompts = [r for r in records if r["kind"] == "prompt"]
    if compactor is not None:
        prompts = compactor(prompts)
    out = ["# Resuming a previous session", ""]
    if rules_text.strip():
        out += ["## Rules (always apply)", rules_text.strip(), ""]
    if prompts:
        out += [
            "## What the user asked, verbatim and in order",
            "Every prompt below is the user's own words. The LAST one may not "
            "have been handled. Earlier ones may hold decisions meant for "
            "later steps; honour all of them.",
            "",
        ]
        for i, rec in enumerate(prompts, 1):
            out.append(f"[prompt {i}] {rec['text']}")
            out.append("")
    else:
        out += ["## What the user asked", "No prompt log exists for this repo "
                "yet (nothing was recorded).", ""]
    n_red = sum(r["text"].count(PLACEHOLDER) for r in prompts)
    if n_red:
        out += [f"Note: {n_red} secret value(s) were removed from the log "
                "([REDACTED...]). If the task needs one, prefer an environment "
                "variable that already exists; ask the user only if it does not.", ""]
    answers = [r for r in records if r["kind"] == "answer"]
    if answers:
        out += ["## The assistant's last reply (for context)",
                answers[-1]["text"][:2000], ""]
    out += ["## Git state now", git_state(cwd), "",
            "Verify against the working tree before acting; the log says what "
            "was asked, the tree says what exists."]
    text = "\n".join(out)
    if compactor is None and len(text) > RESUME_BUDGET_CHARS:
        text += (f"\n\n(note: this resume is {len(text)} chars, over the "
                 f"{RESUME_BUDGET_CHARS} budget; no compactor is configured)")
    return text


def main(argv: list[str] | None = None) -> int:
    """``python -m reverie.spool log prompt|answer TEXT`` — manual append, for
    harnesses with no hooks. Harness hooks use their own package's shim."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(argv) >= 3 and argv[0] == "log" and argv[1] in ("prompt", "answer"):
            append(os.getcwd(), argv[1], " ".join(argv[2:]), harness="manual")
    except Exception:
        pass
    return 0            # never fail the caller


if __name__ == "__main__":
    raise SystemExit(main())
