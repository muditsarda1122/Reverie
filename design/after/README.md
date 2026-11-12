# Proposals, redesigned (theme: Ink)

Static pages built by `python3 design/tools/build_after.py` from the seed in `design/before/`. Screenshots (desktop 1440 and 900 wide, light and dark) come from `node design/tools/shoot.mjs` and sit in `screenshots/`.

| File | Replaces | Identity | Shows |
|---|---|---|---|
| `proposals.html` | `design/before/proposals.html` | mudit | 2 waiting on you (ledger, small PRs), 1 on others (security, owner Priya), 1 turned down |
| `proposals-arjun.html` | `proposals-arjun.html` | arjun | Nothing on him; "Nothing waiting on you. 3 waiting on others."; no "Waiting on you" section and no buttons |
| `proposals-priya.html` | `proposals-priya.html` | priya | 2 on her (small PRs, pip-audit), 1 on others (the ledger one, owner Mudit) |
| `empty-proposals.html` | `empty-proposals.html` | mudit, notifications-service | "Nothing waiting. Suggest a rule with Add a rule." and the two commands, quieter |
| `proposals-accepted.html` | extra | mudit | The page after accepting "Prefer small PRs": the plain flash line quotes the rule; the only place the teal appears ("in force") |
| `proposals-15.html` | extra | mudit | **Layout test, not product data.** 15 waiting; the three seed proposals plus twelve with invented rule text. It says so at the top of the page and in its title. |

`screenshots/proposals-opened-desktop-*.png` shows the Reject reason field, the commands and Turned down open.

## Overview (built from `design.md`)
| File | Replaces | Identity | Shows |
|---|---|---|---|
| `overview.html` | `design/before/overview.html` | mudit | 4 waiting on you (2 proposals, the conflict, the ledger rule that may be outdated), 2 on others (pip-audit, the webhook rule: owner Priya) |
| `overview-arjun.html` | `overview.html` (no capture of its own) | arjun | 1 on him (the conflict: no CODEOWNERS entry covers either document); 5 on others |
| `overview-capped.html` | `overview-capped.html` | mudit | As mudit, plus "6 rules are cut by the start limit. See what agents are told." |
| `empty-overview.html` | `empty-overview.html` | mudit, notifications-service | "Nothing needs you." |

- **Waiting on you** (one definition, also the Overview tab count): proposals you can decide, plus unresolved conflicts and possibly-outdated rules in documents you own or that no CODEOWNERS entry covers. Everything else is listed after under "Waiting on others", with who it waits on. Order inside each group: proposals, conflicts, possibly-outdated rules.
- Each item opens with a label line (Proposal, Conflict, Possibly outdated); rule text is plain, never a link. One text link per item: "Decide" or "View" (to `/proposals?p=…#p-…`, the old links), "Open rule" for a possibly-outdated rule and under each side of a conflict. A conflict shows both rules, where each comes from (team folder and document), that agents are told to ask, who can settle it, and its `reverie context resolve` command quietly. A possibly-outdated rule shows the rule, the reason and the commit (short hash and message) that changed the code.
- **New route for the port:** `/rule?p=<project>&id=<rule id>`, a read-only page for one rule (designed next). Rules link to it from Overview.
- No stat strip, no "Rules in force" number on Overview. The cut-by-the-start-limit line appears only when the count is above zero.
- Data used that the old Overview did not print: the commit message beside the hash (`context_check.check_stale` always writes `message`, the commit subject, but it is empty for a commit with no subject, so the page prints the message only when present), and the folder of each conflicting rule (`CtxItem.label`).

## Rules (built from `design.md`)
| File | Replaces | Shows |
|---|---|---|
| `rules.html` | `design/before/rules.html` | The seed: 8 rules in force. Team rules (team folders platform and payments, then this repo's documents), Your rules, Remembered |
| `rules-capped.html` | none (no capture) | The 35-extra-rules seed: "37 rules in force, 6 cut by the start limit"; the 36 remembered rules sit under one disclosure |
| `empty-rules.html` | `empty-rules.html` | notifications-service: only the one personal bullet, "1 rule in force" |
| `rules-states.html` | extra | **Layout test, invented rule text.** Locked, overridden (locked and narrower scope), a conflict whose documents have owners, the shared rules file |

- Grouped by source with the design.md names; empty groups are omitted (the seed has no shared rules file, no org folder). Each rule: serif text, then one meta line: the status word (exceptions only), document name (full path in the title), "only under", "locked", "Open rule". Rule ids appear only in the Open rule link.
- **Mark exceptions, never the normal state.** A rule that is in force carries no status; only Possibly outdated, Conflict, Overridden, Hidden: same as a team rule and Cut by the start limit are marked (ink words). The teal "in force" appears once, in the summary line ("8 rules in force."). "Open rule" in a list is quiet: weight 400, muted, underlined. A conflict's note sits under both rules; an overriding rule says what it overrides and why.
- A group with more than 10 rules folds, but its exceptions (cut, conflict, outdated ...) stay visible above the fold; only the in-force rest collapses, and the summary says "<n> more in force" ("30 more in force" in the capped seed).
- **Added beyond the brief: a "Remembered" group** (memory items of kind rule that are in force). They are part of the "in force" count and the only rules the start limit cuts in the capped seed, so they have to be listed to show which were cut.
- For the port: the old page listed only team documents and the shared file; Your rules (`rules.bullets` of `~/.reverie/rules.md`) and Remembered rules (`mem_rules`) are in `collect()` already. Rules-file bullets have no id, so `Open rule` would use a hash of the text (here the first 10 hex of a sha1 of the normalised text). The ids of the six cut remembered rules in `rules-capped.html` are made up (the capture lists only 30 in the start block).
- Dropped from the old page: the Id column and the Kind and Marks columns (now the meta line), the separate Conflicts lane (the note sits with each rule) and the "Hint shown to agents" line.

## Rule page (`/rule?p=<project>&id=<rule id>`, read-only)
| File | Case | Data |
|---|---|---|
| `rule-team.html` | in force, scoped to `services/ledger/**` | seed rule `59cf39933e` |
| `rule-conflict.html` | unresolved conflict `http-retries` | seed rule `e3d2757f37` and the other side in full |
| `rule-outdated.html` | possibly outdated | seed rule `9c55b68de3`, its commit and reason |
| `rule-overridden.html` | overridden by a narrower rule | **invented rule text**: the seed has no overridden rule (labelled on the page) |
| `rule-remembered.html` | a remembered rule | seed item `932ff77c` (user said it; no anchor, commit, verification or usage) |
| `rule-remembered-challenged.html` | extra | the seed's challenged item `23e2645b`. It is a remembered decision, not a rule, but it is the only seed item with anchors, evidence, a usage tally and "challenged" |

- The rule text is the page's only `h1`, set large in serif. Status appears under it only for exceptions. Rules is the active tab; "Back to Rules" is a quiet link at the top. The remembered decision page is the exception: Memory is the active tab and the link reads "Back to Memory".
- Sections: Where it comes from (layer, folder, document path, section, owners, "only under", "locked"), then Conflict, Overridden, Possibly outdated or Remembered on this machine as the case needs; the id with `reverie show <id>` at the end. No actions: the one command (`reverie context resolve`) sits in a small block.
- **What the port can read.** `context.show` / `reverie show` print: id, layer and folder (label), kind, locked, text, document (absolute path; the page shows it repo-relative), section, applies-to, paths, owners (`owner_text`), the conflict key, status and parties, and the stale commit (8 characters) and reason. The page also needs, all already in memory: the conflict's `reason` and `winner` (`Conflict` in `context.resolve`) and the stale entry's `message`.
- **Not available from `context.show`, so not drawn (a field with no value is omitted from the page):** which file a possibly-outdated rule's commit changed (`check_stale` stores commit, message, reason and the document only); for a remembered item, anchors, grounding commit, last verified and usage when the item has none (the seed's rule has none). `reverie show` for a remembered item prints files, symbols, source, author, level, evidence and a freshness line, but not `created_at`, `last_verified`, the usage tally or `challenged`; the page reads them from the item row (`meta.usage`, `meta.challenged`).
- Ids for bullets in `rules.md` files are a hash of the text (see Rules); there is no `reverie show` for them today.

## What agents are told (`/start?p=<project>`)
| File | Replaces | Shows |
|---|---|---|
| `start.html` | `design/before/start.html` | The seed: "8 rules are told to agents, plus an index of 5 remembered decisions and findings.", the one conflict told as ask-first, the verbatim text |
| `start-capped.html` | `start-capped.html` | "37 rules are told to agents, plus an index of 5 ... 6 more rules apply but were cut, because agents are told at most 30 remembered rules for this repo.", the six cut rules, the limits line, the text |
| `empty-start.html` | `empty-start.html` | notifications-service: "1 rule in force is told to agents." and the 113-character text |

- The text in the block is copied from the old captures, which are the output of `deliver.startup_text` itself. The page does not rebuild it. **`ui.py` already calls that function** (`collect()["start"]`, used by `start()` and `rules_in_force`), so the port keeps calling it.
- Verbatim text contains the words "scope" and "scopes"; it is quoted as agents receive it, so the plain-words rule does not apply to it.
- **Port: the start-text function must return which limit cut each rule.** Today it does not: `startup_text` and `rules_in_force` report no reason. `_capped` adds the same "(+N more ...)" line whether it stopped at the line cap (30 remembered rules, 15 across-all-repos rules, 8 outdated ones) or at its character budget, and a whole-text truncation adds a "(…truncated" line. The port needs `rules_in_force` (or `startup_text`) to return, per cut rule, the limit that cut it (the 30-line remembered-rules cap, the 15-line cap, the 8-line outdated cap or the character cap); the capped page gives the reason that applied in the seed. The index count ("an index of 5 ...") should come from the same function too; the mock counts the index lines in the text. The character note prints the configured limit (`deliver.startup_cap()`), not 8000.
- **No command changes the limits.** The 15/30/8 line caps are constants in `deliver.py`; the 8000-character cap is `deliver.startup_chars` in `config.yaml` or `REVERIE_STARTUP_CHARS`, with no CLI command. The page says nothing about changing them. It names only `reverie rules list`, which the old page also named.
- The cut rules' texts come from the page's rule data; in the port they are the rules counted by `rules_in_force` whose text is not in the block. The ids of the six cut rules are the made-up ones noted under Rules.
- The capped and standard captures came from two seed runs, so the commit hashes in their verbatim text differ from each other and from the Overview pages.
- Dropped from the old page: the "exactly what an agent is handed ... is not counted as a delivery" paragraph (read-only is implied; the page says whose agents are told) and the combined "rules in force · cut" hint (now the summary line).

## Memory (`/memory?p=<project>`)
| File | Replaces | Shows |
|---|---|---|
| `memory.html` | `design/before/memory.html` | 5 items (the rule is listed under Rules); "Needs a look" holds the challenged decision, then Decisions, Rejected approaches, Preferences, Environment |
| `memory-capped.html` | none (no capture) | The same 5 items and "36 rules you stated are listed under Rules." |
| `empty-memory.html` | `empty-memory.html` | notifications-service: "Nothing remembered for this repo yet." |
| `memory-states.html` | extra | **Layout test, invented text.** Possibly outdated items (with and without a commit message) next to the challenged one |
| `rule-remembered-outdated.html` | extra | **Layout test, invented text.** The item page of a possibly outdated decision, with `reverie stale keep` |

- **Every item is listed on exactly one tab.** Rules lists every rule, remembered ones included; Memory lists the other kinds and says how many rules are under Rules (linked, omitted at zero). The summary counts what Memory lists: "5 remembered items. 5 are told to agents in an index. 1 needs a look."
- Each item: serif stated text, one meta line (who said it, anchors in mono with the path in the title, date, usage in words only when non-zero, a quiet "Open" link to the item's page). The kind label sits above an item only in "Needs a look", where kinds mix.
- **Needs a look** = challenged items (`meta.challenged`) and possibly outdated ones (`meta.stale`, set by `reverie stale check`). "Anchor no longer exists" is not a state in the data, so it is not built. Each item says why in one sentence and ends with one sentence carrying the one command that fixes it (`reverie undo <id>`, or `reverie stale keep <id>` for a possibly outdated one).
- **Commands live on the item's page** (the remembered pages), each with a plain sentence: `reverie show <id>`, `reverie undo <id>` and `--restore`, `reverie promote <id>` and `--demote`, and on possibly outdated items `reverie stale keep <id>`. There is no edit or verify command. `reverie usage` lists tallies, `reverie merge` merges near-duplicates and `reverie digest` shows what was remembered since you last looked; none is shown per item. `promote` only changes items that belong to one repo.
- Port: the old page paged its items (`PAGE_SIZE`); the grouping needs all of them, or the same pager per group, and must leave out kind `rule` (those go to Rules).
- Dropped from the old page: the id column (the id is on the item's page), the lane-per-kind board and the "Open one with reverie show <id>" hint.

## Add (`/add?p=<project>`; the form posts to `/do/add`)
| File | Shows |
|---|---|
| `add.html` | As Mudit (an owner of `rules.md`): "No review: you are an owner" |
| `add-arjun.html` | As Arjun (owns nothing): "A rule waits until an owner of rules.md accepts it (Priya Nair or Mudit Sarda); guidance is added straight away" |
| `add-error.html` | Empty text: "Refused: text is empty" above the field, which is marked `aria-invalid` and `aria-describedby` |
| `add-error-kept.html` | "Refused: that is not a place this project can add to." with the typed text and reason kept; the destination radios are marked |
| `add-error-secret.html` | The secret refusal: the text is **not** kept, as in `ui.py` |
| `add-done.html` | After a direct add: the flash quotes the rule, then a blank form |
| `add-arjun-done.html` | After a proposal: the flash quotes the rule and names the owners |

The typed rule text in the error and done pages is an example, not product data.

- Field names, method and action are exactly the old ones (`token`, `p`, `text`, `kind`, `target`, `why`; POST `/do/add`). The old selects (`kind`, `target`) are radio groups with the same names and values (`repo`, `team|orgdocs/platform`, `team|orgdocs/payments`; `rule`, `guidance`). The wiring check passes.
- The one visible heading is the question "What should agents know about this repo?"; the form creates a rule or guidance, so it does not say "always". The rule text field is a large serif textarea. The submit is the bordered button with the neutral label "Add rule", because what happens depends on the radios and there is no JavaScript.
- Each destination carries one plain sentence on who is told and whether anyone reviews. All destinations are usable by every viewer, so no radio is disabled and no reason is given.
- **"Your rules" is not a destination.** The form cannot write to `~/.reverie/rules.md`; that stays the quiet command `reverie rules add --user`. The other quiet commands are `reverie context add` (the same as the form) and `reverie context propose`.
- The form cannot pick a document or a path scope: a rule goes to `rules.md` and guidance to `guidance.md`. In the seed no CODEOWNERS entry covers `guidance.md`, so guidance from a non-owner is added directly; that is said on the radio.
- **Reworded flash (for the port):** the old messages are "Added to rules.md. It applies from the next session. ..." and "Written as a proposal (<slug>.md). The owners (<emails>) review it; it is not in force yet." The new ones quote the rule and name the owners (name, email in the title, "you" for the viewer). The wording is a design change; the refusals' messages stay as in `ui.py`.
- **Server behaviour worth knowing:** there is no "too long" refusal (each field is silently cut to its limit: text 1000, why 300); the secret and Reverie-off refusals do not keep the typed text; whitespace in the text is collapsed to single spaces.
- **Refusal wording (words only; the server still refuses exactly as before):**

| Old (`ui.py`) | New |
|---|---|
| Refused: that project is not known to Reverie. | Nothing was saved: Reverie does not know that project. Open it from the project list. |
| Refused: Reverie is turned off for this project. Turn it on first. | Nothing was saved: Reverie is turned off for this project. Turn it on with `reverie enable`, then try again. |
| Refused: this looks like it contains a secret, so nothing was saved. Remove the value and try again. | Nothing was saved: this looks like it contains a secret. Remove the value and try again. The text was cleared on purpose. |
| Refused: that is not a place this project can add to. | Nothing was saved: this project cannot add to that place. Pick another option. |
| Refused: text is empty | Nothing was saved: write the rule first. |
| Refused: <specific reason from `context.add`>, e.g. "no team source configured: add [sources] team = [...] to .reverie/context.toml" | Nothing was saved: <the same reason>, then what to do. |
| That did not work, and nothing was changed. The CLI still does. | Nothing was saved: something went wrong. Try again, or add it with `reverie context add`. |

- Success messages end with a link: "See it in Rules" (`/context`) after a direct add, "See the proposal" (`/proposals`) after a proposal.
- **What `why` does:** it is written into a proposal file for the owners (shown on Proposals as "Why: ..."). A direct add (`_append`) does not store it. The page says so under the field.
- **Where the change goes:** `context.add` edits (or creates) the file in the chosen folder and never runs `git commit`; the git identity only decides whether the writer is an owner and names the proposer. Each destination says "Reverie edits the file and does not commit it."
- The three terminal commands sit behind "From a terminal"; the secret line stays visible.
- Dropped from the old page: the "Shared folders use their own git identity: ..." line (each team destination says it instead) and the two cards, now the quiet commands.

## The small screens
| File | Replaces | State |
|---|---|---|
| `board.html` | `design/before/projects.html` | As Mudit: payments-api has 4 waiting on you (2 proposals, 1 conflict, 1 possibly outdated rule); notifications-service has nothing; legacy-billing is turned off |
| `board-empty.html` | same | The state where nothing waits anywhere (the seed's payments-api with everything decided) |
| `new.html` | `new.html` | Add a project: three steps with the real commands. **There is no form here, so there is no error or done state** |
| `connect.html` | `connect.html` | Four steps with the real commands; "Reverie is on for this repo."; "No agent is set up on this machine yet." |
| `connect-connected.html` | extra | **Layout test**: the same with two agents set up (the seed's throwaway home has none) |
| `connect-off.html` | extra | legacy-billing: "Reverie is turned off for this repo." (Connect stays reachable when a project is off) |
| `search.html`, `search-both.html`, `search-empty.html`, `search-blank.html` | `search-ledger.html` | "ledger" (2 rules, no memory), "refund" (a rule and a memory item), no match, no query |
| `off.html` | `disabled-project.html` | The page every tab shows when Reverie is off for a project, with the one way to turn it on |
| `not-found.html` | none | An unknown path (the only 404) |
| `not-found-rule.html` | none | **Layout for the new `/rule` route**: an unknown rule id, with "Back to Rules" |
| `error.html` | none | The 500 page |

- **Board.** The old cards (path, "N rules in force · N remembered", chips, three lanes sorted by `_attention`) are replaced by the per-viewer "waiting on you" definition (proposals you can decide, conflicts and possibly outdated rules you can settle); the repo path and the rules-in-force figure are dropped. In the port, the per-project identity is each repo's own git identity.
- **Add a project (`/new`) is static**: no form, no POST route, no refusals. The brief's error and done pages do not exist.
- **What makes a repo appear in the list:** `ui.projects()` reads the `repos` table plus the repo `reverie ui` was started in. A row is written the first time Reverie checks a repo's state (`repos.state_of`, called when an agent session or a Reverie command runs there; a first-seen repo is recorded as enabled, because Reverie is on by default) and by `reverie enable` / `reverie disable` (`repos.set_repo_state`). So `reverie enable` is optional; using an agent in the repo is enough. Add a project and Connect say this the same way, with `reverie install` first.
- **"Connect an agent" is removed from the tab row's actions on every page** (the Connect tab keeps the link). The wiring check does not flag it, because `/connect?p=...` is still on each page through the tab; the removal is intended.
- **Connect.** `install.configured_servers()` returns every harness config's Reverie entry (agent key, command); `_AGENTS_BY_KEY` names them; `report_configured_servers` checks that the command still exists. The old page does not call any of it, so "what is already connected" is new data for the port (a read-only call). The mock's seed has none. The command shows the real repo path (`cd /path/to/payments-api && reverie enable` here is a placeholder).
- **Search** is a case-insensitive substring match over team context items and memory items (50 each), from `ui.search_page`; an empty query shows "Type something to search"; the query is cut at 120 characters. The new page keeps the query in the box (the old box was empty) and sends remembered rules to Rules, other memory items to Memory, as on the tabs. Each result links to its own page.
- **Turned off.** Every project tab except Connect shows the off page when `is_disabled`; so does Add a rule. The only way back is the CLI (`reverie enable`); the UI has no write route for it.
- **Not found.** Only an unknown path returns 404. An unknown project id does not: `_target` silently falls back to the first project. An unknown rule id has no route yet.
- **Error.** `render()` returns 500 with "Something went wrong. This page could not be built. The CLI still works." and no sidebar; no trace is shown. The page adds one next step. Other refusals (403, 405, 411, 413, 415) are plain text, not pages.

## Rules this screen follows (from the review)
- One quiet metadata line under the why, at every width: name, date, document name, "only under <paths>". The full path (`.reverie/context/<name>`) is the document's title attribute; the proposer's email is the name's title attribute. "Waiting on an owner of ..." also shows names (email in the title, "you" for the viewer); an email is printed only when no name is known for it. CODEOWNERS gives only emails, so the port must look names up.
- No repo path on this page (the switcher names the repo).
- Tab counts mean "waiting on you" and vanish at zero: Overview (conflicts + possibly outdated rules + proposals you can decide) and Proposals (the proposals you can decide). No counts on Rules or Memory.
- When nothing waits on you the "Waiting on you" section is omitted; the summary line says so.
- Accept is the only bordered button. "Add a rule" and "Connect an agent" are links. Reject opens its reason field; the submit says "Turn down with this reason".
- There is no visible page title; the active tab says it. A visually hidden `h1` ("Proposals for <repo>") stays for screen readers and heading order.

## Checks
- `sloplint --render` over all six pages: 0 FAIL, 0 REFLEX, 1 WARN (a one-word last line on a heading at 375px in the layout test, whose rule text is invented).
- `python3 scripts/check-design-wiring.py` compares every link, form, field and anchor id of the four old pages with the new ones (it uses a real HTML parser, so the old pages' unquoted attributes are read). Everything is kept except the Accept and Reject forms of proposals the viewer cannot decide, listed as EXPECTED.
- **`sloplint --kept` reports one FAIL, and it is intended.** It is the per-proposal hidden `slug` inputs (and their forms) that are no longer drawn for a proposal the viewer cannot decide. The brief says never to show an action that will be refused, and the old page marked those "Not an owner: the CLI would refuse". The tool compares files in place, so it was run in a scratch copy of the four pages; the stamp records the drop, and the wiring script above is the precise check.

## For the port into `ui.py`
- **Hiding a button is not a permission check.** The server-side refusal in `ui.py` (`context.accept` / `context.reject` raising for a non-owner, and `/do/accept`, `/do/reject` refusing) stays exactly as it is; the redesign only stops drawing actions the viewer would be refused.
- Data the old page did not print, all already in the proposal files: the document name and its repo-relative path (from `document`), a proposal's `paths` scope, and on a turned-down proposal who proposed it (`proposed-by`) next to who turned it down. `from`, `first-stated` and `usage`, when a proposal carries them, would go on the same metadata line (the seed has none).
- Names come from `proposed-by` ("Name <email>"): the name is the text and the email the title attribute.
- The Overview and Proposals tab counts use a new definition (waiting on the viewer) in place of `_attention` (which counts all proposals); the Rules and Memory counts are dropped.
- The flash after accept quotes the rule; today's message does not, so the accepted proposal's text would be added to it. The conflict-check sentence (`CHECK_LATER`) is unchanged.
- Rule text is rendered with `backticked` spans as monospace code.

## Tooling
- `puppeteer-core` lives outside the repo at `~/.cache/reverie-design-tools/puppeteer` (installed with `npm i puppeteer-core` there). `design/tools/shoot.mjs` finds it there; for Hallmark's render tier set `HALLMARK_PUPPETEER="$HOME/.cache/reverie-design-tools/puppeteer"`. Chrome is taken from `/Applications/Google Chrome.app` or `CHROME_PATH`.


## Screenshots are not in git

`screenshots/` is gitignored (about 35 MB per folder). Regenerate it with `node design/tools/shoot.mjs` (needs `puppeteer-core` and Chrome, see above); it writes 1440, 900 and 390 wide, light and dark, one PNG each.
