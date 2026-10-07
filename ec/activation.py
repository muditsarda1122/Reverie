"""Spreading activation (Spec §9/§12) — session-scoped, time-decaying.

§12.1: when ECUs are retrieved, their direct network neighbours (via any
edge type, both directions, cross-brain) get a transient activation boost.
§12.2: boosts are session-specific — a new session starts with a clean
slate; a resumed session restores its scores and decays them for the break
(§6.6). §12.4: ``activation[ECU] = base_boost × decay_factor^hop_distance``
and ``activation *= exp(-activation_decay_rate × elapsed_hours)``.
§12.5: scores are normalized at read time by dividing by the maximum score
(divide_by_max) — the Experiment-2 anti-anchoring fix that bounds the
ranking formula's activation term to [0, w_activation].

Scores are stored RAW between reads (like the verified reference
implementation); normalization happens only at read time. Per-ECU
timestamps drive the exponential time decay, so the same mechanism covers
within-session quiet periods (§11.10) and cross-break resume (§6.6).

Spreading implements the spec formula exactly — hop-h boost =
``base_boost × decay_factor**hop`` from the current retrieval's seeds,
max-merged across seeds/paths and with existing scores — rather than the
reference's "spread from every warm ECU each hop" loop (documented
deviation D12; both are bounded identically by divide_by_max).

Mode-aware spread (§11.9: "in debugging mode, activation spreads more
aggressively through contradicts edges; in architecture, depends_on") uses
a capped multiplier — preferred edges decay slower, never amplify (D9).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from .config import get_config

log = logging.getLogger("ec.activation")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> datetime:
    """Parse ISO 8601 timestamps (tolerant of a trailing 'Z')."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ActivationState:
    """Per-session spreading-activation state (§12.2).

    ``_scores`` maps ecu_id -> (raw_score, last_updated). ECU ids may belong
    to either brain — activation is stored per-session, not per-brain
    (§11.10 cross-brain activation).
    """

    def __init__(self, session_id: str, config=None):
        self.session_id = session_id
        self.config = config or get_config()
        self._scores: dict[str, tuple[float, datetime]] = {}

    def __len__(self) -> int:
        return len(self._scores)

    def __contains__(self, ecu_id: str) -> bool:
        return ecu_id in self._scores

    # ------------------------------------------------------------------
    # read side
    # ------------------------------------------------------------------

    def get(self, ecu_id: str) -> float:
        """Raw activation score (0.0 if the ECU is cold)."""
        return self._scores.get(ecu_id, (0.0, None))[0]

    def normalized_scores(self) -> dict[str, float]:
        """§12.5 divide_by_max normalization, bounded to [0, 1].

        If all scores are zero (no prior retrieval this session),
        normalization is skipped and everything stays 0.0.
        """
        if not self._scores:
            return {}
        max_score = max(score for score, _ in self._scores.values())
        if max_score <= 0.0:
            return {ecu_id: 0.0 for ecu_id in self._scores}
        return {
            ecu_id: score / max_score for ecu_id, (score, _) in self._scores.items()
        }

    def normalized(self, ecu_id: str) -> float:
        """Normalized score for one ECU — what the ranking formula consumes."""
        return self.normalized_scores().get(ecu_id, 0.0)

    # ------------------------------------------------------------------
    # dynamics
    # ------------------------------------------------------------------

    def apply_time_decay(self, now: datetime | None = None) -> None:
        """§12.4/§6.6: score *= exp(-activation_decay_rate × elapsed_hours).

        Each ECU decays against its own last-updated timestamp and is
        restamped, so repeated calls are idempotent over the same instant.
        """
        now = now or _utcnow()
        rate = self.config.activation.activation_decay_rate  # per hour
        for ecu_id, (score, updated_at) in list(self._scores.items()):
            elapsed_h = (now - updated_at).total_seconds() / 3600.0
            if elapsed_h <= 0.0:
                continue
            self._scores[ecu_id] = (score * math.exp(-rate * elapsed_h), now)

    def spread(
        self,
        brain,
        retrieved_ids: list[str],
        mode: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """§12.1/§12.4 spreading activation after a retrieval.

        Retrieved ECUs accumulate the full ``base_boost`` (repeated
        retrievals keep warming them). Neighbours receive
        ``base_boost × decay_factor**hop`` for hop = 1..max_hops along a
        BFS frontier (hop_distance = shortest path from the retrieved set),
        max-merged across seeds, paths, and existing scores.

        Mode-aware spread (§11.9, D9): boosts crossing the mode's preferred
        edge type (debugging→contradicts, architecture→depends_on) are
        multiplied by ``mode_spread_multiplier``, capped at one hop less of
        decay — preferred edges cool slower, never heat up.

        Returns a diagnostic dict of the boosts applied.
        """
        now = now or _utcnow()
        cfg = self.config.activation
        base, decay, max_hops = cfg.base_boost, cfg.decay_factor, cfg.max_hops
        preferred_edge = cfg.mode_edge_preference.get(mode) if mode else None
        mult = cfg.mode_spread_multiplier

        seeds = list(dict.fromkeys(retrieved_ids))  # dedupe, keep order
        for ecu_id in seeds:
            current, _ = self._scores.get(ecu_id, (0.0, None))
            self._scores[ecu_id] = (current + base, now)

        best: dict[str, float] = {}  # ecu_id -> best candidate boost this spread
        frontier = set(seeds)
        visited = set(seeds)  # seeds are never their own neighbours
        for hop in range(1, max_hops + 1):
            hop_boost = base * (decay ** hop)
            preferred_cap = base * (decay ** (hop - 1))  # one hop less of decay
            next_frontier: set[str] = set()
            for src in frontier:
                for nb in brain.get_neighbourhood(src, session_id=self.session_id):
                    nid = nb["other_id"]
                    if nid in visited:
                        continue
                    boost = hop_boost
                    if preferred_edge and nb["type"] == preferred_edge and mult > 1.0:
                        boost = min(preferred_cap, hop_boost * mult)
                    if boost > best.get(nid, 0.0):
                        best[nid] = boost
                    next_frontier.add(nid)
            visited |= next_frontier
            frontier = next_frontier

        for nid, boost in best.items():
            current, _ = self._scores.get(nid, (0.0, None))
            self._scores[nid] = (max(current, boost), now)

        return {"seeds": seeds, "boosted": best}

    # ------------------------------------------------------------------
    # persistence (§6.6)
    # ------------------------------------------------------------------

    def save(self, brain) -> None:
        """Persist scores with their timestamps (§6.6 'what persists')."""
        brain.save_activation(
            self.session_id,
            {
                ecu_id: (score, updated_at.isoformat())
                for ecu_id, (score, updated_at) in self._scores.items()
            },
        )

    @classmethod
    def load(
        cls,
        brain,
        session_id: str,
        config=None,
        now: datetime | None = None,
    ) -> "ActivationState":
        """Restore a session's activation state and decay it for the break.

        §6.6 resume: load scores + timestamps, then apply
        ``exp(-rate × elapsed)`` — a 5-minute break barely changes them, a
        15-hour break decays them significantly. No new-vs-resumed
        distinction needed.
        """
        state = cls(session_id, config=config)
        for ecu_id, (score, updated_at) in brain.load_activation(session_id).items():
            state._scores[ecu_id] = (score, _parse_iso(updated_at))
        state.apply_time_decay(now=now)
        return state
