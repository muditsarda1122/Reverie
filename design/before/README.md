# The Reverie web UI, before the redesign

Static copies of every page, saved exactly as `reverie ui` served them (HTML, inline styles, no scripts), at branch tip
`1c1f4d1` of `feat/continuity-spine`. Regenerate with `scripts/capture-before.sh`; the data comes from
`scripts/seed-demo.sh` (the `payments-api` seed proposed in `docs/ui-inventory.md` section 8), built in a throwaway
folder with its own `HOME`, `REVERIE_HOME` and git config, so nothing under `~/.reverie` or the real git config is touched.

Notes for viewers:
- Links and form actions are the server's own (`/project?p=...`), so they do not work when a file is opened from disk. The pages are for looking at.
- Each page header shows the seed's temporary path (`.../reverie-capture.XXXXXX/seed/payments-api`) and each form carries that run's one-off CSRF token. Both are meaningless outside that run.
- The three people are fictional. Identity is only the git config (`user.email`), read on every request: **mudit** = Mudit Sarda, owner of `rules.md` and
  the ledger document; **priya** = Priya Nair, owner of `rules.md` and `security.md`; **arjun** = Arjun Mehta, owner of nothing.

## Pages as mudit (owner of `rules.md` and `paths-services-ledger.md`, not `security.md`)

| File | Screen | What it shows |
|---|---|---|
| `projects.html` | `/` Projects | The board: `payments-api` under Needs attention, `notifications-service` under All clear, `legacy-billing` under Turned off. Card chips and the "N rules in force · N remembered" line. |
| `new.html` | `/new` Add a project | The "Projects are repos" screen with its command steps. |
| `overview.html` | `/project` Overview, `payments-api` | Stat line (9 rules in force, 1 conflict, 2 possibly outdated, 3 proposals), then the three lanes: the http-retries conflict, the two outdated rules, the three proposals. |
| `rules.html` | `/context` Rules | The table of all 10 team rules by scope (platform, payments, repo), marks (outdated, conflict, path-only), and the conflict card. |
| `memory.html` | `/memory` Memory | The six remembered items in lanes by kind, including the challenged decision with its usage tally. |
| `start.html` | `/start` What agents are told | The exact start block an agent receives, no cut note. |
| `proposals.html` | `/proposals` Proposals | The three waiting proposals with decide blocks (as Mudit: one "Not an owner", the security one) and the one turned-down proposal with its reason. |
| `add.html` | `/add` Add a rule | The add form, identity line and the two command cards. |
| `connect.html` | `/connect` Connect | The four connect steps. |
| `search-ledger.html` | `/search?q=ledger` | Search results across team rules and memory for "ledger". |
| `disabled-project.html` | `/project` for `legacy-billing` | The "Reverie is turned off for this repo" screen. |

## Proposals as the other two people

| File | Identity | What it shows |
|---|---|---|
| `proposals-arjun.html` | arjun (owns nothing) | All three decide blocks read "Not an owner: the CLI would refuse". |
| `proposals-priya.html` | priya (owns `rules.md`, `security.md`) | Two decidable blocks (Arjun's `rules.md` and `security.md` proposals); the ledger proposal, her own, is in a document she does not own, so it reads "Not an owner". |

## Empty states, `notifications-service` (enabled, nothing in it), as mudit

| File | Screen | What it shows |
|---|---|---|
| `empty-overview.html` | `/project` | "Nothing needs your action". Rules in force is 2: the two personal rules apply in every repo. |
| `empty-rules.html` | `/context` | "No team context applies here". |
| `empty-memory.html` | `/memory` | "Nothing remembered for this repo yet". |
| `empty-proposals.html` | `/proposals` | "No proposals". |
| `empty-start.html` | `/start` | The start block with only the two personal rules. |

## The "cut by the start limit" state, as mudit

Same seed plus 35 extra repo-level rules remembered for `payments-api` ("Seed rule NN: module N must not run code at import time.").

| File | Screen | What it shows |
|---|---|---|
| `overview-capped.html` | `/project` | Stat line with 38 rules in force and the extra stat "6 Cut by the start limit". |
| `start-capped.html` | `/start` | The start block with 30 of the 36 repo rules (the 30-line cap) and the "(+6 more ...)" line, and under it "38 rules in force · 6 cut by the start limit". |

## Seed check (end of `docs/ui-inventory.md` section 8)

Run against these captures:
- Conflicts 1, Possibly outdated 2, Proposals 3, Turned down 1: match.
- Decide blocks: Arjun sees three "Not an owner"; Mudit one (security); Priya one (the ledger proposal): match.
- **Rules in force: 9, not "about 10 team + 2 personal + memory rules".** The 9 are 6 team rules, 2 personal and 1 remembered rule. The count differs from the plan in two ways:
  - Only 6 of the 10 team rules count: 2 are possibly outdated (rules 4 and 7) and 2 are the unresolved http-retries pair, which agents are told to ask about instead of applying.
  - Nothing is "hidden": the plan assumed the personal rule "Money is integer cents" duplicates team rule 1, but rule 1 is a longer sentence, so it does not match and both personal rules count.
