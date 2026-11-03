"""Typed extraction -> two-stage dedupe -> auto-accepted, undoable items.

Replaces "extract conclusions, wait for a human to review them". There is no
review gate. Instead:

* an item the USER said is accepted as it is (the quote is checked against the
  user's message; if the quote is not there it is treated as inferred);
* an item the AGENT inferred needs evidence: a quote that must appear in the
  exchange, or it is dropped;
* near-duplicates are dropped by the two-stage dedupe (reverie.dedupe);
* everything accepted is reversible (`reverie undo`) and surfaces in a digest.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import dedupe, deliver, items
from .llm import LLMError, call_llm, model_for

log = logging.getLogger("reverie.typed_extractor")

#: extractor kind -> stored item kind
KIND_MAP = {
    "directive": "rule", "correction": "rule", "decision": "decision",
    "rejected_approach": "rejected_approach", "environment": "environment",
    "external_context": "external_context", "plan": "work_state",
    "open_question": "open_question", "preference": "preference",
}
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:]{1,79}$")
_PATHLIKE = re.compile(r"[/\\]|\.[A-Za-z0-9]{1,6}$")
MIN_QUOTE = 12


class TypedExtractionError(RuntimeError):
    """The model's reply was not a JSON array of items (terminal)."""


@dataclass
class ItemResult:
    stored: list[str] = field(default_factory=list)
    rejected_count: int = 0
    rejection_summary: str = ""
    skipped_invalid: int = 0
    deduped: int = 0
    reasons: list[str] = field(default_factory=list)


def load_prompt() -> str:
    path = Path(__file__).parent / "prompts" / "typed_extractor.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return resources.files("reverie").joinpath(
            "prompts/typed_extractor.md").read_text(encoding="utf-8")


def format_exchange(interaction: dict) -> str:
    user = interaction.get("prompt") or ""
    parts = [f"=== EXCHANGE ===\nUSER: {user}"]
    summary = interaction.get("work_summary") or interaction.get("reasoning_trace")
    if summary:
        parts.append(f"AGENT WORK SUMMARY: {summary}")
    parts.append(f"AGENT FINAL ANSWER: {interaction.get('output') or ''}")
    return "\n\n".join(parts) + "\n"


def exchange_text(interaction: dict) -> str:
    return "\n".join(str(interaction.get(k) or "") for k in
                     ("prompt", "work_summary", "reasoning_trace", "output"))


def _norm(s: str) -> str:
    s = re.sub(r"[\"'`“”‘’]", "", s or "").lower()
    return re.sub(r"\s+", " ", s).strip()


def quote_in(quote: str, haystack: str) -> bool:
    q = _norm(quote)
    return len(q) >= MIN_QUOTE and q in _norm(haystack)


def parse_items(raw: str) -> list[dict]:
    """The model's reply -> list of dicts. Tolerates code fences and prose
    around the array. (llm.extract_json is object-oriented: it slices from
    the first '{' to the last '}', which mangles a top-level array.)"""
    text = re.sub(r"```(?:json)?", "", raw or "")
    start = text.find("[")
    if start < 0:
        raise TypedExtractionError(f"no JSON array in reply: {raw[:120]!r}")
    try:
        data, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise TypedExtractionError(f"unparseable JSON array: {exc}") from exc
    if not isinstance(data, list):
        raise TypedExtractionError("reply is not a JSON array")
    return [d for d in data if isinstance(d, dict)]


def validate(raw_item: dict, interaction: dict,
             repo_path: str | None) -> tuple[dict | None, str]:
    """-> (candidate | None, reason). A candidate has the final item fields."""
    kind = KIND_MAP.get(str(raw_item.get("kind", "")).strip())
    text = str(raw_item.get("text") or "").strip()
    if kind is None:
        return None, "unknown kind"
    if len(text) < 8:
        return None, "text too short"
    user_msg = str(interaction.get("prompt") or "")
    everything = exchange_text(interaction)
    source = "user_said" if str(raw_item.get("source")) == "user" else "agent_inferred"
    quote = str(raw_item.get("quote") or "").strip()
    evidence = str(raw_item.get("evidence") or "").strip()
    meta: dict = {"durability": raw_item.get("durability") or "until_changed"}
    if source == "user_said":
        if quote_in(quote, user_msg):
            meta["quote"] = quote
        else:                                  # claimed user-said, but not found
            source = "agent_inferred"
            evidence = evidence or quote
    if source == "agent_inferred":
        if not quote_in(evidence, everything):
            return None, "agent-inferred without verifiable evidence"
        meta["evidence_quote"] = evidence
    anchor_raw = str(raw_item.get("anchor") or "repo").strip()
    files: list[str] = []
    if anchor_raw.lower() not in ("repo", "global", "project", "") and \
            _PATHLIKE.search(anchor_raw):
        files = [anchor_raw]
    symbols = [s for s in (raw_item.get("symbols") or [])
               if isinstance(s, str) and _SYMBOL.match(s.strip())]
    return {"text": text, "kind": kind, "source": source,
            "evidence": meta.get("evidence_quote") or meta.get("quote"),
            "anchor": items.anchor(files, [s.strip() for s in symbols]),
            "meta": meta}, "ok"


def extract_candidates(interaction: dict, config) -> tuple[list[dict], int, list[str]]:
    """LLM call + validation. -> (candidates, rejected_count, reasons)."""
    try:
        raw = call_llm(format_exchange(interaction), system=load_prompt(),
                       model=model_for(config, "extraction"), config=config)
    except LLMError as exc:
        from .extractor import ExtractorError
        raise ExtractorError(f"extraction LLM call failed: {exc}") from exc
    try:
        raw_items = parse_items(raw)
    except TypedExtractionError as exc:
        from .extractor import ExtractionError
        raise ExtractionError(str(exc)) from exc
    repo_path = (interaction.get("session_context") or {}).get("repo")
    good, reasons = [], []
    for r in raw_items:
        cand, why = validate(r, interaction, repo_path)
        if cand is None:
            reasons.append(why)
        else:
            good.append(cand)
    return good, len(reasons), reasons


def extract_and_store_items(
    brain, interaction: dict, *, repo_path: str, embedding_model=None,
    config=None, commit_hash: str | None = None, judge=None,
    author: str | None = None,
) -> ItemResult:
    """The whole pipeline for one exchange. Raises ExtractorError /
    ExtractionError like the legacy extractor so callers' queue handling is
    unchanged."""
    from .config import get_config
    cfg = config or get_config()
    cands, rejected, reasons = extract_candidates(interaction, cfg)
    result = ItemResult(rejected_count=rejected, reasons=list(reasons),
                        rejection_summary="; ".join(sorted(set(reasons)))[:200])
    if not cands:
        return result
    if embedding_model is None:
        from .embeddings import get_embedding_model
        embedding_model = get_embedding_model()
    from .embeddings import from_blob, to_blob
    conn = brain._conn
    key = deliver.repo_key(repo_path)
    existing = [(i["id"], i["text"], from_blob(i["embedding"]))
                for i in items.active(conn, key, with_embedding=True)
                if i.get("embedding")]
    judge = judge or dedupe.llm_judge(cfg)
    agent_id = author or f"agent:{model_for(cfg, 'extraction')}"
    for cand in cands:
        vec = embedding_model.encode_one(cand["text"])
        verdict = dedupe.screen(cand["text"], vec, existing, cfg, judge)
        if not verdict.keep:
            result.deduped += 1
            if verdict.nearest_id:
                items.bump_seen(conn, verdict.nearest_id, key)
            continue
        item_id = items.add(
            conn, text=cand["text"], kind=cand["kind"], source=cand["source"],
            repo_key=key, anchor_=cand["anchor"],
            author="user" if cand["source"] == "user_said" else agent_id,
            level="repo", grounding_commit=commit_hash,
            evidence=cand["evidence"], embedding=to_blob(vec),
            meta={**cand["meta"], "dedupe_stage": verdict.stage,
                  "dedupe_sim": round(verdict.sim, 3)})
        existing.append((item_id, cand["text"], vec))
        result.stored.append(item_id)
    return result
