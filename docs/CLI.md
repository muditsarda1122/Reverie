# Reverie CLI reference

Every `reverie` command and flag. Exit codes: `0` success, `1` failure
(printed to stderr), `2` unknown command / usage error.

The MCP server is `reverie-mcp` (launched by your agent's config — not meant
to run by hand). There are no other console scripts.

## reverie (bare)

Prints the command help.

```bash
reverie
```

A mistyped first word (`reverie reveiw`) is an error with a "did you mean"
hint (exit 2).

## reverie install

One command configures Reverie for all detected coding agents: the MCP
server entry per harness (the ABSOLUTE path of the running environment's
`reverie-mcp` script — harnesses launch servers outside your virtualenv),
the agent instructions reference, the user-level `/reverie-review` and
`/reverie-status` commands, and the `~/.reverie` bootstrap (database,
config, instructions). Also runs the LLM-provider step: detects keys in
your environment, offers the detected one, merges the resolved `llm` block
into `~/.reverie/config.yaml` (the key itself is NEVER written to disk),
and verifies with one cheap live ping. Ends by launching each written
server command, sending an MCP `initialize` (≤10 s) and printing ✓/✗ per
harness — a command that does not start is reported as "Setup incomplete"
(exit `1`), never "Reverie is ready!". Then prints a per-harness summary of
what was written and what to do next.

```bash
reverie install                     # interactive: pick harnesses + provider
reverie install --all               # every detected harness, no prompts
reverie install --only claude_code,codex --provider anthropic
reverie install --dry-run           # print every planned write; change nothing
reverie install --import memory.zip # restore an export on a new machine
```

| Flag | Effect |
|---|---|
| `--all` | configure every detected agent (non-interactive) |
| `--only A[,B…]` | configure exactly these agents by key (`claude_code`, `codex`, `cursor`, `opencode`, `gemini_cli`); may be undetected |
| `--provider NAME` | select the LLM provider without prompting (registry key, e.g. `anthropic`, `openai`, `google`, `openrouter`, `opencode_zen`, `ollama`) |
| `--no-verify` | skip the live provider ping |
| `--import FILE` | import a brain export during setup (only into an empty brain) |
| `--dry-run` | print every planned write with its full content; write nothing |

Exit: `0` configured (or dry run); `1` on `InstallError` — malformed agent
config (invalid JSON/TOML/YAML), unknown `--only`/`--provider` key. No
changes are made to a file that fails to parse.

## reverie enable

Turns Reverie on for a repo. Sessions are automatic per branch afterwards.

```bash
reverie enable                # current directory
reverie enable ~/work/api    # a specific repo
```

| Argument | Effect |
|---|---|
| `path` (optional) | repo path (default: current directory) |

Exit: `0` enabled; `1` when the path is not a git repository.

## reverie disable

Turns Reverie off for a repo — fully off: nothing is captured, retrieved or
injected. Existing memory is kept; `reverie enable` restores everything.

```bash
reverie disable
```

| Argument | Effect |
|---|---|
| `path` (optional) | repo path (default: current directory) |

Exit: `0` disabled; `1` when the path is not a git repository.

## reverie repos

Lists every known repo with its state and counts.

```bash
reverie repos
```

Output per repo: path, enablement state, active sessions, review
candidates, conclusions grounded there, and the origin remote (when known).
Undecided repos that were asked show `(asked once — still undecided)`.
Exit: `0` always.

## reverie review

The terminal review UI: one candidate at a time, single keypresses
(a/r/s/e/g/u/q), `$EDITOR` edit, contradiction pairs shown together, undo of
the last reversible decision, pages of 10 with "more" always available. One
queue across every session, branch and repo.

```bash
reverie review                          # interactive TUI, current repo
reverie review --list --json            # script: JSON listing
reverie review --decide <id>=accept     # script: apply decisions
reverie review --all --by-topic        # every repo, grouped by topic
reverie review --expired               # revive candidates that expired
```

| Flag | Effect |
|---|---|
| `--repo PATH` | only this repo's candidates (default: current repo; outside a repo: everything) |
| `--branch NAME` | only this branch |
| `--all` | every repo's candidates |
| `--by-topic` | group by topic instead of strongest-first |
| `--expired` | review expired candidates (reviving stamps a fresh lease) |
| `--list` | list candidates non-interactively and exit |
| `--json` | with `--list`: emit JSON |
| `--decide ID=ACTION…` | apply decisions non-interactively (accept / reject / skip) |
| `--resolve-open-questions POLICY` | `auto` (prompt when interactive, skip otherwise) / `skip` (leave parked) / `archive` (archive all without prompting) |

Exit: `0` reviewed/no candidates; `1` when a `--decide` id was not applied
(unknown id or a candidate that could not be decided).

## reverie status

The session + brain report: brain statistics, the session for the current
(repo, branch), the session-memory conclusion count, and the brain-wide
pending-review count ("all sessions"). If no session exists yet for the
branch it says so and notes one starts on the next Reverie call. If the
repo is disabled the report says `Reverie is off for this repo.` and no
session is reported. Timestamps show local time with the UTC offset. Also
emitted by the `summary` MCP tool. Additionally checks that the server
command written to each harness config still exists — a moved or deleted
virtualenv breaks the absolute path, and the report then says to run
`reverie install` again.

```bash
reverie status
reverie status --repo ~/work/api --branch main
reverie status --all                   # every enabled repo's active sessions
```

| Flag | Effect |
|---|---|
| `--repo PATH` | repo path (default: git detect) |
| `--branch NAME` | branch (default: git detect) |
| `--all` | every enabled repo's active sessions, one line each |

Exit: `0` always (reports "No active session." when none).

## reverie export

Exports the whole brain to one ZIP: a consistent `VACUUM INTO` snapshot plus
`manifest.json` (Reverie version, schema version, embedding model +
dimensions, row counts, date).

```bash
reverie export                  # ./reverie-export-<date>.zip
reverie export ~/backups/r.zip
```

| Argument | Effect |
|---|---|
| `file` (optional) | output ZIP path (default: `./reverie-export-<date>.zip`) |

Exit: `0` written; `1` on a failed export.

## reverie import

Imports a brain export. Direct when the local brain is empty; `--replace`
otherwise (an automatic backup export is taken first and its path printed).
Merging two brains is not supported. Refused when the embedding model or
dimensions mismatch, or when the export's schema is newer than this
Reverie.

```bash
reverie import memory.zip
reverie import memory.zip --replace
```

| Argument / flag | Effect |
|---|---|
| `file` | export ZIP path |
| `--replace` | replace the non-empty local brain (automatic backup first) |

Exit: `0` imported; `1` on a failed import (nothing changed — the import is
one transaction with rollback).

## reverie reset

Wipes the brain (or one repo's sessions and candidates), always taking an
automatic backup first and printing its path.

```bash
reverie reset               # asks you to type 'reset'
reverie reset --yes         # skip the prompt (the backup is still taken)
reverie reset --repo ~/work/api   # only that repo (long-term memory kept)
```

| Flag | Effect |
|---|---|
| `--repo PATH` | reset only this repo's sessions and candidates; long-term memory and enablement are kept (worktrees included) |
| `--yes` | skip the confirmation prompt (the automatic backup is still taken) |

Exit: `0` reset; `1` aborted confirmation or failed backup (nothing wiped).

## reverie retry

Recovery for failed LLM work. Failed extractions are persisted
(`pending_extractions`) and retried automatically at the next successful
`observe`; this retries them on demand. Also re-runs the Full
Diffuser for conclusions whose review-gate accept could not diffuse
(promoted-but-undiffused).

```bash
reverie retry                       # retry all pending extractions
reverie retry --list                # show them
reverie retry --drop <id>           # discard one without retrying
reverie retry --rediffuse           # re-diffuse undiffused conclusions
reverie retry --rediffuse --ecu-id <id>
```

| Flag | Effect |
|---|---|
| `--list` | list pending extractions and exit |
| `--id ITEM_ID` | retry only this pending extraction id |
| `--drop ITEM_ID` | discard this pending extraction without retrying |
| `--rediffuse` | re-run diffusion for undiffused conclusions instead of retrying extractions |
| `--ecu-id ECU_ID` | with `--rediffuse`: only this id |
| `--repo PATH` | only items for this repo (default: all) |
| `--branch NAME` | only items for this branch (default: all) |

Exit: `0` done (individual failures are reported in the output, never
raised); `1` when `--id` matched nothing.

## reverie repair

Brain integrity check and safe, non-destructive recovery: file is valid
SQLite, every table exists (missing ones are created — additive-only), WAL
checkpointed, orphan edges removed, orphan session rows reported, every
table queryable. Never modifies conclusion data.

```bash
reverie repair
reverie repair --dry-run         # report problems without fixing
reverie repair --db ~/other/reverie.db
```

| Flag | Effect |
|---|---|
| `--dry-run` | report problems without applying fixes |
| `--db PATH` | brain path (default: `$REVERIE_HOME/reverie.db`) |

Exit: `0` healthy or fixed; `1` when problems remain.

## reverie uninstall

Removes exactly what `reverie install` did, nothing more. The full plan —
every harness entry to remove, and whether data will be exported and
deleted — is printed first, and ONE confirmation covers it. Nothing is
touched before you confirm; aborting leaves everything unchanged. Only
Reverie's own entries are removed (merge logic in reverse); edited or
invalid files are skipped and reported, never damaged.

```bash
reverie uninstall                         # harness entries; ~/.reverie kept
reverie uninstall --delete-data           # also remove ~/.reverie (export first)
reverie uninstall --delete-data --no-export
reverie uninstall --dry-run               # the plan, exact before/after, nothing changed
reverie uninstall --yes                   # skip the confirmation (scripts)
```

| Flag | Effect |
|---|---|
| `--delete-data` | also remove `~/.reverie` — a final export is written outside it first (path printed) |
| `--no-export` | with `--delete-data`: skip the final export (the confirmation still covers the deletion) |
| `--yes` | skip the confirmation prompt; the export still happens unless `--no-export` |
| `--dry-run` | print every planned removal with the exact before/after; change nothing |

The last step is always yours: it prints the exact `pip uninstall reverie`
command (or the pipx/uv equivalent) — the running package can't remove
itself.

Exit: `0` uninstalled (or dry run); `1` aborted (nothing changed) or the
final export failed (nothing removed).

## Environment variables

| Variable | Meaning |
|---|---|
| `REVERIE_HOME` | relocate the whole `~/.reverie` directory (tests, sandboxes) |
| `REVERIE_REPO_PATH` + `REVERIE_BRANCH` | override repo/branch detection (both required; used by MCP launches outside a work tree and by tests) |
| provider key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `OPENCODE_ZEN_API_KEY`) | read from the environment at call time; never written to disk |
