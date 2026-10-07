# Contributing to Reverie

Thanks for your interest in contributing. This document covers development
setup, the project's ground rules, and how to propose changes.

## Development setup

Requires Python 3.12 (the pinned heavy dependencies — `torch==2.2.2`,
`numpy<2`, `scikit-learn<1.5` — ship wheels for 3.12).

```bash
git clone https://github.com/muditsarda1122/Reverie.git
cd Reverie
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Verify everything works:

```bash
pytest tests/ -q          # ~500 tests, offline; live API tests auto-skip
ruff check ec tests bench conftest.py
```

The test suite runs fully offline: LLM calls are mocked, and the five live
tests plus the end-to-end test skip unless `OPENCODE_ZEN_API_KEY` is set.
Use `EC_HOME` to redirect the brain directory to a temp path for isolation.

## Ground rules

These follow from the design spec (`docs/SPEC.md`) and keep the project
honest:

1. **Extraction over storage.** The interesting question is *what should be
   stored*, not *how to store more*. Every change must respect this.
2. **Demand-driven, not proactive.** Cognition is never auto-injected; the
   agent retrieves only when it decides to via `ec_query`.
3. **Two brains, one review gate.** The session brain is ephemeral and
   unreviewed. The only path to the canonical brain is the human review gate
   → diffuser.
4. **Confidence math lives in log-odds space** (`ec/confidence.py`). No
   ad-hoc probability arithmetic elsewhere.
5. **Schema changes must be additive** (`CREATE TABLE IF NOT EXISTS`,
   `ALTER TABLE ADD COLUMN`). Never `DROP TABLE` on existing databases.
6. **Stable public surface.** Do not rename the `ec` package, its modules,
   the console scripts (`ec`, `ec-mcp`, `ec-repair`, `ec-start`, `ec-stop`,
   `ec-status`), the MCP tool names (`ec_observe`, `ec_query`,
   `ec_get_summary`, `ec_reconsolidate`), config keys, or paths under
   `~/.ec` — installers, agent instruction files and docs link to them.
7. **Never commit secrets, personal data, or generated data** (`runs/`,
   `*.db`, transcripts).

## Style

- Python 3.12, type hints, snake_case file names.
- LLM calls go through `ec.llm.call_llm()` — never a direct HTTP call from
  feature code. Temperature 0.0 for classification, 0.3 for extraction.
- Tests live in `tests/` as `test_phase<N>_<topic>.py`; offline by default
  with `call_llm` mocked and `now` injectable for time-dependent logic.
- `ruff` is the linter (configured in `pyproject.toml`).
- Keep the installed agent instruction template (`AGENTS.md` block in
  `ec/install.py`) byte-identical with its fenced block in
  `docs/SPEC.md` §28.9 — a test enforces this.

## Proposing changes

1. Open an issue first for anything that changes behaviour, touches the
   config schema, or revises the spec — larger design discussions belong
   there. ([issue templates](.github/ISSUE_TEMPLATE))
2. Fork / branch, keep the change focused.
3. Add or update tests for every change. New code without tests will not be
   merged.
4. Run `pytest tests/ -q` and `ruff check` before opening the PR. CI runs
   both on Linux and macOS.
5. Open a pull request with the
   [PR template](.github/PULL_REQUEST_TEMPLATE.md).

## License

By contributing, you agree that your contributions will be licensed under
the [Apache License 2.0](LICENSE).
