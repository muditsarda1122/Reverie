# Smoke test — run this yourself in each real harness

A step-by-step checklist for verifying Reverie in a real harness (Claude
Code, Codex CLI, Cursor, OpenCode, Gemini CLI). Every step gives the exact
action and what you should see. Run it against a scratch repo you don't mind
getting a `.reverie` entry in your global brain about — nothing is written
inside the repo itself.

Run the same checklist per harness. Time: ~15 minutes each.

---

## 1. Fresh install

| Step | Action | Expected |
|---|---|---|
| 1.1 | `pip install reverie` (or `pip install -e .` in a checkout) | Installs cleanly; `reverie --help` lists the subcommands |
| 1.2 | `reverie install` | Detects your harnesses (✓/✗ table), asks which to configure, runs the provider step (or offers Ollama), then prints **"Reverie is ready!"** with a per-harness summary — every written file listed with a `Next:` line |
| 1.3 | Inspect what was written | The harness config contains the `reverie` MCP server entry (e.g. `~/.claude.json` has `mcpServers.reverie`; `~/.codex/config.toml` has `[mcp_servers.reverie]`; `~/.config/opencode/opencode.json` has `mcp.reverie`; `~/.cursor/mcp.json` has `mcpServers.reverie`; `~/.gemini/settings.json` has `mcpServers.reverie`) |
| 1.4 | Check `~/.reverie/` | Contains `reverie.db`, `config.yaml`, `AGENTS.md` — no `commands/` directory (commands live per harness now) |
| 1.5 | Check the harness command directory | `~/.claude/commands/reverie-review.md` + `reverie-status.md`, or `~/.codex/prompts/…`, or `~/.config/opencode/commands/…`, or `~/.cursor/commands/…`, or `~/.gemini/commands/*.toml` |
| 1.6 | Start the harness fresh (restart it so the MCP server loads) | The harness's MCP log/panel shows a `reverie` server with 7 tools: `reverie_observe`, `reverie_query`, `reverie_summary`, `reverie_reconsolidate`, `reverie_set_repo_state`, `reverie_review_list`, `reverie_review_decide` |

## 2. The agent asks to turn Reverie on

| Step | Action | Expected |
|---|---|---|
| 2.1 | Open a scratch repo with your agent; ask it anything | The first Reverie tool call returns the enablement question instead of acting; the agent asks you exactly once: **"Reverie isn't on for this repo. Turn it on? (yes / not now / never)"** |
| 2.2 | Answer **yes** | The agent records it (`reverie_set_repo_state`); no further asking this conversation |
| 2.3 | `reverie repos` | The repo is listed with `state: enabled` |

## 3. A conclusion gets captured

| Step | Action | Expected |
|---|---|---|
| 3.1 | Give the agent a task with a non-obvious decision (e.g. "add caching but think hard about stale-then-invalidate ordering"), let it work | The agent calls `reverie_observe` with its reasoning; the tool result reports "Extracted N conclusion(s) into session memory (… rejected by the Lifting Test)" |
| 3.2 | Ask the agent what it stored | It can describe the captured conclusion (counts/text via the tool result); not every response produces one — that's normal |

## 4. Both review paths show it

| Step | Action | Expected |
|---|---|---|
| 4.1 | Type `/reverie-review` in the harness chat (Codex: `/prompts:reverie-review`) | The agent calls `reverie_review_list`, shows the candidate's full conclusion text, and asks what to do — it does NOT decide for you |
| 4.2 | In a terminal: `reverie review` | The same candidate appears in the TUI, strongest first, one at a time with a/r/s hints. Press `q` to quit without deciding (leave it for step 5) |
| 4.3 | (optional) `reverie status` | The brain report shows the pending-review count |

## 5. Accept it

| Step | Action | Expected |
|---|---|---|
| 5.1 | `reverie review`, press `a` on the candidate | "Accepted: promoted to the Canonical Brain" (a diffusion warning can appear offline — see limits). The candidate leaves the queue |
| 5.2 | `reverie review` again | "No pending candidates — nothing to review." |

## 6. A new conversation retrieves it

| Step | Action | Expected |
|---|---|---|
| 6.1 | Open a NEW conversation in the same repo; let the agent read the relevant code first, then ask a question that touches the stored conclusion | The agent calls `reverie_query` AFTER reading code; the result includes the accepted conclusion with its confidence, status and grounding references, framed as past understanding to verify |
| 6.2 | Sanity-check the anti-anchoring framing | The agent verifies against the code rather than trusting the memory blindly |

## 7. A branch switch

| Step | Action | Expected |
|---|---|---|
| 7.1 | `git checkout -b smoke-test-branch` in the scratch repo; continue the conversation | Session memory switches with the branch; nothing errors. Long-term memory stays shared |
| 7.2 | `reverie status` | The active session now reports the new branch |
| 7.3 | `git checkout -` (switch back) | Sessions resolve again per branch; no manual action needed |

## 8. `reverie disable`, then `reverie enable`

| Step | Action | Expected |
|---|---|---|
| 8.1 | `reverie disable` | "Reverie is off for … Fully off: nothing is captured, retrieved, or injected here. Existing memory is kept…" |
| 8.2 | Use the agent in the repo | Tool calls return the structured "disabled" response; nothing is captured |
| 8.3 | `reverie enable` | Back on; everything intact (the accepted conclusion is still retrievable) |

## 9. Uninstall — dry run, then real

| Step | Action | Expected |
|---|---|---|
| 9.1 | `reverie uninstall --dry-run` | The full plan prints — every harness entry with its exact before/after — and NOTHING changes (harness configs still contain the entry) |
| 9.2 | `reverie uninstall` | The same plan, then ONE confirmation question. Answer `y`. The harness entries, instruction references and command files are removed; `~/.reverie` is kept |
| 9.3 | Restart the harness | The `reverie` server is gone; your other MCP servers are untouched |
| 9.4 | `reverie uninstall --delete-data` (optional — destructive) | Plan shows the final-export path + removal; confirm once; `~/.reverie` is gone, an export ZIP remains |

## 10. Reinstall keeps the memory

| Step | Action | Expected |
|---|---|---|
| 10.1 | `reverie install` again (same harnesses) | Idempotent — every file written again correctly; `~/.reverie/reverie.db` **already existed — unchanged** (skipped, not recreated) |
| 10.2 | Repeat steps 2.1–2.2 (the repo asks again only if it was wiped; with data kept it stays enabled) | Enablement remembered |
| 10.3 | New conversation, same retrieval check as step 6 | The accepted conclusion from step 5 is still there |

---

## Known limits to keep in mind while smoke testing

- Claude Code, Codex, Cursor and Gemini CLI are verified against vendor docs
  and offline tests, but not yet driven live by the developers — that's what
  this checklist is for. OpenCode is exercised end-to-end.
- Review through the agent relies on the agent relaying your choices; the
  channel is recorded, not verified.
- Cursor: no global rules file exists; guidance arrives via the MCP server
  instructions (if Cursor surfaces them) + tool descriptions. If the agent
  seems to ignore the usage contract, paste it into Cursor Settings → Rules.
- Codex custom prompts are deprecated upstream (still work as
  `/prompts:reverie-review`).
- Gemini CLI was replaced by Antigravity CLI for unpaid/Google One users
  (June 2026); the config format is unchanged for existing users.
- With no LLM key, extraction fails gracefully (pending queue + retry) and
  the review-gate accept can print a "promoted without diffusion" warning —
  `reverie retry --rediffuse` recovers it once a key is configured.
