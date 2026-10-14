# Investigation: Multi-harness and multi-provider support in Reverie

**Date:** 2026-10-14
**Type:** read-only investigation. No code, config, or docs were changed. The only file created is this report.
**Verification:** all offline tests pass (`506 passed, 6 skipped` — the 6 skips are the live-API tests gated on `OPENCODE_ZEN_API_KEY`). No live API calls were made during this investigation; provider wire-level claims about third-party services are marked unverified where noted.

## Confirmation of the seven beliefs

1. **Confirmed.** `call_llm` has exactly four call sites: `ec/extractor.py:234` (extraction), `ec/diffuser.py:270` (relationship classification, both 5-way and lightweight 3-way), `ec/diffuser.py:374` (contradiction adjudication), `ec/mode_detection.py:90` (mode classification).
2. **Corrected.** `ec_reconsolidate` makes **zero LLM calls only when the agent passes an explicit `relationship`** (ec/reconsolidation.py:279-284 skips classification; test_phase10_reconsolidation.py:122 asserts the mock is never called in that case). When the relationship is omitted it calls `_classify_pairs` for the target pair (reconsolidation.py:282-284), and the "related ECUs" pass runs the full diffusion pipeline (reconsolidation.py:319-335 → `diffuser._diffuse_against_brain`), which is up to one batched classification call plus up to one adjudication call per contradicting pair. So a single `ec_reconsolidate` can issue 0–3 LLM calls.
3. **Confirmed.** Default `llm` block: `provider: "opencode_zen"`, `model: "claude-haiku-4-5"`, `base_url: "https://opencode.ai/zen/v1"`, `api_key_env: "OPENCODE_ZEN_API_KEY"` (ec/config.py:175-184). Not a GPT-mini model.
4. **Confirmed.** `_api_format` returns `"anthropic"` iff `model.startswith("claude")`, else `"openai"` (ec/llm.py:58-64); `provider` is documented and tested as informational only (ec/llm.py:11-15; tests/test_llm_providers.py:104-107).
5. **Confirmed.** Embeddings are local `all-MiniLM-L6-v2` via sentence-transformers, 384-dim float32 BLOBs in SQLite; no key, no network at inference (ec/embeddings.py:1-6, 40-79; ec/config.py:187-190).
6. **Confirmed with a major caveat.** `ec install` writes MCP config for Claude Code, Cursor, OpenCode, Codex (ec/install.py:611-697, registry at 700), and writes the three command templates to `~/.ec/commands/` (ec/install.py:732-736). **No harness looks in `~/.ec/commands/`** — in every supported harness the user runs the `ec-start`/`ec-stop`/`ec-status` console scripts from a terminal instead (pyproject.toml:37-39; README.md:50-52). See B9.
7. **Confirmed.** Without a Zen key the installer detects the `ollama` binary, offers to rewrite `~/.ec/config.yaml`'s `llm` block, and pulls `qwen2.5-coder:14b`; the pull is never fatal (ec/install.py:888-918, constants at 291-316).

---

# A. The LLM layer

## A1. The four call sites

**Direct answer:** one call per `ec_observe` (extraction) plus one batched classification call per stored ECU (lightweight diffusion); zero or one tiny call per `ec_query` (mode detection); at `/ec-stop`, one batched 5-way classification per accepted ECU plus rare adjudication calls; inside `ec_reconsolidate`, the same classification/adjudication pair when no explicit relationship is given. Background maintenance makes **zero** LLM calls. All calls share one model and one endpoint.

| # | Call site | Prompt asks for | Temp | Expected output | Tokens in (est.) | Tokens out (est.) | When it runs |
|---|---|---|---|---|---|---|---|
| 1 | `ec/extractor.py:234` (`extract_ecus`) | The 613-line, five-pass extraction methodology (Lifting Test, signal catalog, self-review) applied to the full interaction: session context + developer prompt + reasoning trace + final output (ec/extractor.py:195-215) | 0.3 (config `llm.temperature`, ec/config.py:181) | Strict JSON object: `{"ecus": [...]}`, `rejected_count`, `rejection_summary`; each ECU carries `cognition`, `conclusion_type` (8-value vocab), `scope{level,path}`, `source_type`, `grounding{files,symbols,code_context}`, `evidence_pointer` (ec/extractor.py:117-165, 238-249) | ~9–13K: system = `ec/prompts/extractor_prompt.md` = 34,225 chars ≈ 8.5K tokens, plus the interaction (~0.5–5K) | 0.3–2K JSON (cap `max_tokens` 4096) | Once per `ec_observe` tool call (ec/mcp_server.py:561-565) |
| 2a | `ec/diffuser.py:270` (`_classify_pairs`, lightweight 3-way system at 149-167) | For each pair (new ECU vs existing), choose supports/contradicts/unrelated | 0.0 (hardcoded, ec/diffuser.py:270) | `{"classifications": [{"pair": n, "relationship": "..."}]}` — tolerant parse; omissions/mislabels default to `unrelated` (ec/diffuser.py:275-294) | ~0.7–2K: system ≈ 430 tokens + up to `MAX_CLASSIFY_BATCH = 10` pairs (ec/diffuser.py:46-48), each ~60–120 tokens | ~30–100 tokens | Once per stored ECU during `ec_observe` (ec/mcp_server.py:571-576) |
| 2b | `ec/diffuser.py:270` (full 5-way system at 66-139, incl. Rule A/B verbatim) | For each pair (new vs existing canonical ECU), choose supports/contradicts/supersedes/depends_on/unrelated | 0.0 | Same JSON shape; 5-way vocab | ~1–2.5K (system ≈ 850 tokens + ≤10 pairs; structural proximity may add up to 5 more, ec/diffuser.py:412-456) | ~30–120 tokens | Once per accepted ECU at `/ec-stop` review (ec/review_gate.py:896-899); once per `ec_reconsolidate` target pair (ec/reconsolidation.py:282-284) and once for its related-ECU pass (ec/reconsolidation.py:319-335) |
| 3 | `ec/diffuser.py:374` (`_adjudicate_contradiction`) | Given two possibly-contradicting conclusions, decide genuine_contradiction / orthogonal / unrelated + differentiator (§17.2 stage 2) | 0.0 | `{"verdict": "...", "differentiator": "..."}` | ~0.3–0.6K (two cognition strings + scope + grounding files + confidences, template at ec/diffuser.py:183-195) | ~20–50 tokens | Only when a `contradicts` classification survives the computable pre-filter (ec/diffuser.py:604-614) — at review-gate diffusion and in reconsolidation's related-ECU pass |
| 4 | `ec/mode_detection.py:90-96` (`_llm_mode`) | Classify the query into one of five modes | 0.0 (config `temperature_mode_detection`, ec/config.py:182) | Single mode word; `max_tokens=16` (ec/mode_detection.py:94) | ~60–120 tokens (one-line system + `Query: {query}`) | ≤16 tokens | Per `ec_query` when `mode` is omitted **and** an API key is present (ec/mode_detection.py:107-115) |

Not on the LLM path at all: `ec_get_summary`, retrieval itself (embeddings + SQL only), the Maintainer's four tasks (forgetting, grounding, edge-pruning, clustering — ec/maintainer.py:13-28), and session lifecycle. `ec_reconsolidate`'s LLM usage is covered above (belief 2 correction).

## A2. Failure handling

**Direct answer:** every failure is converted into `LLMError` with a short message — there is **no retry, no backoff, no circuit breaker** anywhere in the package (grep over `ec/` for retry/backoff: no code hits). Consequences per task differ: extraction loses the observation (nothing stored, nothing persisted for retry); classification failures are fail-safe (edges skipped, ECUs survive); mode detection silently degrades to keywords; adjudication fails *toward* "genuine contradiction"; a review-gate diffusion failure leaves a promoted-but-edgeless ECU with only a printed warning and no retry command.

Details:

- **Missing key** — for the Anthropic format only, `call_llm` raises a friendly `LLMError` naming `cfg.llm.api_key_env` but with **hardcoded OpenCode Zen guidance** ("Get one at https://opencode.ai/auth") regardless of which provider is actually configured (ec/llm.py:88-94). For the OpenAI format there is no key check: the Authorization header is simply omitted when the key is empty (ec/llm.py:162-163), so a missing OpenAI key surfaces as a raw `HTTP 401: ...` error.
- **401 / 429 / 5xx** — any non-200 becomes `LLMError("LLM API returned HTTP <code>: <first 200 chars>")` (ec/llm.py:135-139, 181-185). A 429 is not distinguished from a 404; nothing sleeps or retries.
- **Timeout** — `requests.post(..., timeout=cfg.llm.timeout)` (60 s default, ec/config.py:180) raises `requests.RequestException` → `LLMError` (ec/llm.py:129-133, 175-179).
- **Invalid JSON** — per task:
  - Extraction: `json.loads` failure → `ExtractionError` → the MCP tool returns an `isError` payload ("Extraction failed: … not every response produces ECUs") and **nothing is stored** (ec/extractor.py:238-249; ec/mcp_server.py:566-568, 85-89). The interaction is not queued or persisted; the only recovery is the agent calling `ec_observe` again with the same trace.
  - Classification: unparseable response → warning log, **all pairs default to `unrelated`** — the safe direction, since spurious edges are the harmful failure mode (ec/diffuser.py:275-282).
  - Adjudication: any LLM/parse failure → treated as `genuine_contradiction` (challenge is recoverable; a missed contradiction is not, ec/diffuser.py:378-383).
- **Lightweight diffusion failure during observe** — caught; the ECUs remain stored and retrievable, `diffusion_failures` is counted and returned to the agent (ec/mcp_server.py:570-582, 605). Edges are lost for that ECU (best-effort by design).
- **Full-diffusion failure at review accept** — the human's promotion **stands**; the ECU is recorded in `result.undiffused`, a prominent CLI warning is printed (ec/review_gate.py:896-908, 974-988). `docs/ASSESSMENT.md:178` already flags that there is **no retry command** for this state; `ec-repair` does not re-diffuse (ec/repair.py is structural-only).
- **Reconsolidation classification failure** — error payload telling the agent to retry with an explicit relationship; nothing written (ec/mcp_server.py:709-720).
- **Mode detection failure** — keyword fallback; retrieval never blocks (ec/mode_detection.py:107-115).

So: extraction is the one task where a failure silently loses data (bounded — the reasoning is still in the agent's own context and can be re-observed); everything else degrades without data loss.

## A3. Which tasks need a strong model?

**Direct answer:** extraction clearly does; the two classification tasks are medium (small models are plausible but unproven here); mode detection does not need an LLM at all. There is **no automated, in-repo eval measuring extraction or classification quality by model** — the only evidence is a prose claim in the spec comments plus loose assertions in the 6 skipif-gated live tests.

- **Extraction** is a 5-pass methodology with a Lifting Test, controlled vocabularies, grounding extraction and a self-review pass (ec/prompts/extractor_prompt.md, 613 lines). `docs/SPEC.md:1994-1998` states it was "Tested against qwen2.5:7B, qwen2.5-coder:14B (local via Ollama), gpt-5.4-mini (Zen), and claude-haiku-4-5 (Zen). Haiku 4.5 produced the highest quality extraction: caught 4x more genuine conclusions than the 7B, valid format compliance, working Self-Review Pass, and caught deep invariants/trade-offs that other models missed." **No artifact of that comparison exists in the repo** (no script, no scores, no runs) — it is an unverifiable spec comment.
- **Classification (both variants)** is a batched ≤10-pair, single-word-per-pair task. The tolerant parser makes small-model failures safe-but-lossy (pairs become `unrelated`, i.e. silently fewer edges). Quality-by-model is unmeasured.
- **Adjudication** needs semantic nuance (mutual exclusivity vs orthogonality); its failure direction is the *pessimistic* one (treats as genuine), so a weak model biases toward challenged ECUs. Unmeasured.
- **Mode detection** is a 5-way classification of one sentence with a keyword fallback that the system already uses whenever no key is set (ec/mode_detection.py:107-115). The keyword classifier is fully tested offline (`test_mode_autodetect_offline`, tests/test_phase3_retrieval.py:644). An LLM here is a nice-to-have.
- **Existing evals:** the 6 live tests (`test_mode_detection_llm_live` tests/test_phase1_foundation.py:357-365; `test_extractor_live` + `test_lightweight_classification_live` tests/test_phase2_extractor_diffuser.py:667-700; `test_retrieval_live` tests/test_phase3_retrieval.py:665-666; `test_live_observe_and_query` tests/test_phase4_mcp_server.py:389-411; `test_e2e_full_pipeline` tests/test_e2e_integration.py:5-8, 69-70) assert loose bounds only (e.g. "at least the timezone/root-cause conclusion"). EC-Bench (`bench/`) judges the *coding agent's* answers with GLM-5.2 (bench/judge_ecbench.py:2-5) — it measures whether memory helped the agent, **not** extraction/classification quality per model. EC-Bench also runs only on OpenCode headless (`opencode run`, ec/run_ecbench.py:69).

## A4. Can a user point Reverie at each provider today, by editing `~/.ec/config.yaml` only?

**Direct answer:** Zen, Ollama, Anthropic-direct, OpenAI-direct (non-reasoning models), Gemini's OpenAI-compat endpoint (in principle) and OpenRouter (with vendor-prefixed model names) work or plausibly work by config alone; Bedrock and Vertex do not (auth + URL scheme need new code); OpenAI *reasoning* models fail on request parameters. Nothing here is prevented by code structure other than the two hard rules: wire format is derived from the model name, and `temperature`/`max_tokens` are always sent.

Exact config per provider (`llm` block keys; `provider` is cosmetic except where noted):

| Provider | Works by config today? | Config | Notes / what breaks |
|---|---|---|---|
| OpenCode Zen | ✅ (default, tested) | defaults in ec/config.py:175-184 | Baseline for all live tests |
| Ollama | ✅ (tested offline) | `provider: ollama`, `model: qwen2.5-coder:14b`, `base_url: http://localhost:11434/v1` | Keyless works: header omitted when key empty (ec/llm.py:162-163); installer writes this block (ec/install.py:291-298); `/v1` suffix tested (tests/test_llm_providers.py:242-263) |
| Anthropic direct | ✅ by config (unverified live) | `provider: anthropic`, `model: claude-haiku-4-5`, `base_url: https://api.anthropic.com/v1`, `api_key_env: ANTHROPIC_API_KEY` | `_call_anthropic` posts `{base_url}/messages` with `x-api-key` + `anthropic-version: 2023-06-01` (ec/llm.py:111-127) — matches Anthropic's API. Caveat: if `ANTHROPIC_API_KEY` is unset the error message misleadingly says "Get one at https://opencode.ai/auth" (ec/llm.py:88-94). This exact config was pre-designed in the Phase 13 design doc (docs/handoffs/PHASE_13_AUDIT_FIXES_DESIGN.md:204-209) but never tested live |
| OpenAI direct | ✅ for gpt-4o/gpt-4.1-class (unverified live); ❌ for o-series / gpt-5 reasoning models | `provider: openai`, `model: gpt-4o-mini`, `base_url: https://api.openai.com/v1`, `api_key_env: OPENAI_API_KEY` | Same design-doc precedent (docs/handoffs/PHASE_13_AUDIT_FIXES_DESIGN.md:197-202). Reasoning models fail: `temperature` and `max_tokens` are always sent (ec/llm.py:168-173) — see A5 |
| Google Gemini (OpenAI-compat) | ✅ in principle (unverified) | `base_url: https://generativelanguage.googleapis.com/v1beta/openai`, `model: gemini-2.5-flash`, `api_key_env: GEMINI_API_KEY` | Endpoint accepts Bearer auth and `POST {base}/chat/completions`; nothing in the code blocks it; no test covers it |
| OpenRouter | ✅ with vendor-prefixed names (unverified live) | `base_url: https://openrouter.ai/api/v1`, `model: anthropic/claude-3.5-haiku`, `api_key_env: OPENROUTER_API_KEY` | `anthropic/claude-…` does not start with `"claude"`, so it correctly routes to OpenAI Chat. **Breaks** if the user writes a bare `claude-haiku-4-5` as the model: that routes to Anthropic format and would hit `https://openrouter.ai/api/v1/messages`, whose existence/compatibility is unverified. No `HTTP-Referer`/`X-Title` headers are sent (OpenRouter recommends but does not require them) |
| Amazon Bedrock | ❌ | — | Two blockers: (1) auth — `_call_anthropic`/`_call_openai` only know `x-api-key`/`Bearer`, while Bedrock needs AWS SigV4 signing (or a bearer-token setup with different headers); (2) path scheme — Bedrock's invoke/converse URLs embed the model ID per region (`bedrock-runtime.{region}.…/model/{id}/invoke`), not `{base_url}/messages` (ec/llm.py:114). Needs a new `_call_*` + credential handling |
| Google Vertex | ❌ | — | Needs an OAuth2 bearer (gcloud ADC) and the `projects/…/locations/…/publishers/anthropic/models/{model}:messagesRaw` URL shape; `x-api-key` and `{base_url}/messages` are both wrong for it (ec/llm.py:114-119) |

Related config-only facts: `api_key_env` is read at call time via `os.environ` (ec/llm.py:52-55), so any env-var name works; the deep-merge in `load_config` means a user override only needs the four `llm` keys (ec/config.py:332-352).

## A5. Edge cases in model-name routing and request parameters

**Direct answer:** four real edge cases — (1) Claude models on OpenAI-compatible gateways are unusable, (2) any model whose name merely starts with `claude` is misrouted to the Anthropic wire format, (3) OpenAI reasoning models are rejected because `temperature`/`max_tokens` are unconditionally sent, (4) there is no header/format override of any kind.

1. **Claude model on an OpenAI-compatible gateway.** `model.startswith("claude")` forces the Anthropic Messages format (ec/llm.py:58-64); there is no config key to force a format. A user with a claude-named model on a `/chat/completions`-only gateway (e.g., an OpenAI-compat Bedrock proxy, Azure AI Foundry deployments, some corporate proxies) cannot use it — the request would go to `{base_url}/messages` (ec/llm.py:114). The docstring advertises this as a feature ("Claude models via Zen or the direct Anthropic API") but the direct-API case is the only one served.
2. **Non-Claude models whose names start with "claude".** The prefix match is intentional and tested (`"claudev1"` → anthropic, tests/test_llm_providers.py:84-91, design doc §1.2). Any future non-Anthropic model with a `claude*` name (fine-tune, renames, a vendor's "claude-like" slug) would silently be sent to `/messages` with `x-api-key` and fail with a confusing 404/401. The failure is at least loud (non-200 → `LLMError`), not silent.
3. **`max_tokens` / `temperature` vs OpenAI reasoning models.** `_call_openai` always includes `"temperature"` and `"max_tokens"` in the body (ec/llm.py:168-173), with values 0.3/0.0 and 4096 from config. OpenAI's o-series and gpt-5-class models reject non-default `temperature` and require `max_completion_tokens` instead of `max_tokens` (API-side behavior — unverified here, no test exercises it). Consequence: selecting an o-series/gpt-5 model today fails with HTTP 400 on the *first* call, with the raw provider error text surfaced by `LLMError`. The config offers no way to omit or rename these fields. (Anthropic's API is unaffected: `max_tokens` is required there and `temperature` is accepted.)
4. **Anthropic-specific fields.** The Anthropic branch is minimal but correct for the direct API: top-level `system` string (ec/llm.py:126-127), pinned `anthropic-version: 2023-06-01` (ec/llm.py:117), `max_tokens` always present. It does not send any beta headers, and it cannot use an `Authorization: Bearer` style gateway that speaks the Anthropic *schema* but expects a bearer token (no header override exists). The response parser joins all `type == "text"` blocks and ignores other block types (ec/llm.py:141-147), which is robust to tool_use/thinking blocks appearing in the payload (exercised by the ANTHROPIC_PAYLOAD test, tests/test_llm_providers.py:57-63, 115-134).

## A6. User-facing strings that mention Zen / OpenCode / specific providers

**Direct answer:** the runtime error message, the entire installer LLM section, the benchmark runner, and a substantial amount of README/docs copy all name Zen/OpenCode. Inventory (grep-verified):

In code (user-visible):

- `ec/llm.py:88-94` — missing-key `LLMError`: "EC needs an OpenCode Zen API key … Get one at https://opencode.ai/auth". Shown for **any** Anthropic-format provider, including direct Anthropic.
- `ec/install.py:278` — `ZEN_KEY_ENV = "OPENCODE_ZEN_API_KEY"`.
- `ec/install.py:280-283` — `ZEN_GUIDANCE` (opencode.ai/auth), printed verbatim when no key (also asserted verbatim against SPEC in tests/test_phase5_install.py:406-411).
- `ec/install.py:284-286` — `"(Fallback: set up Ollama with qwen2.5-coder:14b for offline use.)"`.
- `ec/install.py:292-316` — Ollama constants + messages (model name, `curl -fsSL https://ollama.com/install.sh | sh`, the yaml snippet, the y/n offer).
- `ec/install.py:877-886` — "✓ OpenCode Zen API key found (OPENCODE_ZEN_API_KEY env var)" and "(EC uses Claude Haiku 4.5 via Zen for extraction, diffusion, and mode detection.)".
- `ec/install.py:888-918` — "✗ OPENCODE_ZEN_API_KEY is not set" + Zen guidance + Ollama flow.
- `ec/install.py:926-930` — final transcript: "1. Open your coding agent (Claude Code, Cursor, or Codex)".
- `ec/config.py:176-179` — default `llm` block values (surface as the shipped `~/.ec/config.yaml`).
- `ec/run_ecbench.py:48-49, 69` — docstring "Requires … `opencode run` … OPENCODE_ZEN_API_KEY"; `DEFAULT_AGENT_CMD = "opencode run --dir {repo} --format json --auto {prompt}"` (developer-facing).

In docs:

- `README.md:3` ("works with OpenCode today; Claude Code, Cursor and Codex coming soon"), 20-29 (supported-agents table + installer note), 40-42 (`export OPENCODE_ZEN_API_KEY=...`), 77 (`llm` default description), 84 (Ollama), 89-90 (privacy: "hosted by default, or a local model through Ollama"; "API keys are read from environment variables only").
- `docs/SPEC.md:1994-2016` (tested-models comment + `llm` block incl. endpoint-per-family comment), `2532`, `2599-2601` (Ollama fallback).
- `docs/PRODUCT.md:45, 124, 196` (Mermaid diagram: "call_llm Claude Haiku 4.5 via OpenCode Zen"), `741, 743, 769, 795, 879-880`.
- `docs/TECHNICAL_REFERENCE.md:397, 409-423, 1020`.
- `docs/INSTALLATION.md:24, 49-54, 121-130, 155-168, 192-193, 254-256` (per-harness + key-check flows).
- `AGENTS.md:17` ("llm.py — Zen API client"), `20` ("Prompt-based ECU extraction (Haiku 4.5 via Zen)") — dev-facing but shapes contributor assumptions.
- `CONTRIBUTING.md:27` (test suite "skip unless `OPENCODE_ZEN_API_KEY` is set"), 57, 60.
- `SECURITY.md:21`, `.github/ISSUE_TEMPLATE/bug_report.yml:20`.
- `bench/judge_ecbench.py:36` (judge requires `OPENCODE_ZEN_API_KEY`).

The MCP server itself is provider-clean: its error strings mention only `/ec-start` and extraction failure generically (ec/mcp_server.py:82-93).

## A7. Per-task model choice and cost/token accounting

**Direct answer:** none and none. One model serves all four tasks (`cfg.llm.model` is read inside `call_llm` from the single `llm` block; no per-task override exists in config or code). There is no usage parsing, no token/cost meter, no budget awareness — the only "token" logic in the package is the retrieval payload budget, a character heuristic over retrieved ECUs (ec/retrieval.py:373-410), which measures output size to the *agent*, not LLM spend.

Evidence: `call_llm` signature takes only `prompt/system/temperature/max_tokens/config` (ec/llm.py:67-73); no call site passes a different model; responses' `usage` fields are discarded by both `_call_anthropic` and `_call_openai` (ec/llm.py:141-150, 187-192). The mode-detection call does get its own `max_tokens=16` and temperature (ec/mode_detection.py:90-96), but the same model. A per-task model split (cheap classifier + strong extractor) would require config keys plus a way to pass a model override through `call_llm`.

---

# B. Harness integration

## B8. What `ec install` writes per harness

**Direct answer:** four registry entries (ec/install.py:700), each writing (a) an MCP server entry in the harness's config file and (b) an instructions reference; plus the shared `~/.ec` bootstrap (DB, config.yaml, AGENTS.md, `commands/*.md`). Locations and formats are **broadly current**, with two flagged suspects: Cursor's global rules file and Claude Code's user-scope state file.

Common server command (`server_command`, ec/install.py:380-405): pip-installed → bare `ec-mcp` console script; dev checkout → absolute `<repo>/.venv/bin/python` + `args: ["-m","ec.mcp_server"]` + `env: {PYTHONPATH: <repo root>}`.

| Harness | File(s) written | Content | Merge semantics | Current? |
|---|---|---|---|---|
| Claude Code (ec/install.py:611-630) | `~/.claude.json` | `mcpServers.ec = {"type":"stdio", command,args,env}` | JSON merge: only the `ec` key under `mcpServers` is upserted; siblings/top-level keys preserved; aborts (no write) on invalid JSON (ec/install.py:430-465) | **Mostly.** User-scope MCP servers in `~/.claude.json` is still Claude Code's documented location (`claude mcp add --scope user` writes there — unverified against current docs). Risk: `~/.claude.json` is Claude Code's *state* file, heavily rewritten by the app itself; INSTALLATION.md:72 already notes it "is Claude Code's state file, not an install proof". The merge is correct today but lives in a file Claude Code owns |
| | `~/.claude/CLAUDE.md` | appends the import line `@~/.ec/AGENTS.md` | idempotent line append (`_line_once`, ec/install.py:466-479) | ✅ `@import` syntax in CLAUDE.md is supported |
| Cursor (ec/install.py:633-652) | `~/.cursor/mcp.json` | `mcpServers.ec = {command,args,env}` | same JSON merge | ✅ Global MCP config location is current (Cursor reads `~/.cursor/mcp.json` for global servers — unverified against latest docs, but this is the standard location) |
| | `~/.cursor/rules/ec.md` | `## Engineering Cognition (EC)` reference block naming the 4 tools + user-controlled lifecycle (ec/install.py:577-585) | verbatim create/update (`_verbatim_file`) | ⚠️ **Suspicious.** Cursor's rules system uses per-project `.cursor/rules/*.mdc` files (with `mdc` frontmatter) and *User Rules* stored in app settings; there is no documented global `~/.cursor/rules/*.md` load path (unverified). If unread, the only instruction surface Cursor gets is the MCP tool descriptions themselves — still functional, but the "when to observe/query" guidance is lost |
| OpenCode (ec/install.py:655-679) | `~/.config/opencode/opencode.json` | `mcp.ec = {"type":"local", "command": [<python>, …], "environment": {…}, "enabled": true}` (`env` renamed to `environment` — OpenCode's key name, ec/install.py:666-668); `$schema` added when the file is created fresh | JSON merge under `mcp` | ✅ Current OpenCode local-MCP shape (argv-list command + `environment`) |
| | `~/.config/opencode/AGENTS.md` | appended reference block | idempotent block append (`_block_once`, marker = the AGENTS.md reference, ec/install.py:482-496) | ✅ OpenCode loads global AGENTS.md from there |
| Codex (ec/install.py:682-697) | `~/.codex/config.toml` | `[mcp_servers.ec]` + `command`/`args`/`env = { PYTHONPATH = … }` inline table | byte-preserving table upsert; merged result re-validated with `tomllib`; refuses to write unparseable TOML (ec/install.py:528-574) | ✅ `[mcp_servers.<name>] { command, args, env }` matches Codex CLI's MCP table format (unverified against the very latest Codex releases, which evolve quickly) |
| | `~/.codex/AGENTS.md` | appended reference block | idempotent block append | ✅ Codex reads global instructions from `~/.codex/AGENTS.md` |
| All (ec/install.py:708-737) | `~/.ec/ec.db`, `~/.ec/config.yaml`, `~/.ec/AGENTS.md`, `~/.ec/commands/ec-{start,stop,status}.md` | DB via `Brain()` schema; full DEFAULT_CONFIG yaml if missing (existing user config kept); 209-line agent instructions; three command templates | verbatim re-write when changed (this is the upgrade mechanism) | ✅ |

Transport is stdio for all four; no HTTP/SSE exists (ec/mcp_server.py:1-5; INSTALLATION.md:168). No secrets are ever written — the key is only *read* from the environment for a presence check (INSTALLATION.md:172; ec/install.py:872-918).

## B9. How does the user actually run /ec-start, /ec-stop, /ec-status?

**Direct answer: they can't, as slash commands, in any harness.** The templates go to `~/.ec/commands/` (ec/install.py:732-736), which no harness scans. In practice the user runs the `ec-start` / `ec-stop` / `ec-status` console scripts in a terminal (pyproject.toml:37-39 → `ec.session:main_start/…`), which is exactly what README.md:50-52 instructs. The slash-command story is currently aspirational.

Per harness (command-file locations are general harness knowledge, not repo facts — unverified against each harness's current docs):

- **Claude Code** — user-scope custom slash commands live in `~/.claude/commands/*.md` (project: `.claude/commands/`). EC writes nothing there, so `/ec-start` does not appear. Today: run `ec-start` in the terminal.
- **OpenCode** — custom commands live in `~/.config/opencode/command/*.md` (project: `.opencode/command/`). Note the directory is `command`, not `commands`, and EC's templates do include the `Run: python -m ec.session …` footer that OpenCode-style command files could reference — but nothing is written there. Today: run `ec-start` in the terminal.
- **Codex** — custom prompts live in `~/.codex/prompts/*.md` (invoked as `/prompt-name`). Not written. Today: `ec-start` in the terminal.
- **Cursor** — commands are a newer, primarily project-level feature (`.cursor/commands/*.md`); a global `~/.cursor/commands` path is not clearly established (unverified). Not written. Today: `ec-start` in the terminal.

The installer's final transcript compounds the confusion: it advertises "/ec-start, /ec-stop, /ec-status" as *Commands* (ec/install.py:926-928) while step 1 of "How to use" says "Open your coding agent (Claude Code, Cursor, or Codex)" (ec/install.py:930) — the user can reasonably expect the slash commands to exist inside the agent.

## B10. What Reverie depends on from the harness

**Direct answer:** three things — (1) the harness loading an instruction file that points at `~/.ec/AGENTS.md`, (2) the agent voluntarily calling the four MCP tools at the right moments, (3) the *user* starting/ending sessions via the console scripts. OpenCode provided all three in the tested configuration; the other harnesses provide (1) in varying strength, (2) via MCP (assumed), and (3) only through the terminal. Hooks could automate (3) in some harnesses, but the repo has zero hook integration.

1. **Instruction-file loading.** The per-harness references written by the installer: Claude Code `@~/.ec/AGENTS.md` import in `~/.claude/CLAUDE.md` (ec/install.py:625-628); OpenCode global `AGENTS.md` block (675-678); Codex global `AGENTS.md` block (693-696); Cursor a rules file that is likely unread (see B8). The actual behavioral contract — when to observe, when to query (only after reading code), never to suggest lifecycle commands, how to read confidence/status — lives entirely in `~/.ec/AGENTS.md` (ec/install.py:45-209). If the harness doesn't load the reference, the agent still sees the four tool descriptions (`TOOLS` in ec/mcp_server.py:136-204, which carry abbreviated versions of the same guidance) — weaker but not zero.
2. **Agent calls `ec_observe`/`ec_query`/`ec_reconsolidate`.** This is MCP + model-following-instructions; identical mechanism across harnesses. Nothing harness-specific in the server (stdio NDJSON, ec/mcp_server.py:760-769). The anti-anchoring timing guidance ("read code first, then query") is instruction-file content, so its strength varies per harness exactly as (1).
3. **Session start/end.** Always manual: the MCP server never creates sessions (ec/mcp_server.py:26-29, 82-84: "No active EC session. Suggest the user run /ec-start"). The CLI (`python -m ec.session`, ec/session.py:26-29, 367-464) is the only lifecycle path.

What OpenCode specifically provided during development: the global AGENTS.md load path, the `mcp.ec` local-server config shape (argv command + `environment` key), the headless `opencode run --format json` used by the bench runner (ec/run_ecbench.py:69), and the `OPENCODE_CONFIG` env-var injection used to isolate benchmark conditions (ec/run_ecbench.py:124-165, 319). None of that machinery exists for the other three harnesses — most importantly **no benchmark/E2E runner** for them.

**Hooks (unverified — general harness knowledge, plus grep-verified absence in this repo):** Claude Code has SessionStart/SessionEnd/PreCompact hooks that could invoke `ec-start`/`ec-stop --all-skip` automatically; OpenCode has plugin events (session lifecycle) that could do the same; Codex has a `notify`-style config and Cursor has limited hooking. **Nothing in this repo uses hooks** (grep for "hook" across `ec/`: no code hits — only an unrelated docstring word). Automating lifecycle via hooks would also need a policy decision: the review gate (`/ec-stop`) is interactive in the terminal and `--all-accept`/`--all-skip` bypass review (ec/session.py:388-402) — a hook-driven auto-stop would have to choose one.

## B11. What has actually been tested per harness

**Direct answer:** file-writing for all four (offline, mocked); runtime end-to-end for **OpenCode only**; Claude Code, Cursor, and Codex have never been run against a live EC server by anything in this repo.

- **All four agents — install plans:** `tests/test_phase5_install.py` covers detection by marker/binary for each agent (86-105), per-agent merge semantics — Claude JSON merge + CLAUDE.md import (119-145), Cursor merge + rules file (148-160), OpenCode merge + AGENTS.md + `$schema` (163-189), Codex TOML create/append/replace (192-228) — plus idempotent re-run (422-440), dry-run (346-354), invalid-JSON abort (278-283), `--only` unknown keys (395-397), Zen key messaging (406-411), and a real-subprocess CLI run in a sandboxed home (447-470).
- **MCP server runtime (harness-agnostic):** the stdio NDJSON protocol is tested against a real subprocess launched from a *foreign cwd* using the OpenCode-style config entry (tests/test_phase5_install.py:245-275) and extensively in-process (tests/test_phase4_mcp_server.py).
- **OpenCode end-to-end:** the bench harness drives `opencode run` headlessly through 3 sessions × 10 prompts with per-condition `OPENCODE_CONFIG` injection (ec/run_ecbench.py:1-50, 124-165); the live E2E test (`test_e2e_full_pipeline`, tests/test_e2e_integration.py:5-8) and the 5 other live tests exercise the full pipeline over real LLM calls — all gated on `OPENCODE_ZEN_API_KEY` (6 skips in the current run).
- **Claude Code / Cursor / Codex:** no live test, no E2E, no bench support. README.md:29 states it plainly ("OpenCode is the integration exercised end-to-end"), and INSTALLATION.md §2/§12 concur. Whether their MCP entries actually launch and speak to the hand-rolled server in the real apps is unverified — the *shapes* written are correct, but nothing has proven the apps read them.

## B12. Would Gemini CLI be a small addition?

**Direct answer:** yes — the smallest possible harness addition, roughly one 40-line `AgentSpec` plus a registry entry. No `ec` code outside `install.py` changes.

The pattern to copy is `_ClaudeCode`/`_OpenCode`: an `AgentSpec(key="gemini_cli", name="Gemini CLI", binary="gemini", marker=".gemini/settings.json")` whose `plan()` writes an `mcpServers.ec` stdio entry into `~/.gemini/settings.json` (Gemini CLI's MCP config location — unverified) and a reference block into `~/.gemini/GEMINI.md` (its global context file — unverified). Detection would pick up either the `gemini` binary or the marker. Everything EC-side is already harness-agnostic: the MCP server speaks plain stdio JSON-RPC with `initialize`/`tools/list`/`tools/call` (ec/mcp_server.py:426-439), which Gemini CLI's MCP client supports. Slash-command parity (`~/.gemini/commands/*.toml`) could come later for `/ec-start` parity. The only real cost is the same as for the other untested harnesses: nobody has run it live, so the config shape and the agent's instruction-following are unverified until someone drives a session.

---

# C. Bring-your-own-access design inputs

## C13. Where would a provider registry fit most cleanly?

**Direct answer:** a small static provider table plus an optional `wire_format` config key, inside the existing two-format client. That is option (a)+(c) combined; LiteLLM (b) is the odd one out and costs the most in this codebase.

- **(a) Extend the current two-format client.** The client already isolates every provider-specific byte: URL suffix, headers, body shape are fully contained in `_call_anthropic`/`_call_openai` (ec/llm.py:111-192), and routing is one function (ec/llm.py:58-64). What it lacks is (i) a format override (Claude-on-OpenAI-compat case, A5-1) and (ii) provider defaults. Cost: add an optional `llm.wire_format` key checked before `_api_format`, plus a `PROVIDERS` mapping. ~1–2 files, no new deps, existing tests extend naturally (`test_llm_providers.py` already tests both formats and the informational-provider rule).
- **(b) LiteLLM as a dependency.** Gains: 100+ providers, retries, usage accounting, router fallbacks. Costs in *this* codebase: it pulls a large transitive tree (openai/anthropic/tiktoken/tokenizers…) into a deliberately pinned minimal dependency set (pyproject.toml:13-23 — the package hand-rolls its own MCP server to avoid even the `mcp` SDK, D14/INSTALLATION.md §8); it violates the stated convention "LLM calls go through `ec.llm.call_llm()` — never a direct HTTP call" only if bypassed, but it *replaces* the two-format design decision D48 (docs/handoffs/PHASE_13_AUDIT_FIXES_DESIGN.md:71-88); its own model-name routing re-introduces the same `claude*`-style guessing the codebase just removed; and every one of the 4 call sites + ~30 offline tests mock `call_llm`'s exact contract (string in, string out) — that contract is preserved by LiteLLM's `completion()` only after an adapter. Cost: high dependency + review burden; benefit mostly unrealized because EC needs only 2 wire formats.
- **(c) A small table of providers in config.** A `PROVIDERS` dict (name → base_url, api_key_env, default model, wire format, key-required flag) living in `ec/config.py` next to `DEFAULT_CONFIG` (or a tiny `ec/providers.py`). The installer's LLM section (ec/install.py:872-918) and the missing-key error message (ec/llm.py:88-94) consume it; users write `provider: anthropic` and get everything else filled in; the YAML deep-merge already supports user overrides (ec/config.py:332-352). Cost: lowest — mostly data + installer flow + tests. This is where D48 was already heading ("Adding a provider that speaks OpenAI Chat is a pure config change", ec/llm.py:13-15).

**Recommended shape:** (c) for the registry + (a)'s `wire_format` override key. Registry entries: `opencode_zen`, `anthropic`, `openai`, `google` (OpenAI-compat endpoint), `openrouter`, `ollama`, plus a generic `custom` passthrough (today's behavior).

## C14. Feasibility of a "harness CLI" backend (subscriptions via `claude -p`, `codex exec`, `cursor-agent -p`, `opencode run`)

**Direct answer:** mechanically easy in this codebase — `call_llm` is a single choke point with a string-in/string-out contract, so a third branch that shells out and pipes stdout through `extract_json` fits without touching any call site. The real problems are agentic, not plumbing: recursive MCP loading (the CLI would launch EC itself), tool use that must be disabled, per-call process latency, per-CLI output-envelope parsing, and timeouts.

- **Could `call_llm` shell out?** Yes. The signature `call_llm(prompt, system, temperature, max_tokens, config) -> str` (ec/llm.py:67-73) is the only contract every call site and ~30 offline tests rely on. A `_call_cli` implementation would: build a single-turn command, `subprocess.run(..., capture_output=True, timeout=cfg.llm.timeout)`, extract the assistant text from the CLI's JSON envelope (each CLI has one — `claude -p --output-format json`, `opencode run --format json` — the latter already parsed by the bench runner, ec/run_ecbench.py:69, 197-221), then reuse `extract_json` (ec/llm.py:38-49) exactly as the HTTP paths do. No call site changes. (Flag names quoted here are general CLI knowledge — unverified.)
- **Problems:**
  1. **Recursion.** EC is installed *into* these same harnesses as an MCP server (B8). A `claude -p` spawned by EC would load Claude Code's MCP config, launch a second EC MCP server (with its `MaintainerThread`, ec/mcp_server.py:363-397), which on its own maintenance could spawn… nothing recursive in EC itself (maintenance makes no LLM calls), but the nested server is pure overhead and the *agent* may decide to call EC's tools inside a classification request. Must be disabled per CLI: e.g. `claude -p --strict-mcp-config --mcp-config <empty>`, `codex exec -c mcp_servers={}` (flags unverified). If the user runs *this* flow while EC is installed in that same CLI's config, this is not optional.
  2. **Tool use must be disabled.** These CLIs are agents; given the classification prompt they might try to read files. Single-turn/no-tool flags are needed per CLI (`--max-turns`-style / sandbox read-only / agent-without-tools) (unverified). Otherwise a "classify these 10 pairs" prompt could produce file edits.
  3. **Latency.** Per call: process spawn + CLI startup + model inference (the CLI may run its own system-prompt preamble). EC's tolerance varies by call site (see ranking below). The current 60 s timeout (ec/config.py:180) may be tight for CLI startup on cold caches; a CLI backend needs its own timeout.
  4. **Output format flags.** Each CLI has a different JSON envelope; the text must be pulled from a different field per CLI (and some emit event streams unless a final-text mode is selected). The bench runner already does exactly this parsing for OpenCode (ec/run_ecbench.py:197-221) — a proof the pattern works, for one CLI.
  5. **Working directory / context pollution.** The CLI resolves workspace context from cwd; running it from the session repo is natural (EC knows the repo path, ec/session.py:91-107) but means every utility call loads project context (slower, and the CLI may inject AGENTS.md/CLAUDE.md into its own context, costing tokens).
  6. **System prompt support.** `claude -p` supports `--system-prompt` (unverified); other CLIs vary. Fallback: prepend the system text to the prompt — acceptable for the classification prompts, wasteful for extraction (8.5K-token system prompt would land in the user turn instead).
  7. **Auth/rate limits.** Subscription logins enforce their own rate limits and may refuse headless usage patterns; parallel review-time calls (multiple accepted ECUs diffusing back-to-back) serialize against one CLI session.
- **Which call sites tolerate the latency?** Ranked: (1) full-diffusion classification at `/ec-stop` — the user is already in an interactive terminal review; seconds are fine. (2) contradiction adjudication — rare, small. (3) extraction per `ec_observe` — the agent's response is already complete; a few seconds of post-hoc extraction is acceptable but noticeable if the agent calls observe frequently. (4) lightweight classification — runs once per stored ECU per observe (up to ~5+ calls per observe with today's extraction volume); a CLI backend here would multiply the pain; batching or skipping would be needed. (5) mode detection — should simply stay on the keyword fallback under a CLI backend (it already does whenever no key is set, ec/mode_detection.py:110).

Net: a CLI backend is a viable *opt-in* provider (`provider: harness_cli` + `cli: claude|codex|cursor|opencode`), best limited to extraction + full diffusion, with MCP/tool flags forced and mode detection left on keywords.

## C15. Does the MCP library Reverie uses support sampling?

**No.** Reverie does not use an MCP library at all — the server is hand-rolled NDJSON JSON-RPC 2.0 (D14: "the `mcp` SDK is not in the pinned venv", ec/mcp_server.py:8-10; INSTALLATION.md §8 "zero SDK dependencies"). It implements exactly `initialize`, `ping`, `tools/list`, `tools/call` (ec/mcp_server.py:426-439), declares only `capabilities: {"tools": {}}` (ec/mcp_server.py:429), and is purely reactive — it never sends a request to the client, so there is no path for a `sampling/createMessage` call even at the protocol level. (Whether the official `mcp` Python SDK supports sampling is moot here; if a future design wanted sampling, it would mean either adopting the SDK or hand-implementing client→server requests, plus depending on client support that most major clients reportedly lack.)

## C16. Key handling and installer flow

**Direct answer:** keys are env-only today, and the README's privacy promise is written as "environment variables only" — so a `~/.ec` secrets file would contradict the published sentence and require rewording; a keychain would too, though its spirit (local-only, never committed) is preserved. The installer today knows only two providers (Zen, Ollama) and asks nothing about provider choice.

- **Current behavior:** `api_key()` reads `os.environ[cfg.llm.api_key_env]` at call time (ec/llm.py:52-55); the installer checks presence only and "never writes the key" (ec/install.py:872-918; INSTALLATION.md:172). README promise: "API keys are read from environment variables only; Reverie never writes them anywhere" (README.md:90), reinforced by "nothing from your `~/.ec` is committed" (README.md:91).
- **A `~/.ec/secrets` file:** mechanically trivial (load in `api_key()` with env precedence), and low actual risk — `~/.ec` is outside any repo and explicitly described as machine-local. But it directly contradicts README.md:90's "environment variables only" sentence; the README would need to change, and the "never writes them anywhere" phrasing (the installer does not write keys *today*) would become false as written. A keychain (macOS `security`, libsecret) avoids plaintext-at-rest but changes the same sentence. Recommendation: if either is added, update the README sentence to "keys stay on your machine — env vars by default, optional OS keychain / `~/.ec` secrets file" rather than silently expanding behavior.
- **What `ec install` should ask, in order (design input, not implemented):**
  1. Harness selection (exists today, ec/install.py:822-851).
  2. **Provider selection** (new): detect existing env keys first — `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `OPENCODE_ZEN_API_KEY` — and offer "Detected `ANTHROPIC_API_KEY` — use Anthropic?" before asking anything. Zero-friction default: if exactly one key is present, preselect it.
  3. If none detected: one question, "Where should EC send its LLM calls?" listing the registry providers (C13) plus "Local via Ollama" (auto-selected when the `ollama` binary exists, as today, ec/install.py:890-898) and "Skip — decide later" (extraction stays broken until configured, with the current actionable error).
  4. Write the resolved `llm` block into `~/.ec/config.yaml` (merge, as the Ollama path already does, ec/install.py:319-359) and print the exact `export VAR=…` line for the chosen key.
  5. (Optional) one live "ping" call to validate the key at install time — currently there is no connectivity check of any kind (INSTALLATION.md:29, 136).

---

# Summary tables

## 1. Harnesses

| Harness | Install written | Slash commands visible | Tested end to end | Known gaps |
|---|---|---|---|---|
| OpenCode | ✅ `~/.config/opencode/opencode.json` (`mcp.ec`, argv command, `environment` key) + `~/.config/opencode/AGENTS.md` block | ❌ (commands belong in `~/.config/opencode/command/`; user runs `ec-start` console script) | ✅ **Yes** — bench runner + live E2E + 5 live tests | Marker misses `opencode.jsonc` (INSTALLATION.md:75) |
| Claude Code | ✅ `~/.claude.json` (`mcpServers.ec`, user scope) + `@~/.ec/AGENTS.md` in `~/.claude/CLAUDE.md` | ❌ (user commands live in `~/.claude/commands/`; user runs console script) | ❌ offline install tests only | `~/.claude.json` is CC's own state file (rewrite risk, unverified); no hooks automation; nothing proves the app loads the entry |
| Cursor | ✅ `~/.cursor/mcp.json` + `~/.cursor/rules/ec.md` | ❌ (project `.cursor/commands/` not written; user runs console script) | ❌ offline install tests only | `~/.cursor/rules/*.md` is probably not a Cursor load path (unverified) — instruction surface reduces to MCP tool descriptions |
| Codex | ✅ `~/.codex/config.toml` (`[mcp_servers.ec]`) + `~/.codex/AGENTS.md` block | ❌ (Codex prompts live in `~/.codex/prompts/`; user runs console script) | ❌ offline install tests only | Codex CLI evolves quickly; TOML shape unverified against latest releases |
| Gemini CLI | ❌ not implemented | n/a | ❌ | Would be a small `AgentSpec` addition (B12), untested |

All four supported harnesses: `/ec-start`/`/ec-stop`/`/ec-status` are **not** natively visible anywhere; the real interface is the `ec-start`/`ec-stop`/`ec-status` console scripts (pyproject.toml:37-39) run from a terminal, while the installer's closing transcript advertises the slash form (ec/install.py:926-928).

## 2. Providers

| Provider | Works today by config alone | What breaks | Size of fix |
|---|---|---|---|
| OpenCode Zen | ✅ default (tested) | — | — |
| Ollama | ✅ (tested offline; installer auto-configures) | extraction quality on small local models (per spec comment, unverified) | none — done |
| Anthropic direct | ✅ by config (unverified live) | missing-key error text misleadingly points at opencode.ai/auth (ec/llm.py:88-94) | S — provider-aware error/installer strings |
| OpenAI direct | ✅ for gpt-4o/gpt-4.1-class (unverified live) | o-series/gpt-5 reasoning models reject always-sent `temperature`/`max_tokens` (ec/llm.py:168-173) | S–M — omit or remap params per model family |
| Google Gemini (OpenAI-compat endpoint) | ✅ in principle (unverified) | nothing known | S — registry entry + doc + one live check |
| OpenRouter | ✅ with vendor-prefixed model (`anthropic/claude-…`) (unverified live) | bare `claude-*` model names misroute to Anthropic format (ec/llm.py:58-64) | S — optional `llm.wire_format` override + doc |
| Amazon Bedrock | ❌ | SigV4 auth + per-region URL scheme incompatible with both `_call_*` (ec/llm.py:111-192) | M–L — new adapter + AWS creds |
| Google Vertex | ❌ | OAuth2 token + `:messagesRaw` URL scheme incompatible | M–L — new adapter + gcloud creds |
| Subscriptions (Claude Pro/Max, ChatGPT Plus) | ❌ | not API keys; the APIs reject them; only reachable via harness CLIs | M — opt-in `harness_cli` provider (C14), with recursion/tool flags per CLI |

## 3. The five highest-impact changes for "works with my setup" (ordered by impact per unit of effort)

1. **Provider registry + installer provider selection + provider-aware strings** (C13c, C16). One static table consumed by `call_llm`'s error path and the installer's LLM section; the installer detects existing `*_API_KEY` env vars, offers the matching provider, writes the `llm` block, and stops telling Anthropic-key users to visit opencode.ai/auth. Turns "Zen-only" into "your key, one question" with almost no new machinery.
2. **Optional `llm.wire_format` config override.** ~10 lines in `call_llm` routing + tests. Unblocks Claude models on OpenAI-compatible gateways, bare `claude-*` names on OpenRouter, and any gateway that speaks Anthropic *schema* but wants Bearer auth (A5-1/2) — the single routing flaw today.
3. **Reasoning-model-safe OpenAI parameters.** Omit `temperature`/`max_tokens` when unset, or map `max_tokens`→`max_completion_tokens` for o-series/gpt-5 models. Unlocks the modern OpenAI catalog (A5-3).
4. **Slash-command registration per harness.** Write the three command templates where each harness actually looks (`~/.claude/commands/`, `~/.config/opencode/command/`, `~/.codex/prompts/`) alongside the canonical `~/.ec/commands/` copies (paths unverified per harness). Makes the advertised `/ec-start` real and removes the biggest first-run surprise (B9).
5. **Retry with backoff on 429/5xx + a re-diffuse path for undiffused ECUs.** Transient provider errors currently lose extractions permanently and leave promoted-but-edgeless ECUs with only a printed warning (A2; ASSESSMENT.md:178). A small retry (2–3 attempts, capped backoff) in `call_llm` plus an `ec`-side re-diffuse of `metadata`-flagged undiffused ECUs makes flaky providers non-events.

*(Runner-up worth noting: per-task model config keys — `llm.model_extraction` / `llm.model_classification` — cheap to add once (1) exists, and would let users pair a cheap mode/classifier model with a strong extractor, per A3/A7.)*

---

**Unverified items index:** all external-harness config/command paths and CLI flags (B8, B9, B12, C14); live behavior of Anthropic/OpenAI/Gemini/OpenRouter endpoints (A4); the Haiku-vs-qwen extraction comparison (A3, spec-comment only); Cursor rules load path (B8); OpenAI reasoning-model API behavior (A5-3). Everything else is verified against repo code as of commit `e8fac54` ("Initial public release"), test suite state `506 passed, 6 skipped`.
