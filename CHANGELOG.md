# Changelog

All notable changes to Reverie are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-26

First release since 0.1.0. Reverie is early: see the README for known limits.

### Breaking changes
- - Renamed from `ec` to `reverie`. The package, the CLI (`reverie install`, `reverie review`, and so on), the MCP server (`reverie-mcp`), the data folder (`~/.reverie`) and the environment variables (`REVERIE_HOME`, and so on) all use the new names. The 0.1.0 release used the old ones.
- Sessions are automatic per branch. There is no start or stop command.

### Added
- Setup for four agents: Claude Code, Cursor, Codex CLI and OpenCode. `reverie install` configures each one it finds.
- Turn Reverie on or off per repo: `reverie enable`, `reverie disable`, `reverie repos`.
- One review queue across all branches and repos: `reverie review`. Accept, reject or skip each candidate.
- Agents can list and record review decisions, but only decisions you state.
- Export and import of your memory: `reverie export`, `reverie import`.
- `reverie uninstall` removes exactly what install added, and keeps your memory unless you pass `--delete-data`.
- LLM providers: OpenCode Zen (default), Anthropic, OpenAI, Google Gemini, OpenRouter and Ollama.
- `reverie retry` recovers failed extractions. `reverie repair` checks the database.

### Changed
- The rules agents follow are now one shared policy. It's installed for each agent and sent in the server's instructions.
- Re-running `reverie install` refreshes outdated instruction blocks.
- Reverie's read tools are allowed in each agent's permission settings, so they don't prompt. Write tools still prompt.
- Claude Code loads Reverie's tools upfront. This needs Claude Code 2.1.121 or later.
- Codex CLI uses skills (`$reverie-review`, `$reverie-status`) instead of prompts.

### Removed
- Gemini CLI support. It was never verified.
- The `ec-start` and `ec-stop` commands.

### Fixed
- Claude 5.5-family models reject sampling settings. Reverie no longer sends them.
- Failed extractions are kept and retried, not lost.
- A failed import leaves your existing memory unchanged.

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


[Unreleased]: https://github.com/muditsarda1122/Reverie/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/muditsarda1122/Reverie/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/muditsarda1122/Reverie/releases/tag/v0.1.0
