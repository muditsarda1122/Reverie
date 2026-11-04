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

from . import anchors, dedupe, deliver, items, provenance
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
#: the user's own words must say it covers all their work, not one codebase
_GLOBAL_MARK = re.compile(
    r"\b(always|never|every (?:project|repo|codebase)|all (?:my )?(?:projects|repos)|"
    r"any (?:project|repo)|everywhere|in general|by default)\b", re.I)
_REPO_BOUND = re.compile(r"\b(this|the) (codebase|repo|repository|project|sdk|service)\b", re.I)
_USER_WIDE_KINDS = ("preference", "rule")
#: kinds a user can reverse ("forget what I said about X"); others are facts
REVERSIBLE_KINDS = ("rule", "decision", "preference", "rejected_approach")


class TypedExtractionError(RuntimeError):
    """The model's reply was not a JSON array of items (terminal)."""


@dataclass
class ItemResult:
    stored: list[str] = field(default_factory=list)
    rejected_count: int = 0
    rejection_summary: str = ""
    skipped_invalid: int = 0
    deduped: int = 0
    promoted: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
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
    parts.append(f"AGENT FINAL ANSWER (context only, NOT evidence): "
                 f"{interaction.get('output') or ''}")
    if interaction.get("diff"):
        parts.append("WORKING-TREE DIFF (the real change; evidence for source=agent "
                     f"must be quoted from here):\n{interaction['diff']}")
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
    everything = provenance.evidence_text(interaction)   # user + diff, never narration
    source = "user_said" if str(raw_item.get("source")) == "user" else "agent_inferred"
    quote = str(raw_item.get("quote") or "").strip()
    evidence = str(raw_item.get("evidence") or "").strip()
    meta: dict = {"durability": raw_item.get("durability") or "until_changed"}
    if source == "user_said":
        if quote_in(quote, user_msg):
            meta["quote"] = quote
            meta["provenance"] = "user"
        else:                                  # claimed user-said, but not found
            source = "agent_inferred"
            evidence = evidence or quote
    if source == "agent_inferred":
        if not quote_in(evidence, everything):
            return None, "agent-inferred without evidence in the user's words or the diff"
        meta["evidence_quote"] = evidence
        meta["provenance"] = ("user" if quote_in(evidence, user_msg) else "diff")
    user_wide = False
    if str(raw_item.get("scope")) == "user" and source == "user_said" \
            and kind in _USER_WIDE_KINDS:
        said = meta.get("quote") or ""
        user_wide = bool(_GLOBAL_MARK.search(said)) and not _REPO_BOUND.search(said)
    anchor_raw = str(raw_item.get("anchor") or "repo").strip()
    files: list[str] = []
    if anchor_raw.lower() not in ("repo", "global", "project", "") and \
            _PATHLIKE.search(anchor_raw):
        files = [anchor_raw]
    symbols = [s for s in (raw_item.get("symbols") or [])
               if isinstance(s, str) and _SYMBOL.match(s.strip())]
    syms = [s.strip() for s in symbols]
    if not user_wide:       # user-wide rules are not about one codebase
        g = anchors.ground(text, repo_path, files, syms)
        if g["grounded"]:
            files, syms = g["files"], g["symbols"]
            meta["anchor_grounded"] = True
    return {"text": text, "kind": kind, "source": source,
            "evidence": meta.get("evidence_quote") or meta.get("quote"),
            "user_wide": user_wide,
            "anchor": items.anchor(files, syms),
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


def _seen_in_other_repo(conn, cand: dict, vec, key: str, cfg, judge,
                        result: ItemResult) -> bool:
    """Cross-repo learning. If this agent-inferred, promotable-kind candidate
    restates an item another repo already holds, that item becomes user-wide
    (once seen in ``promotion.min_repos`` repos) and the candidate is not
    stored again. Repo facts and decisions never reach here: only the kinds in
    ``promotion.kinds`` and only when the agent inferred them."""
    from .embeddings import from_blob
    p = cfg.promotion
    if cand["source"] != "agent_inferred" or cand["kind"] not in p.kinds:
        return False
    marks = ",".join("?" * len(p.kinds))
    rows = conn.execute(
        f"SELECT * FROM items WHERE status='active' AND source='agent_inferred' "
        f"AND repo_key IS NOT NULL AND repo_key != ? AND kind IN ({marks}) "
        f"AND embedding IS NOT NULL", (key, *p.kinds)).fetchall()
    others = [(r["id"], r["text"], from_blob(r["embedding"])) for r in rows]
    verdict = dedupe.screen(cand["text"], vec, others, cfg, judge)
    if verdict.keep or not verdict.nearest_id:
        return False
    meta = items.bump_seen(conn, verdict.nearest_id, key)
    if len(set(meta.get("seen_repos") or [])) >= int(p.min_repos):
        if items.promote(conn, verdict.nearest_id, key):
            result.promoted.append(verdict.nearest_id)
    result.deduped += 1
    return True


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
    def load_existing() -> tuple[list[tuple[str, str, object]], dict[str, str]]:
        rows = [i for i in items.active(conn, key, with_embedding=True)
                if i.get("embedding")]
        return ([(i["id"], i["text"], from_blob(i["embedding"])) for i in rows],
                {i["id"]: i["kind"] for i in rows})
    existing, kinds = load_existing()
    judge = judge or dedupe.llm_judge(cfg)
    agent_id = author or f"agent:{model_for(cfg, 'extraction')}"
    for cand in cands:
        vec = embedding_model.encode_one(cand["text"])
        verdict = dedupe.screen(cand["text"], vec, existing, cfg, judge)
        if verdict.keep:
            # Another writer may have stored this fact while the judge ran
            # (several agents share one memory): screen against what is new.
            known = {e[0] for e in existing}
            fresh, fresh_kinds = load_existing()
            added = [e for e in fresh if e[0] not in known]
            if added:
                existing, kinds = fresh, fresh_kinds
                verdict = dedupe.screen(cand["text"], vec, added, cfg, judge)
        if not verdict.keep:
            result.deduped += 1
            if verdict.nearest_id:
                items.bump_seen(conn, verdict.nearest_id, key)
            continue
        if not cand["user_wide"] and _seen_in_other_repo(
                conn, cand, vec, key, cfg, judge, result):
            continue
        replaced: list[str] = []
        if cfg.dedupe.replace and cand["source"] == "user_said" \
                and cand["kind"] in REVERSIBLE_KINDS:
            texts = {e[0]: e[1] for e in existing}
            near = [(i, texts[i], sim) for i, sim in
                    dedupe.nearest(cand["text"], vec, existing, int(cfg.dedupe.neighbors) + 2)
                    if sim >= float(cfg.dedupe.replace_min_sim)
                    and kinds.get(i) in REVERSIBLE_KINDS]
            replaced = dedupe.replaces(cand["text"], near, judge)
        item_id = items.add(
            conn, text=cand["text"], kind=cand["kind"], source=cand["source"],
            repo_key=None if cand["user_wide"] else key,
            anchor_=items.anchor(type_="user") if cand["user_wide"]
            else cand["anchor"],
            author="user" if cand["source"] == "user_said" else agent_id,
            level="user" if cand["user_wide"] else "repo",
            grounding_commit=commit_hash,
            evidence=cand["evidence"], embedding=to_blob(vec),
            meta={**cand["meta"], "dedupe_stage": verdict.stage,
                  "dedupe_sim": round(verdict.sim, 3)})
        if replaced:
            done = items.supersede(conn, item_id, replaced)
            result.superseded += done
            existing = [e for e in existing if e[0] not in done]
        existing.append((item_id, cand["text"], vec))
        kinds[item_id] = cand["kind"]
        result.stored.append(item_id)
    return result
