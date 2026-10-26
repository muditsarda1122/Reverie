"""Neutral launcher resolution for ``reverie`` commands.

Callers ask for an executable and argument vector; they wrap the result in
their own configuration format. This module knows nothing about any harness.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

REVERIE_CONSOLE_SCRIPT = "reverie"
REVERIE_CLI_MODULE = "reverie.cli"

#: argv tail for the session-start hook (recognised by idempotent install and
#: surgical uninstall). The list keeps the words separate — no harness here.
HOOK_ARGS_TAIL = ["hook", "session-start"]


def _is_pip_installed() -> bool:
    """True when ``reverie`` is pip-installed (editable counts)."""
    try:
        from importlib import metadata
        metadata.version("reverie")
        return True
    except Exception:            # not installed / metadata unreadable
        return False


def _script_candidates(script: str, executable: str | None = None) -> list[Path]:
    """Absolute candidate locations of a pip-generated console script."""
    exe_dir = Path(executable or sys.executable).parent
    if os.name == "nt":
        return [
            exe_dir / f"{script}.exe",
            exe_dir / f"{script}.cmd",
            exe_dir / "Scripts" / f"{script}.exe",
            exe_dir / "Scripts" / f"{script}.cmd",
            exe_dir / script,
            exe_dir / "Scripts" / script,
        ]
    return [exe_dir / script]


def _script_path(script: str, executable: str | None = None,
                 which=shutil.which) -> str | None:
    """Absolute path of an installed console script, or None."""
    for candidate in _script_candidates(script, executable):
        if candidate.is_file():
            return str(candidate)
    found = which(script)
    if found:
        return str(Path(found).resolve())
    return None


def _reverie_script_path(executable: str | None = None,
                         which=shutil.which) -> str | None:
    """Absolute path of the installed ``reverie`` CLI console script, or None."""
    return _script_path(REVERIE_CONSOLE_SCRIPT, executable, which)


def reverie_command(tail: list[str] | None = None,
                    executable: str | None = None,
                    which=shutil.which) -> tuple[str, list[str]]:
    """``(command, args)`` that launch ``reverie <tail>`` from any cwd.

    pip install → the ABSOLUTE ``reverie`` console script beside the running
    interpreter, with the tail as exec-form args (harnesses launch commands
    outside the user's virtualenv, so a bare ``reverie`` would not resolve).
    No script found → the running interpreter with ``-m reverie.cli``.
    Development checkout → the project venv's interpreter with PYTHONPATH
    pointing at the project root, returned as a shell-form command with no
    args (the dev fallback needs the env prefix).
    """
    tail = list(tail or [])
    if _is_pip_installed():
        script = _reverie_script_path(executable, which)
        if script is not None:
            return script, tail
        return executable or sys.executable, ["-m", REVERIE_CLI_MODULE, *tail]
    root = Path(__file__).resolve().parent.parent
    python: str | None = None
    for candidate in (root / ".venv" / "bin" / "python",          # POSIX venv
                      root / ".venv" / "Scripts" / "python.exe"):  # Windows venv
        if candidate.is_file():
            python = str(candidate)
            break
    if python is None:   # no project venv — the running interpreter has reverie
        python = sys.executable
    argv = " ".join(shlex.quote(part)
                    for part in (python, "-m", REVERIE_CLI_MODULE, *tail))
    return f"PYTHONPATH={shlex.quote(str(root))} {argv}", []


#: Descriptive alias for callers that just want "how do I launch it".
launch_command = reverie_command
