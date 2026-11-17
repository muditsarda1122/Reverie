# Changelog

All notable changes to Reverie are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0]

- **The PyPI package is named `reverie-mem`** (`reverie` was taken). The command is still `reverie`. An install made before the rename keeps working; `reverie --version` and the uninstall hint find either name.

### Changed
- **Install is one command and about a minute.** `curl -LsSf https://raw.githubusercontent.com/muditsarda1122/Reverie/main/scripts/install.sh | sh` (see `scripts/install.sh`) installs `uv` if needed, installs Reverie as an isolated tool and runs `reverie install`. The embedding model now runs on ONNX Runtime instead of PyTorch: the same `all-MiniLM-L6-v2` vectors (cosine 1.0 against the PyTorch output in our check), a ~170 MB install instead of ~1.2 GB, and a ~0.2 s model load instead of ~10 s. The ~90 MB model files are downloaded once by `reverie install` into `~/.cache/reverie/models` (`REVERIE_MODEL_DIR` moves it) and checked against pinned SHA-256 hashes. PyTorch, `transformers`, `sentence-transformers`, `scikit-learn`, `scipy` and `hdbscan` are no longer dependencies: the legacy conclusion-clustering task is skipped without `pip install 'reverie[cluster]'`, and `pip install 'reverie[torch]'` keeps the PyTorch backend. Existing stored embeddings stay valid.
- `reverie install` asks no question when an API key is already set: it uses that provider and says how to change it (`--provider`). With no key it names the one thing to do and finishes the rest of the install. The Ollama install walkthrough is gone from the no-key path (`--provider ollama` still works).
- Remembered conclusions now show their confidence (the stored belief strength after the brain's time decay, e.g. `0.72 (high)`): on the Memory card, on the item page, and in `reverie show`. Items that have no conclusion behind them show nothing, because nothing is measured for them.
- `reverie ui` is redesigned (the "Ink" design, `design.md`). The project board shows only what waits on you; each project has Overview, Rules, Memory, What agents are told, Proposals and Connect tabs; every rule and remembered item has its own page (`/rule`); Search groups Rules and Memory; Add says what will happen to a rule before you add it, with a link to the result; Turned off, Not found and Something went wrong have their own pages (a failure shows no trace). Names come from git identities, with the email in a tooltip. Still no JavaScript and no external resources, and every check the server made before (token, host, origin, size, ownership, secrets, turned off) is unchanged.
- `reverie show` prints more of an item: when it was remembered, last verified, how agents have used it (in words), a challenged line, and for a team rule the conflict reason, the winner and the commit message that made it stale.
- The start text now records which limit cut each rule (line limit, character budget, rules-file limit, whole-text limit) through `deliver.startup_report`; `startup_text` returns exactly what it did.
- Tests run with the `.venv` Python and need the ONNX embedding model on disk; `AGENTS.md` and `CONTRIBUTING.md` say so.

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
