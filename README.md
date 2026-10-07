# Reverie

**Memory for coding agents.** Reverie keeps the engineering conclusions your agent reaches, reviewed by you, grounded in your code and revised as evidence changes, in one local SQLite file. An MCP server: works with [OpenCode](https://opencode.ai) today; Claude Code, Cursor and Codex coming soon.

## What it does

- **Extracts conclusions, not logs.** While your agent works, Reverie distills its reasoning into ECUs — Engineering Cognition Units: "authentication correctness depends on optimistic token refresh", not "TokenManager.refresh() was called".
- **Confidence that evolves.** Every conclusion carries a Bayesian confidence (computed in log-odds space), starts stronger for debugging insights than for planning guesses, and is re-evaluated when new evidence contradicts it.
- **Grounded in your code.** Each ECU references files, symbols and a commit hash. A background maintainer verifies that grounding against the live repository and deprecates conclusions whose code has disappeared.
- **Reviewed by you.** A session brain captures raw extraction; nothing reaches the durable canonical brain until the terminal review gate — you accept, reject or skip each candidate.
- **Demand-driven retrieval.** Cognition is never auto-injected. The agent consults it through `ec_query` when it recognizes a gap; a 4-factor ranker (relevance, confidence, scope proximity, recency) returns what fits the current task.

## Status and honest limits

- Developed and tested on **macOS**. Other platforms may work but are untested.
- The review gate happens **in the terminal**, one candidate at a time — there is no GUI.
- **You start and end sessions yourself** (`ec-start` / `ec-stop`). Nothing is captured outside a session you open.
- EC-Bench, the built-in benchmark, has **not** shown a clear advantage yet — see [Research](#research).

## Supported agents

| Agent | Status |
|---|---|
| OpenCode | ✅ Works today |
| Claude Code | 🔜 Coming soon |
| Cursor | 🔜 Coming soon |
| Codex | 🔜 Coming soon |

The installer can already write config scaffolding for all four (`ec --only claude_code,codex,cursor,opencode`); OpenCode is the integration exercised end-to-end.

## Install and quick start

Requirements: Python 3.12 (pinned dependencies ship wheels for 3.12), pip, git.

```bash
# 1. Install
pip install git+https://github.com/muditsarda1122/Reverie.git
# (published package name: engineering-cognition)

# 2. Provide an LLM key — hosted by default (OpenCode Zen),
#    or skip via Ollama (see Configuration below)
export OPENCODE_ZEN_API_KEY=...

# 3. Configure your agent and bootstrap ~/.ec
#    (interactive; --all for non-interactive, --dry-run to preview)
ec install

# 4. In your project, start a session, work with your agent, end the session
cd your-project
ec-start        # opens a session for (repo, branch); activates the MCP server
# ... work with the agent; it calls ec_observe / ec_query / ec_reconsolidate
ec-stop         # runs the review gate in your terminal, then diffuses
                # accepted candidates into the canonical brain
```

Handy extras: `ec-status` (current session + brain stats), `ec --dry-run` (show exactly what the installer would write), `ec-repair` (ops recovery). Full details, including per-agent config files the installer touches, are in [`docs/INSTALLATION.md`](docs/INSTALLATION.md).

After `ec install`, your agent config points at the MCP server (`ec-mcp`). Restart your agent and the four tools below appear.

## How it works

The short version: observe → extract → review → ground → retrieve. The long version — pipeline, ECU schema, confidence math, edge types, scope hierarchy — is at [ems-gold-seven.vercel.app/how-it-works](https://ems-gold-seven.vercel.app/how-it-works). The complete design spec lives in [`docs/SPEC.md`](docs/SPEC.md).

## MCP tools

| Tool | What it does | Parameters |
|---|---|---|
| `ec_observe` | Feeds the agent's reasoning to the extractor; new ECUs land in the **session** brain | `user_prompt`, `reasoning_trace` (required), `final_output` (optional) |
| `ec_query` | Retrieves relevant engineering cognition, demand-driven | `query` (required), `scope` (optional), `mode` (optional — e.g. `debugging`, `architecture`) |
| `ec_get_summary` | Brain stats for awareness without anchoring | — |
| `ec_reconsolidate` | Re-evaluates a retrieved ECU against new evidence | `ecu_id`, `evidence` (required), `relationship` (optional) |

## Configuration

Everything lives in one YAML file, `~/.ec/config.yaml`, deep-merged over the defaults in [`ec/config.py`](ec/config.py). Useful blocks:

- `llm` — provider, model, `base_url`, `api_key_env`. Default: OpenCode Zen (`https://opencode.ai/zen/v1`) with `claude-haiku-4-5`. Temperature 0.0 for classification, 0.3 for extraction.
- `confidence` — base priors per source type, scope multipliers, decay, thresholds.
- `ranking` / `retrieval` / `activation` — retrieval weights and budgets.
- `maintainer` / `clustering` — background maintenance intervals and HDBSCAN settings.

Set the `EC_HOME` environment variable to relocate the whole `~/.ec` directory (useful for tests and sandboxes).

**Offline / local model:** if the `ollama` binary is present, `ec install` offers to configure it automatically and pull `qwen2.5-coder:14b` — no hosted API involved.

## Data and privacy

- Your memory is **one SQLite file**: `~/.ec/ec.db`. No account, no cloud storage, no telemetry.
- What leaves your machine is exactly what the LLM features need: the extraction prompt, your agent's reasoning and final output, and candidate pairs of conclusions (for diffusion and review). That goes to the configured LLM endpoint — **hosted by default, or a local model through Ollama** if you prefer nothing leaving your machine.
- API keys are read from environment variables only; Reverie never writes them anywhere.
- Benchmark run outputs (`runs/`), databases and logs are gitignored — nothing from your `~/.ec` is committed.

## Research

Reverie ships with EC-Bench (`bench/`), a harness that runs a real agent through sequential sessions on a target repository in two conditions — with and without memory — and judges the transcripts. Honest status: **EC-Bench has not shown a clear advantage yet.** The harness is published as a research instrument, not as proof. Current results and method: [ems-gold-seven.vercel.app/research](https://ems-gold-seven.vercel.app/research).

## Contributing

Bug reports, benchmarks on other repos, and spec-grounded PRs are welcome — start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup, ground rules and style. Please follow the [code of conduct](CODE_OF_CONDUCT.md).

## License

[Apache-2.0](LICENSE) © 2026 Mudit Sarda. The extraction prompt ([`ec/prompts/extractor_prompt.md`](ec/prompts/extractor_prompt.md)) is separately MIT-licensed.

## Citation

If you use Reverie in your work, please cite it:

```bibtex
@software{reverie2026,
  author  = {Sarda, Mudit},
  title   = {Reverie},
  year    = {2026},
  url     = {https://ems-gold-seven.vercel.app/},
  version = {0.1.0}
}
```

Machine-readable metadata: [`CITATION.cff`](CITATION.cff).
