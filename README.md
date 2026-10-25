# Reverie

Reverie is engineering memory for coding agents. While you and your agent work, it captures the engineering conclusions you reach — invariants, root causes, design decisions, corrected assumptions — and stores them in one local SQLite file. In a later conversation, after you have reviewed them, those conclusions come back to the agent with their confidence, their grounding in your code, and their current status, framed as past understanding to verify — never as authority.

## Install

Requires Python 3.12, pip, and git. From a clean machine:

**Easiest — an isolated environment on your PATH** ([pipx](https://pipx.pypa.io) or [uv](https://docs.astral.sh/uv/)):

```bash
pipx install reverie         # or: uv tool install reverie
reverie install              # configure your agents + ~/.reverie
```

Plain pip works just as well — install into a virtualenv and run the CLI from there:

```bash
python -m venv ~/.reverie-venv
source ~/.reverie-venv/bin/activate     # Windows: .venv\Scripts\activate
pip install reverie
reverie install
```

The installer writes the **absolute path** of the `reverie-mcp` script into every harness config — harnesses launch MCP servers outside your virtualenv, so a bare command name is not enough. It then starts the written command, performs an MCP `initialize` handshake, and prints ✓ or ✗ per harness; a failing setup is reported (with the command and the error) instead of "Reverie is ready!". If you later move or delete the environment, `reverie status` flags the broken path and tells you to re-run `reverie install`.

`reverie install` detects which harnesses you have, wires the MCP server into each, writes the per-harness instructions and the `/reverie-review` / `/reverie-status` commands, creates `~/.reverie`, detects an LLM API key in your environment (or offers Ollama for local-only), and prints a per-harness summary of exactly what it wrote. For Claude Code it also installs a read-only SessionStart hook and an async Stop capture hook. Non-interactive: `reverie install --all` (every detected harness) or `--only claude_code,codex`; `--dry-run` previews everything without writing.

Provide a key for one of the supported providers before the provider prompt, or choose "Local via Ollama" — see [Model providers](#model-providers).

## Quick start

1. Open your agent in any project.
2. **The first time, your harness asks once:** "Reverie isn't on for this repo. Turn it on? (yes / not now / never)". Your answer is remembered; `reverie enable` / `reverie disable` flip it any time.
3. **Just code.** Sessions are automatic per branch. As your agent works it calls `observe` when it reaches a real engineering conclusion — not for every edit.
4. **Review when you like.** Conclusions wait as candidates in one review queue across every branch and repo. Run `reverie review` in a terminal (a calm one-candidate-at-a-time UI), say *"show my Reverie candidates"* in the chat, or type `/reverie-review`. Nothing reaches long-term memory without your decision.
5. That's it. In a future conversation, after your agent reads the relevant code it calls `query` and past conclusions come back with their confidence, grounding and current status — framed as *past understanding to verify*, never as authoritative context.

## Supported harnesses

Reverie plugs into Claude Code, Codex CLI, Cursor and OpenCode as an MCP server. The tested versions, what Reverie writes to each, and exactly what was verified for each version are listed in [`docs/SUPPORTED_HARNESSES.md`](docs/SUPPORTED_HARNESSES.md); the per-harness file paths, hooks and permission setup are in [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md). Any other MCP client works with the manual snippet in [`docs/INSTALLATION.md`](docs/INSTALLATION.md).

## Model providers

One provider serves every LLM call (extraction, relationship classification, mode detection). Configure with `reverie install` or the `llm:` block of `~/.reverie/config.yaml`:

| Provider | Default model | Key env var | Notes |
|---|---|---|---|
| OpenCode Zen *(default)* | `claude-haiku-5-5` | `OPENCODE_ZEN_API_KEY` | Multi-family endpoint |
| Anthropic | `claude-haiku-5-5` | `ANTHROPIC_API_KEY` | Recent Anthropic models reject non-default sampling parameters; the request shape is handled for you |
| OpenAI | `gpt-5-mini` | `OPENAI_API_KEY` | Reasoning models: `max_completion_tokens` shape auto-applied |
| Google Gemini | `gemini-3.8-flash` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | OpenAI-compatible endpoint |
| OpenRouter | `anthropic/claude-haiku-5.5` | `OPENROUTER_API_KEY` | Vendor-prefixed model names |
| Ollama (local) | `qwen2.5-coder:14b` | none | Nothing leaves your machine |

Switching: `reverie install --provider anthropic`, or edit `llm:` in `~/.reverie/config.yaml` (`provider:`, and optionally `model:`, `base_url:`, `api_key_env:`, `wire_format:`). Everything unset is filled from the registry; anything you set explicitly wins. Per-task models: `llm.models.extraction` / `models.classification` / `models.mode_detection`.

## Commands

| Command | What it does |
|---|---|
| `reverie install` | Configure agents + `~/.reverie` |
| `reverie enable` / `reverie disable` | Turn Reverie on/off for a repo |
| `reverie repos` | Every known repo with state and counts |
| `reverie review` | Review pending candidates (terminal UI) |
| `reverie status` | Session + brain report |
| `reverie export` / `reverie import` | Backup / restore (one ZIP) |
| `reverie reset` | Wipe the brain (or one repo), auto-backup first |
| `reverie retry` | Recover failed extractions / re-diffuse |
| `reverie repair` | Brain integrity check + safe recovery |
| `reverie uninstall` | Remove Reverie's harness entries (memory kept unless `--delete-data`) |

Full reference with flags, examples and exit behaviour: [`docs/CLI.md`](docs/CLI.md).

## Where your data lives

- Everything is local: one SQLite file, `~/.reverie/reverie.db` (config, agent instructions and slash commands live beside it). No account, no cloud, no telemetry.
- **Reverie never writes into your repositories.** Its per-repo state (on/off, asked) is stored in `~/.reverie`, keyed by the repo's git identity — not in a file inside your project.
- **What leaves your machine:** only what the LLM features need — the extraction prompt, your agent's reasoning and output, and candidate pairs for classification — sent to the endpoint **you** configure. Pick Ollama and nothing leaves your machine at all.
- **API keys** are read from environment variables only; Reverie never writes them anywhere.
- **Export / import:** `reverie export` writes one ZIP (consistent snapshot + manifest); `reverie import` restores it (`--replace` takes an automatic backup first).

## Uninstall

```bash
reverie uninstall                 # removes the harness entries; keeps ~/.reverie
reverie uninstall --delete-data   # also removes ~/.reverie (final export first)
```

Uninstall removes exactly what `reverie install` wrote: Reverie's MCP entries in each harness config, its instruction references, its slash-command files, and the hooks and permission entries it added — nothing else in those files is touched, and an edited file is reported, never damaged. Your memory (`~/.reverie`) is **kept** by default; `--delete-data` takes a final export outside `~/.reverie` first (the path is printed) and only then removes it. Uninstall shows the full plan and asks **one** confirmation before touching anything (`--yes` skips it; the export-before-delete rule always holds). The last step prints the `pip uninstall reverie` command to run by hand — the running package can't remove itself.

## Troubleshooting

**The agent doesn't call `query` (or ignores Reverie).**
The usage contract reaches the agent through the harness's instruction file and the server's `instructions` field — if the agent never sees them, it has no reason to call the tools. Check, in order: (1) the harness loads the instructions — Claude Code needs 2.1.121+ for `"alwaysLoad": true`, older Codex versions don't read skills, details in [`docs/SUPPORTED_HARNESSES.md`](docs/SUPPORTED_HARNESSES.md); (2) restart the harness so the MCP server and instructions are reloaded; (3) `reverie status` — a moved or deleted virtualenv breaks the configured server path, and re-running `reverie install` repairs both the path and the instruction files (a stale instruction block from an older version is refreshed on reinstall).

**A permission prompt appears for Reverie's tools.**
`reverie install` adds allow entries for the read tools to each harness's permission config (`mcp__reverie__*` in `~/.claude/settings.json`, per-tool approvals in `~/.codex/config.toml`, `Mcp(reverie:*)` in `~/.cursor/cli-config.json`). If a read tool still prompts: re-run `reverie install` (it merges the entry back if it is missing), or check your harness version — older versions ignore per-tool approval settings. Write tools (`observe`, `reconsolidate`, `set_repo_state`, `review_decide`) are allowed to prompt on purpose.

**An old install is still configured.**
Console scripts only update on reinstall — if `reverie install` behaves like an older version (or an `ec` command still exists), re-run `pip install reverie` (or `pip install -e .` in a checkout) and then `reverie install`. Instruction blocks written by an older version are refreshed by the next `reverie install`; `reverie status` reports a server path that no longer exists; `reverie uninstall --dry-run` shows exactly which entries are configured where before you change anything.

## Contributing

Contributions are welcome — start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup and ground rules, and the [code of conduct](CODE_OF_CONDUCT.md). Reverie also ships with a benchmark harness (`bench/`) that runs a real agent through sequential sessions on a target repository with and without memory; it is published as a research instrument, not as proof of advantage.

## License

[Apache-2.0](LICENSE) © 2026 Mudit Sarda. The extraction prompt ([`reverie/prompts/extractor_prompt.md`](reverie/prompts/extractor_prompt.md)) is separately MIT-licensed.

## Citation

```bibtex
@software{reverie2026,
  author  = {Sarda, Mudit},
  title   = {Reverie},
  year    = {2026},
  url     = {https://ems-gold-seven.vercel.app/},
  version = {0.2.0}
}
```

Machine-readable metadata: [`CITATION.cff`](CITATION.cff).
