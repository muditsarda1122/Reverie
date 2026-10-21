"""The small, uniform contract every harness integration exposes.

The core installer and uninstaller never know what a harness's config looks
like: they ask the registry, then call these four methods. A new harness is
one package implementing this contract — no core edits.

``install`` and ``uninstall`` are *planners*: they return what would change
(``PlannedWrite`` / ``PlannedRemoval``) and never touch disk. The core
``reverie install`` / ``reverie uninstall`` commands apply or print those
plans. ``dry_run`` is accepted for callers that prefer the contract's own
switch; the core always plans first and applies second, so integrations may
ignore it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from .common import ConfiguredServer, PlannedRemoval, PlannedWrite


class Integration:
    """One coding harness's integration with Reverie."""

    #: Stable machine key (also the CLI/config name), e.g. ``"claude_code"``.
    key: str = ""
    #: Human name shown by the installer, e.g. ``"Claude Code"``.
    name: str = ""
    #: Executable used only for detection (None → marker-file detection only).
    binary: str | None = None
    #: Path relative to the user's home that marks an installed harness.
    marker: str | None = None
    #: What to tell the user after install, e.g. "Restart Claude Code".
    next_step: str = ""

    def detect(self, home: Path, which=shutil.which) -> bool:
        """Whether this harness is installed on the machine."""
        marker = getattr(self, "marker", None)
        if marker and (Path(home) / marker).exists():
            return True
        binary = getattr(self, "binary", None)
        return bool(binary and which(binary))

    #: Back-compat spelling used by the original installer loop.
    def detected(self, home: Path, which=shutil.which) -> bool:
        return self.detect(home, which)

    def install(self, home: Path, reverie_bin: str | None = None,
                dry_run: bool = False, *,
                reverie_home: Path | None = None,
                which=shutil.which) -> list[PlannedWrite]:
        """Compute every file write this harness needs."""
        raise NotImplementedError

    def uninstall(self, home: Path, dry_run: bool = False, *,
                  reverie_home: Path | None = None,
                  manifest: dict[str, str] | None = None
                  ) -> list[PlannedRemoval]:
        """Compute every reverse step for this harness."""
        raise NotImplementedError

    def plan(self, home: Path, reverie_home: Path | None = None
             ) -> list[PlannedWrite]:
        """Alias for the common read-only planning path."""
        return self.install(home, None, True, reverie_home=reverie_home)

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        """The server entry this harness will actually launch, or None."""
        return None


def by_key(integrations: Sequence[Integration], key: str) -> Integration | None:
    for integration in integrations:
        if integration.key == key:
            return integration
    return None
