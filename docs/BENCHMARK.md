# Does Reverie make work better? The benchmark design

Question: with several agents on different harnesses, one user or a team, does
the work get better over time WITH Reverie than WITHOUT it?

## What "better" means here (measured, not felt)

An agent's weights never change. "Learning" means: what it is shown when it acts
changes, so the same model needs less steering. Per task we record:

| metric | how it is scored |
|---|---|
| rule violations | hidden check per stated rule (grep/AST), not the agent's own report |
| user corrections | scripted: a correction turn is only sent when the hidden check fails; count them |
| tool calls and tokens to the first correct edit | from the harness transcript |
| repeated mistakes | same hidden check failing on a later task |
| hidden-test pass rate | tests the agent never sees |
| handoff cost | prompts needed to resume a cut-off task in another harness |
| context cost | tokens Reverie injected per task (must stay flat as memory grows) |

Headline number: task 10 vs task 1 (and the slope), memory ON vs OFF.

## Design

- **Repo**: one new, bigger repo cloned per run (never a repo used before).
- **Task sequence**: 12 tasks, each in a NEW file (so an agent cannot copy a
  neighbouring function; this confounded round 1). Rules are introduced at
  tasks 1, 3 and 6 by "the user", stated once, in natural language, and are
  tempted later. At task 8 one rule CHANGES (tests staleness and reversal).
- **Users**: scenario A, one user, three harnesses taking turns (claude, codex,
  opencode), including two cut-offs resumed in another harness. Scenario B, two
  users on two clones with a shared repo and separate memory homes: user 2 never
  states the rules user 1 did (tests the team gap honestly: today there is no
  shared layer, so the expected result is NO benefit across users, and that is
  the baseline the team layer must beat).
- **Arms**: Reverie ON, Reverie OFF (control, same prompts), 3 repeats each.
  ON/OFF are paired on the same task order.
- **Hidden checks written BEFORE any run**, committed with the task list.
- **Cost**: roughly 12 tasks x 2 arms x 3 repeats x 2 scenarios = 144 agent runs
  (plus handoffs). Estimated at several hundred dollars of model usage on API
  keys; opencode runs use the deepseek route. Run in stages: 1 repeat of scenario
  A first, then widen only if the signal is there.

## Reporting rules

- Say "in my tests", give n, give the spread, never "proves".
- A null or negative result is reported as such. Round 1 of the pytest slice was
  confounded and is reported as no evidence.
- Everything is logged in the lab (`~/reverie-lab/P0_LOG.md`); scripts live in
  `~/reverie-lab/big/` and are reusable.
