"""Harness integrations — the only place Reverie knows harness names.

Each subpackage owns one coding harness's files, config keys and hook
schema. The core (`reverie.install`, `reverie.uninstall`, `reverie.cli`,
`reverie.hooks`) imports this registry, never a subpackage directly.

Core rule: no harness name appears in ``reverie/*.py`` outside this
directory. Adding a harness is one package plus one line in ``INTEGRATIONS``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import Integration, by_key
from .claude import CLAUDE_CODE_COMMANDS
from .claude import INTEGRATION as CLAUDE
from .codex import CODEX_PROMPTS
from .codex import INTEGRATION as CODEX
from .cursor import CURSOR_COMMANDS
from .cursor import INTEGRATION as CURSOR
from .gemini import GEMINI_COMMANDS
from .gemini import INTEGRATION as GEMINI
from .opencode import INTEGRATION as OPENCODE
from .opencode import OPENCODE_COMMANDS

INTEGRATIONS: list[Integration] = [CLAUDE, CURSOR, OPENCODE, CODEX, GEMINI]

#: Back-compat name used by the installer/uninstaller.
AGENTS: list[Integration] = INTEGRATIONS

_BY_KEY = {integration.key: integration for integration in INTEGRATIONS}


def get_integration(key: str) -> Integration | None:
    return _BY_KEY.get(key)


def configured_servers(home: Path):
    """Every server entry the installed harnesses would actually launch."""
    servers = []
    for integration in INTEGRATIONS:
        server = integration.configured_server(Path(home))
        if server is not None:
            servers.append(server)
    return servers


# ---------------------------------------------------------------------------
# the generic SessionStart hook handler (no harness names beyond argparse)
# ---------------------------------------------------------------------------

def hook_handler(executable: str | None = None, which=shutil.which) -> dict:
    """The generic ``reverie hook session-start`` handler dict.

    Claude's settings schema is the canonical form the installer has always
    written; the Claude package adds it to ``settings.json``. Kept here so
    the core installer does not own a harness-shaped constant.
    """
    from ..install import hook_handler as _handler
    return _handler(executable=executable, which=which)


def _is_reverie_hook_handler(handler: object) -> bool:
    """Whether ``handler`` is one of Reverie's generic hook handlers."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    args = handler.get("args")
    if isinstance(args, list):
        if args[-2:] in (["hook", "session-start"], ["capture-turn"]):
            return True
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    return command.endswith(" hook session-start") or \
        command.endswith(" capture-turn")


def capture_turn_main(argv: list[str] | None = None) -> int:
    """``reverie capture-turn`` — Claude's Stop hook dispatches here."""
    from .claude.capture import main
    return main(argv)


def install_plan(key: str, home: Path, reverie_bin: str | None = None,
                 *, reverie_home: Path | None = None, which=shutil.which):
    integration = get_integration(key)
    if integration is None:
        return []
    return integration.install(home, reverie_bin, True,
                               reverie_home=reverie_home, which=which)


__all__ = [
    "AGENTS",
    "CLAUDE_CODE_COMMANDS",
    "CODEX_PROMPTS",
    "CURSOR_COMMANDS",
    "GEMINI_COMMANDS",
    "INTEGRATIONS",
    "OPENCODE_COMMANDS",
    "Integration",
    "_is_reverie_hook_handler",
    "by_key",
    "capture_turn_main",
    "configured_servers",
    "get_integration",
    "hook_handler",
    "install_plan",
]
