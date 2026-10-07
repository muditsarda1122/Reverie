# Engineering Cognition (EC) — Implementation Specification

**Document Status:** Implementation spec for EC v2 system build
**Date:** 12 August 2026
**Based on:** EC Design Spec v2.17
**Scope:** All components needed for EC-Bench v2 + demo video. HDBSCAN clustering, Cognition Maintainer, Controlled Forgetting, Reconsolidation Loop, and Grounding Verification are explicitly deferred and NOT in this document.

## What This Spec Covers

This spec contains everything needed to implement the EC system end-to-end:
- **ECU data model** (Section 1)
- **System architecture** (Section 2)
- **Extractor** — prompt-based, Claude Haiku 4.5 via OpenCode Zen (Section 3)
- **Session Brain and Canonical Brain** — two-brain SQLite system (Section 4)
- **Review Gate** — CLI for promoting session ECUs to canonical (Section 5)
- **Full Diffuser** — 5-way relationship classification with disambiguation rules (Section 6)
- **Engineering Brain** — structure and partitions (Section 7)
- **Demand-Driven Retrieval** — relevance gate, cognitive grouping, dedup, mode-aware (Section 8)
- **Spreading Activation** — with normalization (Section 9)
- **Edges** — four edge types, structure, creation, pruning (Section 10)
- **Confidence Mathematics** — Bayesian updates (Section 11)
- **Contradiction Handling** — flags, competing hypotheses (Section 12)
- **ECU Immutability** (Section 13)
- **Deprecation and Scope Rules** (Section 14)
- **Configuration Parameters** — all constants and defaults (Section 15)
- **MCP Server + OpenCode Integration** — tools, AGENTS.md, config (Section 16)

## What Is Deferred (NOT in this spec)

- HDBSCAN clustering (brain too small at 50-100 ECUs to benefit)
- Cognition Maintainer (single benchmark run, no background maintenance needed)
- Controlled Forgetting / Reinforcement (tied to maintainer)
- Reconsolidation Loop (diffuser handles confidence updates at extraction time)
- Grounding Verification (repo doesn't change between prompts)
- Business model, product design, open questions (not implementation)

## Implementation Notes

- **LLM for all components:** Claude Haiku 4.5 via OpenCode Zen API. Endpoint: `https://opencode.ai/zen/v1/messages` (Anthropic Messages API format). API key env var: `OPENCODE_ZEN_API_KEY`. Temperature: 0.3 for extraction/diffusion, 0.0 for mode detection. JSON post-processing required (strip markdown code fences from Claude output).
- **Embedding model:** `all-MiniLM-L6-v2` (384-dim, sentence-transformers). Set at init, never changed.
- **Database:** SQLite at `~/.ec/ec.db`. Global brain — one database for all projects.
- **Python version:** 3.12. On Intel Mac: pin `numpy<2`, `scipy<1.13`, `scikit-learn<1.5`, `transformers<5` for torch 2.2.2 compatibility.
- **Reference implementation:** The file `test_retrieval_ranking_zen_v2.py` (from Experiment 2) contains working, tested code for the retrieval pipeline including relevance gate, normalized activation, within-group dedup, and cross-group dedup. Adapt this code rather than implementing from scratch.
- **Extractor prompt:** The file `extractor_prompt.md` (alongside this spec) contains the tested, MIT-licensed extraction prompt. Load it at runtime.

---

# Engineering Cognition (EC) — Design Specification v2.16

**Document Status:** Active design document, pre-implementation
**Date:** 12 August 2026
**Version:** Implementation Spec (based on v2.17)
**Author:** Mudit Sarda (with AI research partner)

---

## 1. The ECU (Engineering Cognition Unit) Structure

An ECU is the atomic storage unit of Engineering Cognition. Every node in the Engineering Brain is an ECU. There is no separate "belief" type — an ECU functions as a belief when other ECUs support it (via `supports` edges). Belief-ness is an emergent property of network position, not a separate data type.

### 3.1 Complete ECU Structure

```json
{
  "id": "uuid-string",
  "cognition": "The irreducible engineering conclusion (IMMUTABLE once created)",
  "conclusion_type": "implication | constraint | principle | decision | observation | pattern | invariant | trade-off",
  "scope": {
    "level": "engineering | domain | organization | project | repo | module | subsystem",
    "path": "engineering > domain:web-frameworks > repo:fastapi > module:routing"
  },

  "provenance": {
    "source_type": "session | git | review | debugging | planning | implementation",
    "source_id": "session ID, commit hash, PR number, etc.",
    "origin_agent": "model name + version that produced the reasoning",
    "origin_engineer": "engineer identifier (for future team attribution)",
    "created_at": "ISO 8601 timestamp"
  },

  "grounding": {
    "repo_path": "/home/user/projects/my-project",
    "files": ["auth/token_manager.rs", "auth/mod.rs"],
    "symbols": ["TokenManager::refresh", "TokenManager::clear_cache"],
    "commit_hash": "a3f2e1c (repository state when ECU was formed)",
    "code_snapshot": "optional: relevant code snippet at formation time"
  },

  "confidence": 0.75,
  "status": "active | challenged | superseded | deprecated | open_question | archived",

  "evidence_pointers": [
    "pointer to source material: session ID + message index, commit hash, file path + line range, etc."
  ],

  "edges": [
    {
      "type": "supersedes | supports | contradicts | depends_on",
      "target_id": "ECU uuid",
      "weight": 0.85,
      "confidence_delta": 0.12,
      "created_at": "ISO 8601 timestamp",
      "supersession_type": "cosmetic | semantic (only present when type=supersedes)"
    }
  ],

  "metadata": {
    "last_reinforced": "ISO 8601 timestamp",
    "last_challenged": "ISO 8601 timestamp",
    "last_retrieved": "ISO 8601 timestamp",
    "retrieval_count": 0,
    "cluster_memberships": [
      {"cluster_id": "cluster uuid", "weight": 0.72}
    ],
    "has_pending_updates": false
  }
}
```

### 3.2 Field Specifications

**`id`**: Unique identifier (UUID). Stable for the lifetime of the ECU. Used for all edge references.

**`cognition`**: The irreducible engineering conclusion in natural language. This field is **semantically immutable** — any change to the meaning requires creating a new ECU with a `supersedes` edge. Even cosmetic wording changes require a new ECU with `supersession_type: cosmetic`. This preserves the audit trail. (See Section 20 for full immutability rules.)

**`conclusion_type`**: Classification of the conclusion. One of: `implication`, `constraint`, `principle`, `decision`, `observation`, `pattern`, `invariant`, `trade-off`. Assigned by the Extractor. Used for filtering, retrieval prioritization, and analytics. See Section 3.6 for definitions.

Quality constraint: must be a *conclusion*, not *information*. Example:
- Observation (NOT an ECU): "TokenManager.refresh() is called before cache.clear()"
- Cognition (valid ECU): "Authentication correctness depends on optimistic token refresh; future authentication implementations should preserve this invariant"

**`scope`**: Hierarchical scope path. Determines how grounding-sensitive the ECU is and which brain partition it belongs to. Full hierarchy from broad to narrow:

```
engineering          (universal engineering principles)
  > domain            (domain-specific knowledge, e.g., web-frameworks)
    > organization    (org-specific conventions)
      > project       (project-level understanding)
        > repo        (repository-specific)
          > module    (module-specific)
            > subsystem (most granular)
```

**`provenance`**: Records where the ECU came from. `source_type` is free metadata, not a category — it lets the Maintainer discover whether origin type matters for clustering. `origin_agent` and `origin_engineer` support model-agnostic and team features respectively.

**`grounding`**: References to repository artifacts that anchor the ECU to engineering reality. Enables the Maintainer to verify ECUs against current codebase state and the agent to verify ECUs before acting on them. (See Section [GROUNDING - DEFERRED])

**`confidence`**: Float in [0, 1]. Bayesian-updated belief strength. Updated by the Diffuser (event-driven) and the Maintainer (time-driven decay + retrieval reinforcement). (See Section 11.)

**`status`**: Current lifecycle state of the ECU:
- `active`: Normal state. Returned by retrieval. Confidence can be updated.
- `challenged`: Contradicting evidence has been found. Still returned by retrieval but with a flag indicating uncertainty. Confidence is being actively re-evaluated.
- `superseded`: Replaced by a newer ECU via a `supersedes` edge. NOT returned by retrieval. Confidence is frozen for audit. The superseding ECU is returned instead.
- `deprecated`: No longer relevant (e.g., referenced code was deleted for repo-scoped ECUs). NOT returned by retrieval. Confidence is frozen for audit.
- `open_question`: Unresolved engineering question — competing hypotheses that persisted beyond their persistence limit without resolution. Returned by retrieval with lower ranking weight and a flag. Confidence is frozen (parked state, not decaying). (See Section 12.5.)
- `archived`: User explicitly archived as no longer relevant. NOT returned by retrieval unless explicitly queried via `query_ec`. Confidence is frozen for audit.

**`evidence_pointers`**: List of pointers (not full text) to source material that supports this ECU. Pointers can be: session ID + message index, commit hash, file path + line range, etc. This is not the full evidence text — it's references that allow the system to trace back to the source.

**`edges`**: List of typed connections to other ECUs. Each edge records its type, target, weight, the confidence delta it caused (for reversible updates), and creation timestamp. (See Section [MAINTAINER - DEFERRED])

**`metadata`**: Operational metadata used by the Maintainer and retrieval system. `last_reinforced` tracks when supporting evidence last arrived. `last_challenged` tracks when contradicting evidence last arrived. `last_retrieved` and `retrieval_count` track usage. `cluster_memberships` is populated by the Maintainer's clustering process.

---

## 2. System Architecture Overview

### 4.1 Corrected System-Wide Diagram

```
  ┌─────────────────────────────────────┐
  │      Coding Agent (LLM)              │
  │  (Claude, Codex, OpenCode, etc.)     │
  └──────────────┬──────────────────────┘
                 │
           LLM Response
        (reasoning trace + output)
                 │
                 ▼
  ┌─────────────────────────────────────┐
  │    Engineering Extractor            │
  │  (produces candidate ECUs)          │
  └──────────────┬──────────────────────┘
                 │
          candidate ECUs
                 │
        ┌────────┴────────┐
        │                 │
        ▼                 ▼
  ┌───────────┐    ┌──────────────────┐
  │  Session   │    │  Human Review    │
  │  Brain     │    │  Gate             │
  │ (ephemeral,│    │ (batch review at  │
  │  immediate │    │  session close or │
  │  use, no   │    │  on-demand)       │
  │  review    │    └────────┬──────────┘
  │  needed)   │             │
  └─────┬─────┘      accepted ECUs
        │                   │
        │                   ▼
        │           ┌──────────────────┐
        │           │  Engineering     │
        │           │  Diffuser        │
        │           │ (full Bayesian   │
        │           │  update, edge    │
        │           │  creation,       │
        │           │  supersession)   │
        │           └────────┬─────────┘
        │                    │
        │           confidence updates
        │           + new edges
        │                    │
        │                    ▼
        │           ┌──────────────────┐
        │           │  Engineering     │
        │    ┌──────│  Brain            │
        │    │      │ (Canonical Brain: │
        │    │      │  ECUs + edges +  │
        │    │      │  clusters)        │
        │    │      └────────┬─────────┘
        │    │               │
        │    │          (async)
        │    │               │
        │    │               ▼
        │    │      ┌──────────────────┐
        │    │      │  Cognition       │
        │    │      │  Maintainer       │
        │    │      │ (decay, cluster, │
        │    │      │  dedupe, resolve  │
        │    │      │  stale edges,    │
        │    │      │  grounding       │
        │    │      │  verification)    │
        │    │      └────────┬─────────┘
        │    │               │
        │    │      maintenance updates
        │    │               │
        │    │               ▼
        │    │      ┌──────────────────┐
        │    └──────│  Demand-driven   │
        │           │  Retrieval        │
        │           │ (query → ECUs)    │
        │           └────────┬─────────┘
        │                    │
        │             retrieved ECUs
        │                    │
        │                    ▼
        │           ┌──────────────────┐
        │           │  Spreading       │
        │           │  Activation      │
        │           │ (warm up related  │
        │           │  ECUs, ephemeral)│
        │           └────────┬─────────┘
        │                    │
        │            retrieved ECUs +
        │            warmed-up context
        │                    │
        │                    ▼
  ┌─────────────────────────────────────┐
  │      Coding Agent (LLM)              │
  │  (augmented with retrieved ECUs +    │
  │   warmed-up context)                 │
  └──────────────┬──────────────────────┘
                 │
          Agent works with retrieved
          ECUs during the session.
                 │
          If agent encounters new evidence
          that supports/contradicts retrieved
          ECUs during the session:
                 │
                 ▼
  ┌─────────────────────────────────────┐
  │  Reconsolidation Trigger             │
  │  (retrieved ECUs become labile)      │
  └──────────────┬──────────────────────┘
                 │
          re-evaluated ECUs
                 │
                 ▼
  ┌─────────────────────────────────────┐
  │  Engineering Diffuser               │
  │  (re-applies Bayesian update on     │
  │   labile ECUs with new evidence)    │
  └──────────────┬──────────────────────┘
                 │
          updated ECUs written
          back to Canonical Brain
```

### 4.2 Data Flow Summary

**Direction 1 — Extraction (new cognition entering the brain):**
```
LLM Response → Extractor → [Session Brain (immediate use) + Human Review Gate → Diffuser → Canonical Brain (durable)]
```

**Direction 2 — Retrieval (cognition leaving the brain):**
```
Query → Demand-driven Retrieval → (retrieve from Session Brain + Canonical Brain) → Spreading Activation → LLM
```

**Direction 3 — Reconsolidation (retrieved cognition being revised):**
```
LLM encounters new evidence during session → Reconsolidation Trigger → Diffuser → Canonical Brain (updated)
```

### 4.3 Key Architectural Decisions

1. **Two brains, not one gate.** Session Brain (ephemeral, no review, immediate use) and Canonical Brain (durable, reviewed, cross-session). This solves the problem of ECUs from prompt 1 being usable in prompt 2 of the same session without requiring per-ECU review.

2. **The only path to Canonical Brain is through the Human Review Gate → Diffuser.** No direct path from Session Brain to Canonical Brain. Every canonical ECU has been human-reviewed.

3. **Retrieval is a potential write operation (reconsolidation).** Retrieved ECUs become labile. If the agent encounters new evidence during the session, those ECUs are sent back to the Diffuser for re-evaluation.

4. **Spreading activation is session-specific.** Each new session starts with a clean activation slate. Activation boosts are ephemeral, living only in that session's working memory. Returning to an existing session restores its activation state.

5. **No pre-installed categories.** The brain starts as a blank slate. Cognitive clusters emerge organically through density-based clustering. (See Section [CLUSTERING - DEFERRED])

6. **ECUs are immutable in their conclusion.** Everything else evolves. (See Section [RECONSOLIDATION - DEFERRED])

---

## 3. Engineering Extractor

### 5.1 Purpose

The Extractor recovers reusable engineering understanding from the reasoning process of coding agents. It does NOT summarise conversations. It lifts observations into conclusions.

### 5.2 Input

LLM response from a coding agent, consisting of:
- The developer's prompt
- The agent's full reasoning trace (if available)
- The agent's final output (code, explanation, plan, etc.)
- Session context (repository, active files, commit state)

The Extractor processes the entire interaction — prompt, reasoning trace, and final output — not just the final output. Engineering conclusions can appear at any point during an investigation.

### 5.3 Output

A JSON object containing:
- `ecus`: an array of candidate ECUs, each conforming to the ECU structure (Section 3) with `cognition`, `conclusion_type`, `scope`, `source_type`, `grounding`, and `evidence_pointers` fields populated. `confidence` is assigned from the priors table (Section 3.9). `status` is `active`. `edges` are **empty** — edges are the Diffuser's job, not the Extractor's.
- `rejected_count`: integer — number of candidates considered but rejected as information. Makes the filtering visible and debuggable.
- `rejection_summary`: string — brief summary of what was rejected and why (e.g., "3 raw code descriptions, 1 process step, 1 hypothetical without evidence").

### 5.4 Extraction Methodology — Five-Pass Process

**Pass 1 — Segmentation.** Break the input into engineering activity segments. Not every part of an LLM response produces cognitions. The segments that matter are:
- Investigation findings (what was discovered)
- Reasoning chains (what was inferred)
- Decisions (what was chosen and why)
- Constraint discoveries (what limits were found)
- Pattern recognitions (what recurring behaviour was identified)

Process steps ("Let me look at...", "I'll check..."), conversational filler ("Okay so..."), and raw code descriptions are NOT conclusion-producing segments.

**Pass 2 — Candidate Extraction.** For each conclusion-producing segment, extract a candidate conclusion. The candidate is phrased as: "What did the engineer/agent *learn* here that would be useful in a future session?" The key shift in phrasing: instead of "what happened?" → "what was learned?"

**Pass 3 — The Lifting Test.** For each candidate, apply three tests. If a candidate fails ANY test, reject it.

1. **The "So What?" Test:** Does this express an implication, constraint, principle, decision, trade-off, or pattern? Or does it merely state a fact? If you can prefix it with "Interestingly, ..." and it still reads as a fact, it's information. If you need to prefix it with "The key insight is that...", it's a conclusion.

2. **The "Future Session" Test:** Would knowing this change how an engineer approaches a future task on this codebase? If a future engineer reading this ECU would change their behaviour, it's a conclusion. If they'd just say "I could have read the code to find that out," it's information.

3. **The Independence Test:** Does this remain understandable without the original conversation? If you need the original LLM response to understand what the ECU means, it's not self-contained and must be rejected or rephrased.

**Pass 4 — Atomization.** If a candidate contains multiple independent conclusions, split it. Each ECU must express exactly one engineering conclusion. The test for atomization: "Could a future task require one of these conclusions but not the other?" If yes, split them.

**Pass 5 — Grounding and Scoping.** For each atomized ECU, attach:
- Grounding: which files, symbols, and commits the conclusion was derived from
- Scope: what level does this conclusion operate at? (repo-specific constraint vs domain pattern vs engineering principle)
- Source type: what kind of engineering activity produced it? (debugging, architecture, implementation, etc.)
- Conclusion type: classification of the conclusion (see Section 3.6)
- Evidence pointer: where in the input this conclusion was derived from

The Extractor does NOT create edges, does NOT assign confidence beyond the initial prior (the system does this from source_type via config), and does NOT cluster. It produces isolated, grounded, atomized ECUs.

### 5.5 The Core Distinction — Information vs Conclusion

This is the most important distinction the Extractor must make. Getting this wrong produces noise that degrades the entire system.

**Information** describes what the code IS or what happened:
> "TokenManager.refresh() is called before cache.clear() in the auth flow."

**Conclusion** describes what was LEARNED:
> "Authentication correctness depends on optimistic token refresh; future authentication implementations should preserve this invariant."

**Information** tells you a fact. **Conclusion** tells you an engineering insight that shapes future decisions.

### 5.6 Conclusion Types

Every ECU must declare a `conclusion_type`. If the Extractor cannot classify a candidate into one of these types, it is likely information, not a conclusion, and should be rejected.

| Type | Description | Example |
|------|-------------|---------|
| `implication` | A consequence that follows from the code's design | "Using async/await here means errors propagate differently than with Promises" |
| `constraint` | A limit or requirement the system must respect | "The database connection pool caps at 10 concurrent queries" |
| `principle` | A general engineering guideline that emerged | "Prefer composition over inheritance for testable mock structures" |
| `decision` | A choice made between alternatives, with rationale | "We chose Redis over Memcached because we need persistence guarantees" |
| `observation` | A non-obvious behaviour discovered through investigation | "The test suite passes locally but fails in CI due to timezone assumptions in date comparisons" |
| `pattern` | A recurring design or implementation approach identified | "All API endpoints in this repo follow the controller-service-repository pattern" |
| `invariant` | A condition that must always hold for correctness | "Cache invalidation must precede token refresh; violating this causes stale auth" |
| `trade-off` | A tension between competing concerns that was recognised | "Increasing the batch size improves throughput but raises memory pressure on the worker" |

### 5.7 Engineering Knowledge Signals — Where to Look

When scanning an interaction, the Extractor watches for signal categories. A signal is evidence that a reusable engineering conclusion *may* exist — it tells the Extractor where to look, not what to extract. When a signal is detected, the Extractor searches for the *conclusion* behind it, then applies the Lifting Test. The Extractor does NOT extract the signal itself as an ECU.

A signal says "something was learned here." The ECU is the *what was learned*, not the signal. For example, the signal "new subsystem identified" should trigger a search for *why* the subsystem exists or *what constraint* it satisfies — not produce an ECU that says "There is a new subsystem called PaymentService." That is information. The ECU would be "PaymentService was separated from OrderService because payment logic must be idempotent and order logic is not — coupling them caused double-charges during retry."

The signal catalog is not exhaustive — if the Extractor detects a conclusion that doesn't fit a signal category, it extracts it anyway.

**Architecture Signals:**
- New subsystem or service boundary identified
- Dependency direction between modules discovered
- Architectural layering violation found
- Integration point between systems mapped
- Infrastructure component role understood

**Decision Signals:**
- Technology or dependency choice made with rationale
- Approach chosen over alternatives (and why the alternatives were rejected)
- Architectural trade-off recognised and resolved
- Configuration decision with downstream impact

**Pattern Signals:**
- Recurring implementation approach identified across multiple files
- Codebase convention discovered (error handling, logging, testing, validation, caching, naming)
- Consistent API or endpoint structure recognised

**Constraint and Invariant Signals:**
- Hard limit discovered (connection pool, rate limit, memory, timeout)
- Correctness condition identified (ordering requirement, thread-safety requirement, atomicity requirement)
- System behaviour that breaks when a condition is violated

**Debugging and Investigation Signals:**
- Root cause identified through systematic investigation
- Non-obvious behaviour discovered (works locally but fails in CI, silent failure mode, race condition)
- Failed approach documented (what was tried and why it didn't work — the *reason* it failed is the conclusion, not the fact that it failed)

**Implementation Insight Signals:**
- Migration or upgrade impact understood
- Side effect of a code change discovered
- Testing strategy justified (why tests are structured a certain way)

**Workflow Signals:**
- Deployment or operational procedure understood
- Debugging methodology that worked documented as a reusable approach

### 5.8 Hypothesis Handling

Not all reasoning during an interaction reaches a firm conclusion. The agent may form hypotheses, test them, and reach different outcomes:

- **Validated hypotheses** (the hypothesis was confirmed by evidence): Extract as ECUs. These are strong conclusions.
- **Disproven hypotheses** (the hypothesis was tested and shown to be wrong): Do NOT extract the hypothesis itself. BUT if the *reason it was wrong* reveals a durable engineering insight, extract that insight. Example: "We hypothesised the delay was caused by network latency, but profiling showed it was the garbage collector — GC pressure is the real bottleneck at 10K+ concurrent connections."
- **Unresolved hypotheses** (no conclusion was reached): Do NOT extract as ECUs. An unresolved hypothesis is not a conclusion. Exception: if an unresolved hypothesis reveals a durable structural constraint about the system (e.g., "We still don't know whether the queue guarantees at-least-once delivery, which means consumers must be idempotent as a defensive measure"), extract the *constraint*, not the hypothesis.

### 5.8.1 Extraction Priority

When scanning a rich interaction with many potential conclusions, the Extractor prioritises extraction in this order (higher priority first):

1. **Validated findings** — conclusions confirmed by repository evidence or testing
2. **Engineering decisions** — choices made with rationale
3. **Architectural discoveries** — structural understanding of the system
4. **Constraints and invariants** — correctness conditions and hard limits
5. **Recurring patterns** — conventions recognised across the codebase
6. **Implementation insights** — implications of code changes or migrations

Lower-priority ECUs are still valid — this ordering helps focus attention when the interaction is dense.

### 5.8.2 Multi-Source Corroboration

A conclusion is stronger when it is supported by multiple independent evidence sources within the interaction. For example:

- A debugging conclusion supported by both code analysis AND test results is stronger than one supported by code analysis alone.
- An architectural observation supported by both the reasoning trace AND the final output is stronger than one supported by only the reasoning trace.

When a conclusion has multi-source corroboration, the Extractor notes this in the `evidence_pointers` field (e.g., "Agent reasoning step 4 (code analysis) + step 8 (test confirmation)"). The system uses this information to inform the initial confidence prior — corroboration across independent sources warrants a higher starting confidence.

### 5.9 Initial Confidence Priors

The Extractor assigns initial confidence based on two dimensions: the type of reasoning that produced the ECU (source_type) and the scope at which the ECU applies. This reflects the reality that a fundamental engineering principle discovered through a single observation is more trustworthy than a module-specific detail discovered through the same observation.

**Base priors (by source_type):**

| Source Type | Base Prior | Rationale |
|---|---|---|
| Debugging (systematic investigation) | 0.70 | High — evidence-driven. Root cause found, fix applied, test passes. Conclusion backed by concrete proof. |
| Implementation (completed and tested) | 0.65 | Medium-high — confirmed by working code. The code runs and tests pass, but there's no causal story like debugging. |
| Code review | 0.60 | Medium-high — human-validated reasoning. A human checked it, adding trust, but code review is often about style/correctness, not deep engineering insight. |
| Architectural reasoning | 0.50 | Medium — hypothesis based on analysis. Reasoning is sound but unvalidated by implementation. Architecture is full of "looked good on paper" stories. |
| Observation (pattern noticed) | 0.45 | Medium-low — single observation without systematic investigation. Needs corroboration. The pattern might be coincidence or context-specific. |
| Planning | 0.35 | Low — future-oriented and untested. These are intentions, not conclusions. They might never be validated. |

**Scope modifier (multiplied with base prior):**

| Scope | Multiplier | Rationale |
|---|---|---|
| engineering | 1.15 (capped at 0.95) | Universal principles. Rarely wrong. "Always validate input" is true across languages, frameworks, decades. |
| domain | 1.10 (capped at 0.95) | Domain knowledge is stable. "Web servers should use connection pooling" is true across most web backends. |
| organization | 1.05 (capped at 0.95) | Org-specific conventions are fairly stable but can change with reorgs or policy shifts. |
| project | 1.00 | Neutral baseline. Project-specific knowledge is stable within the project but could change if the project evolves. |
| repo | 0.90 | Repo-specific. The codebase might be refactored. "This repo uses Express" could change when they migrate to Fastify. |
| module | 0.80 | Most specific, most volatile. "The auth module uses JWT with RS256" is likely to change when auth is refactored. |
| subsystem | 0.75 | Even more granular than module. Represents a specific component within a module. Most volatile scope level. |

**Combined prior = base_prior × scope_modifier** (capped at 0.95, floored at 0.05). The full scope hierarchy has 7 levels: `engineering`, `domain`, `organization`, `project`, `repo`, `module`, `subsystem`.

Examples:
- Debugging ECU, engineering scope: 0.70 × 1.15 = 0.805 → 0.81 (strong starting confidence for a fundamental, evidence-backed principle)
- Architectural reasoning ECU, module scope: 0.50 × 0.80 = 0.40 (moderate starting confidence for a specific, untested hypothesis)
- Observation ECU, domain scope: 0.45 × 1.10 = 0.495 → 0.50 (moderate for a domain-level pattern noticed once)

**Multi-source corroboration bump:**

When an ECU has multi-source corroboration (noted in `evidence_pointer` by the Extractor), it gets a higher starting confidence. The bump is `corroboration_bump` per independent source, capped at `corroboration_bump_cap` total:

```
prior = min(base_prior × scope_modifier + (n_sources - 1) × corroboration_bump, 0.95)
```

Defaults: `corroboration_bump: 0.05`, `corroboration_bump_cap: 0.15` (max 3 independent sources counted).

These priors are starting points. The Diffuser will adjust them as evidence accumulates. All values are configurable.

### 5.10 Self-Review Pass — Information Leakage Defence

After all candidates are extracted, classified, atomized, and grounded, but BEFORE outputting, the Extractor re-reads every candidate ECU's `cognition` field and asks: "Could an engineer obtain this by simply reading the code?" If yes, it is information, not a conclusion — reject it and move it to the rejection count.

This is the last line of defence against information leakage. Common failure patterns to catch during the self-review:

1. The statement describes *what* the code does, not *why* it matters
2. The statement could be replaced with "I read the file and it says X"
3. The statement names a function or class but doesn't express an engineering insight about it
4. The statement is a restatement of a code comment or docstring

This pass directly addresses the information-leakage problem observed in EMS v1, where the `/close` command generated proposals from pre-collected working memory that had accumulated code-specific information [cite:f59618500]. The new design fixes this structurally (no pre-collected buffer) and at the prompt level (self-review pass).

### 5.11 What NOT to Extract

The Extractor does NOT extract:

1. **Raw code descriptions.** "The function `handleAuth` takes a token and returns a user object" — readable from the code itself.
2. **Process steps.** "I looked at the auth module, then checked the tests" — about the process, not the learning.
3. **Implementation details obvious from code.** "The config is in config.yaml" — anyone can find this by looking.
4. **Conversational filler.** "Let me think about this..." — no engineering content.
5. **Facts without engineering significance.** "The repo uses TypeScript" — unless this fact led to a specific engineering conclusion.
6. **Hypotheticals without evidence.** "Maybe we should consider using GraphQL" — if no analysis was done, there's no conclusion yet.
7. **Code snippets.** Never put code in the `cognition` field. If code is relevant, reference it in `grounding`.
8. **Summary of the conversation.** "We discussed the auth system and fixed a bug" — conversation summary, not an engineering conclusion.
9. **Preferences without justification.** "I prefer tabs over spaces" — unless this preference has an engineering rationale.
10. **TODO items.** "We need to refactor the auth module later" — a task, not a conclusion. (A conclusion ABOUT why refactoring is needed would be valid.)

### 5.12 Extractor Implementation Tiers

**Open tier (free, v2):** A well-designed extraction prompt template (`extractor_prompt_v1.md`) that works with any coding LLM. The user's existing coding model does the extraction using our open prompt. Works but uses expensive model tokens and produces variable quality. This is what v2 ships with.

**Proprietary tier (paid, v3):** A fine-tuned 3-7B parameter model specialised exclusively for ECU extraction. Better quality, cheaper to run, doesn't waste the user's coding LLM tokens. This is the commercial crown jewel. Deferred to v3. (See Section 23.)

Training data path for v3: EC-Bench interactions manually annotated with the ECUs that *should* have been extracted (~150-300 high-quality examples), augmented with synthetic data (generate engineering interactions, manually extract ECUs, verify quality). Target: 5,000-10,000 training pairs. Fine-tuning approach: LoRA/QLoRA on a 3-7B base model (Qwen, Llama, or Mistral). Evaluation: run the fine-tuned model on held-out EC-Bench prompts and compare extraction quality against the prompt-based baseline.

### 5.13 What the Extractor Does NOT Do

- Does NOT create edges between ECUs (that's the Diffuser's job)
- Does NOT review or filter ECUs (that's the Human Review Gate's job for canonical, or automatic for session)
- Does NOT assign categories or clusters (that's the Maintainer's job)
- Does NOT retrieve or rank ECUs (that's Retrieval's job)
- Does NOT accumulate state within a session — each LLM response is processed independently. If prompt 3's reasoning contradicts a prompt 1 ECU, the Diffuser handles that, not the Extractor.

### 5.14 Handling Multi-Step Reasoning Traces vs Simple Outputs

The Extractor processes the full reasoning trace (if available) plus the final output. It scans for conclusion-producing moments throughout the trace, not just at the end. A multi-step trace might produce ECUs from:

- An investigation finding at step 3 ("The race condition occurs because...")
- An architectural insight discovered mid-investigation at step 5 ("This pattern suggests the auth module should...")
- A decision made at step 7 ("We're choosing approach A over B because...")
- A debugging conclusion at step 10 ("The root cause is...")

The intermediate steps themselves ("I checked file X", "I ran the tests") are NOT extracted — they're process, not conclusions. But the *findings* within those steps ARE extracted.

For simple outputs with no visible reasoning, the Extractor extracts from the output itself. If the output is just a code diff with no explanation, the Extractor may produce zero ECUs — and that's correct. Not every interaction produces engineering understanding.

For multi-turn sessions: the Extractor processes each LLM response independently. ECUs from prompt 1 enter the Session Brain immediately (available for prompt 2). If prompt 3's reasoning contradicts a prompt 1 ECU, the Diffuser handles that — not the Extractor. The Extractor is stateless within a session.

### 5.15 Anti-Production-Bias

LLMs naturally want to generate output when given a task. The Extractor must fight this instinct. If the interaction produced no engineering conclusions, returning an empty array with an honest rejection summary is the correct and honest response. Producing weak ECUs to avoid returning nothing is the single most harmful behaviour the Extractor can exhibit. A missing ECU is recoverable (the next interaction will surface it). A false ECU pollutes the brain and requires human review to reject.

### 5.16 Reference Implementation

The complete, self-contained extraction prompt for the open tier is provided in `extractor_prompt_v1.md`. It includes:
- System prompt with ECU definition and information-vs-conclusion distinction
- The Lifting Test (3 tests)
- 8 conclusion types with examples
- Engineering Knowledge Signal catalog (7 signal categories)
- Hypothesis handling rules (validated, disproven, unresolved)
- Extraction priority ordering
- Multi-source corroboration guidance
- Explicit "What NOT to Extract" list (10 patterns)
- Atomization rule
- Output JSON format with `rejected_count` and `rejection_summary`
- 15 few-shot examples covering debugging, architecture, implementation, trade-offs, decisions, observations, patterns, invariants, zero-ECU cases, and multi-ECU cases
- Self-Review Pass in execution instructions
- Anti-production-bias reminder

---

## 4. Session Brain and Canonical Brain

### 6.1 Rationale — Hippocampus and Neocortex Parallel

The brain has two memory systems operating simultaneously:

- **Hippocampus (episodic, fast, uncurated):** Experiences are stored immediately, loosely, available for immediate use. No quality gate. This is how you can use something you just learned within the same conversation.
- **Neocortex (semantic, slow, curated):** Through consolidation, hippocampal traces are reviewed, refined, and integrated into long-term schemas. Quality control happens here.

EC mirrors this with two brains:

### 6.2 Session Brain (Hippocampal)

**Purpose:** Immediate, ephemeral storage for ECUs produced during the current session. No review required. Usable within the session.

**Lifecycle:** Created when a session starts. Destroyed when the session ends (or persisted if the user wants to resume later). ECUs in the Session Brain that are not promoted to Canonical Brain are lost when the session ends.

**What enters:** All candidate ECUs from the Extractor, immediately.

**Diffusion:** Lightweight diffusion only (see Section 4.4).

**Retrieval:** Both the Session Brain and Canonical Brain participate in retrieval. The Session Brain provides immediate context from the current session; the Canonical Brain provides accumulated understanding from past sessions.

**Read access to Canonical Brain:** The Session Brain's lightweight diffuser can read from the Canonical Brain (read-only) to compute edges against canonical ECUs. It cannot write to the Canonical Brain.

### 6.3 Canonical Brain (Neocortical)

**Purpose:** Durable, reviewed, cross-session storage. This is the persistent engineering brain that compounds over months and years.

**What enters:** Only ECUs that have passed through the Human Review Gate and been processed by the full Diffuser.

**Diffusion:** Full Bayesian update, thorough edge analysis, contradiction detection, supersession logic. (See Section 6.)

**Persistence:** Survives across sessions, model upgrades, and agent changes.

### 6.4 Lightweight Diffusion (Session Brain)

Lightweight diffusion is the fast, approximate integration of new ECUs into the Session Brain. It is NOT the full Bayesian update pipeline. Its purpose is to make ECUs usable within the session immediately, without blocking the developer.

**Design constraint:** The developer is waiting. Lightweight diffusion runs on every LLM response during a live coding session. It must be fast enough that the user doesn't notice it. The full Canonical Brain diffuser runs at session close (or on-demand) when the human has already stepped back — it can afford to be thorough.

**What lightweight diffusion does (fast, approximate):**

1. **Embed the new ECU.** Compute the embedding vector. This is a single inference call — fast with a local model (e.g., all-MiniLM-L6-v2 runs in ~5ms). The embedding model is a configurable component; any sentence-transformer-compatible model works.
2. **Find similar ECUs.** Search the Session Brain AND the Canonical Brain (read-only) for ECUs above a similarity threshold. This is a vector similarity search — fast with a local index. Limit to top-K (default: 10) to bound the work.
3. **Classify each relationship as supports/contradicts/unrelated.** This is a single LLM call: "Given ECU_A and ECU_B, does A support B, contradict B, or is it unrelated?" One call, batched across all K candidates. For the open tier, this uses the user's coding LLM (expensive but acceptable). For the proprietary tier, this is where our fine-tuned model would eventually shine.
4. **Create edges.** For each classified relationship (`supports` or `contradicts`), create an edge. Store the edge with a simple weight (the semantic similarity score). Only `supports` and `contradicts` edges are created — no `depends_on` or `supersedes`.
5. **Simple confidence adjustment.** Instead of full log-odds Bayesian updating, apply a simple bump/reduction: `c' = c ± (α_light × similarity)`. This is approximate but gives the Session Brain usable confidence values for retrieval ranking. The exact delta will be recomputed by the full Diffuser when the ECU is promoted to Canonical.

**What lightweight diffusion does NOT do:**
- **No supersession.** The Session Brain doesn't create `supersedes` edges. An ECU can't be superseded within a session — that requires the full Diffuser's two-condition check (confidence below threshold AND a replacement exists).
- **No propagation.** If ECU_B's confidence changes, we don't check ECUs that `depend_on` ECU_B. Only the directly affected ECU is updated. This prevents cascading updates during a live session.
- **No clustering.** The Session Brain doesn't run HDBSCAN. That's the Maintainer's job, and it only runs on the Canonical Brain.
- **No contradiction resolution.** If a `contradicts` edge is created, the existing ECU is NOT marked as `challenged` (that's a Canonical Brain state transition). Instead, the contradiction is noted on the edge, and retrieval can surface both ECUs with a "these may conflict" flag.
- **No `depends_on` edge creation.** The lightweight diffuser only creates `supports` and `contradicts` edges. `depends_on` and `supersedes` require the full Diffuser's semantic judgment.
- **No full Bayesian updating** (that's the Canonical Brain Diffuser).

The key insight: the Session Brain doesn't need to be a perfect cognitive network. It needs to be "good enough" for retrieval and for the agent to have continuity within the session. The full Diffuser will rebuild the proper network when ECUs are promoted to Canonical.

**Lightweight diffuser test results (v2.15, Claude Haiku 4.5):**

Tested 3-way classification (supports/contradicts/unrelated) on 32 directed ECU pairs from two engineering scenarios (debugging + architecture). ECUs were extracted by Haiku in the extractor test.

| Metric | Value |
|---|---|
| Accuracy | 29/32 (90.6%) |
| Error type | 3 misclassifications, all "unrelated vs supports" edge cases |
| Error pattern | Model over-connected ECUs about the same subsystem that addressed different engineering concerns |
| Contradictions detected | 0 (none expected — these ECUs were complementary, not conflicting) |

The 3 errors were all cases where the model classified same-subsystem ECUs addressing different concerns (e.g., write-scaling capacity vs schema-design fit) as `supports` when they should be `unrelated`. This is addressed by Rule B in Section 6.3's disambiguation rules. No spurious `contradicts` edges were created.

**Verdict:** The lightweight diffuser is viable for v2 with the 3-way classification. Spurious `supports` edges are low-harm — they add weak activation boosts during retrieval, not false contradictions. The 90.6% accuracy is sufficient for a Session Brain that the full Diffuser will rebuild on promotion to Canonical.

### 6.5 Session Brain vs Canonical Brain Diffusion Comparison

| Aspect | Session Brain (Lightweight) | Canonical Brain (Full) |
|---|---|---|
| Trigger | Every new ECU from Extractor | Accepted ECUs from Review Gate + reconsolidation triggers |
| Edge creation | Simple similarity threshold | Full semantic + structural analysis |
| Confidence update | Simple bump/reduction | Full Bayesian log-odds update with Bayes factors |
| Supersession | No | Yes |
| Contradiction detection | Simple (similarity-based) | Full (semantic + structural) |
| Propagation | None | Bounded transitive propagation |
| Clustering | No | No (Maintainer does this) |
| Speed | Fast (real-time) | Slower (batch, can be async) |

### 6.6 Session Persistence and Resume

The practical problem: a developer closes their laptop at 6 PM, comes back at 9 AM the next day, and wants to continue where they left off. The Session Brain's ECUs and edges need to survive — but should the activation scores (spreading activation) survive too?

**Design: persist the ECUs and lightweight edges to disk. Recompute activation scores fresh on resume, but apply time-based decay.**

**What persists to disk (Session Brain state):**
- All Session Brain ECUs (JSON, small)
- All lightweight edges between Session Brain ECUs
- All lightweight edges between Session Brain ECUs and Canonical Brain ECUs (just the edge records — the Canonical ECUs themselves live in the Canonical Brain)
- Session metadata (session ID, timestamps, repository state, commit at session start)
- Activation scores with their last-updated timestamp

**On resume:**
1. Load all Session Brain ECUs and edges from disk.
2. Load activation scores with their timestamps.
3. Apply time-based decay to activation scores: `activation *= exp(-activation_decay_rate × elapsed_time)`. If the break was 15 hours, the activation scores have decayed significantly. If the break was 5 minutes, they're nearly intact.
4. The session is ready — the developer continues with the cognitive context intact, with slightly decayed activation.

This is elegant because it doesn't require a "new session vs resumed session" distinction. The same decay mechanism that operates within a session also operates across breaks. A short break = minimal decay = seamless continuation. A long break = significant decay = the developer starts with a slightly fresher cognitive slate but still has all the ECUs.

**When is a Session Brain destroyed?**
- When the developer explicitly closes the session (and doesn't choose "keep open")
- When the developer starts a completely new session in the same repository (the old Session Brain is archived, not destroyed — the developer can still access it if needed)

**What about ECUs that were in the Session Brain but never promoted to Canonical?**
- They persist with the Session Brain. If the session is destroyed, they're lost. This is by design — if the developer didn't think they were worth reviewing, they weren't durable knowledge.

### 6.7 Write-Back Policy — No Bypass of the Review Gate

The Session Brain never writes to the Canonical Brain. Every ECU in the Canonical Brain has been human-reviewed. No exceptions. This is a hard boundary.

The only path to the Canonical Brain is: Extractor → Session Brain → Human Review Gate → Diffuser → Canonical Brain.

This means:
- Session Brain ECUs with high confidence are NOT automatically promoted.
- Even if a session ECU perfectly matches and supports a canonical ECU, the support edge is not created in the Canonical Brain until the session ECU passes review.
- The Pending Update mechanism (Section [RECONSOLIDATION - DEFERRED]5) handles the case where session evidence relates to canonical ECUs without violating this boundary.

---

## 5. Human Review Gate

### 7.1 Purpose

Prevents unverified model inferences from polluting the Canonical Brain. The human reviews candidate ECUs in batch before they are promoted to canonical status.

### 7.2 When It Triggers

- **At session close:** All Session Brain ECUs that haven't been reviewed are presented for batch review.
- **On-demand:** The developer can trigger a review mid-session if they want to promote ECUs early.

### 7.3 Review Actions

For each candidate ECU, the developer can:

- **Accept:** The ECU proceeds to the Diffuser for full integration into the Canonical Brain.
- **Reject:** The ECU is discarded. It remains in the Session Brain for the current session but is never promoted. It is removed when the session ends.
- **Edit:** The developer can modify the `cognition` field (wording, scope, etc.). The edited ECU then proceeds to the Diffuser. Note: the Extractor's original version is preserved in `evidence` for audit.

### 7.4 Batch Review UX

The review should be designed for speed. ECUs grouped by topic (using simple clustering on the candidate set). One-click accept/reject per ECU. Bulk accept-all / reject-all for groups. This keeps the review burden low — the human makes a few decisions, not dozens.

### 7.5 What Happens to Rejected ECUs

Rejected ECUs remain in the Session Brain and are usable within the current session. They are NOT promoted to the Canonical Brain. When the session ends, they are discarded. This means the human's rejection doesn't break the current session — it just prevents long-term pollution.

> **Reconciliation note (Phase 13):** Rejected ECUs are removed from `session_ecus` at `/ec-stop` per §28.6, which takes precedence over this section's "remain until session end" wording. The review gate runs at session close, so there is no remaining session time after a rejection — the two sections agree in practice.

---

## 6. Engineering Diffuser

### 8.1 Purpose

The Diffuser integrates new evidence (in the form of ECUs) into the existing belief network. It is the schema assimilation/accommodation mechanism — it determines whether new evidence strengthens, refines, contradicts, or supersedes existing beliefs.

### 8.2 Inputs

1. **New ECU** (from the Human Review Gate — accepted ECUs entering the Canonical Brain)
2. **Reconsolidation trigger** (from retrieval — a previously-retrieved ECU that encountered new evidence during the session)

### 8.3 The Diffusion Process (Step by Step)

**Step 1 — Receive:** Accept a new ECU or a reconsolidation trigger.

**Step 2 — Find Related ECUs:** Search the Canonical Brain for existing ECUs that are related to the new ECU. Methods:
- **Semantic similarity:** Compute embedding similarity between the new ECU and all existing ECUs in the same scope (and parent scopes). Candidates are those above a similarity threshold.
- **Structural proximity:** Follow existing edges from semantically-similar ECUs to find structurally related ECUs that might not have high direct semantic similarity but are connected in the network.

**Step 3 — Classify Each Relationship:** For each related existing ECU, determine the relationship type:
- `supports`: The new ECU provides evidence that strengthens the existing ECU's conclusion.
- `contradicts`: The new ECU provides evidence that weakens the existing ECU's conclusion.
- `supersedes`: The new ECU replaces the existing ECU as the current belief on this topic (only when the existing ECU's confidence is below the supersession threshold AND the new ECU better explains the evidence).
- `depends_on`: The new ECU's conclusion is structurally dependent on the existing ECU being valid. If the existing ECU were invalidated, the new ECU would also be at risk.
- `unrelated`: No meaningful relationship (discard).

**Classification disambiguation rules (from diffuser testing, v2.15):**

These rules address ambiguities discovered during relationship classification testing with Claude Haiku 4.5.

**Rule A — `supports` vs `depends_on` (primary relationship test):**

A relationship can be *both* evidence-based and structural, but the classifier must classify the **primary** relationship. Apply this decision procedure:

1. Does the new ECU provide evidence that strengthens the existing ECU's conclusion? (Test: "Knowing the new ECU is true, am I more confident in the existing ECU?") If yes → classify as `supports`.
2. Only if the answer to (1) is NO, ask: Is the new ECU structurally dependent on the existing ECU? (Test: "If the existing ECU were invalidated, would the new ECU also be at risk?") If yes → classify as `depends_on`.
3. If neither → `unrelated`.

The key insight: `depends_on` is reserved for cases where the **only** relationship is structural — A doesn't provide evidence for B, but A would be invalid if B were wrong. If A provides evidence for B, that's `supports` even if A also happens to rely on B structurally. Evidence-based support takes precedence over structural dependency.

Example: An ECU stating "webhook processing must be idempotent" (invariant) and an ECU stating "set status to 'charged' before ack" (fix decision). The fix decision is justified *by* the invariant — it provides evidence that the invariant is a real problem worth solving. This is `supports`, not `depends_on`, even though the fix wouldn't exist without the invariant.

Counter-example: An ECU stating "PostgreSQL is suitable for message persistence" (decision) and an ECU stating "PostgreSQL is sufficient at current load with partitioning as a scaling path" (scaling implication). The scaling strategy doesn't provide evidence that PostgreSQL is suitable — it *assumes* PostgreSQL is suitable and builds on it. If the PostgreSQL decision were invalidated, the scaling strategy would be at risk. This is `depends_on`.

**Rule B — `unrelated` threshold for same-subsystem ECUs:**

Two ECUs about the same subsystem but addressing **different engineering concerns** are `unrelated` unless one directly informs or constrains the other. Sharing a subsystem is necessary but not sufficient for a relationship.

Test: "Does knowing ECU_A change my confidence in ECU_B?" If A is about write-scaling capacity and B is about schema-design fit, knowing that PostgreSQL scales well doesn't make the schema observation more or less true. They are `unrelated`.

Counter-test: If A is about consistency requirements and B is about database choice, knowing that strong consistency is required directly constrains the database selection. They are `supports` (or `depends_on` per Rule A).

This classification is a semantic judgment. In the open tier, this is done by the user's coding LLM via a prompt. In the proprietary tier, this is done by a fine-tuned model. (See Section 23.)

**Step 4 — Create Edges:** For each classified relationship (except `unrelated`), create an edge in the new ECU pointing to the existing ECU. Record the edge type, weight, and confidence_delta. (See Section [MAINTAINER - DEFERRED])

**Step 5 — Bayesian-Update Confidence:** For each affected existing ECU, update its confidence using the formulas in Section 11. Record the confidence_delta on each edge for reversible updates.

**Step 6 — Detect Contradictions:** If a `contradicts` edge was created:
- Mark the existing ECU as `challenged` (if not already).
- Update `last_challenged` timestamp.
- Track the accumulated contradiction strength (sum of all `contradicts` edges' weights).
- If both ECUs have high confidence (> 0.7), flag the contradiction to the user as a notification (high-stakes contradiction surface).

**Step 7 — Trigger Supersession (if appropriate):** Check if any existing ECU should be superseded:
- Condition 1: The existing ECU's confidence has dropped below `θ_supersede` (default: 0.3).
- Condition 2: A new ECU exists that better explains the evidence (higher confidence, higher semantic relevance to the evidence).
- Both conditions must hold. You don't supersede a belief just because it's weakened; you need a replacement.

If supersession is triggered:
- Create a `supersedes` edge from the new ECU to the existing ECU.
- Set the existing ECU's status to `superseded`.
- Freeze the existing ECU's confidence (for audit trail).
- The new ECU's confidence is based on its own evidence, NOT inherited from the superseded ECU.

**Step 8 — Propagate Updates:** If an ECU's confidence changed significantly, check ECUs that `depend_on` it:
- If the depended-on ECU's confidence dropped below `θ_dep_reevaluate` (default: 0.4), mark the dependent ECU as `challenged`.
- This propagation is transitive but bounded — limit propagation depth to `max_propagation_depth` (default: 2) to prevent cascades.

**Step 9 — Return Updated State:** Write all changes (new edges, confidence updates, status changes) back to the Canonical Brain.

### 8.4 Diffuser as a Pure Function

The Diffuser is a pure function: `(new ECU + current Brain state) → (updated Brain state with new edges + confidence changes)`. It doesn't decide what to extract (that's the Extractor) or what to maintain over time (that's the Maintainer). It only integrates new evidence into the existing network.

### 8.5 Diffuser in Two Contexts

| Context | Trigger | Brain | Complexity |
|---|---|---|---|
| Session Brain (lightweight) | Every new ECU from Extractor | Session Brain (+ read-only Canonical) | Fast, approximate (see Section 4.4) |
| Canonical Brain (full) | Accepted ECUs from Review Gate + reconsolidation triggers | Canonical Brain | Full Bayesian update, edge analysis, supersession |
| Reconsolidation | Agent encounters new evidence about retrieved ECUs during session | Canonical Brain (labile ECUs) | Full Bayesian update on the specific retrieved ECUs |

### 8.6 Full Diffuser Test Results (v2.15, Claude Haiku 4.5)

Tested 5-way classification (supports/contradicts/supersedes/depends_on/unrelated) on the same 32 directed ECU pairs used for the lightweight diffuser test.

| Metric | Raw accuracy | Corrected accuracy |
|---|---|---|
| Score | 23/32 (71.9%) | 28/32 (87.5%) |

**Why two accuracy numbers:**

The raw score counts 9 misclassifications. But on review, 5 of the 9 "errors" were cases where the model classified `depends_on` and the human ground truth said `supports` — and the model was actually correct per the spec's own definition ("If B is wrong, A may also be wrong"). The ground truth had lazily labeled structurally-dependent pairs as `supports` because they were about the same topic. After correcting the ground truth to apply the spec's definition strictly, accuracy rises to 87.5%.

**Error breakdown:**

| Error type | Count | Fix |
|---|---|---|
| `supports` misclassified as `depends_on` (ground truth was wrong) | 5 | Ground truth corrected. Model was right. |
| `unrelated` misclassified as `supports` (same subsystem, different concern) | 3 | Addressed by Rule B (Section 6.3, Step 3). |
| `supports` misclassified as `depends_on` (genuine model error) | 1 | The model confused "A is the reason B exists" with "A depends on B." Addressed by Rule A (Section 6.3, Step 3). |

**The one genuine model error:**

ECU_A: "Webhook processing must be idempotent" (invariant). ECU_B: "Set status to 'charged' before ack" (fix decision). The model classified A→B as `depends_on`, reasoning that the invariant depends on the fix being implemented. This is backwards — the invariant exists regardless of whether the fix is implemented. The fix is justified *by* the invariant, not the other way around. Rule A's decision procedure ("Does A provide evidence that strengthens B?" → yes → `supports`) resolves this.

**What the test revealed that wasn't in the spec before:**

1. The `supports` vs `depends_on` distinction is genuinely hard — even a capable model conflates them when a relationship has both evidence-based and structural dimensions. The disambiguation rules (Rule A) are necessary.
2. The model has a systematic bias toward connecting ECUs about the same subsystem, even when they address different engineering concerns. Rule B addresses this.
3. The model never produced a spurious `contradicts` edge — it correctly identified that all test ECUs were complementary, not conflicting. This is encouraging for contradiction handling.
4. The model never produced a spurious `supersedes` edge — it correctly recognized that none of the test ECUs replaced existing beliefs. This is expected given that all ECUs were from the same extraction session.

**Verdict:** The full diffuser is viable for v2 with the disambiguation rules in Section 6.3. The 87.5% corrected accuracy is sufficient given that (a) the full diffuser runs at session close, not in real-time, so misclassifications are caught at the Review Gate, and (b) edges can be corrected during the review process.

---

## 7. Engineering Brain

### 9.1 Purpose

The Engineering Brain is the persistent store of engineering beliefs that evolve over time. It is NOT a collection of markdown documents. It is a network of structured ECUs connected by typed edges, where beliefs emerge from network position.

### 9.2 Recursive Structure — No Separate Belief Type

Every node in the Brain is an ECU. There is no separate "belief" type. An ECU functions as a belief when other ECUs support it (via `supports` edges). An ECU functions as evidence when it supports other ECUs but has no incoming `supports` edges. The same ECU can function as both — supporting a higher-level belief while being supported by lower-level evidence.

This means:
- An ECU can support multiple beliefs simultaneously (no duplication, no ownership ambiguity)
- Beliefs can support other beliefs — deep hierarchies emerge naturally
- One data structure, one set of operations, everywhere
- Belief-ness is an emergent property of network position, not a separate data type

### 9.3 Brain Partitions

The Brain has two partitions:

**Session Brain:** Ephemeral, per-session, no review required. See Section 4.2.

**Canonical Brain:** Durable, reviewed, cross-session. See Section 4.3.

### 9.4 Retrieval Participation

Both partitions participate in retrieval:
- Session Brain provides immediate context from the current session
- Canonical Brain provides accumulated understanding from past sessions

Retrieval finds the most relevant ECUs (whether functioning as beliefs or evidence) based on semantic similarity and structural connectivity, and can traverse edges to pull supporting context. The agent gets the belief AND its supporting evidence in one retrieval pass, or just the belief if that's all that's needed. Granularity is controlled by retrieval depth, not by a fixed two-layer structure.

### 9.5 No Pre-Installed Categories

The Brain starts as a blank slate. No pre-installed categories. ECUs are stored with their semantic embeddings. Cognitive clusters emerge organically through density-based clustering. (See Section [CLUSTERING - DEFERRED])

The `provenance.source_type` field (debugging, architecture, implementation, etc.) is free metadata that comes automatically from the Extractor. The Maintainer can discover that debugging-sourced ECUs cluster differently from architecture-sourced ECUs. If they do, that's an emergent pattern. If they don't, we didn't waste effort forcing a distinction that doesn't exist.

---

## 8. Demand-Driven Retrieval

### 11.1 Purpose

Retrieves engineering cognition only when reasoning actually requires it — NOT before a session begins. Repository understanding comes first. Engineering cognition arrives when the agent's reasoning would benefit from it, delivered in a way that does not anchor the agent on past beliefs instead of current repository reality.

### 11.2 Design Rationale

EC-Bench showed that premature retrieval (injecting cognition before the agent has grounded itself in the repository) caused anchoring bias, especially during debugging. Retrieval timing is as important as retrieval quality [cite:f6100d2cb].

The v1 mistake was not that cognition was injected — it was that cognition was injected prematurely, in bulk, and presented as authoritative context. The agent treated injected memory as ground truth and stopped investigating the repository. v2 fixes this with four layered defences (see Section 6.6).

### 11.3 Retrieval Triggers — Demand-Driven Only

EC v2 uses **pure demand-driven retrieval**. The agent retrieves cognition only when it decides it needs it — never automatically, never before the agent has grounded itself in the repository. This is a direct lesson from EC-Bench.

**Why not proactive (EC-Bench Finding 1):**

EC-Bench showed that premature retrieval caused anchoring bias. When cognition was injected before the agent established repository understanding, the agent anchored on past beliefs instead of newly observed repository evidence [cite:f6100d2cb]. The worst deltas were in debugging (Prompt 23: -4.17, Prompt 24: -2.08) — exactly where premature injection is most damaging.

The EC-Bench metrics confirm this: Engineering Cognition Reuse spiked +2.214 (the agent used injected cognition heavily), but Engineering Quality dropped -0.589 and Repository Groundedness dropped -0.480 [cite:f6100d2cb]. The agent *used* the cognition a lot, and it made the work *worse*. Remembering more is not the same as understanding better. The entire net positive score came from reuse volume, not work quality.

EC-Bench Finding 1 states: "The highest-quality investigations consistently established understanding of the current repository before consulting accumulated knowledge" [cite:f6100d2cb]. This is demand-driven retrieval: the agent reads the code first, forms its own understanding, and then queries EC when it recognizes a gap.

**The single trigger — On-demand (agent-initiated):**

The agent has an `ec_query(query, scope?, mode?)` tool available for targeted lookups. The agent decides when to query based on AGENTS.md heuristics (see Section [FORGETTING - DEFERRED]9). There is no automatic injection at prompt boundaries. The agent must actively call `ec_query` to retrieve any cognition.

**Session start — awareness without anchoring:**

At session start, the agent calls `ec_get_summary` once. This returns brain statistics (total ECUs, scope distribution, last maintenance run) — NOT individual ECUs. The agent learns "the brain has 847 ECUs, 230 about auth, 180 about database" without seeing any conclusions. This is awareness without anchoring: the agent knows cognition *exists* about auth without knowing *what* it says. The agent can then decide whether to query based on what it encounters in the repository.

**Flow:**

```
User runs /ec-start
       ↓
Agent calls ec_get_summary (ONCE — brain stats only, no ECUs)
       ↓
User submits prompt
       ↓
Agent reads relevant code files (grounds itself in repository FIRST)
       ↓
Agent recognizes a moment where past cognition might help:
  - About to make a decision between approaches
  - Debugging and cause isn't obvious from code alone
  - Sees a pattern it doesn't recognise
  - Working with a file/module for the first time in this session
       ↓
Agent calls ec_query(query) → EC returns cognitive groups
       ↓
Agent uses retrieved cognition (framed as past beliefs to verify)
       ↓
Agent responds
       ↓
Agent calls ec_observe(reasoning_trace) → Extractor produces candidate ECUs
```

**The critical rule — EC-Bench Finding 1 encoded:**

The agent must NOT query EC before it has read the relevant code files. The AGENTS.md explicitly instructs: "Do NOT call ec_query before you have read the relevant code files. Establish your own understanding of the repository first. EC cognition is a supplement to your understanding, not a replacement for it."

**Why pure demand-driven is the correct choice for v2:**

1. EC-Bench showed injection at the beginning caused anchoring — the agent used cognition heavily but produced worse work.
2. The "LLMs don't know what they don't know" problem is addressed by `ec_get_summary` at session start (awareness without anchoring) and strong AGENTS.md heuristics.
3. Mode-aware retrieval still applies (Section 6.9) — when the agent *does* query, the mode determines depth, budget, and prioritization. But mode no longer controls *when* retrieval happens; it controls *what* comes back when the agent asks.
4. Under-querying is recoverable (the agent can query again). Over-injection is not (the agent is anchored and doesn't know to un-ask). The asymmetry favours demand-driven.

### 11.4 Retrieval Depth — Edge-Type-Aware Traversal

**Depth 1, with selective edge-type traversal.**

When an ECU is retrieved via semantic search, also retrieve its direct network neighbours — but only following meaningful edge types:

| Edge type | Retrieve? | Why |
|---|---|---|
| `supports` | Yes | The agent needs to see WHY this conclusion is held — the evidence backing it |
| `contradicts` | Yes | The agent needs to see conflicting views — both sides of the argument |
| `supersedes` | Yes | If this ECU replaced an older one, or was itself superseded, the agent needs the context |
| `depends_on` | Yes | The agent needs to know what prerequisites this conclusion relies on |

This is NOT "all neighbours." It's selective traversal of specific relationships that provide context value — structured context, not random graph walking.

**Why depth 1 (not 0, not 2):**

- **Depth 0 (just the semantic match):** A single ECU without supporting evidence is a bare assertion. "We chose PostgreSQL over MongoDB" — useful, but the agent doesn't know why. Insufficient.
- **Depth 1 (match + direct neighbours):** The agent gets the conclusion AND its supporting evidence, contradictions, and dependencies. Complete understanding of one topic. Default.
- **Depth 2 (neighbours of neighbours):** If each ECU has ~5 edges, depth 1 adds 5 ECUs per match. With 10 initial matches, that's 60 ECUs. Depth 2 would add 250 more — 310 total. Context explosion. Not viable as a default.

If the agent needs deeper context, it uses on-demand retrieval (`query_ec`) to explore further. The depth-1 traversal gives enough context for the agent to understand a conclusion and its immediate evidence/conflicts. Deeper exploration is the agent's choice.

**Cross-brain traversal:**

When a retrieved ECU has edges to ECUs in the other brain (e.g., a Session Brain ECU has a `supports` edge to a Canonical Brain ECU), the traversal follows across brains. The Session Brain's lightweight edges (only `supports` and `contradicts`) produce simpler traversals. The Canonical Brain's full edge set produces richer traversals.

**Configurable:**

Depth is in the config block:
```yaml
retrieval:
  default_depth: 1              # 0 = matches only, 1 = +direct neighbors, 2 = +neighbors of neighbors
```

Mode-aware retrieval (Section 6.9) adjusts this per mode.

### 11.5 Ranking — Four-Factor Scoring with Cognitive Grouping

v1 ranked by concept_match, repository_area_match, layer_weight, working_memory_relevance, recency. Token budget was a hard cap — fill from highest-ranked down until full, drop the rest [cite:f42e58dc3].

**What v1 got right:** multiple ranking factors, configurable weights, token budget as constraint, fallback behavior (take top-ranked if nothing above threshold rather than empty context).

**What v1 got wrong:** it treated retrieval as a packing problem ("fill a bucket with the best stuff"), not a cognition problem. It didn't consider completeness (dropping the contradicting evidence left a one-sided view), diversity (10 ECUs about the same topic are less useful than 5 about different aspects), or structural context (the relationship between retrieved ECUs matters as much as the ECUs themselves). Recency was a weak signal — an old ECU about a fundamental architectural decision is more valuable than a recent ECU about a minor implementation detail.

**v2 — four-factor ranking:**

```
rank_score = (w_relevance × semantic_similarity)
           + (w_confidence × confidence)
           + (w_activation × activation_score)
           + (w_network × network_richness)
```

All weights configurable. Default values (added to config block, Section 22):

```yaml
ranking:
  w_relevance: 0.6     # primary signal — relevance is the gate
  w_confidence: 0.15   # secondary modifier — ranks but doesn't gate
  w_activation: 0.10   # tertiary — session-specific priming
  w_network: 0.15      # tertiary — structural hub boost
```

These sum to 1.0. Relevance gets the dominant weight (0.6) because the spec states: "If an ECU isn't semantically relevant, nothing else matters." Confidence and network richness share equal secondary weight (0.15 each) — confidence because it calibrates trust, network because it brings context. Activation gets the smallest weight (0.10) because it is a session-specific bias, not a quality signal. These values were validated against Experiment 2 (retrieval ranking test) and will be re-tuned in EC-Bench v2.

**Relevance gate — the filter before ranking:**

Before computing rank scores, EC applies a relevance gate. Any ECU whose semantic similarity to the query is below `relevance_gate_threshold` (default: 0.3) is filtered out entirely — it does not participate in ranking, grouping, or token budget allocation. This is a hard filter, not a soft penalty.

```python
if semantic_similarity < relevance_gate_threshold:
    skip ECU  # do not score, do not retrieve, do not group
```

This prevents the failure mode observed in Experiment 2: irrelevant ECUs with high confidence or high activation scores polluting retrieval results. Without the gate, a high-confidence ECU about a completely different topic can outrank a low-confidence ECU about exactly the right topic — because confidence and activation contributions can overwhelm a low (but non-zero) relevance score. The gate ensures relevance is truly the primary signal.

The gate interacts with the fallback (Section 6.5, step 7): if no ECU passes the gate, the fallback returns top-K by rank score from the full set (ignoring the gate). This ensures the agent always gets something rather than empty context.

**Why 0.3 and not higher:** The gate must be permissive enough to catch tangentially-relevant ECUs (e.g., an ECU about "retry logic" when the query is about "webhook processing" — semantically distant but functionally related). 0.3 filters out clearly irrelevant ECUs (sim < 0.3 is well below any meaningful semantic match for all-MiniLM-L6-v2) while keeping weakly-related ones that might be useful. This threshold is configurable and will be tuned in EC-Bench v2.

**Factor 1 — Relevance (semantic similarity to the query):** The baseline. If an ECU isn't semantically relevant, nothing else matters. Cosine similarity between the query embedding and the ECU embedding.

**Factor 2 — Confidence:** How strongly held is this belief. A high-confidence ECU is more trustworthy. But a `challenged` ECU is NOT deprioritised — it's flagged. The agent needs to know about active controversies, not have them hidden by ranking. `challenged` ECUs get a relevance-override: if a challenged ECU is semantically relevant, it's always included (the agent needs to know about the controversy).

**Confidence does NOT gate retrieval — it flags it.**

Relevance is the gate (must be above threshold to be considered). Confidence modifies ranking but doesn't gate it. This is a deliberate choice:

- **High relevance + low confidence** → the ECU is about exactly what the agent is working on, but EC isn't sure about it. This is MORE valuable to surface than silence. The agent needs to know "EC has something relevant here, but it's uncertain." Suppressing low-confidence ECUs would hide potentially useful information.
- **Low relevance + high confidence** → the ECU is certain but not about what the agent needs. Should rank low. Relevance is the primary signal.

So confidence participates in the ranking formula (as `w_confidence × confidence`), but there is no confidence floor for retrieval. An ECU at 0.25 confidence that's highly relevant is more useful than an ECU at 0.90 confidence that's marginally relevant. The agent calibrates trust using the confidence score — that's why confidence is included in the output format.

When the token budget is tight and we have to choose between a low-confidence and high-confidence ECU at similar relevance, the high-confidence one wins — this is already handled by the ranking formula.

**Low-confidence flagging in output:**

Low-confidence ECUs are retrieved with a prominent uncertainty flag:

- Confidence below `confidence_flag_threshold` (default: 0.3): output includes "⚠️ Low confidence — this cognition is uncertain. Treat as a hypothesis, not a conclusion."
- Confidence below `confidence_very_low_threshold` (default: 0.2): output includes "⚠️ Very low confidence — this cognition is highly uncertain. May be noise."

The agent sees both the confidence score and the flag, and can decide for itself whether to act on the cognition.

**Factor 3 — Activation boost (from spreading activation):** ECUs that are "warm" from recent retrieval in this session get a ranking boost. This creates a natural continuity effect — if you retrieved an ECU about the auth system 3 prompts ago, and the current prompt is also about auth, the related ECUs are already warm and surface more easily. Neuroscience parallel: priming in cognitive psychology. (See Section 7.)

**Factor 4 — Network richness (EC's differentiator):** An ECU that has many edges (supporting evidence, contradictions, dependencies) is a "hub" — it connects to a rich understanding. An isolated ECU with no edges is a bare assertion. Hub ECUs get a small ranking boost because they bring more context with them (via depth-1 traversal).

```
network_richness = min(edge_count, edge_count_cap) / edge_count_cap
```

**Session vs Canonical trust weight:**

```python
if source == session_brain:
    trust_weight = 0.8   # unreviewed, slightly less trusted
elif source == canonical_brain:
    trust_weight = 1.0   # human-reviewed, full trust

rank_score *= trust_weight
```

This is simpler than v1's layer weights and reflects the reality that Session Brain ECUs are useful but unverified.

**Cognitive grouping — the key innovation:**

v1 filled the token budget greedily: highest-ranked ECU first, then next, then next, until full. v2 retrieves in **cognitive clusters**, not individual ECUs. When an ECU is selected for retrieval, its depth-1 neighbourhood comes with it as a group. The group is ranked as a unit, not as individual ECUs. This ensures:

- You never retrieve a conclusion without its supporting evidence (unless budget is extremely tight)
- You never retrieve a belief without its contradictions (the agent always sees both sides)
- You never retrieve a dependency without its prerequisites

**Budgeting with groups:**

1. Compute rank scores for all candidate ECUs.
2. Sort by rank score, descending.
3. For each ECU, expand it into a cognitive group (the ECU + its depth-1 neighbourhood).
4. Compute the group's token cost.
5. If the group fits in the remaining budget, include it.
6. If the group doesn't fit, check: can we include just the core ECU (without its neighbourhood)? If yes, include the core ECU with a "context truncated" flag.
7. If even the core ECU doesn't fit, stop.

The atomic unit of retrieval is a cognitive group (conclusion + evidence + conflicts), not an individual ECU. The agent always gets a complete understanding of at least some things, rather than a shallow understanding of many things.

**Deduplication — two rules:**

When building and selecting cognitive groups, the same ECU can be reached via multiple edge directions (e.g., ECU_A is a core ECU in Group 1 but also a supporting-evidence neighbour of ECU_B in Group 2). Without deduplication, the same ECU's cognition text appears multiple times in the output — wasting tokens and confusing the agent. This was the most visible failure in Experiment 2, where groups contained 6 copies of the same 3 supporting ECUs.

**Rule 1 — Within-group deduplication:** When building a cognitive group, track which ECUs have already been added as supporting evidence, contradictions, or dependencies. If an ECU is reached via multiple edge directions (e.g., both as `supports` and `depends_on`), include it only once, in the highest-priority slot. Priority order: `contradictions` > `dependencies` > `supporting_evidence` > `superseded_by`. (Contradictions are highest priority because the agent most needs to know about conflicting evidence.)

```python
added_to_group = set()
for neighbour in neighbours:
    if neighbour.id in added_to_group:
        continue  # already included via another edge
    add_to_group(neighbour)
    added_to_group.add(neighbour.id)
```

**Rule 2 — Cross-group deduplication:** When an ECU is already included as a core ECU in one retrieved group, it should not appear as supporting evidence (or contradiction or dependency) in another group. Instead, it is marked as a reference: `"see Group N for full context"`. This prevents the same cognition from being repeated verbatim across multiple groups.

```python
core_ecus_included = set()
for group in selected_groups:
    core_ecus_included.add(group.core_ecu.id)

for group in selected_groups:
    for evidence in group.supporting_evidence:
        if evidence.id in core_ecus_included:
            replace with: "see Group {N} for full context"
    # same for contradictions, dependencies
```

This ensures the total token cost of retrieval reflects unique content, not repeated content. In Experiment 2, deduplication would have reduced Q2's output from 4491 tokens of largely-repeated content to ~2000 tokens of unique content — freeing budget for additional relevant ECUs.

**Fallback (kept from v1):**

If no ECU scores above the relevance threshold, don't return empty context. Return the top-K by rank score. Empty context is worse than weak context — the agent at least knows what EC has and can judge for itself.

**Retrieval restraint — the asymmetry principle:**

An under-injected agent can query for more via `query_ec`. An over-injected agent is anchored and doesn't know to un-ask. This asymmetry means the default token budgets are deliberately conservative (2000-6000 tokens depending on mode). EC errs on the side of injecting less, not more.

### 11.6 Anti-Anchoring Defences

The v1 mistake (cognition injected as authoritative context → agent anchored on past beliefs instead of repository truth) is addressed by three defences. The most important defence — not injecting cognition the agent didn't ask for — is structural: demand-driven retrieval (Section 6.3).

**Defence 1 — Demand-driven retrieval (structural):**

EC never injects cognition automatically. The agent must actively call `ec_query` to retrieve any cognition. This is the primary anti-anchoring defence — if the agent hasn't asked for it, it doesn't see it. The agent reads the repository first, forms its own understanding, and only queries EC when it recognizes a gap. This directly addresses EC-Bench Finding 1: "Engineering cognition should support reasoning once uncertainty emerges, not initialise it before repository understanding is established" [cite:f6100d2cb].

**Defence 2 — Framing:**

When ECUs are retrieved, they arrive with a clear label: "past engineering understanding — verify against current code before acting." The AGENTS.md says:

> "You have access to Engineering Cognition — past understanding from previous sessions. Always verify this understanding against the current codebase. The cognition includes confidence scores — calibrate your trust accordingly. This is not ground truth; it is accumulated belief that may be outdated."

This sets the epistemic stance: these are beliefs, not facts.

**Defence 3 — Grounding verification (Section 18):**

Every retrieved ECU includes `grounding` references (repo_path, file paths, symbols, commit hashes). The agent is instructed to verify these against the current codebase. If the grounding is stale (the code changed), the agent knows the cognition may no longer apply. This is the real backstop — the agent can independently confirm whether past understanding still holds.

**Defence 4 — Retrieval restraint:**

Conservative token budgets (2000-6000 tokens depending on mode). The asymmetry principle: under-querying is recoverable (agent can query for more), over-querying is not (agent is anchored and doesn't know to un-ask). Default to less, not more.

### 11.7 Token Budget

Configurable, like v1. Unlike v1's layer budgets (which allocated portions to different memory layers), v2 doesn't need layer budgets because there are only two brains and both participate in the same ranking.

```yaml
retrieval:
  max_tokens: 4000              # default token budget for proactive retrieval
  fallback_top_k: 5             # if nothing above threshold, return top-K anyway
  session_brain_trust_weight: 0.8
  canonical_brain_trust_weight: 1.0
  confidence_flag_threshold: 0.3    # below this, add "uncertain" flag to output
  confidence_very_low_threshold: 0.2 # below this, add "very uncertain" flag
```

### 11.8 Retrieval Result Format

Retrieved ECUs are structured for the agent to use efficiently:

```
=== Engineering Cognition ===

[Mode: implementation] [Budget: 3000 tokens] [3 cognitive groups retrieved]

--- Group 1 (rank: 0.82) ---
CONCLUSION: "This project uses connection pooling for all database access"
CONFIDENCE: 0.85 (canonical, reviewed)
SCOPE: project
GROUNDING: src/db/pool.ts, src/config/database.ts
SUPPORTING EVIDENCE:
  - "Connection pool of 20 was chosen because of concurrent request patterns" (confidence: 0.80)
DEPENDS ON:
  - "PostgreSQL driver is configured in src/config/database.ts" (confidence: 0.90)
NOTE: This is past engineering understanding. Verify against current code before acting.

--- Group 2 (rank: 0.71) ---
...

=== End Engineering Cognition ===
```

Each group includes: the core conclusion, confidence, scope, grounding references, supporting evidence, contradictions (if any), and dependencies. The framing note is included with every group.

### 11.9 Mode-Aware Retrieval

EC-Bench Finding 3 explicitly showed that different engineering modes required different patterns of understanding [cite:f6100d2cb]. No single retrieval strategy performed well across every phase.

**The five modes:**

| Mode | Description | What the agent is doing |
|---|---|---|
| `debugging` | Fixing a bug | Investigating root cause, testing hypotheses |
| `architecture` | Designing or analysing system structure | Making structural decisions, evaluating trade-offs |
| `implementation` | Writing code | Translating design into code |
| `investigation` | Exploring unfamiliar code | Building mental model of how things work |
| `planning` | Scoping future work | Estimating, sequencing, identifying risks |

**How mode is detected:**

Mode is detected from the agent's `ec_query` call. The agent can pass a `mode` parameter explicitly, or if omitted, EC classifies the query: "Classify this engineering query into one of: debugging, architecture, implementation, investigation, planning." This is a lightweight LLM call on the query text. (v2 can fall back to keyword heuristics if LLM call cost is a concern.)

**Important: mode controls WHAT comes back, not WHEN.**

In demand-driven retrieval (Section 6.3), the agent decides when to query. Mode affects the retrieval parameters — depth, budget, prioritization — but does not control timing. The previous design's "delayed two-pass" mechanism is removed. It was a workaround for proactive injection, which is no longer used.

**Mode-specific retrieval parameters:**

```
Mode: debugging
  - retrieval_depth: 0          # just the matches, no traversal — speed matters, agent is iterating fast
  - max_tokens: 2000            # small budget — debugging needs focus, not breadth
  - prioritize: contradictions  # edge-type slot: challenged ECUs and ECUs with contradicts edges — known pitfalls and failed approaches
  - bias: toward ECUs with provenance.source_type = "debugging"

Mode: architecture
  - retrieval_depth: 1          # full depth-1 traversal — the agent needs the complete picture
  - max_tokens: 6000            # large budget — architecture benefits from broad context
  - prioritize: decision, pattern, constraint     # architectural ECUs are most valuable
  - bias: toward ECUs with provenance.source_type = "architectural_reasoning"

Mode: implementation
  - retrieval_depth: 1           # need to know prerequisites and constraints
  - max_tokens: 3000             # moderate budget
  - prioritize: pattern, constraint
  - bias: toward ECUs with provenance.source_type = "implementation"

Mode: investigation
  - retrieval_depth: 1          # need to understand relationships and dependencies
  - max_tokens: 4000            # moderate-large budget — investigation is exploratory
  - prioritize: pattern, decision       # structural understanding via conclusion types
  - bias: none                  # exploration should not be steered by source type

Mode: planning
  - retrieval_depth: 1          # need to see dependencies and constraints
  - max_tokens: 5000            # large budget — planning needs comprehensive context
  - prioritize: constraint, decision
  - bias: toward ECUs with provenance.source_type = "planning"
```

> **Reconciliation note (Phase 13):** These parameters mirror the verified Experiment-2 reference implementation (D4/D11), which is what ships — see `ec/config.py::DEFAULT_CONFIG["modes"]`. `prioritize` slots resolve against `conclusion_type` values, plus one special edge-type slot (`contradictions` in debugging mode, resolved as "challenged or has a contradicts edge"). Earlier draft slots like `superseded_ecus` and `architecture` were dropped because they are not conclusion types (and superseded ECUs are non-retrievable by design), so they could never fire; biases resolve strictly against `provenance.source_type`.

**EC-Bench Finding 2 — mode matters for quality:**

EC-Bench showed that accumulated cognition was "Beneficial during planning and architecture; harmful during debugging and verification where the agent wandered into adjacent problems" [cite:f6100d2cb]. The mode-specific parameters above address this: debugging gets a small budget and prioritizes contradictions (known pitfalls), while architecture gets a large budget and prioritizes decisions and patterns.

**Mode-aware activation:**

Spreading activation also respects mode. In `debugging` mode, activation spreads more aggressively through `contradicts` edges (you want to surface related problems fast). In `architecture` mode, activation spreads more aggressively through `depends_on` edges (you want to understand the dependency chain).

### 11.10 Retrieval and Spreading Activation Interaction

When an ECU is retrieved, its network neighbours get an activation boost (see Section 12). This activation score feeds directly into the ranking formula for subsequent retrievals:

```
rank_score = (w_relevance × semantic_similarity)
           + (w_confidence × confidence)
           + (w_activation × activation_score)   ← normalized activation from previous retrievals (Section 7.5)
           + (w_network × network_richness)
```

The activation score here is the normalized value from Section 7.5 — bounded to [0, 1] by dividing by the maximum activation in the brain. This ensures the `w_activation × activation_score` term can never exceed `w_activation` (0.10), preventing activation from overwhelming the relevance signal across successive queries.

This creates a **cognitive continuity effect** across prompts within a session:

```
Prompt N: Agent retrieves ECU_A (about auth middleware)
  → Spreading activation: ECU_A's neighbours get activation boost
    → ECU_B (supports ECU_A, about JWT validation) gets +0.25
    → ECU_C (depends_on ECU_A, about session management) gets +0.25
    → ECU_D (contradicts ECU_A, about OAuth vs JWT) gets +0.25

Prompt N+1: Agent asks about session management
  → Semantic search finds ECU_C naturally (high similarity)
  → ECU_C also has activation_score = 0.25 (from prompt N's retrieval of ECU_A)
  → ECU_C's rank_score gets +w_activation × 0.25 boost
  → ECU_C ranks higher than it would on semantic similarity alone
  → Agent gets the session management ECU contextually related to the auth work from prompt N
```

Without activation, each prompt is retrieved in isolation. With activation, the retrieval system "remembers" what was recently relevant and biases toward related ECUs — exactly how human memory works (priming in cognitive psychology).

The activation score decays over time (Section 7.4), so this bias is strongest immediately after a retrieval and fades. If 30 minutes pass without related work, the activation scores have decayed to near-zero, and the ranking is purely semantic + confidence + network.

**Cross-brain activation:**

If a Canonical Brain ECU is retrieved and activates its neighbours, some of those neighbours might be in the Session Brain (via cross-brain lightweight edges). This is fine — the activation score is stored per-session (not per-brain), so it applies regardless of which brain the ECU lives in. A Session Brain ECU that's warm from activation will rank higher in the next retrieval, even though it's unreviewed.

### 11.11 Retrieval and Scope

**The scope hierarchy:**

```
engineering (top — general engineering principles)
  └── domain (e.g., "web backend", "distributed systems", "embedded")
       └── organization (e.g., "company conventions", "team standards")
            └── project (e.g., "my SaaS app")
                 └── repo (e.g., "api-server")
                      └── module (e.g., "auth module", "payment service")
                           └── subsystem (e.g., "token refresh logic", "rate limiter")
```

An ECU is scoped to one of these levels. "Always use parameterised queries" might be scoped to `engineering` (universal principle). "This project uses PostgreSQL with a connection pool of 20" is scoped to `project`. "The auth module uses JWT with RS256" is scoped to `module`.

**Retrieval searches UP the hierarchy, never down:**

When retrieving, EC searches from the current scope UP the hierarchy:

```
Search: subsystem (token refresh) → module (auth) → repo (api-server) → project (my SaaS app) → organization (company conventions) → domain (web backend) → engineering
```

It does NOT search sibling modules (e.g., the payment module) unless they're connected via edges (the agent might have an edge from an auth ECU to a payment ECU if they share a dependency, found via depth-1 traversal).

**Why up but not sideways:**

- **Up** = more general principles that apply to the current work. "Always validate input at trust boundaries" (engineering scope) applies to auth subsystem work. Always relevant.
- **Sideways** = different modules with different contexts. Auth module cognition isn't necessarily relevant to payment module work. If it IS relevant, there should be an edge connecting them, and the depth-1 traversal will find it.
- **Down** = more specific — doesn't make sense. You can't retrieve a module-scoped ECU when working at the project level because you don't know which module you'll be working in.

**Scope proximity weighting:**

Closer scopes get a ranking boost:

```
scope_proximity = 1.0    # same subsystem
scope_proximity = 0.85   # same module
scope_proximity = 0.7    # same repo
scope_proximity = 0.55   # same project
scope_proximity = 0.4    # same organization
scope_proximity = 0.25   # same domain
scope_proximity = 0.1    # engineering (universal)

rank_score *= scope_proximity
```

Scope proximity is a multiplier, not a filter. A high-relevance engineering-scope ECU can still outrank a low-relevance module-scope ECU. This ensures that universal principles ("always close database connections") surface even when working in a specific module, if they're semantically relevant.

**Stale parent-scope ECUs:**

An ECU scoped to the project level ("this project uses MongoDB") might be stale if the project migrated to PostgreSQL. This is handled by the grounding verification mechanism (Section 18): the ECU includes grounding references, and the agent is instructed to verify them. If the grounding is stale, the agent knows the cognition may no longer apply, and this can trigger reconsolidation (Section 13).

**Configurable:**

The scope hierarchy levels and proximity weights are all in the config block. A user who works across multiple repos in the same domain might want higher domain-scope proximity. A user who works on a single large monorepo might want higher module-scope proximity.

### 11.12 Brain Requirements (inherited from deferred design)

- The Brain must support efficient semantic search over ECUs (vector index)
- The Brain must support structural traversal (following edges)
- Retrieval must return ECUs with their confidence and status (so the agent can calibrate trust)
- Retrieval must return grounding references (so the agent can verify against current code)

### 11.13 Deferred to v3 — Implicit Uncertainty Detection

Monitor the agent's reasoning trace for uncertainty markers ("I'm not sure why...", "This is unexpected...", "Let me try...") and auto-trigger retrieval. This would improve on-demand retrieval timing — the agent gets cognition exactly when it realises it needs it, without having to explicitly call `query_ec`. Requires parsing reasoning traces and adds complexity. v2's dual-trigger (proactive + on-demand) is sufficient. (See Section 26 — Deferred Items.)

### 11.14 Future Research Directions — Mathematics and Physics

**Nonlinear retrieval ranking (v3+):**

The current ranking formula is a weighted linear combination. But human memory retrieval isn't linear — it exhibits nonlinear superposition. When two retrieval cues are present simultaneously (e.g., "auth" AND "debugging"), the relevance isn't the sum of individual relevances — it's often a multiplicative or even superadditive effect. You remember auth debugging experiences better when BOTH cues are present than when either is alone. Future consideration: the ranking could use a kernel function that captures the interaction between query dimensions, similar to how kernel methods in SVMs capture nonlinear feature interactions. (See Section 26 — Deferred Items.)

**Thermodynamic activation decay (v2.5/v3):**

Instead of exponential decay (which is what we have), consider modelling activation as a cooling process where activation "temperature" depends on the density of recently-activated ECUs. If many ECUs in a region are warm (high activation), the region itself stays warm longer (thermal mass). If only one ECU is warm, it cools faster. This would create a "cognitive heat map" where active regions of the brain stay primed longer than isolated ECUs. Exponential decay is sufficient for v2. (See Section 26 — Deferred Items.)

---

## 9. Spreading Activation (Warming Up)

### 12.1 Mechanism

When ECUs are retrieved, their direct network neighbors (via any edge type) get a transient `activation` score boost. This score decays over time within the session and influences retrieval ranking for subsequent queries.

### 12.2 Session-Specific

Activation boosts are **session-specific**. Each new session starts with a clean activation slate. This prevents cross-session contamination — a new session doesn't inherit activation biases from a previous session. When returning to an existing session, the activation state is restored.

### 12.3 Activation Scope

How many hops away should activation spread? This is a parameter to be experimented with:
- 1 hop: only direct neighbors of retrieved ECUs
- 2 hops: neighbors of neighbors (default starting point)
- 3 hops: broader network

Default: 2-3 hops. Exact value TBD through experimentation.

### 12.4 Implementation

```
activation_score[ECU] = base_boost × decay_factor ^ hop_distance
```

Where:
- `base_boost` = configurable (default: 0.5)
- `decay_factor` = configurable (default: 0.5 per hop)
- `hop_distance` = number of edges from the retrieved ECU

Activation scores are ephemeral — stored in session memory, not in the Brain. They decay over time within the session:

```
activation_score[ECU] *= exp(-activation_decay_rate × elapsed_time)
```

### 12.5 Normalization

Before activation scores are used in the ranking formula, they are normalized to the range [0, 1] by dividing every ECU's activation score by the maximum activation score in the brain:

```
normalized_activation[ECU] = activation_score[ECU] / max(activation_scores)
```

This ensures:
1. **Bounded contribution:** The `w_activation × activation_score` term in the ranking formula can never exceed `w_activation` (0.10), so activation can never dominate the ranking. This prevents the failure mode observed in Experiment 2, where unnormalized activation scores grew to 5-410 across successive queries and completely overwhelmed the relevance signal.
2. **Relative ordering preserved:** Normalization by max preserves the relative ordering of activation scores — an ECU with twice the raw activation of another still has twice the normalized activation. What changes is the absolute scale: scores are relative to the "hottest" ECU in the brain, not unbounded.
3. **Cross-query stability:** Whether 1 ECU or 50 ECUs have been retrieved, the normalized activation contribution to the ranking formula stays in [0, w_activation]. This makes the ranking formula's behaviour predictable across sessions of different lengths.

If all activation scores are zero (no prior retrieval in this session), normalization is skipped and all activation scores remain 0.0 (their default).

---

## 10. Edges — Complete Specification

### 14.1 Edge Types (v1 — four types)

| Edge Type | Direction | Semantics | Effect on Target Confidence |
|---|---|---|---|
| `supersedes` | A → B (A supersedes B) | A replaces B as the current belief on this topic | B's confidence is frozen; B's status becomes `superseded` |
| `supports` | A → B (A supports B) | A provides evidence that strengthens B's conclusion | B's confidence increases (Bayesian update) |
| `contradicts` | A → B (A contradicts B) | A provides evidence that weakens B's conclusion | B's confidence decreases (Bayesian update); B may become `challenged` |
| `depends_on` | A → B (A depends on B) | B must be valid for A to be meaningful | No direct confidence effect; if B is deprecated/superseded, A is flagged for re-evaluation |

### 14.2 Edge Structure

```json
{
  "type": "supersedes | supports | contradicts | depends_on",
  "target_id": "ECU uuid",
  "weight": 0.85,
  "confidence_delta": 0.12,
  "created_at": "ISO 8601 timestamp",
  "supersession_type": "cosmetic | semantic"
}
```

- `weight`: The strength of the relationship (0-1). For `supports`/`contradicts`, this is derived from semantic similarity and evidence strength.
- `confidence_delta`: The amount by which this edge changed the target ECU's confidence (in log-odds space). Stored for reversible updates — when an edge is pruned, this delta is subtracted to reverse the effect.
- `supersession_type`: Only present when `type = supersedes`. `cosmetic` means the new ECU is a wording refinement of the old one (same meaning, better phrasing). `semantic` means the new ECU has a different meaning that replaces the old one.

### 14.3 Multiple Edges Between Two ECUs

Two ECUs can have multiple edges of **different** types. For example, ECU_A can both `support` and `depend_on` ECU_B — these are fundamentally different relationships.

For the **same** edge type, there should be one edge per pair, with a weight that accumulates from multiple evidence events. If ECU_A provides multiple pieces of supporting evidence for ECU_B, the single `supports` edge's weight increases rather than creating multiple `supports` edges.

Exception: if ECU_A both `supports` and `contradicts` ECU_B, this is a signal that the relationship needs decomposition into more atomic ECUs. The Diffuser should flag this for human review.

### 14.4 Edge Creation

Edges are created by:
- **Diffuser (Canonical Brain):** Full edge creation with Bayesian weight computation. This is the primary edge creation path.
- **Lightweight Diffuser (Session Brain):** Simple similarity-based edge creation for immediate session use. These edges are ephemeral — they exist only in the Session Brain.

### 14.5 Edge Pruning

Edges are pruned by the Maintainer when:
- The target ECU is `deprecated` or `superseded` (and the edge is no longer meaningful)
- The edge's weight has decayed below a threshold
- The Maintainer detects the edge is stale (the relationship no longer reflects current understanding)

When an edge is pruned:
- If it was a `supports` edge: subtract `confidence_delta` from the target's confidence (reverse the original update)
- If it was a `contradicts` edge: add `confidence_delta` back to the target's confidence (reverse the original update)
- If it was a `depends_on` edge: no confidence change; just remove the structural link
- If it was a `supersedes` edge: this edge is NEVER pruned (it's part of the audit trail)

---

## 11. Confidence Mathematics

### 15.1 Representation

Confidence is stored as a scalar float in [0, 1]. All Bayesian updates are performed in log-odds space for numerical stability, then converted back to [0, 1] for storage.

**Conversion:**
- Log-odds: `L = log(c / (1 - c))`
- Probability: `c = 1 / (1 + exp(-L))` (sigmoid function)

### 15.2 Support Update (when a `supports` edge is created)

When ECU_A supports ECU_B:

```
L_B' = L_B + w_support × r(A, B) × c_A
```

Where:
- `L_B` = current log-odds confidence of ECU_B
- `c_A` = confidence of ECU_A (stronger supporters provide stronger evidence)
- `r(A, B)` = relevance of A to B (semantic similarity, 0-1)
- `w_support` = weight parameter for support edges (configurable, default: 1.0)

The `confidence_delta` stored on the edge is: `w_support × r(A, B) × c_A`

### 15.3 Contradiction Update (when a `contradicts` edge is created)

When ECU_A contradicts ECU_B:

```
L_B' = L_B - w_contradict × r(A, B) × c_A
```

Same structure, negative direction. The `confidence_delta` stored on the edge is: `-w_contradict × r(A, B) × c_A`

### 15.4 Supersession

When ECU_new supersedes ECU_old:

1. Create `supersedes` edge (ECU_new → ECU_old)
2. Set ECU_old.status = `superseded`
3. Freeze ECU_old's confidence at its current value (for audit trail)
4. ECU_new's confidence is based on **its own evidence**, NOT inherited from ECU_old

Supersession is not a confidence transfer. The new ECU earns its own confidence from its supporting evidence.

### 15.5 Supersession Trigger

Supersession triggers when BOTH conditions hold:
1. ECU_old's confidence < `θ_supersede` (default: 0.3)
2. A new ECU exists that better explains the evidence (higher confidence, higher semantic relevance)

You don't supersede a belief just because it's weakened; you need a replacement.

### 15.6 Edge Pruning Reversal

When an edge is pruned:

```
L_B' = L_B - edge.confidence_delta
```

This reverses the original confidence change. Requires that `confidence_delta` was stored when the edge was created.

### 15.7 Propagation

If ECU_B's confidence changed, check ECUs that `depend_on` ECU_B:
- If ECU_B's confidence < `θ_dep_reevaluate` (default: 0.4), mark dependent ECUs as `challenged`
- This propagation is transitive but bounded — limit propagation depth to `max_propagation_depth` (default: 2)

### 15.8 Bayes Factor Calibration — What Makes Evidence Strong vs Weak

The effective evidence delivered by an edge is: `w × r(A, B) × c_A`. The weight `w` is constant — the differentiation between strong and weak evidence comes from the other two factors plus corroboration.

**Symmetric weights for v2:**

`w_support` and `w_contradict` are both 1.0. This is a deliberate choice:

In engineering, a single contradiction usually means "different context" (orthogonal truth), not "your belief is wrong." Our contradiction handling already has three cases (Section 12.1): genuine contradiction, orthogonal, and competing hypotheses. The Diffuser classifies which case it is before applying the update. If it's orthogonal, no `contradicts` edge is created. So by the time a `contradicts` edge IS created, it's a genuine contradiction — and genuine contradictions should have the same weight as genuine support.

The Popperian asymmetry (falsification is stronger than confirmation) is real, but it's already baked in structurally: the supersession mechanism requires both low confidence AND a replacement. One contradiction won't demolish a well-supported belief — it just marks it `challenged` and starts accumulating. Multiple contradictions compound in log-odds space, which is multiplicative in probability space — they accumulate naturally.

**What makes evidence strong vs weak:**

| Factor | Strong Evidence | Weak Evidence |
|---|---|---|
| Relevance (r) | 0.85+ (directly related) | 0.30-0.50 (tangentially related) |
| Evidence confidence (c_A) | 0.75+ (debugging, implementation) | 0.35-0.50 (planning, observation) |
| Corroboration | Multiple independent ECUs support same conclusion | Single ECU, no corroboration |
| Edge weight (w) | 1.0 (constant) | 1.0 (same — other factors differentiate) |

Strong evidence = highly relevant × high-confidence evidence ECU × multiple corroborators. Weak evidence = tangentially relevant × low-confidence evidence ECU × single source. The weight `w` stays constant — differentiation comes from relevance and evidence quality.

**Concrete examples:**

With `w = 1.0`, evidence ECU at confidence 0.80, relevance 0.90:
- Evidence = 1.0 × 0.90 × 0.80 = 0.72
- Target at 0.50 (log-odds = 0): new log-odds = 0.72, new confidence = 0.673
- One strong piece of evidence moves 0.50 → 0.67. Reasonable.

With weak evidence — confidence 0.40, relevance 0.50:
- Evidence = 1.0 × 0.50 × 0.40 = 0.20
- Target at 0.50: new log-odds = 0.20, new confidence = 0.550
- Barely moves. Correct — weak evidence shouldn't shift beliefs much.

**Relevance threshold for edge creation:**

ECUs below the relevance threshold are classified as `unrelated` and don't create edges. This prevents noise from creating spurious edges.

```yaml
diffuser:
  relevance_threshold: 0.6   # minimum semantic similarity for edge creation in full Diffuser
```

This is slightly lower than the lightweight diffuser's 0.7 because the full Diffuser does richer analysis (structural proximity, not just semantic similarity) and can afford to consider slightly weaker matches.

---

## 12. Contradiction Handling

### 17.1 Three Cases of Contradiction

**Case 1 — Genuine Contradiction (same scope, same context, mutually exclusive):**
Example: ECU_A: "Auth uses optimistic refresh" vs ECU_B: "Auth uses pessimistic refresh" — both scoped to the same repo.

This is a real contradiction. The Diffuser detects it, marks both as `challenged`, and lets evidence accumulate. Resolution happens when one side's confidence drops below `θ_supersede` AND a superseding ECU exists. If both ECUs have high confidence (> 0.7), the contradiction is flagged to the user as a notification: "Two of your engineering beliefs about the auth system contradict each other." The agent handles the detection automatically to make the user's life easier.

**Case 2 — Orthogonal (different scope or context, both true):**
Example: ECU_A: "Optimistic refresh is good for low-traffic services" vs ECU_B: "Pessimistic refresh is better for high-traffic services."

These aren't contradictory — they're context-dependent truths. Store both. Each has a scope/condition that disambiguates when it applies. The Diffuser should recognise this as orthogonal, not contradictory, and not create a `contradicts` edge.

**Case 3 — Competing Hypotheses (both plausible, evidence insufficient):**
Example: ECU_A: "The race condition is in TokenManager" vs ECU_B: "The race condition is in CacheInvalidator" — both hypotheses during an active debugging investigation.

Store both, both with moderate confidence, both `challenged` status. The brain holds competing hypotheses until evidence resolves them. When evidence resolves the investigation, the winner gets reinforced (new `supports` evidence) and the loser gets superseded.

### 17.2 Contradiction Classification — Two-Stage Process

The distinction between genuine contradiction (Case 1) and orthogonal truths (Case 2) is fundamentally an engineering judgment, not a pure computation. But it's not purely semantic either — there are computable signals that can pre-filter most cases, leaving only the genuinely ambiguous ones for LLM judgment.

**Stage 1 — Computable pre-filter (fast, no LLM call):**

Before asking the LLM anything, check three signals:

1. **Scope comparison.** If two ECUs are at different scopes, they're likely orthogonal, not contradictory. An engineering-scope ECU ("always validate input at trust boundaries") vs a module-scope ECU ("the auth module skips input validation for internal calls") aren't contradictory — one is a principle, the other is an observation about a specific (possibly non-compliant) implementation. Different scope = likely Case 2 (orthogonal) or not a contradiction at all.

2. **Grounding overlap.** If two ECUs reference the same files/symbols in their grounding, they're more likely to be about the same thing — higher probability of genuine contradiction. If they reference completely different files, they might be about different aspects of the system that happen to be semantically similar. No grounding overlap = lower probability of genuine contradiction.

3. **Condition/qualifier extraction.** The Extractor's `cognition` field often contains implicit conditions. "Optimistic refresh is good for low-traffic services" has the condition "for low-traffic services." If both ECUs have extractable conditions and those conditions are different, they're orthogonal. This can be done with a simple regex or lightweight NLP pass — "for X", "when Y", "in Z context", "under W conditions."

If Stage 1 determines the ECUs are at different scopes with different conditions and no grounding overlap → classify as `unrelated` or `orthogonal`. No LLM call needed. This handles approximately 60-70% of cases.

**Stage 2 — LLM semantic judgment (for cases that pass the pre-filter as potentially contradictory):**

If Stage 1 says "these might be contradictory" (same scope, overlapping grounding, same or no conditions), present both ECUs to the LLM:

```
ECU_A: "Auth uses optimistic refresh"
  Scope: repo (api-server)
  Grounding: auth/token_manager.rs, auth/refresh.rs
  Confidence: 0.75

ECU_B: "Auth uses pessimistic refresh"
  Scope: repo (api-server)
  Grounding: auth/token_manager.rs, auth/refresh.rs
  Confidence: 0.65

Question: Are these genuinely contradictory (same context, mutually exclusive),
or orthogonal (both true in different contexts/conditions)?

Respond with: "genuine_contradiction", "orthogonal", or "unrelated".
If "orthogonal", explain what context/condition differentiates them.
```

This is a single LLM call, the same pattern as the lightweight diffuser's relationship classification. The LLM has engineering context that pure computation can't replicate: it knows that "uses JWT" and "uses OAuth" aren't necessarily contradictory (JWT for tokens, OAuth for authorization flow), but "uses optimistic refresh" and "uses pessimistic refresh" for the same operation ARE contradictory.

**Why both stages are necessary:**

Stage 1 alone would miss engineering nuance. "The auth module uses JWT" vs "The auth module uses OAuth" — same scope, same grounding, no explicit conditions. Stage 1 would flag these as potentially contradictory. Stage 2 correctly identifies them as orthogonal (different aspects of auth).

Stage 2 alone would be too expensive — running an LLM call for every semantically-similar ECU pair would be slow and costly. Stage 1 filters out the obvious non-contradictions first.

**Case 3 identification:**

Case 3 is the easiest to identify. If both ECUs are:
- Same scope
- Same grounding (or overlapping)
- Both have `provenance.source_type = "debugging"` or `"investigation"`
- Both have moderate confidence (0.30-0.60 range)
- Both were created in the same session or within a short time window

→ They're likely competing hypotheses from an active investigation. The Diffuser classifies them as Case 3, stores both with `challenged` status, and waits for evidence. The key signal: competing hypotheses come from investigation contexts. If two ECUs about the same topic were both extracted from debugging sessions and both have moderate confidence, they're competing hypotheses, not genuine contradictions.

### 17.3 The Rule

The Diffuser doesn't automatically resolve contradictions. It detects them, classifies them (via the two-stage process above), marks both as `challenged`, and lets evidence accumulate. Resolution happens when one side's confidence drops below the supersession threshold. This mirrors how scientists hold competing hypotheses — you don't pick a side prematurely.

### 17.4 When to Flag Contradictions to the User vs Handle Automatically

The principle: **flag when the resolution has consequences the user should know about. Handle automatically when the system can resolve it through normal mechanisms.**

**Handle automatically (no user notification):**

1. **When one side has low confidence (< θ_contradiction_flag, default: 0.7).** The system is already handling this — the low-confidence ECU will decay further and eventually be superseded. The user doesn't need to know that a weak belief was contradicted. This is the normal Bayesian resolution path.

2. **When the contradiction is Case 3 (competing hypotheses) during an active investigation.** Competing hypotheses are expected during debugging. They'll resolve when evidence arrives. Flagging them mid-investigation would be noise — the user is actively working on the problem and doesn't need EC to tell them "you have two hypotheses."

3. **When the contradiction is between a session-brain ECU and a canonical-brain ECU.** The Pending Update mechanism (Section [RECONSOLIDATION - DEFERRED]5) already handles this. The user will see it at review time. No separate notification needed.

4. **When the contradiction is Case 2 (orthogonal).** These aren't real contradictions — they're both true in different contexts. The Diffuser classifies them as orthogonal, stores both, and moves on. No flag needed.

**Flag to the user:**

1. **When both ECUs have high confidence (> 0.7).** This means the system has strong evidence for BOTH sides. This is a genuine conflict that the system can't resolve through confidence decay alone — both sides are well-supported. The user needs to know: "Two of your engineering beliefs about the auth system contradict each other. Both are well-supported. You should investigate."

2. **When the contradiction is at engineering or domain scope.** A contradiction at the engineering level ("always validate input at trust boundaries" vs "input validation is unnecessary for internal services") is a fundamental conflict in the user's engineering philosophy. These are rare but significant. Always flag.

3. **When a `depends_on` chain is affected.** If ECU_A depends on ECU_B, and ECU_B is contradicted, ECU_A is structurally at risk. The user should know: "ECU_A depends on ECU_B, which has been challenged. ECU_A may need re-evaluation."

4. **When competing hypotheses (Case 3) have persisted beyond their persistence limit (see Section 12.5).** If evidence hasn't resolved them within the time limit, the user should be notified.

**The notification mechanism:**

- **Not blocking.** The agent continues to work. Notifications are for the human, not the agent.
- **Accumulated at review gate.** Contradictions are surfaced during the Human Review Gate, grouped by topic: "During recent sessions, EC detected 2 contradictions worth your attention."
- **Retrieval flagging.** When a `challenged` ECU is retrieved during a session, it comes with a flag: "⚠️ This cognition is challenged — there is contradicting evidence. See: [ECU_X]." The agent knows to be cautious but isn't blocked.

### 17.5 Competing Hypothesis Persistence Limits

The existing rule says "the brain holds competing hypotheses until evidence resolves them." But what if evidence NEVER arrives?

**Normal decay handles most cases.**

Competing hypotheses are in `challenged` status with moderate confidence (0.30-0.60). If nobody retrieves or reinforces them, normal scope-dependent decay will lower their confidence over time. A module-scope competing hypothesis with no reinforcement for 30 days will have decayed from 0.50 to `0.50 × exp(-0.02 × 30) = 0.27` — approaching the supersession threshold. Eventually it'll cross `θ_supersede` and be deprecated naturally.

So the decay mechanism already handles "forgotten investigations." The real question is: what about hypotheses that KEEP getting retrieved (retrieval bumps reset the decay clock) but never get resolved? That's the signal worth flagging.

**Scope-dependent persistence limits:**

Competing hypotheses get a `competing_since` timestamp (set when the Case 3 relationship is first detected). The persistence limit depends on scope:

| Scope | Persistence limit | Rationale |
|---|---|---|
| engineering | Indefinite | Fundamental principle debates are rare but important. Let them persist. |
| domain | Indefinite | Similar — domain-level debates are long-running by nature. |
| organization | 75 days | Org-level hypotheses should resolve through convention changes within ~2.5 months. |
| project | 90 days | If a project-level hypothesis hasn't resolved in 3 months, the project has likely moved on. |
| repo | 60 days | Repo-specific hypotheses should resolve through code changes within 2 months. |
| module | 30 days | Module-specific hypotheses should resolve quickly through implementation. |
| subsystem | 20 days | Subsystem-level hypotheses should resolve within ~3 weeks through active development. |

**What happens when the persistence limit is reached:**

1. Both ECUs get status `open_question` — a new status. This means: "this is an unresolved engineering question that EC has been tracking. It's not deprecated (it might still be true), but it's not actively being investigated either."

2. The user is notified at the next review gate: "ECU_A and ECU_B have been competing for [duration] without resolution. Options: (a) investigate further, (b) mark one as preferred, (c) mark both as resolved (both true in different contexts — reclassify as orthogonal), or (d) archive as unresolved."

3. The user's options:
   - **(a) Investigate:** Reset the `competing_since` timestamp. The hypotheses persist as `challenged`. The user is essentially saying "I'll look into this."
   - **(b) Mark one preferred:** The preferred ECU stays `active` with a confidence bump. The other gets superseded (`status = superseded`, `superseded_by` = preferred ECU). This is the user resolving the competition through judgment, not evidence.
   - **(c) Reclassify as orthogonal:** Both stay `active`. The Diffuser removes the `contradicts` edge and notes: "User determined these are context-dependent truths, not contradictions." The conditions that differentiate them are noted in each ECU's metadata.
   - **(d) Archive:** Both get `status = archived`. Confidence frozen. Not returned by retrieval unless explicitly queried via `query_ec`. The user is saying "this doesn't matter anymore."

4. If the user ignores the notification: the hypotheses stay as `open_question`. Retrieval returns them with a flag: "⚠️ Open question — unresolved since [date]." They don't block anything. They don't decay further (`open_question` status freezes confidence — it's a parked state, not a decaying state).

**Why `open_question` freezes confidence instead of continuing to decay:**

If the hypotheses kept decaying, they'd eventually be deprecated — and the user would lose the knowledge that there was an unresolved question. Freezing at `open_question` preserves the knowledge: "we don't know which of these is true, and we're not actively investigating, but the question exists." This is more valuable than silently forgetting.

**Retrieval behavior for `open_question` ECUs:**

Returned by retrieval but with lower ranking weight (they're uncertain). Flagged in the output: "⚠️ Open question — unresolved since [date]." The agent can use these as "things to consider but not rely on" — exactly how a human engineer treats an unresolved question in the back of their mind.

### 17.6 Biological Basis

Cognitive dissonance theory (Festinger): humans hold contradictory beliefs simultaneously. The brain tolerates them, and resolution happens slowly through consolidation (especially during sleep, per Walker's research on sleep-dependent memory processing). EC mirrors this: contradictions are tolerated, tracked, and resolved through evidence accumulation, not immediate forced resolution.

The `open_question` status mirrors the Zeigarnik effect — uncompleted tasks (or unresolved questions) are remembered better than completed ones. The brain doesn't forget open questions; it parks them and brings them up when relevant. EC's `open_question` is the brain's way of saying "I haven't forgotten about this, but I'm not actively working on it."

### 17.7 Physics Parallel — Quantum Decoherence

Competing hypotheses are like a quantum superposition — both states exist simultaneously until observation (evidence) collapses the wavefunction. The persistence limit is like decoherence: if no observation happens for long enough, the superposition doesn't collapse — it just fades. But instead of disappearing, it gets "parked" as a known unknown, which is more honest than pretending it resolved.

---

## 13. ECU Immutability Rules

### 20.1 The Rule

The `cognition` field (the conclusion statement) is **semantically immutable**. Any change to the meaning of the conclusion requires creating a new ECU with a `supersedes` edge.

### 20.2 What Is Immutable

- `id` — never changes
- `cognition` — never changes (semantically). Even cosmetic wording changes require a new ECU with `supersession_type: cosmetic`.
- `provenance` — never changes (it records where the ECU came from)
- `scope` — never changes (if scope changes, that's a different ECU)

### 20.3 What Is Mutable

- `grounding` — can update file paths if code was moved (refactoring shouldn't deprecate a valid conclusion). `commit_hash` and `code_snapshot` remain as historical references.
- `confidence` — updates via Bayesian updating (this is the whole point)
- `status` — transitions: active → challenged → superseded, or active → deprecated, or active → challenged → open_question → (active | superseded | archived)
- `evidence` — can add new supporting evidence references (this is reinforcement, not a change to the conclusion)
- `edges` — added/removed by the Diffuser/Maintainer
- `metadata` — `last_reinforced`, `last_challenged`, `last_retrieved`, `retrieval_count`, `cluster_memberships` update constantly

### 20.4 Why Even Cosmetic Changes Require a New ECU

Editing the `cognition` field in place breaks the audit trail — you can't tell whether a wording change also changed the meaning. So even cosmetic changes create a new ECU with a `supersedes` edge and `supersession_type: cosmetic`. This is slightly more overhead but preserves integrity.

---

## 14. Deprecation and Scope Rules

### 21.1 When to Deprecate

An ECU is deprecated when it is no longer relevant to the current state of the system. Deprecation is scope-dependent:

| Scope | Deprecation Trigger |
|---|---|
| `engineering` | Never deprecated by code changes. Only superseded by a better principle. |
| `domain` | Rarely deprecated. Only if the domain itself shifts. |
| `organization` | Deprecated if org conventions change. |
| `project` | Deprecated if the project's architecture changes. |
| `repo` | Deprecated if referenced code is deleted or significantly changed. |
| `module` | Deprecated if the module is removed or significantly refactored. |
| `subsystem` | Deprecated if the subsystem is removed or significantly refactored. |

### 21.2 Deprecation vs Supersession

- **Supersession:** An old ECU is replaced by a new ECU that better explains the evidence. The old ECU's status becomes `superseded`. The new ECU is the current belief.
- **Deprecation:** An ECU is no longer relevant (code deleted, scope removed). The ECU's status becomes `deprecated`. There may not be a replacement — the knowledge is simply no longer applicable.

### 21.3 What Happens to Deprecated ECUs

- NOT returned by retrieval
- Confidence is frozen for audit
- Edges are preserved for audit trail
- `supersedes` edges pointing TO deprecated ECUs are preserved (audit trail)
- `depends_on` edges pointing TO deprecated ECUs trigger re-evaluation of the depending ECU

### 21.4 Distinction: Engineering Conclusion vs Engineering Observation

An engineering conclusion (global scope) should survive code deletion — it's a principle, not a fact about specific code. An engineering observation (repo scope) should be deprecated when its grounding disappears — it was about specific code that no longer exists.

The `scope` field determines this behaviour. The v0.4 definition's "Scoped" property (Property 7) handles this: the scope determines how grounding-sensitive the ECU is.

---

## 15. Configuration Parameters

All configurable constants should live in a configuration file. Below are the parameters and their default values. All are subject to experimentation in EC-Bench v2.

```yaml
# ECU Configuration
ecu:
  id_format: "uuid4"
  
# Confidence Configuration
confidence:
  # Two-dimensional priors: base_prior (by source_type) × scope_modifier
  base_priors:
    debugging: 0.70
    implementation: 0.65
    code_review: 0.60
    architectural_reasoning: 0.50
    observation: 0.45
    planning: 0.35
  
  scope_multipliers:
    engineering: 1.15          # universal principles, rarely wrong (capped at 0.95)
    domain: 1.10              # stable domain knowledge (capped at 0.95)
    organization: 1.05        # org conventions, fairly stable (capped at 0.95)
    project: 1.00             # neutral baseline
    repo: 0.90                # repo-specific, may be refactored
    module: 0.80              # most specific, most volatile
    subsystem: 0.75           # most granular, most volatile
  
  prior_cap: 0.95             # maximum starting confidence
  prior_floor: 0.05           # minimum starting confidence
  
  # Multi-source corroboration bump
  corroboration_bump: 0.05    # per independent source
  corroboration_bump_cap: 0.15  # max total bump (3 sources)
  
  # Bayesian update weights (symmetric — see Section 11.8)
  w_support: 1.0              # weight for support edges
  w_contradict: 1.0           # weight for contradict edges (same as support)
  
  # Thresholds
  theta_supersede: 0.3          # confidence below which supersession can trigger
  theta_dep_reevaluate: 0.4     # confidence below which dependents are re-evaluated
  theta_contradiction_flag: 0.7 # confidence above which contradictions are flagged to user
  
  # Scope-dependent decay rates (per day) — half-life = ln(2)/λ
  lambda_decay:                # scope-specific decay rates (log-odds space)
    engineering: 0.001         # very slow decay
    domain: 0.003              # slow decay
    organization: 0.004        # moderate-slow decay
    project: 0.005             # moderate decay
    repo: 0.01                 # faster decay
    module: 0.02               # fast decay
    subsystem: 0.03            # fastest decay
  
  alpha_retrieval: 0.05         # retrieval reinforcement bump strength
  
  # Propagation
  max_propagation_depth: 2      # max hops for transitive confidence propagation

# Retrieval Ranking
ranking:
  w_relevance: 0.6              # primary signal — relevance is the gate
  w_confidence: 0.15            # secondary modifier — ranks but doesn't gate
  w_activation: 0.10            # tertiary — session-specific priming (normalized [0,1])
  w_network: 0.15              # tertiary — structural hub boost
  relevance_gate_threshold: 0.3  # minimum semantic similarity to participate in ranking
  edge_count_cap: 10           # for network_richness = min(edge_count, cap) / cap

# Spreading Activation
activation:
  base_boost: 0.5               # initial activation boost for retrieved ECU
  decay_factor: 0.5             # per-hop decay
  max_hops: 2                   # default: 2-3, TBD through experimentation
  activation_decay_rate: 0.1     # per-hour decay of activation scores within session
  normalization: "divide_by_max" # normalize all activation scores by the max in the brain -> [0, 1]

# Diffuser
diffuser:
  relevance_threshold: 0.6     # minimum semantic similarity for edge creation in full Diffuser

# Lightweight Diffusion (Session Brain)
lightweight_diffusion:
  alpha_light: 0.1              # simple confidence adjustment strength
  similarity_threshold: 0.7     # minimum similarity for edge creation

# LLM Configuration
#
# EC needs an LLM for extraction, relationship classification (lightweight diffuser),
# and mode detection. These are classification/extraction tasks that require strong
# instruction following and engineering comprehension.
#
# Default: Claude Haiku 4.5 via OpenCode Zen API. Tested against qwen2.5:7B,
# qwen2.5-coder:14B (local via Ollama), gpt-5.4-mini (Zen), and claude-haiku-4-5
# (Zen). Haiku 4.5 produced the highest quality extraction: caught 4x more genuine
# conclusions than the 7B, valid format compliance, working Self-Review Pass, and
# caught deep invariants/trade-offs that other models missed.
#
# The Zen API uses different endpoints per model family:
#   - Claude models → https://opencode.ai/zen/v1/messages (Anthropic Messages API)
#   - GPT models    → https://opencode.ai/zen/v1/responses (OpenAI Responses API)
#   - Others        → https://opencode.ai/zen/v1/chat/completions (OpenAI Chat)
#
# Ollama is supported as a fallback for offline use (lower quality extraction).
llm:
  provider: "opencode_zen"               # "opencode_zen" | "ollama" | "openai" | "anthropic"
  model: "claude-haiku-4-5"             # best extraction quality per cost (tested)
  base_url: "https://opencode.ai/zen/v1"
  api_key_env: "OPENCODE_ZEN_API_KEY"   # env var name for API key
  timeout: 60                           # seconds per LLM call
  temperature: 0.3                      # low temp for consistent extraction
  # Fallback for offline use (lower quality — see extractor test results):
  # provider: "ollama"
  # model: "qwen2.5-coder:14b"
  # base_url: "http://localhost:11434"

# JSON Post-Processing
#
# Claude models wrap JSON output in markdown code fences (```json ... ```).
# The MCP server must strip these before parsing. This is a known behavior
# and is handled by a post-processing step in the Extractor:
#
#   def extract_json(text):
#       if "```" in text:
#           start = text.find("{")
#           end = text.rfind("}") + 1
#           if start != -1 and end > start:
#               return text[start:end]
#       return text.strip()
#
# This is a 5-line fix. The JSON content itself is always valid.

# Embedding Model
embedding:
  model: "all-MiniLM-L6-v2"     # default: 384-dim, ~5ms, ~90MB
  # model: "BAAI/bge-base-en-v1.5"  # better quality: 768-dim, ~15-25ms, ~420MB
  dimensions: 384                # must match model

# Clustering
clustering:
  algorithm: "hdbscan"
  min_cluster_size: 3           # minimum ECUs to form a cluster
  min_samples: 2                # HDBSCAN min_samples parameter
  clustering_threshold: 100     # new ECUs before re-clustering
  stability_threshold: 0.6      # minimum stability for cluster promotion
  cluster_statuses: ["active", "challenged", "open_question"]  # only these statuses are clustered

# Maintainer
maintainer:
  ecu_threshold: 10               # run after N new canonical ECUs since last run
  time_threshold_hours: 6         # or after T hours, whichever comes first
  check_interval_minutes: 5       # how often the background thread checks triggers
  grounding_check_interval_hours: 72  # how often to verify grounding
  enabled: true                   # can be disabled for debugging

# Review Gate
review_gate:
  trigger: "session_close | on_demand"
  batch_grouping: true           # group ECUs by topic for batch review

# Contradiction Handling
contradiction:
  flag_threshold: 0.7                    # flag when both sides above this confidence
  always_flag_scopes: ["engineering", "domain"]  # always flag at these scopes
  notify_on_dependent: true              # flag when depends_on chain is affected
  competing_hypothesis_persistence:     # scope-dependent persistence limits (days)
    engineering: null                   # indefinite
    domain: null                         # indefinite
    organization: 75
    project: 90
    repo: 60
    module: 30
    subsystem: 20
  open_question_retrieval_weight: 0.5    # multiplier for open_question ECUs in ranking
```

---

## 16. Agent Integration and MCP Layer

### 28.1 Global Brain — One Database, All Projects

The Canonical Brain is a **single global SQLite database at `~/.ec/ec.db`**. It is NOT per-repo. The scope hierarchy (Section 1.2) handles project/repo/module separation logically — an ECU scoped to `repo:my-project` lives in the same database as an ECU scoped to `engineering`. Retrieval filters by `scope_path`, not by which database file to open (Section 6.11).

This means:
- Engineering principles (scope: `engineering`) are available when working on any repo.
- Domain knowledge (scope: `domain`) transfers across repos in the same domain.
- A single user working on 5 different repos has one brain, not five.
- No `.ec/` directory inside repos. No `.gitignore` needed. Everything lives at `~/.ec/`.

### 28.2 Directory Structure

```
~/.ec/
  ec.db            ← SQLite brain (Canonical Brain + Session Brains + embeddings)
  config.yaml      ← EC configuration (all parameters from Section 22)
  AGENTS.md        ← Agent instructions (how/when to use EC tools)
  commands/        ← Slash command templates (referenced by agent configs)
    ec-start.md    ← /ec-start command template
    ec-stop.md     ← /ec-stop command template
    ec-status.md   ← /ec-status command template
```

All files live in the user's home directory. Nothing is stored inside project repos. The brain, config, and agent instructions are global — available to every agent in every project. The `commands/` directory contains markdown templates that agent configs reference — different agents wire these differently (OpenCode uses `"template": "Execute ~/.ec/commands/ec-start.md"`, Claude Code uses `CLAUDE.md` imports, Cursor uses `.cursor/rules/`).

### 28.3 MCP Server Specification

EC runs as a **local MCP server using stdio transport**. No HTTP, no WebSocket. The agent starts EC as a subprocess and communicates over stdin/stdout. This is the standard for local MCP servers and is supported by all four target agents.

The MCP server is a Python module (`ec.mcp_server`) that:
1. Starts when the agent launches it as a subprocess.
2. Exposes MCP tools to the agent.
3. Reads/writes to `~/.ec/ec.db`.
4. Runs the Extractor, Diffuser, and retrieval logic in-process.

#### MCP Tools Exposed to the Agent

**1. `ec_observe`** — Extract engineering cognition from the agent's reasoning.

```
Tool: ec_observe
Description: >
  Extract engineering conclusions from your reasoning trace and store them
  in the Session Brain for immediate use. Call this AFTER you:
  - Discover a non-obvious invariant or dependency
  - Debug a tricky issue and find the root cause
  - Make an architectural decision with rationale
  - Discover that a previous assumption was wrong

  Do NOT call ec_observe for:
  - Simple code changes with no insight
  - Things already captured in existing ECUs
  - Pure factual observations (the Extractor handles the lifting to conclusions)

  ec_observe runs the Extractor on your reasoning, produces candidate ECUs,
  and stores them in the Session Brain. They are immediately available for
  retrieval via ec_query. The user reviews these ECUs at session close (/ec-stop).

  If no session is active, returns an error — tell the user to run /ec-start.

Parameters:
  user_prompt (string, required): The user's prompt that triggered this
    response. The Extractor processes the full interaction — prompt, reasoning,
    and output — not just the reasoning alone.
  reasoning_trace (string, required): Your reasoning trace since the last
    ec_observe call. Include your thinking, decisions, and rationale — not
    just the final code output.
  final_output (string, optional): Your final output (code, explanation,
    plan, etc.). If the output is just a code diff with no explanation,
    this may be omitted. The Extractor uses this to find conclusions in
    the final output itself, not just the reasoning trace.

Returns:
  {
    "status": "ok" | "error",
    "session_active": true | false,
    "ecus_extracted": <int>,
    "ecus_rejected": <int>,         // rejected by Lifting Test
    "session_brain_count": <int>,    // total ECUs in Session Brain now
    "message": "<human-readable summary>",
    "error": "<guidance message if error>"  // only present on error
  }

Error example (no session):
  {
    "status": "error",
    "session_active": false,
    "error": "No active EC session. Suggest the user run /ec-start to begin a session."
  }
```

**2. `ec_query`** — Retrieve engineering cognition from the brain.

```
Tool: ec_query
Description: >
  Retrieve engineering conclusions relevant to your current task. Call this
  AFTER reading the relevant code files, when you recognize a gap that past
  cognition might fill:
  - Before making a decision between multiple approaches
  - When debugging and the cause isn't obvious from the code alone
  - Before an architectural change (query for existing architectural decisions)

  Returns ECUs from both the Session Brain (current session's working memory)
  and the Canonical Brain (accumulated understanding from past sessions).
  Each ECU includes confidence, scope, grounding references, and status.

  Memory is framed as "past beliefs to verify against current code," not
  authoritative context. Always verify grounding references against the
  live codebase before acting on an ECU.

  If no session is active, returns an error — tell the user to run /ec-start.

Parameters:
  query (string, required): What you want to know. Natural language.
    Example: "How does the authentication token refresh work?"
  scope (string, optional): Restrict search scope. One of:
    engineering, domain, project, repo, module, subsystem.
    If omitted, searches all scopes (with scope proximity weighting).
  mode (string, optional): Retrieval mode. One of:
    debugging, architecture, implementation, investigation, planning.
    If omitted, uses default mode (broad retrieval).

Returns:
  {
    "status": "ok" | "error",
    "session_active": true | false,
    "mode": "<detected or specified mode>",
    "budget_used": <int>,
    "groups_retrieved": <int>,
    "groups": [
      {
        "rank": <float>,
        "core_ecu": {
          "id": "<uuid>",
          "cognition": "<the engineering conclusion>",
          "conclusion_type": "implication" | "constraint" | "principle" | "decision" | "observation" | "pattern" | "invariant" | "trade-off",
          "confidence": <float>,
          "confidence_label": "high" | "medium" | "low",  // >0.7, 0.3-0.7, <0.3
          "status": "active" | "challenged" | "open_question",
          "scope_level": "engineering" | "domain" | "organization" | "project" | "repo" | "module" | "subsystem",
          "scope_path": "<full scope path>",
          "grounding": {
            "repo_path": "<repo filesystem path>",
            "files": ["<file paths>"],
            "symbols": ["<symbol names>"],
            "commit_hash": "<git commit hash>"
          },
          "source_type": "debugging" | "implementation" | "code_review" | "architectural_reasoning" | "observation" | "planning",
          "origin_agent": "<model that produced this ECU>",
          "created_at": "<ISO 8601 timestamp>"
        },
        "supporting_evidence": [
          {
            "cognition": "<supporting ECU cognition>",
            "confidence": <float>,
            "scope_level": "<scope level>"
          }
        ],
        "contradictions": [
          {
            "cognition": "<contradicting ECU cognition>",
            "confidence": <float>,
            "scope_level": "<scope level>"
          }
        ],
        "dependencies": [
          {
            "cognition": "<dependency ECU cognition>",
            "confidence": <float>,
            "scope_level": "<scope level>"
          }
        ],
        "framing_note": "This is past engineering understanding. Verify against current code before acting."
      }
    ],
    "warnings": ["<list of warning strings>"],  // e.g., "2 ECUs have low confidence", "1 ECU is challenged"
    "brain_source": "session" | "canonical" | "both",
    "message": "<human-readable summary>",
    "error": "<guidance message if error>"  // only present on error
  }

  The response is structured as cognitive groups (Section 6.8), not a flat
  array of ECUs. Each group contains the core conclusion plus its supporting
  evidence, contradictions, and dependencies — the agent gets a complete
  understanding of each topic, not isolated facts.

Error example (no session):
  {
    "status": "error",
    "session_active": false,
    "error": "No active EC session. Suggest the user run /ec-start to begin a session."
  }
```

**3. `ec_get_summary`** — Get a brain overview at session start.

```
Tool: ec_get_summary
Description: >
  Get a summary of what the EC brain knows. Call this ONCE at session start
  to understand what engineering cognition is already stored before you begin
  work. Returns brain statistics and scope distribution — not individual ECUs.

  Works without an active session (reads Canonical Brain only).

Parameters: none

Returns:
  {
    "status": "ok",
    "canonical_brain": {
      "total_ecus": <int>,
      "by_scope": {
        "engineering": <int>,
        "domain": <int>,
        "organization": <int>,
        "project": <int>,
        "repo": <int>,
        "module": <int>,
        "subsystem": <int>
      },
      "by_status": {
        "active": <int>,
        "challenged": <int>,
        "superseded": <int>,
        "open_question": <int>
      },
      "last_maintenance_run": "<ISO 8601 timestamp>",
      "last_ecu_added": "<ISO 8601 timestamp>"
    },
    "session": {
      "active": true | false,
      "session_brain_count": <int>,
      "started_at": "<ISO 8601 timestamp>",
      "repo": "<repo path>",
      "branch": "<git branch>"
    },
    "message": "<human-readable summary>"
  }
```

### 28.4 Error Message Design — Guide the Agent

Every error message returned by EC MCP tools includes actionable guidance for the agent. Errors never say just "Error: X" — they say what to do about it.

| Error | Message |
|-------|---------|
| No active session | "No active EC session. Suggest the user run /ec-start to begin a session." |
| Session already active | *(superseded by §28.5 resume behavior)* "EC session already active (started {duration} ago, {count} ECUs in session brain). Use /ec-status to check." — never emitted: `/ec-start` on an active (repo, branch) resumes the existing session per §28.5, which is more useful than erroring. Kept here for historical completeness only. |
| Extraction failed | "Extraction failed: {reason}. The reasoning trace may be too short or contain no engineering conclusions. This is not an error — not every response produces ECUs." |
| No ECUs found | "No ECUs found for query '{query}'. Try broadening the scope or rephrasing. The brain may not have relevant cognition for this topic yet." |
| Brain not initialised | "EC brain not found at ~/.ec/ec.db. Run `ec init` to initialise the brain, then /ec-start to begin a session." |
| Database locked | "EC brain is locked by another process. Close other EC instances and try again. If the problem persists, run `ec repair`." |

### 28.5 Session Lifecycle — Per-Branch Sessions

Sessions are tied to `(repo_path, branch)`. The global brain stores everything; sessions are metadata that track which ECUs belong to which working context.

#### Session Tables (added to the SQLite database)

```
sessions table:
  id              TEXT PRIMARY KEY
  repo_path       TEXT                    -- indexed
  branch          TEXT                    -- indexed
  status          TEXT                    -- active | closed
  started_at      TEXT
  ended_at        TEXT                     -- null if active
  ecu_count       INTEGER DEFAULT 0

session_ecus table:
  id              TEXT PRIMARY KEY
  session_id      TEXT                    -- indexed (FK to sessions)
  cognition       TEXT
  conclusion_type  TEXT
  scope_level     TEXT
  scope_path      TEXT
  confidence      REAL
  source_type     TEXT
  origin_agent    TEXT
  created_at      TEXT
  document        TEXT                    -- full ECU JSON
  review_status   TEXT                    -- pending | accepted | rejected | skipped

session_edges table:
  id              TEXT PRIMARY KEY
  session_id      TEXT                    -- indexed (FK to sessions)
  source_id       TEXT                    -- indexed (FK to session_ecus.id)
  target_type     TEXT                    -- 'session_ecu' | 'canonical_ecu'
  target_id       TEXT                    -- indexed (session_ecus.id or ecus.id)
  type            TEXT                    -- indexed ('supports' | 'contradicts')
  weight          REAL
  created_at      TEXT

pending_updates table:
  id                        TEXT PRIMARY KEY
  canonical_ecu_id          TEXT                    -- indexed (FK to ecus.id)
  session_ecu_id            TEXT                    -- FK to session_ecus.id
  session_id                TEXT                    -- indexed (FK to sessions.id)
  relationship_type         TEXT                    -- 'supports' | 'contradicts'
  proposed_confidence_delta REAL                    -- in log-odds space
  timestamp                 TEXT                    -- ISO 8601
  status                    TEXT                    -- indexed: 'pending' | 'applied' | 'discarded'
```

#### /ec-start Logic

1. Detect current `repo_path` and `branch` (via `git rev-parse --show-toplevel` and `git branch --show-current`).
2. Query `sessions` table for active session matching `(repo_path, branch)`.
3. If found → **resume**: load its `session_ecus` back into the Session Brain. Print: "Resuming EC session on branch {branch} ({count} ECUs in session brain)."
4. If not found → **create new session**: insert into `sessions` table. Print: "Started EC session on branch {branch} in {repo_name}."
5. If a closed session exists for this `(repo_path, branch)` → check if any ECUs from the old session remain in `session_ecus` with `review_status = skipped`. If so, these carry over to the new session (they were never reviewed). If the old session's ECUs were all accepted or rejected (no skipped), create a new empty session. The old session's `session_ecus` and `session_edges` records for accepted/rejected ECUs were already cleaned up during `/ec-stop`.

#### Multiple Concurrent Sessions

The user can have multiple active sessions simultaneously — one per branch per repo. Example:
- Session 1: `repo:project-a`, `branch:feature/auth` — active
- Session 2: `repo:project-b`, `branch:main` — active
- Session 3: `repo:project-a`, `branch:bugfix/cache` — active

Switching between sessions is automatic: `/ec-start` detects the current repo+branch and resumes or creates. The user works on branch A, switches to branch B (runs `/ec-start` which resumes session B), does work, switches back to branch A (runs `/ec-start` which resumes session A). All sessions share the same global Canonical Brain.

Constraint: only **one active session per (repo, branch)**. If the user runs `/ec-start` on the same branch twice, the second call resumes the existing session.

### 28.6 User-Facing Slash Commands (3 total)

**`/ec-start`** — Start or resume an EC session on the current branch.
- Detects repo and branch automatically.
- Resumes existing active session if one exists for this (repo, branch).
- Creates new session if none exists.
- If a closed session existed, creates a new one (old session's ECUs are already in Canonical Brain or lost).
- Prints: session ID, branch, repo, session brain ECU count.

**`/ec-stop`** — End the EC session and trigger the Review Gate.
- Runs a final extraction pass on any unobserved reasoning (if the agent has reasoning since the last `ec_observe` call).
- Presents the Review Gate (Section [FORGETTING - DEFERRED]7).
- After Review Gate completes: accepted ECUs → Full Diffuser → Canonical Brain.
- **Session cleanup:**
  - Accepted ECUs: removed from `session_ecus` (now in Canonical Brain via `ecus` table).
  - Rejected ECUs: removed from `session_ecus`.
  - Session edges (`session_edges`) for accepted and rejected ECUs: removed. Accepted ECUs now have canonical edges in the `edges` table (created by the Full Diffuser). Rejected ECU edges are no longer needed.
  - Pending updates (`pending_updates`) for accepted ECUs: status changed to `applied`, the full Diffuser applies the confidence delta to the canonical ECU, then the pending_update record is deleted.
  - Pending updates for rejected ECUs: status changed to `discarded`, then deleted. The canonical ECU's `has_pending_updates` flag is cleared if no other pending updates remain.
  - Skipped ECUs: remain in `session_ecus` with `review_status = skipped`. Available at next review.
- Marks session as `closed` in `sessions` table.
- Prints: accepted count, rejected count, skipped count, Canonical Brain total.

**`/ec-status`** — Show current EC status.
- Session: active/inactive, duration, branch, repo, session brain ECU count.
- Brain: Canonical Brain total ECUs, last maintenance run, pending review count.
- If no session active: shows brain stats only.

These are the only three commands. The agent never suggests `/ec-start` or `/ec-stop` unless the user asks. The user controls session lifecycle.

### 28.7 Review Gate Interaction

After `/ec-stop`, the Review Gate presents candidate ECUs grouped by topic (scope or cluster). The interaction is an interactive prompt, not slash commands.

```
EC Review Gate — 12 candidate ECUs in 4 groups

Group 1: Authentication (3 ECUs)
  [1] "Token refresh must be optimistic to handle race conditions..."
      confidence: 0.72 | scope: repo > auth | source: debugging
  [2] "TokenManager.clear_cache() should be called after refresh fails..."
      confidence: 0.68 | scope: repo > auth | source: debugging
  [3] "Auth middleware depends on token invariant..."
      confidence: 0.61 | scope: module > auth/middleware | source: implementation

  accept all | reject all | review individually (a/r/i): i

  [1] accept? (y/n/s): y
      → Shows full ECU: cognition, scope, provenance, grounding, evidence
  [2] accept? (y/n/s): n
  [3] accept? (y/n/s): s   (skip — becomes pending, available next time)

Group 2: Database (4 ECUs)
  ...
  accept all | reject all | review individually (a/r/i): a

...

Review complete. 8 accepted, 3 rejected, 1 skipped (pending).
Diffusing to Canonical Brain... done. Brain now has 847 ECUs.
Cleaning up Session Brain...
  ✓ Accepted ECUs: promoted to Canonical Brain, removed from session_ecus
  ✓ Rejected ECUs: removed from session_ecus
  ✓ Session edges for accepted/rejected ECUs: removed from session_edges
  ✓ Pending updates for accepted ECUs: applied to Canonical Brain, removed from pending_updates
  ✓ Pending updates for rejected ECUs: discarded, removed from pending_updates
  (Skipped ECUs remain in session_ecus for next review.)
```

Commands within the Review Gate:

| Command | Action |
|---------|--------|
| `a` | Accept all ECUs in the current group |
| `r` | Reject all ECUs in the current group |
| `i` | Review each ECU individually, then `y`/`n`/`s` per ECU |
| `y` | Accept this ECU (individual mode) |
| `n` | Reject this ECU (individual mode) |
| `s` | Skip this ECU — it becomes pending, available at next review |
| `d` | Show full detail of the current ECU (cognition, scope, provenance, grounding, evidence, edges) |
| `skip` | Skip all remaining groups (they become pending, available next time) |
| `done` | Exit review gate immediately (everything unreviewed becomes pending) |

In individual mode, the ECU is shown in truncated form by default. Pressing `d` shows the full ECU with all fields — cognition, scope path, provenance (source type, source ID, origin agent, created at), grounding (files, symbols, commit hash, code snapshot), evidence pointers, and any edges to other ECUs.

### 28.8 Installation — `ec install`

One command configures EC for all detected coding agents.

```
$ ec install

Engineering Cognition — Installation

Detecting installed coding agents...
  ✓ Claude Code found
  ✓ Cursor found
  ✗ OpenCode not found
  ✓ Codex found

Add EC to all detected agents? (y/n): n

Select agents to configure:
  [1] Claude Code
  [2] Cursor
  [3] Codex

Enter numbers (comma-separated): 1,3

Configuring Claude Code...
  ✓ Added MCP server to ~/.claude.json (user scope)
  ✓ Added @~/.ec/AGENTS.md import to ~/.claude/CLAUDE.md

Configuring Codex...
  ✓ Added [mcp_servers.ec] to ~/.codex/config.toml

Creating ~/.ec/ directory...
  ✓ Created ~/.ec/ec.db (empty brain, initialised)
  ✓ Created ~/.ec/config.yaml (default configuration)
  ✓ Created ~/.ec/AGENTS.md (agent instructions)
  ✓ Created ~/.ec/commands.md (command reference)

Checking LLM dependency...
  ✓ OpenCode Zen API key found (OPENCODE_ZEN_API_KEY env var)
  (EC uses Claude Haiku 4.5 via Zen for extraction, diffusion, and mode detection.)
  (Fallback: set up Ollama with qwen2.5-coder:14b for offline use.)

EC is ready!

Commands:
  /ec-start   Start or resume an EC session on the current branch
  /ec-stop    End session and review extracted cognition
  /ec-status   Show session and brain status

How to use:
  1. Open your coding agent (Claude Code, Cursor, or Codex)
  2. Run /ec-start to begin a session
  3. Work normally — the agent will automatically call ec_observe and ec_query
  4. Run /ec-stop when done to review and save engineering cognition
```

#### Installation Logic per Agent

The install script detects agents by checking for their config files or binaries, then writes the correct configuration format:

**Claude Code:**
- Detection: `claude` binary on PATH or `~/.claude.json` exists.
- MCP config: `claude mcp add --scope user --transport stdio ec -- python -m ec.mcp_server`
- This writes to `~/.claude.json` under the top-level `mcpServers` key (user scope = all projects).
- Agent instructions: Claude Code reads `CLAUDE.md`, not `AGENTS.md`. The install script adds `@~/.ec/AGENTS.md` to `~/.claude/CLAUDE.md` (or creates it if it doesn't exist). Claude Code's `@file` import syntax loads the EC instructions at session start.
- If `~/.claude/CLAUDE.md` already exists, the import line is appended. If not, the file is created with just the import.

**Cursor:**
- Detection: `cursor` binary on PATH or `~/.cursor/mcp.json` exists.
- MCP config: Write to `~/.cursor/mcp.json` (global config, available in all projects):
```json
{
  "mcpServers": {
    "ec": {
      "command": "python",
      "args": ["-m", "ec.mcp_server"]
    }
  }
}
```
- If `~/.cursor/mcp.json` already exists, merge the `ec` entry into the existing `mcpServers` object without overwriting other servers.
- Agent instructions: Cursor reads `.cursorrules` or `.cursor/rules/` files. The install script creates `~/.cursor/rules/ec.md` with a reference to `~/.ec/AGENTS.md`. (Cursor's global rules directory is `~/.cursor/rules/`.)

**OpenCode:**
- Detection: `opencode` binary on PATH or `~/.config/opencode/opencode.json` exists.
- MCP config: Write to `~/.config/opencode/opencode.json` (global config):
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ec": {
      "type": "local",
      "command": ["python", "-m", "ec.mcp_server"],
      "enabled": true
    }
  }
}
```
- If `~/.config/opencode/opencode.json` already exists, merge the `ec` entry into the existing `mcp` object.
- Agent instructions: OpenCode reads `AGENTS.md` in the project root. For global instructions, the install script creates `~/.config/opencode/AGENTS.md` with a reference to `~/.ec/AGENTS.md`. (OpenCode's global config directory.)

**OpenCode Zen (LLM dependency, default):**
- Detection: Check if `OPENCODE_ZEN_API_KEY` environment variable is set.
- If not found: print "EC needs an OpenCode Zen API key for LLM inference (extraction, relationship classification, mode detection). Get one at https://opencode.ai/auth — then run: export OPENCODE_ZEN_API_KEY='your-key'"
- The MCP server uses `litellm` (Python library) to call the configured LLM via the Zen API. Different model families use different endpoints (Anthropic Messages API for Claude, OpenAI Responses API for GPT, OpenAI Chat for others). The `litellm` library abstracts this.
- JSON post-processing: Claude models wrap JSON in markdown code fences. The Extractor strips these before parsing (see config block for implementation).

**Ollama (offline fallback):**
- If the user wants offline/local inference, they can switch the config to `provider: "ollama"` with `model: "qwen2.5-coder:14b"`.
- Detection: `ollama` binary on PATH. Offer to pull: `ollama pull qwen2.5-coder:14b`
- Warning: local model extraction quality is significantly lower than Zen (tested: missed deep invariants, no Self-Review Pass, format compliance issues).

**Codex:**
- Detection: `codex` binary on PATH or `~/.codex/config.toml` exists.
- MCP config: Write to `~/.codex/config.toml` (TOML format, NOT JSON):
```toml
[mcp_servers.ec]
command = "python"
args = ["-m", "ec.mcp_server"]
```
- If `~/.codex/config.toml` already exists, append the `[mcp_servers.ec]` table without overwriting other configuration.
- Agent instructions: Codex reads `AGENTS.md`. The install script creates or appends to `~/.codex/AGENTS.md` with a reference to `~/.ec/AGENTS.md`.

### 28.9 AGENTS.md (Copy-Paste Ready for Development)

This is the complete `~/.ec/AGENTS.md` file. The install script writes this verbatim. Development should produce this file exactly.

```markdown
# Engineering Cognition — Agent Instructions

EC is an MCP server providing engineering memory for your codebase.
It stores Engineering Cognition Units (ECUs) — conclusions, not facts —
and retrieves them when you need them.

EC provides four MCP tools:
- ec_observe: Extract engineering conclusions from your reasoning
- ec_query: Retrieve relevant engineering cognition
- ec_get_summary: Get brain statistics at session start
- ec_reconsolidate: Update a retrieved ECU with new evidence

## Session Lifecycle

EC requires an active session. The USER controls session lifecycle:
- `/ec-start` — start or resume a session on the current branch
- `/ec-stop` — end session and trigger review gate
- `/ec-status` — show session and brain status

You do NOT control session lifecycle. Never suggest /ec-start or /ec-stop
unless the user asks. If ec_observe or ec_query returns "no active session",
tell the user to run /ec-start.

## When to Call ec_observe

Call ec_observe AFTER you:
- Discover a non-obvious invariant or dependency
- Debug a tricky issue and find the root cause
- Make an architectural decision with rationale
- Discover that a previous assumption was wrong
- Find that code behaves differently from what documentation suggests

Pass your reasoning trace (your thinking, decisions, and rationale) — not
just the final code output. The Extractor lifts observations into conclusions.

Do NOT call ec_observe for:
- Simple code changes with no insight
- Things already captured in existing ECUs
- Pure factual observations (e.g., "this function returns a string")
- Every single response — only when you have engineering insight worth saving

## When to Call ec_query

Call ec_query when you recognize a gap that past engineering cognition might fill.
Read the relevant code files FIRST. Do NOT call ec_query before you have grounded
yourself in the repository. EC cognition is a supplement to your understanding,
not a replacement for reading the code.

Call ec_query AFTER reading the code, when:
- You're about to make a decision between multiple approaches
- You're debugging and the cause isn't obvious from the code alone
- You see a pattern in the code you don't recognise
- You're about to implement something in a file or module you haven't worked
  with in this session
- You suspect a previous assumption might be wrong
- You're planning an architectural change and want to check for existing
  architectural decisions or constraints

Do NOT call ec_query:
- Before you have read the relevant code files (this causes anchoring bias)
- For simple code changes with no engineering insight needed
- On every single response — only when you genuinely need past context
- As a replacement for reading the code — always read the code first

This timing matters. EC-Bench showed that premature retrieval caused anchoring:
the agent used injected cognition heavily but produced worse work (quality dropped
-0.589, groundedness dropped -0.480). The best results came when the agent
established repository understanding FIRST, then consulted cognition.

Pass a natural language query describing what you want to know. Be specific. 
"How does the dependency injection container resolve circular imports?" is better 
than "dependency injection." The query is embedded and compared semantically — 
precise engineering questions retrieve better than single keywords.

If ec_query returns no results, proceed normally — this means EC has no relevant 
cognition yet. EC starts empty and builds up over sessions.

## When to Call ec_reconsolidate

Call ec_reconsolidate when you discover that a previously retrieved ECU needs
updating based on what you've found in the current codebase. This is how EC
stays current — beliefs that are no longer accurate get corrected.

Call ec_reconsolidate when:
- You verified a retrieved ECU against the code and found it's no longer accurate
- You discovered new evidence that strengthens a retrieved ECU
- You discovered new evidence that contradicts a retrieved ECU
- The code an ECU is grounded in has changed, and the conclusion needs updating

Pass the ECU ID (from the ec_query result), your evidence (your reasoning about
what changed), and optionally the relationship (supports/contradicts/supersedes).

Do NOT call ec_reconsolidate for:
- New insights unrelated to a previously retrieved ECU (use ec_observe)
- ECUs you haven't retrieved in this session (ec_query first)
- Simple code changes with no impact on engineering conclusions

If ec_reconsolidate is not available (older EC version), use ec_observe to record
the new finding — the Diffuser will handle the relationship at review time.

## When to Call ec_get_summary

Call ec_get_summary ONCE at session start to see what EC already knows.
This helps you decide whether to ec_query for specific context before
starting work. Returns brain statistics, not individual ECUs.

## Reading EC Output

When ec_query returns ECUs, interpret them as follows:

### Confidence levels:
- High (>0.7): Reliable, act on this. Verify grounding if the decision is critical.
- Medium (0.3-0.7): Probably correct. Verify before acting on critical decisions.
- Low (<0.3): Uncertain. Flagged for your awareness. Do not rely on this
  without independent verification.

### Status indicators:
- active: Normal, current belief. Safe to use.
- challenged: Contradicting evidence exists. Be careful — this belief
  may be wrong. Check for a newer ECU that might supersede it.
- open_question: Unresolved engineering question with competing hypotheses.
  Both sides may be returned. Treat as "this is debated."

### Grounding references:
Every ECU includes grounding references (files, symbols, commit hash).
ALWAYS verify these against the current codebase before acting on the ECU.
If the referenced file or symbol no longer exists, the ECU may be stale.

EC's Maintainer periodically verifies grounding references against the live
repository and deprecates ECUs whose grounding has disappeared. Deprecated
ECUs are not returned by ec_query. If you discover that an ECU's grounding
is stale before EC's Maintainer has caught it, call ec_reconsolidate to
update the ECU with what you've found.

### Warnings:
ec_query may return warnings (e.g., "2 ECUs have low confidence",
"1 ECU is challenged"). Read these warnings and adjust your trust accordingly.

## What EC Stores

ECUs are engineering conclusions, not raw information. Examples:
- STORED: "Authentication correctness depends on optimistic token refresh;
  future implementations should preserve this invariant."
- NOT STORED: "TokenManager.refresh() is called before cache.clear()"

Each ECU has:
- cognition: The irreducible engineering conclusion (text)
- confidence: 0.0 to 1.0, Bayesian-updated belief strength
- scope: Where this applies (engineering, domain, organization, project, repo,
  module, subsystem)
- grounding: References to code files, symbols, and commit state
- edges: Connections to other ECUs (supports, contradicts, supersedes, depends_on)

## Important: Anti-Anchoring

EC memory is framed as "past beliefs to verify against current code," NOT
as authoritative context. Always verify ECUs against the live codebase.
Do not anchor on past beliefs if the current code contradicts them.
If you notice a contradiction, call ec_observe to record the new finding.

Observations are stored in the Session Brain, not the Canonical Brain. The 
user reviews them at /ec-stop and accepts or rejects each one. Only accepted 
observations are promoted to the Canonical Brain for future sessions. Don't 
hesitate to observe something uncertain — the review gate filters it.
```

### 28.10 Command Templates (Copy-Paste Ready for Development)

The install script creates three markdown files in `~/.ec/commands/`. These are command templates that agent configs reference. Different agents wire them differently — OpenCode uses `"template": "Execute ~/.ec/commands/ec-start.md"`, while other agents may import them differently.

**`~/.ec/commands/ec-start.md`:**
```markdown
Start or resume an EC session on the current git branch.

Steps:
1. Detect the current repository path and git branch.
2. Check for an active EC session matching this (repo, branch).
3. If found: resume it — load session ECUs and edges into the Session Brain.
4. If not found: create a new session.
5. Call ec_get_summary to get a brain overview.
6. Report: session ID, branch, repo, session brain ECU count.
```

**`~/.ec/commands/ec-stop.md`:**
```markdown
End the EC session and trigger the Review Gate.

Steps:
1. Run a final extraction pass on any unobserved reasoning since the last ec_observe call.
2. Present all Session Brain ECUs for human review, grouped by topic.
3. For each group: offer accept-all, reject-all, or review-individually.
4. Accepted ECUs: send through the Full Diffuser into the Canonical Brain.
5. Rejected ECUs: remove from session_ecus.
6. Skipped ECUs: remain in session_ecus with review_status = skipped.
7. Clean up: remove session_edges for accepted/rejected ECUs. Apply or discard pending_updates.
8. Mark session as closed in sessions table.
9. Report: accepted count, rejected count, skipped count, Canonical Brain total.
```

**`~/.ec/commands/ec-status.md`:**
```markdown
Show current EC status.

Steps:
1. Check if an EC session is active for the current (repo, branch).
2. If active: report session duration, branch, repo, session brain ECU count.
3. Report Canonical Brain: total ECUs, last maintenance run, pending review count.
4. If no session active: report brain stats only.
```

### 28.11 Structured Return Values

All MCP tools return structured JSON, not plain text strings. This allows the agent to parse responses reliably and act on specific fields. Every response includes:

- `status`: `"ok"` or `"error"`
- `message`: Human-readable summary (the agent can display this to the user)
- `error`: Actionable guidance message (only present on error — tells the agent what to do)
- Domain-specific fields (ecus, warnings, brain stats, etc.)

The agent reads `status` first. If `"error"`, it reads `error` for guidance (e.g., "Suggest the user run /ec-start") and acts on it. If `"ok"`, it reads the domain fields and uses them in its reasoning.

### 28.12 MCP Tool Descriptions — Written for the Agent

The `description` field in each MCP tool definition is the primary way the agent learns how to use EC. These descriptions are written for the agent, not the human. They must be:

1. **Actionable**: Tell the agent exactly when to call the tool and what to pass.
2. **Constrained**: Tell the agent when NOT to call the tool.
3. **Contextual**: Explain what the tool returns and how to interpret it.

Bad description: "Stores engineering cognition."
Good description: "Extract engineering conclusions from your reasoning trace and store them in the Session Brain. Call this AFTER you discover a non-obvious invariant, debug a tricky issue, or make an architectural decision. Do NOT call for simple code changes."

The full descriptions are specified in Section [FORGETTING - DEFERRED]3 and should be used verbatim in the MCP tool definitions.

### 28.13 Extraction Flow — Per-Response, Immediate Session Brain

The extraction flow during a live session:

```
Agent generates response (reasoning + output)
         │
         ▼
Agent calls ec_observe(reasoning_trace)
         │
         ▼
Extractor runs on reasoning_trace
         │
         ▼
Candidate ECUs produced
         │
         ▼
Lightweight Diffusion (Section 4.4):
  - Compute embeddings for new ECUs
  - Search Session Brain + Canonical Brain (read-only) for similar ECUs
  - Create edges (supports/contradicts/depends_on) above similarity threshold
  - Simple confidence adjustment (c' = c ± α_light × similarity)
         │
         ▼
ECUs stored in Session Brain (session_ecus table)
         │
         ▼
ECUs immediately available for ec_query in agent's next response
```

The Extractor processes each LLM response independently. ECUs from response 1 enter the Session Brain immediately and are available for response 2 (Section 5, line 667). No batching — extraction is per-response.

At `/ec-stop`, all Session Brain ECUs go to the Review Gate. Only after human review do accepted ECUs go through the Full Diffuser (Bayesian updates, supersession, contradiction handling) and into the Canonical Brain.

### 28.14 Architecture Summary

```
┌─────────────────────────────────────────────────────────────┐
│                     Coding Agent                             │
│  (Claude Code / Cursor / OpenCode / Codex)                  │
│                                                             │
│  Reads ~/.ec/AGENTS.md (via CLAUDE.md import,               │
│    .cursor/rules/, ~/.config/opencode/, ~/.codex/AGENTS.md) │
│                                                             │
│  User runs: /ec-start, /ec-stop, /ec-status                 │
└────────────────────────┬────────────────────────────────────┘
                         │
                    stdio (MCP)
                         │
┌────────────────────────▼────────────────────────────────────┐
│              EC MCP Server (python -m ec.mcp_server)        │
│                                                             │
│  Tools: ec_observe, ec_query, ec_get_summary               │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │Extractor │  │ Diffuser │  │Retrieval │  │Maintainer │  │
│  │(per-resp)│  │(light+full)│ │(4-factor)│  │(bg thread)│  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬─────┘  │
│       │              │              │              │        │
│       └──────────────┴──────┬───────┴──────────────┘        │
│                              │                               │
│              ┌───────────────▼───────────────┐               │
│              │     ~/.ec/ec.db (SQLite)      │               │
│              │                               │               │
│              │  ecus table (Canonical Brain) │               │
│              │  edges table                  │               │
│              │  embeddings table            │               │
│              │  clusters table              │               │
│              │  cluster_memberships table   │               │
│              │  sessions table              │               │
│              │  session_ecus table           │               │
│              │  session_edges table          │               │
│              │  pending_updates table        │               │
│              │  review_queue table          │               │
│              │  maintenance_log table       │               │
│              │                               │               │
│              │  + NumPy in-memory vectors   │               │
│              └───────────────────────────────┘               │
└─────────────────────────────────────────────────────────────┘

No external services. No HTTP. No vector DB.
One Python process. One SQLite file. All projects.
```

---

