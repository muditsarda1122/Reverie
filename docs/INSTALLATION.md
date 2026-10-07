# Reverie (Engineering Cognition) — Installation & Harness Integration Audit

**Date of audit:** 2026-10-04
**Scope:** read-only archaeology of how Reverie is currently installed and wired into coding-agent harnesses. Every claim cites repository code (`ec/install.py` unless noted). No source, config, or environment was modified. Read together with `PRODUCT.md` (same directory) for the product-level map.

---

## 1. The installer — implementation inventory

Everything installation-related lives in **one module: `ec/install.py`** (985 lines), exposed as the `ec` console script (`pyproject.toml:26` → `ec = "ec.install:main"`) and as `python -m ec.install`.

| Concern | Exists? | Implementation | Evidence |
|---|---|---|---|
| Installer entry point | ✅ | `run()` (pure-compute plan → apply) + `main()` (argparse CLI) | `install.py:802-934, 956-981` |
| Harness detection | ✅ | `AgentSpec.detected()` — binary on PATH **or** marker file exists | `install.py:590-599` |
| Per-harness config planning | ✅ | One `plan()` per agent → list of `PlannedWrite` (path, action, full content, note) | `install.py:414-419, 609-698` |
| JSON merge | ✅ | `_json_merge` — upserts `data[section]["ec"]` only, never siblings | `install.py:430-463` |
| TOML merge | ✅ | `_codex_toml` — upserts the `[mcp_servers.ec]` table, re-validates the merged file | `install.py:526-572` |
| Idempotent line/block append | ✅ | `_line_once` (CLAUDE.md import), `_block_once` (OpenCode/Codex AGENTS.md) | `install.py:466-494` |
| Spec-owned files | ✅ | `_verbatim_file` — AGENTS.md + command templates always rewritten verbatim when changed ("update" action) | `install.py:738-747` |
| `~/.ec` bootstrap | ✅ | `_ec_home_plans` — ec.db, config.yaml, AGENTS.md, commands/ | `install.py:706-735` |
| DB creation | ✅ | `Brain(write.path).close()` — auto-applies full schema | `install.py:753-755`, `ec/brain.py:71-105` |
| LLM key check | ✅ | presence check only; prints guidance; never writes the key | `install.py:872-887` |
| Ollama offline fallback (D42) | ✅ | detect `ollama` binary → offer → merge config.yaml → `ollama pull qwen2.5-coder:14b` (never fatal) | `install.py:286-310, 888-916` |
| Dry-run | ✅ | `--dry-run` prints every planned write incl. full resulting content; writes nothing | `install.py:937-947` |
| Interactive selection | ✅ | `_select_interactive` — "Add EC to all detected agents? (y/n)" → numbered picker | `install.py:765-795` |
| **Uninstall / removal** | ❌ | **does not exist anywhere in the repo** (grep-verified; the only "remove" hits are review-gate docstrings) | — |
| **Upgrade / version tracking** | ⚠️ | No version state, no migration. Re-running `ec install` is the upgrade path: agent entries upserted idempotently; spec-owned files (`AGENTS.md`, command templates) are force-refreshed verbatim when they changed; user's `config.yaml` is **kept** if it exists | `install.py:716-720, 738-747` |
| **First-run wizard beyond agent selection** | ❌ | No post-install verification, no key prompt-entry (only prints instructions), no test call | — |
| Venv/pip setup performed by installer | ❌ | The installer assumes `ec` is already importable; it does not create venvs or install packages | `install.py:378-403` |

Helper modules the installer calls into:

- `ec.brain.Brain` — to create the database file with the full schema (`install.py:753-755`).
- `ec.config.DEFAULT_CONFIG` / `ec.config.ec_home` — default config contents and `$EC_HOME` resolution (`install.py:38, 317-357`).
- `ec.install.server_command()` — decides how harness configs launch the MCP server (§5 below; D45/D28 — `install.py:366-403`).
- `importlib.metadata` — pip-installed detection (`_is_pip_installed`, `install.py:366-375`).

Related but distinct entry points (not part of install, but referenced by it): `python -m ec.session` (slash-command templates invoke it), `python -m ec.mcp_server` / `ec-mcp` (the server the harness configs launch), `ec-repair` (ops recovery).

---

## 2. Supported harnesses — exact table

The registry is a hard-coded list of exactly four agents (`AGENTS = [_ClaudeCode(), _Cursor(), _OpenCode(), _Codex()]`, `install.py:698`). **No other harness is implemented anywhere in the code.**

| Harness | Detected automatically? | Supported? | Configuration written? | MCP configured? | User interaction? | Evidence |
|---|---|---|---|---|---|---|
| **Claude Code** | ✅ binary `claude` on PATH **or** `~/.claude.json` exists | ✅ | `~/.claude.json` (`mcpServers.ec`) + `@~/.ec/AGENTS.md` import line appended to `~/.claude/CLAUDE.md` | ✅ stdio, user scope | Prompted in interactive mode; auto with `--all`/`--only` | `_ClaudeCode.plan` (`install.py:609-628`) |
| **Cursor** | ✅ binary `cursor` on PATH **or** `~/.cursor/mcp.json` exists | ✅ | `~/.cursor/mcp.json` (`mcpServers.ec`) + new file `~/.cursor/rules/ec.md` (reference block) | ✅ stdio | Same as above | `_Cursor.plan` (`install.py:631-650`) |
| **OpenCode** | ✅ binary `opencode` on PATH **or** `~/.config/opencode/opencode.json` exists | ✅ | `~/.config/opencode/opencode.json` (`mcp.ec`, with `$schema` added when creating) + block appended to `~/.config/opencode/AGENTS.md` | ✅ local (argv-list command, `environment` key — OpenCode's own naming) | Same as above | `_OpenCode.plan` (`install.py:653-677`) |
| **Codex** | ✅ binary `codex` on PATH **or** `~/.codex/config.toml` exists | ✅ | `~/.codex/config.toml` (`[mcp_servers.ec]` TOML table) + block appended to `~/.codex/AGENTS.md` | ✅ stdio via TOML table | Same as above | `_Codex.plan` (`install.py:680-695`) |
| Anything else (Windsurf, Aider, Cline, Zed, Gemini CLI, …) | ❌ | ❌ not implemented | ❌ | ❌ | Printed guidance only: "Configure one anyway with --only" | `install.py:849-851`; registry list is exhaustive |
| OpenCode **headless** (benchmark harness) | n/a | ⚠️ harness-specific, not user-facing | per-run `opencode_config.json` injected via `OPENCODE_CONFIG`; baseline condition disables EC explicitly | ✅ (per-run) | none | `ec/run_ecbench.py:127-165` |

---

## 3. Harness detection behavior (exact)

**Signal:** `AgentSpec.detected(home, which)` returns true if **either**

1. `shutil.which(binary)` finds the harness binary on `PATH` (`claude`, `cursor`, `opencode`, `codex`), **or**
2. the marker path exists under the user's home (`~/.claude.json`, `~/.cursor/mcp.json`, `~/.config/opencode/opencode.json`, `~/.codex/config.toml`).

(`install.py:590-599`)

**"Installed" vs "configured":** detection does **not** distinguish them. A harness with the EC entry already present reports exactly the same "found" as one never touched. The distinction surfaces only at plan/apply time: a planned write whose target already contains the identical `ec` entry becomes action `"unchanged"` and prints `· <path> already has the ec MCP server — unchanged` (`_json_merge:458-460`, `_codex_toml:562-564`).

**False positives:** yes, by construction —

- A leftover marker file (e.g., `~/.claude.json` remaining after Claude Code was uninstalled) marks the agent as installed.
- Any other tool that created the marker (e.g., Cursor users get `~/.cursor/mcp.json` created by Cursor itself — that one is the real config; but `~/.claude.json` is Claude Code's *state* file, not an install proof).
- A binary on PATH that the user never uses.

**False negatives:** yes — a harness installed without its binary on PATH and without the exact marker file (e.g., an OpenCode user whose only config is `opencode.jsonc`; the marker is specifically `opencode.json`). `docs/ASSESSMENT.md` §3c documents this exact machine having `opencode.jsonc` alongside.

**User selection:**

- Interactive (default when harnesses are detected): the installer prints a ✓/✗ detection table, then asks **"Add EC to all detected agents? (y/n)"** — answering `y` configures all detected; anything else shows a numbered **"Select agents to configure:"** picker (comma-separated numbers). EOF is treated as an empty/no answer; invalid selections are ignored with a message (`install.py:765-795, 820-851`).
- Non-interactive: `--all` (configure every detected agent) or `--only claude_code,cursor,opencode,codex` (by key; the two flags are mutually exclusive; unknown keys raise `InstallError`).
- **`--only` can configure a harness that was NOT detected** — it prints `(note: <name> was not detected — configuring anyway)` and proceeds (`install.py:831-842`).
- If **no** agents are detected and no `--only` was given: nothing agent-side is configured, but the `~/.ec` bootstrap and LLM check still run, with the message "No supported agents detected. Configure one anyway with `--only`…" (`install.py:843-851, 866-869`).

**All detected harnesses are automatically configured only with `--all` (or by answering `y` to the first prompt).** There is no unattended-by-default behavior.

---

## 4. The actual installation flow (as coded)

```text
User
 ↓
`ec`  (or: python -m ec.install)   [--all | --only KEY[,KEY…]] [--dry-run]
 ↓
Banner: "Engineering Cognition — Installation"  (+ dry-run notice)
 ↓
1. DETECT harnesses — for each of the 4: shutil.which(binary) OR marker exists
      → print "✓/✗ <name> found/not found"
 ↓
2. SELECT — branch:
   ├── --only given        → validate keys (unknown → InstallError, exit 1);
   │                         configure even if undetected (prints note)
   ├── --all given         → all detected, no prompt
   ├── detected non-empty  → "Add EC to all detected agents? (y/n)"
   │     ├── y             → all detected
   │     └── else          → numbered picker (EOF = nothing)
   └── none detected       → selected = []  (+ guidance message)
 ↓
3. PER-HARNESS PLAN+APPLY — for each selected agent, plan() → PlannedWrite list;
   each write is emitted (dry-run: printed with full content; else applied):
   JSON merges / TOML upsert / idempotent line & block appends
      └── malformed JSON/TOML in any target file → InstallError, ABORT, no changes
 ↓
4. ~/.ec BOOTSTRAP (always, even with zero agents):
   • ~/.ec/ec.db            → create via Brain() if missing (full schema); else "unchanged"
   • ~/.ec/config.yaml      → write DEFAULT_CONFIG if missing; else KEEP user's file
   • ~/.ec/AGENTS.md        → write verbatim (163 lines) if different
   • ~/.ec/commands/ec-start.md | ec-stop.md | ec-status.md → write verbatim
 ↓
5. LLM DEPENDENCY CHECK (informational + one optional action):
   ├── OPENCODE_ZEN_API_KEY set
   │     → "✓ key found"; note Ollama availability if binary exists
   └── key NOT set
         → "✗ key not set" + Zen guidance (opencode.ai/auth)
         ├── ollama on PATH  → offer (y/n):
         │     ├── y → merge config.yaml llm block to Ollama (qwen2.5-coder:14b,
         │     │        base_url http://localhost:11434/v1) + `ollama pull`
         │     │       (pull failure → printed, NEVER fatal)
         │     └── n → print the manual yaml snippet
         └── no ollama     → print Ollama install instructions (curl one-liner)
 ↓
6. FINAL TRANSCRIPT ("EC is ready!") — lists /ec-start, /ec-stop, /ec-status and
   the 4-step usage story (open agent → /ec-start → work → /ec-stop)
```

What the flow **does not** contain: no pip/venv creation, no Python-version check, no key entry prompt, no connectivity test, no session start, no post-install verification, no uninstall.

---

## 5. Exactly what gets written

### 5.1 Common server-command resolution (`server_command()`, `install.py:378-403`)

| Condition | Command written into agent configs |
|---|---|
| Package pip-installed (importlib.metadata sees `engineering-cognition`) | bare console script **`ec-mcp`**, no args, no env (D45) |
| Not pip-installed (dev checkout) | **absolute venv python** — first of `<repo-root>/.venv/bin/python` (POSIX) or `<repo-root>/.venv/Scripts/python.exe` (Windows) — with `args: ["-m", "ec.mcp_server"]` and `env: {PYTHONPATH: <repo-root>}` (D28/B2); falls back to `sys.executable` if no venv |

Known deviation (documented in-code): the design doc's literal `"ec"` command would launch the *installer*, hence the dedicated `ec-mcp` script name.

### 5.2 Per-harness artifacts

| Harness | File | Resulting structure (dev checkout shown; pip-installed collapses `command` to `"ec-mcp"`, drops `args`/`env`) | Merge semantics |
|---|---|---|---|
| Claude Code | `~/.claude.json` | `"mcpServers": { "ec": { "type": "stdio", "command": "<abs venv python>", "args": ["-m","ec.mcp_server"], "env": {"PYTHONPATH": "<repo-root>"} } }` | JSON merge; only the `ec` key inside `mcpServers` is added/replaced; all siblings and all other top-level keys preserved; `mcpServers` created if absent |
| Claude Code | `~/.claude/CLAUDE.md` | appends import line `@~/.ec/AGENTS.md` (exact `~`-form when default home) | idempotent line append (skipped if an identical stripped line exists); file created if missing |
| Cursor | `~/.cursor/mcp.json` | `"mcpServers": { "ec": { "command": "<abs venv python>", "args": [...], "env": {...} } }` | same JSON merge |
| Cursor | `~/.cursor/rules/ec.md` | full file: `## Engineering Cognition (EC)` block referencing `~/.ec/AGENTS.md`, naming the 4 MCP tools and the user-controlled session lifecycle | created/overwritten as one unit (idempotent: "unchanged" if byte-equal) |
| OpenCode | `~/.config/opencode/opencode.json` | `"mcp": { "ec": { "type": "local", "enabled": true, "command": ["<abs venv python>", "-m", "ec.mcp_server"], "environment": {"PYTHONPATH": "<repo-root>"} } }` — note **argv-list** `command` and the `environment` (not `env`) key, renamed in code because "OpenCode names the key differently"; `$schema` key added only when the file is created fresh | JSON merge of the `ec` key under `mcp` |
| OpenCode | `~/.config/opencode/AGENTS.md` | appended block: `## Engineering Cognition (EC)` + reference to `~/.ec/AGENTS.md` | idempotent block append (marker = the AGENTS.md reference string) |
| Codex | `~/.codex/config.toml` | `[mcp_servers.ec]` + `command = "<abs venv python>"` + `args = ["-m", "ec.mcp_server"]` + `env = { PYTHONPATH = "<repo-root>" }` (TOML inline table) | table upsert between `[mcp_servers.ec]` and the next `[`-header; existing table replaced wholesale; everything else byte-preserved; merged result re-validated with `tomllib` (refuses to write an unparseable merge) |
| Codex | `~/.codex/AGENTS.md` | appended block (same reference text) | idempotent block append |
| EC home | `~/.ec/ec.db` | empty SQLite brain with the full 11-table schema | created only if missing |
| EC home | `~/.ec/config.yaml` | full `DEFAULT_CONFIG` YAML dump | created only if missing — an existing user config is never touched (except the explicit Ollama offer, which deep-merges just the `llm` block) |
| EC home | `~/.ec/AGENTS.md` | 163-line agent instructions (verbatim spec text; tests assert equality with SPEC) | always rewritten verbatim if content differs (this is the update mechanism) |
| EC home | `~/.ec/commands/ec-start.md`, `ec-stop.md`, `ec-status.md` | step-list templates + the exact CLI line each invokes (`python -m ec.session start/stop/status`) | always rewritten verbatim if different |

**Transport:** all four harnesses are configured for **stdio** transport (Claude Code explicitly `"type": "stdio"`; OpenCode `"type": "local"`; Codex implicit stdio MCP table; Cursor default stdio). No HTTP/SSE transport exists in the product.

**Duplicate prevention & idempotency:** every write computes its full resulting content first and returns `unchanged` when the target already matches — repeated installs produce only `· … unchanged` lines and are safe to run any number of times (covered by `tests/test_phase5_install.py`).

**Secrets:** none are written by the installer — the API key is only *read* from the environment for a presence check; it is never placed into any file. (Agent configs receive `PYTHONPATH` and, in the bench harness only, `EC_HOME`/`EC_REPO_PATH`/`EC_BRANCH`.)

---

## 6. Installation prerequisites

### Required

| Prerequisite | Detail | Enforced? |
|---|---|---|
| Python ≥ 3.12 | `requires-python = ">=3.12"` (`pyproject.toml:9`); venv on this machine is 3.12.9 | By pip at install time; the installer itself performs no check |
| The package importable | `pip install -e .` from the repo root (or any install of `engineering-cognition`); pulls torch 2.2.2, numpy<2, scipy<1.13, scikit-learn<1.5, transformers<5, sentence-transformers==3.0.1, hdbscan, requests, pyyaml | Installer assumes it; if not pip-installed it hardwires the repo venv path + PYTHONPATH (which then must exist) |
| git | Sessions are keyed to `(repo, branch)` via `git rev-parse` / `git branch --show-current`; non-git dir → `ec-start` fails | Enforced at `ec-start` time, not install time |
| A git working repo | required to start a session (`ec/session.py:69-107`) | Enforced at `ec-start` |
| ~ 500 MB+ disk for the embedding model | `all-MiniLM-L6-v2` via sentence-transformers, downloaded on first use | Not checked |

### Optional

| Prerequisite | Behavior when absent |
|---|---|
| `OPENCODE_ZEN_API_KEY` | Install completes; at runtime extraction fails with the actionable `LLMError` ("…Get one at https://opencode.ai/auth — then: export OPENCODE_ZEN_API_KEY='your-key'"), mode detection falls back to keywords, `ec_reconsolidate` works only with an explicit relationship |
| `ollama` binary + `qwen2.5-coder:14b` | Offline fallback; installer prints `curl -fsSL https://ollama.com/install.sh | sh` instructions if absent |
| `pytest` (dev extra) | only for the test suite |

### Automatically configured

`~/.ec/` (db + config.yaml + AGENTS.md + command templates) and the four harness MCP entries (§5).

### User must configure manually

- **Exporting the API key** in their shell profile (the installer checks presence; it never writes it).
- **MCP entry for any non-supported harness** — the server command is documented above; there is no generated snippet for other clients (a user could hand-write the same JSON/TOML shape).
- **Running `ec-start` per repo/branch** (nothing auto-starts).
- If no agent was detected and they skip `--only`: wiring any harness at all.

### Filesystem / OS assumptions

- Writable home directory; installer creates missing parents for every target (`_apply`: `mkdir(parents=True, exist_ok=True)`).
- Paths use `Path.home()` / `~` expansion; display is `~`-relative when possible.
- Windows: the venv-python candidate list handles `.venv/Scripts/python.exe`, but everything else (tests, evidence, CI absence) is macOS/POSIX-flavored; Windows support is untested, not claimed.
- No systemd/launchd service, no daemon install — the only long-lived process is the MCP server, spawned by the harness itself.

---

## 7. First-run behavior (exact sequence after `ec install`)

1. **Nothing starts automatically.** No background process, no service, no launch agent. The MCP server starts only when the harness spawns it per the written config; the DB-creating installer step already happened during install.
2. **What already exists after install:** `~/.ec/ec.db` (empty, schema applied), `~/.ec/config.yaml` (defaults), `~/.ec/AGENTS.md`, `~/.ec/commands/*.md`, and the harness MCP entries + AGENTS references (§5).
3. **User opens their agent.** The harness launches `ec-mcp` (or venv-python `-m ec.mcp_server`) as a subprocess. On startup the server: runs overdue maintenance synchronously if triggers are met, starts the `MaintainerThread`, and logs to stderr (`ec/mcp_server.py:366-391, 773-786`). Repo resolution for grounding: `EC_REPO_PATH`(+`EC_BRANCH`) → git toplevel of cwd → most recent active session's repo (`resolve_maintainer_repo_path`).
4. **Sessions require the user:** the agent is told (via `~/.ec/AGENTS.md` and tool descriptions) to never start sessions itself; without an active session `ec_observe`/`ec_query` return the verbatim error "No active EC session. Suggest the user run /ec-start to begin a session."
5. **`/ec-start`** — the slash-command *templates* live in `~/.ec/commands/` and are referenced from `AGENTS.md`; the installer does **not** natively register slash commands in any harness. The command's actual body is `python -m ec.session start` (`ec-start` console script equivalent), which: detects `(repo, branch)` (git, or `EC_REPO_PATH`+`EC_BRANCH`), resumes the matching active session (loading decayed activation state) or creates one (carrying over unreviewed pending/skipped ECUs from closed sessions of the same context), prints the session id + brain summary, and best-effort runs the throttled (72 h) grounding check for the repo (`ec/session.py:242-330`).
6. **Agent behavior during work** is instructed by `~/.ec/AGENTS.md` (verbatim): when to call `ec_observe`, when to call `ec_query` (only after reading code — anti-anchoring), how to read confidence/status/grounding, and to never suggest lifecycle commands.
7. **`/ec-stop`** → `python -m ec.session stop` → interactive review gate in the terminal (open questions first, then a/r/i per group, y/n/s/d per ECU); non-interactive variants `--all-accept` / `--all-skip` / `--resolve-open-questions skip|archive` (`ec/session.py:378-459`).
8. **First tool calls**: `ec_get_summary` works session-less; `ec_observe`/`ec_query` require the session; the first `ec_observe` additionally stamps the repo's git HEAD into grounding (D49).

---

## 8. The actual MCP interface

Server: hand-rolled stdio NDJSON JSON-RPC 2.0 (`PROTOCOL_VERSION = "2024-11-05"`, server name `ec`, `ec/mcp_server.py:79-80`). Methods: `initialize`, `ping`, `tools/list`, `tools/call`. **Tools only — no resources, no prompts** are exposed. All four tools are defined in `TOOLS` (`mcp_server.py:207-324`) and are automatically available as soon as the harness spawns the server; session-gating is per-tool.

| Tool | Purpose | When the agent is told to call it | Key arguments | Key return values | Session required? |
|---|---|---|---|---|---|
| `ec_observe` | Extract conclusions from the agent's reasoning → Session Brain → lightweight diffusion | After discovering an invariant / root cause / decision / wrong assumption; never for simple code changes | `user_prompt`* , `reasoning_trace`* , `final_output` (opt) | `status, ecus_extracted, ecus_rejected, rejection_summary, session_brain_count, skipped_invalid, diffusion_failures, message` | ✅ |
| `ec_query` | Demand-driven retrieval of past cognition (both brains) | After reading relevant code, before decisions / debugging / architecture | `query`* , `scope` (6-level enum, opt), `mode` (5-mode enum, opt) | `status, mode, mode_detected, groups_retrieved, groups[] (core_ecu + supporting_evidence/contradictions/dependencies/superseded_by), warnings, brain_source, formatted, fallback, filtered_out` | ✅ |
| `ec_get_summary` | Brain statistics (awareness without anchoring) | Once at session start; also usable anytime | none | `canonical_brain{total_ecus, by_scope, by_status, last_maintenance_run, last_ecu_added, pending_review_count}, session{active, session_brain_count, repo, branch}, message` | ❌ (works without) |
| `ec_reconsolidate` | Re-evaluate a *previously retrieved* canonical ECU with new evidence; writes Canonical immediately | When verified evidence shows a retrieved ECU is wrong/outdated/needs strengthening | `ecu_id`* , `evidence`* , `relationship` (supports/contradicts/supersedes, opt) | `status, ecu_id, action_taken (confidence_updated\|challenged\|superseded\|no_change), old_confidence, new_confidence, edges_created, message` | ✅ (+ labile gate: must have been surfaced by `ec_query` in this server process — `mcp_server.py:685-696`) |

`isError` on the tool-result envelope mirrors `payload.status == "error"`. After every successful `ec_query` the server stamps retrieval metadata, applies the confidence reinforcement bump, and marks surfaced canonical ECUs labile (`mcp_server.py:481-526`).

---

## 9. Failure behavior (what the user actually sees)

| Situation | Actual behavior | Evidence |
|---|---|---|
| Unsupported harness installed (e.g., Windsurf) | Not detected, silently unconfigured; if nothing detected: "No supported agents detected. Configure one anyway with `--only` (e.g. `python -m ec.install --only claude_code`)." | `install.py:849-851` |
| `--only windsurf` (unknown key) | `InstallError`: "unknown agent(s): windsurf. Known agents: claude_code, codex, cursor, opencode" → stderr, exit 1 | `install.py:831-837, 979-981` |
| Harness not installed but forced via `--only` | Proceeds, printing "(note: <name> was not detected — configuring anyway)" | `install.py:837-842` |
| Malformed agent config (invalid JSON/TOML/YAML) | `InstallError` naming the file, the parse error, and "Fix or remove it, then re-run `python -m ec.install`. **No changes were made to it.**" Exit 1 | `install.py:438-457, 535-540, 330-348` |
| Existing Reverie config in a harness file | Idempotent merge — only the `ec` entry upserted; identical state → `· <path> already has the ec MCP server — unchanged` | `_json_merge`/`_codex_toml` |
| Repeated installation | Everything re-planned; already-correct targets → `unchanged` lines; spec-owned files rewritten only if content differs; safe | `_verbatim_file`, tests |
| Missing API key at install | Install still succeeds; prints `✗ OPENCODE_ZEN_API_KEY is not set` + guidance + Ollama offer | `install.py:885-916` |
| Missing Ollama binary (no key) | Prints curl install instructions + the manual yaml snippet | `install.py:915-916` |
| Ollama present, user accepts, `ollama pull` fails | "! Could not run `ollama pull` (<exc>). Run it manually, then EC works offline." — **never fatal** | `install.py:903-910` |
| Not a git repo at `ec-start` | `SessionError`: "Could not detect a git repository from <cwd> … run /ec-start from inside a git repository." exit 1 | `ec/session.py:76-87, 405-409` |
| No active session on `ec_observe`/`ec_query`/`ec_reconsolidate` | Structured error payload: "No active EC session. Suggest the user run /ec-start to begin a session." (`isError: true`) — the server keeps running | `mcp_server.py:83-85, 473-479` |
| MCP startup with locked/corrupt DB | **Uncaught `BrainError`** — "EC brain could not be opened at <path>: <exc>. If it is locked by another process, close other EC instances and try again." — the process dies with a traceback (`_ensure_brain()` sits outside the guarded region of `start_maintenance`) | `ec/brain.py:80-85`, `ec/mcp_server.py:346-349, 773-786` |
| DB file missing at any entry point | Auto-created with full schema (`Brain.__init__`) — never an error | `ec/brain.py:71-79` |
| Extraction LLM failure at runtime | Non-fatal tool error: "Extraction failed: <reason>. The reasoning trace may be too short or contain no engineering conclusions. This is not an error…" | `mcp_server.py:86-90, 567-569` |
| Mode detection LLM failure | Falls back to keyword classifier; retrieval proceeds (logs a warning) | `ec/mode_detection.py:107-115` |
| Reconsolidation offline without explicit relationship | Tool error telling the agent to retry with an explicit relationship ("Nothing was changed") | `mcp_server.py:710-721` |
| Diffusion failure at review-gate accept | Promotion stands; ECU listed as un-diffused; prominent CLI warning `⚠️ WARNING: N ECU(s) promoted without diffusion…` | `ec/review_gate.py:900-908, 974-988` |
| DB integrity issues post-install | `ec-repair [--dry-run]` — 6 checks, safe additive fixes only | `ec/repair.py` |
| Config file invalid YAML at load (`~/.ec/config.yaml`) | The installer guards against writing into malformed YAML, but `config.load_config` at runtime would raise `ValueError`/yaml errors | `ec/config.py:342-352` |

---

## 10. Correct eventual README onboarding (strictly implementation-backed)

### What the README can truthfully say today

1. **What Reverie is** — a local MCP server + Python package that stores engineering *conclusions* (ECUs) with confidence, scope, grounding, and typed relationships in one SQLite file, and serves them to coding agents on demand.
2. **Prerequisites** — Python ≥ 3.12; git; a clone of this repository; `pip install -e .` (heavy pinned ML deps); an LLM key (`OPENCODE_ZEN_API_KEY`) *or* Ollama for offline mode.
3. **Install** — `pip install -e .` then `ec` (interactive) / `ec --all` / `ec --only …` / `ec --dry-run`.
4. **Harness selection/configuration** — auto-detects Claude Code, Cursor, OpenCode, Codex (binary-on-PATH or marker file); prompts "Add EC to all detected agents? (y/n)" with a numbered picker; `--only` can force-configure undetected agents.
5. **MCP setup** — automatic for the four supported agents (exact files/keys in §5). For other MCP clients: launch `ec-mcp` (pip-installed) or `<venv python> -m ec.mcp_server` with `PYTHONPATH=<repo root>` over stdio.
6. **Starting a session** — from inside a git repo: `ec-start` (resumes or creates the session for `(repo, branch)`, prints brain summary). The agent never starts sessions itself.
7. **Observing cognition** — the agent calls `ec_observe` with `user_prompt` + `reasoning_trace` (per the instructions in `~/.ec/AGENTS.md`); rejections are reported back.
8. **Querying cognition** — the agent calls `ec_query` (optionally `scope`/`mode`) after reading code; results include confidence labels, status flags, grounding references, and the "verify against current code" framing note.
9. **Ending/reviewing a session** — `ec-stop` for the interactive review gate (open questions a/b/c/d; groups a/r/i; per-ECU y/n/s/d), or `ec-stop --all-accept` / `--all-skip`; accepted ECUs are diffused into the Canonical Brain.
10. **Where memory is stored** — `~/.ec/ec.db` (override dir with `EC_HOME`); config `~/.ec/config.yaml`; agent instructions `~/.ec/AGENTS.md`; integrity tool `ec-repair`.

Also truthful today: idempotent re-install (running `ec` again refreshes `AGENTS.md`/command templates and leaves everything else untouched); `~/.ec/commands/*.md` contain the exact CLI lines the slash commands run.

### What would require additional implementation (honest gap list)

- **Uninstall/removal** — nothing exists; README cannot claim "remove with one command."
- **Slash-command registration** — templates are written to `~/.ec/commands/` but not registered natively in any harness; whether `/ec-start` actually appears in the agent's command palette depends on the harness. README must say "run the template's CLI or type the command your agent recognizes" until native registration exists.
- **PyPI availability** — the package is not published (no publish workflow in repo); "pip install engineering-cognition" cannot be claimed; onboarding must be clone + `pip install -e .`.
- **API key setup** — installer checks presence only; a keyless "paste your key" flow does not exist.
- **Non-tty install** — interactive selection needs a TTY (EOF-safe but effectively requires interactivity unless `--all`/`--only`).
- **Broader harness support** — only 4 agents; any "works with your favorite agent" copy needs the manual-MCP-instructions paragraph instead.
- **Windows** — path handling has partial support; nothing tested; cannot claim.
- **Post-install verification** — no "ec doctor"/connectivity test exists to put behind a "Verify your install" heading (closest is `ec-repair`, which checks DB integrity, not harness wiring).

---

## 11. Website implications

### Website-safe installation messaging (implementation-backed)

1. **"MCP-native."** Hand-rolled stdio JSON-RPC 2.0 MCP server, zero SDK dependencies (`ec/mcp_server.py`).
2. **"One command configures Claude Code, Cursor, OpenCode, and Codex."** `ec install` detects and writes per-agent MCP config + agent instructions, merge-only and idempotent (`ec/install.py`).
3. **"Local-first: your engineering memory stays in one SQLite file on your machine."** `~/.ec/ec.db`; no cloud, no vector DB, embeddings as local BLOBs (`ec/config.py`, `ec/schema.sql`).
4. **"Runs with local models."** Ollama fallback auto-configuration (`qwen2.5-coder:14b`) offered when no API key is set (`ec/install.py:286-310, 888-916`).
5. **"Nothing enters long-term memory without your review."** The review gate is the only write path into the Canonical Brain (`ec/review_gate.py`).

### What NOT to say (unsupported by implementation)

- ❌ "One-line install" / "pip install engineering-cognition" — not on PyPI; requires repo clone + editable install.
- ❌ "Works with any agent" / "works with Windsurf/Aider/Cline…" — only 4 installers; others need a hand-written MCP entry.
- ❌ "Installs /ec-start slash commands into your agent" — templates are written to `~/.ec/commands/` but not natively registered anywhere.
- ❌ "Zero configuration" — an API key (or Ollama setup) is required for the core extraction loop; sessions require a git repo.
- ❌ "Windows/macOS/Linux" — only macOS is evidenced; Windows is partial and untested.
- ❌ Any dashboard/GUI/cloud-sync/removal claims — none exist.
- ❌ Any "uninstall anytime" claim — no uninstall path exists.

---

## 12. Final verdict

### Current installation status: **usable but onboarding needs documentation**

Why:

- The installer itself is genuinely solid for its scope: four-agent registry, dual-signal detection, computed planned writes, merge-only idempotency, TOML re-validation, dry-run with full content preview, `~/.ec` bootstrap, and an Ollama fallback — all covered by `tests/test_phase5_install.py` (26 tests) and exercised by the live E2E/benchmark pipeline.
- What blocks *public* onboarding is everything around it: the package is not on PyPI (install = clone + `pip install -e .`), there is **no README, no LICENSE, no uninstall**, slash-command templates are written but not natively registered in any harness, the API key has a check-but-don't-set flow, and only the interactive TTY path is friendly. None of these are installer bugs — they are missing product surfaces that a README cannot paper over.
- The MCP wiring itself works and was verified live (initialize/tools/list/tools/call against the real server; OpenCode config format validated on a machine with a pre-existing `opencode.jsonc` — `docs/ASSESSMENT.md` §3).

Classification boundary notes: not "ready for public onboarding" (no distribution channel, no license, no README); clearly beyond "experimental" (idempotent, tested, live-verified); not "incomplete" in the sense of broken — the happy path works end-to-end.

### Minimum work required before publishing a polished README (actual gaps only)

1. **LICENSE file + `license` field in `pyproject.toml`** — the repo ships neither; any public distribution is blocked on this. (The extraction prompt self-declares MIT for itself only.)
2. **A distribution story** — publish to PyPI (name `engineering-cognition` is already the distribution name) or document the exact clone + `pip install -e .` flow as the supported install.
3. **`README.md` itself** — none exists; the ten-step onboarding in §10 is fully implementable from current behavior.
4. **Document (or implement) slash-command reality** — state that `/ec-start` etc. are defined by `~/.ec/commands/*.md` + `~/.ec/AGENTS.md`, and either register them natively per harness or tell users to run the console scripts (`ec-start`/`ec-stop`/`ec-status`) directly.
5. **API-key onboarding copy** — document `export OPENCODE_ZEN_API_KEY=…` (and the Ollama alternative) as a manual step; optionally add a post-install connectivity check if one is ever implemented.
6. **(Optional, small) uninstall guidance** — since no uninstaller exists, the README should at minimum list the files created (§5) so users can remove them manually — or an `ec uninstall` would close the loop.
7. **(Optional) `--only`-style non-interactive default note** — document that headless/CI setup must use `--all`/`--only` because the default path prompts.

---

*End of INSTALLATION.md — audit only; no repository files were modified. Installer evidence verified against `ec/install.py` at commit `4e0d312` (HEAD of the audit date) and cross-checked against `tests/test_phase5_install.py`, `docs/ASSESSMENT.md` §3, and the MCP server/session modules.*
