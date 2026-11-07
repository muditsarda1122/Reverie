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


## Results, run 1 (2026-11-07; poetry, claude + codex + opencode)

Hidden checks written and validated against reference solutions before any agent ran.
12 tiny tasks (one new module each), 4 conventions stated once by the "user"
(task 1: name prefix, keyword-only params, error prefix; task 3: docstring last line),
one of them CHANGED at task 9. Memory on vs off, every arm on its own clone.
Clean data: single user, 5 repeats per arm (repeat 1 discarded, see below).

| | memory ON | memory OFF |
|---|---|---|
| valid task-runs | 49 | 43 |
| convention violations per task (max 4) | **0.04** (2 in 49) | **2.33** (100 in 43) |
| tasks with zero violations | 47 / 49 | 5 / 43 |
| behaviour correct (hidden tests) | 49 / 49 | 43 / 43 |
| after the rule change (tasks 10-12): new error prefix used | 12 / 12 | 5 / 10 |
| claude tools per task / before first edit / cost | 4.5 / 3.0 / $0.099 | 3.4 / 1.8 / $0.090 |

By harness (tasks 2+): claude 0.07 vs 2.80, codex 0.05 vs 2.60, opencode 0.00 vs 2.00 (opencode n=9 vs 3, thin).
Cut-off and resume in another harness (task 5 codex->claude, task 8 opencode->codex):
memory ON, nothing existed on disk at the cut and the resumed agent finished correctly
(module + tests) in 8 of 8 runs; memory OFF, 0 of 5 (task 8). Caveat: kill timing differed
between arms, so this is a handoff signal, not a controlled comparison.

Second user (separate clone, own memory, rules never stated to them), claude, 2 repeats x 3 tasks:
nothing known: 4.00 violations per task (all four rules broken every time);
the team's rules in a repo `.reverie/rules.md`: 0.00. Behaviour correct in all.

### What it does and does not show
- Memory removes forgotten conventions across sessions AND harnesses. Without it, an agent
  never learns them (OFF stays flat at 2-3 violations per task for all 12 tasks).
- It is a step (task 1 -> task 2), not a gradual curve: the rules were stated explicitly once,
  so this tests recall and delivery, not judgement. A gradual curve needs implicit corrections.
- Memory cost more exploration (reads the memory first), ~10% more cost per claude task. No saving.
- Tasks are tiny and the conventions arbitrary; this is one repo and one model per harness.
  "In my tests", not proof.

### Problems the run found (all in the lab log)
1. Contamination, caught before reporting: worktrees of one repo share a prompt log by
   design, so the OFF arm's sessions leaked into the ON arm (rule change seen 4 tasks early).
   Fixed with one clone per arm. Then a reused folder name imported an earlier run's
   transcripts (30-day window): repeat 1 is discarded.
2. An agent read the hidden checker from the parent folder: moved out of reach, `--dir` pinned.
3. OpenRouter credits ran out mid-run: every opencode task of repeats 4-6 failed in BOTH arms
   (and the OpenCode key is expired), so opencode evidence is thin. Provider-failed runs are
   excluded and counted, never scored as violations.

### What it says about the team layer
- A shared rules file in the repo already gives a second user the first user's result (0.00 vs 4.00).
  The missing piece is promotion: nothing moves a rule one user taught their agent into the
  team document. Build that, with an approver, before a hosted layer.
- Delivery worked equally in all three harnesses, so a team layer can be harness-agnostic.
- Memory scope is path/worktree based (see 1): a team layer needs explicit scope (repo, team, org),
  not the folder an agent happens to start in.
