"""``reverie install`` — one command configures Reverie for all detected coding agents.

Spec §28.8 (detection + per-agent installation logic + LLM provider step),
§28.9 (``~/.reverie/AGENTS.md``, written verbatim), §28.10 (command templates),
§28.2 (the ``~/.reverie/`` directory structure).

Safety contract: the installer writes ONLY inside the Reverie home (``~/.reverie`` or
``$REVERIE_HOME``) and the four agents' own config locations. Existing agent
config is merged, never overwritten (only the ``reverie`` entry is
added/replaced). ``--dry-run`` prints every planned write with its full
resulting content and writes nothing. API keys are NEVER written to disk —
the installer only reads the environment to detect what is available.

Entry points: ``python -m reverie.install`` or the ``reverie`` console script.
When the package is pip-installed (D45), agent configs carry the ABSOLUTE path
of the ``reverie-mcp`` console script belonging to the running interpreter's
environment (venv, pipx, uv tool or system install — harnesses launch MCP
servers outside the user's virtualenv, so a bare name is not enough);
otherwise the absolute venv-python form is written (B2/D28). Every install
ends by launching each written command, sending an MCP ``initialize`` and
checking the response; ``reverie status`` flags commands that later break
(a moved or deleted venv).

LLM provider step (feat/providers): the installer detects provider keys in
the environment (registry in ``reverie/providers.py``), offers the detected one
as the default, merges the resolved ``llm`` block into ``~/.reverie/config.yaml``
(D42-style), and verifies with one cheap live ping (``--no-verify`` skips).
Ollama remains the offline fallback (D42).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import providers
from .brain import Brain
from .config import DEFAULT_CONFIG, load_config
from .config import reverie_home as _default_reverie_home
from .llm import call_llm

# ---------------------------------------------------------------------------
# Verbatim spec texts (§28.9, §28.10) — tests assert equality with SPEC.md
# ---------------------------------------------------------------------------

AGENTS_MD = """# Reverie — Agent Instructions

Reverie is an MCP server providing engineering memory for your codebase.
It stores engineering conclusions (the internal record is an Engineering
Cognition Unit, ECU) — conclusions, not facts — and retrieves them when
you need them.

Reverie provides seven MCP tools:
- observe: Extract engineering conclusions from your reasoning
- query: Retrieve relevant engineering cognition
- summary: Get brain statistics at session start
- reconsolidate: Update a retrieved conclusion with new evidence
- set_repo_state: Record the user's enablement answer for a repo
- review_list: Show the user's pending review candidates
- review_decide: Apply review decisions the user stated

## Repo Path — Pass It on Every Call

Global MCP servers may be started in the home folder, where the repo
cannot be detected. Pass the workspace path as the `repo_path` argument
on every tool call.

## Enablement — The First-Time Question

Reverie is enabled per repo, and the answer is remembered. The first time
a tool call reports that a repo is undecided, ask the user exactly:

Reverie isn't on for this repo. Turn it on? (yes / not now / never)

Ask this question AT MOST ONCE per conversation. Record the answer with
set_repo_state:
- "yes" -> state="enabled"
- "not now" -> state="not_now" (stays off for now; do not ask again in
  this conversation)
- "never" -> state="disabled" (fully off for this repo: nothing is
  captured, retrieved, or injected; existing memory is kept)

If a tool response reports the repo is disabled, do nothing with
Reverie in that repo and do not ask again.

## Sessions Are Automatic

In an enabled repo, every branch has a session that is created
automatically on the first tool call and never needs closing. Switching
branches mid-conversation switches session memory; long-term memory
stays shared across branches. Never suggest starting or stopping a
session — there is nothing to start or stop.

## When to Call observe

Call observe AFTER you:
- Discover a non-obvious invariant or dependency
- Debug a tricky issue and find the root cause
- Make an architectural decision with rationale
- Discover that a previous assumption was wrong
- Find that code behaves differently from what documentation suggests

Pass your reasoning trace (your thinking, decisions, and rationale) — not
just the final code output. The Extractor lifts observations into conclusions.

Do NOT call observe for:
- Simple code changes with no insight
- Things already captured in existing conclusions
- Pure factual observations (e.g., "this function returns a string")
- Every single response — only when you have engineering insight worth saving

Call observe AT MOST ONCE per task — batch everything worth saving into a
single call after you have the insight, not after every response — unless a
separate new insight appears.

## When to Call query

Call query when you recognize a gap that past engineering cognition might fill.
Read the relevant code files FIRST. Do NOT call query before you have grounded
yourself in the repository. Reverie's memory is a supplement to your
understanding, not a replacement for reading the code.

Call query AFTER reading the code, when:
- You're about to make a decision between multiple approaches
- You're debugging and the cause isn't obvious from the code alone
- You see a pattern in the code you don't recognise
- You're about to implement something in a file or module you haven't worked
  with in this session
- You suspect a previous assumption might be wrong
- You're planning an architectural change and want to check for existing
  architectural decisions or constraints

Do NOT call query:
- Before you have read the relevant code files (this causes anchoring bias)
- For simple code changes with no engineering insight needed
- On every single response — only when you genuinely need past context
- As a replacement for reading the code — always read the code first

This timing matters. EC-Bench showed that premature retrieval caused anchoring:
the agent used injected cognition heavily but produced worse work (quality dropped
-0.589, groundedness dropped -0.480). The best results came when the agent
established repository understanding FIRST, then consulted cognition.

Pass a natural language query describing what you want to know. Be specific.
"How does the dependency injection container resolve circular imports?" is better
than "dependency injection." The query is embedded and compared semantically —
precise engineering questions retrieve better than single keywords.

If query returns no results, proceed normally — this means Reverie
has no relevant memory yet. It starts empty and builds up over sessions.

## When to Call reconsolidate

Call reconsolidate when you discover that a previously retrieved
conclusion needs updating based on what you've found in the current
codebase. This is how Reverie stays current — beliefs that are no longer
accurate get corrected.

Call reconsolidate when:
- You verified a retrieved conclusion against the code and found it's no longer accurate
- You discovered new evidence that strengthens a retrieved conclusion
- You discovered new evidence that contradicts a retrieved conclusion
- The code a conclusion is grounded in has changed, and the conclusion needs updating

Pass the ECU ID (from the query result), your evidence (your reasoning about
what changed), and optionally the relationship (supports/contradicts/supersedes).
Evidence must reference something the user can verify — a file path with
line numbers, a test name, or a commit hash — not free-floating narrative.

Do NOT call reconsolidate for:
- New insights unrelated to a previously retrieved conclusion (use observe)
- Conclusions you haven't retrieved in this session (query first)
- Simple code changes with no impact on engineering conclusions

If reconsolidate is not available, use observe to record
the new finding — the Diffuser will handle the relationship at review time.

## When to Call summary

Call summary ONCE at session start to see what Reverie already
knows. This helps you decide whether to query for specific context
before starting work. Returns brain statistics, not individual conclusions.

If the response contains a pending-review backlog line (counts only),
mention it to the user AT MOST ONCE per conversation and offer two ways
to review: run `reverie review` in a terminal, or say "show my Reverie
candidates" here. Counts only — never candidate contents.

## Review — The Human Decides

Observations are stored as review candidates, never promoted
automatically. The user reviews them:
- in a terminal with `reverie review`, or
- through you: when the user asks to see their Reverie candidates, call
  review_list and show EVERY candidate's full conclusion text before
  asking what they want to do.

Rules for review through you:
- Relay ONLY the decisions the user actually states, via review_decide.
- NEVER decide on the user's behalf.
- NEVER bulk-accept unless the user explicitly asked for that group.
- Be honest about the limits: Reverie cannot verify who made a decision;
  every decision is recorded with its channel (tui, agent, or cli).

## Reading Reverie Output

When query returns conclusions, interpret them as follows:

### Confidence levels:
- High (>0.7): Reliable, act on this. Verify grounding if the decision is critical.
- Medium (0.3-0.7): Probably correct. Verify before acting on critical decisions.
- Low (<0.3): Uncertain. Flagged for your awareness. Do not rely on this
  without independent verification.

### Status indicators:
- active: Normal, current belief. Safe to use.
- challenged: Contradicting evidence exists. Be careful — this belief
  may be wrong. Check for a newer conclusion that might supersede it.
- open_question: Unresolved engineering question with competing hypotheses.
  Both sides may be returned. Treat as "this is debated."

### Grounding references:
Every conclusion includes grounding references (files, symbols, commit hash).
ALWAYS verify these against the current codebase before acting on it.
If the referenced file or symbol no longer exists, it may be stale.

Reverie's Maintainer periodically verifies grounding references against the
live repository and deprecates conclusions whose grounding has disappeared.
Deprecated conclusions are not returned by query. If you discover
that a conclusion's grounding is stale before the Maintainer has caught it,
call reconsolidate to update it with what you've found.

### Warnings:
query may return warnings (e.g., "2 conclusions have low
confidence", "1 conclusion is challenged"). Read these warnings and adjust
your trust accordingly.

## What Reverie Stores

Conclusions are engineering conclusions, not raw information. Examples:
- STORED: "Authentication correctness depends on optimistic token refresh;
  future implementations should preserve this invariant."
- NOT STORED: "TokenManager.refresh() is called before cache.clear()"

Each record has:
- cognition: The irreducible engineering conclusion (text)
- confidence: 0.0 to 1.0, Bayesian-updated belief strength
- scope: Where this applies (engineering, domain, organization, project, repo,
  module, subsystem)
- grounding: References to code files, symbols, and commit state
- edges: Connections to other conclusions (supports, contradicts, supersedes, depends_on)

## Important: Anti-Anchoring

Reverie's memory is framed as "past beliefs to verify against current
code," NOT as authoritative context. Always verify conclusions against the
live codebase.
Do not anchor on past beliefs if the current code contradicts them.
If you notice a contradiction, call observe to record the new finding.

Observations are stored in the Session Brain, not the Canonical Brain. The
user reviews the candidates — via `reverie review` in a terminal, or through you
(see "Review — The Human Decides"). Only accepted observations are promoted
to the Canonical Brain for future use. Don't hesitate to observe something
uncertain — the review gate filters it.
"""


# ---------------------------------------------------------------------------
# user-level slash commands (Phase 3) — installed in each harness's own
# location and format. Two commands: /reverie-review (ask the agent to list
# review candidates and relay decisions) and /reverie-status (the session +
# brain report through summary).
# ---------------------------------------------------------------------------

_REVIEW_COMMAND_BODY = """Review your pending Reverie conclusions (the human decides).

Steps:
1. Call review_list to get the pending candidates
   (pass the workspace path as repo_path).
2. Show EVERY candidate's full conclusion text, one at a time.
3. Ask what the user wants to do with each (accept / reject / skip).
4. Relay ONLY the decisions the user states, via review_decide
   (pass the review_token from the listing).
5. Never decide on the user's behalf; never bulk-accept unless the user
   explicitly asked for that group.
"""

_STATUS_COMMAND_BODY = """Show the current Reverie status.

Steps:
1. Call summary (pass the workspace path as repo_path).
2. Report: brain statistics, session state for the current (repo, branch),
   and the pending-review count. Statistics only — never candidate
   contents unless the user asks for a review (use /reverie-review).
"""

#: Claude Code user-level slash commands: ~/.claude/commands/<name>.md —
#: plain markdown (frontmatter optional). https://code.claude.com/docs/en/slash-commands
CLAUDE_CODE_COMMANDS: dict[str, str] = {
    "reverie-review.md": _REVIEW_COMMAND_BODY,
    "reverie-status.md": _STATUS_COMMAND_BODY,
}

#: Cursor user-level (global) commands: ~/.cursor/commands/<name>.md — plain
#: markdown (beta). https://cursor.com/docs/agent/chat/commands
CURSOR_COMMANDS: dict[str, str] = CLAUDE_CODE_COMMANDS

#: OpenCode user-level commands: ~/.config/opencode/commands/<name>.md —
#: markdown with YAML frontmatter. https://opencode.ai/docs/commands/
_OPENCODE_COMMANDS: dict[str, str] = {}
for _name, _body in CLAUDE_CODE_COMMANDS.items():
    _OPENCODE_COMMANDS[_name] = (
        "---\n"
        f"description: "
        f"{'Review pending Reverie conclusions' if 'review' in _name else 'Show the current Reverie status'}\n"
        "---\n" + _body
    )
OPENCODE_COMMANDS = _OPENCODE_COMMANDS

#: Codex user-level custom prompts: ~/.codex/prompts/<name>.md (invoked as
#: /prompts:<name>; deprecated upstream but still the user-level command
#: mechanism). https://developers.openai.com/codex/custom-prompts
CODEX_PROMPTS: dict[str, str] = {
    "reverie-review.md": (
        "---\ndescription: Review pending Reverie conclusions\n---\n"
        + _REVIEW_COMMAND_BODY
    ),
    "reverie-status.md": (
        "---\ndescription: Show the current Reverie status\n---\n"
        + _STATUS_COMMAND_BODY
    ),
}


def _gemini_command(body: str, description: str) -> str:
    """TOML command file for Gemini CLI: ~/.gemini/commands/<name>.toml with
    a required `prompt` string. https://geminicli.com/docs/cli/custom-commands"""
    lines = [f'# {description}', f'description = "{description}"', "prompt = '''"]
    lines.extend(body.rstrip("\n").splitlines())
    lines.append("'''")
    return "\n".join(lines) + "\n"


GEMINI_COMMANDS: dict[str, str] = {
    "reverie-review.toml": _gemini_command(
        _REVIEW_COMMAND_BODY, "Review pending Reverie conclusions"),
    "reverie-status.toml": _gemini_command(
        _STATUS_COMMAND_BODY, "Show the current Reverie status"),
}

# Ollama offline fallback (§28.8, auto-setup D42 / design doc §6.4).
# base_url includes /v1: Ollama serves its OpenAI-compatible endpoint at
# http://localhost:11434/v1/chat/completions (Phase 13, D48).
OLLAMA_BINARY = "ollama"
OLLAMA_MODEL = "qwen2.5-coder:14b"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
OLLAMA_LLM_BLOCK = {
    "provider": "ollama",
    "model": OLLAMA_MODEL,
    "base_url": OLLAMA_BASE_URL,
}
_OLLAMA_CONFIG_SNIPPET = (
    '  llm:\n'
    f'    provider: "ollama"\n'
    f'    model: "{OLLAMA_MODEL}"\n'
    f'    base_url: "{OLLAMA_BASE_URL}"'
)
_OLLAMA_INSTALL_INSTRUCTIONS = (
    "  ✗ Ollama not found. For offline (local) inference, install it with:\n"
    "      curl -fsSL https://ollama.com/install.sh | sh\n"
    "    then pull the model:\n"
    f"      ollama pull {OLLAMA_MODEL}\n"
    "    and set these keys in ~/.reverie/config.yaml:\n"
    f"{_OLLAMA_CONFIG_SNIPPET}"
)
_OLLAMA_OFFER = (
    f"\nConfigure Reverie for offline use with Ollama ({OLLAMA_MODEL})? "
    "(y/n): "
)

_OLLAMA_FALLBACK_NOTE = (
    "  · Ollama detected — available as an offline fallback (provider "
    f"\"ollama\", model \"{OLLAMA_MODEL}\")."
)

#: The default config's key env var (kept for tests/readers; the flow is
#: registry-driven now — this is simply REGISTRY["opencode_zen"].api_key_env).
ZEN_KEY_ENV = providers.REGISTRY["opencode_zen"].api_key_env


def _export_hint(entry: providers.Provider) -> str:
    """One line of key-setup guidance for a provider."""
    url = f" — get one at {entry.key_url}" if entry.key_url else ""
    return f"export {entry.api_key_env}='your-key'{url}"


def _provider_guidance() -> str:
    """Provider-agnostic key guidance (non-interactive no-key output)."""
    lines = [
        "Reverie needs one LLM provider for extraction, relationship "
        "classification, and mode detection. Export a key for any of these, "
        "then re-run `reverie install` (or use --provider):"
    ]
    for key in providers.MENU_KEYS:
        entry = providers.REGISTRY[key]
        if entry.api_key_env and entry.key_required:
            lines.append(f"  {_export_hint(entry)}")
    lines.append("  (or run a local model via Ollama — see the Ollama "
                 "section below when the binary is absent)")
    return "\n".join(lines)


def _existing_llm_config(reverie_home: Path) -> dict | None:
    """The user's pre-existing ``llm`` block from ``config.yaml`` (None when
    there is no usable one). Read BEFORE the install run writes anything so
    a freshly bootstrapped default never counts as user configuration
    (review fix 44)."""
    path = reverie_home / "config.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    llm = data.get("llm") if isinstance(data, dict) else None
    return llm if isinstance(llm, dict) and llm else None


def _llm_block_config_plan(reverie_home: Path, home: Path, block: dict,
                           detail: str) -> PlannedWrite:
    """Merge a resolved ``llm`` block into ``~/.reverie/config.yaml`` (D42-style).

    Existing config is merged (user keys preserved); a missing file gets the
    full defaults with the block applied. Idempotent: unchanged when the
    file already carries exactly the merged content.
    """
    path = reverie_home / "config.yaml"
    disp = _display(path, home)
    existed = path.exists()
    if existed:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise InstallError(
                f"{disp} is not valid YAML ({exc}). Fix or remove it, then "
                "re-run `python -m reverie.install`. No changes were made to it."
            ) from exc
        if not isinstance(data, dict):
            raise InstallError(
                f"{disp} must contain a YAML mapping. Fix or remove it, then "
                "re-run `python -m reverie.install`. No changes were made to it."
            )
        merged = copy.deepcopy(data)
    else:
        merged = copy.deepcopy(DEFAULT_CONFIG)
    llm = merged.setdefault("llm", {})
    if not isinstance(llm, dict):
        raise InstallError(
            f"{disp}: `llm` is not a YAML mapping. Fix or remove it, then "
            "re-run `python -m reverie.install`. No changes were made to it."
        )
    llm.update(block)
    content = yaml.safe_dump(merged, sort_keys=False)
    if existed and content == path.read_text(encoding="utf-8"):
        return PlannedWrite(path, "unchanged", None,
                            f"{disp} already carries this llm block — unchanged")
    return PlannedWrite(
        path, "merge" if existed else "create", content, detail,
    )


def _configure_provider(reverie_home: Path, home: Path, entry: providers.Provider,
                        *, dry_run: bool, print_fn, environ, runner,
                        ollama_path: str | None) -> None:
    """Write the provider's llm block and report its key status (no secrets)."""
    write = _llm_block_config_plan(
        reverie_home, home, providers.llm_block(entry.key),
        f"Configured {_display(reverie_home / 'config.yaml', home)} for "
        f"{entry.name} ({entry.key})",
    )
    _emit(write, home, dry_run, print_fn)
    if entry.key == "ollama":
        if ollama_path:
            print_fn(f"  ✓ Ollama detected ({ollama_path})")
        if dry_run:
            print_fn(f"  [dry-run] would pull {OLLAMA_MODEL} via "
                     f"`{OLLAMA_BINARY} pull {OLLAMA_MODEL}`")
        else:
            print_fn(f"  Pulling {OLLAMA_MODEL} (this may take a while)...")
            try:
                runner([ollama_path or OLLAMA_BINARY, "pull", OLLAMA_MODEL])
                print_fn(f"  ✓ Model {OLLAMA_MODEL} available")
            except Exception as exc:      # never fatal (D42)
                print_fn(
                    f"  ! Could not run `{OLLAMA_BINARY} pull` ({exc})."
                    f" Run it manually, then Reverie works offline.")
    else:
        env_var = providers.key_env_for(entry, environ)
        if env_var:
            print_fn(f"  ✓ {env_var} is set")
        elif entry.key_required:
            # review fix 43: this check runs in the SAME process as the ping,
            # so name the variable, where it was checked, and how to make it
            # visible here without touching any real shell config.
            print_fn(f"  ✗ {entry.api_key_env} is not set in this process's "
                     "environment — Reverie's LLM calls will fail until it is.")
            print_fn(f"      {_export_hint(entry)}")
            print_fn("      Then open a new terminal (or run `source "
                     "~/.zshrc` / `source ~/.bashrc` for your shell) and "
                     "re-run `reverie install`.")
    if entry.key != "ollama" and ollama_path:
        print_fn(_OLLAMA_FALLBACK_NOTE)


def _live_ping(config) -> tuple[bool, str]:
    """One cheap live call: the model must answer a short prompt."""
    try:
        out = call_llm("Reply with the single word: OK", temperature=0.0,
                       max_tokens=64, config=config)
    except Exception as exc:          # report anything — never fatal
        return False, str(exc)
    first = out.strip().splitlines()[0] if out.strip() else "(empty response)"
    return True, first[:40]


def _verify_provider(reverie_home: Path, *, dry_run: bool, no_verify: bool,
                     verify_fn, print_fn) -> bool:
    """One live ping against the freshly written config (skippable).

    Returns True when the provider answered (or verification was skipped)
    and False only for a real ping failure — the caller reports "Reverie is
    installed, but the LLM is not reachable" and exits non-zero (review
    fix 1). The model name in the message comes from the config that was
    actually written (review fix 2)."""
    if dry_run:
        print_fn("  [dry-run] would verify the provider with one cheap "
                 "live ping (skipped)")
        return True
    if no_verify:
        print_fn("  (live verification skipped — --no-verify)")
        return True
    config = load_config(reverie_home / "config.yaml")
    model = config.llm.model
    print_fn(f"  Verifying {model} with a live ping...")
    ping = verify_fn or _live_ping
    ok, detail = ping(config)
    if ok:
        print_fn(f"  ✓ {detail!r} — {model} is reachable")
        return True
    print_fn(f"  ✗ Ping failed for {model}: {detail}")
    print_fn("    The provider is configured; fix the key or model name "
             "and re-run `reverie install --provider <name>`.")
    return False


def _provider_menu(input_fn, print_fn, environ) -> str | None:
    """Interactive provider picker. Returns a registry key, or None (skip)."""
    print_fn("\nSelect the LLM provider Reverie should use:")
    options: list[str | None] = []
    for key in providers.MENU_KEYS:
        if key == "ollama":
            continue                  # offered last, before skip
        entry = providers.REGISTRY[key]
        marked = ""
        if entry.api_key_env and providers.key_env_for(entry, environ):
            marked = "  ✓ key set"
        print_fn(f"  [{len(options) + 1}] {entry.name}{marked}")
        options.append(key)
    ollama = providers.REGISTRY["ollama"]
    print_fn(f"  [{len(options) + 1}] Local via Ollama ({ollama.default_model})")
    options.append("ollama")
    print_fn(f"  [{len(options) + 1}] Skip for now")
    options.append(None)
    try:
        raw = input_fn("\nEnter number: ")
    except EOFError:
        raw = ""
    try:
        idx = int(raw.strip())
    except ValueError:
        return None
    if 1 <= idx <= len(options):
        return options[idx - 1]
    return None


#: The distribution name importlib.metadata looks up (pyproject.toml).
DIST_NAME = "reverie"
#: Console script launching the MCP server (pyproject [project.scripts]).
MCP_CONSOLE_SCRIPT = "reverie-mcp"
#: The `reverie` console script binding this build expects (pyproject.toml).
#: feat/flow moved `reverie` from the installer to the multi-command CLI
#: (reverie.cli:main); an older install still binds `ec = reverie.install:main`.
REVERIE_CLI_ENTRY = "reverie.cli:main"


def _is_pip_installed() -> bool:
    """D45: True when the ``reverie`` package is pip-installed (editable counts),
    so the ``reverie-mcp`` console script exists and agent configs can launch the
    server with it instead of an absolute venv-python + PYTHONPATH."""
    try:
        from importlib import metadata
        metadata.version(DIST_NAME)
        return True
    except Exception:            # not installed / metadata unreadable
        return False


def stale_entry_point_note() -> str | None:
    """Detect a stale `reverie` console-script binding (feat/flow review fix 9).

    Console scripts only update on reinstall: an install made before the
    multi-command CLI keeps ``reverie = reverie.install:main``, so subcommands like
    `reverie status` fall through to the installer. Returns the note to print,
    or None when the binding is current (or metadata is unreadable)."""
    try:
        from importlib import metadata
        for ep in metadata.entry_points(group="console_scripts"):
            if ep.name == "reverie" and ep.value != REVERIE_CLI_ENTRY:
                return (
                    "Note: your `reverie` command is bound to the OLD entry "
                    f"point ({ep.value}) — the new subcommands (enable, "
                    "disable, repos, review, status, export, import, "
                    "reset, uninstall) do not exist for it yet. Fix: "
                    "re-run `pip install -e .` (or `pip install -U "
                    "reverie`) — console scripts only update on reinstall."
                )
    except Exception:            # metadata unreadable — say nothing
        return None
    return None


def _console_script_candidates(executable: str | None = None) -> list[Path]:
    """Absolute candidate locations of the pip-generated ``reverie-mcp`` script.

    pip puts console scripts beside the environment's interpreter: ``bin/`` in
    a POSIX venv, pipx or uv-tool environment, ``Scripts/`` on Windows. A
    system install puts them in ``Scripts/`` under the interpreter directory.
    """
    exe_dir = Path(executable or sys.executable).parent
    if os.name == "nt":
        return [
            exe_dir / f"{MCP_CONSOLE_SCRIPT}.exe",          # venv Scripts dir
            exe_dir / f"{MCP_CONSOLE_SCRIPT}.cmd",
            exe_dir / "Scripts" / f"{MCP_CONSOLE_SCRIPT}.exe",  # system install
            exe_dir / "Scripts" / f"{MCP_CONSOLE_SCRIPT}.cmd",
            exe_dir / MCP_CONSOLE_SCRIPT,
            exe_dir / "Scripts" / MCP_CONSOLE_SCRIPT,
        ]
    return [exe_dir / MCP_CONSOLE_SCRIPT]


def console_script_path(executable: str | None = None, which=shutil.which,
                        ) -> str | None:
    """Absolute path of the installed ``reverie-mcp`` console script, or None.

    Resolved from the RUNNING interpreter's environment (``sys.executable``):
    venv, pipx, uv tool and system installs all place the script beside the
    interpreter. ``which`` is the fallback for layouts where the scripts
    directory is elsewhere (e.g. ``pip install --user``).
    """
    for candidate in _console_script_candidates(executable):
        if candidate.is_file():
            return str(candidate)
    found = which(MCP_CONSOLE_SCRIPT)
    if found:
        return str(Path(found).resolve())
    return None


def server_command(executable: str | None = None, which=shutil.which,
                   ) -> tuple[str, list[str], dict[str, str]]:
    """``(command, args, env)`` for launching the Reverie MCP server from any cwd.

    D45: when the package is pip-installed, agent configs carry the ABSOLUTE
    path of the ``reverie-mcp`` console script belonging to the running
    interpreter's environment (``console_script_path``). Harnesses launch MCP
    servers outside the user's virtualenv, so a bare ``reverie-mcp`` only
    resolves when it happens to be on the global PATH — with a venv or pipx
    install it is not, and the server never starts. If the script cannot be
    located at all, the running interpreter + ``-m reverie.mcp_server`` (it
    can import the package — this code runs from it) is the fallback, which
    the install-time server check then verifies. NOTE (deviation from design
    doc §6.1): the doc's literal ``"reverie"`` command would launch the
    INSTALLER (``reverie.install:main``), not the server — hence the
    dedicated ``reverie-mcp`` script.

    Development fallback (D28/B2): a bare interpreter could not even import
    ``reverie``, so the configs carry the ABSOLUTE path to the project's venv
    interpreter (dynamically computed from this file's location — never
    hardcoded) plus a PYTHONPATH entry pointing at the project root.
    """
    if _is_pip_installed():
        script = console_script_path(executable, which)
        if script is not None:
            return script, [], {}
        return executable or sys.executable, ["-m", "reverie.mcp_server"], {}
    root = Path(__file__).resolve().parent.parent
    python: str | None = None
    for candidate in (root / ".venv" / "bin" / "python",          # POSIX venv
                      root / ".venv" / "Scripts" / "python.exe"):  # Windows venv
        if candidate.is_file():
            python = str(candidate)
            break
    if python is None:   # no project venv — the running interpreter has `reverie`
        python = sys.executable
    return python, ["-m", "reverie.mcp_server"], {"PYTHONPATH": str(root)}


def _stop_process(proc: subprocess.Popen) -> None:
    """Terminate a check subprocess; kill it when terminate is not enough."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _stderr_tail(proc: subprocess.Popen, limit: int = 300) -> str:
    """The (already-exited) process's stderr tail, for the ✗ report."""
    try:
        err = proc.stderr.read() if proc.stderr else ""
    except (OSError, ValueError):
        return ""
    err = (err or "").strip()
    return err[-limit:] if err else ""


def check_server(command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, *,
                 timeout: float = 10.0, popen=subprocess.Popen,
                 ) -> tuple[bool, str]:
    """Launch ``command`` exactly as written to the harness configs, send an
    MCP ``initialize``, wait up to ``timeout`` seconds for the response, then
    stop the server. Returns ``(ok, detail)``.

    This is the install-time proof that the command just written actually
    starts a server: a broken setup is reported as ✗ (with the command and
    the error) and never ends with "Reverie is ready!". ``popen`` is
    injectable for tests.
    """
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "reverie-install-check", "version": "1"},
        },
    }) + "\n"
    launch_env = dict(os.environ)
    if env:
        launch_env.update(env)
    try:
        proc = popen([command, *(args or [])], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, env=launch_env)
    except OSError as exc:
        return False, f"could not launch: {exc}"
    response: list[str] = []

    def _read() -> None:
        try:
            response.append(proc.stdout.readline())
        except (OSError, ValueError):
            response.append("")

    reader = threading.Thread(target=_read, daemon=True)
    try:
        try:
            proc.stdin.write(request)
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            return False, f"could not send initialize: {exc}"
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            _stop_process(proc)
            return False, f"no MCP response within {timeout:.0f}s"
        raw = response[0] if response else ""
        if not raw:
            _stop_process(proc)
            tail = _stderr_tail(proc)
            detail = "the server exited without responding"
            if tail:
                detail += f" — {tail.splitlines()[-1]}"
            return False, detail
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return False, f"non-JSON response: {raw.strip()[:120]!r}"
        name = None
        if isinstance(message, dict):
            result = message.get("result")
            if isinstance(result, dict):
                info = result.get("serverInfo")
                if isinstance(info, dict):
                    name = info.get("name")
        if not name:
            return False, f"unexpected MCP response: {raw.strip()[:120]!r}"
        return True, f"server started ({name})"
    finally:
        _stop_process(proc)


class InstallError(RuntimeError):
    """Installation failure with actionable guidance (§28.4 message style)."""


# ---------------------------------------------------------------------------
# planned writes — computed pure, applied (or printed) separately
# ---------------------------------------------------------------------------

@dataclass
class PlannedWrite:
    path: Path
    action: str                # create | merge | append | update | unchanged | create-db
    content: str | None        # full resulting content (None for unchanged/create-db)
    detail: str                # transcript-style note for the ✓ line


def _display(path: Path, home: Path) -> str:
    """`~`-relative display for paths under the user's home."""
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


#: Where install records the files IT created (ABS path → sha256 at write
#: time). Uninstall deletes such a file only while the hash still matches;
#: a file that existed before install is never recorded, so it is never
#: deleted — only Reverie's entry is removed from it (review fix 39).
INSTALL_MANIFEST_NAME = "install_manifest.json"


def install_manifest_path(reverie_home: Path) -> Path:
    return Path(reverie_home) / INSTALL_MANIFEST_NAME


def load_install_manifest(reverie_home: Path) -> dict[str, str]:
    """The installer-created file hashes ({} when absent/unreadable)."""
    try:
        data = json.loads(install_manifest_path(reverie_home).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {k: v for k, v in files.items() if isinstance(v, str)}


def _record_install_manifest(reverie_home: Path,
                             created_paths: list[Path]) -> None:
    """Record sha256 hashes for files this run CREATED. Existing entries
    keep their original hash (a user edit must never be re-owned), so this
    is merge-only and idempotent."""
    files = load_install_manifest(reverie_home)
    for path in created_paths:
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            continue
        files.setdefault(str(Path(path).resolve()), digest)
    manifest = install_manifest_path(reverie_home)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps({"version": 1, "files": files}, indent=2) + "\n",
        encoding="utf-8")


def _json_merge(path: Path, home: Path, section: str, entry: dict,
                detail: str, schema: str | None = None) -> PlannedWrite:
    """Merge ``entry`` into ``data[section]["reverie"]`` — never overwriting siblings."""
    disp = _display(path, home)
    existed = path.exists()
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(
                f"{disp} is not valid JSON ({exc}). Fix or remove it, then "
                "re-run `python -m reverie.install`. No changes were made to it."
            ) from exc
        if not isinstance(data, dict):
            raise InstallError(
                f"{disp} must contain a JSON object. Fix or remove it, then "
                "re-run `python -m reverie.install`. No changes were made to it."
            )
    else:
        data = {}
        if schema is not None:
            data["$schema"] = schema
    node = data.setdefault(section, {})
    if not isinstance(node, dict):
        raise InstallError(
            f"{disp}: `{section}` is not a JSON object. Fix or remove it, "
            "then re-run `python -m reverie.install`. No changes were made to it."
        )
    if node.get("reverie") == entry:
        return PlannedWrite(path, "unchanged", None,
                            f"{disp} already has the reverie MCP server — unchanged")
    node["reverie"] = entry
    action = "merge" if existed else "create"
    return PlannedWrite(path, action, json.dumps(data, indent=2) + "\n", detail)


def _line_once(path: Path, home: Path, line: str, detail: str) -> PlannedWrite:
    """Append ``line`` to a text file, creating it if absent (idempotent)."""
    disp = _display(path, home)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if line in (existing.strip() for existing in text.splitlines()):
            return PlannedWrite(path, "unchanged", None,
                                f"{disp} already references {line} — unchanged")
        new = text if text.endswith("\n") else text + "\n"
        if text.strip():
            new += "\n"
        return PlannedWrite(path, "append", new + line + "\n", detail)
    return PlannedWrite(path, "create", line + "\n", detail)


def _block_once(path: Path, home: Path, marker: str, block: str,
                detail: str) -> PlannedWrite:
    """Append a markdown block containing ``marker`` (idempotent)."""
    disp = _display(path, home)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if marker in text:
            return PlannedWrite(path, "unchanged", None,
                                f"{disp} already references {marker} — unchanged")
        new = text if text.endswith("\n") else text + "\n"
        if text.strip():
            new += "\n"
        return PlannedWrite(path, "append", new + block, detail)
    return PlannedWrite(path, "create", block, detail)


def _codex_table() -> str:
    """The ``[mcp_servers.reverie]`` TOML table (console script in pip installs;
    absolute python + PYTHONPATH env otherwise).

    ``json.dumps`` strings/arrays are valid TOML basic strings/arrays for
    ordinary paths, and handle any quoting/escaping correctly.
    """
    python, args, env = server_command()
    lines = ["[mcp_servers.reverie]", f"command = {json.dumps(python)}"]
    if args:
        lines.append(f"args = {json.dumps(args)}")
    if env:
        lines.append(f"env = {{ PYTHONPATH = {json.dumps(env['PYTHONPATH'])} }}")
    return "\n".join(lines)


def _mcp_entry(command: str, args: list[str], env: dict,
               extra: dict | None = None) -> dict:
    """JSON MCP-server entry; empty args/env keys are omitted (D45 writes the
    absolute console-script path as just {"command": "/abs/path/reverie-mcp"})."""
    entry = dict(extra or {})
    entry["command"] = command
    if args:
        entry["args"] = list(args)
    if env:
        entry["env"] = dict(env)
    return entry


# ---------------------------------------------------------------------------
# reading the configured server entry back (install verification + status)
# ---------------------------------------------------------------------------

@dataclass
class ConfiguredServer:
    """One harness config's reverie MCP server entry, read back from disk —
    the exact command that harness will launch."""
    agent: str                  # AgentSpec.key
    config_path: Path
    command: str
    args: list[str]
    env: dict[str, str]


def _load_json_object(path: Path) -> dict | None:
    """Parse a JSON config file; None when absent, unreadable or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _server_from_entry(agent: str, path: Path,
                       entry: object) -> ConfiguredServer | None:
    """Normalize a JSON/TOML MCP entry (``command`` string or argv list) into
    a ConfiguredServer; None when the shape is not a launchable command."""
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    args: list[str] = []
    if isinstance(command, list):
        if not command or not all(isinstance(token, str) for token in command):
            return None
        command, args = command[0], list(command[1:])
    if not isinstance(command, str) or not command:
        return None
    raw_args = entry.get("args")
    if isinstance(raw_args, list) and all(isinstance(a, str) for a in raw_args):
        args = [*args, *raw_args]
    raw_env = entry.get("env") or entry.get("environment") or {}
    env = ({k: v for k, v in raw_env.items()
            if isinstance(k, str) and isinstance(v, str)}
           if isinstance(raw_env, dict) else {})
    return ConfiguredServer(agent, path, command, args, env)


def _json_configured_server(agent: str, path: Path,
                            keys: tuple[str, ...]) -> ConfiguredServer | None:
    node: object = _load_json_object(path)
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return _server_from_entry(agent, path, node)


def configured_servers(home: Path | None = None) -> list[ConfiguredServer]:
    """Every harness config's reverie server entry, in AGENTS order.

    Used by the install-time check (launch what was just written) and by
    ``reverie status`` (does that command still exist?)."""
    home = Path(home) if home else Path.home()
    found: list[ConfiguredServer] = []
    for agent in AGENTS:
        server = agent.configured_server(home)
        if server is not None:
            found.append(server)
    return found


def _command_exists(command: str, which=shutil.which) -> bool:
    """True when a configured command can be launched: an existing file for a
    path, or a resolvable name on PATH."""
    separators = {sep for sep in (os.sep, os.altsep, "/") if sep}
    if Path(command).is_absolute() or any(s in command for s in separators):
        return Path(command).is_file()
    return which(command) is not None


def report_configured_servers(print_fn=print, home: Path | None = None,
                              which=shutil.which) -> bool:
    """``reverie status``: verify every configured harness's server command
    still exists. A moved or deleted venv breaks an absolute path — flag it
    and tell the user to re-run `reverie install`. Returns False on any
    missing command; prints nothing when no harness is configured."""
    servers = configured_servers(home)
    if not servers:
        return True
    ok_all = True
    print_fn("MCP server command:")
    for server in servers:
        agent = _AGENTS_BY_KEY.get(server.agent)
        name = agent.name if agent else server.agent
        if _command_exists(server.command, which):
            print_fn(f"  ✓ {name}: {server.command}")
        else:
            ok_all = False
            print_fn(f"  ✗ {name}: {server.command} is missing — the "
                     "virtualenv was moved or deleted. Run `reverie install` "
                     "again.")
    return ok_all


def _codex_toml(path: Path, home: Path, detail: str) -> PlannedWrite:
    """Upsert the ``[mcp_servers.reverie]`` table; every other byte is preserved."""
    disp = _display(path, home)
    table = _codex_table()
    if not path.exists():
        return PlannedWrite(path, "create", table + "\n", detail)
    text = path.read_text(encoding="utf-8")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(
            f"{disp} is not valid TOML ({exc}). Fix or remove it, then "
            "re-run `python -m reverie.install`. No changes were made to it."
        ) from exc
    lines = text.splitlines()
    start = next(
        (i for i, table_line in enumerate(lines)
         if table_line.strip() == "[mcp_servers.reverie]"),
        None,
    )
    if start is None:
        new = text if text.endswith("\n") else text + "\n"
        if text.strip():
            new += "\n"
        new += table + "\n"
    else:
        end = next(
            (i for i in range(start + 1, len(lines))
             if lines[i].strip().startswith("[")),
            len(lines),
        )
        replacement = table.splitlines()
        if end < len(lines):
            replacement.append("")          # keep a blank line before the next table
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1                        # drop the old table's trailing blanks
        new = "\n".join(lines[:start] + replacement + lines[end:]) + "\n"
    if new == text:
        return PlannedWrite(path, "unchanged", None,
                            f"{disp} already has [mcp_servers.reverie] — unchanged")
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as exc:  # defensive — should never happen
        raise InstallError(
            f"refusing to write {disp}: merged TOML would not parse ({exc}). "
            "No changes were made to it."
        ) from exc
    return PlannedWrite(path, "merge", new, detail)


def _reference_block(agents_ref: str) -> str:
    return (
        f"## Reverie\n\n"
        f"Follow the Reverie agent instructions at {agents_ref}. Reverie is "
        f"an MCP server (`reverie`) providing engineering memory via "
        f"observe, query, summary, "
        f"reconsolidate, set_repo_state, "
        f"review_list, and review_decide. Sessions are "
        f"automatic per branch — never suggest starting or stopping a "
        f"session. Pass the workspace path as repo_path on every call. The "
        f"user reviews candidates via `reverie review` or by asking the "
        f"agent to show them; the agent only relays decisions the user "
        f"states.\n"
    )


#: Marker line identifying Reverie's reference block in AGENTS.md-style
#: files (install appends it; uninstall only removes the exact block).
REFERENCE_BLOCK_MARKER = "## Reverie"


# ---------------------------------------------------------------------------
# agent registry (§28.8 "Installation Logic per Agent")
# ---------------------------------------------------------------------------

@dataclass
class AgentSpec:
    key: str
    name: str
    binary: str
    marker: str                     # home-relative path; existence ⇒ installed
    next_step: str = ""             # printed in the end-of-install summary

    def detected(self, home: Path, which) -> bool:
        return which(self.binary) is not None or (home / self.marker).exists()

    def plan(self, home: Path, reverie_home: Path) -> list[PlannedWrite]:
        raise NotImplementedError

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        """The reverie server entry currently in this harness's config, or
        None when the file is absent/unreadable or carries no entry."""
        return None


def _agents_ref(home: Path, reverie_home: Path) -> str:
    """How agent configs refer to the Reverie AGENTS.md (spec form when default)."""
    return "~/.reverie/AGENTS.md" if reverie_home == home / ".reverie" else str(reverie_home / "AGENTS.md")


class _ClaudeCode(AgentSpec):
    def __init__(self):
        super().__init__(
            "claude_code", "Claude Code", "claude", ".claude.json",
            next_step="restart Claude Code; the tools load in every project, "
                      "/reverie-review and /reverie-status are available in "
                      "the chat.")

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        return _json_configured_server("claude_code", home / ".claude.json",
                                       ("mcpServers", "reverie"))

    def plan(self, home: Path, reverie_home: Path) -> list[PlannedWrite]:
        ref = _agents_ref(home, reverie_home)
        python, args, env = server_command()
        plans = [
            _json_merge(
                home / ".claude.json", home, "mcpServers",
                _mcp_entry(python, args, env, {"type": "stdio"}),
                f"Added MCP server to {_display(home / '.claude.json', home)} "
                "(user scope)",
            ),
            _line_once(
                home / ".claude" / "CLAUDE.md", home, f"@{ref}",
                f"Added @{ref} import to "
                f"{_display(home / '.claude' / 'CLAUDE.md', home)}",
            ),
        ]
        # user-level slash commands (https://code.claude.com/docs/en/slash-commands)
        for name, body in CLAUDE_CODE_COMMANDS.items():
            path = home / ".claude" / "commands" / name
            plans.append(_verbatim_file(
                path, home, body,
                f"Wrote {_display(path, home)} "
                f"(/{'reverie-review' if 'review' in name else 'reverie-status'})"))
        return plans


class _Cursor(AgentSpec):
    def __init__(self):
        super().__init__(
            "cursor", "Cursor", "cursor", ".cursor/mcp.json",
            next_step="restart Cursor; the tools load in every project "
                      "(usage guidance arrives via the server instructions "
                      "and tool descriptions — paste them into Settings → "
                      "Rules if you want them always on), /reverie-review "
                      "and /reverie-status are available in the chat "
                      "(commands are beta).")

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        return _json_configured_server("cursor",
                                       home / ".cursor" / "mcp.json",
                                       ("mcpServers", "reverie"))

    def plan(self, home: Path, reverie_home: Path) -> list[PlannedWrite]:
        python, args, env = server_command()
        plans = [
            _json_merge(
                home / ".cursor" / "mcp.json", home, "mcpServers",
                _mcp_entry(python, args, env),
                f"Added MCP server to "
                f"{_display(home / '.cursor' / 'mcp.json', home)}",
            ),
        ]
        # No global rules file is written: Cursor's documented global
        # instruction mechanisms are User Rules (in-app settings) and
        # AGENTS.md/CLAUDE.md files inside repos — never write into the
        # user's repos. The MCP server's `instructions` field + tool
        # descriptions carry the usage contract instead.
        # User-level commands (beta): ~/.cursor/commands/<name>.md
        # https://cursor.com/docs/agent/chat/commands
        for name, body in CURSOR_COMMANDS.items():
            path = home / ".cursor" / "commands" / name
            plans.append(_verbatim_file(
                path, home, body,
                f"Wrote {_display(path, home)} "
                f"(/{'reverie-review' if 'review' in name else 'reverie-status'})"))
        return plans


class _OpenCode(AgentSpec):
    def __init__(self):
        super().__init__(
            "opencode", "OpenCode", "opencode",
            ".config/opencode/opencode.json",
            next_step="restart OpenCode; /reverie-review and /reverie-status "
                      "are available in the chat.")

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        return _json_configured_server(
            "opencode", home / ".config" / "opencode" / "opencode.json",
            ("mcp", "reverie"))

    def plan(self, home: Path, reverie_home: Path) -> list[PlannedWrite]:
        ref = _agents_ref(home, reverie_home)
        python, args, env = server_command()
        cfg = home / ".config" / "opencode" / "opencode.json"
        agents = home / ".config" / "opencode" / "AGENTS.md"
        entry = _mcp_entry([python, *args], [], env,
                           {"type": "local", "enabled": True})
        if "env" in entry:               # OpenCode names the key differently
            entry["environment"] = entry.pop("env")
        plans = [
            _json_merge(
                cfg, home, "mcp", entry,
                f"Added MCP server to {_display(cfg, home)}",
                schema="https://opencode.ai/config.json",
            ),
            _block_once(
                agents, home, ref, _reference_block(ref),
                f"Wrote {_display(agents, home)} (references {ref})",
            ),
        ]
        # user-level commands (https://opencode.ai/docs/commands/ — note the
        # plural `commands/` directory; markdown + YAML frontmatter)
        for name, body in OPENCODE_COMMANDS.items():
            path = home / ".config" / "opencode" / "commands" / name
            plans.append(_verbatim_file(
                path, home, body,
                f"Wrote {_display(path, home)} "
                f"(/{'reverie-review' if 'review' in name else 'reverie-status'})"))
        return plans


class _Codex(AgentSpec):
    def __init__(self):
        super().__init__(
            "codex", "Codex", "codex", ".codex/config.toml",
            next_step="restart Codex CLI; /prompts:reverie-review and "
                      "/prompts:reverie-status are available (custom "
                      "prompts are deprecated upstream but work).")

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        path = home / ".codex" / "config.toml"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return None
        return _server_from_entry("codex", path,
                                  (data.get("mcp_servers") or {}).get("reverie"))

    def plan(self, home: Path, reverie_home: Path) -> list[PlannedWrite]:
        ref = _agents_ref(home, reverie_home)
        cfg = home / ".codex" / "config.toml"
        agents = home / ".codex" / "AGENTS.md"
        plans = [
            _codex_toml(cfg, home,
                        f"Added [mcp_servers.reverie] to {_display(cfg, home)}"),
            _block_once(
                agents, home, ref, _reference_block(ref),
                f"Wrote {_display(agents, home)} (references {ref})",
            ),
        ]
        # user-level custom prompts (invoked as /prompts:<name>) — deprecated
        # upstream in favour of skills, but still the user-level command
        # mechanism; https://developers.openai.com/codex/custom-prompts
        for name, body in CODEX_PROMPTS.items():
            path = home / ".codex" / "prompts" / name
            plans.append(_verbatim_file(
                path, home, body,
                f"Wrote {_display(path, home)} "
                f"(/prompts:{'.reverie-review' if 'review' in name else 'reverie-status'})"))
        return plans


class _GeminiCLI(AgentSpec):
    def __init__(self):
        super().__init__(
            "gemini_cli", "Gemini CLI", "gemini", ".gemini/settings.json",
            next_step="restart Gemini CLI; /reverie-review and "
                      "/reverie-status are available in the chat.")

    def configured_server(self, home: Path) -> ConfiguredServer | None:
        return _json_configured_server("gemini_cli",
                                       home / ".gemini" / "settings.json",
                                       ("mcpServers", "reverie"))

    def plan(self, home: Path, reverie_home: Path) -> list[PlannedWrite]:
        ref = _agents_ref(home, reverie_home)
        python, args, env = server_command()
        plans = [
            _json_merge(
                home / ".gemini" / "settings.json", home, "mcpServers",
                _mcp_entry(python, args, env),
                f"Added MCP server to "
                f"{_display(home / '.gemini' / 'settings.json', home)}",
            ),
            _block_once(
                home / ".gemini" / "GEMINI.md", home, ref,
                _reference_block(ref),
                f"Wrote {_display(home / '.gemini' / 'GEMINI.md', home)} "
                f"(references {ref})",
            ),
        ]
        # user-level commands: ~/.gemini/commands/<name>.toml (TOML, required
        # `prompt`); https://geminicli.com/docs/cli/custom-commands
        for name, body in GEMINI_COMMANDS.items():
            path = home / ".gemini" / "commands" / name
            plans.append(_verbatim_file(
                path, home, body,
                f"Wrote {_display(path, home)} "
                f"(/{'reverie-review' if 'review' in name else 'reverie-status'})"))
        return plans


AGENTS: list[AgentSpec] = [_ClaudeCode(), _Cursor(), _OpenCode(), _Codex(),
                           _GeminiCLI()]
_AGENTS_BY_KEY = {a.key: a for a in AGENTS}


# ---------------------------------------------------------------------------
# ~/.reverie bootstrap (§28.2)
# ---------------------------------------------------------------------------

def _reverie_home_plans(home: Path, reverie_home: Path) -> list[PlannedWrite]:
    db = reverie_home / "reverie.db"
    plans = [
        PlannedWrite(
            db, "unchanged" if db.exists() else "create-db", None,
            f"{_display(db, home)} already exists — unchanged" if db.exists()
            else f"Created {_display(db, home)} (empty brain, initialised)",
        ),
    ]
    cfg = reverie_home / "config.yaml"
    if cfg.exists():
        plans.append(PlannedWrite(
            cfg, "unchanged", None,
            f"{_display(cfg, home)} already exists — kept"))
    else:
        plans.append(PlannedWrite(
            cfg, "create",
            yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False),
            f"Created {_display(cfg, home)} (default configuration)"))
    agents_md = reverie_home / "AGENTS.md"
    plans.append(_verbatim_file(
        agents_md, home, AGENTS_MD,
        f"Wrote {_display(agents_md, home)} (agent instructions)"))
    return plans


def _verbatim_file(path: Path, home: Path, content: str,
                   detail: str) -> PlannedWrite:
    """Spec-owned file (D25): always written verbatim; unchanged if equal."""
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == content:
        return PlannedWrite(
            path, "unchanged", None,
            f"{_display(path, home)} already up to date — unchanged")
    return PlannedWrite(
        path, "update" if path.exists() else "create", content, detail)


def _apply(write: PlannedWrite) -> None:
    if write.action == "unchanged":
        return
    if write.action == "create-db":
        write.path.parent.mkdir(parents=True, exist_ok=True)
        Brain(write.path).close()          # auto-creates with the full schema
        return
    write.path.parent.mkdir(parents=True, exist_ok=True)
    write.path.write_text(write.content or "", encoding="utf-8")


# ---------------------------------------------------------------------------
# interactive selection (§28.8 transcript)
# ---------------------------------------------------------------------------

def _select_interactive(detected: list[AgentSpec], input_fn, print_fn
                        ) -> list[AgentSpec]:
    try:
        answer = input_fn("\nAdd Reverie to all detected agents? (y/n): ")
    except EOFError:
        answer = ""
    if answer.strip().lower().startswith("y"):
        return list(detected)
    print_fn("\nSelect agents to configure:")
    for i, agent in enumerate(detected, 1):
        print_fn(f"  [{i}] {agent.name}")
    try:
        raw = input_fn("\nEnter numbers (comma-separated): ")
    except EOFError:
        raw = ""
    chosen = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            idx = int(token)
        except ValueError:
            print_fn(f"  Ignoring invalid selection: {token!r}")
            continue
        if 1 <= idx <= len(detected):
            if detected[idx - 1] not in chosen:
                chosen.append(detected[idx - 1])
        else:
            print_fn(f"  Ignoring invalid selection: {token!r}")
    return chosen


# ---------------------------------------------------------------------------
# the installer
# ---------------------------------------------------------------------------

def run(*, home: Path | None = None, reverie_home: Path | None = None,
        all_agents: bool = False, only: list[str] | None = None,
        dry_run: bool = False, provider: str | None = None,
        reconfigure: bool = False,
        no_verify: bool = False, verify_fn=None, import_file: str | None = None,
        check_fn=None, input_fn=input, print_fn=print,
        which=shutil.which, environ=os.environ,
        runner=subprocess.run) -> int:
    """§28.8 `reverie install`. Returns 0 on success, raises InstallError otherwise.

    ``provider`` (--provider) selects the LLM provider non-interactively
    (registry key; ``ollama`` runs the D42 local setup). ``reconfigure``
    (--reconfigure) asks the provider question again even when
    ``config.yaml`` already carries an ``llm`` block; without it an
    existing block is kept and only re-verified (review fix 44).
    ``no_verify`` skips the live ping; ``verify_fn`` (returning
    ``(ok, detail)``) replaces the ping in tests. A failed ping makes the
    run return 1 with "Reverie is installed, but the LLM is not
    reachable" — no "Reverie is ready!" (review fix 1). ``runner``
    executes the Ollama model pull (injectable;
    failures are reported, never fatal). ``check_fn`` replaces the
    end-of-install MCP server check in tests (signature ``(command, args,
    env) -> (ok, detail)``; the real one launches each written command). A
    failed server check makes the run return 1 and suppresses "Reverie is
    ready!". ``import_file`` (``--import``)
    restores a brain export during setup on a new machine (feat/flow D58):
    allowed into the fresh (empty) brain this installer just bootstrapped;
    when the local brain already holds data the import refuses with
    guidance to run `reverie import --replace` manually.
    """
    home = Path(home) if home else Path.home()
    reverie_home = Path(reverie_home) if reverie_home else _default_reverie_home()
    # Read BEFORE anything is written: only a pre-existing llm block counts
    # as user configuration (review fix 44).
    existing_llm = _existing_llm_config(reverie_home)

    print_fn("\nReverie — Installation\n")
    if dry_run:
        print_fn("(dry run — showing exactly what would be written; "
                 "no files will be written)\n")

    # -- detection ---------------------------------------------------------
    print_fn("Detecting installed coding agents...")
    detected = []
    for agent in AGENTS:
        found = agent.detected(home, which)
        print_fn(f"  {'✓' if found else '✗'} {agent.name} "
                 f"{'found' if found else 'not found'}")
        if found:
            detected.append(agent)

    # -- selection ---------------------------------------------------------
    if only is not None:
        unknown = [k for k in only if k not in _AGENTS_BY_KEY]
        if unknown:
            raise InstallError(
                f"unknown agent(s): {', '.join(unknown)}. "
                f"Known agents: {', '.join(sorted(_AGENTS_BY_KEY))}"
            )
        selected = [a for a in AGENTS if a.key in only]
        for agent in selected:
            if agent not in detected:
                print_fn(f"  (note: {agent.name} was not detected — "
                         "configuring anyway)")
    elif all_agents:
        selected = list(detected)
    elif detected:
        selected = _select_interactive(detected, input_fn, print_fn)
    else:
        selected = []
    if not detected and only is None:
        print_fn("  No supported agents detected. Configure one anyway with "
                 "--only (e.g. `python -m reverie.install --only claude_code`).")

    # -- per-agent configuration -------------------------------------------
    writes: list[tuple[str | None, PlannedWrite]] = []
    for agent in selected:
        for write in agent.plan(home, reverie_home):
            writes.append((agent.name, write))

    current_agent: str | None = None
    created_paths: list[Path] = []
    for agent_name, write in writes:
        if agent_name != current_agent:
            print_fn(f"\nConfiguring {agent_name}...")
            current_agent = agent_name
        _emit(write, home, dry_run, print_fn)
        if not dry_run and write.action in ("create", "create-db"):
            created_paths.append(write.path)

    # -- ~/.reverie bootstrap (§28.2) -------------------------------------------
    # review fix 48: only announce directory creation when it happens.
    if not reverie_home.exists():
        print_fn(f"\nCreating {_display(reverie_home, home)}/ directory...")
    for write in _reverie_home_plans(home, reverie_home):
        _emit(write, home, dry_run, print_fn)
        if not dry_run and write.action in ("create", "create-db"):
            created_paths.append(write.path)

    # -- brain import (feat/flow --import, D58) -----------------------------
    if import_file is not None and not dry_run:
        _import_during_install(reverie_home, import_file, print_fn)

    # -- LLM provider (§28.8 + feat/providers) -----------------------------
    print_fn("\nChecking LLM provider...")
    ollama_path = which(OLLAMA_BINARY)

    chosen: providers.Provider | None = None
    verify_existing = False
    if provider is not None:
        entry = providers.get(provider)
        if entry is None or provider == "custom":
            raise InstallError(
                f"unknown provider {provider!r}. Known providers: "
                f"{', '.join(providers.provider_names())}"
            )
        chosen = entry
    elif existing_llm is not None and not reconfigure:
        # review fix 44: a config that already carries an llm block is the
        # user's setup — keep it and only re-verify. --provider and
        # --reconfigure are the explicit ways to change it.
        keep_provider = existing_llm.get("provider") or "(provider unset)"
        keep_model = existing_llm.get("model") or "(model unset)"
        print_fn(f"  · already configured for {keep_provider} — keeping "
                 f"{_display(reverie_home / 'config.yaml', home)}")
        print_fn(f"    ({keep_model}; pass --provider <name> or "
                 "--reconfigure to change it)")
        verify_existing = True
    else:
        detected = [
            key for key in providers.MENU_KEYS
            if providers.key_env_for(providers.REGISTRY[key], environ)
        ]
        for key in detected:
            entry = providers.REGISTRY[key]
            print_fn(f"  ✓ {providers.key_env_for(entry, environ)} detected "
                     f"({entry.name})")
        if len(detected) == 1:
            try:
                answer = input_fn(
                    f"\nConfigure Reverie for {providers.REGISTRY[detected[0]].name} "
                    f"using {providers.key_env_for(providers.REGISTRY[detected[0]], environ)}? "
                    "(Y/n): ")
            except EOFError:
                answer = ""
            if answer.strip().lower() in ("", "y", "yes"):
                chosen = providers.REGISTRY[detected[0]]
        if chosen is None:
            menu_choice = _provider_menu(input_fn, print_fn, environ)
            chosen = providers.get(menu_choice)

    ping_ok = True
    if chosen is not None:
        _configure_provider(reverie_home, home, chosen, dry_run=dry_run,
                            print_fn=print_fn, environ=environ,
                            runner=runner, ollama_path=ollama_path)
        ping_ok = _verify_provider(reverie_home, dry_run=dry_run,
                                   no_verify=no_verify, verify_fn=verify_fn,
                                   print_fn=print_fn)
    elif verify_existing:
        ping_ok = _verify_provider(reverie_home, dry_run=dry_run,
                                   no_verify=no_verify, verify_fn=verify_fn,
                                   print_fn=print_fn)
    else:
        print_fn("\nNo provider configured — Reverie's LLM features stay off "
                 "until one is set.")
        print_fn(_provider_guidance())
        if ollama_path:
            print_fn("\n  To set up Ollama later, re-run `reverie install` and "
                     "choose \"Local via Ollama\", or set these keys in "
                     "~/.reverie/config.yaml:")
            print_fn(_OLLAMA_CONFIG_SNIPPET)
        else:
            print_fn(_OLLAMA_INSTALL_INSTRUCTIONS)

    # Record what this run created (folded in, hashes as written) so the
    # uninstaller can distinguish Reverie-created files from user files.
    if not dry_run:
        _record_install_manifest(reverie_home, created_paths)

    if dry_run:
        if selected:
            print_fn("\n[dry-run] would start each harness's written server "
                     "command, send an MCP initialize and check the response")
        print_fn("\nDry run complete — no files were written.")
        return 0

    # -- verify every written server command actually starts ---------------
    # The absolute path is the whole point of D45: prove it runs before
    # telling the user Reverie is ready. One launch per distinct command
    # (all harnesses share one); ✓/✗ printed per harness.
    if selected:
        checker = check_fn or check_server
        print_fn("\nVerifying the MCP server starts...")
        results: dict[tuple, tuple[bool, str]] = {}
        failures: list[str] = []
        for agent in selected:
            configured = agent.configured_server(home)
            if configured is None:
                print_fn(f"  ✗ {agent.name}: no reverie MCP entry found "
                         "after install")
                failures.append(agent.name)
                continue
            key = (configured.command, tuple(configured.args),
                   tuple(sorted(configured.env.items())))
            if key not in results:
                results[key] = checker(configured.command, configured.args,
                                       configured.env)
            ok, detail = results[key]
            print_fn(f"  {'✓' if ok else '✗'} {agent.name}: {detail}")
            if not ok:
                failures.append(agent.name)
                print_fn("      command: "
                         + " ".join([configured.command, *configured.args]))
        if failures:
            print_fn("\nSetup incomplete — the MCP server did not start for: "
                     + ", ".join(failures))
            print_fn("The harness entries above point at a command that does "
                     "not run. Fix it, then re-run `reverie install`.")
            return 1

    # -- LLM reachability decides success (review fix 1) -------------------
    if not ping_ok:
        print_fn("\nReverie is installed, but the LLM is not reachable.")
        print_fn("  The harness entries are in place. Fix the key or model "
                 "above, then re-run `reverie install` to re-verify.")
        return 1

    # -- per-harness summary: what was written, what to do next -----------
    print_fn("\nReverie is ready!\n")
    print_fn("Configured:")
    for agent in selected:
        agent_writes = [w for name, w in writes
                        if name == agent.name and w.action != "unchanged"]
        print_fn(f"  {agent.name}:")
        for w in agent_writes:
            print_fn(f"    ✓ {_display(w.path, home)}")
        if agent.next_step:
            print_fn(f"    Next: {agent.next_step}")
    if not selected:
        print_fn("  (no agents configured — run `reverie install --only "
                 "<agent>` when ready)")
    print_fn("\nCommands:")
    print_fn("  reverie review   Review pending candidates (terminal UI)")
    print_fn("  reverie enable / reverie disable / reverie repos")
    print_fn("  reverie export / reverie import / reverie reset")
    print_fn("\nHow to use:")
    print_fn("  1. Open your coding agent and restart it so the MCP server "
             "loads")
    print_fn("  2. Work normally — in an enabled repo, sessions are "
             "automatic per branch and never need closing")
    print_fn("  3. The agent asks once whether to turn Reverie on for a new "
             "repo (your answer is remembered)")
    print_fn("  4. Review candidates with `reverie review`, or say \"show my "
             "Reverie candidates\" in the agent")
    return 0


def _import_during_install(reverie_home: Path, import_file: str, print_fn) -> None:
    """feat/flow (D58): `reverie install --import <file>` — restore a brain
    export during setup on a new machine. Only an empty brain (the fresh
    bootstrap) accepts it; anything else refuses with guidance."""
    from . import transport
    brain = Brain(reverie_home / "reverie.db")
    try:
        if not transport._brain_is_empty(brain):
            raise InstallError(
                "The local brain already holds data, so `--import` would "
                "replace it. Run `reverie import <file> --replace` explicitly "
                "(it takes an automatic backup first).")
        transport.import_brain(brain, import_file, print_fn=print_fn)
    except transport.TransportError as exc:
        raise InstallError(str(exc)) from exc
    finally:
        brain.close()


def _emit(write: PlannedWrite, home: Path, dry_run: bool, print_fn) -> None:
    """Print a planned write (and its content in dry-run), then apply it."""
    if write.action == "unchanged":
        print_fn(f"  · {write.detail}")
        return
    if dry_run:
        print_fn(f"  [dry-run] {write.action}: {_display(write.path, home)}")
        if write.content is not None:
            for line in write.content.rstrip("\n").splitlines():
                print_fn(f"      {line}")
        return
    _apply(write)
    print_fn(f"  ✓ {write.detail}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m reverie.install",
        description="Reverie installation (§28.8): configure detected coding "
                    "agents and create the ~/.reverie directory.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true",
                       help="configure every detected agent (non-interactive)")
    group.add_argument("--only", default=None, metavar="AGENT[,AGENT...]",
                       help="configure only these agents by key "
                            f"({', '.join(sorted(_AGENTS_BY_KEY))})")
    parser.add_argument("--provider", default=None, metavar="PROVIDER",
                        help="LLM provider to configure without prompting "
                             f"({', '.join(providers.provider_names())})")
    parser.add_argument("--reconfigure", action="store_true",
                        help="ask the LLM provider question again even when "
                             "config.yaml already carries an llm block "
                             "(by default an existing configuration is kept "
                             "and only re-verified)")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the live ping after provider setup")
    parser.add_argument("--import", dest="import_file", metavar="FILE",
                        default=None,
                        help="import a brain export during setup (new "
                             "machine migration; merging is not supported)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show exactly what would be written; write nothing")
    args = parser.parse_args(argv)

    try:
        code = run(
            all_agents=args.all,
            only=[k.strip() for k in args.only.split(",") if k.strip()]
            if args.only else None,
            dry_run=args.dry_run,
            provider=args.provider,
            reconfigure=args.reconfigure,
            no_verify=args.no_verify,
            import_file=args.import_file,
        )
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if code == 0:
        note = stale_entry_point_note()
        if note:
            print(note)
    return code


if __name__ == "__main__":
    sys.exit(main())
