# Harness integrations

Reverie is an MCP server with a small, harness-specific installation layer.
This document is the boundary contract: what each harness gets, what the
shared core guarantees, and how to add another harness without teaching the
core its name.

## Why the split exists

The MCP tools are harness-neutral. Installation is not: every coding harness
has its own config file, instruction-file convention, command format, and (for
Claude Code) hook schema. The integration layer isolates exactly that
knowledge.

- `reverie/core_api.py` is the harness-neutral API used by MCP tools, the
  CLI, and hooks. It knows nothing about a harness.
- `reverie/integrations/` is the only place harness names, paths, config keys,
  and hook schemas appear. Each harness is one package.
- `reverie/install.py`, `reverie/uninstall.py`, and `reverie/cli.py` ask the
  registry for integrations. They do not import a harness package.

The rule is enforced by
`tests/test_integration_layers.py::test_core_does_not_import_harness_packages`
and
`tests/test_integration_layers.py::test_core_has_no_harness_config_paths_or_hook_events`.

## The integration contract

Every package exposes one `INTEGRATION` instance subclassing
`reverie.integrations.base.Integration`:

| Member | Meaning |
|---|---|
| `key` | Stable machine key used by `--only` and config (`claude_code`, `cursor`, `opencode`, `codex`, `gemini_cli`) |
| `name` | Human name shown by the installer |
| `binary` | Executable used for detection (optional) |
| `marker` | Path relative to the user's home that also counts as installed |
| `next_step` | One-line restart/usage hint printed after install |
| `detect(home, which=...)` | Whether the harness is present |
| `install(...)` | Returns a list of `PlannedWrite`; never writes |
| `uninstall(...)` | Returns a list of `PlannedRemoval`; never writes |
| `plan(...)` | Read-only `install` alias used by the installer |
| `configured_server(home)` | Reads back the exact launch command, or `None` |

`install`/`uninstall` are planners. The installer and uninstaller print or
apply the returned plans, so `--dry-run` and the real run share one code path.
`dry_run` is accepted for callers that prefer the contract switch; integrations
may ignore it.

## Coverage table

| Harness | MCP config | Instruction reference | Commands / prompts | SessionStart | Stop capture | Detection | Smoke-tested |
|---|---|---|---|---|---|---|---|
| **Claude Code** | `~/.claude.json` → `mcpServers.reverie` (`type: stdio`) | `~/.claude/CLAUDE.md` → `@~/.reverie/AGENTS.md` | `~/.claude/commands/reverie-review.md`, `reverie-status.md` | ✅ `settings.json` → `hooks.SessionStart`, matcher `startup` | ✅ `settings.json` → `hooks.Stop`, async, 20s timeout | `claude` binary or `.claude.json` | ❌ offline install/hook tests only |
| **Cursor** | `~/.cursor/mcp.json` → `mcpServers.reverie` | None (Cursor documents no global rules file; MCP `instructions` carries the contract) | `~/.cursor/commands/reverie-review.md`, `reverie-status.md` (beta) | — | — | `cursor` binary or `.cursor/mcp.json` | ❌ offline install tests only |
| **OpenCode** | `~/.config/opencode/opencode.json` → `mcp.reverie` (`type: local`, `enabled: true`, `environment`) | `~/.config/opencode/AGENTS.md` reference block | `~/.config/opencode/commands/reverie-review.md`, `reverie-status.md` | — | — | `opencode` binary or the config | ✅ offline tests + live E2E + EC-Bench |
| **Codex CLI** | `~/.codex/config.toml` → `[mcp_servers.reverie]` | `~/.codex/AGENTS.md` reference block | `~/.codex/prompts/reverie-review.md`, `reverie-status.md` (`/prompts:`) | — | — | `codex` binary or `.codex/config.toml` | ❌ offline install tests only |
| **Gemini CLI** | `~/.gemini/settings.json` → `mcpServers.reverie` | `~/.gemini/GEMINI.md` reference block | `~/.gemini/commands/reverie-review.toml`, `reverie-status.toml` | — | — | `gemini` binary or `.gemini/settings.json` | ❌ offline install tests only |

“Smoke-tested” is the same honest column as the README: for Claude Code,
Codex, Cursor, and Gemini CLI the written shapes are checked against the
vendors' current docs and the server is tested over real stdio, but nobody has
driven a live session. OpenCode is the only harness exercised end-to-end.
Use [`SMOKE_TEST.md`](SMOKE_TEST.md) for the manual checklist.

## Lifecycle hooks

Only Claude Code has a documented, reliable context-injection surface that
fits Reverie's demand-driven model. The installer writes two hooks; the hook
implementations themselves are harness-neutral.

### SessionStart

`reverie hook session-start` is installed in `~/.claude/settings.json` with
matcher `startup`. Its plain-text stdout is injected before the first prompt:

- **enabled** — brain statistics (conclusions, candidates) plus the usage
  rules, never conclusion text;
- **undecided** — the first-time enablement question for the agent to relay;
- **disabled** — a one-line "do not capture or retrieve here".

The hook is read-only: no LLM, no embeddings, no session creation, no writes.
A missing or unreadable brain prints nothing and exits `0`. The stats line is
`core_api.summary(..., read_only=True)["message"]`, so the hook cannot drift
from the MCP `summary` tool.

### Stop

`reverie capture-turn` is installed in `~/.claude/settings.json` as an async
Stop hook with a 20-second timeout. Claude sends `transcript_path`, `cwd`, and
`last_assistant_message` on stdin. The command:

1. reads the last real user request and final assistant text from the Claude
   JSONL transcript (skipping sidechains, meta entries, tool calls, tool
   results, and thinking blocks; falls back to `last_assistant_message`);
2. resolves the repo and branch (hook payload, `--repo`/`--branch`, or
   detection);
3. calls `core_api.capture_turn`.

The Stop hook is fire-and-forget: it prints nothing, never blocks Claude,
and always exits `0`. A disabled/undecided repo is a no-op. A failed or
timed-out extraction is left in the pending queue and can be retried with
`reverie retry`.

### What is deliberately not wired

- No `UserPromptSubmit` or `PreToolUse` hook. Reverie does not inject
  conclusions mid-session; the agent retrieves on demand with `query`.
- No `SessionEnd` hook. Context injection at session end is unusable, and
  capture already happens at Stop.
- No `SubagentStop` hook. The main Stop transcript contains the final answer;
  duplicating subagent turns would add noise.
- Cursor, OpenCode, Codex, and Gemini CLI have no context-injection hook in
  this branch. Their `instructions` / rule-file guidance is loaded at session
  start by the harness; retrieval remains demand-driven.

## Install and uninstall semantics

Both commands are plan-first.

- Every planned write is shown before it is applied; `--dry-run` prints the
  same plan and applies nothing.
- Existing config is merged, never replaced: siblings in
  `~/.claude.json`, `opencode.json`, `settings.json`, `config.toml`, and user
  files are preserved.
- Reverie-owned command/prompt files are written verbatim. Uninstall deletes
  one only when its content still exactly matches what Reverie wrote.
- Instruction references are appended once and removed as an exact block or
  line. An edited block is skipped and reported, never guessed at.
- `~/.reverie/install-manifest.json` records files created by install with
  their SHA-256. A file is deleted on uninstall only when install created it
  and it is still unedited; otherwise only Reverie's entry is removed.
- Invalid JSON/TOML is never rewritten. The installer stops with the path and
  the fix, and changes nothing.

## Per-harness notes

**Claude Code** — `~/.claude.json` is also Claude Code's state file; the merge
touches only `mcpServers.reverie`. The Stop hook reuses the SessionStart
handler's launch form (exec `args` or shell command), so dev-checkout and
pip-installed layouts both work. `CLAUDE.md` receives a single
`@~/.reverie/AGENTS.md` import line.

**Cursor** — no global rules file is written. Cursor's documented global
mechanism is in-app User Rules; the MCP server's `instructions` and tool
descriptions carry the usage contract. Command files are beta.

**OpenCode** — MCP entries use `mcp.reverie` with `{"type": "local",
"enabled": true}` and `environment` instead of `env`. The config gets
`$schema: https://opencode.ai/config.json` when created.

**Codex CLI** — `config.toml` gets a real `[mcp_servers.reverie]` table. User
prompts under `prompts/` are deprecated upstream but still the user-level
command mechanism.

**Gemini CLI** — commands are TOML with a required `prompt` string, not
markdown. Gemini CLI was replaced by Antigravity CLI for unpaid/Google One
users in June 2026; the config format is unchanged.

## Adding a harness

1. Create `reverie/integrations/<name>/__init__.py` and subclass
   `Integration`: set `key`, `name`, `binary`, `marker`, `next_step`.
2. Implement `install`, `uninstall`, and `configured_server` with the shared
   planners in `reverie/integrations/common.py`
   (`_json_merge`, `_block_once`, `_verbatim_file`, `_plan_json_entry`,
   `_plan_block`, `_plan_verbatim_file`, and friends).
3. Instantiate it as `INTEGRATION` and add it to `INTEGRATIONS` in
   `reverie/integrations/__init__.py` (and export any command constants).
4. Add offline tests for detect/install/uninstall/idempotency and a row in the
   coverage table. If the harness has hooks, add their exact schema to the
   package — never to the core.

## Verification and open questions

Claude Code hook documentation was read on **2026-10-21**:

- overview and event list: <https://code.claude.com/docs/en/hooks>
- `SessionStart`: <https://code.claude.com/docs/en/hooks#sessionstart>
- `Stop`: <https://code.claude.com/docs/en/hooks#stop>

What the docs confirm: `SessionStart` stdout and
`hookSpecificOutput.additionalContext` are added to the session context;
`Stop` receives `transcript_path`, `cwd`, and `last_assistant_message`, and
`additionalContext` from a Stop hook continues the conversation. Reverie uses
the former for the read-only status block and the latter only for background
capture — it prints nothing, so it never extends a conversation.

The honest promise: Reverie does not secretly read conversations, does not
inject memories mid-session, does not modify your repositories, and does not
overwrite unrelated config. Retrieval is demand-driven; the agent must ground
itself in the current code before calling `query`, and query results are past
beliefs to verify, not authoritative context. Four of the five harnesses are
verified offline only.

Open decisions:

- (a) whether mid-session relevance hooks are worth building if the docs confirm context injection;
- (b) whether reasoning_trace in observe should be stored;
- (c) whether reconsolidation on accepted conclusions needs user confirmation;
- (d) how near-duplicate candidates are merged.
