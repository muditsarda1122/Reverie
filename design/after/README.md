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

## Rules this screen follows (from the review)
- One quiet metadata line under the why, at every width: name, date, document name, "only under <paths>". The full path (`.reverie/context/<name>`) is the document's title attribute; the proposer's email is the name's title attribute. Emails are printed only in "Waiting on an owner of ...", because CODEOWNERS only gives emails.
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
