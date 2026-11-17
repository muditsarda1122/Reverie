# Reverie web UI: inventory of what exists today

Scope: `reverie ui` (default `http://127.0.0.1:<free port>/`, or `--port 8765`). Written from the code on branch
`feat/continuity-spine`. First written at commit `8610cde`, then updated for the eleven follow-up fixes (nine fixes `3a7d7c3`..`cbd62b0`,
then `df55dbc` and `e5039fb`). Every statement comes from reading the files named. Anything inferred is marked **(inferred)**. Unclear points are collected under "Open questions".

Files read: `reverie/ui.py` (the whole server, all HTML, CSS and copy: there are no templates and no static files),
`reverie/context.py`, `reverie/context_propose.py`, `reverie/context_check.py`, `reverie/rules.py`, `reverie/deliver.py`,
`reverie/items.py`, `reverie/staleness.py`, `reverie/usage.py`, `reverie/digest.py`, `reverie/spool.py`, `reverie/redact.py`,
`reverie/cli.py`, `reverie/schema.sql`, `reverie/hooks.py`, `reverie/typed_extractor.py`, `tests/test_ui.py`,
`tests/test_ui_writes.py`, `docs/CLI.md`, `docs/ROADMAP.md`.

Server facts that apply to every screen (`reverie/ui.py`):

- One Python file, standard library only (`http.server.ThreadingHTTPServer`). Pages are built by string concatenation in
  functions. There are no templates, no JavaScript, no images, no fonts, no external requests. The CSS is one string, `CSS`,
  inlined into every page. Icons are inline SVG from `_ICONS`.
- Content-Security-Policy `default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`
  (`CSP`). Because no script is allowed, **nothing on any page can update without a full page load**.
- Every page is rendered fresh on each request (`render()`), reading files and SQLite each time. There is no cache.
- Reads are GET/HEAD. The only POST routes are `/do/add`, `/do/accept`, `/do/reject` (`WRITE_PATHS`). Everything else is 405.
- A request whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>` gets a bare `403 forbidden`.
- Every text value is passed through `redact.redact` and HTML-escaped by `esc()`, except the "What agents are told" text
  (`esc(text, redacted=False)`, because the start block is already redacted at its source).
- No logging, no telemetry (`log_message` does nothing).
- Everything is one-per-machine: `reverie ui` serves one user's local files and one local SQLite file. It is not a shared web service.

---

## 1. The model

### Project
A **project** is a git repo that Reverie knows about (`projects()` in `ui.py`). The list is: the repo `reverie ui` was started in
(`spool.repo_root`), plus every row of the `repos` table in `~/.reverie/reverie.db` that has a `path` (`SELECT path FROM repos`).
A repo gets a row when `reverie enable`/`disable` is run in it (`repos.set_repo_state`) or when an agent first uses Reverie there
**(inferred, from `schema.sql` comments)**. The UI refers to a project by an opaque id, `sha1(path)[:8]` (`_pid`), never by path.
A project has a state: `enabled`, `disabled` or `undecided` (`repos.state`). The UI only distinguishes `disabled` from everything
else (`deliver.is_disabled`). Disabled means Reverie delivers nothing and the UI shows nothing but "turned off", except that the Connect tab still shows the steps to turn it back on.

### Rule (three different things share this word)
The UI says "rule" for all of these. They live in different places, have different authority, and the UI treats them unequally.

| What | Written by | Stored in | Shared with teammates? | Who may write | Shown in the UI? |
|---|---|---|---|---|---|
| **Team rule** (also "team context") | `reverie context add` or the UI "Add a rule" form | Markdown bullets in `<repo>/.reverie/context/*.md` (layer `repo`), or in folders named under `[sources]` org/team in `<repo>/.reverie/context.toml` or `~/.reverie/context.toml`. Parsed by `context.parse_document`. The database is not involved. | Yes, through git (the files) | Anyone, unless a CODEOWNERS entry covers the document; then owners write directly and everyone else writes a proposal | Yes: Rules tab, Overview, Proposals, Start, Search |
| **Personal rule, file form** | `reverie rules add --user "..."` | `~/.reverie/rules.md` (one bullet per rule) | No (your machine, every repo) | You | **Count only** (inside "Rules in force"); the text is never listed |
| **Repo rules file** | `reverie rules add "..."` with no flag (`level="user" if args.user else "repo"` in `cli._cmd_rules`) | `<repo>/.reverie/rules.md` | Yes, if committed to git; no owners, no proposals | Anyone with file access | **Listed on the Rules screen** under the heading "Shared rules file: no owners, no review" (one card per bullet, `context_page`), **counted** in "Rules in force", and shown in "What agents are told" under "This repo's rules (team)". |
| **Personal context document** | `reverie context add --layer personal` | `~/.reverie/context/*.md` (layer `personal`) | No | You | Yes: it is one of the layers in the Rules tab, labelled `personal` |
| **Remembered rule** (a memory item of kind `rule` or `preference`) | The extractor (`typed_extractor.py`) when you state a rule to an agent | SQLite table `items` in `~/.reverie/reverie.db` | No (your machine) | Created automatically | Yes, as a card in the Memory tab, counted in "Rules in force" |

Team rules use these terms (`context.py`):
- A **document** is one `.md` file. A bullet under a heading that matches `rules|always|never|must|non-negotiables|required` is a
  **rule** (`kind="rule"`, always delivered). Any other bullet is **guidance** (`kind="guidance"`, delivered as a one-line pointer).
- A **layer** is where a document comes from: `org`, `team`, `repo`, `personal` (`LAYERS`, widest first). A **scope** is the folder
  name for org and team layers (`scope_name`, last two path components).
- Optional header lines at the top of a document: `applies-to`, `applies-to-paths` (path globs that narrow it), `owner:` (display
  text, "whom to ask"), `inside:`. Optional bullet tags: `{id: name}`, `{resolves: key}`, `(locked)`.
- A rule's **id** is `sha1(layer, scope, file name, text)[:10]` (`context.item_id`). Edit the text or move the file and the id changes.

### Memory
**Memory** is what agents remembered from sessions, plus conclusions from an older store.
- Table `items` (`items.py`, `schema.sql`): one row per item, columns `id, repo_key, text, kind, anchor_json, source, author, level,
  grounding_commit, last_verified, status, evidence, supersedes, created_at, embedding, meta_json`.
  `kind` is one of `rule, decision, rejected_approach, work_state, preference, environment, external_context, open_question`.
  `source` is `user_said` (stored as said) or `agent_inferred` (must carry evidence). `status` is `active, stale, superseded,
  undone, candidate`. `repo_key` NULL means user-wide (delivered in every repo). Items are accepted automatically; there is no
  review gate (`typed_extractor.py` docstring), and `reverie undo` reverses one.
- Table `ecus` (legacy "conclusions", `deliver._legacy_conclusions`): active or challenged rows for this repo, shown as kind
  `decision` or `observation`.
- The UI shows `deliver.memory()` filtered by `deliver.is_live()`: active items for this repo plus user-wide ones, with
  near-duplicates collapsed (`collapse.collapse`), plus legacy conclusions. It never shows `superseded`, `undone` or `candidate`
  items.
- Memory lives only in `~/.reverie/reverie.db` on one machine. It is not in git. The Memory hint says so.

### Proposal
A proposed team rule waiting for an owner. A Markdown file in `<folder>/proposals/<slug>.md` next to the document it is for
(`context.add` when `owners_of(path)` is non-empty and `is_owner(path)` is false; or `context_propose.propose`). Fields in the
file: `document`, `kind`, `paths`, `proposed-by`, `date`, `why`, optional provenance (`from`, `first-stated`, `usage`,
`proposed-via`, `re-proposal-of`), then the bullet. The `<slug>` is `<first 40 chars of the bullet>-<6 hex of sha1>`.
`context.accept` appends the bullet to the document and deletes the proposal file. `context.reject` moves the file to
`proposals/rejected/<slug>.md` and appends `rejected-by`, `rejected-on`, `reason`. A rejected rule cannot be proposed again
through `context propose` unless `--again --why "what changed"`.

### Conflict
Two rules that cannot both be followed. Two ways one is created (`context.resolve`):
1. **Tag conflict:** two rules with the same `{id: name}` tag. It is a conflict only if they come from different scopes
   (same scope and same layer is "same", no conflict). Computed from the documents on every read.
2. **Judge conflict:** the model judge (`context_check.find_conflicts`) says a rule contradicts a rule of another scope. The pair
   is stored in `~/.reverie/context_state.json` under `"conflicts"` (`context.state_path`), **on one machine, not in the repo**.

Each conflict has a status (`context.Conflict`):
- `overridden`: a narrower scope wins over a wider one (repo over team over org; a path-scoped rule over the same unscoped one), or
  a recorded resolution settled it. The loser stays on disk, is not delivered.
- `locked`: the wider rule is marked `(locked)`, so it wins.
- `unresolved`: sibling scopes (two teams, two orgs) or two locks. **Neither rule applies**; agents are told to ask the user.
  Settled with `reverie context resolve <key> "<decision>"`, which writes a new rule tagged `{resolves: key}`.

### "Possibly outdated"
Two unrelated mechanisms, both displayed with the same words:
- **Team rule:** `context_check.check_stale` takes the backticked files and identifiers in a rule's text (`anchors_of`), looks at
  commits since the last check that touched them, asks the judge, and on verdict `CONTRADICTED` stores
  `{commit, message, reason, file}` in `context_state.json` under `"stale"`, keyed by rule id. The rule stops being delivered as
  "always apply" and is listed in a separate "Possibly outdated" group. Nothing in the code clears this flag except the rule id
  changing (edit the text) or the rule being deleted **(inferred: no function removes a key from `state["stale"]`; `context.resolve`
  only keeps keys whose id still exists)**.
- **Memory item:** `staleness.check` does the same for `items` (anchored files/symbols) and sets `meta.stale`. It also stores a
  *candidate* replacement item (`status='candidate'`, never delivered). `reverie stale accept <candidate>` makes the candidate
  active and supersedes the old one; `reverie stale keep <id>` clears the flag and drops the candidate.

### Owner
There is **no role list** (`context.py` docstring). An owner is whoever a CODEOWNERS entry names for the document.
- `owners_of(doc)`: finds the git root of the document's folder, reads the first non-empty of `.github/CODEOWNERS`, `CODEOWNERS`,
  `docs/CODEOWNERS`, and takes the **last matching line** (CODEOWNERS-style globs, `glob_match`). No CODEOWNERS, or no matching
  line, gives `[]`, meaning nobody owns it.
- `identity(folder)`: lower-cased `git config` values `user.email`, `user.name`, `github.user` (also as `@name`) and
  `reverie.handle`, read in the document's folder (so an org/team folder that is its own git repo uses its own identity).
- `is_owner(doc)`: true if `owners_of` is empty, or if any owner string equals any identity string. Exact string comparison.
  `@org/team` entries are never expanded **(inferred: nothing in `identity()` could match them)**.
- The `owner:` header line in a document and the optional `authority` file only change display text ("Ask ..."). Nothing branches
  on them.

### How they relate
```
repo .reverie/context/*.md  --- parse ---> team rules + guidance  --\
org/team folders (context.toml) ----------------------------------- +--> context.resolve() --> effective rules,
~/.reverie/context/*.md (personal layer) ------------------------- /     conflicts, hidden duplicates, stale
        ^                                                                       ^
        | accept (owner)                                                        | ~/.reverie/context_state.json
proposals/*.md <-- context.add (non-owner)                                      |   (judge conflicts, stale flags; local)
        |  reject (owner, reason)
        v
proposals/rejected/*.md

~/.reverie/rules.md (personal) and <repo>/.reverie/rules.md  ---> "Reverie rules" block (rules.py)
~/.reverie/reverie.db  items / ecus  --------------------------> memory (deliver.memory)

team rules + rules files + memory --> deliver.startup_text() --> what an agent is told at session start (screen "What agents are told")
```
Delivery order in `deliver.startup_text`: team context block first (`context.render`), then "Reverie rules" (the two rules.md files),
then user-wide remembered rules, then repo remembered rules, then "Possibly outdated" memory, then a one-line-per-item index of the
rest. A personal rule or remembered rule that repeats a team rule (same normalised text) is hidden at delivery time, never
deleted (`context.shared_rule_norms`, `Resolved.hidden`).

**Team rules vs personal rules vs memory, in one line each.**
- Team rules: shared files in git with an owner model, a proposal flow, conflicts and outdated flags; always delivered.
- Personal rules: your own Markdown bullets, no owners, no review, always delivered to your agents only (`~/.reverie/rules.md`).
  Careful: `reverie rules add` without `--user` is **not** personal; it writes `<repo>/.reverie/rules.md`.
- Memory: facts and decisions agents captured from your sessions; automatic, reversible, kept in a local database; delivered as an
  index plus on-demand lookups, with only rules/preferences delivered in full.

---

## 2. Roles

Authority is decided in one place: `context.is_owner()` (CODEOWNERS + git identity). The UI adds no checks of its own; it calls the
same functions and shows their refusals. Identity is read from `git config` in the document's folder; the UI reports it as
"You would act as **Name <email>** (git identity)" (`_who`).

| Person | Can do in the UI | Cannot do |
|---|---|---|
| **Owner** of a document (identity matches a CODEOWNERS entry covering it) | Everything a reader can. "Add a rule": written straight into the document (`AddResult.proposal` false). Accept or Reject a waiting proposal for that document. | Add locked, tagged, path-scoped rules, choose the document file, or settle a conflict (no UI for these). |
| **Non-owner teammate** (CODEOWNERS covers the document, identity does not match) | Read every screen. "Add a rule": becomes a proposal file; the page says "Written as a proposal (...). The owners (...) review it; it is not in force yet." The Accept and Reject buttons are shown, marked "Not an owner: the CLI would refuse", and pressing them is refused: "Refused: only the owners of rules.md can accept: <owners>". | Put a rule in force. Settle proposals. |
| **Working alone, no CODEOWNERS** | Everything an owner can. `owners_of` is `[]`, so `is_owner` is true for anyone, "Add a rule" always writes directly, and a proposal can only exist if someone hand-made the file or another tool made one. The Proposals screen shows the identity line "Owners of rules.md: nobody (anyone may edit)". | Nothing is restricted. |
| **Anyone with no git identity set**, when CODEOWNERS exists | Same as a non-owner (empty identity set matches nothing). The screens show "no git identity is set here". | Same as a non-owner. |

Notes:
- Ownership is per document, not per repo: CODEOWNERS can give `rules.md` and `security.md` different owners
  (last matching line wins). The same person can be an owner for one proposal and not for another on the same screen.
- "Owner" is decided from the viewer's machine config. The server has no login, no session and no user accounts. Whoever can reach
  `127.0.0.1:<port>` and holds the per-process token can write as the git identity of that machine. The token is in the HTML of
  each form (`State.token`).
- Org and team documents may be in other git repos with their own CODEOWNERS and identity; the Add form lists their identities
  ("Shared folders use their own git identity: ...").
- The personal layer (`~/.reverie/context`) is not in a git repo, so `owners_of` gives `[]`; the UI does not offer it as a target
  anyway (`_targets` lists only `repo` and org/team folders).
- Reading is never restricted. There is no per-project permission.

---

## 3. Every screen

Layout shared by all pages (`_doc`, `frame`, `shell`):
- Left sidebar: logo "Reverie"; link "All projects" (`/`); link "Add a project" (`/new`); section "Projects" with one link per
  project (`/project?p=<id>`), first 40 only (`plist[:40]`); footer text.
- Top bar: breadcrumbs; a search form (GET `/search?q=...&p=<id>`, placeholder "Search rules and memory", max 120 chars).
- Project pages add: a header (project name tile, name, absolute path, button "Connect an agent" → `/connect`, primary button
  "Add a rule" → `/add`) and a tab bar: Overview (`/project`), Rules (`/context`), Memory (`/memory`), What agents are told (`/start`),
  Proposals (`/proposals`), Connect (`/connect`). Tab badges: Overview = count of things needing action, Proposals = waiting
  proposals (both amber when non-zero), Rules = the same number as "Rules in force" (`rules_in_force`, one function), Memory = count of memory items. Connect has none.
- Footer note on every page: "Everything here is also a command; the page calls the same functions and refuses what the CLI refuses."
- The `p` query parameter selects the project. **An unknown or missing `p` silently falls back to the first project (the one the
  server was started in)** (`_target`). Reads never refuse on a bad `p`.

Shared data, `collect(root)` (`ui.py`), run once per page, built from:
`context.resolve(root)` (team rules, conflicts, stale, hidden), `memory_items(root)` (`deliver.memory` + `deliver.is_live`),
`rules.load(root, hide_shared=True)["user"]` (personal bullets, count only), `rules.load(root)["repo"]` (listed on the Rules screen),
`context.proposals(root)`, `context.rejected(root)`, and `deliver.startup_text(root, log=False, mem=<already loaded memory>)` as `d["start"]`. Every call is wrapped in `_safe()`, which returns an empty value on any
exception, so **a broken file shows as "nothing", not as an error** (see section 4).

Derived numbers used on several screens:
- *Rules in force* (`rules_in_force(d)` returns `(given, cut)`; used by the Rules tab badge, Overview, Projects and the start screen, so they
  cannot differ) = the rules agents actually receive in the start block. Candidates: team rules (effective, `kind=="rule"`, not stale),
  personal `rules.md` bullets, `<repo>/.reverie/rules.md` bullets, and memory items that `deliver.startup_text` hands over in full
  (kind `rule`, or a user-wide `preference`; not stale-flagged; not a repeat of a team rule, which the start block hides). A candidate
  counts as *given* only if its text (or, for memory, its `[id8]`) appears in the actual start block, so the line caps (15 user-wide, 30 repo
  rules), the character budgets and the final truncation are all applied. *Cut* = candidates not in the block. When *cut* is above 0 the
  UI says so: stat "Cut by the start limit" on Overview, "N cut by the start limit" on the Projects card, and
  "N rules in force · N cut by the start limit: ..." under the start text.
- *Needs attention* (`_attention`) = unresolved conflicts + stale team items + stale memory items + waiting proposals.
- *Conflicts to settle* = conflicts with status `unresolved` only.
- *Possibly outdated* = stale team items (rules and guidance) + stale memory items.

### 3.1 `GET /` and `/repos`: Projects

**Job:** see which projects on this machine have something waiting for a person, and open one.

Data (function `projects_page`):
- Per project (every project, including disabled): name (last path component), absolute path, source `projects()`.
- Disabled projects: card with "Reverie is turned off for this repo" (`deliver.is_disabled`).
- Enabled: chips "N conflict(s)" (red, `unresolved`), "N outdated" (amber, stale team + stale memory), "N proposal(s)" (blue);
  line "N rules in force · [N cut by the start limit ·] N remembered" (`collect`).
- Lane assignment: "Needs attention" if `_attention > 0`, else "All clear"; "Turned off" lane only if any disabled project exists.
  Lane headings carry a count.

Actions:
- Each card is a link to `/project?p=<id>`.
- Header button "Add a project" → `/new` (link). Sidebar "Add a project" → `/new`.

Success/failure: GET only. A broken project renders with zeroes (see `_safe`). Fails as a whole only on an unexpected exception
inside `render()`, which returns status 500 with the "Something went wrong" page.

### 3.2 `GET /new`: Add a project

**Job:** learn how to make a repo appear in the sidebar.

Data: static. Three numbered steps, each with a command: `cd path/to/your/repo`, `reverie enable`, `reverie install`.
Actions: none (no button creates anything; text only). Source: `new_page`.

### 3.3 `GET /project`: Overview

**Job:** decide what to deal with first in this project.

Data (function `overview`):
- Stat line: Rules in force, [Cut by the start limit, only when above 0,] Conflicts to settle, Possibly outdated, Proposals waiting (numbers above).
- If any of the three lanes is non-empty, heading "Needs your action" and three lanes (Conflicts, Possibly outdated, Proposals);
  otherwise the empty state "Nothing needs your action right now."
- **Conflict card** (unresolved only): chip "Conflict"; heading "Two rules disagree"; for each of the two rules its label
  (e.g. `team acme/payments`) and text clipped to 160 chars; "Agents are told to ask before acting on either."; "Ask <owners>"
  from `context.owner_text` (the document's `owner:` line, else CODEOWNERS entries with `authority` labels).
- **Possibly outdated, team rule:** chip; the rule text (200 chars); label; the judge's `reason` (200 chars) from
  `res.stale[id]`. The commit sha and file are not shown; the file path appears only inside the instruction.
- **Possibly outdated, memory item:** chip; item text; "Remembered from an earlier session."
- **Proposal card:** chip "Proposal"; heading "A proposal is waiting"; rule text (200); "Because: <why>" if set; proposer
  (`proposed-by`) with avatar.

Actions (all are expandable text, **no buttons**):
- Conflict: details "How to settle it" with `reverie context resolve <key> "your decision"`.
- Stale team rule: details "What to do" with `Check the code, then edit or remove the rule in <absolute file path>`.
- Stale memory item: details "What to do" with `reverie stale list`.
- Proposal: details "An owner decides" with `reverie context accept <slug>` and `reverie context reject <slug> --why "…"`.
  (The buttons for the same action are on `/proposals`; the Overview does not link there.)

Success/failure: none (read only). If the repo is disabled: the "Reverie is turned off" screen (3.11).

### 3.4 `GET /context`: Rules

**Job:** see every team rule that applies here, where it comes from, and which are in conflict, outdated or hidden.

Data (function `context_page`; source `context.resolve`):
- If no team document applies at all: empty state (see section 4). Personal `rules.md` bullets and memory rules do not count as team context, so the empty state shows for them; but if
  `<repo>/.reverie/rules.md` has bullets, the "Shared rules file: no owners, no review" lane (with a hint) replaces the empty state, and with team
  items it follows the team table.
- A "Conflicts" lane, **all** conflicts (`overridden`, `locked`, `unresolved`), one card each: chip with the status (red if
  `unresolved`, green otherwise), the conflict key (e.g. `c-1a2b3c4d`, or the `{id:}` tag), each party as `label: "text"` (120
  chars), and the `reason` (the judge's one-line reason, or "sibling scopes have no order", "resolved by <owner>", etc.).
- "Hint shown to agents: If two rules above seem to contradict, ask before acting." when more than one org/team scope applies
  (`context.contradiction_hint`).
- Table, one row per item in `res.items` (rules **and guidance**, including overridden, hidden and stale ones, from all four
  layers), columns: *Applies to* (the label: `org <scope>`, `team <scope>`, `repo`, `personal`), *Kind* (`rule`/`guidance`),
  *Rule* (text clipped to 300 chars plus the document's **absolute file path**), *Marks*, *Id* (10-hex). Marks (chips):
  `locked`, `conflict` (it lost or is half of an unresolved pair), `possibly outdated`,
  "also a shared rule: hidden here" (a personal copy of a team rule), "only under <globs>".
- Not shown: the winner of an `overridden` conflict is not marked; owner; section heading; the date; who wrote it;
  `applies-to` repo list; stale reason or commit.

Actions: none on this screen. No row links anywhere.

### 3.5 `GET /memory[?page=N]`: Memory

**Job:** see what Reverie has remembered for this project.

Data (function `memory_page`, `_mcard`; source `deliver.memory` + `is_live`):
- Hint: "N item(s) remembered on this machine for this repo. Open one with `reverie show <id>`."
- Lanes by kind in the order rule, preference, decision, then any other kind alphabetically. Lane titles (`KIND_NAMES`, `_kind_name`): "Rules",
  "Preferences", "Decisions", "Rejected approaches", "Environment", "Observations", "Open questions", "Work in progress", "External context";
  an unknown kind falls back to `kind.replace("_", " ").capitalize()`. There is no "Findings" lane.
- Card: text clipped to 300 chars; chips "possibly outdated" and "challenged" when set (`meta.stale`, `meta.challenged`); a source chip
  (the raw `source` value: `user_said`, `agent_inferred`; or `origin`; else "unknown"); usage tally as `used=3, contradicted=1`
  (`meta.usage`); the first 8 characters of the id.
- Paging: 100 items per page (`PAGE_SIZE`), "Page X of Y" with Previous/Next links. A bad `page` is clamped or ignored.
- Not shown: anchor files/symbols, grounding commit, evidence, author, date, level (repo vs user-wide), whether user-wide,
  how often seen, status history, replaced items.

Actions: Previous / Next links only. Each card has no link; opening one is the command `reverie show <id>` in the hint.

### 3.6 `GET /start`: What agents are told

**Job:** check exactly what an agent would be handed if a session started in this project now.

Data (`start`): `deliver.startup_text(root, log=False)` in a scrollable `<pre>`, then "N characters." Intro line: "This is exactly what
an agent is handed when a session starts in this repo. Viewing it here is not a delivery and is not counted as one."
`log=False` keeps the view out of `deliver.usage.log_delivery` (`deliveries.jsonl`). The text is capped by
`deliver.startup_cap()` (default 8000 chars, env `REVERIE_STARTUP_CHARS` or `deliver.startup_chars`).
Actions: none. Not shown: which part came from where (the text carries its own headings), what was cut by the cap beyond the
text's own "(+N more ...)" lines, anything about a past session.

### 3.7 `GET /proposals`: Proposals (the one screen with the decide buttons)

**Job:** accept or turn down a rule someone proposed (owners), or read why one was turned down (everyone).

Data (`proposals_page`; sources `context.proposals`, `context.rejected`, `context.owners_of`, `context.is_owner`, `_who`):
- One-shot flash banner (green/red) if the URL carries `done=<id>` (see "Flash" below).
- Hint: "Rules people suggested for the team. Only an owner of the document can accept or turn one down; turned-down ones keep their
  reason, so the proposer can see it."
- Lane "Waiting": per proposal: the rule text (full, not clipped), "Why: <why>", an extra line `from: ... · first-stated: ... ·
  usage: ...` when the proposal came from `context propose`, proposer with avatar, date, "for <document>.md", then the decision block:
  "You would act as **<name <email>>** (git identity). Owners of <doc>: <owners or 'nobody (anyone may edit)'>." plus chip
  "You can decide this" (green) or "Not an owner: the CLI would refuse" (red).
- Lane "Turned down": rule text, the reason, who (`rejected-by`), when (`rejected-on`). No time limit, no paging.

Actions:
- **Accept** button: POST `/do/accept` fields `token`, `p`, `slug` → `do_write` → `context.accept(target, slug)`.
  Success flash: "Accepted into <doc>.md. The proposal is gone and the rule is in force." followed by `CHECK_LATER`: "It has not been checked for conflicts yet: that happens at the next session start, or now with `reverie context check`." (the same sentence follows a direct add; it is not added to the proposal-written message or to refusals). (Redirects to `/proposals`.)
- **Reject** form: text input "Reason for turning it down (the proposer will read it)" (required, max 300 chars) + button "Reject":
  POST `/do/reject` fields `token`, `p`, `slug`, `why` → `context.reject(target, slug, why)`.
  Success flash: "Rejected. The reason is kept in proposals/rejected/<file>.md".
- Failure flashes (all red, nothing changes): "Refused: that project is not known to Reverie." (redirects to `/`),
  "Refused: Reverie is turned off for this project. Turn it on first.", "Refused: that proposal is not waiting any more." (the slug
  must match exactly; unlike the CLI, no prefix and no empty slug), "Refused: only the owners of <doc> can accept: <owners>" /
  "...can reject: ...", "Refused: say why (--why): the proposer will read it", "Refused: the reason looks like it contains a secret,
  so nothing was changed. Remove the value and try again.", "Refused: no single proposal matches '<slug>'", and the catch-all
  "That did not work, and nothing was changed. The CLI still does."

### 3.8 `GET /add`: Add a rule

**Job:** put a new rule in front of the team, in the way the CLI would.

Data (`add_page`): "You would act as **<identity>** (this repo's git identity). If a CODEOWNERS entry covers the document and you
are not an owner, this becomes a proposal the owners review." plus, if org/team folders are configured, "Shared folders use their own
git identity: <label: identity; ...>". Flash banner if present.

Actions:
- **Add** button, form POST `/do/add` → `context.add(target, text, layer=..., scope=..., rule=(kind != "guidance"), why=...)`:
  fields `text` (textarea, required, max 1000), `kind` (select: "Rule (always delivered)" / "Guidance (delivered when relevant)"),
  `target` (select: "This project (<name>/.reverie/context)", then `<layer>: <scope>` for each org/team folder in `context.toml`),
  `why` (optional text, max 300).
  - Always writes document `rules.md` (rule) or `guidance.md` (guidance); no document name, no paths, no lock, no tag, no resolves.
  - Success (direct): "Added to <doc>.md. It applies from the next session."
  - Success (proposal): "Written as a proposal (<file>.md). The owners (<owners>) review it; it is not in force yet."
  - Failure: "Refused: this looks like it contains a secret, so nothing was saved. Remove the value and try again.";
    "Refused: that is not a place this project can add to."; "Refused: <ValueError text>" (for example "text is empty", "no team
    source configured: add [sources] team = [\"<folder>\"] to .reverie/context.toml"); the catch-all above. On refusal the text and why
    are put back in the form (unless it was a secret).
- Two command cards (no buttons): "A rule only you follow" with `reverie rules add --user "Money is integer cents"`;
  "Turn a rule you already use into a team proposal" with `reverie context propose "Money is integer cents"`.

### 3.9 `GET /connect`: Connect an agent

**Job:** learn the steps to attach an agent to this project.
Data: chip "Reverie is on for this repo" / "Reverie is turned off for this repo" (a disabled project's Connect tab shows the "turned off" chip and the same steps), then four numbered steps
with commands: `reverie install`; `cd <project path> && reverie enable`; `reverie status`; `reverie context`.
Actions: none (text only).

### 3.10 `GET /search?q=...`: Search

**Job:** find a rule or memory item by words.
Data (`search_page`): case-insensitive substring match (not ranked, not fuzzy) of the query (first 120 chars) in `res.items[*].text`
(team rules and guidance) and in `mem[*].text`; 50 hits max per lane. Lanes "Team rules" (label chip, text, file path) and
"Memory" (the same card as the Memory screen). Hint: "Results for “<q>” in <project>." Searches only the current project.
Not searched: personal `rules.md`, repo `rules.md`, proposals, rejected proposals, why-text, ids. Actions: none.

### 3.11 Disabled project (any project page)
`/project`, `/context`, `/memory`, `/start`, `/proposals`, `/add`, `/search` for a disabled project render
`_disabled()`: icon, "Reverie is turned off for this repo", "Nothing is delivered to agents and nothing is shown here. Turn it back on
with `reverie enable`." Page title "Turned off". `/connect` is the exception: it renders the Connect page with the steps (including `reverie enable`). Writes to a disabled project are refused as above.

### 3.12 Error screens
- Unknown path: status 404, page "Not found", subtitle `No such page. <a href="/">Back to all projects</a>`.
- Any unexpected exception while building a page: status 500, page "Something went wrong", "This page could not be built. The CLI still works." (sidebar empty).
- Bad Host: 403 `forbidden`. POST to a non-write path: 405 "only GET and HEAD are served here". POST without same-origin: 403
  "forbidden: not a same-origin request"; no/invalid Content-Length: 411/413; wrong content type: 415 "unsupported content type";
  wrong token: 403 "forbidden: missing or wrong token". These are plain-text responses, not styled pages.

### Flash messages (post/redirect/get)
Every write ends in `303 See Other` to `/add?p=<id>&done=<flash id>` or `/proposals?...`. `State.put` stores the message in
process memory (max 50, oldest dropped); `State.take` shows it once and deletes it. A reload, a copied URL, or a restart of
`reverie ui` shows no banner. Writes are serialised by one lock.

---

## 4. States

Legend: **yes** exists in code today; **no** missing. "Loading" is **no** everywhere: pages are rendered synchronously on the server,
there is no script, no spinner, no skeleton. A slow page is a blank browser tab until the response arrives.

| Screen | Empty | Typical | Heavy (40 rules, 15 proposals, 500 memory) | Errors | Permission refusal |
|---|---|---|---|---|---|
| **Projects `/`** | Yes: with no project the current repo is still listed; lane text "Nothing waiting on anyone." / "No project is fully clear yet." | Yes | Board is unpaginated: every project is a card and **each card runs a full `collect()`** (git calls, file reads, SQLite) on every load; sidebar caps at 40 projects but the board does not **(inferred cost)** | 500 page only; a broken project silently shows zeroes | n/a |
| **Overview** | Yes: "Nothing needs your action right now." + hint | Yes: stat line + up to three lanes | No limit, no grouping, no sorting rule beyond file order: 15 proposal cards = 15 tall cards in a lane; no "show more" | 500 page; broken files read as empty (`_safe`) | n/a (no buttons) |
| **Rules** | Yes: "No team context applies here" | Yes: table | One unpaginated, unsorted-by-meaning table (sorted by layer then file order, `load_items`); 40 rows works; 400 would be a very long page. No filter, no column sort, no grouping by document | 500 page; an unreadable/oversized (>200 KB) document is skipped silently (`MAX_DOC_BYTES`, `_read`) | n/a |
| **Memory** | Yes: "Nothing remembered for this repo yet" | Yes | Yes: 100 per page, tested with 500 (`test_memory_500_items_is_paged_and_bounded`). Paging is by position, not by kind, so a kind's lane is split across pages | Missing brain = empty; 500 page | n/a |
| **What agents are told** | Yes: "Nothing." / "No rules or memory apply here yet, so an agent starts with a clean slate." | Yes | The text is capped (default 8000 chars) by the delivery code; the page shows the cut text, with the cap notices inside it | A failure inside `startup_text` returns "" and the page shows the empty state **(inferred: indistinguishable from "nothing applies")** | n/a |
| **Proposals** | Yes: "No proposals" + how to make one | Yes | Unpaginated: every waiting proposal and every rejected one ever kept, each with two forms. 15 waiting = 15 cards with 15 reject text boxes | Flash banners (red) for every refusal in 3.7; 500 page | Yes: red chip "Not an owner: the CLI would refuse" before the click; refusal flash after |
| **Add** | n/a (always a form) | Yes | n/a | Flash banners (red) in 3.8; invalid input is refused server-side, no client-side validation except `required`/`maxlength` | Proposal outcome (not a refusal): "Written as a proposal..." |
| **Connect / New** | n/a (static) | Yes | n/a | 500 page | n/a |
| **Search** | Yes: "Type something to search"; per lane "No match" | Yes | 50 hits per lane cap, no "more" indicator | 500 page | n/a |
| **Disabled project** | The screen itself (3.11) | n/a | n/a | n/a | Writes refused with a flash |

Other states and gaps:
- **Pending/in-progress states: no.** There is nothing for "the judge is still checking" or "last checked at ...". The UI never runs
  the judge and shows no date for the last conflict or staleness check. A just-added rule that conflicts shows no conflict until a
  later session-start worker or `reverie merge` runs the judge; the UI add does **not** run it (the CLI does,
  `cli._context_detect`) (`test_the_ui_never_runs_the_judge`).
- **Success confirmation of a settled conflict, accepted proposal history: no.** Accepting deletes the proposal file; there is no
  "accepted" list. The UI keeps no history except the rejected folder.
- **Stale or expired page: no.** A page left open for a long time keeps old data and an old form token; a server restart invalidates
  every open form's token and the next press shows "forbidden: missing or wrong token" as plain text (`test_a_token_from_another_process_is_refused`).
- **Unreadable data vs empty data: not distinguished.** `_safe` swallows every error.
- **A teammate changing files while the page is open: no live update.** Reload required.
- **Mobile:** a CSS breakpoint at 900px turns the sidebar into a top strip and hides search and the footer text. No other mobile handling.
- **Dark mode:** yes (`prefers-color-scheme`). **Reduced motion:** yes.
- **Keyboard/focus:** `:focus-visible` outline exists. Form fields have `label` elements. No skip link. Cards are links or articles; the
  expandable "How to settle it" uses `<details>`.

---

## 5. Not exposed

Everything below exists in the CLI or library and has no screen or button. Command, one line each.

**Team layer, writing and deciding**
- Add a **locked** rule: `reverie context add "..." --locked`.
- Add a **tagged** rule (so another scope can answer it): `reverie context add "..." --tag name`.
- Add a **path-scoped** rule (applies only under globs): `reverie context add "..." --paths "services/billing/**"`.
- Choose the **document file**: `reverie context add "..." --doc security`.
- Write to the **personal** layer: `reverie context add "..." --layer personal`.
- **Settle a conflict**: `reverie context resolve <key> "decision"` (the Overview only shows this command).
- **Propose a personal rule or a memory item to the team**: `reverie context propose "<text, personal rule id, or memory id>" [--layer team|org|repo] [--why ...]`.
- **Re-propose a rejected rule**: `reverie context propose "..." --again --why "what changed"`.
- **Accept the only waiting proposal without naming it**: `reverie context accept` (the UI requires the exact name; deliberate).
- **Run the conflict and outdated checks now**: `reverie context check [--since REV]` (the quiet form is `--if-changed`).
- **Clear or keep a possibly-outdated team rule**: no command **(inferred)**; the only route is editing/removing the rule.

**Rule history and replaced rules**
- Rule **history** (who changed a team rule, when, why): no Reverie command; the documents are in git, so `git log -p .reverie/context/rules.md`.
  There is no "accepted by/on" stamp: `context.accept` deletes the proposal and stores nothing about the decision.
- **Replaced/superseded** team rule: not a concept for team rules (a replacement is an edit). For memory: items with `status='superseded'` or
  `'undone'` are never shown. Bring back an undone item: `reverie undo --restore <id>`; undo an item (and re-activate what it replaced):
  `reverie undo <id>`. The `supersedes` and `meta.superseded_ids` fields are not printed by any command **(inferred: `deliver.show` does not print them)**.
- **Memory the agent captured, newest first**: `reverie digest [--all] [--no-mark]`.

**Evidence, confidence, grounding, decay**
- **Evidence, author, source, level, files, symbols, freshness** for one memory item: `reverie show <id>` (the Memory card shows none of these).
- Full detail for one **team rule** (document, section, owner, conflicts, stale reason with commit): `reverie show <rule id>`
  (`context.show`). The Rules table shows the id but does not link to it.
- **Usage per item** (used / ignored / irrelevant / contradicted): the Memory card shows the counts; `reverie usage` lists them sorted.
  Team rules are not tracked by usage at all **(inferred: `deliver.startup_text` logs only memory item ids)**.
- **Confidence** of conclusions and **decay**: the legacy `ecus` brain computes confidence and decays it lazily at retrieval, and the
  Maintainer changes statuses (`reverie/maintainer.py`, run inside the MCP server). No CLI command prints a conclusion's confidence
  except through `reverie review` (terminal UI) and the MCP tools. The Memory screen shows legacy conclusions without confidence or status.
- **Grounding to files and commits**: `grounding_commit`, `last_verified`, and anchors are in the `items` row and `reverie show <id>`.
  Not on any screen.
- **Check memory against the code**: `reverie stale check [--since REV] [--tests]`; list proposed replacements: `reverie stale list`;
  apply: `reverie stale accept <candidate id>`; keep the old one: `reverie stale keep <memory id>`.
- **Merge near-duplicate memory**: `reverie merge`.
- **Make a memory item apply in every repo, or take it back**: `reverie promote <id>` / `reverie promote <id> --demote`.

**What agents received**
- **What an agent actually received in a past session**: not stored as text. `deliveries.jsonl` in the spool
  (`usage.log_delivery`) records timestamp, channel (`start`, `edit`, `query`) and **item ids only**, for memory items; no command prints it
  and team rules are not logged. The closest are: the **prompts you sent** (`reverie resume [--all] [--prompt N]`), the aggregated
  **usage tally** (`reverie usage`), and what a session starting *now* would get (`reverie hook session-start`, and the UI's
  "What agents are told").
- Edit-time deliveries (path-scoped team rules and anchored memory shown when an agent edits a file): `deliver.point_of_action` /
  `context.point_text`; no screen previews what a given file edit would trigger.

**Personal rules, project controls**
- List your personal and repo `rules.md` bullets: `reverie rules list`. Add: `reverie rules add [--user] "..."` (the UI lists the repo file's bullets on the Rules screen; personal bullets are only counted).
- Turn a repo on/off: `reverie enable` / `reverie disable`. State of all repos: `reverie repos`. Sessions and brain status: `reverie status [--all]`.
- Install into agents: `reverie install` (the Connect/New screens print it only).
- Review the older conclusion candidates: `reverie review` (terminal UI); also via MCP `review_list`/`review_decide`.
- Export/import/reset the brain: `reverie export`, `reverie import`, `reverie reset`. Repair: `reverie repair`.
- Sibling and org scoping inputs: `.reverie/context.toml` `[sources] org/team`, the optional `authority` file: edited by hand only.

---

## 6. Copy

Every user-facing string, by screen, taken from `ui.py`. `<x>` marks data. Strings that are the same across screens are listed once.

### Global (every page)
- Title suffix `· Reverie`. Brand: "Reverie".
- Sidebar: "All projects", "Add a project", section "Projects".
- Footer (sidebar): "Local only. It writes only when you press a button, with the CLI's own rules."
- Search: placeholder and aria-label "Search rules and memory".
- Note (bottom of every page): "Everything here is also a command; the page calls the same functions and refuses what the CLI refuses."
- Tabs: "Overview", "Rules", "Memory", "What agents are told", "Proposals", "Connect".
- Header buttons: "Connect an agent", "Add a rule". Breadcrumb root: "Projects".
- Status words: "Conflict", "Possibly outdated", "Proposal" (chips); "conflict(s)", "outdated", "proposal(s)" (chip counts).

### Projects `/`
- Title "Projects". Subtitle "Every repo Reverie knows on this machine, sorted by what needs a person." Button "Add a project".
- Lanes: "Needs attention", "All clear", "Turned off". Empty: "Nothing waiting on anyone.", "No project is fully clear yet."
- Card: "Reverie is turned off for this repo"; "<n> rules in force", "<n> cut by the start limit" (only when above 0), "<n> remembered".

### Add a project `/new`
- Title "Add a project". Subtitle "Projects are repos. Reverie finds them once you use it in them."
- Steps: "Open a terminal in the project" / "Any git repo works." / `cd path/to/your/repo`;
  "Turn Reverie on there" / "It then appears in the sidebar the next time this page is opened." / `reverie enable`;
  "Connect an agent" / "One time per machine." / `reverie install`.

### Overview
- Stat labels: "Rules in force", "Conflicts to settle", "Possibly outdated", "Proposals waiting". Heading "Needs your action".
- Lanes: "Conflicts" (empty "No conflicts"), "Possibly outdated" (empty "Nothing outdated"), "Proposals" (empty "No proposals").
- Conflict: "Conflict"; "Two rules disagree"; "<label>: “<text>”"; "Agents are told to ask before acting on either."; "Ask <owners>"; details "How to settle it"; `reverie context resolve <key> "your decision"`.
- Team outdated: "Possibly outdated"; “<text>”; "<label>. A later change to the code may contradict it: <reason>"; details "What to do"; "Check the code, then edit or remove the rule in <file>".
- Memory outdated: "Remembered from an earlier session."; details "What to do"; `reverie stale list`.
- Proposal: "Proposal"; "A proposal is waiting"; "Because: <why>"; details "An owner decides"; `reverie context accept <slug>`; `reverie context reject <slug> --why "…"`.
- Empty: "Nothing needs your action right now." / "Conflicts, outdated rules and waiting proposals will show up here first."

### Rules
- Empty: "No team context applies here" / "Put markdown in `.reverie/context/` (see `reverie context`) to share rules with a team."
- Lane "Conflicts"; chips "unresolved" / "overridden" / "locked"; "Hint shown to agents: If two rules above seem to contradict, ask before acting."
- Table headers: "Applies to", "Kind", "Rule", "Marks", "Id". Marks: "locked", "conflict", "possibly outdated", "also a shared rule: hidden here", "only under <globs>".

### Memory
- Empty: "Nothing remembered for this repo yet" / "Decisions and findings from agent sessions appear here once Reverie has captured some."
- Hint: "<n> item(s) remembered on this machine for this repo. Open one with `reverie show <id>`."
- Lanes: "Rules", "Preferences", "Decisions", "Rejected approaches", "Environment", "Observations", "Open questions", "Work in progress", "External context". Chips: "possibly outdated", "challenged", source value or "unknown". Pager: "Page <x> of <y>", "Previous", "Next".

### What agents are told
- Intro: "This is exactly what an agent is handed when a session starts in this repo. Viewing it here is not a delivery and is not counted as one."
- Footer "<n> characters." Empty: "Nothing." / "No rules or memory apply here yet, so an agent starts with a clean slate."
- The block itself carries delivery-side copy, for example: "Team context rules (the narrower scope answers the wider unless locked; always apply):",
  "Rules for specific paths (apply ONLY to files under the path):", "Conflicts between scopes (never silently dropped):",
  "- UNRESOLVED <key>: ... Neither applies yet: ask the user which one to follow before acting on either ...",
  "If two rules above seem to contradict, ask before acting.", "Possibly outdated team context (a later commit may contradict it; verify):",
  "Team guidance (open one with `reverie show <id>`):", "Reverie rules (the user's own words; always apply):", "Your personal rules",
  "This repo's rules (team)", "Your preferences and rules across all repos:", "Rules you stated in earlier sessions:",
  "Possibly outdated (a later commit may contradict these; verify against the code before relying on them):", "Reverie memory index. These are
  past decisions and findings, not facts: verify against the code. Open one with `reverie show <id>`.", "[MAY BE STALE]",
  "[agents contradicted this repeatedly: confirm or fix it]", "(+N more ...)", "(…truncated; `reverie rules list` and `reverie digest` show the rest)".

### Proposals
- Empty: "No proposals" / "Anyone can suggest a rule with `reverie context add`, or turn one of their own into a proposal with `reverie context propose`."
- Hint: "Rules people suggested for the team. Only an owner of the document can accept or turn one down; turned-down ones keep their reason, so the proposer can see it."
- Lanes: "Waiting" (empty "Nothing waiting"), "Turned down" (empty "Nothing turned down").
- Card: "Why: <why>"; "for <document>"; "You would act as <identity> (git identity). Owners of <doc>: <owners | nobody (anyone may edit)>."
- Chips: "You can decide this"; "Not an owner: the CLI would refuse". Buttons: "Accept", "Reject". Label: "Reason for turning it down (the proposer will read it)".
- Flashes: see 3.7.

### Add a rule
- Title "Add a rule". Card "A rule or guidance for the team". Labels: "The rule", "Kind", "Where", "Why (optional)". Options: "Rule (always delivered)",
  "Guidance (delivered when relevant)", "This project (<name>/.reverie/context)", "<layer>: <scope>". Button "Add".
- "You would act as <identity> (this repo's git identity). If a CODEOWNERS entry covers the document and you are not an owner, this becomes a proposal the owners review."
- "Shared folders use their own git identity: <...>".
- Hint: "The same as `reverie context add`. A rule that looks like a secret is refused and not kept."
- Card "A rule only you follow": "Stays on your machine and is delivered to your agents. Still a command." `reverie rules add --user "Money is integer cents"`.
- Card "Turn a rule you already use into a team proposal": "Takes a rule from your own rules or memory and proposes it to the team. Still a command." `reverie context propose "Money is integer cents"`.
- Flashes: see 3.8.

### Connect
- Title "Connect an agent". Chips "Reverie is on for this repo" / "Reverie is turned off for this repo".
- Steps: "Set up your agent once per machine" / "This adds Reverie to the agents you use (Claude Code, Codex and others it finds)." / `reverie install`;
  "Turn it on for this project" / "Reverie is on by default. Use this to turn it back on after `reverie disable`." / `cd <path> && reverie enable`;
  "Start a session and check" / "Open your agent in the project. What it receives is on the “What agents are told” tab." / `reverie status`;
  "Share rules with your team" / "Point the repo at a shared folder of markdown rules in `.reverie/context.toml`; everyone who opens the project gets them." / `reverie context`.

### Search
- Empty query: "Type something to search" / "Searches the team rules and the remembered items of this project."
- "Results for “<q>” in <project>."; lanes "Team rules", "Memory"; "No match".

### Turned off / errors
- "Reverie is turned off for this repo" / "Nothing is delivered to agents and nothing is shown here. Turn it back on with `reverie enable`."
- "Not found" / "No such page. Back to all projects". "Something went wrong" / "This page could not be built. The CLI still works."
- Plain-text server replies: "forbidden", "forbidden: not a same-origin request", "forbidden: missing or wrong token", "Content-Length required", "too large",
  "unsupported content type", "only GET and HEAD are served here".
- CLI help text for `reverie ui`: "local web page: what agents are told, team context, proposals, memory; it writes only when you press add, accept or reject (stops on Ctrl-C)" (`cli.py`).

---

## 7. Lifecycle of a team rule

"Screen" = where the UI shows that state. The UI never triggers a state change except steps 1 and 3.

1. **Written, direct.** *Trigger:* `reverie context add` (CLI), or the UI Add form, by an owner or in a document nobody owns. The bullet is appended to `<folder>/rules.md`
   (`context._append`). In force from the next session (rules are read from files each time). *Screen:* Rules (after reload), Start, Overview stat line.
   The CLI then runs the judge (`_context_detect`); the UI does not.
2. **Proposed.** *Trigger:* `context.add` by a non-owner, or `reverie context propose`. A file in `proposals/`. Not delivered to agents (proposals are not parsed as context).
   *Screen:* Overview "Proposals" lane, Proposals "Waiting", Projects board chip, tab badges, flash after a UI add.
3. **Accepted.** *Trigger:* an owner runs `reverie context accept <slug>` or presses "Accept". The bullet moves into the document, the proposal file is deleted, the CLI then runs the judge.
   *Screen:* Rules (as a normal rule), flash "Accepted into <doc>.md...". The proposal vanishes from Proposals; nothing records the acceptance except git history.
   **Turned down instead:** `reverie context reject <slug> --why ...` or "Reject"; the proposal moves to `proposals/rejected/` with the reason. *Screen:* Proposals "Turned down".
   A later `context propose` of the same text is refused unless `--again --why`.
4. **Challenged.** The word has two meanings in the code. For **team rules** there is no "challenged" state. For **memory** items,
   `usage.judge_exchange` labels deliveries after each captured exchange; when `CONTRADICTED` ≥ 2 and more than `USED`, `meta.challenged` is set and delivery appends
   "[agents contradicted this repeatedly: confirm or fix it]". *Screen:* Memory card chip "challenged" (and the Start text). Not applicable to team rules.
   Closest team-rule equivalent: **in conflict** (step 4b).
   4b. **In conflict.** *Trigger:* a tag shared across scopes (immediately, from the files) or a judge verdict stored in `context_state.json`. The judge runs in `context add`/`accept` (CLI only),
   in the detached session-start worker (`hooks._spawn_context_check` → `reverie context check`), in `reverie merge` (daily job), or by `reverie context check`; and only when documents changed since the last run (`doc_signature`).
   *Screen:* Rules "Conflicts" lane (all statuses), Overview "Conflicts" lane (unresolved only), Projects chip, Start text ("UNRESOLVED ..."). Ends when someone runs `reverie context resolve <key> "..."` (becomes `overridden`, reason "resolved by ...") or edits/deletes one of the rules.
5. **Possibly outdated.** *Trigger:* `context_check.check_stale` (same runners as the judge, plus staleness on new commits) finds a commit touching a file or identifier named in backticks in the rule and the judge says CONTRADICTED. Stored in `context_state.json` (this machine only).
   Effect: the rule leaves the "always apply" list and is delivered in the "Possibly outdated team context" group with commit and reason. *Screen:* Overview "Possibly outdated" lane (reason only), Rules table mark, Projects chip, Start text. Ends only by editing the rule text (new id) or deleting it **(inferred)**.
   A rule with no backticked file or identifier can never be flagged (`anchors_of` returns nothing; `check_stale` skips it).
6. **Replaced.** Not a state for team rules: replacement is an edit of the bullet (new id) or a resolution rule (`{resolves: key}`) that overrides both sides of a conflict. The old text is only in git history.
   For **memory**: `reverie stale accept <candidate>` or the extractor's `replaces` judge marks the old item `superseded`; `reverie undo <id>` reverses it. *Screen:* none; superseded and undone items disappear from Memory.
7. **Removed.** Delete the bullet from the Markdown. *Screen:* gone from Rules after reload. No UI action.

---

## 8. Seed data

### What exists today
- **No demo data ships with the product.** There is no `--demo` flag, no sample repo, no fixture used by the UI itself. `grep` finds no seed or demo loader in `reverie/`.
- **Test fixtures only:** `tests/test_ui.py::_seed` creates a repo `svc` with two sibling teams `co/a` and `co/b` that contradict on `squash merge {id: merge}`,
  one stale rule ("Use `old_helper` everywhere", flagged by writing `context_state.json` directly), one waiting proposal ("Prefer small PRs", why "reviews are faster"),
  one personal rule ("Money is integer cents"). `tests/test_ui_writes.py::_repo` makes a repo with `CODEOWNERS` `/.reverie/context/ lead@acme.com` and a non-owner identity `dev@acme.com`.
  `test_memory_500_items_is_paged_and_bounded` inserts 500 `items` rows. `tests/fixtures/claude_transcript.jsonl` is a transcript for importer tests.
- Outside the repo, `~/reverie-lab/` holds benchmark labs (e.g. `home`, `big`, `bench3`); these are not UI demos **(inferred from directory names; not opened)**.
- `docs/CLI.md` uses a fictional `payments`/`platform` team layout (`team = ["../acme/payments", "../acme/platform"]`) as a documentation example only; it is not loadable data.

### Proposed realistic seed for `payments-api` (not created)
All of this can be produced by commands and file writes the product already supports; only the two stale flags and the judge-found conflict need a direct write to
`~/.reverie/context_state.json` (or a real judge run), the same way `tests/test_ui.py::_seed` does. Use a throwaway `REVERIE_HOME`, never `~/.reverie`.

**People (git identities; fictional addresses).**
- *Me:* Mudit Sarda `mudit@payments.example` (use your real config locally).
- *Priya Nair* `priya@payments.example`, payments tech lead.
- *Arjun Mehta* `arjun@payments.example`, backend engineer, not an owner of anything.

**CODEOWNERS** (`.github/CODEOWNERS`):
```
/.reverie/context/rules.md       priya@payments.example mudit@payments.example
/.reverie/context/security.md    priya@payments.example
/.reverie/context/paths-services-ledger.md  mudit@payments.example
```
This makes Arjun a non-owner everywhere, Mudit an owner of `rules.md` and the ledger document but not `security.md`, and Priya an owner of `rules.md` and `security.md`.

**Team rules (10), with reasons.** The document format has no separate "reason" field for a rule, so each reason is written into the bullet text itself ("..., because ...").
(A proposal does carry a separate `why:`, see below.) Documents in `<repo>/.reverie/context/`:

`rules.md` (heading `## Rules`)
1. Money is stored and passed as integer minor units (cents) with an ISO 4217 currency code; never use floats, because rounding drift broke reconciliation in the March settlement run.
2. Every endpoint that creates a charge or refund must require an `Idempotency-Key` header and store the response for 24 hours, because card-network retries otherwise double-charge.
3. Database migrations must be backward compatible with the previous release, because we deploy with rolling restarts and `alembic` runs before the new pods are ready.
4. Use `app/clients/ledger.py` for all ledger writes; do not call the ledger service directly from handlers, because it owns retry and outbox semantics.
5. Log payment identifiers (`payment_id`, `merchant_id`) but never card numbers, CVV or full bank account numbers, because logs ship to a third-party aggregator.

`security.md` (owner Priya only)
6. Secrets come from the secrets manager through `app/config.py`; never read them from environment files committed to the repo, because we rotate them quarterly.
7. Webhook payloads from the card processor must be verified with `verify_signature()` in `app/webhooks/verify.py` before any parsing.

`paths-services-ledger.md` (`applies-to-paths: services/ledger/**`, owner Mudit)
8. Ledger entries are append-only: corrections are new reversing entries, never UPDATE or DELETE, because auditors replay the table.

Rules 9 and 10 are the conflict pair. They live in two sibling team folders named in `.reverie/context.toml`:
`[sources] team = ["../orgdocs/platform", "../orgdocs/payments"]`.
9. (`platform/rules.md`) All outbound HTTP calls retry up to 3 times with exponential backoff {id: http-retries}
10. (`payments/rules.md`) Never retry the card processor's `charge` endpoint; use the idempotency key and poll status instead {id: http-retries}

That pair is a tag conflict between sibling scopes, so it shows as **unresolved** with no judge needed (Overview "Conflicts", Rules "Conflicts", Start "UNRESOLVED ...").
(Rules 1-8 are the repo's own; 9-10 sit in the team folders; total 10.)

**Possibly outdated (2 rules, because their files changed).** Make rules 4 and 7 the flagged ones; both name a real tracked file in backticks, so the product's own
rule for what can be flagged is met. Create a commit that changes `app/clients/ledger.py` (the ledger client now owns the outbox through a new `LedgerWriter` class,
handlers call it directly) and one that renames `verify_signature()` to `verify_webhook()` in `app/webhooks/verify.py`. Then either run `reverie context check` with a judge key,
or write `~/.reverie/context_state.json` `"stale"` entries keyed by the two rule ids (`context.item_id(layer, file, text, scope)`) with `{commit, message, reason, file}`:
- Rule 4: commit "ledger: route writes through LedgerWriter", reason "handlers now call LedgerWriter directly; ledger.py no longer wraps retries".
- Rule 7: commit "webhooks: rename verify_signature to verify_webhook", reason "verify_signature no longer exists; verification moved to verify_webhook with a timestamp tolerance".

**Proposals (3 waiting).**
- *From an owner:* an owner who writes into a document she owns writes directly, so a pending proposal from an owner needs a document she does **not** own.
  Priya (owner of `rules.md` and `security.md`) proposes into `paths-services-ledger.md`: "Ledger reconciliation jobs must be idempotent per `batch_id`", why "the nightly job re-ran twice last week and posted duplicates". File written by `reverie context add` run as Priya with `--doc paths-services-ledger --paths "services/ledger/**" --why ...`.
- *From non-owner Arjun (1):* into `rules.md`: "Prefer small PRs: under 400 changed lines unless it is a mechanical rename", why "reviews of the 1,200-line refund PR took four days".
- *From non-owner Arjun (2):* into `security.md`: "Run `pip-audit` in CI and fail the build on a high severity finding", why "we shipped with an unpatched `cryptography` last quarter".

**Turned-down proposal (1) with reason.** Arjun proposed into `rules.md`: "Use the ORM for all queries, no raw SQL". Priya rejected it with reason: "Reporting queries need raw SQL for window functions; the ORM version was 10x slower in the settlement report. Reopen with a benchmark."
Produced by `reverie context propose`/`add` as Arjun, then `reverie context reject <slug> --why "..."` as Priya.

**Conflict (1):** rules 9 and 10 above (unresolved). To also exercise the resolved case, optional: tag `{id: log-level}` in a repo rule and a team rule where the repo (narrower) wins, giving an `overridden` conflict.

**Memory entries (6)** in `items` for the repo, mixed sources, written with `items.add(..., repo_key=deliver.repo_key(repo))` (an agent-inferred item needs `evidence`):
1. `rule`, user_said: "Never run migrations against the production replica; use the primary and the `--dry-run` flag first."
2. `preference`, user_said: "Keep handler functions under 40 lines; extract to `app/services/`."
3. `decision`, user_said: "We chose the transactional outbox over direct publishing to the event bus for payment events, because the bus lost two events during a failover." (anchor `app/events/outbox.py`)
4. `rejected_approach`, user_said: "Rejected: caching merchant fee schedules in process memory; they change intraday and workers disagreed." (anchor `app/fees/schedule.py`)
5. `environment`, agent_inferred, evidence quote: "Integration tests need `docker compose up postgres redis` first and take about 90 seconds to start." (anchor `docker-compose.yml`)
6. `decision`, agent_inferred with `meta.usage` `{"USED": 3, "CONTRADICTED": 2}` and `meta.challenged: true`: "Refunds over 10,000 INR need manager approval in the admin tool." Evidence: "approval gate in `app/refunds/policy.py`". This one exercises the "challenged" chip and usage tally.

**Personal side (to exercise the count in "Rules in force"):** two personal rules. One is an exact copy of team rule 1's text, kept as a personal-layer context rule (`reverie context add --layer personal`): the Rules table marks it "also a shared rule: hidden here" and delivery leaves it out. (A bullet in `~/.reverie/rules.md` that copies a team rule is also hidden from the start block, but the Rules table never lists those bullets, so it carries no mark.) The other is a bullet in `~/.reverie/rules.md`: "Prefer `rg` over `grep` in examples". `scripts/seed-demo.sh` builds exactly this.

**Also seed:** one second project (an empty repo with Reverie enabled, e.g. `notifications-service`) so the Projects board has "Needs attention" and "All clear" lanes; one disabled project for the "Turned off" lane.

Check after seeding (measured on the seed built by `scripts/seed-demo.sh`): Overview reads Rules in force **8** = 6 team + 1 personal + 1 remembered rule. Of the 10 team rules, two are possibly outdated (rules 4 and 7) and two are the unresolved http-retries pair (agents are told to ask before acting on either), so neither pair is counted; the personal copy of team rule 1 is hidden, so it is not counted either; the other five memory items are a preference (repo-level, an index line only), decisions, a rejected approach and an environment note, so only the one remembered rule counts. Conflicts 1; Possibly outdated 2; Proposals 3; Proposals "Turned down" 1.
As Arjun's identity the three proposals' decide blocks show "Not an owner"; as Mudit the first two (rules, ledger) are decidable and the security one is not; as Priya `rules.md` and `security.md` ones are.
The counts were measured on that seed (see `design/before/README.md`).

---

## Open questions

1-5. **Resolved by the fixes.** Memory hint now says "on this machine" (Q1); the Add card uses `--user` (Q2); a disabled project's Connect tab shows the steps (Q3); `<repo>/.reverie/rules.md` is listed and counted (Q4); the "Findings" lane is gone and kinds use human names (Q5).
6. **Where is `context_state.json` supposed to live for a team?** It sits in `REVERIE_HOME` (per person), so one teammate's judge verdicts (conflicts, outdated flags) are invisible to another until their own judge runs. Tag conflicts are shared (they come from the files). Is that the intended team behaviour?
7. **Judge not run by the UI.** Still true (deliberate, `ui.py` docstring), but a direct add or accept now says the rule has not been checked for conflicts and how to run the check.
8. **CODEOWNERS team handles.** `@org/team` entries are compared as plain strings against `user.email/name/github.user/reverie.handle`. I found no expansion. Confirm that team handles are unsupported.
9. **Resolved.** "Rules in force" now equals what the start block hands over, and says how many rules its limits cut (see section 3, derived numbers).
10. **Unknown `p` on reads.** An invalid project id silently shows the default project. Intentional?
11. **Live behaviour not exercised.** I did not start the server or open a browser, so layout, truncation and wrapping of the screens, and the exact behaviour under `--port 8765`, are read from code and tests, not observed.
