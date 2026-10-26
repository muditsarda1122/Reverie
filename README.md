# Reverie

Reverie is engineering memory for coding agents. While you and your agent work, it captures the engineering conclusions you reach (invariants, root causes, design decisions, corrected assumptions) and stores them in one local SQLite file. In a later conversation, after you have reviewed them, those conclusions come back to the agent with their confidence, their grounding in your code, and their current status, framed as past understanding to verify, never as authority.

> **Status: early.** Reverie has been used on a small number of projects so far. Some candidate conclusions are wrong, and your agent doesn't always check memory on its own. Review what Reverie keeps, and tell us where it fails.

## Install

You need Python 3.12 and git. Run these commands from a clean machine:

```bash
git clone https://github.com/muditsarda1122/Reverie.git
cd Reverie
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
reverie install
```

The first install downloads PyTorch and a sentence-embedding model. Expect several gigabytes and a few minutes.

`reverie install` detects which agents you have, wires the MCP server into each one, writes the per-agent instructions and the `/reverie-review` and `/reverie-status` commands, creates `~/.reverie`, and prints a summary of exactly what it wrote. It then starts the server and checks that it answers, and it reports success or failure for each agent. Restart each agent afterwards.

The installer writes absolute paths into each agent's config, so agents can find the server outside your virtualenv. If you move the `Reverie` folder, run `reverie install` again.

To see what the installer would change without writing anything, run `reverie install --dry-run`. Non-interactive: `reverie install --all` configures every detected agent, and `--only claude_code,codex` configures only the ones you name.

You need an LLM provider key before Reverie can extract anything. Set one before running the installer, or choose "Local via Ollama". See [Model providers](#model-providers).

## Quick start

1. Open your agent in any project.
2. **The first time, your agent asks once:** "Reverie isn't on for this repo. Turn it on? (yes / not now / never)". Your answer is remembered. `reverie enable` and `reverie disable` change it at any time.
3. **Just code.** Sessions are automatic per branch. As your agent works, it records a conclusion when it reaches a real engineering insight, not for every edit. A candidate from one branch isn't visible on another branch. Accepted conclusions are visible everywhere.
4. **Review when you like.** Candidates wait in one review queue across every branch and repo. Run `reverie review` in a terminal, say "show my Reverie candidates" in the chat, or type `/reverie-review`. Nothing reaches long-term memory without your decision.
5. In a later conversation, after your agent reads the relevant code, it asks Reverie for past conclusions. They come back with their confidence, grounding and status, framed as past understanding to verify, never as authoritative context.

## Supported agents

Reverie connects to Claude Code, Codex CLI, Cursor and OpenCode as an MCP server. The tested versions, what Reverie writes for each, and what was verified are in [`docs/SUPPORTED_HARNESSES.md`](docs/SUPPORTED_HARNESSES.md). The file paths, hooks and permission setup for each agent are in [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md). Any other MCP client can be set up by hand, as described in [`docs/INSTALLATION.md`](docs/INSTALLATION.md).

## Model providers

Reverie makes its own LLM calls to extract conclusions and classify relationships. These calls are separate from your agent. Set the key for one provider, then choose it during `reverie install`, or set `provider:` in the `llm:` block of `~/.reverie/config.yaml`.

| Provider | Default model | Key environment variable |
|---|---|---|
| OpenCode Zen (default) | `claude-haiku-5-5` | `OPENCODE_ZEN_API_KEY` |
| Anthropic | `claude-haiku-5-5` | `ANTHROPIC_API_KEY` |
| OpenAI | `gpt-5-mini` | `OPENAI_API_KEY` |
| Google Gemini | `gemini-3.8-flash` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) |
| OpenRouter | `anthropic/claude-haiku-5.5` | `OPENROUTER_API_KEY` |
| Ollama (local) | `qwen2.5-coder:14b` | none |

To switch provider, run `reverie install --provider anthropic`, or edit the `llm:` block directly. Anything you don't set is filled from the registry, and anything you set wins. You can also choose a different model for each task under `llm.models`.

## Commands

| Command | What it does |
|---|---|
| `reverie install` | Configure your agents and `~/.reverie` |
| `reverie enable` / `reverie disable` | Turn Reverie on or off for the current repo |
| `reverie repos` | List every repo Reverie knows about, with its state and counts |
| `reverie review` | Review pending candidates in a terminal interface |
| `reverie status` | Show the session and brain report |
| `reverie export` / `reverie import` | Back up or restore your memory as one ZIP file |
| `reverie reset` | Wipe the brain, or one repo, after an automatic backup |
| `reverie retry` | Recover failed extractions |
| `reverie repair` | Check brain integrity and recover safely |
| `reverie uninstall` | Remove what `reverie install` added (memory is kept unless `--delete-data`) |

The full reference, with flags and examples, is in [`docs/CLI.md`](docs/CLI.md).

## Where your data lives

- Everything is local, in one SQLite file: `~/.reverie/reverie.db`. Your configuration, the agent instructions and the slash commands sit beside it. There is no account, no cloud service and no telemetry.
- **Reverie never writes into your repositories.** Each repo's state (on, off, asked) is stored in `~/.reverie`, not in a file inside your project.
- **What leaves your machine:** only what the LLM features need, such as the extraction prompt, your agent's reasoning and output, and candidate pairs for classification. Those go to the provider you configured. With Ollama, nothing leaves your machine.
- **API keys** are read from environment variables. Reverie never writes them to disk.
- **Backups:** `reverie export` writes one ZIP file with a consistent snapshot. `reverie import` restores it. `--replace` makes an automatic backup first.

## Uninstall

```bash
reverie uninstall                 # removes the agent entries; keeps ~/.reverie
reverie uninstall --delete-data   # also removes ~/.reverie, after a final export
```

Uninstall removes only what `reverie install` wrote: Reverie's server entries, instruction references, slash-command files, hooks and permission entries. Anything else in those files is left alone. If you've edited one of them, Reverie reports it and doesn't touch it. Uninstall shows the full plan and asks for one confirmation. Use `--yes` to skip the prompt. Your memory in `~/.reverie` is kept unless you pass `--delete-data`, which exports a final backup first and prints where it saved it.

The last step prints `pip uninstall reverie` for you to run yourself. A running package can't remove itself.

## Troubleshooting

**The agent doesn't use Reverie.**
Your agent learns how to use Reverie from its instruction file and from the server. If it never sees them, it has no reason to call the tools. Check these in order:
1. Your agent's version is listed in [`docs/SUPPORTED_HARNESSES.md`](docs/SUPPORTED_HARNESSES.md). Claude Code needs 2.1.121 or later.
2. Restart the agent, so it reloads the server and its instructions.
3. Run `reverie install` again. It repairs the server path and refreshes any outdated instruction block.

**A permission prompt appears for Reverie's tools.**
`reverie install` allows Reverie's read tools in each agent's permission settings. If a read tool still prompts, run `reverie install` again. If it still prompts, check your agent's version. Older versions ignore per-tool approval settings. Write tools (`observe`, `reconsolidate`, `set_repo_state`, `review_decide`) prompt on purpose.

**Reverie behaves like an older version.**
Run `git pull` in your checkout, then `pip install -e .` and `reverie install`. `reverie uninstall --dry-run` shows which entries are configured and where, before you change anything.

## Documentation

- [Installation](docs/INSTALLATION.md)
- [CLI reference](docs/CLI.md)
- [Supported agents](docs/SUPPORTED_HARNESSES.md)
- Website: https://reverie.muditsarda.com/

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup and ground rules, and read the [code of conduct](CODE_OF_CONDUCT.md). Reverie also includes a benchmark harness (`bench/`) that runs a real agent through sequential sessions on a target repository, with and without memory. It's published as a research tool, not as proof of an advantage.

## License

[Apache-2.0](LICENSE) © 2026 Mudit Sarda. The extraction prompt ([`reverie/prompts/extractor_prompt.md`](reverie/prompts/extractor_prompt.md)) is separately MIT-licensed.

## Citation

```bibtex
@software{reverie2026,
  author  = {Sarda, Mudit},
  title   = {Reverie},
  year    = {2026},
  url     = {https://reverie.muditsarda.com/},
  version = {0.2.0}
}
```

Machine-readable metadata is in [`CITATION.cff`](CITATION.cff).