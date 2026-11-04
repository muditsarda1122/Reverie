"""Transcript importers: read what harnesses already wrote, log it, remember it.

STDLIB ONLY at import time (extraction is delegated to a detached worker).

A harness keeps its own transcript (Claude/Codex: JSONL, opencode: SQLite,
Cursor: JSONL). Hooks only see a session while Reverie's hook is installed,
fires and is trusted; transcripts also cover headless, scripted and killed
sessions. So Reverie reads them lazily (at ``summary``/``resume``), through one
harness-agnostic core and one small adapter per harness:

    adapter.sources(paths, since)  -> Source(key, path, stamp)  per candidate
    adapter.read(source, state)    -> (list[Turn], new_state)   only NEW data

Rules the core enforces for every adapter:

* Raw transcripts stay where the harness keeps them. Reverie stores only the
  redacted prompt/answer log (``spool.append`` redacts BEFORE writing).
* A per-repo cursor (``import_state.json``: source key -> offset + the open,
  not-yet-answered prompt) means nothing is read twice.
* Prompts are logged as soon as they are seen (a killed session keeps them).
  The final answer of an exchange is logged when the next prompt arrives or
  the session has been idle for ``IDLE_SECS``.
* Records already in the log (live hooks wrote them) are not duplicated.
* Exchanges nobody has run through extraction yet go to a detached worker.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from .. import spool
from ..redact import redact

IDLE_SECS = 600                 # an unanswered prompt untouched this long was cut off
ANSWERED_IDLE_SECS = 60         # an answered one is final once the source is quiet this long
MAX_AGE_DAYS = 30               # first import looks back this far
EXTRACT_AGE_DAYS = 7            # only exchanges this recent are sent to extraction
MAX_EXTRACT = 8                 # per run: newest first
NO_REPLY = "(the session ended before the assistant replied)"


@dataclass
class Turn:
    role: str                   # "prompt" | "answer"
    text: str
    session: str
    t: str = ""                 # ISO time, "" = unknown (core fills file mtime)
    branch: str = ""
    cwd: str = ""


@dataclass
class Source:
    key: str                    # stable id for the cursor
    path: str
    stamp: float                # mtime / time_updated: unchanged stamp => skip
    harness: str = ""
    session: str = ""


class Adapter(Protocol):
    name: str

    def sources(self, paths: list[str], since: float) -> Iterable[Source]: ...

    def read(self, src: Source, state: dict) -> tuple[list[Turn], dict]: ...


# ---------------------------------------------------------------------------
# helpers shared by adapters
# ---------------------------------------------------------------------------

def slug_of(path: str) -> str:
    """Claude/Cursor project-directory slug for an absolute path."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def norm(text: str) -> str:
    return " ".join((text or "").split())


def digest(session: str, kind: str, text: str) -> str:
    return hashlib.sha256(
        f"{session}|{kind}|{norm(redact(text))[:2000]}".encode()).hexdigest()[:20]


def exchange_key(session: str, prompt: str) -> str:
    return digest(session, "x", prompt)


def worktree_paths(cwd: str | Path) -> list[str]:
    root = spool.repo_root(cwd)
    out = [root]
    listing = spool._git(["worktree", "list", "--porcelain"], root)
    for line in listing.splitlines():
        if line.startswith("worktree "):
            p = line[9:].strip()
            if p not in out:
                out.append(p)
    return out


def under(path: str, paths: list[str]) -> bool:
    p = os.path.realpath(path) if path else ""
    return any(p == q or p.startswith(q.rstrip("/") + "/")
               for q in (os.path.realpath(x) for x in paths))


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


def read_new_lines(path: str, offset: int) -> tuple[list[str], int]:
    """Complete new lines after ``offset`` and the offset just past them. A
    torn last line (no newline yet) is left for the next read."""
    try:
        size = os.path.getsize(path)
        if offset > size:            # file was replaced/truncated: start over
            offset = 0
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    lines = data[:end + 1].decode("utf-8", errors="replace").splitlines()
    return lines, offset + end + 1


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def _state_path(cwd: str | Path) -> Path:
    return spool.repo_dir(cwd) / "import_state.json"


def load_state(cwd: str | Path) -> dict:
    try:
        data = json.loads(_state_path(cwd).read_text())
        if isinstance(data, dict):
            data.setdefault("sources", {})
            data.setdefault("captured", [])
            for src in data["sources"].values():     # state written before redaction
                op = src.get("open") if isinstance(src, dict) else None
                if isinstance(op, dict):
                    for f in ("prompt", "answer"):
                        op[f] = redact(op.get(f) or "")
            return data
    except (OSError, ValueError):
        pass
    return {"sources": {}, "captured": []}


def save_state(cwd: str | Path, state: dict) -> None:
    state["captured"] = state["captured"][-5000:]
    path = _state_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def mark_captured(cwd: str | Path, session: str, prompt: str) -> None:
    """Record that an exchange went through extraction (live hook path)."""
    try:
        with _lock(cwd, block=True) as held:
            if not held:
                return
            st = load_state(cwd)
            k = exchange_key(session, prompt)
            if k not in st["captured"]:
                st["captured"].append(k)
                save_state(cwd, st)
    except Exception:
        pass


class _lock:
    def __init__(self, cwd, block=False):
        self.path = spool.repo_dir(cwd) / "import.lock"
        self.block = block
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | (0 if self.block else fcntl.LOCK_NB))
            return True
        except OSError:
            return False

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

@dataclass
class Result:
    sources: int = 0
    prompts: int = 0
    answers: int = 0
    queued: int = 0
    by_harness: dict = field(default_factory=dict)


def lazy(cwd: str | Path, *, finalize: bool = False) -> Result | None:
    """Import for ``summary`` / ``resume`` / session start. Honours a disabled
    repo (nothing is read) and ``REVERIE_NO_IMPORT=1``. Never raises."""
    try:
        if os.environ.get("REVERIE_NO_IMPORT"):
            return None
        from .. import deliver
        conn = deliver.open_db()
        if conn is not None:
            try:
                if deliver.is_disabled(conn, deliver.repo_key(cwd)):
                    return None
            finally:
                conn.close()
        return run(cwd, finalize=finalize)
    except Exception:
        return None


def adapters() -> list[Adapter]:
    from . import claude, codex, cursor, opencode
    return [claude.ADAPTER, codex.ADAPTER, opencode.ADAPTER, cursor.ADAPTER]


def _known(cwd: str | Path) -> set[str]:
    return {digest(r.get("session", ""), r["kind"], r.get("text", ""))
            for r in spool.read(cwd)}


def run(cwd: str | Path, *, adapters_: list[Adapter] | None = None,
        now: float | None = None, max_age_days: float = MAX_AGE_DAYS,
        extract: bool = True, finalize: bool = False) -> Result:
    """Import new transcript data for the repo containing ``cwd``. Never raises.

    ``finalize`` (a harness said the turn is over, e.g. opencode ``session.idle``)
    treats every open exchange as complete instead of waiting out the idle window."""
    res = Result()
    try:
        with _lock(cwd) as held:
            if not held:                      # another import is running
                return res
            _run(cwd, res, adapters_ if adapters_ is not None else adapters(),
                 now if now is not None else time.time(), max_age_days, extract,
                 finalize)
    except Exception:
        pass
    return res


def _run(cwd, res: Result, ads: list[Adapter], now: float, max_age_days: float,
         extract: bool, finalize: bool = False) -> None:
    paths = worktree_paths(cwd)
    since = now - max_age_days * 86400
    state = load_state(cwd)
    known = _known(cwd)
    queue: list[dict] = []
    default_branch = spool._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)

    def log(kind: str, turn: Turn, harness: str, t: str) -> None:
        d = digest(turn.session, kind, turn.text)
        if d in known:
            return
        known.add(d)
        if spool.append(cwd, kind, turn.text, session=turn.session,
                        branch=turn.branch or default_branch, harness=harness, t=t):
            if kind == "prompt":
                res.prompts += 1
            else:
                res.answers += 1
            res.by_harness[harness] = res.by_harness.get(harness, 0) + 1

    def close(op: dict, ended: bool) -> None:
        """An exchange is complete (answered) or its session was cut off."""
        reply = op.get("answer") or ""
        if reply:
            log("answer", Turn("answer", reply, op["session"], branch=op.get("branch", "")),
                op["harness"],
                op.get("t_answer") or op["t"])
        if not reply and not ended:
            return
        k = exchange_key(op["session"], op["prompt"])
        if k in state["captured"]:
            return
        if op["epoch"] >= now - EXTRACT_AGE_DAYS * 86400:
            queue.append({"key": k, "prompt": op["prompt"], "reply": reply or NO_REPLY,
                          "session": op["session"], "branch": op.get("branch", ""),
                          "epoch": op["epoch"]})
        else:
            state["captured"].append(k)     # too old to bother the LLM with

    for ad in ads:
        try:
            srcs = list(ad.sources(paths, since))
        except Exception:
            continue
        for src in srcs:
            st = state["sources"].setdefault(src.key, {})
            if st.get("stamp") == src.stamp and not st.get("open"):
                continue                       # unchanged and nothing pending
            res.sources += 1
            try:
                turns, newst = ad.read(src, st)
            except Exception:
                continue
            op = newst.pop("open", None)
            for turn in turns:
                if not norm(turn.text):
                    continue
                t = turn.t or iso(src.stamp)
                epoch = _epoch(t, src.stamp)
                if turn.role == "prompt":
                    if op:
                        close(op, ended=False)
                    log("prompt", turn, ad.name, t)
                    op = {"prompt": redact(turn.text), "session": turn.session or src.session,
                          "harness": ad.name, "t": t, "epoch": epoch,
                          "branch": turn.branch, "answer": ""}
                elif op:
                    op["answer"] = redact(turn.text)[:spool.ANSWER_CAP]
                    op["t_answer"] = t
            idle = finalize or (now - src.stamp) >= (
                ANSWERED_IDLE_SECS if op and op.get("answer") else IDLE_SECS)
            if op and idle:
                close(op, ended=True)          # session over / killed
                op = None
            newst["stamp"] = src.stamp
            if op:
                newst["open"] = op
            state["sources"][src.key] = newst
    save_state(cwd, state)
    res.queued = len(queue)
    if extract and queue:
        queue.sort(key=lambda e: e["epoch"], reverse=True)
        _extract(cwd, queue[:MAX_EXTRACT])


def _epoch(t: str, fallback: float) -> float:
    try:
        return time.mktime(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return fallback


def _extract(cwd, exchanges: list[dict]) -> None:
    """Hand exchanges to a detached worker (LLM calls must not block a hook)."""
    if os.environ.get("REVERIE_IMPORT_INLINE"):
        _extract_worker(str(cwd), exchanges)
        return
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "reverie.importers", "--extract", str(cwd)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(exchanges).encode())
        proc.stdin.close()
    except Exception:
        pass


def _extract_worker(cwd: str, exchanges: list[dict]) -> None:
    from .. import core_api
    from ..repos import resolve_repo
    for ex in exchanges:
        try:
            branch = ex.get("branch") or resolve_repo(cwd).branch
            out = core_api.capture_turn(cwd, branch, ex["prompt"], ex["reply"],
                                        timeout=120.0)
            if out.get("status") in ("ok", "skipped", "disabled"):
                mark_captured(cwd, ex["session"], ex["prompt"])
        except Exception:
            continue


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(argv) >= 2 and argv[0] == "--extract":
            _extract_worker(argv[1], json.loads(sys.stdin.read() or "[]"))
        elif argv and argv[0] == "--run":
            r = run(argv[1] if len(argv) > 1 else os.getcwd())
            print(f"imported: {r.prompts} prompt(s), {r.answers} answer(s) "
                  f"from {r.sources} source(s); {r.queued} exchange(s) queued "
                  f"for extraction {r.by_harness or ''}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":        # pragma: no cover
    raise SystemExit(main())
