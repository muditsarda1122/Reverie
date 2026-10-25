# Reverie — Guide for Coding Agents

You are working on Reverie: a local-first engineering-memory layer for AI
coding agents. It stores engineering conclusions from coding sessions and
puts them back to the agent later, after the user reviews them. Read this
file before changing anything; `docs/SPEC.md` is the authoritative design
reference for anything it does not cover.

## Repo layout

```
reverie/                 # the pip-installable package
├── cli.py               # `reverie` multi-command CLI (thin dispatch)
├── mcp_server.py        # hand-rolled NDJSON MCP server (stdio)
├── core_api.py          # harness-neutral API behind the MCP tools and CLI
├── policy.py            # THE usage policy — single source for agent-facing text
├── install.py           # `reverie install` (plans, provider step, ~/.reverie)
├── uninstall.py         # `reverie uninstall` (plan-first removal)
├── hooks.py, launch.py  # hook plumbing shared with the integrations
├── brain.py, schema.sql # persistence (one SQLite file, additive-only schema)
├── config.py            # every tunable + YAML deep-merge loader
├── extractor.py, prompts/  # prompt-based conclusion extraction
├── diffuser.py, retrieval.py, confidence.py, activation.py
├── session.py, review_queue.py, review_gate.py, review_cli.py
├── maintainer.py, grounding.py, clustering.py, reconsolidation.py
├── transport.py, pending.py, repair.py, repos.py
├── providers.py         # LLM provider registry (base URLs, key env vars)
├── llm.py               # the only place that talks to an LLM
└── integrations/        # ONE PACKAGE PER HARNESS (see the rule below)
    ├── base.py, common.py   # the Integration contract + shared planners
    ├── claude/, codex/, cursor/, opencode/
tests/                   # pytest suite (offline; live tests self-skip)
bench/                   # the benchmark harness (separate concerns, no product code)
docs/                    # SPEC, CLI reference, per-harness docs (see docs/READMEs)
pyproject.toml           # package definition, console scripts, tool config
```

## The integration rule

**No harness name may appear in the core.** Harness names, config paths,
command locations, hook schemas and permission syntax live only in
`reverie/integrations/<harness>/`. The core (`reverie/*.py` outside
`integrations/`) asks the registry in `reverie/integrations/__init__.py`
(`INTEGRATIONS`) and never imports a harness package.

Enforced by:

- `tests/test_integration_layers.py::test_core_does_not_import_harness_packages`
- `tests/test_integration_layers.py::test_core_has_no_harness_config_paths_or_hook_events`

Run them before committing anything that touches install/uninstall, hooks,
or the MCP server.

## The usage policy is single-source

`reverie/policy.py` (`USAGE_POLICY`, `RECONSOLIDATE_TRIGGER`) is the single
source for the "When to call each command" text. It is injected into the
installed agent instructions (`AGENTS_MD` in `reverie/install.py`) and into
the MCP tool descriptions. `docs/SPEC.md` §28.9 carries a copy of the
installed instructions; `tests/test_phase5_install.py` asserts the copy
matches byte-for-byte. If you change the policy, update the SPEC copy in the
same commit and keep both harness-free.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # editable install — required for the tests
pytest -q                      # the whole suite, offline
ruff check reverie tests bench conftest.py
mypy                           # config lives in pyproject.toml (files = reverie)
```

The tests import `reverie` from the installed package: an editable install
(`pip install -e ".[dev]"`) is required, not optional. LLM calls are mocked;
live tests skip unless a provider key is set. `REVERIE_HOME` redirects the
brain directory for isolation.

These commands were verified from a clean virtualenv with Python 3.12.

## Before you commit

1. `pytest -q`, `ruff check reverie tests bench conftest.py` and `mypy` all
   pass.
2. New behaviour has tests. Offline by default, `call_llm` mocked, `now`
   injectable for time logic.
3. Schema changes are additive (`CREATE TABLE IF NOT EXISTS`,
   `ALTER TABLE ADD COLUMN`). Never `DROP TABLE`.
4. User-facing text says "conclusion"; "ECU" is the internal schema name.
5. Commit messages describe the change; never name an LLM model.
6. Never commit secrets, API keys, personal paths (`/Users/...`), or
   generated data (`runs/`, `*.db`).

## Adding a harness

1. Create `reverie/integrations/<name>/__init__.py` with one `INTEGRATION`
   instance subclassing `reverie.integrations.base.Integration` (`key`,
   `name`, `binary`, `marker`, `next_step`, `detect`, `install`,
   `uninstall`, `configured_server`). Use the planners in
   `reverie/integrations/common.py`; never write files directly.
2. Register it: add the instance to `INTEGRATIONS` in
   `reverie/integrations/__init__.py` — one line.
3. Tests: detect / install / uninstall / idempotency in a sandboxed home.
4. Add a row to `docs/SUPPORTED_HARNESSES.md` (tested version + what was
   verified) and update `docs/INTEGRATIONS.md` and `docs/INSTALLATION.md`.

## Changing the MCP surface

Seven tools (`observe`, `query`, `summary`, `reconsolidate`,
`set_repo_state`, `review_list`, `review_decide`) are the public contract;
renaming or removing them breaks installed agents. `docs/CLI.md` must list
every CLI subcommand and flag — `tests/test_cli_docs.py` fails when the
parser grows undocumented surface.
