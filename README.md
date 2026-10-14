# Reverie

**Memory for coding agents.** Reverie keeps the engineering conclusions your agent reaches, reviewed by you, grounded in your code and revised as evidence changes, in one local SQLite file. An MCP server: works with [OpenCode](https://opencode.ai) today; Claude Code, Cursor and Codex coming soon.

## What it does

- **Extracts conclusions, not logs.** While your agent works, Reverie distills its reasoning into ECUs — Engineering Cognition Units: "authentication correctness depends on optimistic token refresh", not "TokenManager.refresh() was called".
- **Confidence that evolves.** Every conclusion carries a Bayesian confidence (computed in log-odds space), starts stronger for debugging insights than for planning guesses, and is re-evaluated when new evidence contradicts it.
- **Grounded in your code.** Each ECU references files, symbols and a commit hash. A background maintainer verifies that grounding against the live repository and deprecates conclusions whose code has disappeared.
- **Reviewed by you, independently of sessions.** Observations wait as candidates in one review queue across every branch and repo. Review them with `ec review` in a terminal, or ask your agent to show them and decide in the chat. Nothing reaches the durable canonical brain without your decision.
- **Demand-driven retrieval.** Cognition is never auto-injected. The agent consults it through `ec_query` when it recognizes a gap; a 4-factor ranker (relevance, confidence, scope proximity, recency) returns what fits the current task.

## Status and honest limits

- Developed and tested on **macOS**. Other platforms may work but are untested.
- The terminal review UI is one candidate at a time, plain text — there is no GUI.
- **Review through your agent relies on the agent relaying your choices.** Reverie records which channel applied each decision (terminal, agent, or cli) but cannot verify who actually made it.
- Enabling/disabling is per repo and remembered; you can flip it any time (`ec enable` / `ec disable`). Disabled means fully off for that repo.
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

# 2. Provide an LLM key — any major provider works (see Configuration below),
#    or skip via Ollama. `ec install` detects keys already in your
#    environment and asks at most one question.
export ANTHROPIC_API_KEY=...   # or OPENAI_API_KEY, GEMINI_API_KEY,
                               # OPENROUTER_API_KEY, OPENCODE_ZEN_API_KEY

# 3. Configure your agent and bootstrap ~/.ec
#    (interactive; --all for non-interactive, --dry-run to preview)
ec install

# 4. Just code. Sessions are automatic per branch; the first time your agent
#    uses Reverie in a repo it asks you once whether to turn it on.
cd your-project
# ... work with your agent; it calls ec_observe / ec_query / ec_reconsolidate
# ... whenever you like, review what it learned:
ec review       # terminal UI, one candidate at a time
#    or just say "show my Reverie candidates" and decide in the chat
```

Handy extras: `ec enable` / `ec disable` / `ec repos` (per-repo control), `ec export` / `ec import` / `ec reset` (backup, migrate, wipe), `ec-status` (brain stats), `ec-retry` (recover failed extractions / re-diffuse), `ec-repair` (ops recovery). Full details, including per-agent config files the installer touches, are in [`docs/INSTALLATION.md`](docs/INSTALLATION.md).

After `ec install`, your agent config points at the MCP server (`ec-mcp`). Restart your agent and the tools below appear.

> Deprecated shims, kept for compatibility: `ec-start` now enables the repo and shows status (nothing to "start"), `ec-stop` is an alias for `ec review`, `ec-status` unchanged.

## How it works

The short version: observe → extract → review → ground → retrieve. The long version — pipeline, ECU schema, confidence math, edge types, scope hierarchy — is at [ems-gold-seven.vercel.app/how-it-works](https://ems-gold-seven.vercel.app/how-it-works). The complete design spec lives in [`docs/SPEC.md`](docs/SPEC.md).

## MCP tools

Every tool takes an optional `repo_path` (the agent's workspace path — pass it explicitly; global MCP servers may start in the home folder). In an undecided repo, tools return a short "ask the user" prompt instead of acting; in a disabled repo they do nothing.

| Tool | What it does | Parameters |
|---|---|---|
| `ec_observe` | Feeds the agent's reasoning to the extractor; new ECUs land in the **session** brain | `user_prompt`, `reasoning_trace` (required), `final_output` (optional) |
| `ec_query` | Retrieves relevant engineering cognition, demand-driven | `query` (required), `scope` (optional), `mode` (optional — e.g. `debugging`, `architecture`) |
| `ec_get_summary` | Brain stats for awareness without anchoring; adds one counts-only line when the review backlog is piling up | — |
| `ec_reconsolidate` | Re-evaluates a retrieved ECU against new evidence | `ecu_id`, `evidence` (required), `relationship` (optional) |
| `ec_set_repo_state` | Records the user's enablement answer (`enabled` / `disabled` / `not_now`) or flips a repo on/off | `state` (required) |
| `ec_review_list` | The review queue page for the agent to relay (returns a short-lived `review_token`) | `all`, `by_topic`, `page` (optional) |
| `ec_review_decide` | Applies decisions the **user stated**; refuses a bulk accept-all | `review_token`, `decisions` (required) |

## Configuration

Everything lives in one YAML file, `~/.ec/config.yaml`, deep-merged over the defaults in [`ec/config.py`](ec/config.py). Useful blocks:

- `llm` — `provider` selects from a registry (`opencode_zen`, `anthropic`, `openai`, `google`, `openrouter`, `ollama`, `custom`); fields you don't set (`model`, `base_url`, `api_key_env`, `wire_format`) are filled from the registry, and anything you set explicitly wins. Extras: `wire_format` (`anthropic`/`openai`) overrides the model-name request shape (use it for `claude-*` models on OpenAI-compatible gateways); `openai_reasoning: true` forces the reasoning-model parameter shape (`max_completion_tokens`, no temperature — auto-detected otherwise); `retry_attempts` / `retry_backoff` / `retry_backoff_cap` tune retry-with-backoff on 429/5xx/timeouts; `models.extraction` / `models.classification` / `models.mode_detection` optionally give each task its own model. Default: OpenCode Zen (`https://opencode.ai/zen/v1`) with `claude-haiku-4-5`. Temperature 0.0 for classification, 0.3 for extraction.
- `review` — the queue's behaviour: `expire_after_days` (candidates unreviewed this long become `expired`; 0 disables), `nudge_threshold` / `nudge_age_days` (when `ec_get_summary` mentions the backlog — counts only), `page_size` (candidates shown per page in `ec review` and `ec_review_list`).
- `confidence` — base priors per source type, scope multipliers, decay, thresholds.
- `ranking` / `retrieval` / `activation` — retrieval weights and budgets.
- `maintainer` / `clustering` — background maintenance intervals and HDBSCAN settings.

Set the `EC_HOME` environment variable to relocate the whole `~/.ec` directory (useful for tests and sandboxes).

**Offline / local model:** `provider: ollama` with any locally pulled model (the installer's default is `qwen2.5-coder:14b`). If the `ollama` binary is present, `ec install` offers to configure it automatically and pull that model — no hosted API involved.

## Data and privacy

- Your memory is **one SQLite file**: `~/.ec/ec.db`. No account, no cloud storage, no telemetry.
- What leaves your machine is exactly what the LLM features need: the extraction prompt, your agent's reasoning and final output, and candidate pairs of conclusions (for diffusion and review). That goes to the LLM endpoint **you** configure — any major hosted provider, or a local model through Ollama if you prefer nothing leaving your machine.
- API keys are read from environment variables only — including by `ec install` and its live ping; Reverie never writes them anywhere.
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
