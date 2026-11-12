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
