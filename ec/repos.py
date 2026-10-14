"""Repo enablement (feat/flow, D55) — three states per repo, stored in the
global brain, never inside the repo.

States (``repos.state``): ``undecided`` (default) | ``enabled`` | ``disabled``.
A repo can also be *asked* (``asked_at`` set) while still undecided — the
record of a "not now" answer, so the first-time question is not repeated.

Repo identity (D58): the repo's **git common directory** — the absolute,
resolved path of ``git rev-parse --path-format=absolute --git-common-dir``.
A worktree shares its common directory with the main checkout, so a
worktree counts as the same repo for enablement. Consequences, chosen
deliberately:

- **Moved or re-cloned on the same machine:** the common-directory path
  changes, which is a *new* identity. On first use we try to rematch by the
  repo's origin remote URL (recorded in ``repos.url``): exactly one known
  repo with the same URL adopts its state and re-keys onto the new path
  (D58). Ambiguous matches (several known repos share the URL) and
  key-less remotes (e.g. a local clone with no origin) start fresh as
  ``undecided`` — a plain ``ec enable`` inside the repo resolves it.
- **Session identity is separate:** sessions stay tied to the worktree
  toplevel path + branch (§28.5), because grounding verification and commit
  provenance must run against the *actual working tree* the agent is
  editing. Two worktrees therefore get separate sessions even when they
  hold the same branch checked out — each has its own files.

Disabled means fully off for that repo: no capture, no retrieval, nothing
injected. Existing data is kept; re-enabling resumes with everything
intact.

This module is policy; storage lives in ``Brain`` (repos table), session
resolution in ``ec/session.py``, MCP gating in ``ec/mcp_server.py``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .brain import Brain
from .session import _git

#: The exact first-time question (feat/flow). Shown to the user once per
#: conversation; the agent records the answer via ``ec_set_repo_state``.
FIRST_TIME_QUESTION = (
    "Reverie isn't on for this repo. Turn it on? (yes / not now / never)"
)

_GIT_TIMEOUT = 10


@dataclass
class RepoInfo:
    """Everything one tool call needs to know about where it is running."""

    repo_path: str            # worktree toplevel — session identity (§28.5)
    branch: str
    repo_key: str             # git common directory — enablement identity
    url: str | None = None    # origin remote URL (rematching, D58)


def remote_url(repo_path: str | Path) -> str | None:
    """The repo's origin remote URL, or None (no origin / not a repo)."""
    try:
        url = _git(["remote", "get-url", "origin"], Path(repo_path))
    except Exception:                       # no origin — never fatal
        return None
    return url or None


def common_dir(toplevel: str | Path) -> str:
    """The resolved git common directory for a work tree (D58 identity).

    Falls back to ``<toplevel>/.git`` (then the toplevel itself) when git
    cannot answer — old git without ``--path-format``, unusual layouts."""
    try:
        out = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"],
                   Path(toplevel))
        if out:
            return str(Path(out).resolve())
    except Exception:
        pass
    dotgit = Path(toplevel) / ".git"
    return str(dotgit) if dotgit.exists() else str(toplevel)


def resolve_repo(cwd: str | Path | None = None) -> RepoInfo:
    """Locate the repo for this call: worktree path, branch, identity, URL.

    ``EC_REPO_PATH`` + ``EC_BRANCH`` env overrides win when both are set
    (test/launch escape hatch, §28.5): the env repo path *is* the identity
    (no git is consulted). Otherwise git detection applies.
    """
    env_repo = os.environ.get("EC_REPO_PATH")
    env_branch = os.environ.get("EC_BRANCH")
    if env_repo and env_branch:
        repo_path = str(Path(env_repo).expanduser().resolve())
        return RepoInfo(repo_path=repo_path, branch=env_branch,
                        repo_key=repo_path, url=None)
    from .session import detect_repo_branch
    repo_path, branch = detect_repo_branch(cwd)
    return RepoInfo(repo_path=repo_path, branch=branch,
                    repo_key=common_dir(repo_path), url=remote_url(repo_path))


# ---------------------------------------------------------------------------
# state access (with move/re-clone rematching, D58)
# ---------------------------------------------------------------------------

def resolve_repo_state(brain: Brain, info: RepoInfo) -> dict | None:
    """The repo's enablement row, or None when unknown (undecided).

    Unknown identity + exactly one previously-seen repo with the same
    origin URL → rematch: adopt that state and re-key it onto the current
    identity (a moved or re-cloned repo keeps its answer, D58)."""
    row = brain.get_repo(info.repo_key)
    if row is not None:
        if info.url and row["url"] != info.url:
            row = brain.upsert_repo(info.repo_key, url=info.url)
        return row
    if info.url:
        matches = [r for r in brain.list_repos() if r["url"] == info.url]
        if len(matches) == 1:
            return brain.upsert_repo(
                info.repo_key, path=info.repo_path, url=info.url,
                rekey_from=matches[0]["repo_key"],
            )
    return None


def state_of(brain: Brain, info: RepoInfo) -> str:
    """``enabled`` | ``disabled`` | ``undecided`` for this repo."""
    row = resolve_repo_state(brain, info)
    return row["state"] if row else "undecided"


def set_repo_state(brain: Brain, info: RepoInfo, state: str) -> dict:
    """Switch a repo's state at any time, as often as the user likes."""
    return brain.upsert_repo(info.repo_key, path=info.repo_path,
                             url=info.url, state=state)


def mark_asked(brain: Brain, info: RepoInfo) -> None:
    """Record that the first-time question was surfaced ("not now", or any
    needs_consent response) — undecided-but-asked."""
    from .brain import _now_iso
    row = brain.get_repo(info.repo_key)
    if row is not None and row["asked_at"]:
        return                          # already asked — keep the first stamp
    brain.upsert_repo(info.repo_key, path=info.repo_path, url=info.url,
                      asked_at=_now_iso())


# ---------------------------------------------------------------------------
# branch existence + session archiving (feat/flow, D56)
# ---------------------------------------------------------------------------

def branch_exists(repo_path: str, branch: str) -> bool:
    """Whether the session's branch still exists locally.

    A detached session tracks its commit (``detached:<hash>``): it exists
    while the commit is reachable. Named branches are checked with
    ``git show-ref``. A missing repo path (deleted / moved checkout)
    counts as gone."""
    if not Path(repo_path).exists():
        return False
    try:
        if branch.startswith("detached:"):
            probe = branch[len("detached:"):]
            proc = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet",
                 f"{probe}^{{commit}}"],
                cwd=repo_path, capture_output=True, text=True,
                timeout=_GIT_TIMEOUT,
            )
        else:
            proc = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet",
                 f"refs/heads/{branch}"],
                cwd=repo_path, capture_output=True, text=True,
                timeout=_GIT_TIMEOUT,
            )
    except (OSError, subprocess.TimeoutExpired):
        return True        # cannot tell — never archive on a git failure
    return proc.returncode == 0


def archive_missing_branch_sessions(
    brain: Brain, repo_path: str, skip_branch: str | None = None
) -> list[str]:
    """Archive active sessions of ``repo_path`` whose branch is gone.

    Runs at session lookup (cheap: one git call per stale-looking session)
    — an archived session's unreviewed candidates stay in the review queue
    (D56). ``skip_branch`` is the branch just detected (known to exist)."""
    archived: list[str] = []
    for row in brain.list_sessions(repo_path=repo_path, status="active"):
        if row["branch"] == skip_branch:
            continue
        if branch_exists(repo_path, row["branch"]):
            continue
        if brain.archive_session(row["id"]):
            archived.append(row["id"])
    return archived
