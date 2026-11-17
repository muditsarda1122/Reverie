"""User-entered rules: plain markdown files, stored verbatim, always loaded.

    ~/.reverie/rules.md        personal; applies in every repo
    <repo>/.reverie/rules.md   team/repo; shared through git

The agent never edits these. Each rule is one markdown bullet (``- text``);
anything else in the file (headings, prose) is passed through untouched.
STDLIB ONLY (hooks load this on every session start).
"""

from __future__ import annotations

import os
from pathlib import Path

from .spool import repo_root, reverie_home

HEADER = "# Reverie rules\n\nOne rule per bullet. Edited by you, never by the agent.\n\n"


def personal_path() -> Path:
    return reverie_home() / "rules.md"


def repo_path(cwd: str | Path) -> Path:
    return Path(repo_root(cwd)) / ".reverie" / "rules.md"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def load(cwd: str | Path, *, hide_shared: bool = False) -> dict[str, str]:
    """``{"user": text, "repo": text}`` with empty strings when absent. With
    ``hide_shared`` a personal bullet that repeats a team rule in force is left out
    (delivery only: the file is never edited, and the bullet returns when the team
    rule goes away)."""
    user = _read(personal_path())
    if hide_shared and user:
        from . import context
        shared = context.shared_rule_norms(str(repo_root(cwd)))
        if shared:
            user = "\n".join(ln for ln in user.splitlines()
                             if not (ln.startswith("- ") and context.norm(ln[2:]) in shared))
    return {"user": user, "repo": _read(repo_path(cwd))}


def render(cwd: str | Path) -> str:
    """Both files as one block for injection (personal first)."""
    loaded = load(cwd, hide_shared=True)
    parts = []
    if loaded["user"].strip():
        parts.append("### Your personal rules\n" + loaded["user"].strip())
    if loaded["repo"].strip():
        parts.append("### This repo's rules (team)\n" + loaded["repo"].strip())
    return "\n\n".join(parts)


def add(text: str, *, level: str = "repo", cwd: str | Path = ".") -> Path:
    """Append ``text`` verbatim as a bullet to the personal or repo file."""
    text = " ".join(text.split())
    if not text:
        raise ValueError("rule text is empty")
    if level not in ("user", "repo"):
        raise ValueError("level must be 'user' or 'repo'")
    path = personal_path() if level == "user" else repo_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read(path)
    if not existing:
        existing = HEADER
    elif not existing.endswith("\n"):
        existing += "\n"
    if f"- {text}\n" in existing:
        return path                      # already there; keep it idempotent
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(existing + f"- {text}\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def bullets(text: str) -> list[str]:
    return [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")]
