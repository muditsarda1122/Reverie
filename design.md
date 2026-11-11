# Reverie web UI: design system

Settled on the Proposals screen (`design/after/proposals*.html`, committed in `704332b`). Every later screen follows this file; change it here first. The page source is `design/tools/build_after.py`; the old pages are `design/before/`.

## Constraints (from `docs/ui-inventory.md`, not negotiable)
- No JavaScript. Every change is a full page load. `<details>` is allowed.
- CSP is `default-src 'none'`: no web fonts, no images, no external requests. System font stacks and inline SVG only.
- Light and dark through `prefers-color-scheme`; reduced motion respected; works at 390, 900 and 1440 wide. Every screen is checked at all three, light and dark (`node design/tools/shoot.mjs`).
- Every link, form, field and handler of the old page survives, except an action the viewer would be refused (below). `python3 scripts/check-design-wiring.py` checks it.
- Hiding a button is not a permission check. The server-side refusals in `ui.py` stay exactly as they are.

## Theme: Ink
White paper, a hueless black, hairline rules, one teal. Flat: no resting shadows, no cards inside cards, no stat strip, no icon tiles, no pills, no badges, no avatars. The stamp, waivers and critique line sit first in every page's CSS.

## Type
- Rule text: `ui-serif` (`--font-serif`), 1.2rem, line-height 1.4, weight 400, ink. Rule text is the content; it is never wrapped early (no `text-wrap: balance`) and runs to the column width.
- Everything else: `ui-sans-serif` (`--font-sans`), 1rem body, 0.875rem for metadata, headings 700 with -0.02em tracking.
- Paths, commands and file names: `ui-monospace` (`--font-mono`), 0.88em in running text. `` `backticked` `` spans in rule text are code.
- Labels are sentence case, untracked, never capitals.

## Colour (tokens at `:root`; no literal colours elsewhere)
| Token | Light | Dark |
|---|---|---|
| paper, paper-2, paper-3 | 99.5%, 97%, 94% | 19%, 22.5%, 27% |
| ink, ink-2, muted | 16%, 28%, 44% | 92%, 83%, 68% |
| rule, line (control borders) | 89%, 62% | 31%, 54% |
| in (teal) | oklch 50% 0.095 188 | oklch 76% 0.09 188 |
- **Teal means "in force" and nothing else.** Not links, not focus rings, not selection, not "waiting on you". Focus rings and carets are ink. It appears as a word (a flash that says "in force"; later, rules that are in force), never as a field.
- Urgency comes from order and words, not colour. A conflict is first in its list and says "Conflict" in text. There is no red.
- The dark theme is drawn, not inverted: a lifted grey (19%, not black) so the tab does not glare beside a dark terminal; text at 92% so serif does not bloom; rules and control lines raised in lightness; the teal lifted and softened.

## Layout
- Page: `--page` 60rem, centred, 24px gutters (16px under 40rem). One column. No sidebar.
- Top bar: typeset wordmark "Reverie" (links to `/`), a project switcher (`<details>` listing projects, All projects, Add a project), search at the right.
- Tab row: the six tabs on the left, "Connect an agent" and "Add a rule" as plain links at the right end. Under 40rem the tabs wrap onto two lines (never scroll sideways) and the links sit under them.
- **No visible page title.** The active tab names the page. A visually hidden `h1` ("Proposals for payments-api") stays for screen readers and heading order.
- No repo path on project pages; the switcher names the repo.
- Acting-as line: "Acting as <name>, from your git identity." once, under the tab row, where the viewer's authority matters.

## Controls
- **Accept is the only bordered button on a page.** Everything else is text weight: links (ink, 600, underlined), text buttons (underlined, 600), disclosures. Controls are 40px (44px under 40rem), radius 10px.
- A destructive or consequential choice sits behind one click (`<details>`): Reject opens its required reason field; the submit says what it does ("Turn down with this reason").
- All eight states exist on controls; focus-visible is a 2px ink outline; motion is state only (120ms), none under reduced motion.

## Content patterns
- **Entry:** a ruled item, `padding-block` 24px, hairline below. Rule text (serif) first, then "Why: …" in sans, then one metadata line, then actions.
- **One metadata line** under the content, items separated by spacing (never middle dots, never a side column): name, date, document name, "only under <paths>". The document name is mono; its full path (`.reverie/context/<name>`) is its `title` attribute.
- **Names, not emails.** A person is shown by name with the email in a `title` attribute. Emails are printed only where CODEOWNERS only gives emails: "Waiting on an owner of `rules.md`: priya@…, mudit@…".
- **Item-type label.** On a list of mixed items (Overview), each item opens with one small label line: "Proposal", "Conflict" or "Possibly outdated" (sans, 600, sentence case, ink). It is the only thing that names the kind.
- **Rule text is never a link.** It stays plain serif. Each item has one explicit text link that does the navigating: "Decide" (a proposal you can decide), "View" (a proposal waiting on others), "Open rule" (a rule, a possibly-outdated one or either side of a conflict).
- **Optional data is printed only when present.** The commit message beside a commit hash is shown only if the judge stored one; otherwise the short hash alone.
- **Group heading:** an `h2` with a 1px ink rule under it. "Waiting on you" first, "Waiting on others" after; the others carry who they wait on.
- **Summary line:** one bold line under the acting-as line: "2 waiting on you, 1 waiting on others." / "Nothing waiting on you. 3 waiting on others." / "Nothing waiting."
- **Counts:** only for what waits on the viewer, only on the tabs that mean it (Overview, Proposals), hidden at zero. No counts on Rules or Memory. A count is a plain bold numeral beside the label, not a badge.
- **Empty sections are omitted, not drawn.** No empty boxes, no "none" rows, no zero counts. When there is nothing at all, one line says so.
- **Never show an action the viewer would be refused.** Where they cannot decide, no buttons: say who can.
- **The CLI command sits quietly next to its action**, behind "As commands" or as a small mono line; there is no footnote on every page.
- **Command blocks size to their content** (`width: fit-content; max-width: min(65ch, 100%)`), small mono on `paper-2` with a hairline. They never outweigh the line above them.
- **Disclosures** (`<details>`): a plain underlined summary, a small hairline caret where it opens a list. Collapsed history ("Turned down, 1") is quiet.
- **Flash:** a plain line, no box. It quotes the rule and says "in force" in teal.

## Names (every screen uses these and no others)
Team rules (documents in `.reverie/context` and the org and team folders; they have owners and proposals) · Shared rules file (`<repo>/.reverie/rules.md`: through git, no owners, no review) · Your rules (`~/.reverie/rules.md` bullets and personal-layer documents; only your agents, every repo) · Remembered (memory items agents captured on this machine).

## Copy
Plain, short, specific, in the product's words. No marketing, no exclamation marks, no emoji. Do not invent features or numbers; list data the inventory says does not exist instead of drawing it.

## Waiting on you (one definition, used by the Overview tab count, Overview and Proposals)
- A proposal you can decide (you are an owner of its document, or no CODEOWNERS entry covers it).
- An unresolved conflict, or a possibly-outdated rule, in a document you own or that no CODEOWNERS entry covers.
- Everything else is "waiting on others", listed after, with who it waits on.

## Process
Screen order: Overview, Rules, Rule page, What agents are told, Memory, Add, the rest. Each screen is built in `design/tools/build_after.py`, linted (`sloplint --render`), wiring-checked, and shot at 1440, 900 and 390, light and dark, before review.
