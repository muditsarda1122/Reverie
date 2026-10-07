"""Mode detection (Spec §11.9 'How mode is detected').

Five modes: debugging, architecture, implementation, investigation, planning.

The agent may pass `mode` explicitly to ec_query; when omitted, EC classifies
the query with a lightweight LLM call (temperature 0.0). Per §11.9, v2 may
fall back to keyword heuristics — used when no API key is set or the LLM
call fails, so mode detection never blocks retrieval.
"""

from __future__ import annotations

import logging
import re

from .config import MODES, get_config
from .llm import LLMError, api_key, call_llm

log = logging.getLogger("ec.mode_detection")

_DEFAULT_MODE = "investigation"  # broad exploratory default (§16.3)

_CLASSIFY_SYSTEM = (
    "Classify this engineering query into one of: debugging, architecture, "
    "implementation, investigation, planning. Respond with only the mode name."
)

# Keyword fallback lists (§11.9: 'v2 can fall back to keyword heuristics').
# Order of evaluation is the order of this dict; first highest-scoring mode wins.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "debugging": (
        "bug", "error", "fail", "failing", "failure", "broken", "break", "fix",
        "debug", "crash", "exception", "traceback", "stack trace", "500", "404",
        "wrong", "regression", "not working", "doesn't work", "unexpected",
        "root cause", "race condition", "flaky", "timeout",
    ),
    "architecture": (
        "architect", "architecture", "design", "structure", "refactor",
        "module boundary", "layer", "dependency direction", "decompose",
        "trade-off", "tradeoff", "coupling", "cohesion", "service boundary",
        "system design", "component",
    ),
    "implementation": (
        "implement", "write", "add", "create", "build", "code", "feature",
        "endpoint", "function", "class", "method", "migrate", "upgrade",
        "change", "update", "modify", "patch",
    ),
    "investigation": (
        "how does", "how do", "understand", "explore", "where is", "where does",
        "explain", "what does", "what is", "why does", "walk me through",
        "trace", "find", "look at", "read",
    ),
    "planning": (
        "plan", "planning", "estimate", "scope", "roadmap", "sequence",
        "prioritize", "should we", "approach", "strategy", "steps",
        "breakdown", "schedule", "risk",
    ),
}

_WORD_RE = re.compile(r"[a-z0-9'-]+")


def _keyword_mode(query: str) -> str:
    """Score each mode by keyword hits; default to investigation.

    Single-word keywords match exactly or as a prefix (length >= 4), so
    "failing" hits "fail" and "structured" hits "structure". Multi-word
    keywords are phrase matches and weigh double.
    """
    text = query.lower()
    tokens = set(_WORD_RE.findall(text))
    best_mode, best_score = _DEFAULT_MODE, 0
    for mode, keywords in _KEYWORDS.items():
        score = 0
        for kw in keywords:
            if " " in kw:
                score += 2 if kw in text else 0          # phrase hit weighs more
            elif kw in tokens or any(
                len(kw) >= 4 and tok.startswith(kw) for tok in tokens
            ):
                score += 1
        if score > best_score:
            best_mode, best_score = mode, score
    return best_mode


def _llm_mode(query: str, config=None) -> str:
    """Classify via LLM at temperature 0.0. Raises LLMError on any failure."""
    cfg = config or get_config()
    raw = call_llm(
        prompt=f"Query: {query}",
        system=_CLASSIFY_SYSTEM,
        temperature=cfg.llm.temperature_mode_detection,
        max_tokens=16,
        config=cfg,
    )
    cleaned = raw.strip().lower().strip('."\'`')
    for mode in MODES:  # tolerate e.g. "Mode: debugging." or extra words
        if cleaned == mode:
            return mode
    for mode in MODES:
        if mode in cleaned:
            return mode
    raise LLMError(f"mode classifier returned unusable response: {raw!r}")


def detect_mode(query: str, use_llm: bool = True, config=None) -> str:
    """Detect the engineering mode of a query. Always returns a valid mode."""
    cfg = config or get_config()
    if use_llm and api_key(cfg):
        try:
            return _llm_mode(query, cfg)
        except LLMError as exc:
            log.warning("LLM mode detection failed (%s); using keywords", exc)
    return _keyword_mode(query)


def get_mode_params(mode: str, config=None):
    """Mode-specific retrieval parameters (§11.9 / config `modes` block)."""
    cfg = config or get_config()
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    return cfg.modes[mode]
