"""Engineering Extractor (Spec Section 3 / §5.x).

Loads the tested extraction prompt (``ec/prompts/extractor_prompt.md``) at
runtime — the prompt is the extraction methodology (five-pass process,
Lifting Test, signal catalog, self-review pass); this module is the plumbing
around it:

1. send the full interaction (prompt + reasoning trace + output + session
   context, §5.2) to Claude Haiku 4.5 via ec.llm at temperature 0.3
2. strip fences + parse JSON (ec.llm.extract_json)
3. validate each candidate ECU against §1 vocabularies (invalid ECUs are
   skipped with a warning — one malformed ECU must not lose the batch)
4. assign initial confidence priors per §5.9 (base x scope modifier,
   corroboration bump from "+"-separated evidence_pointer sources,
   cap 0.95 / floor 0.05)
5. stamp provenance; leave storage to the caller (Session Brain now,
   Canonical Brain only after the review gate — §6.7)

The Extractor does NOT create edges (that's the Diffuser), does NOT review
(that's the Human Review Gate), and is stateless within a session (§5.13).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import confidence as conf
from .config import CONCLUSION_TYPES, SCOPE_LEVELS, get_base_prior, get_config
from .llm import LLMError, call_llm, extract_json

log = logging.getLogger("ec.extractor")

_PROMPT_FILENAME = "extractor_prompt.md"
_PROMPT_ENV_VAR = "EC_EXTRACTOR_PROMPT"


class ExtractorError(RuntimeError):
    """Raised when extraction itself fails (missing prompt, LLM error,
    unparseable response). Individual malformed ECUs are NOT errors —
    they are skipped and logged."""


class ExtractionError(ExtractorError):
    """The LLM response could not be parsed into the expected JSON shape."""


@dataclass
class ExtractionResult:
    """Output of one extraction pass (§5.3)."""

    ecus: list[dict] = field(default_factory=list)      # validated, prior-bearing
    rejected_count: int = 0
    rejection_summary: str = ""
    raw_response: str = ""                              # audit trail
    skipped_invalid: int = 0                            # failed local validation


# ---------------------------------------------------------------------------
# prompt loading
# ---------------------------------------------------------------------------

_prompt_cache: str | None = None


def _prompt_candidates() -> list[Path]:
    return [
        Path(__file__).resolve().parent / "prompts" / _PROMPT_FILENAME,  # in-package
        Path.cwd() / _PROMPT_FILENAME,
    ]


def load_extractor_prompt(refresh: bool = False) -> str:
    """Load ``extractor_prompt.md`` at runtime (never paraphrased into code).

    Resolution order: ``EC_EXTRACTOR_PROMPT`` env var, package-relative
    ``ec/prompts/`` (always works), cwd. Cached process-wide.
    """
    global _prompt_cache
    if _prompt_cache is not None and not refresh:
        return _prompt_cache

    env_path = os.environ.get(_PROMPT_ENV_VAR)
    candidates = [Path(env_path)] if env_path else []
    candidates.extend(_prompt_candidates())
    for path in candidates:
        if path.is_file():
            _prompt_cache = path.read_text(encoding="utf-8")
            return _prompt_cache
    searched = ", ".join(str(p) for p in candidates)
    raise ExtractorError(
        f"{_PROMPT_FILENAME} not found (searched: {searched}). The extractor "
        "prompt is the tested extraction methodology and must be loadable at "
        f"runtime. Keep it in ec/prompts/ or set {_PROMPT_ENV_VAR}."
    )


# ---------------------------------------------------------------------------
# validation + priors
# ---------------------------------------------------------------------------

def count_evidence_sources(evidence_pointer: str | None) -> int:
    """Independent sources in an evidence_pointer string (§5.9 corroboration).

    The extractor prompt notes multi-source corroboration as "+"-separated
    sources, e.g. "Agent reasoning step 4 (code analysis) + step 8 (test
    confirmation)".
    """
    if not evidence_pointer:
        return 1
    return max(1, len([part for part in evidence_pointer.split("+") if part.strip()]))


def validate_ecu(raw: dict) -> dict:
    """Normalize one extractor-emitted ECU and enforce §1 vocabularies.

    Raises ValueError on any violation — the caller skips invalid ECUs.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"ECU must be a JSON object, got {type(raw).__name__}")

    cognition = str(raw.get("cognition") or "").strip()
    if not cognition:
        raise ValueError("ECU cognition must be a non-empty string")

    conclusion_type = raw.get("conclusion_type")
    if conclusion_type not in CONCLUSION_TYPES:
        raise ValueError(
            f"invalid conclusion_type {conclusion_type!r}; "
            f"expected one of {CONCLUSION_TYPES}"
        )

    scope = raw.get("scope") or {}
    scope_level = scope.get("level")
    if scope_level not in SCOPE_LEVELS:
        raise ValueError(
            f"invalid scope level {scope_level!r}; expected one of {SCOPE_LEVELS}"
        )

    source_type = raw.get("source_type") or "session"
    get_base_prior(source_type)  # raises ValueError on unknown source_type

    grounding = raw.get("grounding") or {}
    if not isinstance(grounding, dict):
        raise ValueError("grounding must be an object")

    # The prompt emits a singular `evidence_pointer` string; the §1 ECU
    # structure stores a list of `evidence_pointers`.
    pointer = raw.get("evidence_pointer")
    if pointer is None:
        pointers = raw.get("evidence_pointers") or []
        pointer = "+".join(str(p) for p in pointers)
    pointer = str(pointer)

    return {
        "cognition": cognition,
        "conclusion_type": conclusion_type,
        "scope": {"level": scope_level, "path": scope.get("path") or scope_level},
        "source_type": source_type,
        "grounding": grounding,
        "evidence_pointer": pointer,
    }


def _assign_priors(ecu: dict, config=None) -> dict:
    """§5.9 prior: base x scope modifier + corroboration bump, cap/floor."""
    n_sources = count_evidence_sources(ecu.get("evidence_pointer"))
    confidence = conf.initial_prior(
        ecu["source_type"], ecu["scope"]["level"], n_sources=n_sources, config=config
    )
    return {
        "cognition": ecu["cognition"],
        "conclusion_type": ecu["conclusion_type"],
        "scope": ecu["scope"],
        "provenance": {
            "source_type": ecu["source_type"],
            "source_id": ecu.get("source_id"),
            "origin_agent": ecu.get("origin_agent"),
            "origin_engineer": None,
        },
        "grounding": ecu["grounding"],
        "confidence": confidence,
        "status": "active",
        "evidence_pointers": [ecu["evidence_pointer"]] if ecu["evidence_pointer"] else [],
    }


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def _format_interaction(interaction: dict) -> str:
    """One user message carrying the full interaction (§5.2)."""
    parts: list[str] = []
    ctx = interaction.get("session_context")
    if ctx:
        parts.append(f"## Session Context\n{ctx}")
    prompt = interaction.get("prompt")
    if prompt:
        parts.append(f"## Developer Prompt\n{prompt}")
    trace = interaction.get("reasoning_trace")
    if trace:
        parts.append(f"## Agent Reasoning Trace\n{trace}")
    output = interaction.get("output")
    if output:
        parts.append(f"## Agent Final Output\n{output}")
    if not parts:
        raise ExtractorError(
            "extract_ecus requires at least one of: prompt, reasoning_trace, "
            "output, session_context"
        )
    return "\n\n".join(parts)


def extract_ecus(
    interaction: dict,
    origin_agent: str | None = None,
    config=None,
) -> ExtractionResult:
    """Run the extractor over one agent interaction. Returns candidates.

    The returned ECUs are validated and carry §5.9 priors, but are NOT
    stored anywhere — storage is the caller's choice (Session Brain for
    live sessions; Canonical Brain only after the Human Review Gate).
    """
    cfg = config or get_config()
    user_message = _format_interaction(interaction)
    system = load_extractor_prompt()

    try:
        response = call_llm(user_message, system=system, config=cfg)
    except LLMError as exc:
        raise ExtractorError(f"extraction LLM call failed: {exc}") from exc

    try:
        payload = json.loads(extract_json(response))
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            f"extractor returned unparseable JSON: {exc}. "
            f"Response starts: {response[:200]!r}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ecus"), list):
        raise ExtractionError(
            "extractor response must be a JSON object with an 'ecus' array; "
            f"got: {response[:200]!r}"
        )

    session_ctx = interaction.get("session_context") or {}
    if isinstance(session_ctx, dict):
        source_id = session_ctx.get("session_id")
    else:
        source_id = None

    result = ExtractionResult(
        rejected_count=int(payload.get("rejected_count") or 0),
        rejection_summary=str(payload.get("rejection_summary") or ""),
        raw_response=response,
    )
    for raw in payload["ecus"]:
        try:
            ecu = validate_ecu(raw)
        except ValueError as exc:
            result.skipped_invalid += 1
            log.warning("skipping invalid extracted ECU: %s", exc)
            continue
        ecu["source_id"] = source_id
        ecu["origin_agent"] = origin_agent or cfg.llm.model
        result.ecus.append(_assign_priors(ecu, cfg))
    return result


def stamp_grounding_commit_hash(ecus: list[dict], commit_hash: str | None) -> None:
    """D49 server-side provenance stamp (design doc §4.2): fill each ECU's
    ``grounding.commit_hash`` when the extractor left it absent or empty.

    The extractor produces cognition; the caller (MCP server) stamps
    provenance. This helper never overwrites a commit_hash the LLM did
    provide. The grounding dict is created here if absent, so callers can
    stamp without shape-checking every candidate.
    """
    if not commit_hash:
        return
    for ecu in ecus:
        grounding = ecu.get("grounding")
        if not isinstance(grounding, dict):
            grounding = {}
            ecu["grounding"] = grounding
        if not grounding.get("commit_hash"):
            grounding["commit_hash"] = commit_hash


def extract_and_store_session(
    brain,
    session_id: str,
    interaction: dict,
    embedding_model=None,
    origin_agent: str | None = None,
    config=None,
    commit_hash: str | None = None,
) -> tuple[ExtractionResult, list[str]]:
    """§28.13 live-session flow: extract, embed, store in the Session Brain.

    ``commit_hash`` (D49): when given, stamped into every stored ECU's
    ``grounding.commit_hash`` after extraction and before storage — the
    server reads it from the session repo's HEAD; see
    ec.mcp_server._current_commit_hash.

    Lightweight diffusion of the stored ECUs is a separate step
    (ec.diffuser.diffuse_session_ecu) — the Extractor never creates edges.
    """
    if embedding_model is None:
        from .embeddings import get_embedding_model
        embedding_model = get_embedding_model()

    result = extract_ecus(interaction, origin_agent=origin_agent, config=config)
    stamp_grounding_commit_hash(result.ecus, commit_hash)
    ids = []
    for ecu in result.ecus:
        embedding = embedding_model.encode_one(ecu["cognition"])
        ids.append(brain.insert_session_ecu(session_id, ecu, embedding=embedding))
    return result, ids
