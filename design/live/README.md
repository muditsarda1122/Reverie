# The live pages (the port into reverie/ui.py)

Pages of the real `reverie ui`, saved by `scripts/capture-live.sh` from the same demo seeds as `design/before/` (throwaway home; nothing under `~/.reverie` is touched), and named like the mockups in `design/after/` so the two can be compared:

- `node` + `SHOOT_DIR=design/live node design/tools/shoot.mjs` writes `screenshots/` at 1440, 900 and 390, light and dark.
- `WIRING_AFTER=design/live python3 scripts/check-design-wiring.py` compares every link, form, field and id with `design/before/`.

## Stage 1: shell, Overview, Proposals, unknown project

Visible differences from the mockups (text and pixel comparison, as mudit, priya and arjun, plus the empty and capped states):

1. **"you" for the viewer.** Where the viewer is named on a page (the proposer of their own proposal, who turned something down), the live page says "Arjun (you)"; the mockup says "Arjun Mehta" there. The acting-as line keeps the full name. The mockups only used "(you)" in owner lists; the brief says "you for the viewer" everywhere.
2. **Commit hashes** differ because the live seeds are built again (a different commit each run). Nothing else on Overview differs.
3. **`/rule` links.** Overview's "Open rule" links land on the real Rule page (stage 2); every one answers 200 (see the link crawl below).
4. **Unknown project:** `unknown-project.html` is new (no mockup): "No project with that id.", a link back, no tab row.
5. **Everything else is identical:** the Overview and Proposals screenshots of mudit are pixel-identical to the mockups except for the commit hashes; the empty states and Proposals as mudit are pixel-identical.

## Stage 2: Rules, the Rule page, What agents are told, Memory

Captured from the real server on the same seeds (`scripts/capture-live.sh`): `rules*.html`, `empty-rules.html`, `rule-team`, `rule-conflict`, `rule-outdated`, `rule-remembered`, `rule-remembered-challenged`, `not-found-rule`, `start*.html`, `empty-start.html`, `memory*.html`, `empty-memory.html`. The script also crawls every `/rule` link on the saved Overview, Rules, What agents are told and Memory pages (seed: 18 distinct links; capped seed: 53) and fails on any that does not answer 200: none is broken.

Visible differences from the mockups (text diff of every page pair, and a pixel diff of the screenshots):

1. **Ids, hashes and dates that come from a fresh seed run.** Remembered-item ids (random uuids), commit hashes, and the `Remembered` date on a remembered item's page differ on every run. Titles of rule pages carry the id of that run. Nothing else in the text differs: for the Rules, Memory, What agents are told, Rule and empty pages the diff is only those values, plus "(you)" where the viewer is named (stage 1).
2. **Pages with no live capture.** The demo seed has no overridden or locked-and-overridden rule, no rules-file bullet in the shared file, no possibly-outdated remembered item and no unresolved conflict with owners, so `rules-states.html`, `rule-overridden.html`, `rule-remembered-outdated.html` and `memory-states.html` have no live page. The real code renders those states in `tests/test_ui_rules.py` (overridden by a narrower scope, overridden by a locked rule and the winner's "Overrides" section, a hidden personal copy, remembered outdated with and without a commit message, rules-file bullets, a challenged decision).
3. **What agents are told, "Cut because ..."**: the reason now comes from the start-text trace. When the cut rules were stopped by different limits, the summary drops the single "because ..." and each cut rule says its own reason; the seeds only have one reason, so the pages match the mockups.
4. **Guidance.** Team documents may hold guidance items; the old page listed them in its table. The live Rules page lists them beside the rules with the word "Guidance" and no state. The mockups have none.
5. **Everything else is identical:** the empty states, the capped state and the Rule pages are pixel-identical to the mockups except for the values in 1.

## Stage 3: Add, the board, Add a project, Connect, Search, Turned off, Not found, Something went wrong

Captured from the real server on the demo seed, plus form submissions made the way a browser makes them (token from the page, same-origin header, redirect followed): `board`, `new`, `connect`, `connect-off`, `off`, `off-rules`, `search`, `search-both`, `search-empty`, `search-blank`, `add`, `add-arjun`, `add-error` (empty), `add-error-kept` (unknown destination), `add-error-secret`, `add-done`, `add-arjun-done`, `not-found`, `error`.

`scripts/capture-live.sh` ends by crawling **every internal link** on every saved page (not only `/rule`) against the running server with `scripts/crawl-links.py`: 29 distinct links on the Overview/Rules/Start/Memory/Proposals/rule pages, 21 on the stage-3 pages, 64 on the capped seed; 0 broken (a 404 or 500 fails the script). The wiring check passes on every stage-3 page.

Visible differences from the mockups (text diff of every page pair, and a pixel diff of the screenshots):

1. **Ids and counts from a fresh seed run.** Project ids in `?p=` and the hidden `p` field, and a remembered item's id in a "forget it" sentence, differ on every run. Nothing else on `board`, `new`, `search*`, `add*` and `not-found` differs in text.
2. **Connect lists the machine's real agents.** The mockup `connect.html` says "No agent is set up on this machine yet." because it was drawn for the seed's empty home; `install.configured_servers()` reads the real home directory (it is not part of the throwaway seed), so the live capture says "Set up on this machine: Claude Code." on this machine. `connect-connected.html` (the mockup's "layout test" with two agents) has no capture of its own; a test fakes the list and asserts the names.
3. **The real repo path in the enable command.** `connect-off` and `off` show `cd '<real path>' && reverie enable`, shell-quoted, where the mockup had `/path/to/legacy-billing`. The path is the repo's real one (a throwaway directory in the capture).
4. **Quotes in command blocks are written as `&quot;`** in the source (the same text on screen; the mockups wrote a bare `"`).
5. **`board-empty` has no live capture.** On the seed, payments-api always has something waiting on any viewer (a conflict nobody owns, and possibly outdated remembered items belong to the person who runs `reverie ui`). `tests/test_ui_stage3.py` renders "Nothing is waiting on you in any project." with a non-owner.
6. **`error.html` is the page `ui.error_page()` returns,** saved directly: a 500 cannot be forced on the live server without a hook the product should not have. A test forces an exception in a page handler and in a write and checks the response.
7. **A refusal after an unknown project** (a POST naming a project the server does not know) now ends on the board with the line "Nothing was saved: that project is not known to Reverie." The mockups have no such state.
8. **Stage-1 and stage-2 fix carried over:** the Proposals accept message now shows `reverie context check` as code (the mockup did; stage 1 showed backticks).
9. **Pixel comparison (19 pages x 3 widths x light and dark):** identical except `connect` (the agents line, difference 2), `connect-off` and `off` (the real path wraps onto more lines, so those pages are 31 px taller at 1440 and 900, difference 3), `search-both` (one remembered id, difference 1) and a one-pixel anti-aliasing difference in two dark `add-error-kept` shots. Everything else is identical.


## After the stage 3 review (intended differences from the mockups, which were not regenerated)

The wiring check now reports 7 items missing, all intended:

- **`POST /do/add` field `why`** (5 pages) and the Why label: shown only when an option can lead to a proposal for this viewer. The live pages are captured as an owner (mudit), so no Why field; `add-arjun*` (a non-owner) keep it.
- **`/add` link** (2 pages: `off`, `off-rules`): no "Add a rule" while Reverie is off.

Other differences: the "From a terminal" direct-add example has no `--why` and the `propose` example does; Connect and Add a project say "Set up another agent" when an agent is already set up, and list the agents `reverie install` supports (Claude Code, Cursor, OpenCode and Codex); the error page has no "The CLI still works." and no menu divider; `error-write.html` is the failed-write wording; the board and Not found have a hidden `<h1>`.

## Screenshots are not in git

`screenshots/` is gitignored (about 35 MB per folder). Regenerate it with `SHOOT_DIR=design/live node design/tools/shoot.mjs` (needs `puppeteer-core` and Chrome, see above); it writes 1440, 900 and 390 wide, light and dark, one PNG each.
