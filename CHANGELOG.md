# Changelog

All notable changes to Reverie are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — integration layers (`feat/integration-layers`)
- **Integration layer.** Each supported harness is now its own package;
  install and uninstall are plan-first, so `--dry-run` and the real run
  share one code path, and adding a harness no longer touches the core.
- **Claude Code: tools load upfront.** The MCP server entry now sets
  `alwaysLoad: true` (Claude Code 2.1.121+), so Reverie's tools are
  available immediately instead of being deferred — the agent can call
  `query` before its first edit.
- **Codex CLI: skills instead of prompts.** `/reverie-review` and
  `/reverie-status` are installed as skills under `~/.agents/skills/`
  (Codex 0.163 no longer loads `~/.codex/prompts/`); install cleans up old
  prompt files it wrote.
- **Usage policy, one source.** The agent-facing usage rules (query before
  editing code, reconsolidate when the code contradicts a retrieved
  conclusion, review only through you) now live in one place, surfaced
  through the installed instructions, the MCP server instructions and the
  tool descriptions so every harness gets the same contract.
- **Permission setup.** `reverie install` allows Reverie's read tools
  through each harness's permission config (Claude Code
  `permissions.allow`, Codex per-tool approvals, Cursor CLI
  `permissions.allow`); write tools still ask.
- **Stale instruction blocks are refreshed.** Re-running
  `reverie install` replaces an inline instruction block written by an
  older version, so the usage rules stay current after an upgrade.

### Removed — integration layers (`feat/integration-layers`)
- **Gemini CLI support.** Gemini CLI is not supported and has been removed
  from the installer and docs; nothing about it was ever verified.

### Changed — Phase 3 (`feat/reverie`)
- **The rename.** Everything a user sees moved from `ec` to `reverie`: the
  package (`reverie`), the CLI (`reverie` with subcommands `install`,
  `enable`, `disable`, `repos`, `review`, `status`, `export`, `import`,
  `reset`, `retry`, `repair`, `uninstall`), the MCP server (`reverie-mcp`),
  the seven MCP tools (`observe`, `query`, `summary`,
  `reconsolidate`, `set_repo_state`, `review_list`,
  `review_decide`), the data directory (`~/.reverie`), the database
  (`reverie.db`), the env vars (`REVERIE_HOME`, `REVERIE_REPO_PATH`,
  `REVERIE_BRANCH`) and the PyPI name (`reverie` — verified free;
  `reverie-memory` is an unrelated package). One clean rename — no
  compatibility aliases. `ec-repair` and `ec-retry` folded into
  `reverie repair` / `reverie retry`; the deprecated `ec-start` / `ec-stop` /
  `ec-status` console scripts removed entirely.
- **User-level slash commands per harness** — `/reverie-review` (list +
  relay review decisions) and `/reverie-status` installed in each harness's
  own location and format: `~/.claude/commands/*.md`,
  `~/.config/opencode/commands/*.md`, `~/.cursor/commands/*.md` (beta),
  and skills under `~/.agents/skills/` for Codex.
- **MCP `instructions` field** — the initialize response now carries the
  core usage contract (query after reading code; results are past
  understanding to verify; never decide reviews for the user; ask once per
  repo). Documented as surfaced by Claude Code and Codex;
  captured by recent OpenCode; not documented by Cursor — where no
  instruction file is read, this is the fallback.
- **Installer honesty summary** — `reverie install` ends with a per-harness
  list of exactly what was written plus the next step in that harness.
- `docs/CLI.md` — a full reference for every command and flag, with a test
  that fails when the parser grows undocumented surface. `docs/SMOKE_TEST.md`
  — the manual per-harness smoke-test checklist.

### Fixed — Phase 3 (`feat/reverie`)
- **Newer Claude models reject Reverie's requests.** Claude 5.5-family
  models (Opus/Sonnet/Haiku 5.5, plus Opus 4.7/4.8/5 and Sonnet 5) return
  HTTP 400 for any non-default `temperature`/`top_p`/`top_k`; Reverie always
  sent `temperature`. The request now omits sampling parameters for that
  family (one conservative model-name rule, forceable with
  `llm.claude_no_sampling`) and carries the cheapest valid thinking
  configuration (Haiku: `disabled` at effort low; Sonnet: `between_tools`;
  Opus: cannot disable — effort only). Source checked 2026-10-15:
  platform.claude.com thinking + Haiku 5.5 migration guide.
- **Cursor global rules** — the installer no longer writes
  `~/.cursor/rules/ec.md` (Cursor documents no global rules file load
  path); the MCP instructions + tool descriptions carry the contract, and
  users can paste guidance into Settings → Rules.
- **Uninstall asks first.** `reverie uninstall` shows the full plan (harness
  entries + data plan) and takes ONE confirmation before touching anything;
  aborting leaves everything byte-identical; `--yes` skips the prompt but
  never the export-before-delete rule.


### Added — the product flow (`feat/flow`)
- **Per-repo enablement (D55):** every repo is `undecided` / `enabled` /
  `disabled`, stored in the global brain (never inside the repo). The
  first tool call in an undecided repo returns a short, structured
  ask-the-user response ("Reverie isn't on for this repo. Turn it on?
  (yes / not now / never)"); the agent records the answer via the new
  `set_repo_state` MCP tool. `reverie enable` / `reverie disable` switch it
  directly; `reverie repos` lists every known repo with state and counts.
  Disabled = fully off (no capture, no retrieval; data kept).
- **Repo identity (D58):** the git common directory, so worktrees share
  enablement with their main checkout. Moved/re-cloned repos re-match
  by origin remote URL on first use (ambiguous → fresh, resolved by
  `reverie enable`). Sessions stay keyed to the worktree path + branch.
- **Automatic sessions (D56):** in an enabled repo any tool call
  resolves or creates the session for (repo, branch) — the D15
  "no active session" error is gone from the product flow. Sessions
  are never closed by the user; branches resolve on every call;
  sessions whose branch no longer exists are archived at lookup and
  their candidates stay in the review queue.
- **Review independent of sessions (D57):** one queue across all
  sessions and repos. New `reverie review` terminal UI: one candidate at a
  time, single keypresses (a/r/s/e/g/u/q), $EDITOR edit, contradiction
  pairs shown together, undo of the last reversible decision, pages of
  10 with "more" available. Non-interactive flags: `--list [--json]`,
  `--decide <id>=<accept|reject|skip>`, `--repo/--branch/--all`,
  `--by-topic`, `--expired` (revive expired candidates).
- **Agent review channel (D57):** `review_list` returns the ordered
  page plus a short-lived `review_token`; `review_decide` applies
  only decisions stated by the user, refuses a bulk accept-all, and
  records the channel (`tui`/`agent`/`cli`) in a new `review_decisions`
  audit table.
- **Candidate expiry (D57):** candidates unreviewed for
  `review.expire_after_days` (default 21, 0 disables) become `expired`
  — kept, still usable in session memory, revived only via
  `ec review --expired`.
- **The nudge (D57):** `summary` adds one counts-only line when
  the backlog exceeds `review.nudge_threshold` (10) or the oldest
  unreviewed candidate is older than `review.nudge_age_days` (5).
- **Transport (D58):** `reverie export [file]` (ZIP: consistent snapshot +
  manifest with versions/dimensions/counts), `reverie import <file>`
  (direct when empty, `--replace` otherwise with automatic backup;
  merging not supported), `reverie install --import <file>` for
  new-machine setup, and `reverie reset [--repo P] [--yes]` (always backs
  up first).
- `ec` is now a multi-command CLI (`reverie install` remains the default for
  backwards compatibility with `reverie --all` etc.).
- **`reverie uninstall`** removes exactly what `reverie install` did, and nothing
  more: Reverie's MCP entries, import lines, reference blocks and
  command files are removed from every supported harness (same
  merge-only logic in reverse; invalid or edited files are skipped and
  reported, never damaged). Your memory (`~/.reverie`) is kept by default;
  `--delete-data` takes a final export outside `~/.reverie` first (its path
  is printed; `--no-export` skips it), asks you to type `delete`, and
  only then removes it. `--dry-run` prints every planned removal with
  the exact before/after and writes nothing. The last step prints the
  exact `pip uninstall` / pipx / uv command to run by hand.
- **`reverie status`** — the `ec-status` report as a subcommand.

### Fixed — the product flow (`feat/flow`)
- **Import is atomic.** A failed `reverie import --replace` (missing table,
  column mismatch, disk full) used to leave the local brain wiped; the
  delete and the insert now run in ONE transaction with rollback — a
  failed import leaves the brain exactly as it was.
- **Import is schema-robust.** The extracted snapshot is upgraded
  through the normal schema/migration path before copying, and tables
  are copied with explicit column lists (intersection by name) instead
  of `INSERT ... SELECT *` — an export from an older schema, or a
  database whose column order was moved by `ALTER TABLE ... ADD
  COLUMN`, imports correctly; tables absent from the export are skipped
  and reported.
- **Foreign-key checks are real.** `PRAGMA foreign_key_check` runs
  BEFORE the commit in `clear_all_data` and the import copy, and
  violations raise a real exception that rolls everything back (an
  `assert` after the commit was stripped under `python -O` and checked
  too late anyway).
- **Review tokens are single-use.** A successful `review_decide`
  consumes the token, so the "no bulk accept-all" guard cannot be
  bypassed by accepting part of a listing, then the rest with the same
  token in a second call — the next batch requires a fresh
  `review_list`. This adds friction; it is not a security boundary
  (the docs say so).
- **The nudge line matches the listing.** `summary`'s review
  backlog line splits the count when a repo is known — "14 conclusions
  waiting for review (3 in this repo, oldest 6 days)" — because
  `review_list` defaults to the current repo; outside a repo it
  stays global-only.
- **Bare `reverie` shows help** instead of running the installer, and a
  mistyped first word (`reverie reveiw`) is an error with a "did you mean"
  hint. Invocations whose first argument starts with `-` (`reverie --all`,
  `reverie --only ...`) still go to the installer. `ec review --list` and
  friends reach the review CLI again.
- **`reverie reset --repo` covers worktrees.** Sessions are keyed by
  worktree path while repo identity is the git common dir; the reset
  now resolves the argument to the identity, deletes every worktree's
  sessions and prints which paths were included.
- `reverie install` detects a stale `ec` console-script binding (pre-
  subcommand installs bound `ec` to the installer) and says to re-run
  `pip install -e .` — console scripts only update on reinstall.

### Changed — the product flow (`feat/flow`)
- **Sessions are automatic; the user never starts or stops them.**
  `ec-start` is now a deprecated shim ("enable this repo and show
  status"), `ec-stop` is an alias for `reverie review` (old flags refused
  with a note; `--resolve-open-questions` moved to `reverie review`),
  `ec-status` unchanged. All docs stop presenting `/ec-start` → work →
  `/ec-stop` as the flow.
- `~/.reverie/AGENTS.md` (spec §28.9) rewritten for the new flow: the
  first-time question wording, `repo_path` on every call, review
  rules for the agent (show full text, relay only stated decisions,
  never decide unilaterally, never bulk-accept unasked), the nudge
  rule, and an honesty note that Reverie cannot verify who made an
  agent-channel decision.
- The installer's closing transcript advertises `reverie review` and the
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
  via the new `reverie retry` console script (`--list`, `--id`, `--drop`,
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
