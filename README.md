# Reverie

Reverie is engineering memory for coding agents. It works across Claude Code, Codex CLI, opencode and Cursor, on one local SQLite file. It remembers what **you** said (rules, decisions, preferences) and what the code really changed, hands it to the next agent at the start of a session and again when that agent edits the file it applies to, keeps it true as the code moves on, and lets you pick up a session another tool left unfinished.

- **Capture from evidence.** An item is stored only when your own words or the real diff back it. What an agent says about its own work is never a source.
- **Across tools.** Reverie reads the transcripts each harness already keeps (including headless, scripted and killed sessions) and, where a harness allows hooks, hooks in too. A session started in Codex can be finished in Claude: `reverie resume`.
- **Secrets never stored.** API keys, tokens, `Authorization` headers and `KEY=value` secrets are redacted before anything is written, and again when it is shown (`KEY=[REDACTED]` keeps the variable name).
- **Stays correct.** Anchors point at real files and symbols; when a commit contradicts an item, it is flagged with a proposed replacement.
- **Bounded.** Near-duplicates are shown once ("seen 5 times"), and a daily merge folds restatements together.

> **Status: early.** Reverie has been used on a small number of projects. Expect rough edges and tell us where it fails.

## Why it was built

A terminal crash interrupted a long session and the state was lost. Many memory tools load past notes into the prompt at the start of a session. In an informal test, that made the agent trust the notes over the code in front of it. Reverie retrieves on demand instead, and the agent is told to read the code first.

## Install

One command (macOS and Linux; it needs `git` and `curl`, nothing else, because it brings its own Python):

```bash
curl -LsSf https://raw.githubusercontent.com/muditsarda1122/Reverie/main/scripts/install.sh | sh
```

It installs [`uv`](https://docs.astral.sh/uv/) for your user if you do not have it, installs Reverie as an isolated tool, and runs `reverie install`. In our test on a MacBook it took about a minute, most of it downloading the ~90 MB embedding model once; the install is about 170 MB. [Read the script first](scripts/install.sh) if you like: it is short and does nothing else.

Then set **one** LLM API key in your environment (Anthropic, OpenAI, Gemini, OpenRouter or OpenCode Zen; Reverie uses the one it finds, see [Model providers](#model-providers)) and restart your agent. That is all. The key is what Reverie uses for its own extraction calls, so it works the same under every agent.

**Platforms:** macOS and Linux (tested on every change). On native Windows the installer (`scripts/install.ps1`) and the CLI run in CI, but 68 of our 1,443 tests still fail there, mostly path handling, so treat Windows as experimental and prefer WSL.

Prefer your own tool? The package is called `reverie-mem` (the command is `reverie`): `uv tool install reverie-mem` or `pipx install reverie-mem`, then `reverie install`.

Prefer to do it by hand? With Python 3.12 and git:

```bash
git clone --depth 1 https://github.com/muditsarda1122/Reverie.git
cd Reverie
python3.12 -m venv .venv && source .venv/bin/activate     
pip install -e .
reverie install
```

`reverie install` detects which agents you have, wires the MCP server into each one, writes the per-agent instructions and the `/reverie-review` and `/reverie-status` commands, creates `~/.reverie`, and prints a summary of exactly what it wrote. It then starts the server and checks that it answers, and it reports success or failure for each agent. Restart each agent afterwards.

The installer writes absolute paths into each agent's config, so agents can find the server outside your virtualenv. If you move the `Reverie` folder (manual install), run `reverie install` again.

To see what the installer would change without writing anything, run `reverie install --dry-run`. Non-interactive: `reverie install --all` configures every detected agent, and `--only claude_code,codex` configures only the ones you name.

You need an LLM provider key before Reverie can extract anything. Set one before running the installer, or choose "Local via Ollama". See [Model providers](#model-providers).

## Quick start

1. Open your agent in any project. **Reverie is on in every repo by default**; opt a repo out with `reverie disable` (back in with `reverie enable`). Existing memory is kept.
2. **Just code.** Reverie logs your prompts (redacted) and extracts rules, decisions and preferences in the background. Nothing to review before it works; every item can be undone with `reverie undo <id>`, and `reverie digest` lists what was added.
3. **New session, any tool.** Your rules and a short index arrive at session start; items tied to a file arrive when the agent edits it.
4. **Session cut off?** In the next agent, say "my previous session got cut off, run `reverie resume`". It prints your rules, every prompt you sent (verbatim, redacted) and the git state, whichever tool the session ran in.
5. **Keep it true.** `reverie stale check` (also automatic after new commits) flags items a commit contradicts. `reverie stale accept|keep` resolves them.

### What you see

After a few sessions, `reverie digest` lists what Reverie remembered (here from a throwaway repo):

```text
3 item(s) added since the beginning:

[f48988f3] rule (you said)
    Never commit directly to main; open a PR.
[d3c7b2c5] preference (you said)
    Use pytest, not unittest, for new tests.
[a4c5003b] decision (you said)
    Chose Postgres advisory locks over Redis for job dedupe: one less service to run.

Undo any of them with `reverie undo <id>`.
```

`reverie status` shows what this repo holds, and warns when no key is visible:

```text
Memory for this repo: 3 items (1 rule, 1 preference, 1 decision). `reverie digest` lists what was added recently.

!! No LLM key found (OPENCODE_ZEN_API_KEY is not set in this shell).
   Reverie records your prompts, but it cannot extract rules or decisions without a key.
```

If `reverie status` shows that warning, nothing is being extracted yet. Put the key in your shell profile and restart your agent (an agent started from a desktop icon does not see variables set only in a terminal).

## Supported agents

| Agent | Memory delivered by | Prompts and answers captured by |
|---|---|---|
| Claude Code | hooks (session start, before each edit) + MCP | hooks + transcript importer |
| opencode | plugin (rules in the system prompt) + MCP | plugin + transcript importer |
| Codex CLI | hooks (`reverie install` approves them for you through Codex's own API; `--no-trust-codex-hooks` leaves it to `/hooks`) + MCP | hooks (once approved) + transcript importer |
| Cursor | MCP | transcript importer (hooks not verified yet) |

Every agent is also an MCP client of Reverie (`query`, `summary`, ...), which stays the fallback. Codex only runs a hook after it is approved. `reverie install` does that for Reverie's own three hooks (the same `trusted_hash` the `/hooks` screen writes, taken from Codex's own `hooks/list`; it never touches a hook you wrote). Pass `--no-trust-codex-hooks` to approve them yourself with `/hooks`. Until approved, the transcript importer still covers every session. Tested versions, what Reverie writes for each agent, and the spike results are in [`docs/SUPPORTED_HARNESSES.md`](docs/SUPPORTED_HARNESSES.md); file paths and permissions are in [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md). Any other MCP client can be set up by hand, as in [`docs/INSTALLATION.md`](docs/INSTALLATION.md).

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

**Local models:** Ollama keeps everything on your machine, but capture quality depends on the model. In one test the offline default kept 4 of 13 planted facts and timed out on a long exchange, while a hosted small model kept 12 of 13. Use a coding-capable model at least that strong, or keep a hosted provider for extraction.

To switch provider, run `reverie install --provider anthropic`, or edit the `llm:` block directly. Anything you don't set is filled from the registry, and anything you set wins. You can also choose a different model for each task under `llm.models`.

## Commands

| Command | What it does |
|---|---|
| `reverie install` | Configure your agents and `~/.reverie` |
| `reverie enable` / `reverie disable` | Turn Reverie on or off for the current repo |
| `reverie repos` | List every repo Reverie knows about, with its state and counts |
| `reverie resume` | Print rules, your prompt log and git state to continue a cut-off session in any tool |
| `reverie transcripts` | Import new prompts/answers from the harnesses' own transcripts (also automatic) |
| `reverie rules` | Show or edit the rules Reverie always applies |
| `reverie show` | Show one memory item in full |
| `reverie undo` | Undo an item that was added automatically |
| `reverie digest` | List what was added recently |
| `reverie promote` | Make an item apply to all your projects (or back to one repo) |
| `reverie stale` | Check memory against new commits; accept or keep what is flagged |
| `reverie usage` | Show which items agents used, ignored or contradicted |
| `reverie merge` | Merge near-duplicate items (also runs by itself, at most daily) |
| `reverie status` | Show the session and brain report |
| `reverie export` / `reverie import` | Back up or restore your memory as one ZIP file |
| `reverie reset` | Wipe the brain, or one repo, after an automatic backup |
| `reverie retry` | Recover failed extractions |
| `reverie repair` | Check brain integrity and recover safely |
| `reverie uninstall` | Remove what `reverie install` added (memory is kept unless `--delete-data`) |

### Experimental (team features, not needed for solo use)

These exist for shared team rules and are still changing. You can ignore them.

| Command | What it does |
|---|---|
| `reverie context` | Team context as plain markdown in scopes (org, team, repo, paths, you): the narrower scope answers the wider unless locked, sibling teams that contradict make the agent ask (a background judge finds the conflicts when a document or the code changes; a one-line hint nudges asking), anyone can propose, CODEOWNERS accept, and `propose` turns a rule you taught your agent into a team proposal (owners accept or reject with a reason). Early feature on the branch |
| `reverie ui` | A team workspace on localhost: your projects on one board, and per project what needs action first (conflicts, outdated rules, waiting proposals), team rules (and the repo's shared rules file), what this machine remembers, what agents are told, and how to connect. Three buttons write, through the CLI's own functions and its rules: add a rule or guidance, accept a proposal, reject one with a reason (owners only; secrets refused; same-origin and token checked). Everything else is a command it shows. No telemetry. Early feature on the branch |
| `reverie review` | Review legacy candidate conclusions in a terminal interface |

The full reference, with flags and examples, is in [`docs/CLI.md`](docs/CLI.md).

## Where your data lives

- Everything is local, in one SQLite file: `~/.reverie/reverie.db`. Your configuration, the agent instructions and the slash commands sit beside it. There is no account, no cloud service and no telemetry.
- **Reverie never writes into your repositories.** Each repo's state (on, off, asked) is stored in `~/.reverie`, not in a file inside your project.
- **Secrets are redacted before they are stored** (the prompt log, items, the importer's cursor file) and again when shown. Reverie keeps no raw copy. Your harness's own transcript is outside Reverie and may still hold what you typed.
- **What leaves your machine:** only what the LLM features need, such as the redacted exchange and the working-tree diff for extraction, and candidate pairs for merging. Those go to the provider you configured, a third party (the default is OpenCode Zen; Anthropic, OpenAI, Gemini and OpenRouter are the alternatives). Redaction is pattern-based and best-effort, so it can miss an unusual secret. With Ollama, nothing leaves your machine. There is no telemetry, and `reverie install` prints this same notice.
- **API keys** are read from environment variables. Reverie never writes them to disk.
- **Backups:** `reverie export` writes one ZIP file with a consistent snapshot. `reverie import` restores it. `--replace` makes an automatic backup first.

## Uninstall

```bash
reverie uninstall                 # removes the agent entries; keeps ~/.reverie
reverie uninstall --delete-data   # also removes ~/.reverie, after a final export
```

Uninstall removes only what `reverie install` wrote: Reverie's server entries, instruction references, slash-command files, hooks and permission entries. Anything else in those files is left alone. If you've edited one of them, Reverie reports it and doesn't touch it. Uninstall shows the full plan and asks for one confirmation. Use `--yes` to skip the prompt. Your memory in `~/.reverie` is kept unless you pass `--delete-data`, which exports a final backup first and prints where it saved it.

The last step prints the command that removes the package itself, because a running package can't remove itself: `uv tool uninstall reverie-mem` if you installed with the one-line script, or `pip uninstall reverie-mem` otherwise.

## Updating

```bash
reverie --version                  # what you have now
uv tool upgrade reverie-mem        # installed with the one-line script (or: pipx upgrade reverie-mem / pip install -U reverie-mem)
reverie install                    # refresh the agent wiring; safe to repeat
```

Upgrades keep your memory. Your database is migrated in place when a newer version opens it (never lowered, never emptied), and `reverie export` makes a backup you can restore with `reverie import` before you upgrade if you want one.

## Troubleshooting

**The agent doesn't use Reverie.**
Your agent learns how to use Reverie from its instruction file and from the server. If it never sees them, it has no reason to call the tools. Check these in order:
1. Your agent's version is listed in [`docs/SUPPORTED_HARNESSES.md`](docs/SUPPORTED_HARNESSES.md). Claude Code needs 2.1.121 or later.
2. Restart the agent, so it reloads the server and its instructions.
3. Run `reverie install` again. It repairs the server path and refreshes any outdated instruction block.

**A permission prompt appears for Reverie's tools.**
`reverie install` allows Reverie's read tools in each agent's permission settings. If a read tool still prompts, run `reverie install` again. If it still prompts, check your agent's version. Older versions ignore per-tool approval settings. Write tools (`observe`, `reconsolidate`, `set_repo_state`, `review_decide`) prompt on purpose.

**Reverie behaves like an older version.**
Run `reverie --version`, then update as in [Updating](#updating). `reverie uninstall --dry-run` shows which entries are configured and where, before you change anything.

## Documentation

- [Installation](docs/INSTALLATION.md)
- [CLI reference](docs/CLI.md)
- [Supported agents](docs/SUPPORTED_HARNESSES.md)
- Website: https://reverie.muditsarda.com/

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup and ground rules, and read the [code of conduct](CODE_OF_CONDUCT.md). Reverie also includes an evaluation harness (`bench/`) that runs a real agent through sequential sessions on a target repository, with and without memory. It's published as a research tool, not as proof of an advantage.

## License

[Apache-2.0](LICENSE) © 2026 Mudit Sarda. The extraction prompt ([`reverie/prompts/extractor_prompt.md`](reverie/prompts/extractor_prompt.md)) is separately MIT-licensed.

## Citation

```bibtex
@software{reverie2026,
  author  = {Sarda, Mudit},
  title   = {Reverie},
  year    = {2026},
  url     = {https://reverie.muditsarda.com/},
  version = {0.3.0}
}
```

Machine-readable metadata is in [`CITATION.cff`](CITATION.cff).