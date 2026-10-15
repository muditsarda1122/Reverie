"""Reverie configuration — every tunable constant in one place (Spec Section 15).

Defaults live here in code. If ``~/.reverie/config.yaml`` (or ``$REVERIE_HOME/config.yaml``)
exists it is deep-merged over the defaults, so users can override any value
without touching code.

REVERIE_HOME resolution order:
    1. ``REVERIE_HOME`` environment variable
    2. ``~/.reverie``

Deferred sections (clustering) are included so the config surface matches
Section 15 exactly. The maintainer block became live code in Phase 6;
clustering parameters are read once Phase 9 lands.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from . import providers as _providers

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def reverie_home() -> Path:
    """Root directory for the global Reverie brain. One DB for all projects (§28.1)."""
    return Path(os.environ.get("REVERIE_HOME", "~/.reverie")).expanduser()


#: This Reverie version's schema level (feat/flow). Stored in the database as
#: ``PRAGMA user_version`` (raised, never lowered, on open). Exports record
#: it; imports refuse files written by a NEWER schema.
SCHEMA_VERSION = 1


def default_db_path() -> Path:
    return reverie_home() / "reverie.db"


def default_config_path() -> Path:
    return reverie_home() / "config.yaml"


# ---------------------------------------------------------------------------
# Defaults — Spec Section 15 (+ §11.7 retrieval block, §11.9 mode params,
# §11.11 scope proximity, all of which the spec says belong in config)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "ecu": {
        # Decorative (§15): documents the id scheme; UUID4 is hardcoded in
        # brain.py and this key is never read by code.
        "id_format": "uuid4",
    },

    # -- Confidence (§5.9 priors, §11 updates, §15 thresholds) --------------
    "confidence": {
        # Two-dimensional priors: base_prior (by source_type) x scope_modifier.
        # The extractor prompt emits source_type values: session | debugging |
        # implementation | planning | review | architectural_reasoning.
        # Section 15 names the review prior "code_review" — alias handled in
        # get_base_prior(). "session" (generic interaction) has no spec prior;
        # interpolated to the observation prior (0.45) — documented deviation.
        "base_priors": {
            "debugging": 0.70,
            "implementation": 0.65,
            "code_review": 0.60,
            "architectural_reasoning": 0.50,
            "observation": 0.45,
            "planning": 0.35,
        },
        # Aliases for extractor-emitted source_types not named in Section 15.
        "base_prior_aliases": {
            "review": "code_review",
            "session": "observation",
        },
        "scope_multipliers": {
            "engineering": 1.15,      # capped at prior_cap
            "domain": 1.10,           # capped at prior_cap
            "organization": 1.05,     # capped at prior_cap
            "project": 1.00,
            "repo": 0.90,
            "module": 0.80,
            "subsystem": 0.75,
        },
        "prior_cap": 0.95,
        "prior_floor": 0.05,
        # Multi-source corroboration bump (§5.9)
        "corroboration_bump": 0.05,       # per independent source
        "corroboration_bump_cap": 0.15,   # max total bump (3 sources)
        # Bayesian update weights — symmetric (§11.8)
        "w_support": 1.0,
        "w_contradict": 1.0,
        # Thresholds
        "theta_supersede": 0.3,           # below this, supersession can trigger
        "theta_dep_reevaluate": 0.4,      # below this, dependents are challenged
        "theta_contradiction_flag": 0.7,  # both sides above this -> flag to user
        # Scope-dependent decay rates per day (log-odds space) — Maintainer (DEFERRED)
        "lambda_decay": {
            "engineering": 0.001,
            "domain": 0.003,
            "organization": 0.004,
            "project": 0.005,
            "repo": 0.01,
            "module": 0.02,
            "subsystem": 0.03,
        },
        "alpha_retrieval": 0.05,          # retrieval reinforcement bump (DEFERRED)
        "max_propagation_depth": 2,
    },

    # -- Retrieval ranking (§11.5) ------------------------------------------
    "ranking": {
        "w_relevance": 0.6,             # primary signal — relevance is the gate
        "w_confidence": 0.15,           # secondary modifier — ranks, doesn't gate
        "w_activation": 0.10,           # tertiary — session priming (normalized [0,1])
        "w_network": 0.15,              # tertiary — structural hub boost
        "relevance_gate_threshold": 0.3,  # hard filter before ranking
        "edge_count_cap": 10,           # network_richness = min(edges, cap) / cap
    },

    # -- Retrieval (§11.7, §11.11) ------------------------------------------
    "retrieval": {
        "default_depth": 1,             # 0 = matches only, 1 = +direct neighbours
        "max_tokens": 4000,             # default budget when mode not specified
        "fallback_top_k": 5,            # if nothing passes the gate, return top-K
        "session_brain_trust_weight": 0.8,
        "canonical_brain_trust_weight": 1.0,
        "confidence_flag_threshold": 0.3,      # below -> "uncertain" flag
        "confidence_very_low_threshold": 0.2,  # below -> "very uncertain" flag
        # Mode-aware reweighting bonuses (§11.9) — magnitudes from the
        # verified Experiment-2 reference implementation (D11).
        "mode_prioritize_bonus": 0.05,
        "mode_bias_bonus": 0.03,
        # Scope proximity multiplier (§11.11) — retrieval searches UP the
        # hierarchy; closer scopes get a bigger multiplier. Never a filter.
        "scope_proximity": {
            "subsystem": 1.0,
            "module": 0.85,
            "repo": 0.7,
            "project": 0.55,
            "organization": 0.4,
            "domain": 0.25,
            "engineering": 0.1,
        },
    },

    # -- Spreading activation (§9) ------------------------------------------
    "activation": {
        "base_boost": 0.5,
        "decay_factor": 0.5,                # per hop
        "max_hops": 2,                      # default 2-3, TBD through experimentation
        "activation_decay_rate": 0.1,       # per hour within session
        "normalization": "divide_by_max",   # bound activation term to [0, w_activation]
        # Mode-aware spread (§11.9): boosts crossing the mode's preferred
        # edge type decay slower (x multiplier, capped at one hop less of
        # decay — never amplifies). Spec gives no parameters (D9).
        "mode_edge_preference": {
            "debugging": "contradicts",
            "architecture": "depends_on",
        },
        "mode_spread_multiplier": 1.5,      # 1.0 disables mode-aware spread
    },

    # -- Full Diffuser (§6, §11.8) ------------------------------------------
    "diffuser": {
        "relevance_threshold": 0.6,     # min semantic similarity for edge creation
    },

    # -- Lightweight Diffusion, Session Brain (§4.4) -------------------------
    "lightweight_diffusion": {
        "alpha_light": 0.1,             # simple confidence adjustment strength
        "similarity_threshold": 0.7,    # min similarity for edge creation
    },

    # -- LLM (§15 llm block) -------------------------------------------------
    # `provider` selects an entry from reverie/providers.py; fields the user does
    # not set explicitly are filled from that entry (custom/unknown = today's
    # behaviour: every field is used as written below).
    "llm": {
        "provider": "opencode_zen",
        "model": "claude-haiku-4-5",
        "base_url": "https://opencode.ai/zen/v1",
        "api_key_env": "OPENCODE_ZEN_API_KEY",
        # Overrides the model-name wire-format rule (D48) when set:
        # "anthropic" | "openai". None = derive from the model name.
        "wire_format": None,
        # True/False force the reasoning-model request shape (no temperature,
        # max_completion_tokens instead of max_tokens); None = auto-detect by
        # a conservative model-name rule (reverie.llm._is_openai_reasoning_model).
        "openai_reasoning": None,
        # True/False force omitting the sampling parameters for Claude
        # models; None = auto-detect by a conservative model-name rule
        # (reverie.llm._claude_rejects_sampling). Anthropic models released
        # after Claude Opus 4.6 (Opus 4.7/4.8/5/5.5, Sonnet 5/5.5, Haiku
        # 5.5) return HTTP 400 for any non-default temperature/top_p/top_k;
        # the documented fix is to omit all three. For those models on the
        # Anthropic wire format the request also carries the cheapest valid
        # thinking configuration (thinking disabled/between_tools at effort
        # "low" — adaptive thinking is on by default and its tokens are
        # billed as output). Source checked 2026-10-15:
        # https://platform.claude.com/docs/en/build-with-claude/thinking
        # https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide
        "claude_no_sampling": None,
        # Retry policy for transient failures (429 / 5xx / timeouts).
        "retry_attempts": 3,            # total attempts (1 = no retry)
        "retry_backoff": 0.5,           # seconds before first retry
        "retry_backoff_cap": 8.0,       # ceiling on a single backoff (excl. jitter)
        # Per-task model overrides; every None falls back to `model` above.
        "models": {
            "extraction": None,
            "classification": None,
            "mode_detection": None,
        },
        "timeout": 60,                  # seconds per LLM call
        "temperature": 0.3,             # extraction / diffusion
        "temperature_mode_detection": 0.0,
        "max_tokens": 4096,
    },

    # -- Embedding model (set at init, never changed) ------------------------
    "embedding": {
        "model": "all-MiniLM-L6-v2",
        "dimensions": 384,
    },

    # -- Mode-aware retrieval parameters (§11.9, critical detail #7) --------
    "modes": {
        "debugging": {
            "depth": 0,
            "max_tokens": 2000,
            "prioritize": ["contradictions"],   # edge-type slot, not conclusion_type
            "bias": "debugging",                # provenance.source_type bias
        },
        "architecture": {
            "depth": 1,
            "max_tokens": 6000,
            "prioritize": ["decision", "pattern", "constraint"],
            "bias": "architectural_reasoning",
        },
        "implementation": {
            "depth": 1,
            "max_tokens": 3000,
            "prioritize": ["pattern", "constraint"],
            "bias": "implementation",
        },
        "investigation": {
            "depth": 1,
            "max_tokens": 4000,
            "prioritize": ["pattern", "decision"],
            "bias": "none",
        },
        "planning": {
            "depth": 1,
            "max_tokens": 5000,
            "prioritize": ["constraint", "decision"],
            "bias": "planning",
        },
    },

    # -- Review gate (§5) ----------------------------------------------------
    "review_gate": {
        # Decorative: the trigger is implicit (review decisions via `reverie review`,
        # plus on-demand apply_review_decisions); this key is never read.
        "trigger": "session_close | on_demand",
        # Decorative: grouping behaviour is governed by grouping_strategy
        # below; this key is never read by code.
        "batch_grouping": True,
        # D36: candidate grouping strategy — "scope" groups by scope_path
        # (works at any brain size); "cluster" groups by HDBSCAN cognitive
        # cluster, falling back to scope when no clusters exist.
        "grouping_strategy": "scope",
    },

    # -- Review queue (feat/flow, D57): cross-session review -----------------
    "review": {
        # Candidates unreviewed this many days become review_status='expired'
        # (kept, never deleted; still usable in session memory; revived only
        # via `reverie review --expired`). 0 disables expiry.
        "expire_after_days": 21,
        # reverie_summary nudge: one counts-only line when the unreviewed
        # backlog exceeds this many candidates...
        "nudge_threshold": 10,
        # ...or the oldest unreviewed candidate is older than this many days.
        "nudge_age_days": 5,
        # Page size for `reverie review` and reverie_review_list (never a filter —
        # "more" is always available).
        "page_size": 10,
    },

    # -- Contradiction handling (§12) ----------------------------------------
    "contradiction": {
        # Redundant with confidence.theta_contradiction_flag — that is what
        # code actually reads; kept for §15 config-surface fidelity only.
        "flag_threshold": 0.7,
        "always_flag_scopes": ["engineering", "domain"],
        # Wired (design doc §11): review_gate._notify_propagation gates its
        # depends_on-chain notifications on this. False silences the notice;
        # §15.7 propagation itself always runs.
        "notify_on_dependent": True,
        "competing_hypothesis_persistence": {   # days; null = indefinite
            "engineering": None,
            "domain": None,
            "organization": 75,
            "project": 90,
            "repo": 60,
            "module": 30,
            "subsystem": 20,
        },
        "open_question_retrieval_weight": 0.5,
    },

    # -- DEFERRED: clustering (not built in v2) ------------------------------
    "clustering": {
        "algorithm": "hdbscan",
        "min_cluster_size": 3,
        "min_samples": 2,
        "clustering_threshold": 100,
        "stability_threshold": 0.6,
        "cluster_statuses": ["active", "challenged", "open_question"],
    },

    # -- Cognition Maintainer (Phase 6) --------------------------------------
    "maintainer": {
        "ecu_threshold": 10,
        "time_threshold_hours": 6,
        "check_interval_minutes": 5,
        "grounding_check_interval_hours": 72,
        "enabled": True,    # spec §15 default; set False to disable for debugging
    },
}


# ---------------------------------------------------------------------------
# Controlled vocabularies (Spec Section 1)
# ---------------------------------------------------------------------------

CONCLUSION_TYPES = (
    "implication", "constraint", "principle", "decision",
    "observation", "pattern", "invariant", "trade-off",
)

SCOPE_LEVELS = (
    "engineering", "domain", "organization", "project",
    "repo", "module", "subsystem",
)

ECU_STATUSES = (
    "active", "challenged", "superseded",
    "deprecated", "open_question", "archived",
)

# Statuses that participate in retrieval (§1.2 status semantics)
RETRIEVABLE_STATUSES = ("active", "challenged", "open_question")

EDGE_TYPES = ("supports", "contradicts", "depends_on", "supersedes")

MODES = ("debugging", "architecture", "implementation", "investigation", "planning")

# feat/flow (D55): repo enablement states (repos.state) + the first-time
# question surface. "not_now" is recorded as asked_at on an undecided repo.
REPO_STATES = ("undecided", "enabled", "disabled")

# feat/flow (D57): channels that can apply a review decision (audit trail).
REVIEW_CHANNELS = ("tui", "agent", "cli")


# ---------------------------------------------------------------------------
# Config access
# ---------------------------------------------------------------------------

class AttrDict(dict):
    """Dict with recursive dot-access: cfg.ranking.w_relevance."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"no config key: {name!r}") from None


def _to_attrdict(obj: Any) -> Any:
    if isinstance(obj, dict):
        return AttrDict({k: _to_attrdict(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_attrdict(v) for v in obj]
    return obj


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


#: config ``llm`` key -> Provider attribute it is filled from.
_PROVIDER_FILLED_KEYS = {
    "base_url": "base_url",
    "model": "default_model",
    "api_key_env": "api_key_env",
    "wire_format": "wire_format",
}


def _resolve_provider(merged: dict, user_cfg: dict) -> dict:
    """Fill unset ``llm`` fields from the registry when the user names a provider.

    ``user_cfg`` is the raw user mapping, so "explicitly set" means "the key
    is present in the user's YAML" — anything the user wrote wins over the
    registry. ``custom`` and unknown provider names change nothing (the
    merged block is used as-is, i.e. today's behaviour).
    """
    user_llm = user_cfg.get("llm") if isinstance(user_cfg, dict) else None
    if not isinstance(user_llm, dict):
        return merged
    entry = _providers.get(user_llm.get("provider"))
    if entry is None or entry.key == "custom":
        return merged
    llm = merged.setdefault("llm", {})
    for key, attr in _PROVIDER_FILLED_KEYS.items():
        if key in user_llm:
            continue
        value = getattr(entry, attr)
        if key == "wire_format":
            if value is not None:      # None = keep model-name routing
                llm[key] = value
        else:
            llm[key] = value           # may be None (e.g. Ollama's key env)
    return merged


def load_config(path: str | os.PathLike | None = None) -> AttrDict:
    """Load config: DEFAULT_CONFIG deep-merged with the YAML file if it exists."""
    cfg_path = Path(path) if path else default_config_path()
    merged = copy.deepcopy(DEFAULT_CONFIG)
    user_cfg: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh) or {}
        if not isinstance(user_cfg, dict):
            raise ValueError(f"config file {cfg_path} must contain a YAML mapping")
        merged = _deep_merge(merged, user_cfg)
    merged = _resolve_provider(merged, user_cfg)
    return _to_attrdict(merged)


_config_cache: AttrDict | None = None


def get_config(refresh: bool = False) -> AttrDict:
    """Process-wide config singleton (loads ~/.reverie/config.yaml once)."""
    global _config_cache
    if _config_cache is None or refresh:
        _config_cache = load_config()
    return _config_cache


def get_base_prior(source_type: str, config: AttrDict | None = None) -> float:
    """Base confidence prior for a source_type, resolving aliases (§5.9)."""
    cfg = config or get_config()
    priors = cfg.confidence.base_priors
    aliases = cfg.confidence.base_prior_aliases
    key = aliases.get(source_type, source_type)
    if key not in priors:
        raise ValueError(
            f"unknown source_type {source_type!r}; known: {sorted(priors)} "
            f"(+ aliases {sorted(aliases)})"
        )
    return priors[key]
