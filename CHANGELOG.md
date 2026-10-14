# Changelog

All notable changes to Reverie are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — the product flow (`feat/flow`)
- **Per-repo enablement (D55):** every repo is `undecided` / `enabled` /
  `disabled`, stored in the global brain (never inside the repo). The
  first tool call in an undecided repo returns a short, structured
  ask-the-user response ("Reverie isn't on for this repo. Turn it on?
  (yes / not now / never)"); the agent records the answer via the new
  `ec_set_repo_state` MCP tool. `ec enable` / `ec disable` switch it
  directly; `ec repos` lists every known repo with state and counts.
  Disabled = fully off (no capture, no retrieval; data kept).
- **Repo identity (D58):** the git common directory, so worktrees share
  enablement with their main checkout. Moved/re-cloned repos re-match
  by origin remote URL on first use (ambiguous → fresh, resolved by
  `ec enable`). Sessions stay keyed to the worktree path + branch.
- **Automatic sessions (D56):** in an enabled repo any tool call
  resolves or creates the session for (repo, branch) — the D15
  "no active session" error is gone from the product flow. Sessions
  are never closed by the user; branches resolve on every call;
  sessions whose branch no longer exists are archived at lookup and
  their candidates stay in the review queue.
- **Review independent of sessions (D57):** one queue across all
  sessions and repos. New `ec review` terminal UI: one candidate at a
  time, single keypresses (a/r/s/e/g/u/q), $EDITOR edit, contradiction
  pairs shown together, undo of the last reversible decision, pages of
  10 with "more" available. Non-interactive flags: `--list [--json]`,
  `--decide <id>=<accept|reject|skip>`, `--repo/--branch/--all`,
  `--by-topic`, `--expired` (revive expired candidates).
- **Agent review channel (D57):** `ec_review_list` returns the ordered
  page plus a short-lived `review_token`; `ec_review_decide` applies
  only decisions stated by the user, refuses a bulk accept-all, and
  records the channel (`tui`/`agent`/`cli`) in a new `review_decisions`
  audit table.
- **Candidate expiry (D57):** candidates unreviewed for
  `review.expire_after_days` (default 21, 0 disables) become `expired`
  — kept, still usable in session memory, revived only via
  `ec review --expired`.
- **The nudge (D57):** `ec_get_summary` adds one counts-only line when
  the backlog exceeds `review.nudge_threshold` (10) or the oldest
  unreviewed candidate is older than `review.nudge_age_days` (5).
- **Transport (D58):** `ec export [file]` (ZIP: consistent snapshot +
  manifest with versions/dimensions/counts), `ec import <file>`
  (direct when empty, `--replace` otherwise with automatic backup;
  merging not supported), `ec install --import <file>` for
  new-machine setup, and `ec reset [--repo P] [--yes]` (always backs
  up first).
- `ec` is now a multi-command CLI (`ec install` remains the default for
  backwards compatibility with `ec --all` etc.).

### Changed — the product flow (`feat/flow`)
- **Sessions are automatic; the user never starts or stops them.**
  `ec-start` is now a deprecated shim ("enable this repo and show
  status"), `ec-stop` is an alias for `ec review` (old flags refused
  with a note; `--resolve-open-questions` moved to `ec review`),
  `ec-status` unchanged. All docs stop presenting `/ec-start` → work →
  `/ec-stop` as the flow.
- `~/.ec/AGENTS.md` (spec §28.9) rewritten for the new flow: the
  first-time question wording, `repo_path` on every call, review
  rules for the agent (show full text, relay only stated decisions,
  never decide unilaterally, never bulk-accept unasked), the nudge
  rule, and an honesty note that Reverie cannot verify who made an
  agent-channel decision.
- The installer's closing transcript advertises `ec review` and the
  new commands instead of `/ec-start`/`/ec-stop`.
- `review_gate.apply_review_decisions` gained keyword-only
  `close`/`undecided`/`channel` parameters (defaults preserve the old
  behaviour); the session-closing stop path is unchanged for the bench
  harness, which now uses hidden `ec.session bench-start` /
  `bench-close` hooks instead of the user-facing shims.

### Added — bring-your-own-model providers (`feat/providers`)
- **Provider registry** (`ec/providers.py`): a registry of
  `opencode_zen`, `anthropic`, `openai`, `google`, `openrouter`, `ollama`
  and `custom`. Setting `provider` in `~/.ec/config.yaml` fills the rest of
  the `llm` block from the registry; explicit fields still win.
- `llm.wire_format` override (`anthropic`/`openai`) for `claude-*` models
  on OpenAI-compatible gateways.
- Reasoning-model-safe OpenAI requests: `llm.openai_reasoning` (auto by
  default) switches to `max_completion_tokens` and drops `temperature` for
  the o-series / gpt-5 families.
- Retry with capped exponential backoff and jitter on 429/5xx/timeouts,
  honouring `Retry-After` (`llm.retry_attempts` / `retry_backoff` /
  `retry_backoff_cap`).
- No lost extractions: failed extractions are persisted to
  `pending_extractions` and retried at the next successful `ec_observe` or
  via the new `ec-retry` console script (`--list`, `--id`, `--drop`,
  `--rediffuse` for promoted-but-undiffused ECUs).
- Optional per-task models: `llm.models.extraction` / `models.classification`
  / `models.mode_detection` (each falls back to `llm.model`).
- `ec install --provider <name>` (non-interactive provider selection) and
  `--no-verify`; the installer now detects keys for every registry provider,
  offers the detected one, merges the resolved `llm` block, prints the exact
  `export` line, and verifies with one cheap live ping. Keys are still never
  written to disk.

### Changed
- All provider-facing messages (missing-key error, installer output, docs)
  name the configured provider and its key URL instead of assuming OpenCode
  Zen; Zen remains the default provider.
- Live tests are gated on the configured provider's key (any registry
  provider), not only `OPENCODE_ZEN_API_KEY`.
- The D42 Ollama fallback is reachable from the installer's provider menu
  ("Local via Ollama") in addition to the flag.

## [0.1.0] - 2026-10-07

Initial public release.

### Added
- `ec` Python package: ECU/edge/cluster/session persistence in a single
  SQLite file (`~/.ec/ec.db`), prompt-based ECU extraction, Bayesian
  confidence in log-odds space, 9-step and lightweight diffusers, 4-factor
  retrieval ranking with cognitive grouping, session-scoped spreading
  activation, human review gate, background maintainer (decay, grounding
  verification, clustering), reconsolidation, and agent-config installer.
- MCP server (`ec-mcp`) exposing four tools: `ec_observe`, `ec_query`,
  `ec_get_summary`, `ec_reconsolidate`.
- Session lifecycle CLI: `ec-start`, `ec-stop`, `ec-status`, plus `ec-repair`
  for ops recovery and `ec install` for agent configuration.
- Extraction prompt (`ec/prompts/extractor_prompt.md`), MIT-licensed,
  compatible with any coding LLM.
- EC-Bench harness (`bench/`): sequential-session benchmark with LLM judge.
- Design spec (`docs/SPEC.md`), technical reference, product map, and
  installation audit (`docs/`).

[0.1.0]: https://github.com/muditsarda1122/Reverie/releases/tag/v0.1.0
