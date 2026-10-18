# Installation & Harness Integration

**Last verified against vendor docs: 2026-10-15.** Each harness section cites
the exact documentation pages the install shapes were checked against.

Reverie is installed once per machine and configured once per harness. It is
a stdio MCP server (one subprocess of the agent) plus a global data
directory (`~/.reverie/`). No daemons, no background services beyond a
maintenance thread inside that subprocess.

## Requirements

- Python ≥ 3.12 (pinned heavy dependencies ship 3.12 wheels)
- pip, git
- An LLM provider key (any registry provider) **or** Ollama for local-only

## The `~/.reverie` directory

| File | What it is |
|---|---|
| `reverie.db` | the brain — one SQLite file for all projects |
| `config.yaml` | config (created with defaults; your edits are kept) |
| `AGENTS.md` | the agent instructions (written verbatim; safe to re-run install) |

`REVERIE_HOME` relocates the whole directory. `REVERIE_REPO_PATH` +
`REVERIE_BRANCH` override repo/branch detection for MCP launches outside a
work tree.

## `reverie install`

```bash
reverie install                    # interactive
reverie install --all              # every detected harness, non-interactive
reverie install --only claude_code,codex --provider anthropic --no-verify
reverie install --reconfigure      # ask the provider question again
reverie install --dry-run          # print every planned write, change nothing
reverie install --import memory.zip  # new machine: restore an export
```

`--provider <key>` configures an LLM provider without prompting (see the
provider table below); `--reconfigure` asks the provider question again even
when `config.yaml` already carries an `llm` block (by default an existing
block is kept and only re-verified); `--no-verify` skips the live ping.
After the provider is configured the installer pings it once with the model
name read from the written config. A failed ping makes the install **exit
non-zero** with "Reverie is installed, but the LLM is not reachable." — the
config and harness entries are still written; fix the key/model and re-run.

| Provider (`--provider`) | Key environment variable | Default model |
|---|---|---|
| `opencode_zen` | `OPENCODE_ZEN_API_KEY` | `claude-haiku-5-5` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-5-5` |
| `openai` | `OPENAI_API_KEY` | `gpt-5-mini` |
| `google` | `GEMINI_API_KEY` (alias `GOOGLE_API_KEY`) | `gemini-3.8-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `anthropic/claude-haiku-5.5` |
| `ollama` | — (local, no key) | `qwen2.5-coder:14b` |

`custom` has no installer flow: set `llm.*` by hand in
`~/.reverie/config.yaml`. API keys are never written to disk — the
installer only reads the environment. When a key is missing it says so
(the variable name, that it checked this process's environment, and that a
new terminal or `source ~/.zshrc` / `source ~/.bashrc` makes an exported
key visible).

What it does, in order:

1. **Detects harnesses** — binary on PATH or the harness's config file exists.
2. **Selects** — interactive picker, `--all`, or `--only` (keys:
   `claude_code`, `codex`, `cursor`, `opencode`, `gemini_cli`).
3. **Per-harness writes** — see the table below. All merges are idempotent:
   only Reverie's own entry is added or replaced, every other key/line/byte
   is preserved, and a malformed file aborts with no changes to it.
4. **Bootstraps `~/.reverie`** (DB with the full schema, default config,
   agent instructions).
5. **LLM provider step** — detects registry provider keys in the
   environment, offers the detected one as the default, merges the resolved
   `llm` block into `~/.reverie/config.yaml`, prints the exact `export`
   line, and pings the provider once with the configured model name. A
   failed ping is reported and the install **exits 1**. If `config.yaml`
   already contains an `llm` block it is kept and only re-verified —
   `--provider` or `--reconfigure` are the explicit ways to change it. Keys
   are never written to disk.
6. **Per-harness summary** — exactly what was written and the next step in
   that harness.

Re-running `reverie install` is safe and is the upgrade path: entries
upsert idempotently, owned files refresh verbatim, your `config.yaml` is
kept. Every file the installer *creates* is recorded (path + hash) in
`~/.reverie/install_manifest.json`, which lets `reverie uninstall` remove
exactly what it made; a file that existed before install is never deleted —
uninstall only removes Reverie's entry from it.

## What gets written per harness

| Harness | MCP server (user scope) | Instructions | Slash commands |
|---|---|---|---|
| **Claude Code** | `~/.claude.json` → top-level `mcpServers.reverie` (`type: stdio`) | `@~/.reverie/AGENTS.md` import appended to `~/.claude/CLAUDE.md` | `~/.claude/commands/reverie-review.md`, `reverie-status.md` |
| **Codex CLI** | `~/.codex/config.toml` → `[mcp_servers.reverie]` | block appended to `~/.codex/AGENTS.md` | `~/.codex/prompts/reverie-review.md`, `reverie-status.md` (invoked `/prompts:reverie-review`) |
| **Cursor** | `~/.cursor/mcp.json` → `mcpServers.reverie` | **none written** — Cursor documents no global rules file; guidance arrives via the server `instructions` + tool descriptions (paste into Settings → Rules if you want it always on) | `~/.cursor/commands/reverie-review.md`, `reverie-status.md` (beta) |
| **OpenCode** | `~/.config/opencode/opencode.json` → `mcp.reverie` (`type: local`, argv-list `command`, `environment` key) | block appended to `~/.config/opencode/AGENTS.md` | `~/.config/opencode/commands/reverie-review.md`, `reverie-status.md` |
| **Gemini CLI** | `~/.gemini/settings.json` → `mcpServers.reverie` | block appended to `~/.gemini/GEMINI.md` | `~/.gemini/commands/reverie-review.toml`, `reverie-status.toml` (TOML `prompt`) |

The server entry itself (pip-installed — pipx, uv tool, venv or system):

```json
{"command": "/absolute/path/to/reverie-mcp"}
```

The installer resolves the **absolute path** of the `reverie-mcp` script
belonging to the running interpreter's environment
(`Path(sys.executable).parent / "reverie-mcp"`, `Scripts\reverie-mcp.exe`
on Windows, `shutil.which` fallback). Harnesses launch MCP servers outside
the user's virtualenv, so a bare name would only work if it happened to be
on the global PATH — with a venv or pipx install it is not.

Dev checkout (not pip-installed): the absolute venv-python form with
`args: ["-m", "reverie.mcp_server"]` and `env.PYTHONPATH: <repo root>`.

At the end of the install the exact command written to each config is
launched, sent an MCP `initialize` and checked (≤10 s); ✓/✗ is printed per
harness and a change that doesn't produce a server is reported as
"Setup incomplete" (never "Reverie is ready!"). `reverie status` re-checks
that the configured command still exists and says to re-run
`reverie install` when the environment was moved or deleted.

### Slash commands

Two commands are installed in every harness that has a user-level command
mechanism: **`/reverie-review`** (asks the agent to list review candidates,
show every candidate's full text, and relay only the decisions you state)
and **`/reverie-status`** (the session + brain report through
`summary`). Formats: plain markdown for Claude Code and Cursor;
markdown + YAML frontmatter for OpenCode; frontmatter markdown under
`~/.codex/prompts/` for Codex (custom prompts are deprecated upstream —
still the user-level mechanism); TOML (`prompt` string) for Gemini CLI.
All are removed by `reverie uninstall` (verbatim content only — edited
files are reported, not deleted).

### The MCP `instructions` field

The initialize response carries the core usage contract in a few lines:
query only after reading the code; treat results as past understanding to
verify; never decide reviews for the user; ask once per repo before turning
Reverie on. Client support, per each vendor's docs (2026-10-15):

- **Claude Code** — surfaces server instructions at session start
  (documented).
- **Codex CLI** — "Codex reads the MCP `instructions` field returned during
  initialization and uses it as server-wide guidance" (documented).
- **Gemini CLI** — instructions are "appended to the system instructions"
  (documented).
- **OpenCode** — captured in recent versions (visible in the v1.18+ source;
  not clearly documented).
- **Cursor** — not documented; rely on tool descriptions there.

## Sources (checked 2026-10-15)

- Claude Code: MCP configuration (`~/.claude.json` user scope), memory
  (`~/.claude/CLAUDE.md`, `@import`), slash commands/skills
  (`~/.claude/commands/`), server instructions —
  <https://code.claude.com/docs/en/mcp>, <https://code.claude.com/docs/en/memory>,
  <https://code.claude.com/docs/en/slash-commands>
- Codex CLI: MCP config (`~/.codex/config.toml`, `[mcp_servers.*]`), global
  `AGENTS.md`, custom prompts (`~/.codex/prompts/`, deprecated), server
  instructions — <https://developers.openai.com/codex/mcp>,
  <https://developers.openai.com/codex/guides/agents-md>,
  <https://developers.openai.com/codex/custom-prompts>
- Cursor: global MCP (`~/.cursor/mcp.json`), rules (User Rules in settings;
  no documented global rules **file**), commands (`~/.cursor/commands/`,
  beta) — <https://www.cursor.com/docs/mcp>,
  <https://cursor.com/docs/context/rules>, <https://cursor.com/docs/agent/chat/commands>
- OpenCode: global config (`~/.config/opencode/opencode.json`, `mcp.<name>`
  with `type: local`/`command` argv/`environment`), global rules
  (`~/.config/opencode/AGENTS.md`), commands
  (`~/.config/opencode/commands/`) — <https://opencode.ai/docs/mcp-servers/>,
  <https://opencode.ai/docs/rules/>, <https://opencode.ai/docs/commands/>
- Gemini CLI: `~/.gemini/settings.json` (`mcpServers`), `~/.gemini/GEMINI.md`,
  TOML commands (`~/.gemini/commands/`), server instructions —
  <https://geminicli.com/docs/tools/mcp-server/>,
  <https://geminicli.com/docs/cli/gemini-md/>,
  <https://geminicli.com/docs/cli/custom-commands>

## Other MCP clients

Any client that launches a stdio MCP server works:

- command: the absolute path to `reverie-mcp` (pip-installed;
  `<venv>/bin/reverie-mcp`, `<venv>\Scripts\reverie-mcp.exe` on Windows) or
  `<venv>/bin/python -m reverie.mcp_server` with `PYTHONPATH=<repo root>`
  (dev checkout)
- transport: stdio, NDJSON JSON-RPC 2.0, protocol `2024-11-05`
- 7 tools (see the README's glossary + `docs/CLI.md`)

## Uninstall

```bash
reverie uninstall [--delete-data] [--no-export] [--yes] [--dry-run]
```

Shows the full plan first (harness entries + data plan) and asks ONE
confirmation. Removes only Reverie's own entries (merge logic in reverse)
and deletes a whole file only when the install manifest shows Reverie
created it and it is still unedited — a file that existed before install
survives with just Reverie's entry removed. Keeps `~/.reverie` unless
`--delete-data` (a final export is written outside it first), and prints
the `pip uninstall reverie` command for the last step. See `docs/CLI.md`
for every flag.

## Smoke test

After installing in a real harness, run [`docs/SMOKE_TEST.md`](SMOKE_TEST.md).
