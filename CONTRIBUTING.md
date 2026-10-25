# Contributing to Reverie

Thanks for contributing. This covers setup, the test commands, and the two
special rules (harness packages and the usage policy). The root
[AGENTS.md](AGENTS.md) has the full repo guide; this file is the short version.

## Development setup

Requires Python 3.12 (the pinned heavy dependencies ship 3.12 wheels).

```bash
git clone https://github.com/muditsarda1122/Reverie.git
cd Reverie
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Test commands

```bash
pytest -q                      # ~990 tests, fully offline; live tests self-skip
ruff check reverie tests bench conftest.py
mypy                           # config in pyproject.toml
```

The suite runs offline: LLM calls are mocked, and the live tests skip unless
the configured provider's key is set. `REVERIE_HOME` redirects the brain
directory to a temp path for isolation. The tests import the installed
package — the editable install above is required.

Run all three before opening a pull request; CI runs them on Linux and macOS.

## Adding a harness

One package, one registry line, tests, one doc row:

1. `reverie/integrations/<name>/` with an `INTEGRATION` instance subclassing
   `reverie.integrations.base.Integration`.
2. Add it to `INTEGRATIONS` in `reverie/integrations/__init__.py`.
3. Offline tests for detect / install / uninstall / idempotency.
4. A row in `docs/SUPPORTED_HARNESSES.md` with the tested version and what
   was verified, plus updates to `docs/INTEGRATIONS.md` and
   `docs/INSTALLATION.md`.

The rule: no harness name appears in the core (`reverie/` outside
`integrations/`). `tests/test_integration_layers.py` enforces it.

## Changing the usage policy

`reverie/policy.py` is the single source of the agent-facing usage text.
Anything that changes it must also update the copy in `docs/SPEC.md` §28.9
(a test compares them byte-for-byte) and keep the text harness-free.

## Internal documents

These are working documents kept in the repo for contributors; they are not
part of the user documentation:

- [`docs/SMOKE_TEST.md`](docs/SMOKE_TEST.md) — the manual per-harness
  smoke-test checklist.
- [`docs/PRODUCT.md`](docs/PRODUCT.md) — a point-in-time code archaeology
  snapshot (stale by design; trust the code over it).
- [`docs/TECHNICAL_REFERENCE.md`](docs/TECHNICAL_REFERENCE.md) — an older
  technical snapshot, kept for background.
- [`docs/investigations/`](docs/investigations/) — one-off investigations.

## Pull requests

1. Open an issue first for anything that changes behaviour, the config
   schema, or the spec.
2. Fork / branch, keep the change focused, add or update tests — new code
   without tests will not be merged.
3. Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md). Commit messages
   describe the change and never name an LLM model; never commit secrets,
   personal paths, or generated data.

## License

By contributing, you agree that your contributions will be licensed under
the [Apache License 2.0](LICENSE).
