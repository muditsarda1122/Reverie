# Reverie

**Reverie is engineering memory for coding agents.** While you and your agent work, it captures the engineering conclusions you reach — invariants, root causes, decisions, corrected assumptions — and brings them back in a later conversation, after they've been reviewed by you and grounded in your code.

Everything lives in one SQLite file on your machine. Reverie is an MCP server: it plugs into Claude Code, Codex CLI, Cursor, OpenCode and Gemini CLI (see [Supported harnesses](#supported-harnesses) for honest status per harness).

## Install

Requires Python 3.12, pip, and git.

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

Both layouts work. The installer writes the **absolute path** of the `reverie-mcp` script into every harness config — harnesses launch MCP servers outside your virtualenv, so a bare command name is not enough. It then starts the written command, performs an MCP `initialize` handshake, and prints ✓ or ✗ per harness; a failing setup is reported (with the command and the error) instead of "Reverie is ready!". If you later move or delete the environment, `reverie status` flags the broken path and tells you to re-run `reverie install`.

`reverie install` detects which harnesses you have, wires the MCP server into each, writes the per-harness instructions and the `/reverie-review` / `/reverie-status` commands, creates `~/.reverie`, detects an LLM API key in your environment (or offers Ollama for local-only), and prints a per-harness summary of exactly what it wrote. For Claude Code it also installs a read-only SessionStart hook and an async Stop capture hook. Non-interactive: `reverie install --all` (every detected harness) or `--only claude_code,codex`; `--dry-run` previews everything without writing.

Provide a key for one of the supported providers before the step 2 prompt, or choose "Local via Ollama" — see [Model providers](#model-providers).

## Quick start

1. Open your agent in any project.
2. **The first time, your harness asks once:** "Reverie isn't on for this repo. Turn it on? (yes / not now / never)". Your answer is remembered; `reverie enable` / `reverie disable` flip it any time.
3. **Just code.** Sessions are automatic per branch. As your agent works it calls `observe` when it reaches a real engineering conclusion — not for every edit.
4. **Review when you like.** Conclusions wait as candidates in one review queue across every branch and repo. Run `reverie review` in a terminal (a calm one-candidate-at-a-time UI), say *"show my Reverie candidates"* in the chat, or type `/reverie-review`. Nothing reaches long-term memory without your decision.
5. That's it. In a future conversation, after your agent reads the relevant code it calls `query` and past conclusions come back with their confidence, grounding and current status — framed as *past understanding to verify*, never as authoritative context.

## How it works

Observe → extract → review → ground → retrieve. Your agent's reasoning is distilled by a prompted extractor into **conclusions** (not logs), stored with Bayesian confidence in log-odds space, typed relationships to other conclusions, and grounding references (files, symbols, commit). A background maintainer verifies that grounding against your live repository and lets stale conclusions fade. The long version — pipeline, confidence math, edge types — is at [ems-gold-seven.vercel.app/how-it-works](https://ems-gold-seven.vercel.app/how-it-works); the complete design spec is [`docs/SPEC.md`](docs/SPEC.md).

## Supported harnesses

| Harness | Install writes | Smoke-tested | Known limits |
|---|---|---|---|
| **OpenCode** | `~/.config/opencode/opencode.json` (`mcp.reverie`), `AGENTS.md` block, `commands/*.md` | ✅ exercised end-to-end (offline tests + live E2E + EC-Bench) | Marker detection misses `opencode.jsonc` configs |
| **Claude Code** | `~/.claude.json` (`mcpServers.reverie`, user scope), `@import` in `~/.claude/CLAUDE.md`, `commands/*.md`, SessionStart + Stop hooks in `settings.json` | 🟡 hand smoke test: SessionStart state + on-request capture; Stop capture offline only | Reads server `instructions`; `~/.claude.json` is also Claude Code's state file (the merge only ever touches its own key); the Stop hook is async and never blocks a session |
| **Codex CLI** | `~/.codex/config.toml` (`[mcp_servers.reverie]`), `AGENTS.md` block, `prompts/*.md` | ❌ offline install tests only | Reads server `instructions`; custom prompts are deprecated upstream (still the user-level command mechanism) |
| **Cursor** | `~/.cursor/mcp.json` (`mcpServers.reverie`), `commands/*.md` (beta) | ❌ offline install tests only | No global rules file is written (Cursor documents no global rules file; use Settings → Rules to paste the guidance if you want it always on). Server `instructions` support is not documented by Cursor |
| **Gemini CLI** | `~/.gemini/settings.json` (`mcpServers.reverie`), `GEMINI.md` block, `commands/*.toml` | ❌ offline install tests only | Reads server `instructions`; Gemini CLI was replaced by Antigravity CLI for unpaid/Google One users in June 2026 (config format unchanged) |

"Smoke-tested" means what it says: OpenCode is exercised end-to-end. Claude Code has been exercised by hand in a smoke test (session start state and capture of a conclusion on request) but not with the per-reply Stop capture, which is new on this branch; that path is verified offline only and needs one live check in Claude Code before merge. Codex, Cursor and Gemini CLI have their written shapes verified against each vendor's current docs and the server is tested over real stdio, but no live sessions — check [`docs/SMOKE_TEST.md`](docs/SMOKE_TEST.md) if you want the checklist. Any other MCP client works with the manual snippet in [`docs/INSTALLATION.md`](docs/INSTALLATION.md). The integration contract, per-harness paths, hook behaviour and verification date are in [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md).

## Model providers

One provider serves every LLM call (extraction, relationship classification, mode detection). Configure with `reverie install` or the `llm:` block of `~/.reverie/config.yaml`:

| Provider | Default model | Key env var | Notes |
|---|---|---|---|
| OpenCode Zen *(default)* | `claude-haiku-4-5` | `OPENCODE_ZEN_API_KEY` | Multi-family endpoint |
| Anthropic | `claude-haiku-5-5` | `ANTHROPIC_API_KEY` | 5.5-family models: sampling parameters omitted automatically (they reject non-default `temperature`) |
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

## Glossary

- **Conclusion** — one engineering belief Reverie stores, e.g. "authentication correctness depends on optimistic token refresh". Stored internally as an *Engineering Cognition Unit (ECU)* — the schema-level name you'll see in code, exports and the spec.
- **Session memory** — conclusions captured in the current (repo, branch) context. Automatic, per branch, never closed by you.
- **Long-term memory** — the Canonical Brain: conclusions you accepted in review, promoted with their relationship edges, shared across branches and repos.
- **Review** — the human gate. Captured conclusions wait as *candidates*; only your accept promotes one into long-term memory. Review in the terminal, in the chat, or with `/reverie-review`.
- **Confidence** — a 0–1 Bayesian belief strength (computed in log-odds space), set by the conclusion's source type and scope, strengthened by corroboration and retrieval.
- **Decay** — confidence fades slowly over time (faster for narrow scopes); retrieval reinforcement resets the clock. Narrow, unused conclusions fade first.
- **Grounding** — every conclusion references files, symbols and the commit it was made on; a background maintainer verifies those against your live repository and deprecates conclusions whose grounding vanished.
- **Repo enablement** — per-repo on/off state (`undecided` / `enabled` / `disabled`), remembered globally, asked once.
- **Engineering Cognition** — the research programme this work is part of; the benchmark is **EC-Bench**.

## Privacy and data

- **What stays local:** everything. The brain is one SQLite file, `~/.reverie/reverie.db` (config, agent instructions and slash commands live beside it). No account, no cloud, no telemetry.
- **What leaves your machine:** only what the LLM features need — the extraction prompt, your agent's reasoning and output, and candidate conclusion pairs for classification — sent to the endpoint **you** configure. Pick Ollama and nothing leaves your machine at all.
- **API keys** are read from environment variables only; Reverie never writes them anywhere.
- **Export / import:** `reverie export` writes one ZIP (consistent snapshot + manifest); `reverie import` restores it (`--replace` takes an automatic backup first). Benchmark outputs, DBs and logs are gitignored — nothing from `~/.reverie` is committed.

## Uninstall

```bash
reverie uninstall                 # removes the harness entries; keeps ~/.reverie
reverie uninstall --delete-data   # also removes ~/.reverie (final export first)
```

Uninstall shows the full plan — every harness entry it will remove, and whether data will be exported and deleted — and asks **one** confirmation before touching anything (`--yes` skips it; the export-before-delete rule always holds). It removes only Reverie's own entries, byte-reverses its merges, and never guesses at edited files. The last step prints the `pip uninstall reverie` command to run by hand (the running package can't remove itself).

## Research and contributing

Reverie ships with EC-Bench (`bench/`), a harness that runs a real agent through sequential sessions on a target repository with and without memory and judges the transcripts. Honest status: **EC-Bench has not shown a clear advantage yet** — it is published as a research instrument, not as proof. Method and results: [ems-gold-seven.vercel.app/research](https://ems-gold-seven.vercel.app/research).

Contributions are welcome — start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup and ground rules, and the [code of conduct](CODE_OF_CONDUCT.md).

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
