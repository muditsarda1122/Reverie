# Releasing

One-time setup (about five minutes, no token to store):
1. Create an account at https://pypi.org and turn on two-factor authentication.
2. Make the name yours **before** the first release: PyPI → Publishing → "Add a pending publisher" with
   project `reverie`, owner `muditsarda1122`, repository `Reverie`, workflow `release.yml`, environment `pypi`.
3. In the GitHub repo: Settings → Environments → New environment `pypi` (add yourself as a required reviewer if you want a manual gate).

Each release:
1. Bump `version` in `pyproject.toml`, update `CHANGELOG.md`, and `CITATION.cff`.
2. Add the new database fixture (see below), merge to `main`.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`. The workflow builds, checks the tag matches the version,
   installs the wheel into a clean tool environment, then publishes.

Upgrade promise: a user on any earlier release can upgrade and keep their memory.
`tests/test_upgrade_compat.py` opens a real database from each released version with the current code.
When you release, create a database with the released code (as `tests/fixtures/db_v0.2.0.sqlite` was made:
check out the tag, create a `Brain`, insert one item and a session) and commit it as `db_vX.Y.Z.sqlite`.
Schema changes must be additive (`ALTER TABLE ... ADD COLUMN`) and `PRAGMA user_version` only goes up.

If you must test with a token instead: `uv publish --token "$PYPI_TOKEN"` (create the token at
pypi.org → Account settings → API tokens; keep it in an environment variable or your password manager,
never in the repository or in chat).
