# Engineering Cognition (EC v2) — Technical Reference

**Document status:** Comprehensive technical reference for external readers. Written from a full read of every file in `ec/`, `tests/`, `bench/`, `docs/SPEC.md`, `docs/ASSESSMENT.md`, `docs/SPEC_AUDIT.md`, `docs/SPEC_AUDIT_FINAL.md`, and all 24 files in `docs/handoffs/`. Every claim is traceable to a file path (and, where useful, a function name). No code was modified to produce this document.

**Verified at time of writing:** test suite `.venv/bin/python -m pytest tests/` → **512 collected, 506 passed, 6 skipped** (the 6 skips are `OPENCODE_ZEN_API_KEY`-gated live tests). Git HEAD is post-Phase-13 (audit baseline `3dd5320`).

**Companion documents:**
- `docs/SPEC.md` — the authoritative implementation spec (16 sections, 2,938 lines).
- `docs/EC_TECHNICAL_REFERENCE.md` — an earlier 20-section reference organized around the system narrative; this document follows the 13-section structure requested for external readers.
- `docs/ASSESSMENT.md` — EC-Bench v2 readiness audit (2026-08-13).
- `docs/SPEC_AUDIT.md` / `docs/SPEC_AUDIT_FINAL.md` — spec-compliance audits (268 then 303 requirements traced).
- `docs/handoffs/` — 24 phase handoff documents containing design decisions D1–D57.

**One-paragraph orientation.** Engineering Cognition (EC) is a persistent cognition layer for AI coding agents. It is a pip-installable Python package (`ec/`) plus a local MCP server (stdio NDJSON JSON-RPC 2.0) that extracts *engineering conclusions* (not facts) from agent reasoning, stores them as ECUs (Engineering Cognition Units) in a two-brain SQLite database (`~/.ec/ec.db`, one database for all projects), and retrieves them on demand with confidence, scope, grounding, and typed relationships. Everything is one Python process per agent session plus short-lived CLI processes; no vector DB, no HTTP, no external services.

**Package layout (all paths relative to repo root):**

```
ec/
├── __init__.py           # version marker (0.1.0)
├── schema.sql            # SQLite DDL — all 11 tables (ec/schema.sql)
├── config.py             # every tunable constant + YAML deep-merge loader (ec/config.py)
├── brain.py              # Brain class: canonical + session CRUD, edges, similarity (ec/brain.py)
├── embeddings.py         # all-MiniLM-L6-v2 wrapper, float32 BLOB codec (ec/embeddings.py)
├── llm.py                # Anthropic/OpenAI wire-format client + JSON fence stripper (ec/llm.py)
├── mode_detection.py     # 5-mode classifier: LLM (temp 0.0) + keyword fallback (ec/mode_detection.py)
├── confidence.py         # pure log-odds Bayesian math, lazy decay, priors (ec/confidence.py)
├── extractor.py          # prompt-based ECU extraction plumbing (ec/extractor.py)
├── prompts/
│   └── extractor_prompt.md  # the tested MIT-licensed extraction prompt (613 lines)
├── diffuser.py           # full 9-step + lightweight 3-way diffuser (ec/diffuser.py)
├── retrieval.py          # demand-driven 14-step ec_query pipeline (ec/retrieval.py)
├── activation.py         # session-scoped spreading activation (ec/activation.py)
├── session.py            # SessionManager + /ec-start /ec-stop /ec-status CLI (ec/session.py)
├── review_gate.py        # Human Review Gate + open-question resolution (ec/review_gate.py)
├── mcp_server.py         # hand-rolled MCP server + 4 tools (ec/mcp_server.py)
├── maintainer.py         # background maintenance: 4 tasks (ec/maintainer.py)
├── grounding.py          # file/symbol/commit verification (ec/grounding.py)
├── clustering.py         # HDBSCAN (+ scipy fallback) clustering (ec/clustering.py)
├── reconsolidation.py    # ec_reconsolidate evidence-update loop (ec/reconsolidation.py)
├── install.py            # agent detection + ~/.ec bootstrap (ec/install.py)
├── repair.py             # ec-repair integrity check (ec/repair.py)
└── run_ecbench.py        # benchmark runner (ec/run_ecbench.py)
bench/
├── ecbench_v2.json       # 30-prompt spec, 3 sessions
├── judge_ecbench.py      # final-answer judge (GLM-5.2)
├── judge_ecbench_v3_fulltranscript.py  # full-transcript judge
└── run_and_judge.py      # unified runner + inline judge
tests/                    # 26 test files, 512 tests (see Section 13)
docs/                     # SPEC, audits, handoffs, references
pyproject.toml            # package + console scripts (pyproject.toml)
```

---

## 1. ECU Structure

### 1.1 Definition and the atomicity thesis

The ECU is the atomic storage unit of Engineering Cognition. Per the extraction prompt (`ec/prompts/extractor_prompt.md`, "What is an ECU?") and `docs/SPEC.md` §1:

> An ECU is the smallest irreducible engineering conclusion, derived through engineering reasoning or experience, that is **self-contained**, **persists independently of its originating interaction**, and has **the potential to influence future engineering decisions**.

There is no separate "belief" type (`docs/SPEC.md` §9.2): an ECU *functions as* a belief when other ECUs support it via `supports` edges; belief-ness is an emergent property of network position.

**What makes an ECU atomic** (enforced at three levels):

1. **The Atomization Rule** (`ec/prompts/extractor_prompt.md`, "Atomization Rule"): *"Each ECU must express exactly ONE engineering conclusion. If a candidate contains multiple independent conclusions, split it into separate ECUs. Test: 'Could a future task require one of these conclusions but not the other?' If yes, split them."* The prompt shows a three-conclusion sentence that must become 3 ECUs.
2. **The Lifting Test** (same file): every candidate must pass three tests —
   - **"So What?"** — is it a conclusion, not a fact? ("If you can prefix it with 'Interestingly, ...' and it still reads as a fact, it's information.")
   - **"Future Session"** — would knowing this change how an engineer approaches a future task?
   - **"Independence"** — does it remain understandable without the original conversation?
3. **The Self-Review Pass** (same file, execution-instruction step 9): before output, the extractor re-reads every candidate and rejects anything "an engineer could obtain by simply reading the code."

`cognition` is **semantically immutable** (`docs/SPEC.md` §13/§20, implemented as a design constraint in `ec/brain.py` module docstring): any change to meaning — even cosmetic rewording — requires a new ECU with a `supersedes` edge. Confidence/status/edges/metadata are mutable in place (design decision **D2**, `docs/handoffs/HANDOFF_PHASE_1.md:99`).

### 1.2 The ECU data format (as built)

The authoritative in-memory/JSON representation is built in `Brain.insert_ecu()` (`ec/brain.py:197-214`) and re-materialized by `_row_to_ecu()` (`ec/brain.py:251-271`). It matches `docs/SPEC.md` §3.1:

```json
{
  "id": "uuid4-string",
  "cognition": "The irreducible engineering conclusion (IMMUTABLE once created)",
  "conclusion_type": "implication | constraint | principle | decision | observation | pattern | invariant | trade-off",
  "scope": {
    "level": "engineering | domain | organization | project | repo | module | subsystem",
    "path": "engineering > domain:web-frameworks > repo:fastapi > module:routing"
  },
  "provenance": {
    "source_type": "session | debugging | implementation | planning | review | architectural_reasoning",
    "source_id": "session ID / commit / PR",
    "origin_agent": "model name + version that produced the reasoning",
    "origin_engineer": "engineer identifier (future team attribution; currently null)",
    "created_at": "ISO 8601 UTC timestamp"
  },
  "grounding": {
    "repo_path": "/path/to/repo",
    "files": ["auth/token_manager.rs"],
    "symbols": ["TokenManager::refresh"],
    "commit_hash": "a3f2e1c9d0e1 (12-char short hash, stamped server-side, D49)",
    "code_context": "optional free-text description of code state"
  },
  "confidence": 0.75,
  "status": "active | challenged | superseded | deprecated | open_question | archived",
  "evidence_pointers": ["Agent reasoning trace, step 4 (code analysis) + step 8 (test confirmation)"],
  "metadata": {
    "last_reinforced": null,
    "last_challenged": null,
    "last_retrieved": null,
    "retrieval_count": 0,
    "cluster_memberships": [],
    "has_pending_updates": false
  },
  "embedding": "<float32 bytes | null> (384-dim, all-MiniLM-L6-v2, L2-normalized)"
}
```

Note on `metadata`: the defaults are defined in `_DEFAULT_METADATA` (`ec/brain.py:42-49`):

```python
_DEFAULT_METADATA = {
    "last_reinforced": None,
    "last_challenged": None,
    "last_retrieved": None,
    "retrieval_count": 0,
    "cluster_memberships": [],
    "has_pending_updates": False,
}
```

Controlled vocabularies live in `ec/config.py:287-307`:

```python
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
RETRIEVABLE_STATUSES = ("active", "challenged", "open_question")  # §1.2 status semantics
EDGE_TYPES = ("supports", "contradicts", "depends_on", "supersedes")
MODES = ("debugging", "architecture", "implementation", "investigation", "planning")
```

Status semantics (`docs/SPEC.md` §3.2, implemented via `RETRIEVABLE_STATUSES` and `FROZEN_STATUSES` in `ec/confidence.py:76-78`):

| Status | Retrieval? | Confidence | Meaning |
|---|---|---|---|
| `active` | yes | live (decays/reinforces) | Normal current belief |
| `challenged` | yes (flagged, never deprioritised — D10) | live | Contradicting evidence exists |
| `superseded` | no | **frozen** | Replaced via a `supersedes` edge |
| `deprecated` | no | **frozen** | Grounding disappeared (grounding verification) |
| `open_question` | yes (×0.5 weight + flag) | **frozen** | Competing hypotheses past persistence limit |
| `archived` | no | **frozen** | User explicitly archived |

### 1.3 Storage: SQLite schema

One global database, default `$EC_HOME/ec.db` = `~/.ec/ec.db` (`ec/config.py:29-39`, `ec/config.py::default_db_path`). WAL mode + foreign keys ON on every connect (`ec/brain.py:75-78`). Full DDL in `ec/schema.sql`:

```sql
-- Canonical Brain (durable, human-reviewed)
CREATE TABLE IF NOT EXISTS ecus (
    id                      TEXT PRIMARY KEY,          -- uuid4
    cognition               TEXT NOT NULL,             -- IMMUTABLE (§13.2)
    conclusion_type         TEXT NOT NULL,             -- 8 types
    scope_level             TEXT NOT NULL,             -- 7 levels
    scope_path              TEXT NOT NULL,
    source_type             TEXT NOT NULL,
    source_id               TEXT,
    origin_agent            TEXT,
    origin_engineer         TEXT,
    created_at              TEXT NOT NULL,             -- ISO 8601
    grounding_json          TEXT NOT NULL DEFAULT '{}',
    confidence              REAL NOT NULL,             -- [0,1]
    status                  TEXT NOT NULL DEFAULT 'active',
    evidence_pointers_json  TEXT NOT NULL DEFAULT '[]',
    metadata_json           TEXT NOT NULL DEFAULT '{}',
    embedding               BLOB,                      -- float32 x 384
    document                TEXT NOT NULL              -- full ECU JSON (audit fidelity)
);
CREATE INDEX IF NOT EXISTS idx_ecus_status      ON ecus(status);
CREATE INDEX IF NOT EXISTS idx_ecus_scope_level ON ecus(scope_level);
CREATE INDEX IF NOT EXISTS idx_ecus_created     ON ecus(created_at);

CREATE TABLE IF NOT EXISTS edges (
    id                  TEXT PRIMARY KEY,
    source_id           TEXT NOT NULL REFERENCES ecus(id),
    target_id           TEXT NOT NULL REFERENCES ecus(id),
    type                TEXT NOT NULL,   -- supports | contradicts | depends_on | supersedes
    weight              REAL NOT NULL DEFAULT 1.0,
    confidence_delta    REAL NOT NULL DEFAULT 0.0,  -- log-odds space (reversible updates)
    supersession_type   TEXT,            -- cosmetic | semantic (only type=supersedes)
    created_at          TEXT NOT NULL,
    UNIQUE (source_id, target_id, type)  -- §14.3: one edge per pair per type
);
```

Session-side tables (`ec/schema.sql:60-147`): `sessions` (`id, repo_path, branch, status, started_at, ended_at, ecu_count`), `session_ecus` (same core columns as `ecus` plus `session_id` FK and `review_status TEXT NOT NULL DEFAULT 'pending'` — `pending | accepted | rejected | skipped`; note the schema comment records a deliberate deviation: session ECUs carry an `embedding BLOB` even though §28.5 of the spec omits it, because the lightweight diffuser needs it), `session_edges` (`source_id` → `session_ecus`, `target_type` ∈ `session_ecu | canonical_ecu`, `type` ∈ `supports | contradicts` only), `pending_updates` (`canonical_ecu_id, session_ecu_id, session_id, relationship_type, proposed_confidence_delta, status ∈ pending|applied|discarded`), and `session_activation` (`session_id, ecu_id, score, updated_at`, PK `(session_id, ecu_id)` — additive table for D13).

Maintenance/clustering tables (`ec/schema.sql:154-195`): `maintenance_log` (`run_at, action, details, ecus_affected`), `maintenance_state` (key/value; keys `last_run_at`, `last_run_ecu_count`, `grounding_last_check:<repo>`, `last_clustering_ecu_count`), `clusters` (`label INT, stability REAL`), `cluster_memberships` (PK `(ecu_id, cluster_id)`, `weight` = cluster stability).

Embeddings are stored as **float32 BLOBs directly on the ECU rows** (not a separate table — documented deviation D3, `docs/handoffs/HANDOFF_PHASE_1.md:100`): 384 × 4 = 1536 bytes; serialized by `to_blob`/`from_blob` (`ec/embeddings.py:27-34`). The `document` column stores the full ECU JSON for audit fidelity and is regenerated from authoritative columns on any mutation via `_rewrite_document()` (`ec/brain.py:351-360`).

### 1.4 How an ECU is created

There are exactly three creation paths; all validate against the vocabularies above via `Brain._validate_ecu_fields()` (`ec/brain.py:131-155`), which rejects empty cognition, unknown `conclusion_type`/`scope_level`/`status`, and confidence outside [0,1].

**Path 1 — Session Brain (the normal path).** `ec_observe` → `extractor.extract_and_store_session()` (`ec/extractor.py:295-324`):

1. `extract_ecus()` calls the LLM (see Section 3) and gets candidate JSON.
2. Each candidate is normalized by `validate_ecu()` (`ec/extractor.py:117-165`) — enforces §1 vocabularies; the prompt's singular `evidence_pointer` string is folded into the `evidence_pointers` list convention; invalid ECUs are skipped (`skipped_invalid += 1`), never fatal to the batch.
3. `_assign_priors()` (`ec/extractor.py:168-188`) computes the §5.9 initial confidence (Section 2.3 of this document), stamps `provenance` (source_type from the LLM, source_id = session id, origin_agent = `cfg.llm.model` unless overridden, origin_engineer = None), sets `status: "active"`.
4. `stamp_grounding_commit_hash()` (`ec/extractor.py:275-292`) fills `grounding.commit_hash` from the session repo's git HEAD (D49 — stamped server-side; never overwrites an LLM-provided hash; any git failure degrades to None, `ec/mcp_server.py:101-123`).
5. Each ECU is embedded (`embedding_model.encode_one(ecu["cognition"])`) and stored via `Brain.insert_session_ecu()` (`ec/brain.py:621-682`) with `review_status='pending'`; the session's `ecu_count` increments.

**Path 2 — Canonical Brain via the Review Gate.** `review_gate._promote()` (`ec/review_gate.py:187-208`) re-inserts the accepted session ECU into `ecus` **keeping the same id** (D21), preserving `metadata.review_edit` if the human edited the cognition (D19), then `diffuser.diffuse_ecu()` integrates it into the belief network (Section 6). `Brain.insert_ecu()` (`ec/brain.py:171-249`) applies defaults: uuid4 id, current timestamp, empty grounding/metadata, status `active`, confidence default 0.5.

**Path 3 — Canonical Brain via reconsolidation supersession.** `reconsolidation._materialize_evidence_ecu()` (`ec/reconsolidation.py:189-214`) inserts the agent's evidence as a real canonical ECU (source_type `"reconsolidation"`, confidence 0.5, `metadata.reconsolidates = <old id>`) — required because §15.5 supersession needs a stored replacement ECU. This is the only other writer of `insert_ecu` besides promotion.

### 1.5 Mutability contract (§13/§20)

Implemented as the public surface of `Brain` (`ec/brain.py:349-451`):

- **Immutable:** `id`, `cognition`, `conclusion_type`, `scope`, `provenance`. `brain.py` deliberately exposes **no method** to mutate cognition (module docstring, `ec/brain.py:8-11`).
- **Mutable in place:** `confidence` (`update_ecu_confidence`), `status` (`update_ecu_status`), `metadata` (`update_ecu_metadata`, merge semantics), `grounding` (`update_ecu_grounding` — file paths can move; `commit_hash`/`code_snapshot` preserved unless explicitly supplied; D55), `evidence_pointers` (`add_evidence_pointer` — append-only, deduped; D55).

---

## 2. Confidence System

All confidence mathematics live in `ec/confidence.py` — pure functions, no DB, no LLM. The module docstring states the design: *"All Bayesian updates happen in log-odds space for numerical stability (§15.1), then convert back to [0, 1] for storage."*

### 2.1 Representation and the log-odds formulas

Confidence is stored as a float in [0, 1] on the ECU row. Updates are computed in log-odds (logit) space (`ec/confidence.py:56-67`):

```python
def to_log_odds(c: float) -> float:
    c = min(max(c, _EPS), 1.0 - _EPS)      # _EPS = 1e-9, clamped to open interval
    return math.log(c / (1.0 - c))

def to_probability(L: float) -> float:      # sigmoid
    if L >= 0:
        return 1.0 / (1.0 + math.exp(-L))
    exp_l = math.exp(L)
    return exp_l / (1.0 + exp_l)
```

- **Support update** (`support_update`, `ec/confidence.py:206-216`; spec §15.2):

  ```
  L_B' = L_B + w_support × r(A,B) × c_A
  ```

  where `L_B` = target's current log-odds, `c_A` = source ECU's confidence, `r(A,B)` = semantic similarity (edge weight), `w_support` = 1.0. Returns `(new_c_B, delta)` where `delta = w_support × r × c_A` is stored on the edge as `confidence_delta` for reversibility (§15.6).

- **Contradiction update** (`contradiction_update`, `ec/confidence.py:219-229`; spec §15.3): identical but negative:

  ```
  L_B' = L_B - w_contradict × r(A,B) × c_A      (stored delta is negative)
  ```

- **Reversal** (`reverse_update`, `ec/confidence.py:232-234`; spec §15.6, used by edge pruning):

  ```
  L_B' = L_B - edge.confidence_delta
  ```

- **Calibration rationale** (spec §15.8): `w_support = w_contradict = 1.0` (symmetric) is deliberate. Genuine contradictions are already gated by two-stage classification (Section 6.4), so by the time a `contradicts` edge exists it deserves equal weight. Evidence strength differentiates through `r` and `c_A`, not `w`. Worked examples from the spec: strong evidence (r=0.90, c_A=0.80) moves a 0.50 target to 0.673; weak evidence (r=0.50, c_A=0.40) moves it to 0.550.

### 2.2 How multiple pieces of evidence combine

Because updates are additive in log-odds space, N independent pieces of supporting evidence accumulate as `L_B' = L_B + Σᵢ (w × rᵢ × c_Aᵢ)`, which is multiplicative in probability space — corroborating evidence compounds naturally. Two structural notes:

1. **Edge accumulation, not edge duplication** (`Brain.add_edge`, `ec/brain.py:457-512`): the `UNIQUE (source_id, target_id, type)` constraint means repeated evidence for the *same relationship* updates the existing edge — `new_weight = min(1.0, existing_weight + weight)` and `new_delta = existing_delta + confidence_delta` — rather than creating a second row. Two ECUs *can* hold edges of different types simultaneously (e.g. supports + depends_on).
2. **Opposing-edges exception** (§14.3, D53): if the same directed pair ends up with *both* a `supports` and a `contradicts` edge, `_check_opposing_edges()` (`ec/diffuser.py:532-563`) emits a flag — "This relationship may need decomposition into more atomic ECUs" — surfaced at the review gate (and re-scanned across all edges by `review_gate._opposing_edge_notifications`, `ec/review_gate.py:331-365`).

At extraction time, multi-source corroboration within a single interaction is handled by the prior bump (Section 2.3).

### 2.3 Initial confidence (extraction priors)

`initial_prior()` (`ec/confidence.py:174-199`; spec §5.9) computes a two-dimensional prior plus corroboration bump:

```
prior = base_prior(source_type) × scope_multiplier(scope_level)
      + min((n_sources − 1) × corroboration_bump, corroboration_bump_cap)
then clamped to [prior_floor, prior_cap] = [0.05, 0.95]
```

Base priors (`ec/config.py:62-69`; spec rationale table in `docs/SPEC.md` §5.9):

| source_type | Prior | Rationale |
|---|---|---|
| `debugging` | 0.70 | Evidence-driven: root cause found, fix applied, test passes |
| `implementation` | 0.65 | Confirmed by working code, no causal story |
| `code_review` | 0.60 | Human-validated, often style not deep insight |
| `architectural_reasoning` | 0.50 | Sound reasoning, unvalidated by implementation |
| `observation` | 0.45 | Single observation, needs corroboration |
| `planning` | 0.35 | Future-oriented, untested intentions |

Alias resolution (`get_base_prior`, `ec/config.py:366-377`): the extractor prompt emits `review` (→ `code_review`) and `session` (→ `observation` = 0.45); the `session` prior is a documented interpolation since the spec table lacks it.

Scope multipliers (`ec/config.py:75-83`): `engineering 1.15, domain 1.10, organization 1.05, project 1.00, repo 0.90, module 0.80, subsystem 0.75` — universal principles are more trustworthy than module-specific details.

Corroboration counting (`count_evidence_sources`, `ec/extractor.py:105-114`): the prompt marks multi-source evidence with `+`-separated sources in `evidence_pointer` (e.g. *"Agent reasoning step 4 (code analysis) + step 8 (test confirmation)"*); the count of non-empty `+`-separated parts is `n_sources`. Defaults: `corroboration_bump = 0.05` per independent source, `corroboration_bump_cap = 0.15` (max 3 sources counted).

Spec examples: debugging@engineering → 0.70×1.15 = 0.805 → 0.81; architectural@module → 0.50×0.80 = 0.40; observation@domain → 0.45×1.10 ≈ 0.50.

### 2.4 Confidence decay (lazy) — exact math

**Decision D34** (`docs/handoffs/V2_COMPLETION_DESIGN.md:1131`): decay is *lazy* — computed at retrieval time, never written on a timer. The stored value is always "the value set by the last event" (Diffuser update, reinforcement, status transition); the effective value is derived. `effective_confidence()` (`ec/confidence.py:92-131`):

```
L_eff = logit(c_stored) − lambda_decay[scope_level] × elapsed_days
c_eff = sigmoid(L_eff)
```

where `elapsed_days = max(0, now − last_reinforced)` in seconds/86400 (future timestamps never boost), and the decay clock is `metadata.last_reinforced`, falling back to `provenance.created_at` for never-reinforced ECUs (`ecu_effective_confidence`, `ec/confidence.py:134-154` — §2.6: an unreinforced ECU decays from creation). The convenience wrapper raises for session ECUs — session ECUs are ephemeral and never decay.

Scope-dependent decay rates (`ec/config.py:97-105`; per-day, log-odds space):

| Scope | λ/day | Half-life ≈ ln(2)/λ |
|---|---|---|
| engineering | 0.001 | ~693 days |
| domain | 0.003 | ~231 days |
| organization | 0.004 | ~173 days |
| project | 0.005 | ~139 days |
| repo | 0.01 | ~69 days |
| module | 0.02 | ~35 days |
| subsystem | 0.03 | ~23 days |

**Frozen statuses** (`FROZEN_STATUSES`, `ec/confidence.py:76-78`): `open_question`, `superseded`, `deprecated`, `archived` return the stored value unchanged — parked/terminal beliefs keep the value they had at transition so the audit trail stays intact (§17.5).

Why lazy (design doc §2.2): eager per-tick decay would need O(N) writes every interval, produce stale reads between ticks, and risk double-counting on restart; lazy computation is exact to the second and idempotent.

### 2.5 Eager confidence changes (writes)

Confidence is *written* (the stored value changes) only by events:

1. **Diffuser Bayesian updates** — `support_update` / `contradiction_update` applied by `_handle_support` / `_handle_contradiction` (`ec/diffuser.py:566-658`) during full diffusion, and by pending-update application at the review gate (`ec/review_gate.py:764-806`).
2. **Retrieval reinforcement** — after every `ec_query`, the MCP server bumps the *stored* confidence of every surfaced canonical ECU (`ec/mcp_server.py:481-526`, `_record_retrieval_metadata`):

   ```
   c' = sigmoid(logit(c_stored) + alpha_retrieval)    # alpha_retrieval = 0.05
   ```

   (`reinforce_stored_confidence`, `ec/confidence.py:157-167`). It also sets `last_reinforced = now` — **resetting the decay clock** — plus `last_retrieved` and `retrieval_count += 1` (D17). Frozen-status ECUs get the bookkeeping stamps but **skip the bump** (§17.5: reinforcement must not unfreeze parked beliefs by stealth). Session ECUs are never stamped.
3. **Open-question resolution option (b)** — the preferred ECU gets the same `alpha_retrieval` bump (`ec/review_gate.py:540`).
4. **Reconsolidation** — support/contradiction updates on the target with the pseudo-ECU's `PSEUDO_CONFIDENCE = 0.5` as `c_A` (`ec/reconsolidation.py:161-186`).
5. **Edge pruning reversal** — `reverse_update` in `maintainer.task_edge_pruning` (`ec/maintainer.py:307-370`).
6. **Lightweight (session) bumps** — `c' = c ± alpha_light × similarity` clamped to [0.05, 0.95] (D7), a plain probability-space approximation in the session brain only (`ec/diffuser.py:788-791`); exact log-odds deltas are recomputed by the full diffuser on promotion (D16).

### 2.6 Exact thresholds

All in `ec/config.py:92-107`:

| Key | Value | Effect | Code |
|---|---|---|---|
| `theta_supersede` | 0.3 | Below this, supersession can trigger (both §15.5 conditions required) | `should_supersede`, `ec/confidence.py:241-255` |
| `theta_dep_reevaluate` | 0.4 | Below this, `depends_on` dependents are marked `challenged` | `propagate`, `ec/confidence.py:292-336` |
| `theta_contradiction_flag` | 0.7 | Both sides above → flag to user | `ec/diffuser.py:641-652`, `ec/review_gate.py:283-292` |
| `prior_cap` / `prior_floor` | 0.95 / 0.05 | Prior clamp; also lightweight-bump clamp (D7) | `ec/config.py:84-85` |
| `confidence_flag_threshold` | 0.3 | "⚠️ Low confidence" flag in retrieval output | `ec/retrieval.py:440-445` |
| `confidence_very_low_threshold` | 0.2 | "⚠️ Very low confidence" flag | same |
| `alpha_retrieval` | 0.05 | Reinforcement bump size | `ec/config.py:106` |
| `max_propagation_depth` | 2 | Bounded transitive propagation (§15.7) | `ec/config.py:107` |

**Supersession trigger** (`should_supersede`): BOTH conditions must hold — (1) old confidence < 0.3, (2) a replacement ECU exists. "You don't supersede a belief just because it's weakened; you need a replacement." Classification of the supersession (`supersession_type`, `ec/confidence.py:258-285`): embedding cosine of the two cognitions ≥ `COSMETIC_SIMILARITY = 0.92` → `cosmetic` (rewording), else `semantic`; embedding failure defaults to `semantic` (never guesses cosmetic).

**Propagation** (`propagate`, §15.7): BFS over inbound `depends_on` edges; if the changed ECU's confidence < 0.4, dependents are marked `challenged` (terminal statuses skipped); transitive but bounded to `max_propagation_depth = 2`; a challenged dependent's own dependents re-check at the next depth. Returns newly challenged ids.

---

## 3. Extraction Pipeline

### 3.1 Components and call chain

Raw agent text becomes ECUs through `ec/extractor.py` (plumbing) + `ec/prompts/extractor_prompt.md` (the methodology — loaded at runtime, never paraphrased into code, per repo AGENTS.md and `ec/extractor.py:19`).

```
Agent calls MCP tool ec_observe(user_prompt, reasoning_trace, final_output?)
  └─ mcp_server._tool_observe (ec/mcp_server.py:530-607)
       ├─ resolve current session (per-call DB lookup, D15) — else NO_SESSION_ERROR
       ├─ build interaction dict {prompt, reasoning_trace, output?, session_context}
       ├─ extractor.extract_and_store_session(brain, session_id, interaction,
       │        commit_hash=_current_commit_hash(repo_path))      (D49)
       │    ├─ _format_interaction → one user message (§5.2)
       │    ├─ load_extractor_prompt() → system prompt        (cached; EC_EXTRACTOR_PROMPT
       │    │                                                  env override, package path, cwd)
       │    ├─ call_llm(user_message, system=…)               (claude-haiku-4-5, temp 0.3)
       │    ├─ extract_json() + json.loads → {"ecus": [...], "rejected_count", "rejection_summary"}
       │    ├─ validate_ecu() per candidate                   (invalid → skipped, batch survives)
       │    ├─ _assign_priors() per valid candidate           (§5.9)
       │    └─ stamp commit hash → embed cognition → brain.insert_session_ecu
       └─ diffuser.diffuse_session_ecu(ecu_id) per stored ECU  (lightweight diffusion;
                                            best-effort, offline-safe — failures counted,
                                            never fatal)
```

### 3.2 The model and call parameters

`call_llm` (`ec/llm.py:67-108`) with defaults from `ec/config.py:175-184`:

```yaml
llm:
  provider: "opencode_zen"
  model: "claude-haiku-4-5"
  base_url: "https://opencode.ai/zen/v1"
  api_key_env: "OPENCODE_ZEN_API_KEY"
  timeout: 60
  temperature: 0.3              # extraction / diffusion
  temperature_mode_detection: 0.0
  max_tokens: 4096
```

Wire format is derived from the model name (`_api_format`, `ec/llm.py:58-64`): `claude*` → Anthropic Messages (`POST {base_url}/messages`, `x-api-key` header); everything else → OpenAI Chat Completions (`POST {base_url}/chat/completions`, Bearer only when a key is present — so Ollama works keyless; D48).

### 3.3 The prompt (what is actually sent)

**System prompt:** the full text of `ec/prompts/extractor_prompt.md` ("Extraction Prompt v1.0 (Open Tier)", MIT license, 613 lines). Its components:

1. **ECU definition + IS/IS-NOT lists** — an ECU is not a summary, code description, repo fact, process step, snippet, or unjustified preference; it is a learned conclusion that would change future behavior.
2. **The core Information-vs-Conclusion distinction** — *"Information describes what the code IS"* ("TokenManager.refresh() is called before cache.clear()") vs *"Conclusion describes what was LEARNED"* ("Authentication correctness depends on optimistic token refresh…").
3. **The Lifting Test** (3 tests, quoted in Section 1.1).
4. **The 8 conclusion types** with descriptions and examples (implication / constraint / principle / decision / observation / pattern / invariant / trade-off). "If you cannot classify a candidate into one of these types, it is likely information… and should be rejected."
5. **"What NOT to Extract"** — 10 patterns (raw code descriptions, process steps, obvious implementation details, conversational filler, facts without engineering significance, unevidenced hypotheticals, code snippets, conversation summaries, unjustified preferences, TODO items).
6. **Atomization Rule** (Section 1.1).
7. **Input handling** — scan the ENTIRE interaction (prompt, reasoning trace, output, session context); "Process steps themselves… are NOT extracted. But the FINDINGS within those steps ARE extracted." Zero ECUs is a valid, correct output.
8. **Signal catalog** — 7 categories (architecture, decision, pattern, constraint/invariant, debugging/investigation, implementation insight, workflow). A signal says "something was learned here"; extract the *what was learned*, never the signal.
9. **Hypothesis handling** — validated → extract; disproven → extract the *insight from why it was wrong*; unresolved → don't, unless it reveals a durable constraint.
10. **Extraction priority** — validated findings > decisions > architectural discoveries > constraints/invariants > patterns > implementation insights.
11. **Multi-source corroboration** — note independent sources in `evidence_pointer` with `+` separators; the system uses this for the prior.
12. **Output format:**

```json
{
  "ecus": [
    {
      "cognition": "self-contained conclusion statement",
      "conclusion_type": "implication | constraint | principle | decision | observation | pattern | invariant | trade-off",
      "scope": { "level": "engineering | domain | organization | project | repo | module | subsystem",
                 "path": "engineering > domain:web-frameworks > repo:fastapi" },
      "source_type": "session | debugging | implementation | planning | review | architectural_reasoning",
      "grounding": { "files": ["path/to/file.ext"], "symbols": ["ClassName::methodName"],
                     "code_context": "optional" },
      "evidence_pointer": "where in the input this was derived from ('A + B' for multi-source)"
    }
  ],
  "rejected_count": 0,
  "rejection_summary": "e.g. '3 raw code descriptions, 1 process step, 1 hypothetical without evidence'"
}
```

13. **Field guidelines** for each field (including: "`scope`: The level at which this conclusion operates. A conclusion about a specific repo's auth flow is `repo` scope…"; "`source_type`: …This determines the initial confidence prior").
14. **15 few-shot examples** — debugging race condition, dependency-direction violation, error-handling pattern, caching trade-off, REST-vs-GraphQL decision, CI-timezone observation, mocking principle, connection-pool constraint, global-scope principle, FastAPI-upgrade implication, factory pattern, **zero-ECU case**, multi-ECU case, invariant, rejected-approach decision. Each shows the extracted ECU and the rejected candidates with reasons.
15. **Execution instructions** — an 11-step process (read → identify conclusion-producing segments → extract candidates → Lifting Test → classify → atomize → ground → scope → **Self-Review Pass** → count rejections → output) plus **Critical Reminders** ("Quality over quantity", "When in doubt, don't extract", "Resist the urge to produce… Producing weak ECUs to avoid returning nothing is the single most harmful behaviour you can exhibit").

**User message** — `_format_interaction()` (`ec/extractor.py:195-215`) concatenates whichever of these are present as markdown sections (at least one required):

```
## Session Context
## Developer Prompt
## Agent Reasoning Trace
## Agent Final Output
```

### 3.4 Pipeline steps (post-LLM)

1. **Fence-strip + parse** — `extract_json()` (`ec/llm.py:38-49`) finds the first `{` and last `}` when fences are present (exact implementation from the spec's §15 JSON post-processing block); `json.loads` must yield a dict with an `ecus` array, else `ExtractionError` with a 200-char response prefix.
2. **Validate each candidate** (`validate_ecu`) — vocabulary enforcement as above; `grounding` must be an object; unknown `source_type` raises (via `get_base_prior`).
3. **Assign priors** (`_assign_priors`) — Section 2.3.
4. **Stamp provenance** — `source_id` = session id; `origin_agent` = extractor model name.
5. **Stamp commit hash** (`stamp_grounding_commit_hash`, D49) — server-side, between extraction and storage.
6. **Embed + store** — one `encode_one(cognition)` per ECU; `insert_session_ecu` (review_status `pending`).
7. **Lightweight diffuse** (separate step, `diffuser.diffuse_session_ecu`) — the Extractor never creates edges (§5.13).

### 3.5 How associations are (not) detected during extraction

**The Extractor creates no edges.** This is stated in the module docstring (`ec/extractor.py:18-19`), the spec (§5.13: "Does NOT create edges between ECUs (that's the Diffuser's job)"), and the prompt (the output schema contains no edge field; SPEC §5.3: "`edges` are **empty** — edges are the Diffuser's job"). Relationship detection happens later: the lightweight diffuser (3-way supports/contradicts/unrelated, similarity threshold 0.7) runs per `ec_observe`, and the full diffuser (5-way) runs at promotion. Scope level is likewise assigned by the LLM inside the prompt (guided by the prompt's `scope` field guidelines), then merely *validated* by `validate_ecu` against the 7 allowed levels; if the LLM omits `path`, the level name is stored as the path (`ec/extractor.py:161`).

### 3.6 Extraction quality evidence

- Prompt-selection testing (spec §15 LLM comment): Haiku 4.5 "caught 4x more genuine conclusions than the 7B, valid format compliance, working Self-Review Pass" vs qwen2.5:7B / qwen2.5-coder:14B / gpt-5.4-mini.
- Live audit (`docs/ASSESSMENT.md` §2 step c): a real FastAPI investigation yielded `ecus_extracted=2, ecus_rejected=2` via the Lifting Test; §4: "precise `constraint`/`principle` ECUs with sensible scopes/priors, and the Lifting Test rejected narrative filler."
- `rejected_count` / `rejection_summary` are surfaced in the `ec_observe` payload (D56, `ec/mcp_server.py:601-602`) to make filtering visible and debuggable (§5.3).

---

## 4. Brain Structure

### 4.1 Session Brain vs Canonical Brain

The hippocampus/neocortex parallel (`docs/SPEC.md` §6.1–6.3). Both live in **one SQLite database** ("two brains, one database", `ec/schema.sql:1-10`):

| | Session Brain | Canonical Brain |
|---|---|---|
| Tables | `sessions`, `session_ecus`, `session_edges`, `pending_updates` | `ecus`, `edges` |
| Review required | No (`insert_session_ecu` docstring: "no review required, §6.2") | Yes — only path is Review Gate → Diffuser |
| Lifetime | Per session (resumable); skipped/pending ECUs carry to the next session for the same (repo, branch) | Forever |
| Diffusion | Lightweight (3-way, α_light bump, no supersession/propagation/depends_on) | Full (5-way, log-odds, contradictions, supersession, propagation) |
| Decay | Never (ephemeral) | Lazy scope-dependent decay |
| Retrieval | Participates (trust weight 0.8) | Participates (trust weight 1.0) |
| Clustering | Never (§4.4) | Yes (Maintainer task) |

The hard boundary (§6.7, D5): *"The Session Brain never writes to the Canonical Brain… No exceptions."* When session evidence relates to a canonical ECU, the lightweight diffuser records a `pending_updates` row instead (`ec/diffuser.py:796-812`) and sets `has_pending_updates=True` on the canonical ECU; the review gate later applies (exact recomputed deltas, never double-applied — D16) or discards these rows (`review_gate._resolve_pending_updates`, `ec/review_gate.py:750-813`).

Sessions are keyed by `(repo_path, branch)` (`ec/session.py:91-107`, git-detect or `EC_REPO_PATH`/`EC_BRANCH` env override; detached HEAD → `detached:<short-hash>`). One active session per (repo, branch); `/ec-start` resumes it, carrying pending/skipped ECUs + their session edges + pending updates over from closed sessions (`Brain.reassign_session_rows`, `ec/brain.py:1060-1103`).

### 4.2 Session → Canonical: the transition

**Trigger:** session close via `/ec-stop` (`python -m ec.session stop`), or programmatic `review_gate.apply_review_decisions` on demand. The flow (`ec/review_gate.py::apply_review_decisions`, lines 846-971):

1. **Grouping** — candidates (review_status `pending|skipped`) grouped by `scope_path` (D18) or, if `review_gate.grouping_strategy: "cluster"`, by HDBSCAN cognitive cluster with scope fallback (D36; `_cluster_groups`, `ec/review_gate.py:108-155` — a candidate joins the cluster of its most-similar canonical ECU above the diffuser relevance threshold, or keeps its own scope group).
2. **Accept → promote** — `_promote()` copies the session ECU into `ecus` with the same id (D21), applying an optional human edit: the edited cognition replaces the text, is re-embedded, and the original is preserved under `metadata.review_edit = {"original_cognition": …, "edited_at": …}` (D19, §7.3).
3. **Full Diffuser** — `diffuser.diffuse_ecu()` runs the 9-step integration (Section 6.2) against the Canonical Brain. Promotion stands even if diffusion fails offline (the human decided); the ECU lands in `result.undiffused` and both CLI paths print a prominent warning (`diffusion_failure_warning`, `ec/review_gate.py:974-988` — "These ECUs have no edges and unchanged confidence").
4. **Reject → delete** — removed from `session_ecus` (§28.6 takes precedence over the spec §7.5 "remain until session end" wording; reconciled in `docs/SPEC.md:763`).
5. **Skip → retain** — `review_status = 'skipped'`; available at the next review; carried into the next session on `/ec-start`.
6. **Pending updates** — accepted sources get exact log-odds deltas recomputed from embedding cosine (or marked applied, no second update, if the diffuser already edged the pair — D16); rejected sources discarded; skipped stay pending; `has_pending_updates` cleared when none remain.
7. **Notifications/flags** — Section 10.4.
8. **Cleanup + close** — session edges/rows for accepted+rejected deleted, open-question persistence sweep runs, grounding-deprecation notifications aggregated, session marked `closed`, activation rows dropped ("fresh slate next time", §12.2), a `review_gate` row appended to `maintenance_log`.

### 4.3 Reconsolidation

**Concept** (`ec/reconsolidation.py` docstring): retrieval makes a canonical ECU *labile*; when the agent discovers new evidence about it, `ec_reconsolidate` feeds that evidence back through the Diffuser. The change is written to the Canonical Brain **immediately** — the ECU was already human-reviewed when it entered; reconsolidation is an evidence update, not a new ECU entering (§5.5).

**Gatekeeping (D39):** only ECUs retrieved in the current session are labile. `SessionManager` keeps an in-memory per-session `_labile` set (`ec/session.py:225-238`); `mcp_server._record_retrieval_metadata` marks surfaced canonical ids labile after every `ec_query`; `_tool_reconsolidate` refuses others with actionable guidance ("you can't reconsolidate something you haven't looked at", `ec/mcp_server.py:685-696`). Additional validations: ECU must exist and have a retrievable status (`active|challenged|open_question`); `relationship` if given must be in `("supports", "contradicts", "supersedes")`.

**Mechanics (D38, design doc §5.3):** the evidence becomes a **pseudo-ECU** (`_build_pseudo_ecu`, `ec/reconsolidation.py:109-126`):

```python
{
  "id": f"pseudo::{uuid4()}",
  "cognition": evidence,
  "conclusion_type": target["conclusion_type"],   # inherited
  "scope": dict(target["scope"]),                 # inherited
  "grounding": dict(target.get("grounding")),     # inherited
  "confidence": PSEUDO_CONFIDENCE,                # 0.5 — neutral, evidence is unreviewed
  "provenance": {"source_type": "reconsolidation", "created_at": now},
}
```

The pseudo-ECU runs the standard find-related → classify → update pipeline via the shared `_diffuse_against_brain(..., store_edges=False, exclude_ids=(ecu_id,))` (D38 — an unstored source cannot own edges, and §15.5's second supersession condition is unmet without a stored replacement).

**Per-relationship outcomes** (`reconsolidate`, `ec/reconsolidation.py:217-342`):

| Resolved relationship | What happens | `action_taken` |
|---|---|---|
| `supports` | `support_update` on target with c_A = 0.5; evidence appended to `evidence_pointers` as `"reconsolidation: <evidence[:280]>"`; audit note in `metadata.reconsolidations` | `confidence_updated` |
| `contradicts` | `contradiction_update`; status → `challenged` + `last_challenged`; §15.7 propagation; audit note | `challenged` |
| `supersedes` AND trigger passes (c < 0.3) | Evidence **materialized** as a real canonical ECU (source_type `reconsolidation`, confidence 0.5 — NOT inherited, §15.4); `_apply_supersession` creates the `supersedes` edge and freezes the old ECU | `superseded` |
| `supersedes` AND trigger fails | Falls back to contradiction handling — "an agent asserting 'this belief is outdated' is never evidence FOR it; the honest state is contested" | `challenged` |
| `unrelated` (or misclassified depends_on) | Nothing changed | `no_change` |

If the agent passes an explicit `relationship`, classification and §17.2 adjudication are skipped for the target pair (the agent made the semantic judgment against live code); otherwise one batched classification call decides.

**Related-ECU pass (§5.3 step 5):** the evidence may affect other beliefs — the pseudo-ECU (or the materialized replacement) is diffused against the rest of the brain (best-effort; failure recorded in `result.diffusion_error`, the target update stands).

**Audit trail:** edges cannot reference the unstored pseudo-ECU, so `_append_audit_note` writes `metadata.reconsolidations` entries: `{"at", "relationship", "evidence" (≤280 chars), "confidence_delta", "via": "ec_reconsolidate"}` (`ec/reconsolidation.py:136-151`).

**Confidence during reconsolidation, precisely:** the target's update magnitude is `w × r(evidence, target) × 0.5` in log-odds space (the neutral pseudo-confidence is c_A). On the supersession path the *new* ECU starts at its own 0.5; the old ECU's confidence is frozen, never transferred.

---

## 5. Scope Hierarchy

### 5.1 The seven levels

`SCOPE_LEVELS` (`ec/config.py:292-295`), general → specific (`docs/SPEC.md` §1.2/§11.11):

```
engineering          (universal engineering principles)
  > domain           (domain-specific knowledge, e.g. web-frameworks)
    > organization   (org-specific conventions)
      > project      (project-level understanding)
        > repo       (repository-specific)
          > module   (module-specific)
            > subsystem (most granular)
```

An ECU's `scope` is `{level, path}`; `path` is a hierarchical label like `"repo:myapp > module:auth"` (prompt output format) or an extractor default (the level name itself).

### 5.2 Scope × confidence prior

Scope multiplies the base prior (Section 2.3): `engineering 1.15, domain 1.10, organization 1.05, project 1.00, repo 0.90, module 0.80, subsystem 0.75`, capped at 0.95. Rationale (spec §5.9 table): universal principles are rarely wrong; subsystem details are most volatile.

### 5.3 Scope × decay

λ_decay per day (Section 2.4): engineering 0.001 → subsystem 0.03. Broad truths decay slowest.

### 5.4 Scope × retrieval

Three independent mechanisms in `ec/retrieval.py`:

1. **Explicit restriction (§16.3)** — the `scope` parameter of `ec_query` hard-filters to one level (`retrieve()` line 637-638). Distinct from the proximity multiplier.
2. **Search-UP-the-hierarchy filter (§11.11, D50)** — `_filter_by_scope_hierarchy` (`ec/retrieval.py:169-218`) anchors at the session's repo (`_working_scope_from_session` → `("repo", session.repo_path)`; an explicit `scope=` narrows the anchor level). An ECU is kept when its level is **UP** (smaller index — `engineering`/`domain` unconditionally as universal; intermediate levels when their path is compatible), **same level with the same context** (path prefix or the basename "convention bridge" — `PurePath(working_path).name` appearing in the ECU's label path, so `repo:api-server` ECUs match an `/x/api-server` session), or has an unknown level/path (never over-filter). **DOWN and sideways are dropped.** If the filter empties the candidate list, the unfiltered set is restored ("an empty result is worse than a broad one").
3. **Scope proximity multiplier (§11.11)** — `rank *= scope_proximity[level]` (`ec/retrieval.py:293-294`): `subsystem 1.0, module 0.85, repo 0.7, project 0.55, organization 0.4, domain 0.25, engineering 0.1`. Spec: *"a multiplier, never a filter"* — a highly relevant engineering-scope ECU can still outrank a weakly relevant module-scope one.

### 5.5 Scope × grounding deprecation

`should_deprecate` (`ec/grounding.py:137-159`) and its scope sets (`ec/grounding.py:59-65`):

| Scope set | Members | Rule |
|---|---|---|
| `NEVER_DEPRECATE_SCOPES` | engineering, domain | Principles survive code deletion (§21.4) |
| `ALL_FILES_GONE_SCOPES` | organization, project | Deprecate only when **all** grounding files are gone |
| `ANY_GONE_SCOPES` | repo, module, subsystem | Deprecate when **any** grounding file is deleted **or any symbol vanished** — "code-specific ECUs die with their code" |

### 5.6 Scope × competing-hypothesis persistence

`contradiction.competing_hypothesis_persistence` (`ec/config.py:250-258`), in days: engineering `null` (indefinite), domain `null`, organization 75, project 90, repo 60, module 30, subsystem 20. These drive the `open_question` parking sweep (Sections 8.3 and 10.4).

### 5.7 Scope × clustering

**No effect.** Clustering is purely embedding-based (`ec/clustering.py`); scope is not a feature. Session ECUs are never clustered, and only `cluster_statuses` (`active|challenged|open_question`) participate.

---

## 6. Associations and Clustering

### 6.1 Edge types

Four canonical types (`EDGE_TYPES`, `ec/config.py:305`; table from spec §14.1):

| Type | Direction | Semantics | Confidence effect on target |
|---|---|---|---|
| `supports` | A → B | A provides evidence strengthening B | `L_B += w × r × c_A` |
| `contradicts` | A → B | A weakens B (same scope/context, mutually exclusive) | `L_B −= w × r × c_A`; B may become `challenged` |
| `depends_on` | A → B | B must be valid for A to be meaningful | None directly; flows through §15.7 propagation |
| `supersedes` | A → B | A replaces B as current belief | B's confidence frozen; status `superseded` |

Edge structure (`edges` table + `Brain._row_to_edge`): `{id, source_id, target_id, type, weight (0–1, = semantic similarity for supports/contradicts), confidence_delta (log-odds, reversible), supersession_type (cosmetic|semantic), created_at}`. One edge per `(source, target, type)`; weight accumulates capped at 1.0. `supersedes` edges are **never pruned** (audit trail, §14.5/D52). Session edges (`session_edges`) support only `supports|contradicts` and may target canonical ECUs (`target_type`), enabling cross-brain traversal.

### 6.2 How associations are created

1. **Full Diffuser (Canonical)** — `diffuse_ecu` / `_diffuse_against_brain` (`ec/diffuser.py:391-529`). Steps (§8.3): (1) load ECU (must be retrievable-status); (2) `find_similar` over canonical embeddings with `threshold = diffuser.relevance_threshold = 0.6`, top_k ≤ `MAX_CLASSIFY_BATCH = 10`; (2b) **structural proximity** (D54) — one hop along existing canonical edges from the semantic candidates, ≤ `STRUCTURAL_PROXIMITY_MAX_EXTRA = 5` extra, each inheriting its seed's similarity; (3) one batched 5-way classification LLM call at temperature 0.0 (D29) — `unrelated` candidates discarded; (4-8) per-relationship handlers (below); (9) result.
2. **Lightweight Diffuser (Session)** — `diffuse_session_ecu` (`ec/diffuser.py:716-813`): 3-way classification, threshold 0.7, `c' = c ± alpha_light(0.1) × similarity` on session targets; canonical targets become `pending_updates` only. No supersession/propagation/challenged/depends_on.
3. **Pending-update application** — at the review gate, exact §15.2/§15.3 deltas recomputed from cosine; creates canonical edges (`ec/review_gate.py:774-795`).
4. **Open-question resolution (b)** — a real `supersedes` edge (semantic) from winner → loser (`ec/review_gate.py:544-546`).
5. **Reconsolidation supersession** — `_apply_supersession` on the materialized ECU.
6. **Maintainer supersession pass** — `_supersession_pass` creates `supersedes` edges for decayed-but-replaced ECUs (`ec/maintainer.py:182-227`).

**Handler behavior** (`ec/diffuser.py:566-709`):
- `_handle_support` — edge + in-place Bayesian update + opposing-edge check + propagation + post-update supersession check.
- `_handle_contradiction` — **two-stage** handling (below), then edge + update + `challenged` + `last_challenged` + high-stakes flag + propagation + post-update supersession check. Never auto-resolves (§17.3).
- `_handle_depends_on` — structural edge only, `confidence_delta = 0.0` (§11 defines no direct confidence effect).
- `_handle_supersedes` — the LLM proposal is honored **only if** `should_supersede` passes; otherwise downgraded to `supports` (D8: "LLM proposes relationships; the spec's computable rules decide"). With `store_edges=False` (reconsolidation), always downgraded.
- `_apply_supersession` — `supersedes` edge (weight 1.0), old status → `superseded` (confidence frozen), classification via embedding cosine vs `COSMETIC_SIMILARITY = 0.92`; the new ECU's confidence is its own, never inherited.

### 6.3 The classifier prompts and disambiguation rules

`CLASSIFICATION_SYSTEM` (`ec/diffuser.py:66-139`) defines the 5-way task and embeds **Rule A** and **Rule B** verbatim from spec §8.3:

> **Rule A — supports vs depends_on (primary relationship test):** 1. Does the new ECU provide evidence that strengthens the existing ECU's conclusion? …If yes → supports. 2. Only if the answer to (1) is NO, ask: Is the new ECU structurally dependent…? If yes → depends_on. 3. If neither → unrelated. *The key insight: depends_on is reserved for cases where the **only** relationship is structural… Evidence-based support takes precedence over structural dependency.* (Includes the webhook-idempotency example and the PostgreSQL counter-example, plus: "If ECU A is a recommendation, decision, or implication that follows logically from ECU B…, the relationship is 'supports', never 'contradicts'. A 'contradicts' classification requires that the two ECUs make mutually exclusive claims.")

> **Rule B — unrelated threshold for same-subsystem ECUs:** *"Two ECUs about the same subsystem but addressing different engineering concerns are unrelated unless one directly informs or constrains the other. Sharing a subsystem is necessary but not sufficient… Test: 'Does knowing ECU_A change my confidence in ECU_B?'"*

Also: *"Classify supersedes ONLY when ECU_A states the same belief as ECU_B in an updated or replaced form… not merely when it contradicts it."* Output contract: `{"classifications": [{"pair": n, "relationship": "..."}]}`; tolerant parse — any omitted/mislabelled pair defaults to `unrelated` (§8.6: spurious edges are the harmful failure mode). Classification temperature is 0.0 (D29, determinism).

Measured quality (spec §8.6, Claude Haiku 4.5, 32 directed pairs): lightweight 3-way 29/32 (90.6%); full 5-way 23/32 raw → 28/32 (87.5%) after correcting ground truth per Rule A. Live audit probe (`docs/ASSESSMENT.md` §4): 5/6, with the residual failure mode being spurious `contradicts` (Rule A violation) at temperature 0.3 — the motivation for D29.

### 6.4 Contradiction handling (§17.2, two-stage)

**Stage 1 — computable pre-filter** (`_contradiction_prefilter`, `ec/diffuser.py:315-354`), no LLM:
1. Different scope levels → `orthogonal`, **unless** grounding files overlap **or** similarity > `PREFILTER_SIMILARITY_FLOOR = 0.8` (D40 fix: the extractor's noisy scope assignment was silently killing genuine contradictions; `docs/ASSESSMENT.md` §4.2).
2. Same scope, no grounding-file overlap → `no_overlap` (weak evidence; Stage 2 decides).
3. Condition/qualifier extraction — regex `_QUALIFIER_RE = r"\b(?:for|when|in|under)\s+([a-z][a-z0-9_\- ]{2,40}?)(?:[,.;]|$)"`; disjoint explicit conditions on both sides → `orthogonal`.
4. Otherwise → `potential`.

**Stage 2 — LLM adjudication** (`_adjudicate_contradiction`, temperature 0.0) — verdict `genuine_contradiction | orthogonal | unrelated` + a `differentiator` for orthogonal. Fail-safe: any LLM/parse problem → `genuine_contradiction` ("challenged is recoverable; a missed contradiction is not").

**Case 3 (competing hypotheses)** — computable signals in `review_gate._is_case3` (`ec/review_gate.py:232-247`): same scope level + overlapping grounding + both `source_type ∈ {debugging, investigation}` + both confidence in [0.30, 0.60] → both get `metadata.competing_since` (first detection only; `_mark_competing`). Resolution paths in Sections 8.3/10.4.

### 6.5 Clustering

`ec/clustering.py` — cognitive clusters emerge from canonical ECU embeddings (§9.5: no pre-installed categories).

- **What is clustered:** only canonical ECUs with status in `clustering.cluster_statuses` (`active|challenged|open_question`) **and** a non-null embedding. Session ECUs never; vectorless ECUs simply stay unclustered.
- **Algorithm:** HDBSCAN (`hdbscan_labels`, `min_cluster_size = 3`, `min_samples = 2`) with per-cluster `cluster_persistence_` as stability. Fallback (design doc §4.7 / missing wheel): `agglomerative_labels` — scipy average-linkage over cosine distance, largest dendrogram cut (k = 2..12) where every cluster has ≥ `min_cluster_size` members; "stability" = mean intra-cluster pairwise cosine similarity (`_intra_coherence`).
- **Ephemerality:** clusters are recomputed **wholesale** each run — `brain.clear_cluster_memberships()` deletes all `clusters` + `cluster_memberships` rows first (HDBSCAN labels are not stable across runs; `ec/brain.py:1224-1233`). Noise (label −1) and clusters with stability < `stability_threshold` (0.6) get no membership rows.
- **Trigger:** `maintainer.task_clustering` (`ec/maintainer.py:373-401`) runs only when ≥ `clustering_threshold` (100) **new** canonical ECUs exist since the last run (baseline `maintenance_state['last_clustering_ecu_count']`); skipped as `too_few_ecus` below `min_cluster_size` eligible ECUs. At 50–100-ECU scale it rarely fires — infrastructure for scale.
- **Consumers:** review-gate cluster grouping (D36, Section 4.2). Storage: `clusters` + `cluster_memberships` tables (Section 1.3).

**What determines which ECUs cluster together:** semantic density of their 384-dim cognition embeddings — nothing else. Clusters "evolve" only by wholesale recompute; there is no incremental membership update.

---

## 7. Retrieval Mechanism

### 7.1 Demand-driven contract

`ec/retrieval.py` docstring, §11.3: *"retrieval is PURE demand-driven — this module acts only when the agent explicitly queries (the `ec_query` path). Nothing here injects cognition proactively; that structural choice is anti-anchoring Defence 1."* There is exactly one caller path: MCP `ec_query` → `ECServer._tool_query` → `retrieval.retrieve()`. The no-session case returns the §28.4 error ("No active EC session. Suggest the user run /ec-start…"). `ec_get_summary` provides "awareness without anchoring" — brain statistics only, never ECU contents.

**What triggers a retrieval:** only the agent deciding to call `ec_query(query, scope?, mode?)`. `AGENTS.md` (`ec/install.py:44-207`, written verbatim to `~/.ec/AGENTS.md`) instructs: read the code FIRST; query when about to decide between approaches, when debugging and the cause isn't obvious, before architectural changes, etc. It cites the v1 evidence: "premature retrieval caused anchoring: quality dropped −0.589, groundedness −0.480."

### 7.2 Query construction and the 14-step pipeline

The query is a natural-language string supplied by the agent; it is embedded with the same all-MiniLM-L6-v2 model and matched by cosine similarity (dot product of L2-normalized vectors). The pipeline (`retrieve`, `ec/retrieval.py:574-744`; docstring lines 10-33):

1. **Resolve mode** — explicit `mode` param or `detect_mode(query)`; invalid → ValueError. `mode_detected` flag returned.
2. **Embed query → `find_similar` over BOTH brains** with `threshold=-1.0` (the full set — the gate and fallback live downstream), `include_session=True`, restricted to the caller's session when given, canonical statuses = `RETRIEVABLE_STATUSES`. `find_similar` (`ec/brain.py:1272-1327`) loads all embedding BLOBs and does numpy dot products (no vector DB — fine at 50–1000-ECU scale); returns `{"id", "similarity", "brain"}` sorted desc.
3. **Quiet-period activation fade** — `activation.apply_time_decay()` before scores are read (§11.10).
4. **(3) Explicit scope restriction** — if `scope` given, keep only that level.
5. **(3b) §11.11 UP-only hierarchy filter (D50)** — Section 5.4; unfiltered fallback if emptied.
6. **(4) Relevance gate (§11.5)** — hard filter at `relevance_gate_threshold = 0.3` before any ranking ("if an ECU isn't semantically relevant, nothing else matters"); **fallback:** if nothing passes and candidates exist, return `fallback_top_k = 5` by rank from the full set ("empty context is worse than weak context").
7. **(5-9) Four-factor ranking** (`_rank_candidates`, `ec/retrieval.py:237-337`) — Section 7.3.
8. **(10-11) Cognitive grouping + token-budget packing** — Section 7.4.
9. **(12) Cross-group dedup** — Section 7.4.
10. **(13) Spreading activation** on retrieved cores (`activation.spread(brain, [core ids], mode=mode)`) — Section 7.5.
11. **(14) Output** — §11.8 formatted text + §16.3 structured payload (Section 7.6).

`retrieve()` itself is read-only over the brain; the MCP layer performs the post-retrieval writes (Section 7.7).

### 7.3 Ranking — the exact algorithm

Per candidate (each with `similarity` and `brain`):

```
rank = w_relevance   × semantic_similarity        # 0.6
     + w_confidence  × confidence                  # 0.15 — EFFECTIVE (decayed) for canonical,
     + w_activation  × normalized_activation       # 0.10 — divide-by-max [0,1]
     + w_network     × network_richness            # 0.15 — min(edge_count, 10)/10
rank *= trust_weight        # session 0.8 | canonical 1.0
rank *= scope_proximity     # §11.11 multiplier (Section 5.4)
if status == "open_question": rank *= 0.5         # open_question_retrieval_weight
rank += mode_bonus           # +0.05 prioritize / +0.03 bias (Section 7.6-below)
```

Precise details (`ec/retrieval.py:258-337`):
- **Factor 2 uses effective confidence for canonical ECUs** (`ecu_effective_confidence`, lazy decay, D34); session ECUs pass stored confidence straight through. Display and flags always use the **stored** value — "decay affects ranking only."
- **network_richness** counts canonical degree (`brain.edge_count`) plus session edges (filtered to the session when given), capped: `min(n_edges, edge_count_cap=10) / 10`. Hubs bring more context via depth-1 traversal.
- **Challenged: no ranking penalty** (D10, §11.5 — "flagged, not deprioritised"); the token budget remains a hard constraint.
- **Mode bonus:** if `"contradictions" in mode_params["prioritize"]` (debugging's edge-type slot) → +0.05 for ECUs that are `challenged` **or** have any `contradicts` edge (`_has_contradicts_edge` via `brain.get_neighbourhood`); otherwise +0.05 if `conclusion_type ∈ prioritize`. Additionally +0.03 if `provenance.source_type == mode_params["bias"]` (bias `"none"` disables). Sorted descending by rank.

### 7.4 Cognitive grouping, budget packing, dedup

- **Group construction** (`_build_group`, `ec/retrieval.py:357-393`): the core ECU plus its depth-1 neighbourhood via `brain.get_neighbourhood` (both directions, both brains; all four edge types). Neighbours are included regardless of status ("a superseded ECU reached via a supersedes edge is exactly the context §11.4 asks for"). **Within-group dedup (§11.5 Rule 1):** each neighbour appears once, in the highest-priority slot — `_SLOT_PRIORITY = {contradicts: 0, depends_on: 1, supports: 2, supersedes: 3}` mapped to group slots `supporting_evidence / contradictions / dependencies / superseded_by`.
- **Token estimates** (`estimate_tokens`): `max(50, (len(cognition) + files + symbols) // 4)` — char heuristic, no tokenizer (accepted deviation; errs low, safe under restraint budgets).
- **Packing (§11.7)** (`_pack_groups`): walk scored entries; if the whole group fits the remaining mode budget → include; else if just the core fits → include with `truncated=True`, all slots emptied ("Context truncated due to token budget. Only core ECU included."); else **stop** — the budget is a hard constraint. Budgets per mode (Section 7.6).
- **Cross-group dedup (§11.5 Rule 2)** (`_apply_cross_group_dedup`): an ECU that is the core of group N and also appears in another group's slots is replaced there by `"see Group N for full context"`.

### 7.5 Spreading activation

`ec/activation.py` — session-scoped, time-decaying (§9/§12). Formulas (§12.4/§12.5):

```
boost(hop) = base_boost × decay_factor^hop        # 0.5 × 0.5^hop, hops 1..max_hops(2)
activation *= exp(−activation_decay_rate × elapsed_hours)   # rate 0.1/hour
normalized = score / max(scores)                  # divide_by_max, bounded [0, w_activation]
```

- **Spread** (`ActivationState.spread`, `ec/activation.py:116-174`): retrieved seeds accumulate the full `base_boost` (repeated retrievals keep warming); neighbours receive hop boosts via BFS from the seed set, max-merged across seeds/paths/existing scores (spec-formula implementation — D12, not the reference's "spread from every warm ECU" loop; both bounded identically by divide_by_max).
- **Mode-aware spread (§11.9, D9):** boosts crossing the mode's preferred edge type (`debugging → contradicts`, `architecture → depends_on`) are multiplied by `mode_spread_multiplier = 1.5`, **capped at one hop less of decay** (`min(base × decay^(hop−1), hop_boost × 1.5)`) — preferred edges cool slower, never amplify.
- **Persistence (§6.6, D13/D22):** scores + per-ECU timestamps live in `session_activation`; saved after every `ec_query` (`activation.save(brain)` in `_tool_query`); on resume, `ActivationState.load` re-applies `exp(−rate × elapsed)` for the break (5-minute break ≈ intact; 15-hour break ≈ decayed); on session close the rows are deleted — a new session starts with a clean slate (§12.2). ECU ids may belong to either brain (activation is per-session, not per-brain).

### 7.6 Modes and the output format

**Mode detection** (`ec/mode_detection.py`): LLM classification at temperature 0.0 (`"Classify this engineering query into one of: debugging, architecture, implementation, investigation, planning. Respond with only the mode name."`, max_tokens 16) with a keyword fallback (per-mode keyword lists, phrase hits weigh double, single words match exactly or as ≥4-char prefixes; default `investigation`) when no API key or the LLM fails — mode detection never blocks retrieval.

**Mode parameters** (`ec/config.py:193-224`):

| Mode | depth | max_tokens | prioritize | bias |
|---|---|---|---|---|
| debugging | 0 | 2000 | `["contradictions"]` (edge-type slot) | debugging |
| architecture | 1 | 6000 | decision, pattern, constraint | architectural_reasoning |
| implementation | 1 | 3000 | pattern, constraint | implementation |
| investigation | 1 | 4000 | pattern, decision | none |
| planning | 1 | 5000 | constraint, decision | planning |

**Formatted output (§11.8)** per group: `--- Group N (rank: X) ---`, `CONCLUSION`, `CONFIDENCE: <float> (<label high>0.7/>0.3/low, brain, reviewed|unreviewed)` + confidence flag, status flags (challenged with pointer to first contradiction; open_question with unresolved-since date), `SCOPE`, `TYPE`, `SOURCE`, `GROUNDING`/`SYMBOLS`, slot sections, truncation note, and always the framing note: *"This is past engineering understanding. Verify against current code before acting."* (§11.6 Defence 2).

**Structured result (§16.3)** returned by `retrieve()`: `{status, mode, mode_detected, budget, budget_used, groups_retrieved, groups: [{rank, core_ecu {id, cognition, conclusion_type, confidence, confidence_label, status, scope_level, scope_path, grounding, source_type, origin_agent, created_at, brain}, supporting_evidence, contradictions, dependencies, superseded_by, truncated, framing_note}], warnings, brain_source ("session"|"canonical"|"both"), message, formatted, fallback, filtered_out, cross_group_dedup_count}`. Warnings count low-confidence and challenged cores and note the fallback.

### 7.7 Post-retrieval writes (MCP layer)

`_tool_query` then: (1) `_record_retrieval_metadata` — stamps `last_retrieved`/`last_reinforced`/`retrieval_count` and applies the `alpha_retrieval` bump on **stored** confidence for surfaced canonical ECUs (cores + all neighbour slots; frozen statuses skip the bump), and marks them labile (D39); (2) `activation.save(brain)` (D22). If zero groups retrieved, the message becomes the §28.4 `NO_ECUS_FOUND` guidance.

---

## 8. Maintenance Loop

### 8.1 What it is and where it runs

`ec/maintainer.py` — the Cognition Maintainer keeps the Canonical Brain healthy. **Decision D33:** it runs *inside the MCP server process*, never as a separate daemon, in two modes:

- **Startup check (eager):** `ECServer.start_maintenance()` (`ec/mcp_server.py:366-391`) runs `run_maintenance` synchronously *before* the JSON-RPC loop if `is_maintenance_overdue`; failures are logged, never prevent serving.
- **Background thread (lazy):** `MaintainerThread` (daemon, `ec/maintainer.py:491-553`) checks every `check_interval_minutes = 5` and runs maintenance when a trigger fires. The thread opens its **own** SQLite connection over the same DB file (connections are thread-bound; WAL keeps reads concurrent). The first check fires one full interval after start; a crashed run never kills the thread.

**How often:** trigger-based — run when `now − last_run_at > time_threshold_hours (6h)` **or** `count(ecus) − last_run_ecu_count ≥ ecu_threshold (10)` (`is_maintenance_overdue`, `ec/maintainer.py:457-484`; never-run brains are overdue). Never-run/never-used periods are fine: on next start everything catches up in one pass, and numeric decay is lazy anyway.

**The user never sees it** (repo principle #4): no CLI command, no user action; the only visible surface is `last_maintenance_run` in `ec_get_summary` (plus `maintenance_log` rows, which also record review-gate activity).

### 8.2 The run and its bookkeeping

`run_maintenance` (`ec/maintainer.py:416-450`) executes four tasks in fixed `TASK_ORDER` (`ec/maintainer.py:58`):

```
("forgetting", "grounding", "edge_pruning", "clustering")
```

Order matters: grounding may deprecate ECUs → edge pruning then removes edges to dead ECUs → clustering should see the post-pruning brain. Each task logs a `maintenance_log` row (`action`, JSON `details`, `ecus_affected`); a failing task is logged with its error and does **not** abort the rest ("partial hygiene beats none"). A `full_run` summary row is appended, then `maintenance_state` baselines are stamped (`last_run_at`, `last_run_ecu_count`) so future trigger checks measure only new work.

### 8.3 The four tasks

**1. `task_forgetting` (Phase 7)** — eager status transitions only (decay is lazy; Section 9):
- *Persistence-limit pass* (`_persistence_limit_pass`): every `challenged` ECU with `metadata.competing_since` whose elapsed days ≥ its scope's persistence limit (Section 5.6; `null` = never) transitions — together with its canonical `contradicts`-pair members still in `active|challenged` — to `open_question`. Confidence freezing is a property of the status (`FROZEN_STATUSES`), not a write.
- *Supersession pass* (`_supersession_pass`): every `active|challenged` ECU whose **effective** (decayed) confidence < `theta_supersede (0.3)` is superseded **if** `_find_replacement` finds one — a different, `active`, **newer**, strictly-more-confident canonical ECU with cosine ≥ `diffuser.relevance_threshold (0.6)` (highest similarity wins; no embedding → conservative no-op). Applies: `supersedes` edge (weight = similarity, type classified via `supersession_type`), old status → `superseded`, §15.7 propagation to dependents. "Rare by design: decay alone is never enough, you need a replacement."

**2. `task_grounding` (Phase 8)** — delegates to `GroundingVerifier` (`ec/grounding.py:267-375`). Verifies canonical ECUs whose `grounding.repo_path` matches the given repo and whose status is `active|challenged`:
- *Checks:* (1) every `grounding.files` path must exist on disk (`file_exists`); (2) every `grounding.symbols` name must appear via substring search in surviving files (`symbol_present` — best-effort; structured names like `TokenManager::refresh` are also tried as separator-separated fragments ≥4 chars; unreadable files count as present — errs toward keeping the ECU); (3) informational commit staleness — `git rev-list --count <hash>..HEAD` > `COMMIT_STALENESS_COMMITS = 50` sets `metadata.stale_commit`, **never deprecates**.
- *Deprecation rules:* per-scope (Section 5.5). On deprecation: status → `deprecated` (confidence auto-freezes), edges preserved, direct `depends_on` dependents challenged (`challenge_dependents`; `open_question` stays parked).
- *Throttle (D35):* at most once per `grounding_check_interval_hours = 72` **per repo** (`maintenance_state['grounding_last_check:<repo>']`); skipped (clock unstamped) when no repo, repo not on disk, or throttled. Also run on session start for the session's repo (`SessionManager._run_startup_grounding`, best-effort) and at MCP startup with the resolved repo path (`EC_REPO_PATH` env → git toplevel → most recent active session's repo, `resolve_maintainer_repo_path`).

**3. `task_edge_pruning` (Phase 13, D52)** — scans `list_all_edges()`: skips `supersedes` (audit trail); removes edges whose target ECU is `deprecated|superseded`, or whose target row is gone (orphan cleanup). Reversal: for `supports|contradicts` edges with a non-zero stored delta, `reverse_update` subtracts/adds back the delta (`L_B' = L_B − delta`) before deletion; `depends_on` just removes. Weight-decay/stale-edge triggers are explicitly optional for v2. Sits after grounding so edges to freshly deprecated ECUs are cleaned in the same pass.

**4. `task_clustering` (Phase 9)** — Section 6.5 (HDBSCAN, gated by 100 new ECUs).

### 8.4 Conflicts and contradictions — who handles what

The Maintainer does **not** resolve contradictions. Division of labor:

- **Detection/classification:** the Diffuser's two-stage contradiction handling at diffusion time (Section 6.4); `challenged` status + `last_challenged`; high-stakes (both > 0.7) and scope flags ride `DiffusionResult.flags` to the review gate.
- **Accumulation:** contradicts edges and repeated Bayesian negative deltas compound in log-odds space; Case-3 pairs get `competing_since`.
- **Parking:** the persistence-limit sweep (Maintainer *and* review-gate clock) parks expired pairs as `open_question` (frozen, retrievable ×0.5 with ⚠️ flag, never blocking — §17.5 item 4).
- **Human resolution:** the review gate's open-question UI (options a–d, Section 10.4).
- **Dead-belief hygiene:** grounding deprecation + edge pruning + decay-driven supersession.

---

## 9. Forgetting Mechanism

### 9.1 The two-layer design

**Lazy decay (no writes)** — `effective_confidence` (Section 2.4): every ranking read of a canonical ECU applies `L_eff = logit(c_stored) − λ_scope × days_since_last_reinforced`. Frozen statuses (`open_question`, `superseded`, `deprecated`, `archived`) are exempt; session ECUs never decay. The stored value is never reduced by time itself.

**Eager transitions (writes)** — only the Maintainer's forgetting task (Section 8.3) and grounding's deprecation change *statuses*, which is how forgetting actually "happens":

| Transition | Trigger | Effect |
|---|---|---|
| `active/challenged` → `superseded` | Effective confidence < 0.3 **and** a valid replacement exists (Maintainer), or Diffuser supersession, or open-question option (b) | Confidence frozen; excluded from retrieval (`RETRIEVABLE_STATUSES`); `supersedes` edge preserved forever |
| `active/challenged` → `deprecated` | Grounding verification: grounding gone per scope rules | Confidence frozen; excluded from retrieval; edges preserved; direct dependents → `challenged` |
| `challenged` → `open_question` | Competing hypotheses past scope persistence limit (20–90 days; engineering/domain indefinite) | Confidence frozen; still retrievable at ×0.5 with ⚠️ "unresolved since" flag |
| `active/challenged/open_question` → `archived` | User decision (open-question option d) | Frozen; not returned by retrieval |

### 9.2 When confidence is reduced (events)

- `contradicts` edges (diffuser, pending-update application, reconsolidation).
- Edge-pruning reversal of a previously-applied `contradicts` delta *raises* confidence (the stored delta was negative) — and reversal of a `supports` delta *lowers* it back. Both are `L_B' = L_B − delta`.
- Scope-dependent lazy decay reduces only the **effective** value used for ranking (and for the Maintainer's supersession-eligibility check) — never the stored audit value.

### 9.3 Is an ECU ever fully removed?

**No.** There is no code path that deletes a canonical ECU. Terminal statuses ("frozen for audit") plus preserved edges are the design: *"Nothing is ever deleted"* (`ec/grounding.py` docstring, §3.5/§21.3). Deletions that do exist, and their scope:

- **Session ECUs:** deleted at review (accepted → promoted then removed; rejected → removed) — `brain.delete_session_ecu` (`ec/review_gate.py:942-944`).
- **Edges:** pruned by `task_edge_pruning` (targets dead/orphaned) and by open-question option (c) (contradicts edges removed on orthogonal reclassification). `supersedes` edges are never pruned.
- **Orphan edges:** `ec-repair` (`ec/repair.py`) removes edges whose endpoint ECU row is gone — the only place edges to "missing" ECUs disappear.
- **Activation rows:** deleted at session close (ephemeral by design).

### 9.4 What happens to associations when an ECU is forgotten

- `superseded`: stays connected via its `supersedes` edge (retrieval's `superseded_by` slot derives context from it when the *successor* is retrieved); other edges are pruned in the next maintenance pass (target status trigger), with their confidence deltas reversed first.
- `deprecated`: same pruning behavior; its `depends_on` dependents were already challenged at deprecation time (§17.4 flag #3 / §21.3).
- `open_question`/`archived`: edges untouched (statuses are parked, not dead); retrieval skips archived but surfaces open_question with weight 0.5.
- Retrieval-side effects are automatic because both `find_similar` (canonical status filter) and `_fetch_ecu`-based grouping exclude non-retrievable statuses from *core* selection, while supersedes-linked context remains reachable through depth-1 traversal.

---

## 10. Human Review Gate

### 10.1 Purpose and trigger

`ec/review_gate.py` — *"the only path into the Canonical Brain (§6.7: no bypass, no exceptions)."* Triggers: session close via `/ec-stop` (the slash-command template runs `python -m ec.session stop`), or on-demand via `apply_review_decisions` (used by tests and the benchmark's `--all-accept` flow). Only `active` sessions can go through the gate.

### 10.2 What the user is asked to do

**Grouping:** candidates (review_status `pending|skipped`) are grouped by `scope_path` topic (D18) or HDBSCAN cluster with scope fallback (D36, `review_gate.grouping_strategy`), sorted by label; ECUs within a group sort by confidence descending.

**Interactive driver** (`interactive_review`, `ec/review_gate.py:1033-1117`), per the §28.7 transcript in the spec:

```
EC Review Gate — 12 candidate ECUs in 4 groups

Group 1: repo:myapp > module:auth (3 ECUs)
  [1] "Token refresh must be optimistic to handle race conditions..."
      confidence: 0.72 | scope: repo:myapp > module:auth | source: debugging
  ...

  accept all | reject all | review individually (a/r/i), or skip/done:
```

| Command | Action |
|---|---|
| `a` | Accept all in the group |
| `r` | Reject all in the group |
| `i` | Per-ECU loop: `y` accept / `n` reject / `s` skip / `d` full detail (cognition, type, scope, confidence, provenance, grounding, evidence, edges) |
| `skip` / `done` | Stop reviewing; everything unreviewed becomes `skipped` |

An EOF/non-answer leaves things parked; open questions never block anything (§17.5 item 4).

**Before the normal review**, open questions resolve first (§17.5 a–d, D41) — `resolve_open_questions` presents each competing pair (two `open_question` ECUs joined by a contradicts edge; unpaired singles get only investigate/archive):

- **(a) Investigate** — both back to `challenged`, `competing_since` reset (persistence clock restarts).
- **(b) Mark one preferred** — winner gets the `alpha_retrieval` bump and returns to `active`; loser `superseded` by it with a real semantic `supersedes` edge.
- **(c) Reclassify as orthogonal** — both `active`; all contradicts edges between them deleted; each metadata gains an `orthogonal_note` ("User reclassified as orthogonal on <date>").
- **(d) Archive** — both archived (confidence frozen at the parked value).

### 10.3 What happens on accept / reject / skip

Exactly Section 4.2's step list: accept → promote (same id, optional edit preserved in `metadata.review_edit`) → full Diffuser (edges, log-odds updates, contradiction handling, supersession, propagation) + contradiction surfacing + propagation notifications; reject → row deleted; skip → retained as `skipped` (returns at next review, carries to next session). Invalid decision strings are recorded in `result.errors` and treated as skip ("one bad decision must not lose the batch"). `ReviewResult` (`ec/review_gate.py:72-87`) carries `accepted/rejected/skipped/diffusions/flags/notifications/pending_applied/pending_discarded/errors/undiffused/canonical_total/message`. Closing summary: *"Review complete. N accepted, N rejected, N skipped (pending). Diffusing to Canonical Brain... done. Brain now has N ECUs."*

### 10.4 Flags and notifications surfaced at the gate

- **`high_stakes_contradiction`** (§17.4 #1) — both sides > `theta_contradiction_flag` (0.7): "Two well-supported engineering beliefs contradict each other…"
- **`scope_contradiction`** (§17.4 #2) — contradiction at `engineering`/`domain` scope (`contradiction.always_flag_scopes`): "a fundamental conflict in engineering beliefs worth investigating."
- **`depends_on_at_risk`** (§17.4 #3) — for each ECU challenged by §15.7 propagation: "ECU X depends on a cognition that was challenged in this review and may need re-evaluation" — gated by `contradiction.notify_on_dependent` (notification only; propagation itself never disabled).
- **`opposing_edges`** (§14.3, D53) — a pair holding both supports and contradicts: "This relationship may need decomposition into more atomic ECUs." The gate re-scans *all* canonical edges so cross-session accumulation is caught (deduped against flags the diffuser already raised).
- **`open_question`** — persistence sweep notification: "…have been competing for N days without resolution (scope limit: L days). Both are now parked as open_question — confidence frozen… Options: investigate further, mark one preferred, reclassify as orthogonal, or archive."
- **`grounding_deprecations`** (§3.6) — aggregated from `maintenance_log` since the last `review_gate` row: lists each deprecated ECU, missing files/symbols, and the count of challenged dependents.

### 10.5 The `--accept-all` flag (and siblings)

`python -m ec.session stop` accepts (`ec/session.py:388-402`):

- `--all-accept` — non-interactive: builds `{ecu_id: "accept"}` for every `pending|skipped` session ECU and calls `apply_review_decisions`; prints the result message, the diffusion-failure warning, and flags.
- `--all-skip` — same but every decision is `skip` (close without reviewing; ECUs carry to the next session).
- `--resolve-open-questions {auto,skip,archive}` — `auto` (default) prompts interactively on an interactive stop and skips otherwise; `archive` archives all open questions without prompting (`resolve_open_questions_archive_all`); `skip` never touches them.

With either bulk flag the flow is identical to an interactive accept (promotion + full diffusion + cleanup), minus the prompts. This is what the EC-Bench runner uses between sessions (`ec/run_ecbench.py:392-395`, `--all-{accept|skip}`), and the spec's §28.10 `ec-stop.md` template documents both flags. The interactive path needs a TTY (documented limitation, `docs/ASSESSMENT.md` triage table).

---

## 11. EC-Bench

### 11.1 Structure

**EC-Bench v2** (`bench/ecbench_v2.json`): **30 prompts across 3 sequential sessions** on a target repository (FastAPI), comparing an agent **WITH EC** against a **stateless baseline**. Session composition:

- **session-1 — 14 prompts:** Investigation (repo orientation, GET-request call-path trace, `get_request_handler` factory analysis, inner-helper inventory, closure capture analysis, streaming-test baseline, benchmark-coverage review, empirical instrumentation of per-request closure creation) then Architectural Reasoning (design rationale inference, approach evaluation/selection, integration-point mapping, state-flow preservation, PR design section, test strategy).
- **session-2 — 13 prompts:** Implementation (foundational abstraction, incremental integration, complex streaming path, completion/reuse, dead-code cleanup + docstrings, benchmark coverage, streaming test suite, broader suite) then Debugging (regression investigation ×2, type-check fixes, lint fixes, final integration run).
- **session-3 — 3 prompts:** Planning (WebSocket generalization evaluation, next-bottleneck proposal, broader architectural roadmap).

Each prompt declares an objective, the prompt text, the "expected engineering knowledge accumulated" (the memory payload the session should leave behind), and "why this prompt naturally follows from previous work" — i.e., later prompts are answerable faster/ better *if* earlier cognition was retained. The design embeds EC-Bench Findings 1–3 (§11.3/§11.9): grounding before retrieval, mode-specific value, and the anchoring hazard.

### 11.2 The runner

`ec/run_ecbench.py` (`python -m ec.run_ecbench`) and `bench/run_and_judge.py` (unified runner + inline judge). Per condition (`ec` | `baseline`), condition isolation (D31/D32):

1. Fresh **copy** of the target repo (`workdir/`) — identical starting state, original untouched.
2. Per-condition `EC_HOME` (the baseline never gets a brain) and isolated `XDG_DATA_HOME`/`XDG_STATE_HOME` (fresh agent context per prompt by default; `--continue-within-session` opts into `--continue`); provider `auth.json` copied into the isolated XDG dir.
3. Agent config injected via `OPENCODE_CONFIG` overlay (`build_agent_config`, `ec/run_ecbench.py:127-165`): the `ec` condition gets the EC MCP server (absolute venv python + PYTHONPATH + `EC_HOME` + `EC_REPO_PATH`/`EC_BRANCH`) and the §28.9 AGENTS.md; the baseline gets **no** EC instructions and the `ec` server explicitly `enabled: false` (neutralizes a global install).
4. Per session: `python -m ec.session start` → run each prompt headlessly (`opencode run --dir {repo} --format json --auto {prompt}` by default; JSONL transcripts + `.meta.json` per prompt) → `python -m ec.session stop --all-accept` (or `--all-skip`) as the between-session review gate.
5. Outputs: `manifest.json` per condition (per-prompt transcripts, exit codes, durations, session CLI logs, diffusion warnings, `ec_stats` — canonical ECU count, edge counts by type, retrieval events, sessions) and a top-level `run.json`. Crash-safe resume via `--run-id` (completed prompts are skipped). `--dry-run` prints the plan.

### 11.3 The judge

`bench/judge_ecbench.py` (final-answer judge) and `bench/judge_ecbench_v3_fulltranscript.py` (full-transcript judge, also embedded in `run_and_judge.py`):

- **Model:** GLM-5.2 via OpenCode Zen (`https://opencode.ai/zen/v1/chat/completions`), `temperature = 0.0`, `response_format: {"type": "json_object"}`, `max_tokens 4096`, 3 retries with backoff on 429/errors. Resume support (`scores.json`, atomic writes).
- **Inputs:** the engineering prompt + either the extracted **final answer** (last non-step-marker text block of the JSONL transcript; truncated at 50,000 chars) — judge v2 — or the **full transcript** (all text blocks + tool calls; non-EC tool outputs truncated to 500 chars, EC tool outputs to 1000; total 80,000 chars) — judge v3. v3 exists because "both the process AND the final output" carry signal.
- **Blindness:** the judge system prompt mandates it remain blind to condition, prior scores, and expected outcome; every answer evaluated independently; metrics scored independently; conservative scoring when evidence is ambiguous.
- **Metrics and weights** (`METRIC_DEFINITIONS`, embedded; also in the system prompt):

| Metric | Weight | What it measures |
|---|---|---|
| Architectural Continuity | 0.30 | Consistency with the repo's existing architecture/module boundaries; no unnecessary disruption |
| Engineering Cognition Reuse | 0.30 | "Would this answer likely have been different if accumulated engineering cognition did not exist?" — prior conclusions influencing decisions; explicitly *not* mere mentioning/quoting |
| Repository Groundedness | 0.15 | References to actual modules/functions/patterns; not generic |
| Engineering Quality | 0.15 | Correct judgment, minimal changes, discipline |
| Debugging & Investigation Efficiency | 0.10 | Systematic, non-redundant investigation |

```
overall_score = (architectural_continuity × 0.30)
              + (engineering_cognition_reuse × 0.30)
              + (repository_groundedness × 0.15)
              + (engineering_quality × 0.15)
              + (debugging_investigation_efficiency × 0.10)
```

Each metric 0.0–10.0 continuous with a mandatory evidence-based reason; output is strict JSON (`{metric: {score, reason}}`, `overall_score`, `strengths[]`, `weaknesses[]`). The report generator (`generate_report`) emits per-session/per-condition averages, a metric-by-metric comparison table, and a **fairness note**: the reuse metric (30% weight) structurally favors the EC condition since the baseline has no cognition to reuse — "interpret comparison results with this context."

### 11.4 Baselines and the exact v1/v2 numbers

**Run 1 (v1 — "EMS", proactive injection; pre-v2):** on Mudit's summary, EMS won **22 of 30 prompts on raw score**, but metric decomposition exposed anchoring: Engineering Cognition Reuse **+2.214**, Engineering Quality **−0.589**, Repository Groundedness **−0.480**; worst in debugging — Prompt 23: **−4.17**, Prompt 24: **−2.08** (cited in `docs/SPEC.md` §11.3 with `[cite:f6100d2cb]`, in `docs/ASSESSMENT.md`, and in `~/.ec/AGENTS.md` itself). Conclusion: "the entire net positive came from reuse volume, not work quality… Remembering more is not the same as understanding better." This run designed v2's demand-driven architecture.

**Run 2 (v2, demand-driven, headless)** — `runs/20260813-141550/`, glm-5.2 judge, 30 prompts, both conditions judged. Exact values computed from `runs/20260813-141550/scores.json`:

| Metric (weight) | EC | baseline |
|---|---|---|
| Architectural Continuity (0.30) | 8.423 | 8.743 |
| Engineering Cognition Reuse (0.30) | 7.737 | 8.277 |
| Repository Groundedness (0.15) | 8.927 | 9.030 |
| Engineering Quality (0.15) | 7.933 | 8.430 |
| Debugging & Investigation Efficiency (0.10) | 7.583 | 7.953 |
| **Overall (n=30 each)** | **8.112** | **8.527** |

Reading (per `docs/handoffs/HANDOFF_PHASE_12.md` §3 and the prior reference): with retrieval fully agent-controlled, memory neither anchored (no negative quality/groundedness deltas like v1) nor yet paid for itself in aggregate — the EC condition scored slightly below baseline while producing a working accumulated brain (stats in the manifest). Note the reuse metric's structural favor toward EC (fairness note above) makes the EC-below-baseline result notable; the intended comparisons are per-metric.

**Run 3 (post-v2-complete, with forgetting/grounding/clustering/reconsolidation/pruning live):** intentionally deferred as a manual run for Mudit (Phase 12 handoff §6): export `OPENCODE_ZEN_API_KEY`, then `bench/run_and_judge.py --spec bench/ecbench_v2.json --repo ~/Desktop/practise/fastapi --runs-dir runs`. Prior full two-condition run took ~32h wall time. Expected deltas: `engineering_cognition_reuse` and debugging efficiency.

---

## 12. System Boundaries

### 12.1 Agent-agnostic

- **Protocol:** the MCP server speaks standard stdio MCP — NDJSON JSON-RPC 2.0 (`initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`; protocol version `2024-11-05`), hand-rolled because the `mcp` SDK is not in the pinned venv (D14). Any MCP-capable client can attach; the tool surface (4 tools with JSON Schemas, `ec/mcp_server.py:207-324`) is agent-neutral, and tool descriptions are written for the agent, not a specific product (§28.12).
- **Installer abstraction:** `ec/install.py` has an `AgentSpec` registry (`AGENTS` list) with one subclass per agent — Claude Code (`~/.claude.json` mcpServers + `CLAUDE.md` import), Cursor (`~/.cursor/mcp.json` + `~/.cursor/rules/ec.md`), OpenCode (`~/.config/opencode/opencode.json` `mcp` key + `~/.config/opencode/AGENTS.md`), Codex (`~/.codex/config.toml` `[mcp_servers.ec]` + `~/.codex/AGENTS.md`). Detection = binary on PATH **or** marker file. Supporting a new agent = one new `AgentSpec` subclass with its two config writes; everything else is shared.
- **Launch form:** `server_command()` returns the pip-installed `ec-mcp` console script when available (D45) or an absolute venv python + `PYTHONPATH` fallback (D28) — independent of any agent.
- **EC's internal LLM is separate from the agent's LLM:** EC calls its own configured model for extraction/classification/mode detection; the agent's identity only lands in `provenance.origin_agent` metadata.

### 12.2 Model-agnostic

- **EC's utility LLM:** `call_llm` branches on wire format derived from the model *name* (`claude*` → Anthropic Messages; anything else → OpenAI Chat), not the provider key (D48). Adding a provider that speaks OpenAI Chat (vLLM, LM Studio, Together, Groq, Ollama) is a pure config change; a new wire format needs one `_call_*` function. Ollama (`qwen2.5-coder:14b` at `http://localhost:11434/v1`) is a first-class offline fallback (D42), auto-offered by the installer and usable keyless.
- **Extraction prompt:** explicitly model-agnostic by design ("works with any coding LLM (Claude, GPT, Gemini, Kimi, GLM, etc.)", prompt header); the proprietary tier (fine-tuned 3–7B extractor) is deferred to v3 (spec §5.12/§23).
- **Embeddings:** `all-MiniLM-L6-v2` is config-driven (`embedding.model`, `dimensions` 384) — "set at init, never changed" within a brain, but the model itself is a config value.

### 12.3 Hardcoded to coding agents / repositories

- **Sessions are git-and-filesystem bound:** `(repo_path, branch)` detection shells out to `git rev-parse --show-toplevel` / `git branch --show-current` (`ec/session.py:69-107`); commit-hash stamping runs `git rev-parse HEAD` (D49); grounding verification reads the working tree and counts commits (`ec/grounding.py`). `EC_REPO_PATH`/`EC_BRANCH` env vars are the only bypass (used by tests/benchmark).
- **The agent contract is a coding-agent conversation:** `ec_observe` expects `user_prompt` + `reasoning_trace` (+ optional `final_output`); the extractor prompt's signal catalog, Lifting Test, and few-shot examples are all engineering/coding-specific; scope levels and grounding (`files`/`symbols`) presume a codebase.
- **Session lifecycle is user-controlled via CLI slash-command templates** (`/ec-start`/`/ec-stop`/`/ec-status`), which the installer writes into `~/.ec/commands/` — an agent-hosting convention, wired per agent.
- **The 4 agents' config formats** (JSON/TOML shapes, import syntaxes) are hardcoded in the `AgentSpec` subclasses.

### 12.4 What would need to change for a different agent type

1. **A new `AgentSpec`** for config wiring (or manual MCP config) — the protocol itself needs nothing.
2. **Session anchoring** for non-git environments: replace/augment `detect_repo_branch` and the commit-hash storer with the new environment's identity/unit-of-work notion (the env-var override already provides an escape hatch).
3. **`ec_observe` input mapping:** whatever the new agent calls "reasoning" must be passed as `reasoning_trace`; the tool schema is already loose strings.
4. **The extraction prompt** if the domain is not software engineering (the ECU definition, conclusion types, and grounding fields are engineering-specific; the confidence/retrieval machinery is domain-neutral).
5. **AGENTS.md heuristics** (`~/.ec/AGENTS.md`) reference coding-specific behaviors (reading code first, /ec-start usage) and would need domain-appropriate rewording.
6. **Grounding verification** assumes files/symbols on disk; a non-filesystem "repo" would need a different verifier behind `task_grounding`.

What would *not* change: confidence math, retrieval ranking, diffusers, review gate, maintainer tasks (except grounding), clustering, schema, and the MCP tool surface — all are domain- and agent-neutral by construction.

---

## 13. Test Coverage

### 13.1 Counts (verified by execution at time of writing)

`.venv/bin/python -m pytest tests/` → **512 collected, 506 passed, 6 skipped, 0 failed** (~3 min wall). The 6 skips are all `OPENCODE_ZEN_API_KEY`-gated live tests (existing repo pattern: everything offline except 5 live + 1 E2E):

| Skipped test | File |
|---|---|
| live mode detection | `tests/test_phase1_foundation.py:357` |
| live extractor | `tests/test_phase2_extractor_diffuser.py:672` |
| live lightweight classification | `tests/test_phase2_extractor_diffuser.py:690` |
| live retrieval | `tests/test_phase3_retrieval.py:665` |
| live MCP observe+query | `tests/test_phase4_mcp_server.py:392` |
| live E2E full pipeline | `tests/test_e2e_integration.py:195` |

Per-file distribution (512 total):

| Test file | Tests | Covers |
|---|---|---|
| `test_phase7_forgetting.py` | 44 | lazy decay math, effective confidence in ranking, reinforcement, persistence limits, supersession-from-decay, frozen statuses |
| `test_phase8_grounding.py` | 40 | file/symbol/commit checks, scope deprecation rules, dependents challenge, 72h throttle, wiring |
| `test_phase9_clustering.py` | 33 | HDBSCAN + agglomerative, noise/stability gating, wholesale recompute, review-gate cluster grouping + fallback |
| `test_phase3_retrieval.py` | 31 | 14-step pipeline, relevance gate + fallback, four-factor ranking, grouping/dedup, activation interaction, formatted output |
| `test_phase2_extractor_diffuser.py` | 31 | prompt loading, validation, priors, full + lightweight diffusers, supersession, contradictions |
| `test_phase11_production_gaps.py` | 31 | D40 prefilter fix, open-question UI (a–d), Ollama auto-setup, packaging/installer integration |
| `test_phase10_reconsolidation.py` | 30 | supports/contradicts/supersedes paths, labile gate, pseudo-ECU, materialization, audit notes, MCP errors |
| `test_phase5_install.py` | 26 | agent detection, per-agent config writes, ~/.ec bootstrap, dry-run, Ollama flow |
| `test_phase6_maintainer.py` | 24 | triggers, task order, logging, state baselines, startup check, thread lifecycle |
| `test_llm_providers.py` | 24 | D48 dual wire format, keyless Ollama, fence stripping |
| `test_phase4_review_gate.py` | 22 | grouping, promotion/edit, pending updates (D16), flags, cleanup |
| `test_phase1_foundation.py` | 19 | config defaults + YAML merge, brain CRUD, validation, edges, embeddings, `find_similar`, keyword mode detection |
| `test_phase4_mcp_server.py` | 17 | JSON-RPC methods, tool schemas, handlers, metadata/reinforcement writes, labile marking |
| `test_phase13_opposing_edges.py` | 17 | D53 diffuser flags + review-gate cross-session scan |
| `test_phase4_session.py` | 16 | start/resume/carry-over/stop, labile tracking, summary payload |
| `test_phase13_mutators.py` | 15 | D55 grounding/evidence mutators |
| `test_phase13_repair.py` | 13 | six `ec-repair` checks, dry-run, non-destructiveness |
| `test_phase13_edge_pruning.py` | 13 | D52 triggers, delta reversal, supersedes never pruned |
| `test_phase13_commit_hash.py` | 13 | D49 stamping end-to-end + staleness wakeup |
| `test_ecbench_runner.py` | 12 | spec loading, condition configs, resume, dry-run |
| `test_phase13_scope_hierarchy.py` | 11 | D50 UP-only filter, anchor, bridge, fallback |
| `test_phase13_structural_proximity.py` | 9 | D54 candidate expansion bounds |
| `test_phase13_polish.py` | 9 | D56 rejection_summary surfacing, D57 pending_review_count |
| `test_phase13_config_cleanup.py` | 6 | D-config: decorative keys documented, `notify_on_dependent` wired |
| `test_e2e_integration.py` | 6 | 1 live full-pipeline + 5 offline extended E2E (maintenance mid-session, grounding chain, reconsolidation via MCP, clustering population, controlled-forgetting retrieval) |

Testing conventions (root `AGENTS.md`, handoffs): `EC_HOME` env redirect for isolation; `call_llm` mocked via `unittest.mock.patch` offline; `now` parameter injectable for all time-dependent logic; filesystem work in `tmp_path`; hermetic config (explicit `DEFAULT_CONFIG` copies rather than `get_config()`, which could read the user's `~/.ec/config.yaml`).

### 13.2 Spec requirements coverage

Two read-only compliance audits trace every actionable requirement to code + tests:

- **`docs/SPEC_AUDIT.md` (first audit):** **268 requirements** extracted section-by-section from `docs/SPEC.md` ("MUST"/"MUST NOT"/explicit design rules) and traced to file/function. Found 10 ❌ missing / 14 ⚠️ partial items — the 13 gaps that became Phase 13.
- **`docs/SPEC_AUDIT_FINAL.md` (final audit, HEAD `3dd5320`):** **303 requirements checked** (the original set re-verified + 10 new rows for D48–D57): **279 implemented, 3 partial, 0 missing, 2 deferred, 19 documented deviations.** All 13 earlier gaps verified closed with commit hashes; zero regressions. The three partials are two cosmetic §28.4 error-message wordings and one accepted diffuser scope-restriction nuance — none functional. The 19 deviations are documented deliberate choices (e.g., embedding BLOB columns instead of an `embeddings` table, D3; `session` source_type prior interpolated to 0.45; edge-weight accumulation `min(1.0, w1+w2)` unspecified by spec; token estimate as a char heuristic).

### 13.3 What the tests collectively prove (system-level invariants)

- **No-bypass boundary:** cross-brain evidence only via `pending_updates`; D16 never-double-applies; rejected sources discard their pendings.
- **Confidence correctness:** exact log-odds update values asserted (e.g., audit-verified 0.700→0.798, 0.750→0.830); decay half-lives per scope; reinforcement resets the clock (the offline E2E ranks a decayed old ECU *below* a fresher weaker one, then proves the reset via a 35-day counterfactual); frozen statuses never decay and never un-freeze via reinforcement.
- **Two-condition §15.5 supersession:** never without low confidence *and* a replacement — including the reconsolidation fallback-to-challenged path.
- **Ranking gates:** relevance gate 0.3 (verified with a sim-0.021 off-topic ECU in the live E2E), fallback top-K, divide-by-max activation bounded to [0, 0.10], trust weights, scope proximity, open_question ×0.5, mode bonuses.
- **Forgotten ≠ deleted:** deprecated/superseded ECUs remain rows with frozen confidence and (for supersedes) permanent edges; retrieval exclusion is status-driven.
- **Session semantics:** resume/carry-over of pending+skipped ECUs with edges and pendings; activation decay across breaks; fresh slate after close; multiple concurrent (repo, branch) sessions.
- **Offline robustness:** every LLM-dependent path has a graceful offline behavior (keyword mode detection; skip-and-log extraction failures; `unrelated` default classifications; `genuine_contradiction` fail-safe; undiffused-promotion warning), and the 5 offline E2E tests run ≥3 real components per test through public interfaces with zero LLM calls.

---

## Appendix A — Design decision index (D1–D57)

| D | Decision | Where |
|---|---|---|
| D1 | Session ECUs in a separate `session_ecus` table; uniform brain-tagged `find_similar` | HANDOFF_PHASE_1 |
| D2 | Confidence mutable in place; `supersedes` only on replacement/rewording | HANDOFF_PHASE_1 |
| D3 | Embeddings as BLOB columns, not a table | HANDOFF_PHASE_1 |
| D4 | Mode params = spec critical detail #7 verbatim | HANDOFF_PHASE_1 |
| D5 | Cross-brain session evidence → `pending_updates` only | HANDOFF_PHASE_2 |
| D6 | Cosmetic vs semantic supersession = cosine ≥ 0.92 | HANDOFF_PHASE_2 |
| D7 | Lightweight bump clamped to [0.05, 0.95] | HANDOFF_PHASE_2 |
| D8 | LLM-proposed supersedes requires the §15.5 trigger; else downgraded to supports | HANDOFF_PHASE_2 |
| D9 | Mode-aware spread = capped ×1.5 multiplier on preferred edge type | HANDOFF_PHASE_3 |
| D10 | Challenged = no ranking penalty + ⚠️ flag; budget hard | HANDOFF_PHASE_3 |
| D11 | Mode bonuses +0.05/+0.03; debugging slot = challenged-or-contradicts-edge | HANDOFF_PHASE_3 |
| D12 | Spread implements the §12.4 formula (max-merged BFS) | HANDOFF_PHASE_3 |
| D13 | Activation persistence in additive `session_activation` | HANDOFF_PHASE_3 |
| D14 | Hand-rolled NDJSON JSON-RPC MCP subset; `isError` mirrors payload status | HANDOFF_PHASE_4 |
| D15 | Session state in DB; lifecycle via CLI; MCP never creates sessions | HANDOFF_PHASE_4 |
| D16 | Pending updates: exact recomputed deltas, never double-applied | HANDOFF_PHASE_4 |
| D17 | Retrieval metadata on the query path, canonical only | HANDOFF_PHASE_4 |
| D18 | Review grouping by scope_path topic | HANDOFF_PHASE_4 |
| D19 | Accept-with-edit preserves original in `metadata.review_edit`, re-embeds | HANDOFF_PHASE_4 |
| D20 | Case-3 sets `competing_since`; gate sweep parks open_question | HANDOFF_PHASE_4 |
| D21 | Promotion keeps the session ECU's id | HANDOFF_PHASE_4 |
| D22 | Activation saved after every ec_query; rows deleted at close | HANDOFF_PHASE_4 |
| D23–D27 | Installer: direct merge-not-overwrite configs; verbatim spec-owned files; `--only`; `commands/` dir | HANDOFF_PHASE_5 |
| D28 | Dev-mode MCP launch = absolute venv python + PYTHONPATH | HANDOFF_FIXES |
| D29 | All classification calls at temperature 0.0 | HANDOFF_FIXES |
| D30 | Promoted-but-undiffused ECUs tracked + prominent warning | HANDOFF_FIXES |
| D31/D32 | Per-condition repo copies + `OPENCODE_CONFIG` injection for the bench | HANDOFF_FIXES |
| D33 | Maintainer inside the MCP server (startup check + daemon thread) | V2_COMPLETION_DESIGN |
| D34 | Lazy decay, eager transitions | V2_COMPLETION_DESIGN |
| D35 | Grounding on session start, per-repo 72h throttle | V2_COMPLETION_DESIGN |
| D36 | Review grouping strategy scope/cluster with fallback | V2_COMPLETION_DESIGN |
| D37 | `ec_reconsolidate` tool | V2_COMPLETION_DESIGN |
| D38 | Unified `_diffuse_against_brain` entry point | V2_COMPLETION_DESIGN |
| D39 | Labile set in session memory gates reconsolidation | V2_COMPLETION_DESIGN |
| D40 | §17.2 prefilter unblocks different-scope pairs (grounding overlap / sim > 0.8) | V2_COMPLETION_DESIGN |
| D41 | Open-question resolution UI (a–d) at the gate | V2_COMPLETION_DESIGN |
| D42 | Ollama auto-setup when no Zen key | V2_COMPLETION_DESIGN |
| D43/D44 | Repo reorg; root AGENTS.md | V2_COMPLETION_DESIGN |
| D45 | pip-installable package; `ec-mcp` console script launch | V2_COMPLETION_DESIGN |
| D46/D47 | Rapid commits; session-chained execution | V2_COMPLETION_DESIGN |
| D48 | LLM wire format from model name (Anthropic vs OpenAI Chat) | PHASE_13_AUDIT_FIXES_DESIGN |
| D49 | commit_hash stamped server-side at observe time | PHASE_13_AUDIT_FIXES_DESIGN |
| D50 | §11.11 search-UP scope-hierarchy filter with anchor + fallback | PHASE_13_AUDIT_FIXES_DESIGN |
| D51 | `ec repair` safe non-destructive recovery | PHASE_13_AUDIT_FIXES_DESIGN |
| D52 | Edge pruning as a Maintainer task; supersedes never pruned | PHASE_13_AUDIT_FIXES_DESIGN |
| D53 | Opposing-edges decomposition flag | PHASE_13_AUDIT_FIXES_DESIGN |
| D54 | Structural proximity: ≤5 one-hop extras after find_similar | PHASE_13_AUDIT_FIXES_DESIGN |
| D55 | Grounding/evidence mutators (merge semantics, append-only) | PHASE_13_AUDIT_FIXES_DESIGN |
| D56 | `rejection_summary` surfaced in ec_observe payload | PHASE_13_AUDIT_FIXES_DESIGN |
| D57 | `pending_review_count` in the summary payload | PHASE_13_AUDIT_FIXES_DESIGN |

## Appendix B — Console scripts and entry points

From `pyproject.toml:25-31`:

| Script | Target | Purpose |
|---|---|---|
| `ec` | `ec.install:main` | Installer (agent detection + `~/.ec` bootstrap) |
| `ec-mcp` | `ec.mcp_server:main` | MCP server launcher (used by agent configs when pip-installed, D45) |
| `ec-start` / `ec-stop` / `ec-status` | `ec.session:main_start` / `main_stop` / `main_status` | Session lifecycle |
| `ec-repair` | `ec.repair:main` | Brain integrity check (D51) |
| `python -m ec.session` | CLI invoked by slash-command templates | start/stop/status with `--all-accept`/`--all-skip`/`--resolve-open-questions` |
| `python -m ec.run_ecbench` | Benchmark runner | EC-Bench conditions |

Dependencies (`pyproject.toml:10-20`): `torch==2.2.2, numpy<2, scipy<1.13, scikit-learn<1.5, transformers<5, sentence-transformers==3.0.1, hdbscan>=0.8.33, requests, pyyaml` (Python ≥ 3.12). The pins exist for torch-2.2.2 ABI compatibility on the development machine and are asserted in the phase handoffs.
