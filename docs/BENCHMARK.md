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
between arms: memory OFF task 5 had the module on disk at the cut and was finished 5 of 5 from git state alone,
so the 0 of 5 is only for the case where nothing existed yet. A handoff signal, not a controlled comparison.

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


## Results, run 2: the team layer (2026-11-09; click, claude + codex)

What was tested: scoped team context (`reverie context`, commit d2381cf), delivered at session
start and by the edit hook. Early results, in my tests, small n. Everything is on one repo
(pallets/click, a fresh clone per cell, never used in an earlier run), one model per harness,
tiny single-function tasks, arbitrary conventions that exist ONLY in the team documents and
never in a prompt. Each cell had its own clone, folder name and `REVERIE_HOME`; the real
`~/.reverie` was not used. Memory extraction was off for every agent run (the model key was
withheld from the agents' environment), so only the team layer can explain a result, never an
item learned from an earlier task. The control arm ("off") has the same files and prompts with
Reverie disabled for the repo. Hidden checker written and validated against reference solutions
before any agent ran (changes made afterwards are listed under caveats). 138 agent runs, 3
repeats per cell, 0 provider failures (none had to be excluded). Claude spend $5.79; codex
(subscription) 1.57M tokens.

Violations are convention violations per task (lower is better); "zero" = tasks with none.

| case | what the hidden check scores | Reverie on | control (off) |
|---|---|---|---|
| 1. second user inherits org + team + repo rules (3 rules, 2 tasks per cell) | prefix, keyword-only, error text | claude 0.00 (6/6 tasks clean), codex 0.00 (5/5 scored; 1 run left no file) | claude 3.00, codex 2.50 (0 clean of 12) |
| 2. person belongs to ONE of two teams (alpha, beta); own tag, other team's tag absent | docstring tag | claude 0 of 6 violations, codex 1 of 6 (a beta run ignored the delivered rule) | 12 of 12 violated |
| 4. rule scoped to `src/click/billing/**` (one task under it, one outside) | applied under the path; not leaked outside | 0 of 12 violations (no leak either) | 6 of 12 (the under-path task, every time) |
| 5. rule written by a non-developer through `reverie context add` (proposal, owner `accept`, delivered) | docstring tag | 0 of 6 | 6 of 6 |

Read every row with these limits: one repo (pallets/click), tiny single-function tasks,
conventions that cannot be guessed (so the effect is large by construction), memory extraction
off, n = 3 repeats per cell. The control arm kept the rule files on disk, and in two control
runs Claude opened `.reverie/context/` itself (neither changed the outcome). One codex run (case
1) reported a file that does not exist; it is unscored and unexplained. The case 2, 3(b) and
3(c) rows above are complete in the committed file; a copy that showed cut-off cells was a
paste problem, not the document.

Case 5 tests the contribution path only (a non-owner's `add` became a proposal, which was not in
force until an owner ran `accept`; the proposal-written and not-in-force checks held in 6/6
setups). The promotion path (a user's learned rule into a proposal) is not built and is not
tested here.

Case 3, two orgs with conflicting locks (`xo_` vs `yo_` function-name prefix). Pass = no
function written with either prefix AND the reply flags the conflict (a headless agent cannot ask
interactively, so "asked" is judged from its final reply). Control runs only show what an agent
does with no rules (it writes a function), so the control column is not a meaningful comparison.

| variant | claude on | codex on |
|---|---|---|
| (a) detected: both rules share an `{id}` -> UNRESOLVED block, "ask the user" | 3/3 asked, wrote nothing | 3/3 asked, wrote nothing |
| (b) undetected: different wording, no shared id, no `check` run | 3/3 asked | 3/3 asked |
| (c) undetected AND subtle (added after wave 1; X "raise ValueError on bad input", Y "never raise, return None") | 3/3 asked | **0/3**: wrote a function that picked a side every time (raise x2, None x1); it mentioned the conflict in 1 of 3 |

Finding about detection: (b) did not fail, but only because the two rules contradicted each
other blatantly and were printed next to each other. (c) is the real test and it failed for
codex in 3 of 3. Unresolved/locked conflicts are handled well once Reverie has DETECTED them
(a); an undetected, non-obvious contradiction is left to the model, and one of the two harnesses
resolves it silently. Nothing was fixed mid-run. Proposed fix, for after this run: run the
conflict judge when a document changes (at `reverie context add`/`accept`, in a detached
session-start worker keyed by document content hash, and in the daily merge job), never inside a
hook, so the UNRESOLVED block exists before the agent starts. Also worth adding a fixed line in
the start block whenever more than one org/team source applies ("if two rules above seem to
contradict, ask before acting").

Case 6, a rule goes stale (rule: use `legacy_hash` from `_hashing.py`; a later commit removed it
and added `stable_hash`; `reverie context check` flagged the rule as outdated in 6/6 flagged
setups, so the judge is reliable on this one clear case):

| arm | claude | codex |
|---|---|---|
| on, flagged ("possibly outdated" group) | 0/3 used `legacy_hash` | 0/3 |
| on, NOT flagged (same rule listed as always-apply) | 0/3 (read the code, used `stable_hash`) | 2/3 re-added `legacy_hash` to the shared `_hashing.py` to satisfy the rule; 1/3 stopped and asked |
| off | 0/3 | 0/2 scored (1 run left no file) |

So an unflagged stale rule misled codex in 2 of 3 and made it edit shared code to comply;
claude checked the code first. The flag removed the effect in 6/6. n=3 per cell.

Cost and exploration (cases 1, 2, 4, 5, 6 pooled): claude $0.097 per task on vs $0.077 off, 3.2
vs 1.7 tool calls; codex 23.2k vs 19.9k tokens per task. The start block was at most 407
characters in these runs (a handful of rules, so this says nothing about a large team document).

### Caveats (all real)
- One repo, tiny tasks, arbitrary conventions, 3 repeats, two harnesses. The effect sizes are
  large because the conventions cannot be guessed; this shows delivery and following, not
  judgement. "In my tests", early results, no claim beyond that.
- The control also had the rule files on disk. In 2 of 3 `c6 unflagged off` claude runs the
  agent opened `.reverie/context/` itself (the only control reads, 2 of 66 control runs); neither
  changed the outcome (no violation either way).
- Checker changes after seeing wave 1, all stated here: (1) case 6 behaviour check could not
  import a module that imports a sibling (relative import); fixed by loading it as a package
  module. (2) a function with a private name (`_format_percent`) was ignored as "no function";
  now scored like any other (this only ever changed a case-5 run from "not scored" to "clean").
  Violation rules did not change. Case 3c was added, with its check written before it ran.
- Not-scored runs (no file written, counted as "no deliverable", not as a violation): case 1
  codex x1, case 6 codex x2 (one asked, one with no file). One codex case-1 run reported "Added
  `_c1a.py`" with no such file on disk (unexplained; not a provider failure).
- Violations in the control are guaranteed for case 3 (no rule is shown); read only the "on" column.
- Cases 1, 2, 4, 5 are three repeats of two or three tasks per cell, so intervals are wide.
  Case 2's single miss (codex, beta) is the only on-arm convention violation outside case 3c and 6.

### Problems found on the way (fixed unless stated)
1. `reverie context add --why/--paths/--scope` failed: the flags had been registered on the
   `stale` parser (found by the lab driver's dry run, before any agent ran; the docs test did
   not catch it). Fixed with a regression test (d2381cf).
2. Path-scoped team rules were delivered by the edit hook even when Reverie was disabled for the
   repo, which would have leaked into every control arm (found while preparing the control; fixed
   with a regression test, 91b2d69).
3. Open: undetected non-obvious conflicts (case 3c) and an unflagged stale rule (case 6) both
   depend on a check the user has to run. Proposed fix above.
