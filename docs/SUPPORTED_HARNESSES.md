# Supported harnesses

Reverie is tested against the harness versions below. Each release updates
this table. If a harness changes how it loads instructions, commands or
permissions, the change goes in the same commit as the Reverie change that
handles it.

Tests were run on 2026-10-25. "Memory use" means the agent called Reverie's
`query` tool before its first edit in a test prompt. One prompt per harness,
in one repo, so treat it as a smoke test, not a benchmark.

| Harness | Tested version | Reverie writes | Verified |
|---|---|---|---|
| Claude Code | 2.1.310 | `~/.claude.json` (MCP server, `alwaysLoad`), `~/.claude/CLAUDE.md`, `~/.claude/commands/`, `~/.claude/settings.json` (hooks and `permissions.allow`) | MCP tools load upfront; memory use |
| Codex CLI | 0.163.0 | `~/.codex/config.toml` (MCP server and per-tool approvals), `~/.codex/AGENTS.md`, `~/.agents/skills/` | MCP tools; skills (`$reverie-status`); memory use |
| Cursor CLI | 2026.10.22-27dcf5f | `~/.cursor/mcp.json`, `~/.cursor/cli-config.json` (`permissions.allow`), `~/.cursor/commands/` | MCP tools and commands; memory use |
| OpenCode | 1.18.35 | `~/.config/opencode/opencode.json`, `~/.config/opencode/AGENTS.md`, `~/.config/opencode/commands/` | MCP tools; memory use with `glm-5.3-flash` and `claude-haiku-5-5` |

## Hooks and transcripts (spike, 2026-11-04)

How Reverie gets prompts in and rules out of each harness. "Fires" was tested
by installing a logging hook in a throwaway directory.

| | Claude Code | opencode 1.18.35 | Cursor CLI 2026.10.22 | Codex CLI 0.163.0 |
|---|---|---|---|---|
| Hook mechanism | `settings.json` command hooks | JS plugin (`~/.config/opencode/plugin/*.js`) | `hooks.json` (user or project) | `[[hooks.*]]` in `config.toml` or `hooks.json` |
| Fires headless | yes (sync hooks; async ones are cancelled by `-p`) | yes (`opencode run`) | `sessionStart` yes (`-p --trust`); prompt/stop **not tested**: the account hit its usage limit before a prompt was submitted | **no until the user approves each hook** (see below) |
| Fires interactive | yes | not tested (plugin events are the same in TUI and `run`) | not tested | not tested |
| Trust step | none | none | none (`--trust` trusts the workspace, not hooks) | **yes**: every hook is `untrusted` until a `trusted_hash` is persisted; unapproved hooks are skipped silently in `exec` (no warning). Approval is per hook and per hash: editing the command re-triggers it. |
| Payload | prompt, answer, `transcript_path`, session id | `chat.message`: user text (JSON-quoted for `run "msg"`), `sessionID`, model; `session.idle` has only the session id | `sessionStart`: `conversation_id`, `workspace_roots`, `model`, `transcript_path: null` | not observed |
| Latency (hook cost) | 0.3-0.5 s per hook | plugin spawns a detached shim: ~0 on the agent's path; the system-prompt hook runs `start` synchronously (~0.3 s, once per process) | not measured | not measured |
| Can add context for the model | `additionalContext` | `experimental.chat.system.transform` (verified: model quoted the rules) | `sessionStart` can return context (not verified) | `additionalContext` (not verified) |
| Transcript on disk | `<config>/projects/<slug>/*.jsonl` | `opencode.db` (SQLite: session, message, part) | `~/.cursor/projects/<slug>/agent-transcripts/*/*.jsonl` | `$CODEX_HOME/sessions/Y/M/D/rollout-*.jsonl` |
| **What Reverie does** | hooks + importer | **plugin + importer** (MCP fallback) | importer + MCP; hooks later, once they can be verified | importer + MCP; hooks after the user approves them |

Decision: transcripts are the base layer for every harness (they cover
headless, scripted and killed sessions and need no approval). Hooks are added
where the spike showed they fire without a trust step, because they are
deterministic: they do not depend on the agent choosing to call an MCP tool.

Importer readers (all stdlib, read-only, redact before storing, cursor =
file + offset so nothing is read twice): Claude, Codex, opencode (only the
session/message/part tables, never the credential tables), Cursor.

## Verifying Cursor hooks (needs an account with usage left)

Cursor's `sessionStart` hook was seen firing headless. The prompt, stop and
context-injection hooks were not, because the test account was over its usage
limit before a prompt was submitted, so Reverie does not install Cursor hooks
yet (the transcript importer and MCP cover Cursor). To finish the check:

1. In a throwaway git repo create `.cursor/hooks.json` with `beforeSubmitPrompt`,
   `stop` and `sessionStart` entries that run a script appending stdin to a file.
2. Run `cursor-agent -p --trust "say pong"` and read the file: expect the three
   events, a `conversation_id`, and for `stop` the final message or a `transcript_path`.
3. Make the `sessionStart` hook print `{"additional_context": "the secret word is plum"}`
   and ask the agent "what is the secret word?": if it answers, context injection works.

If all three hold, add a `cursor` hooks block mirroring `integrations/opencode`
(stdlib shim + installer entry) and move Cursor to "hooks + importer" in the README table.

## Version notes

- **Claude Code** needs 2.1.121 or later for `"alwaysLoad": true`. Without it,
  Reverie's tools are deferred and the agent may not call `query`.
- **Codex CLI 0.163.0** no longer loads custom prompts from `~/.codex/prompts/`.
  Reverie installs skills instead. Older Codex versions need the previous install.
  Reverie's `query`, `summary` and `review_list` tools are approved per tool;
  the write tools prompt for approval.
- **Cursor CLI** allows Reverie's tools through `Mcp(reverie:*)`. Reverie does
  not change the `approvalMode` setting.
- **OpenCode** loads Reverie's instructions from the MCP server and from
  `AGENTS.md`. Memory use was verified with the models named above.

## Not supported

- **Gemini CLI** is not supported in this release. Nothing has been tested.