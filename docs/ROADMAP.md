# Reverie roadmap

Last reordered: 2026-11-04 (after P0). Reorder it whenever a test teaches us something.

## What "better with work" means (the yardstick for every item)

> Same model, same task difficulty: the agent needs less exploration, fewer
> user corrections, fewer failed attempts and fewer repeated mistakes, and the
> gap widens as work accumulates, while the context cost stays flat.

An agent's weights do not change, so improvement can only come from changing
what it sees when it acts. Five requirements:

1. **Capture from evidence**: user statements, diffs, tool outcomes, test
   results. Never the agent's own narration.
2. **Deliver at the moment of need**.
3. **Stay correct**: verification and decay.
4. **Bounded cost**: a fixed retrieval budget; bloat makes the agent worse.
5. **Closed loop**: record whether a retrieved item was used, ignored or
   contradicted, and adjust its confidence. Without this, Reverie accumulates
   but does not learn.

Product thesis: every user has the same models, so code converges. A
user-specific memory layer is what makes one user's agents write different
(their) code.

Measure with paired runs of a task sequence, memory on vs off: user
corrections, tool calls to the first correct edit, rule violations, the same
mistake made twice, hidden-test pass rate. The learning curve (task 10 vs
task 1) is the headline number.

## Design decisions (settled)

- Hooks where a harness has them (read AND write); MCP as the fallback and for
  on-demand `query`. Hooks are deterministic, MCP depends on the agent
  choosing to call.
- Transcript import (option B) first, harness hooks (option A) second.
  Raw transcripts stay where the harness keeps them; Reverie stores only a
  redacted derived log, cards and items.
- Secrets are redacted at write time and again at render time; no raw copy.
- Resume card: bounded (~1.5-2K tokens), one card per task (a chain of
  sessions linked by resume), rewritten not appended. Full log on disk,
  fetched on demand.
- Start block: always the rules; everything else as pointers fetched on
  demand. Size is configurable.
- Dedupe: keep only the cheap near-identical drop on write. No LLM judge on
  write. Collapse near-duplicates at delivery, merge in a periodic job, count
  repeats ("seen N times") as an importance signal. The reversal judge
  (user says "forget the old rule") stays.
- Staleness: an LLM reads the diff of the anchored files. Tests are optional
  extra evidence, off by default.
- Anchors are attached at capture time by searching the code for the
  identifiers a statement mentions.

## Done on `feat/continuity-spine`

Typed items; rules; spool and `resume`; claude capture hook (sync + detached
worker); stale worker; reversal supersession with undo; liveness filtering;
typed memory over MCP `summary`/`query`; capped start block; agent-about-memory
guard; `CLAUDE_CONFIG_DIR`; **resume picks the session on the current branch
and asks when ambiguous** (1911bd0).

## Next, in order

### P0: close the capture gap (DONE except two items waiting on the user / quota)
1. **Spike: hooks on Codex, Cursor, opencode.** DONE, table in
   `docs/SUPPORTED_HARNESSES.md`. opencode: yes (plugin, no trust step).
   Codex: hooks load but are `untrusted` and skipped silently until the user
   approves them. Cursor: `sessionStart` fires headless; prompt/stop untested
   (usage limit).
2. **Redaction** (write + render). DONE (1e55868). Found on a real run: the
   importer's own cursor file kept the open prompt raw; fixed.
3. **Transcript importers** (core + Claude/Codex/opencode/Cursor adapters).
   DONE (14032cc, 98c692c). Real run: codex headless -> claude resume worked.
   Found and fixed: the `/reverie` housekeeping marker matched any path
   containing it, so those exchanges were never captured.
4. **Hook integrations.** opencode DONE (02a974e: prompt log, session import,
   rules in the system prompt). **Codex** DONE in code (ffdc5db): hooks fire
   headless after approval. Trust is per user and per hook definition (Codex
   stores a `trusted_hash`); unapproved hooks are skipped silently. Decision
   taken: Reverie does NOT write the hash itself. The importer + MCP already
   cover Codex without hooks, the installer prints the one-time `/hooks` step,
   and `reverie status` should warn when hooks are installed but untrusted
   (see "Codex trust" below). **Cursor**: importer + MCP only; hooks are not
   shipped until someone with quota can verify prompt/stop/context injection.

Learned from the P0 runs, now on the list:
- Provenance (P1 item 5) is the most visible quality problem: the rules index
  now carries `agent_inferred` junk such as "tests were added but have not been
  run yet" and "the user pasted credentials, rotate them". Do it next.
- Worktrees of one repo share one prompt log and one memory by design; a
  session in another worktree shows up in `resume`. Fine, but `resume` should
  say which worktree a session ran in (small, add under item 10).
- A harness's own transcript keeps any secret the user typed; Reverie never
  copies it. Say so in the install text.

### P1: make what is stored trustworthy
5. **Provenance-based capture** DONE. Items record `meta.provenance`
   (`user` | `diff`); an agent-inferred item needs a quote from the user's words
   or the working-tree diff; the agent's own answer is never evidence. The
   regex guard is gone. Tool results are now a third source (item (e), below); still to add:
   diff-at-capture-time for imported exchanges.
6. **Ground at capture** DONE (b11f425). `reverie/anchors.py` checks proposed
   and mentioned files/symbols with `git grep`, drops invented ones, adds the
   defining files. Real run: a symbol-only rule gained its file, the stale
   worker then flagged it correctly after a teammate commit.
7. **Dedupe change** DONE (2547079). No judge on write; delivery collapse with
   "seen N times"; `reverie merge` (daily, lazy). Learned: embeddings cannot
   separate paraphrases from opposites (0.64-0.84 for both), so the periodic
   job asks one batched judge call per candidate cluster. Real run: 10
   delivered rule lines -> 4. Open: the extractor sometimes emits one compound
   rule; splitting it is a prompt change.
8. **Staleness**: diff-judge is primary; tests off by default.

### P1: make delivery bounded
9. **Start block** DONE: rules always, the rest as pointers; cap is
   `deliver.startup_chars` (config.yaml) or `REVERIE_STARTUP_CHARS`, clamped
   1500-9500, default 8000. A smaller cap drops index lines, never rules.
10. **Resume card** DONE (this commit). Bounded (~2K chars last prompt, 6K for the
    rest), buried constraints kept, per-prompt tool label, `--prompt N` for the whole
    text, secrets as a note. Cut-off sessions are announced by MCP `summary` (works with
    no shell/PATH and no hooks) and `summary resume:true` returns the card. Chains of
    sessions across tools are now linked (item (f)); open: an LLM rewrite of the card.

### P2: install experience
11. **Profile discovery** DONE: `reverie/profiles.py`, `reverie install
    --list-profiles`, `--profile KEY=PATH`; asks or refuses, never guesses. Also fixed:
    Codex and opencode ignored `CODEX_HOME` / `XDG_CONFIG_HOME`.
12. **Local-model guidance** DONE: install text and README say a local model is a
    weaker extractor (measured 4/13 vs 12/13) and what to do about it.

### P2: prove it learns
13. **Usage feedback loop** DONE (first slice): deliveries are logged, a batched
    judge labels used/ignored/irrelevant/contradicted/overridden per exchange, counts live
    in `meta.usage`, `reverie usage` reports, repeat contradictions mark an item
    `challenged`. Open: let usage RANK delivery (dormant items sink, proven ones rise);
    verify the judge on more than three cases.
14. **Learning-curve benchmark**: first slice DONE (pytest, 6 tasks, memory on vs off,
    claude, 1 run per arm). Round 1 was confounded (agent imitated the earlier function
    in the same file) and is reported as no evidence. Round 2 (new file per task, an
    arbitrary naming rule): memory-on 0 violations in 5 post-rule tasks, off 3 of 5; behaviour 6/6
    both; no drop in exploration; ~10% more cost per task. Next: 10+ tasks, 3 repeats, a second
    harness, rules that change (staleness), cost over time. Details in `P0_LOG.md`.

Full benchmark design (multi-harness, one user and two users, ON vs OFF, 12 tasks,
3 repeats, hidden checks first): `docs/BENCHMARK.md`. Run 1 done 2026-11-07: single user 0.04 vs 2.33 violations per task (memory on vs off), second user without shared rules 4.00 vs with a repo rules file 0.00; results and caveats in that file. Still open: opencode runs (provider credits and key), implicit corrections for a gradual curve, more repos.

Benchmark finding for the team layer: promotion path (a rule one user taught their agent
-> the shared team document, with an approver) is the missing piece; the shared file itself
already works. See `docs/BENCHMARK.md`.

### Codex trust (open decision for the user)
DONE (user decision, 2026-11-07). Codex runs a hook only once approved
(`trusted_hash`, per user and per hook definition). `reverie install` now
approves Reverie's OWN three hooks by default, through Codex's app-server:
`hooks/list` gives Codex's own `currentHash`, `config/batchWrite` stores it.
Reverie never computes a hash and never approves a hook it did not write
(`reverie/integrations/codex/trust.py`). `--no-trust-codex-hooks` leaves approval
to the user (`/hooks`). Verified headless: install -> `codex exec` -> hooks fire
-> prompt and answer in the log. `reverie status` now warns when hooks are installed but not approved
(read-only, 10 s timeout, DONE).
Note: any change to the hook command changes the hash, so re-run `reverie install`.

### Cursor without a subscription
Importer + MCP already work from the transcripts Cursor keeps. Hooks stay
unshipped until verified. Ship a `docs/` checklist (3 commands) that anyone with
quota can run to verify prompt/stop/context injection, and retry when the
account's quota resets.

### Team context layer (design, validate before building)
Idea: a team keeps shared context documents that load into every teammate's
agent; anyone proposes a change, authorised people approve (merge-request
style). Assessment:
- Real pain (senior vs junior AGENTS.md quality). But a repo AGENTS.md under git
  with PR review and CODEOWNERS already gives approval and history for one repo,
  so the product must beat that. Where git falls short: context that spans
  repos; contributors who are not engineers; layering and precedence (org >
  team > repo > personal, with conflicts shown); nobody knows whether agents
  follow it or whether it is still true; and one large file dumped into every
  prompt bloats context.
- Reverie's edge, so build THESE and not "a shared markdown file": split the doc
  into items with anchors and deliver rules always, the rest at the point of
  need (bounded); run the staleness check on the shared doc ("this paragraph
  contradicts `x.py` since commit Y"); record whether agents used or ignored an
  item (roadmap item 13); show precedence and conflicts.
- v1 without a server: documents in the repo (`.reverie/context/*.md`) with
  CODEOWNERS as approvers, imported as items. The paid tier adds hosted sync,
  cross-repo documents and an approver UI. Not in the single-person plan; the
  single-person version is personal, ordered, versioned context files.

Decisions (2026-11-07): the product-manager contributor stays in v1. Layering and
precedence are in: first a WORKING fixed structure (org > team > repo > personal,
conflicts shown, org rules can be non-overridable). Letting each company define its
own layers, and edit layers and their rules from the UI, comes AFTER the fixed
structure works. Whether to build a rules editor waits on whether people edit rules
by hand today (a product decision, later).

#### Team layer v1: design note (2026-11-09, build spec)

**Layers**, highest first: `org > team > repo > personal`. Fixed names, no
user-defined layers yet.

**Files** (plain markdown, no server, git is the history, CODEOWNERS the approvers):
- repo layer: `<repo>/.reverie/context/*.md` (and the existing `.reverie/rules.md`)
- org / team layers: a folder of the same kind of `.md` files, usually its own git
  repo checked out next to the code. Pointed at by `<repo>/.reverie/context.toml`
  (committed) or `~/.reverie/context.toml` (personal):
  `[sources]` `org = "../acme-context/org"`, `team = "../acme-context/payments"`.
  Relative paths resolve from the repo root; `~` works.
- personal layer: `~/.reverie/rules.md` plus the learned items (as today).
- Cross-repo: a document may start with a small header
  `applies-to: payments-api, billing` (repo folder or remote names; `*` or absent
  = every repo that points at its folder).

**Document format (what a product manager writes)**: ordinary markdown. A
section headed *Rules* / *Always* / *Never* / *Must* holds rules, one per
bullet; every other bullet is *guidance* (a pointer). A bullet ending in
`(locked)` cannot be overridden by a lower layer. Optional `{id: no-print}` tag
at the end of a bullet names it, so another layer can answer the same rule.
No code, no YAML, no front matter required; `reverie context add` writes the
bullet for you.

**Items**: each bullet becomes an item at read time (stable id = hash of
layer + file + text, so usage and staleness data stick to it). Read at delivery,
stdlib only; nothing is copied into the database, the files stay the source.

**Precedence and conflicts**: higher layer wins. A conflict is two rules at
different layers with the same `{id}`, or one a judge flags (`reverie context
check`, cached by content hash, never run in a hook). The lower rule is NOT
dropped: delivery lists it as "overridden by <layer> rule X (locked)" so the
agent and the human see both; a locked org rule beats everything and the
overriding attempt is shown as a violation. Never silent.

**Delivery** is unchanged in spirit: rules always (grouped by layer, bounded),
guidance as pointers (`reverie show <id>`), size capped by `deliver.startup_chars`.

**Staleness**: backticked paths/symbols in a bullet become anchors (checked
against the repo, as `anchors.py`); `reverie context check --stale` runs the
existing diff judge on commits touching them and flags the bullet ("this
paragraph contradicts `x.py` since commit Y"). Flags are kept in a sidecar in
`REVERIE_HOME`, not written into the shared document.

**Promotion**: `reverie context propose <rule-id>` writes a proposal file
`.reverie/context/proposals/<slug>.md` (what, why, who taught it, evidence)
that goes through an ordinary PR; CODEOWNERS on that folder approve; `reverie
context accept <proposal>` moves the bullet into the target document and
deletes the proposal.

**Not built now**: user-defined layers, UI editing, hosted sync.

#### Team layer v1, REVISION (2026-11-09): scopes and authority, not a fixed ladder

Three problems found in review of the note above. This revision REPLACES the
"fixed layer names" and "higher layer wins" parts; everything else stands.

**1. Roles: Reverie does not know any.** The note assumed "developers and PMs".
Real teams also have designers, legal, security, support, data, sales engineers.
Two separate questions, kept apart:
- *Contribute* is permissionless. Anyone, whatever their job, can write a bullet
  or run `reverie context add`. What they write is a PROPOSAL until it lands in a
  scope whose owners accept it. Reverie has no role list and never asks "are you
  a developer".
- *Authority* is the only permissioned part: who may accept, change or lock
  things in a given scope. v1 takes this from what the team already has (git:
  CODEOWNERS on the folder; identity = git identity). Role names, if a team wants
  them, are free labels in an optional `authority` file (`legal = @a, @b`); the
  code never branches on a role name. A hosted tier later adds a permission
  screen over the same model.
- Content stays two kinds only (rule, guidance) so the format never depends on
  who wrote it. An optional `owner:` line says whom to ask about a document.

**2. Many teams, many orgs in one repo: scopes, not a stack.** A document
declares WHERE it applies, and the only precedence is containment:
- Scope = an org, a team, a repo, a path inside a repo (`applies-to-paths:
  services/billing/**`, the same idea as CODEOWNERS), or a person. A session's
  active scopes = the ones its repo, path and user belong to. Team B's rules do
  not reach team A's people unless the work touches a path B owns.
- Nested scopes (a path inside a repo, a repo inside a team): the narrower one
  answers the wider one, unless the wider one is locked.
- Sibling scopes (team A vs team B, org X vs org Y) have NO order and neither
  wins. If their rules contradict on the same work, that is an UNRESOLVED
  conflict: both are shown, and the agent is told to ask the user instead of
  guessing. A human resolves it once, by writing a resolution (`{id}` answer)
  in the scope that contains both (the repo owner); it is then remembered.
- A lock binds only inside its author's own scope: an org lock covers that
  org's people and repos and cannot bind another org. Two locks that contradict
  in a shared repo = unresolved conflict, never a silent winner.
- The four named layers (org > team > repo > personal) become the DEFAULT
  PRESET of this model, not a special case, so user-defined layers later are
  just new named scopes with a parent. No rewrite then.

**3. Conflicts stay shown, and now say what to do.** Every conflict is listed
with both rules, whose they are, and a status: `overridden` (nested, a winner
exists), `locked` (a lock won), or `unresolved` (siblings: agent asks the user;
resolution is a recorded decision). Never dropped silently.

**Build order change**: refactor the core already committed (7e3c1df) to this
scope model, add path scoping, peer conflicts and proposals-by-anyone, THEN run
the lab test. The lab run must include: two teams in one repo, two orgs with a
conflicting lock, a path-scoped rule, a rule written by a non-developer role,
and a second user inheriting rules (each with a memory-off control).

**Follow-up to lab run 2 (the three gaps).** Run 2 found two open failures: a subtle
conflict nobody had flagged (codex picked a side silently in 3 of 3) and an unflagged
stale rule (codex re-added a removed helper in 2 of 3). Fixes, kept as separate
mechanisms: (1) DETECTION: the conflict judge runs when a document changes (after
`context add`/`accept`, in a detached session-start worker keyed by a content hash,
in the daily run); (2) HINT: a fixed line when more than one org/team source applies,
a nudge that does not detect anything; (3) the staleness pass runs in the same worker
on new commits. Never in a hook; a failing judge leaves delivery unchanged. Whether
this fixes the two cells is a measured question: both cells passed in the follow-up (n=3, see
docs/BENCHMARK.md), with the first-session race unmeasured.

**Promotion path (step 2, BUILT; approved with three changes below).** A rule one person taught their agent
becomes a PROPOSAL for the team document, reviewed by whoever owns it. Reuses what exists:
`proposals/` files, CODEOWNERS authority, `reverie context accept`. No new store, no model call.
- Command: `reverie context propose <rule-id | "text"> [--layer team|org|repo] [--scope S]
  [--paths G] [--why W]`. The source is one of the user's rules (a bullet in `rules.md`, or a
  stored memory item of kind rule by id). Nothing leaves the personal scope until the user runs
  it: no automatic suggestion in v1 (`propose --list` may rank candidates by usage data later).
- Permission is the same as `context add`: an owner of the target document writes directly
  (self-approving, like a commit by a maintainer); anyone else writes a proposal file. The
  approver is whoever CODEOWNERS names, nothing else. New: `reverie context reject <slug> --why`
  so an approver can decline with a reason (the proposal file is removed; git keeps the history).
- A proposal carries provenance: the user's own words verbatim, `from:` (personal rule or memory
  item id), date first stated, and, when usage data exists, how often agents followed or violated
  it. Text goes through the existing redaction; a rule that contains a secret is refused, not
  stripped quietly.
- After accept, the personal copy is not deleted: delivery suppresses a personal rule whose
  normalised text equals a team rule, so the agent never sees it twice. A rule already present in
  the target document is refused as a duplicate. Conflict detection runs after accept as it does
  for `add`.
- Approved changes: (1) a duplicate personal rule is hidden at DELIVERY time only, never deleted,
  and comes back when the team rule is removed, overridden or flagged outdated (tested);
  (2) `reject --why` keeps the reason in `proposals/rejected/` (git history, visible to the
  proposer, never delivered); `propose` refuses a rule that was rejected and prints the reason,
  `--again --why` is the only way to re-propose; (3) the lab has a fourth arm, "rejected": B must
  not get the rule and the reason must not appear in B's context.
- Out of scope: auto-promotion, voting, notifications, a UI (step 3 shows proposals read-only).
- Lab (fresh repo, memory-off control, checker written first, cap $15): user A's rule is seeded in A's
  personal rules; B works in a clone. Arms: before accept (B should NOT get it), after accept (B
  should follow it), rejected (B must not get it, reason absent), off. Tests also cover redaction, duplicate, reject, owner direct write.

**Open item: the first-session race (measure, do not build yet).** Detection runs in a detached
worker, so after a document or commit changes the agent's FIRST session may start before the
worker finishes: the UNRESOLVED or outdated block then appears only in the next session, and
until then only the hint helps. Measure how often the worker loses to the agent. If the window
is real, also run detection on `git pull` (a hook installed by `reverie install`, opt-in) and at
`reverie install`.

### Smaller items: progress
- (b) Codex untrusted-hooks warning in `reverie status`: DONE.
- (d) refresh installed instruction blocks: DONE. `reverie install --refresh-instructions` updates only text that is already installed and stale (the owned `~/.reverie/AGENTS.md`, the marker-delimited block in a harness AGENTS.md); `reverie status` warns; nothing else is touched or created. (`reverie install` itself already refreshed a stale block; what was missing was noticing, and a way to do only that.)
- (e) tool results as a provenance source: DONE for Claude Code's Stop hook (the live capture reads the real `tool_result` blocks of the last exchange, redacted and capped at 12k chars, head and tail). They can back FACTS (environment, a decision's reason, a negative result) and never a rule or preference (tool output can carry text written by anyone). `REVERIE_PROVENANCE_TOOLS=0` turns it off. Other harnesses and the importers do not pass tool results yet.
- (f) session chains across tools: DONE. A session whose first prompt asks to resume continues the newest earlier session that had ended; `reverie resume` shows the whole chain in order with each tool, and the picker no longer asks which of two linked sessions. A heuristic on order and wording; no chain is invented without a resume prompt.
- (c) compound rules: real repro found (above), prompt changed, cost measured: DONE.

### Usage-ranked delivery (item (a)): design, NOT BUILT, waiting for approval
It changes what agents are shown, so nothing is built until this is approved.

**What it would change.** Today every capped list is ordered by creation time. Caps bite in four
places: the rule lines beyond `MAX_WIDE_LINES`/`MAX_RULE_LINES`, the headline index of everything
else, the "possibly outdated" group, and `query` results. Usage ranking would order ONLY inside
those lists, so what survives a cap is what agents have actually used.

**What it must never do.** (1) Hide or drop an item: a sunk item moves down, it stays reachable
(the index line, `reverie show`, `reverie usage`). (2) Touch the user's own standing rules: rules
the user said (`user_said`, `rules.md`, team documents) are always delivered and never reordered
below an agent-inferred item. (3) Run a model: the hook only reads counts already stored in
`meta.usage`. (4) Replace the "challenged" and "possibly outdated" flags, which keep working.

**The score** (per item, from `meta.usage` = counts of USED, IGNORED, IRRELEVANT, CONTRADICTED,
OVERRIDDEN; n = deliveries judged). An item with fewer than 8 judged deliveries is "unproven" and
keeps its creation order, ranked ABOVE sunk items and below proven ones (no one is penalised on
thin data). Proven: USED/n >= 0.5 rises. Sunk: n >= 8 and USED = 0 (IRRELEVANT counts as "not
about this task", so it only counts when it dominates: IRRELEVANT+IGNORED >= 80% of n) sinks. A
sunk item re-enters the order after 30 days without a delivery (decay), and any item shown only in
the index still gets judged when an exchange touches its files.

**The feedback-loop risk, stated up front.** Items shown more get more chances to be USED, so
ranking by use rewards what was already shown. The unproven band, the 30-day decay and the
rule above (nothing disappears) are the counter; the before/after below is built to expose it.

**Gate 1: verify the judge first (the open "more than three cases").** A held-out set of 40
exchanges with labels written by me from the diff BEFORE the judge sees them, spread over all five
labels. Pass bar: >= 80% agreement on USED vs not-USED, no label with 0 recall, confusion matrix
published. If it fails, nothing is built on it, and the judge prompt is the work.

**Gate 2: before/after, delivery only (no agent spend).** Seed a repo with 60 items of which 8 are
the ones the next tasks need; fill `meta.usage` by REPLAYING a recorded history (the 8 used,
the rest ignored) at three history lengths (5, 15, 40 exchanges). Metric: recall@cap, the share of
the 8 that survive the startup cap, old order vs usage order, at caps of 1500, 3000 and 6000
chars. Success: usage order raises recall@cap at the small caps and never lowers the share of the
user's standing rules delivered (must stay 100%).

**Gate 3: before/after with agents, only if 1 and 2 pass.** A fresh repo, a hidden checker written
first and validated on reference solutions, tasks whose correct answer depends on an item the OLD
order cuts at a small cap but the usage order keeps. Arms: old order, usage order, Reverie off.
Claude and Codex, 3 repeats, a fresh clone, folder and `REVERIE_HOME` per arm, key withheld,
provider failures counted apart, spend cap proposed at $15. Report violations per task with n and
spread; a null result (no gain) is reported as a null.

**Rollout.** Behind `deliver.usage_ranking` (config, default OFF) until Gate 3 passes; the UI and
`reverie usage` show each item's rank reason ("proven: used 6 of 9", "unproven: 3 deliveries",
"sunk: ignored 12 of 12") so a person can see why an item moved.

**Decisions for you:** (1) agree the thresholds (8 deliveries, 0.5 used, 80% not-relevant, 30 days),
(2) agree the order of gates, (3) agree the $15 cap for Gate 3.

### Local UI (validate first)
A command, not a daemon: `reverie ui` serves a localhost page until Ctrl-C
(stdlib `http.server`, no new dependency). First slice, read-only: repos,
rules, items with source/provenance/stale flags, history. Editing, version
history and ordering of several context files come after the read-only page
shows people use it. Usefulness test before building the editor: do users edit
rules by hand (`reverie rules` usage), and does a read-only page make them
trust or fix memory. Version history can reuse git for repo-resident files.

**Redesign (approved after review): the page is now a team workspace (projects board, per-project tabs for Overview, Rules, Memory, What agents are told, Proposals, Connect; search; guided add/connect screens that show the command). Still read-only; projects open only by opaque id. Real add/connect buttons (a page that writes) are NOT built: a design note on CSRF, Host checks and what may be written comes first.**

### UI write actions: design note (2026-11-10), NOT BUILT, waiting for approval
Today the page is read-only (GET/HEAD only; everything else 405). This note says which writes become
real and how they are made safe. Nothing below is built until you approve it.

**Which actions (candidates, with the CLI function each must call)**
| Action | CLI function | Notes |
|---|---|---|
| Add a team rule or proposal | `context.add` (layer repo/team/org) | direct edit if no CODEOWNERS covers the document or the writer owns it, otherwise a proposal file; the UI shows which happened |
| Propose a personal rule to the team | `context_propose.propose` | needs a rule id from memory; larger form, later |
| Add a personal rule | `rules.add(level="user")` | writes under `REVERIE_HOME`, no authority question |
| Turn a repo on/off | `repos.set_repo_state` (what `reverie enable/disable` call) | writes the brain; the only write outside the repo files |
| Accept / reject a proposal | `context.accept` / `context.reject(why)` | owner only; reject needs a reason |

**Recommended slice one (smallest useful set): two actions.**
1. **Add a rule or guidance to the team context** (`context.add`) from the existing `/add` screen. It covers
   both "add a team rule" and "add a proposal", because the CLI function decides which, from CODEOWNERS and
   git identity.
2. **Accept / reject a proposal** (`context.accept`, `context.reject`) from the Proposals tab. This closes the
   loop: a non-owner's proposal can be settled where it is read.

Deferred: personal rules (low value, already one command), repo on/off (writes the brain, a different
risk class, one command), propose-from-memory (needs a picker), `/new` and `/connect` (they install and
register things on disk; they stay command screens). If you want one more after slice one, repo on/off
is the cheapest.

**Authority: one write path, the CLI's.** The UI imports and calls the same functions and catches the same
`ValueError` the CLI prints. It has no write code of its own: no path building, no file writes, no
ownership check of its own. `context.add` writes a proposal when the git identity is not an owner;
`accept`/`reject` raise "only the owners of X can accept" and the UI shows that text as a refusal.
Identity is the git config of the repo the server was started in, exactly as for the CLI, so the page
grants nothing the person could not do in their terminal. The server is the person's own process on their
own machine; the threat is other web pages and other local processes, not other users.
One gap found while reading the code: only `context_propose.propose` refuses secrets today;
`context.add` and `rules.add` accept them. The UI will refuse in its own form handler with the same
`redact.contains_secret` check, and (proposed, small, separate commit) the same check moves into
`context.add` so the CLI refuses too and the two cannot drift. Needs your yes, because it changes CLI behaviour.

**Threat model and controls**
- **CSRF:** a random 256-bit token (`secrets.token_urlsafe`) made once per process, put in a hidden field
  of every form, compared with `hmac.compare_digest` on every POST. Missing or wrong: 403, nothing written.
  No cookie, so there is no ambient credential for a foreign page to ride.
- **Origin:** a POST must carry `Origin` equal to `http://<bound host:port>` (or `Sec-Fetch-Site: same-origin`
  when `Origin` is absent); `Sec-Fetch-Site: cross-site` or `same-site` is refused outright. No Origin and no
  Sec-Fetch-Site: refused (browsers always send one on form posts; a non-browser caller must send the right one).
- **DNS rebinding:** the existing Host allow-list check runs first on every method, POST included.
- **No cross-origin form posts:** CSP gains nothing new except what exists: `form-action 'self'`, plus
  `frame-ancestors 'none'` and `X-Frame-Options: DENY` (clickjacking, new with real buttons).
- **POST only for writes; GET has no side effects.** Writes live at fixed paths (`/do/add`,
  `/do/accept`, `/do/reject`). A GET to those paths is a 405 or a plain redirect to the form; every GET page
  stays pure. A test snapshots the filesystem around a GET crawl including those paths.
- **Content-Type and size:** only `application/x-www-form-urlencoded`; body capped (16 KB, 413 above);
  `Content-Length` required; unknown fields ignored; every field length-capped.
- **No JavaScript.** Plain forms and a redirect are enough; scripts would force loosening the CSP
  (`script-src`) for no gain.
- **Projects:** the target project is the opaque id already used for reading, re-resolved on every POST
  against the known list; never a path from the form. Proposals are named by slug and re-looked-up by
  `context.proposals` inside `accept/reject`, so a slug cannot escape the proposals folder (the CLI
  function only matches existing slugs by prefix).
- **Hostile text:** form values go through the same escape and redaction at render as everything else; the
  stored text is what the CLI would store (`context.add` normalises whitespace; `_bullet` keeps it on one line).

**Secrets:** a submission containing a secret is refused with a fixed message ("looks like it contains a
secret; nothing was saved. Remove the value and try again"). The refusal page re-shows the form with the
text field EMPTY, never the submitted text, so a secret is not echoed back, not logged (the server logs
nothing) and not kept in a redirect URL. Non-secret errors may keep the text so the person need not retype.

**Failure handling and what the page shows (post/redirect/get)**
Every write ends in `303 See Other` to a GET page; the browser never holds a POST that a refresh could
repeat. The outcome travels as a one-shot flash, not in the URL: the server keeps a single in-memory
message keyed by a random id in the redirect query (`?done=<id>`), shown once and then forgotten. The
message states the truth: "Added to rules.md" / "Written as a proposal; the owners (...) review it" /
"Accepted into ..." / "Rejected; the reason is kept in ..." / "Refused: only the owners of rules.md can accept: ...".
Refusals redirect too (to the same form, with the flash), so there is one code path. An exception inside
a CLI function shows "That did not work; the CLI still does", and nothing partial remains because the CLI
writes through temp file plus `os.replace`.
Running the judge: the CLI runs `_context_detect` after add/accept (a best-effort judge call in a command). The UI
does NOT call a model in the request: conflict detection is left to the existing detached session-start
worker and the daily job, which already pick up a changed document by its content hash.

**Tests (split of the existing snapshot test)**
- Read-only run: crawl every page and every write path with GET/HEAD; `REVERIE_HOME` and the repo are
  byte-identical afterwards (the current test, now also covering the write URLs).
- Write run: with a valid token and Origin, one POST changes exactly the intended file (and nothing else in
  `REVERIE_HOME` or the repo, apart from the documented proposal file or its move).
- Refusals, each asserting no file changed: forged Origin; `Sec-Fetch-Site: cross-site`; missing token;
  wrong token; wrong Host; GET on a write path; wrong Content-Type; oversize body; unknown project id;
  secret in text (and the response body does not contain it); hostile text stored and then rendered escaped;
  a non-owner accepting or rejecting (refused with the CLI's message, proposal still there); owner accepting
  (proposal gone, rule in the document); reject without a reason; refresh after the redirect does not repeat the write.
- Parity: for each action, the UI result equals the CLI result on an identical fixture.
- ruff, mypy and the full suite stay green; docs/CLI.md and README updated with the build.

**Decisions for you:** (1) slice one = add-to-team-context plus accept/reject, or a different set;
(2) OK to move the secret refusal into `context.add` so the CLI refuses too; (3) OK that the UI never
runs the judge (it is left to the worker); (4) the Origin rule above (refuse when both Origin and
Sec-Fetch-Site are missing).

**`reverie ui`, read-only slice: BUILT (design approved with four changes: plain-language home with action items first, no telemetry, hostile/large-data tests, start page equals the CLI text).** Cut from the design: the hooks-installed column on Repos (`reverie status` has it). A command, not a daemon. Editing,
user-defined layers and version history stay out; this page only shows what is already stored.
- Process: `reverie ui [--repo PATH] [--port N] [--no-open]` runs `http.server` in the foreground on
  127.0.0.1 (random free port unless `--port`), prints the URL, opens the browser unless `--no-open`,
  stops on Ctrl-C. Stdlib only, no new dependency, no external fonts or scripts (works offline);
  pages are server-rendered HTML with inline CSS and no JavaScript, so there is nothing to attack
  or to break.
- Pages (each a plain GET, all built from functions that already exist): **Start block** (the exact
  text an agent gets at session start for this repo, with its size and the disabled state);
  **Team context** (scopes and documents, rules with locked, path-scoped and "hidden duplicate"
  marks, conflicts with status, owners and the possibly-outdated flags, the contradiction hint);
  **Proposals** (open ones with provenance and `why`, rejected ones with reason, who and when);
  **Memory** (items by kind with source, status, grounding, usage counts, stale and challenged
  flags; agent-inferred vs user-said); **Repos** (enabled or disabled, hooks installed). Every row
  shows the command that changes it (`reverie context accept ...`), because editing is not here.
- Read-only is enforced, not promised: only GET and HEAD answer (everything else is 405); no handler
  writes a file or opens the brain read-write (memory is opened `readonly`); a test snapshots
  `REVERIE_HOME` and the repo before and after a crawl of every page and requires them identical.
- Safety: bind 127.0.0.1 only; reject a Host header that is not the bound address (DNS rebinding);
  escape every value (documents are written by other people); a strict Content-Security-Policy
  (`default-src 'none'; style-src 'unsafe-inline'`); text passes through the existing redaction
  before display; a disabled repo shows only "disabled" and no content, as delivery does.
- Tests: serve on port 0 in a thread; every page returns 200 and contains seeded data; non-GET is
  405; wrong Host is 403; a hostile rule text (`<script>`) is escaped; the no-writes snapshot test;
  a disabled repo leaks nothing; CLI docs/README tests cover the new flags and command.
- Validation before more: this is a read-only look, so the lab is not an agent run. Instead a
  manual walkthrough in a browser on the run 3 repo (screenshots of each page) and, if you use it,
  whether it changes what you fix by hand. No agent spend.

### Later (accepted as deferred)
- **Cursor hooks**: parked until there is quota to verify prompt/stop/context
  injection (checklist in `docs/SUPPORTED_HARNESSES.md`). Importer + MCP cover Cursor now.
- **Compound rules**: REPRODUCED and changed (see docs/BENCHMARK.md, "Compound rules"): 6 of 30
  real extractor runs on the scrapy D1 messages gave a compound item; a prompt sentence plus an
  example brought that to 0 of 30, at the cost of ~3 over-split single rules in 24 runs. A
  deterministic splitter at delivery stays the fallback if that cost matters.
Team layer, multi-agent board, cross-machine sync (the paid cloud tier),
UI, git-history mining. Retention/pruning of the derived log.

## Standing rules for this roadmap
- Every item ships with unit tests AND a run on a real repo as a developer
  would use it.
- Nothing is pushed or PR'd without asking.
