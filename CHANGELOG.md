# Changelog

All notable changes to Reverie are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Bring-your-own-model providers** (`ec/providers.py`): a registry of
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
