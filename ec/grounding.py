"""EC Grounding Verification — do ECU grounding references still exist?

When code changes, ECUs grounded in that code may become stale: a repo-scoped
ECU saying "the auth module uses JWT with RS256" is stale once the auth module
is refactored to OAuth. Grounding verification (design doc Section 3, D35;
spec §1.3.5, §11.6 Defence 3, §21) checks whether an ECU's grounding
references still exist in the live repository and deprecates the ECU when its
grounding has disappeared (§21.1, scope-dependent).

What is checked, per canonical ECU grounded in the active repo:

1. **File existence** — every ``grounding.files`` path must exist on disk.
2. **Symbol existence (best-effort)** — every ``grounding.symbols`` name must
   appear (substring search) in the files that remain. This is a heuristic,
   not an AST parse; false positives err toward keeping the ECU alive, false
   negatives deprecate a valid ECU — acceptable because deprecation is
   reversible and surfaced at the review gate (§3.6).
3. **Commit-hash staleness (informational)** — if ``grounding.commit_hash``
   is more than ``COMMIT_STALENESS_COMMITS`` behind HEAD, the ECU is flagged
   in metadata (``metadata.stale_commit``). Commit distance NEVER deprecates:
   the code might not have changed in the relevant files.

Deprecation rules (§3.4 / §21.1):

- ``engineering`` / ``domain``: never deprecated by code changes.
- ``organization`` / ``project``: deprecated only when ALL grounding files
  are gone (the whole context changed, not one file).
- ``repo`` / ``module`` / ``subsystem``: deprecated when ANY grounding file
  is deleted or ANY symbol vanished (code-specific ECUs die with their code).

On deprecation (§3.5) the ECU's status becomes ``deprecated`` (confidence
freezes automatically via FROZEN_STATUSES — no extra write), its edges are
preserved for audit, and ECUs that ``depends_on`` it are challenged
(``metadata.last_challenged`` set) so they get re-evaluated (§17.4 flag #3).
Nothing is ever deleted.

The Maintainer invokes this through ``maintainer.task_grounding``; runs are
throttled per repo to ``maintainer.grounding_check_interval_hours`` (72h,
D35). Repos not on disk are skipped — their files cannot be verified.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ec.grounding")

#: Informational staleness flag: more than this many commits between the ECU's
#: grounding.commit_hash and HEAD marks ``metadata.stale_commit``. Never a
#: deprecation trigger by itself (design doc §3.3). Module constant rather
#: than a config key — the design doc specifies no config change for it.
COMMIT_STALENESS_COMMITS = 50

#: Scopes never deprecated by grounding loss (universal principles).
NEVER_DEPRECATE_SCOPES = frozenset({"engineering", "domain"})

#: Scopes deprecated only when ALL grounding files are gone.
ALL_FILES_GONE_SCOPES = frozenset({"organization", "project"})

#: Scopes deprecated when ANY grounding file/symbol disappears.
ANY_GONE_SCOPES = frozenset({"repo", "module", "subsystem"})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# filesystem + git primitives
# ---------------------------------------------------------------------------

def file_exists(repo_path: str, rel_path: str) -> bool:
    """Does grounding file ``rel_path`` exist under ``repo_path``?

    Absolute paths are honoured as-is (the Extractor normally emits
    repo-relative paths); symlinks count as existing."""
    p = Path(rel_path)
    if not p.is_absolute():
        p = Path(repo_path) / p
    return p.exists()


def symbol_present(repo_path: str, rel_path: str, symbol: str) -> bool:
    """Best-effort symbol check (§3.3): substring search of ``symbol`` in the
    file text.

    Structured names like ``TokenManager::refresh`` rarely occur verbatim in
    source, so if the full name is absent each separator-separated fragment
    (>= 4 chars, longest first) is tried too — finding ANY fragment counts as
    present. Errs toward keeping the ECU alive. Unreadable/missing files
    return True (cannot establish absence → keep)."""
    p = Path(rel_path)
    if not p.is_absolute():
        p = Path(repo_path) / p
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True  # unreadable ≠ absent — keep the ECU
    if symbol in text:
        return True
    import re
    fragments = sorted(
        {f for f in re.split(r"[::\.\(\)\s]+", symbol)
         if len(f) >= 4},
        key=len, reverse=True,
    )
    return any(f in text for f in fragments)


def commit_distance(repo_path: str, commit_hash: str) -> int | None:
    """Commits between ``commit_hash`` and HEAD (exclusive..HEAD), or None
    when git fails / the hash is unknown (e.g. shallow clones, rebases)."""
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--count", f"{commit_hash}..HEAD"],
            cwd=str(repo_path), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("commit_distance failed in %s: %s", repo_path, exc)
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# scope-dependent deprecation decision (§3.4 / §21.1)
# ---------------------------------------------------------------------------

def should_deprecate(
    scope_level: str,
    missing_files: list[str],
    missing_symbols: list[str],
    total_files: int,
) -> bool:
    """§3.4 deprecation trigger for one ECU's verification outcome.

    - engineering/domain: never (principles survive code deletion, §21.4).
    - organization/project: only when ALL grounding files are gone — partial
      change means the context survives.
    - repo/module/subsystem: any missing file OR any vanished symbol.
    """
    if scope_level in NEVER_DEPRECATE_SCOPES:
        return False
    if scope_level in ALL_FILES_GONE_SCOPES:
        return total_files > 0 and len(missing_files) == total_files
    if scope_level in ANY_GONE_SCOPES:
        return bool(missing_files or missing_symbols)
    raise ValueError(
        f"unknown scope_level {scope_level!r}; expected one of "
        f"{sorted(NEVER_DEPRECATE_SCOPES | ALL_FILES_GONE_SCOPES | ANY_GONE_SCOPES)}"
    )


# ---------------------------------------------------------------------------
# per-ECU verdict
# ---------------------------------------------------------------------------

@dataclass
class GroundingVerdict:
    """Verification outcome for one canonical ECU."""

    ecu_id: str
    scope_level: str
    missing_files: list[str] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)
    stale_commit: int | None = None      # commits behind HEAD (>0 → flag)
    deprecated: bool = False
    challenged_dependents: list[str] = field(default_factory=list)

    def to_details(self) -> dict:
        """JSON-safe entry for the maintenance_log details payload (§3.5)."""
        return {
            "ecu": self.ecu_id,
            "scope": self.scope_level,
            "missing_files": self.missing_files,
            "missing_symbols": self.missing_symbols,
            "stale_commit_behind": self.stale_commit,
            "deprecated": self.deprecated,
            "challenged_dependents": self.challenged_dependents,
        }


def evaluate_ecu(ecu: dict, repo_path: str, now: datetime | None = None,
                 commit_staleness_limit: int = COMMIT_STALENESS_COMMITS,
                 ) -> GroundingVerdict:
    """Check one ECU's grounding references against the live repo.

    Pure inspection + decision — NO writes. Files are checked first; symbols
    are searched only across the files that still exist (a deleted file
    already proves stale grounding for code scopes, and its content cannot be
    searched). An ECU with no grounding files at all yields no evidence
    either way and is kept alive."""
    grounding = ecu.get("grounding") or {}
    files = [f for f in (grounding.get("files") or []) if f]
    symbols = [s for s in (grounding.get("symbols") or []) if s]
    verdict = GroundingVerdict(
        ecu_id=ecu["id"], scope_level=ecu["scope"]["level"]
    )

    if not files:
        # Nothing to anchor to: cannot establish staleness → keep.
        return verdict

    existing_files = [f for f in files if file_exists(repo_path, f)]
    verdict.missing_files = [f for f in files if f not in existing_files]

    for sym in symbols:
        if not any(symbol_present(repo_path, f, sym) for f in existing_files):
            verdict.missing_symbols.append(sym)

    commit_hash = grounding.get("commit_hash")
    if commit_hash:
        distance = commit_distance(repo_path, commit_hash)
        if distance is not None and distance > commit_staleness_limit:
            verdict.stale_commit = distance     # informational only

    verdict.deprecated = should_deprecate(
        ecu["scope"]["level"], verdict.missing_files,
        verdict.missing_symbols, len(files),
    )
    return verdict


# ---------------------------------------------------------------------------
# dependents challenge (§3.5 step 4 / §21.3 / §17.4 flag #3)
# ---------------------------------------------------------------------------

def challenge_dependents(brain, ecu_id: str, now: datetime | None = None) -> list[str]:
    """Mark direct dependents of a deprecated ECU as ``challenged``.

    Sources of inbound depends_on edges are re-evaluation candidates. Already
    terminal statuses (superseded/deprecated/archived) are left alone;
    open_question stays parked (it is already flagged for user resolution);
    ``metadata.last_challenged`` is stamped on transitions. Returns the ids
    newly challenged. Direct dependents only — transitive propagation is the
    Diffuser's §15.7 job, not grounding's."""
    now_iso = _utcnow_iso() if now is None else now.isoformat()
    challenged: list[str] = []
    for edge in brain.get_edges_to(ecu_id):
        if edge["type"] != "depends_on":
            continue
        dependent_id = edge["source_id"]
        dependent = brain.get_ecu(dependent_id)
        if dependent is None or dependent["status"] in (
            "superseded", "deprecated", "archived", "open_question",
        ):
            continue
        if dependent["status"] != "challenged":
            brain.update_ecu_status(dependent_id, "challenged")
            brain.update_ecu_metadata(dependent_id, last_challenged=now_iso)
            challenged.append(dependent_id)
    return challenged


# ---------------------------------------------------------------------------
# the verifier — orchestrates one throttled run over one repo
# ---------------------------------------------------------------------------

class GroundingVerifier:
    """Verifies canonical ECUs grounded in one repo against the working tree
    (D35), deprecating those whose grounding disappeared (§21.1).

    Only ECUs whose ``grounding.repo_path`` matches are examined; frozen
    statuses (superseded/deprecated/archived/open_question) are skipped —
    terminal/parked beliefs are not re-litigated by hygiene. Runs are
    throttled per repo via maintenance_state
    (``grounding_last_check:<repo_path>`` vs
    ``maintainer.grounding_check_interval_hours``).
    """

    #: statuses eligible for grounding-driven deprecation
    ELIGIBLE_STATUSES = ("active", "challenged")

    def __init__(self, brain, config, repo_path: str | None,
                 now: datetime | None = None):
        self.brain = brain
        self.config = config
        self.repo_path = repo_path
        self.now = now or datetime.now(timezone.utc)

    # -- throttle ----------------------------------------------------------

    def state_key(self) -> str:
        from .maintainer import STATE_GROUNDING_CHECK_PREFIX
        return f"{STATE_GROUNDING_CHECK_PREFIX}{self.repo_path}"

    def _last_check_at(self):
        value = self.brain.get_maintenance_state(self.state_key())
        if value is None:
            return None
        from .maintainer import _parse_iso
        return _parse_iso(value)

    def is_throttled(self) -> bool:
        """True when the last check for THIS repo is within the configured
        interval (D35: at most one verification per interval per repo)."""
        last = self._last_check_at()
        if last is None:
            return False
        hours = (
            (self.now - last).total_seconds() / 3600.0
        )
        return hours < float(
            self.config.maintainer.grounding_check_interval_hours
        )

    # -- run -----------------------------------------------------------------

    def run(self):
        """Execute one verification pass. Returns a maintainer.TaskResult.

        Skips (without stamping the throttle clock) when there is no repo
        path, the repo is not on disk, or the per-repo interval has not
        elapsed. On an actual run the throttle clock is stamped even when
        zero ECUs were checked — the check happened."""
        from .maintainer import TaskResult

        if not self.repo_path:
            return TaskResult("grounding", 0, {"skipped": "no_repo"})
        if not Path(self.repo_path).exists():
            return TaskResult("grounding", 0,
                              {"skipped": "repo_not_on_disk",
                               "repo": self.repo_path})
        if self.is_throttled():
            return TaskResult("grounding", 0,
                              {"skipped": "throttled",
                               "last_check": self._last_check_at().isoformat()})

        candidates = [
            ecu for status in self.ELIGIBLE_STATUSES
            for ecu in self.brain.list_ecus_for_repo(
                self.repo_path, statuses=[status])
        ]

        verdicts: list[GroundingVerdict] = []
        for ecu in candidates:
            verdict = evaluate_ecu(ecu, self.repo_path, now=self.now)
            if verdict.deprecated:
                self._deprecate(ecu, verdict)
            elif verdict.stale_commit:
                # informational §3.3 flag — never a status change
                self.brain.update_ecu_metadata(
                    ecu["id"], stale_commit=verdict.stale_commit)
            verdicts.append(verdict)

        self.brain.set_maintenance_state(
            self.state_key(), self.now.isoformat())
        deprecated = [v.to_details() for v in verdicts if v.deprecated]
        affected = len(deprecated) + sum(
            len(v.challenged_dependents) for v in verdicts)
        return TaskResult("grounding", affected, {
            "repo": self.repo_path,
            "checked": len(candidates),
            "deprecated": deprecated,
            "flagged_stale_commits": [
                {"ecu": v.ecu_id, "commits_behind": v.stale_commit}
                for v in verdicts
                if v.stale_commit and not v.deprecated
            ],
        })

    def _deprecate(self, ecu: dict, verdict: GroundingVerdict) -> None:
        """§3.5: status → deprecated (confidence freezes via FROZEN_STATUSES),
        edges preserved, direct dependents challenged."""
        self.brain.update_ecu_status(ecu["id"], "deprecated")
        verdict.challenged_dependents = challenge_dependents(
            self.brain, ecu["id"], now=self.now)
