# Reverie CLI reference

Every `reverie` command and flag. Exit codes: `0` success, `1` failure
(printed to stderr), `2` unknown command / usage error.

Global flag: `reverie --client KEY <command>` records the harness key that
launched the command (for logs; no behaviour change).

The MCP server is `reverie-mcp` (launched by your agent's config — not meant
to run by hand). There are no other console scripts.

## reverie (bare)

Prints the command help (as does `reverie --help`).

A mistyped first word (`reverie reveiw`) is an error with a "did you mean"
hint (exit 2).

## reverie install

One command configures Reverie for all detected coding agents: the MCP
server entry per harness (the ABSOLUTE path of the running environment's
`reverie-mcp` script — harnesses launch servers outside your virtualenv),
the per-harness instructions reference, the user-level `/reverie-review` and
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
reverie install --trust-codex-hooks      # default: approve Reverie's own Codex hooks via Codex's API
reverie install --no-trust-codex-hooks   # leave Codex hook approval to you (/hooks)
reverie install --import memory.zip # restore an export on a new machine
reverie install --refresh-instructions   # update only out-of-date instruction text already installed
```

| Flag | Effect |
|---|---|
| `--all` | configure every detected agent (non-interactive) |
| `--only A[,B…]` | configure exactly these agents by key (`claude_code`, `codex`, `cursor`, `opencode`); may be undetected |
| `--provider NAME` | select the LLM provider without prompting (registry key, e.g. `anthropic`, `openai`, `google`, `openrouter`, `opencode_zen`, `ollama`) |
| `--no-verify` | skip the live provider ping |
| `--import FILE` | import a brain export during setup (only into an empty brain) |
| `--dry-run` | print every planned write with its full content; write nothing |
| `--refresh-instructions` | rewrite only Reverie's instruction text that is already installed and out of date (`~/.reverie/AGENTS.md` and the Reverie block in a harness's AGENTS.md, after the usage policy changed in a newer Reverie); nothing else is touched, a missing block is never created, text outside the block's markers is left alone; combine with `--dry-run` to preview. `reverie status` warns when this is needed |

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
Exit: `0` always.

## reverie usage

Shows, per memory item, how often agents **used**, **ignored** or **contradicted**
it (and how often it was **irrelevant** or rightly **overridden** by what you asked).
Reverie logs every delivery (session start, before an edit, `query`) and, after an
exchange is captured, one batched judge call labels the delivered items using your
prompt, the real diff and the agent's reply. An item that agents keep contradicting is
marked challenged and delivery says so. Turn the delivery log off with
`REVERIE_NO_USAGE_LOG=1`.

```bash
reverie usage [--repo PATH]
```

Exit: `0` on success, `1` when there is no memory yet.

## reverie merge

Merges near-duplicate memory items for this repo: same kind, embeddings very
close (`dedupe.merge_sim`), same polarity and numbers (so "never retry 429" is
never merged with "retry 429"). The keeper is the user's own words, then the
most-seen, then the oldest, and absorbs the others' "seen" counts; the others
become `superseded` with the keeper named in `meta.merged_into`. Nothing is
deleted. Runs by itself at most once a day at session start. Items that are
near-identical but not yet merged are shown once, with "(seen N times)".

```bash
reverie merge [--repo PATH]
```

Exit: `0` on success, `1` when there is no memory yet.

## reverie install: profiles

An agent can have several config directories (`CLAUDE_CONFIG_DIR=~/.claude-work`,
a second `~/.claude-*`, `CODEX_HOME`, a relocated `XDG_CONFIG_HOME`). Reverie
never guesses which one your agent reads. If your shell's environment points at
one, it installs there and lists the others. If several exist and nothing points
at one, it asks (interactive) or stops and lists them (`--all` / `--only`).

```bash
reverie install --list-profiles                      # what is on this machine
reverie install --profile claude_code=~/.claude-work   # choose explicitly (repeatable)
reverie install --profile codex=~/.codex-alt
```

## reverie transcripts

Reads the transcripts the harnesses already keep (Claude Code, Codex,
opencode, Cursor) for this repo and its worktrees, and adds the new prompts
and answers to the prompt log, redacted. This also runs on its own at session
start, `summary` and `resume`, so headless, scripted and killed sessions are
covered. A cursor (file + offset) means nothing is read twice; the raw
transcripts are never copied. Exchanges not yet run through extraction are
handed to a background worker. `REVERIE_NO_IMPORT=1` turns the automatic
import off; a repo disabled with `reverie disable` is never read.

```bash
reverie transcripts [--repo PATH]
```

Exit: `0` always.

## reverie resume

Prints what a fresh agent needs to continue a cut-off session, in any
harness: your rules, every prompt you sent in the session (verbatim, in
order; the last may be unhandled), and the git state. The prompt log is
written by the harness hooks (Claude Code: UserPromptSubmit/Stop) to
`~/.reverie/spool/`, crash-safe and append-only.

```bash
reverie resume                  # latest session in this repo
reverie resume --session <id>   # a specific session
reverie resume --all            # every recorded prompt
reverie resume --latest         # skip the picker, take the most recent session
reverie resume --prompt 3       # prompt 3 of the session in full (the card trims long ones)
```

Exit: `0` always.

## reverie rules

Your always-on rules, stored verbatim as markdown bullets: `~/.reverie/rules.md`
(personal, every repo) and `<repo>/.reverie/rules.md` (shared through git).
Agents read them at session start and never edit them.

```bash
reverie rules add "Never use print() for diagnostics"          # this repo
reverie rules add --user "Prefer minimal diffs"                # everywhere
reverie rules list
```

## reverie context

Team context as plain markdown, in scopes. A document says where it applies: an
org, a team, this repo, paths inside it, or you. Reverie has no list of roles.
Anyone can contribute; who may accept or lock things is whatever your git
setup already says (CODEOWNERS plus the git identity of whoever runs the command).

Where documents live: `.reverie/context/*.md` (repo), `~/.reverie/context/*.md`
(you), and folders of `.md` named in `.reverie/context.toml` (or `~/.reverie/context.toml`).
Several orgs and teams can share one repo:

```toml
[sources]
org  = ["../acme/org", "../globex/org"]
team = ["../acme/payments", "../acme/platform"]
```

Format: a section headed Rules / Always / Never / Must holds rules, one per
bullet; every other bullet is guidance, delivered as a pointer (`reverie show <id>`).
A bullet may end with `{id: name}` (so another scope can answer the same rule),
`{resolves: key}` (a recorded decision) and `(locked)`. Optional lines at the top
of a document: `applies-to: repo-a, repo-b`, `applies-to-paths: services/billing/**`
(the rules apply only to files under those paths; the edit hook delivers them
when you touch such a file), `owner: whom to ask`, `inside: acme` (a team naming
its org).

How scopes combine: the narrower scope answers the wider (org, then team, then
repo, then you) unless the wider rule is locked. Sibling scopes (two teams, two
orgs) have no order; if their rules contradict, the conflict is UNRESOLVED: both
are shown, neither is applied, and the agent is told to ask you. A lock only
binds inside its own scope; two locks that contradict are unresolved too.
Every conflict is listed with both rules, their owners and a status
(overridden, locked or unresolved); nothing is dropped silently.

```bash
reverie context list                                   # scopes, conflicts, flags
reverie context add "Error messages never blame the user"      # a rule, repo scope
reverie context add --layer org --scope acme/org --locked --tag no-pii "Never log card numbers"
reverie context add --paths "services/billing/**" "Amounts are integer cents"
reverie context add --guidance --doc deploys "Deploys go through the staging gate"
reverie context add --why "support asked" "Never blame the user"   # becomes a proposal if the folder has owners
reverie context proposals                              # open proposals
reverie context accept <name>                          # an owner moves a proposal into its document
reverie context reject <name> --why "Ledgers use Decimal"   # an owner declines; the reason is kept
reverie context propose "integer cents"                # one of YOUR rules -> a proposal for the team (default --layer team)
reverie context propose a1b2c3d4 --layer org --scope acme/org --why "bit us twice"   # a memory rule by id
reverie context propose "integer cents" --again --why "ledgers changed"    # a rule that was rejected before
reverie context resolve <conflict-key> "Public APIs use REST, internal ones gRPC"
reverie context check                                  # judge cross-scope conflicts + staleness
reverie context check --since v1.2                     # recheck commits since a revision
reverie context check --if-changed                     # quiet; only if a document or the code changed
```

`add` writes the bullet directly when nobody owns the target document or you are
one of its owners. Otherwise it writes a proposal file under `proposals/` next to
the document, to be reviewed like any pull request; an owner runs `accept`.
`propose` is the promotion path: a rule you taught your agent (a bullet of
`~/.reverie/rules.md` by its text or a unique fragment, a rule you stated that is stored
as a memory item by id, or a personal context rule by id; agent-inferred items cannot be
promoted) becomes a proposal for a team, org or repo document. Nothing leaves your
personal scope until you run it. It follows the same permission as `add`: an owner of the
target writes directly (self-approving), anyone else writes a proposal for the owners. The
proposal carries your words verbatim, where the rule came from, the date, your usage
counts when they exist, and your `--why`. A rule that looks like a secret is refused, not
stripped; one already in a document or already proposed is refused as a duplicate.
`reject` (owners only, `--why` required) moves the proposal to `proposals/rejected/` with
the reason, who and when: it stays in git history and `context proposals` shows it to the
proposer. It is never delivered to an agent. `propose` on a rule that was rejected refuses
and prints the reason; `--again --why "what changed"` is the only way to re-propose it.
After `accept`, your own copy is not deleted: it is hidden at DELIVERY time while the team
rule is in force, and shown again as soon as that rule is removed, overridden by a narrower
rule, or flagged outdated.

`resolve` writes a `{resolves: key}` decision into this repo's context (or a
proposal for it): it settles the conflict and is remembered because it lives in git.
An optional `authority` file (`legal = @dana, @sam`) only adds display labels to
owner names; nothing in Reverie branches on a label.

`check` calls the model (conflicts: one call per rule, cached by content;
staleness: the diff judge on commits touching a bullet's backticked files and
symbols). Results are kept in `~/.reverie/context_state.json`, never written into
the shared documents.

**Detection and the hint are separate.**
- *Detection* (the judge above) runs only where a model call is allowed, never in a
  hook: at the end of `context add` and `context accept` (when the document changed),
  in a detached worker that session start launches when a document's content hash
  or the repo's HEAD changed since the last run (`context check --if-changed`), and
  once a day (a full recheck, with the daily merge job's cadence). A missing or
  failing judge changes nothing: delivery is identical, nothing is recorded as
  checked, and the next run tries again. Conflicts the judge found are then in the
  start block as UNRESOLVED before the agent starts.
- *The hint* is a mitigation, not detection: when more than one org or team source
  applies, the start block carries one fixed line, "If two rules above seem to
  contradict, ask before acting." It says nothing about whether a conflict exists;
  repos with a single source pay no extra context.

## reverie ui

A read-only page in your browser. It is a command, not a daemon: it serves on
`127.0.0.1` until you press Ctrl-C and changes nothing.

```bash
reverie ui                       # a free port, opens your browser
reverie ui --port 8765           # a fixed port (says so if it is taken)
reverie ui --no-open             # print the address only
reverie ui --repo ~/code/api     # another repo (default: the current directory)
```

The home page is in plain language and starts with what needs action: conflicts
between rules that agents are told to ask about, rules that may be outdated, and
proposals waiting for an owner, each with the command that deals with it. Below
that are counts of the rules in force and one-click views: **What agents are told**
(exactly the text a session starts with, the same function the CLI uses), **Team
context** (scopes, marks, conflicts), **Proposals** (waiting, and turned down with
the reason), **Memory** (paged, 100 per page) and **Repos**. A repo that is turned off
shows only that. Looking at the start block is not a delivery and is not counted in
usage.

Safety and privacy: only GET and HEAD answer (anything else is 405); the brain is opened
read-only (SQLite may create its own `-wal`/`-shm` files, the database is not
changed); it binds to `127.0.0.1` and refuses a Host header that is not its own
address; every value is HTML-escaped and passed through redaction (the start block is
shown exactly as delivered); no script and no external resource, a strict
Content-Security-Policy; no access log and no telemetry of any kind. The page never
reports its own usage.

## reverie show

Prints one memory item in full (id prefix is enough), with its anchor,
source, author and whether its file changed since it was verified.

```bash
reverie show a1b2c3d4
```

## reverie undo

Reverses an automatically accepted item. Nothing Reverie remembers is
permanent: `--restore` brings it back.

```bash
reverie undo a1b2c3d4
reverie undo --restore a1b2c3d4
```

## reverie digest

What Reverie remembered since you last looked, with who said it (you, or the
agent's inference) and the undo command for each. There is no review gate;
this is the occasional look. Session start nudges once enough has piled up.

```bash
reverie digest            # this repo, marks everything seen
reverie digest --all      # every repo
reverie digest --no-mark  # look without marking
```

## reverie stale

Keeps memory true as the code moves. Git is only a trigger: for each commit
since the last check, only memories whose anchors (files, or symbols in the
diff) the commit touched are rechecked. The test suite runs first (at HEAD,
clean tree only) and a failure is passed to the judge as evidence. A memory
the judge finds CONTRADICTED is never silently rewritten: it is flagged
(shown as stale wherever it is delivered) and a superseding **candidate** is
stored for you to accept or reject. Session start launches a background check
when new commits exist.

```bash
reverie stale check               # recheck since the last checked commit
reverie stale check --since v1.2  # or since a revision
reverie stale check --tests       # also run the project's tests as extra evidence (off by default)
reverie stale list                # pending superseding candidates
reverie stale accept <candidate>  # the candidate replaces the old memory
reverie stale keep <memory>       # the old memory was fine; clear the flag
```

The first check in a repo only records HEAD as the baseline.

## reverie promote

Makes a memory item user-wide so it is delivered in every repo (the way to
mark something "always" or "never"), or takes it back. Reverie also promotes
by itself, but only agent-inferred preferences seen in two repos; repo facts
and decisions never promote. Things you say that cover all your work ("I
always ...") are user-wide from the start.

```bash
reverie promote a1b2c3d4
reverie promote --demote a1b2c3d4   # back to the repo it came from
```

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
`reverie install` again. It also warns when Reverie's Codex hooks are installed
but not approved: Codex skips an unapproved hook silently, so none of Reverie's
hooks would fire there. The warning says how many are unapproved and to run
`reverie install` (which approves Reverie's own hooks) or type `/hooks` in Codex.
The check is read-only (it asks Codex's own app-server, never approves anything),
gives up after 10 seconds instead of hanging, and says so when it could not check.
It also warns when the instruction text Reverie installed (`~/.reverie/AGENTS.md`,
the Reverie block in a harness's AGENTS.md) is out of date after an upgrade, and
points to `reverie install --refresh-instructions`.

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

## reverie hook

Lifecycle hooks for agent harnesses. `reverie install` wires these into the
harness config; the harness itself invokes them — you never need to run them
by hand.

```bash
reverie hook session-start            # print the session-start context block
reverie hook session-start --repo ~/code/app
```

`session-start` is installed as a Claude Code SessionStart hook: its
plain-text stdout is injected into the conversation at startup, so a fresh
session sees Reverie's state and usage rules without a first tool call. The
matching Stop hook (`reverie capture-turn`) captures the completed exchange.
Two outputs: **enabled** (the default for every repo) — brain counts
(conclusions in memory, candidates awaiting review) plus the usage rules;
**disabled** — a one-line
"do not capture or retrieve here". Contract: read-only (no LLM, no writes,
never creates a brain), quiet on error (not a git repo, unreadable brain →
no output), always exits `0` — a hook can never break a session.

| Flag | Effect |
|---|---|
| `--repo PATH` | workspace path (default: current directory) |

Exit: always `0` (hook contract).

## reverie capture-turn

The Claude Code Stop-hook entry point. Reads the hook JSON on stdin, takes the
last real user request and final assistant text from the transcript, and hands
them to the same extraction path as `observe`. Installed automatically as an
async Stop hook with a 20-second timeout; it prints nothing and always exits
`0`, so it can never block Claude.

```bash
reverie capture-turn --transcript ~/.claude/projects/.../session.jsonl
reverie capture-turn --repo ~/code/app --branch main
```

| Flag | Effect |
|---|---|
| `--transcript PATH` | Claude JSONL transcript (default: `transcript_path` from stdin) |
| `--repo PATH` | Repository path (default: hook `cwd`) |
| `--branch NAME` | Branch (default: detected) |
| `--timeout SECONDS` | Extraction time limit (default: `20`) |

A failed or timed-out capture is left in the pending queue; `reverie retry`
retries it. Disabled repos capture nothing. Exit: always `0`.

Hook schemas and per-harness details: [`INTEGRATIONS.md`](INTEGRATIONS.md).

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
| `REVERIE_PROVENANCE_TOOLS=0` | do not use tool output (test runs, command output) as evidence for a memory item; by default Claude Code's captured exchange includes the real tool results of that exchange (redacted, bounded), which can back facts but never a rule or preference |
