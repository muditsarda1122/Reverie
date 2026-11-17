"""Find every config directory (profile) of each supported harness.

A machine can hold several: ``CLAUDE_CONFIG_DIR`` pointing at ``~/.claude-work``,
a second ``~/.claude-*``, ``CODEX_HOME``, a relocated ``XDG_CONFIG_HOME``. The
installer must not guess which one the user's agent reads (it did once: hooks
went to ``~/.claude`` while Claude read ``~/.claude-work``, so no Reverie hook had
ever fired). It lists what exists, says which one the environment points at, and
asks. STDLIB ONLY.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Profile:
    harness: str          # integration key: claude_code | codex | opencode | cursor
    path: Path
    via: str              # "env CLAUDE_CONFIG_DIR" | "default" | "found by name"
    active: bool          # the environment of this shell points at it

    def label(self) -> str:
        return f"{self.path}  ({self.via}{', active in this shell' if self.active else ''})"


def _uniq(paths: list[tuple[Path, str, bool]], harness: str) -> list[Profile]:
    seen: set[str] = set()
    out: list[Profile] = []
    for p, via, active in paths:
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(Profile(harness, p, via, active))
    return out


def discover(home: Path | None = None, environ: Mapping[str, str] | None = None
             ) -> dict[str, list[Profile]]:
    """-> {harness key: [Profile, ...]} for directories that exist (the env-pointed
    one is listed even if it does not exist yet)."""
    home = Path(home) if home else Path.home()
    env = environ if environ is not None else os.environ
    out: dict[str, list[Profile]] = {}

    found: list[tuple[Path, str, bool]] = []
    if env.get("CLAUDE_CONFIG_DIR"):
        found.append((Path(env["CLAUDE_CONFIG_DIR"]).expanduser(), "env CLAUDE_CONFIG_DIR", True))
    for g in sorted(glob.glob(str(home / ".claude*"))):
        p = Path(g)
        if p.is_dir() and (p / "settings.json").exists() or (p / "projects").is_dir():
            found.append((p, "default" if p.name == ".claude" else "found by name", False))
    out["claude_code"] = _uniq(found, "claude_code")

    found = []
    if env.get("CODEX_HOME"):
        found.append((Path(env["CODEX_HOME"]).expanduser(), "env CODEX_HOME", True))
    if (home / ".codex").is_dir():
        found.append((home / ".codex", "default", False))
    out["codex"] = _uniq(found, "codex")

    found = []
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        found.append((Path(xdg).expanduser() / "opencode", "env XDG_CONFIG_HOME", True))
    if (home / ".config" / "opencode").is_dir():
        found.append((home / ".config" / "opencode", "default", False))
    out["opencode"] = _uniq(found, "opencode")

    found = []
    if (home / ".cursor").is_dir():
        found.append((home / ".cursor", "default", False))
    out["cursor"] = _uniq(found, "cursor")
    return out


def codex_dir(home: Path, environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    return Path(env["CODEX_HOME"]).expanduser() if env.get("CODEX_HOME") else Path(home) / ".codex"


def opencode_dir(home: Path, environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    if env.get("XDG_CONFIG_HOME"):
        return Path(env["XDG_CONFIG_HOME"]).expanduser() / "opencode"
    return Path(home) / ".config" / "opencode"


ENV_FOR = {"claude_code": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
