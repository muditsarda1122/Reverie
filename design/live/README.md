# The live pages (the port into reverie/ui.py)

Pages of the real `reverie ui`, saved by `scripts/capture-live.sh` from the same demo seeds as `design/before/` (throwaway home; nothing under `~/.reverie` is touched), and named like the mockups in `design/after/` so the two can be compared:

- `node` + `SHOOT_DIR=design/live node design/tools/shoot.mjs` writes `screenshots/` at 1440, 900 and 390, light and dark.
- `WIRING_AFTER=design/live python3 scripts/check-design-wiring.py` compares every link, form, field and id with `design/before/`.

## Stage 1: shell, Overview, Proposals, unknown project

Visible differences from the mockups (text and pixel comparison, as mudit, priya and arjun, plus the empty and capped states):

1. **"you" for the viewer.** Where the viewer is named on a page (the proposer of their own proposal, who turned something down), the live page says "Arjun (you)"; the mockup says "Arjun Mehta" there. The acting-as line keeps the full name. The mockups only used "(you)" in owner lists; the brief says "you for the viewer" everywhere.
2. **Commit hashes** differ because the live seeds are built again (a different commit each run). Nothing else on Overview differs.
3. **`/rule` links.** Overview links to `/rule?p=...&id=...` (the Rule page is stage 2); until then they land on the old "Not found" page.
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
