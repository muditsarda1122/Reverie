# Reverie roadmap

Last reordered: 2026-11-04. Reorder it whenever a test teaches us something.

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

### P0: close the capture gap (most serious)
1. **Spike: do Codex, Cursor and opencode hooks give a good result?** One
   short experiment per harness: install a prompt/stop hook, check it fires
   in interactive AND headless runs, check latency and what the payload
   holds. Includes how bad Codex's trust-approval step is for a new user.
   Output: a table in `docs/SUPPORTED_HARNESSES.md`. Decides item 3.
2. **Transcript importers** (harness-agnostic core + one adapter per
   harness: Claude, Codex, opencode, Cursor). Run lazily at `summary` /
   `resume` and on demand. Covers headless, scripted and killed sessions.
   Redacts before storing; keeps a cursor (file + offset) so nothing is read
   twice.
3. **Hook integrations** for harnesses where item 1 says yes.
4. **Redaction** (write + render): key formats, `Authorization` headers,
   `KEY=` assignments, `.env`-style blocks, high-entropy tokens. A redacted
   value becomes `[REDACTED:<kind>]`; a `KEY=` name is kept so the agent can
   tell the user which variable is needed. Do this BEFORE item 2 ships.

### P1: make what is stored trustworthy
5. **Provenance-based capture**: every item records its source (user said /
   diff / tool result / agent inference). Agent narration is never a source.
   Replaces the regex guard.
6. **Ground at capture**: attach file/symbol anchors by searching the code
   for the statement's identifiers. Fixes the stale-worker misses that had no
   anchor.
7. **Dedupe change**: remove the write-path LLM judge; add delivery-time
   collapse, a periodic merge job and the "seen N times" counter.
8. **Staleness**: diff-judge is primary; tests off by default.

### P1: make delivery bounded
9. **Start block**: rules always, the rest as pointers; configurable cap
   (`deliver.startup_chars`).
10. **Resume card**: per-task rewritten card with a fixed budget; last
    unhandled prompt trimmed with a pointer; card built lazily at `resume`
    when the session was killed. Redacted secrets appear as a note, not a
    value. The agent asks the user only if it actually needs one, and
    prefers an env var.

### P2: install experience
11. **Profile discovery**: find every Claude/Codex/OpenCode/Cursor profile or
    config dir (`CLAUDE_CONFIG_DIR`, `~/.claude*`, `CODEX_HOME`, XDG), list
    them, ask the user where to install. Never guess.
12. Local-model guidance in the install text: use a coding-capable model;
    the offline default is not good enough for capture.

### P2: prove it learns
13. **Usage feedback loop**: log item delivered, then whether the agent used,
    ignored or contradicted it; feed confidence.
14. **Learning-curve benchmark**: a sequence of N realistic tasks on a fresh
    repo, memory on vs off, the metrics above. Repeat on a new repo each time
    (httpx was the first). Runs after every P0/P1 item that changes behaviour.

### Later (accepted as deferred)
Team layer, multi-agent board, cross-machine sync (the paid cloud tier),
UI, git-history mining. Retention/pruning of the derived log.

## Standing rules for this roadmap
- Every item ships with unit tests AND a run on a real repo as a developer
  would use it.
- Nothing is pushed or PR'd without asking.
