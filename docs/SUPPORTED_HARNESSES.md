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