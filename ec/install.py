"""``ec install`` — one command configures EC for all detected coding agents.

Spec §28.8 (detection + per-agent installation logic + Zen key check),
§28.9 (``~/.ec/AGENTS.md``, written verbatim), §28.10 (command templates),
§28.2 (the ``~/.ec/`` directory structure).

Safety contract: the installer writes ONLY inside the EC home (``~/.ec`` or
``$EC_HOME``) and the four agents' own config locations. Existing agent
config is merged, never overwritten (only the ``ec`` entry is
added/replaced). ``--dry-run`` prints every planned write with its full
resulting content and writes nothing.

Entry points: ``python -m ec.install`` or the ``ec`` console script. When
the package is pip-installed (D45), agent configs carry the ``ec`` console
script; otherwise the absolute venv-python form is written (B2/D28).

Offline fallback (D42): when no Zen key is set, the installer detects the
``ollama`` binary, offers to configure ``~/.ec/config.yaml`` for local
inference, and pulls ``qwen2.5-coder:14b`` (never fatal).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from .brain import Brain
from .config import DEFAULT_CONFIG
from .config import ec_home as _default_ec_home

# ---------------------------------------------------------------------------
# Verbatim spec texts (§28.9, §28.10) — tests assert equality with SPEC.md
# ---------------------------------------------------------------------------

AGENTS_MD = """# Engineering Cognition — Agent Instructions

EC is an MCP server providing engineering memory for your codebase.
It stores Engineering Cognition Units (ECUs) — conclusions, not facts —
and retrieves them when you need them.

EC provides four MCP tools:
- ec_observe: Extract engineering conclusions from your reasoning
- ec_query: Retrieve relevant engineering cognition
- ec_get_summary: Get brain statistics at session start
- ec_reconsolidate: Update a retrieved ECU with new evidence

## Session Lifecycle

EC requires an active session. The USER controls session lifecycle:
- `/ec-start` — start or resume a session on the current branch
- `/ec-stop` — end session and trigger review gate
- `/ec-status` — show session and brain status

You do NOT control session lifecycle. Never suggest /ec-start or /ec-stop
unless the user asks. If ec_observe or ec_query returns "no active session",
tell the user to run /ec-start.

## When to Call ec_observe

Call ec_observe AFTER you:
- Discover a non-obvious invariant or dependency
- Debug a tricky issue and find the root cause
- Make an architectural decision with rationale
- Discover that a previous assumption was wrong
- Find that code behaves differently from what documentation suggests

Pass your reasoning trace (your thinking, decisions, and rationale) — not
just the final code output. The Extractor lifts observations into conclusions.

Do NOT call ec_observe for:
- Simple code changes with no insight
- Things already captured in existing ECUs
- Pure factual observations (e.g., "this function returns a string")
- Every single response — only when you have engineering insight worth saving

## When to Call ec_query

Call ec_query when you recognize a gap that past engineering cognition might fill.
Read the relevant code files FIRST. Do NOT call ec_query before you have grounded
yourself in the repository. EC cognition is a supplement to your understanding,
not a replacement for reading the code.

Call ec_query AFTER reading the code, when:
- You're about to make a decision between multiple approaches
- You're debugging and the cause isn't obvious from the code alone
- You see a pattern in the code you don't recognise
- You're about to implement something in a file or module you haven't worked
  with in this session
- You suspect a previous assumption might be wrong
- You're planning an architectural change and want to check for existing
  architectural decisions or constraints

Do NOT call ec_query:
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

If ec_query returns no results, proceed normally — this means EC has no relevant 
cognition yet. EC starts empty and builds up over sessions.

## When to Call ec_reconsolidate

Call ec_reconsolidate when you discover that a previously retrieved ECU needs
updating based on what you've found in the current codebase. This is how EC
stays current — beliefs that are no longer accurate get corrected.

Call ec_reconsolidate when:
- You verified a retrieved ECU against the code and found it's no longer accurate
- You discovered new evidence that strengthens a retrieved ECU
- You discovered new evidence that contradicts a retrieved ECU
- The code an ECU is grounded in has changed, and the conclusion needs updating

Pass the ECU ID (from the ec_query result), your evidence (your reasoning about
what changed), and optionally the relationship (supports/contradicts/supersedes).

Do NOT call ec_reconsolidate for:
- New insights unrelated to a previously retrieved ECU (use ec_observe)
- ECUs you haven't retrieved in this session (ec_query first)
- Simple code changes with no impact on engineering conclusions

If ec_reconsolidate is not available (older EC version), use ec_observe to record
the new finding — the Diffuser will handle the relationship at review time.

## When to Call ec_get_summary

Call ec_get_summary ONCE at session start to see what EC already knows.
This helps you decide whether to ec_query for specific context before
starting work. Returns brain statistics, not individual ECUs.

## Reading EC Output

When ec_query returns ECUs, interpret them as follows:

### Confidence levels:
- High (>0.7): Reliable, act on this. Verify grounding if the decision is critical.
- Medium (0.3-0.7): Probably correct. Verify before acting on critical decisions.
- Low (<0.3): Uncertain. Flagged for your awareness. Do not rely on this
  without independent verification.

### Status indicators:
- active: Normal, current belief. Safe to use.
- challenged: Contradicting evidence exists. Be careful — this belief
  may be wrong. Check for a newer ECU that might supersede it.
- open_question: Unresolved engineering question with competing hypotheses.
  Both sides may be returned. Treat as "this is debated."

### Grounding references:
Every ECU includes grounding references (files, symbols, commit hash).
ALWAYS verify these against the current codebase before acting on the ECU.
If the referenced file or symbol no longer exists, the ECU may be stale.

EC's Maintainer periodically verifies grounding references against the live
repository and deprecates ECUs whose grounding has disappeared. Deprecated
ECUs are not returned by ec_query. If you discover that an ECU's grounding
is stale before EC's Maintainer has caught it, call ec_reconsolidate to
update the ECU with what you've found.

### Warnings:
ec_query may return warnings (e.g., "2 ECUs have low confidence",
"1 ECU is challenged"). Read these warnings and adjust your trust accordingly.

## What EC Stores

ECUs are engineering conclusions, not raw information. Examples:
- STORED: "Authentication correctness depends on optimistic token refresh;
  future implementations should preserve this invariant."
- NOT STORED: "TokenManager.refresh() is called before cache.clear()"

Each ECU has:
- cognition: The irreducible engineering conclusion (text)
- confidence: 0.0 to 1.0, Bayesian-updated belief strength
- scope: Where this applies (engineering, domain, organization, project, repo,
  module, subsystem)
- grounding: References to code files, symbols, and commit state
- edges: Connections to other ECUs (supports, contradicts, supersedes, depends_on)

## Important: Anti-Anchoring

EC memory is framed as "past beliefs to verify against current code," NOT
as authoritative context. Always verify ECUs against the live codebase.
Do not anchor on past beliefs if the current code contradicts them.
If you notice a contradiction, call ec_observe to record the new finding.

Observations are stored in the Session Brain, not the Canonical Brain. The 
user reviews them at /ec-stop and accepts or rejects each one. Only accepted 
observations are promoted to the Canonical Brain for future sessions. Don't 
hesitate to observe something uncertain — the review gate filters it.
"""

_EC_START_STEPS = """Start or resume an EC session on the current git branch.

Steps:
1. Detect the current repository path and git branch.
2. Check for an active EC session matching this (repo, branch).
3. If found: resume it — load session ECUs and edges into the Session Brain.
4. If not found: create a new session.
5. Call ec_get_summary to get a brain overview.
6. Report: session ID, branch, repo, session brain ECU count.
"""

_EC_STOP_STEPS = """End the EC session and trigger the Review Gate.

Steps:
1. Run a final extraction pass on any unobserved reasoning since the last ec_observe call.
2. Present all Session Brain ECUs for human review, grouped by topic.
3. For each group: offer accept-all, reject-all, or review-individually.
4. Accepted ECUs: send through the Full Diffuser into the Canonical Brain.
5. Rejected ECUs: remove from session_ecus.
6. Skipped ECUs: remain in session_ecus with review_status = skipped.
7. Clean up: remove session_edges for accepted/rejected ECUs. Apply or discard pending_updates.
8. Mark session as closed in sessions table.
9. Report: accepted count, rejected count, skipped count, Canonical Brain total.
"""

_EC_STATUS_STEPS = """Show current EC status.

Steps:
1. Check if an EC session is active for the current (repo, branch).
2. If active: report session duration, branch, repo, session brain ECU count.
3. Report Canonical Brain: total ECUs, last maintenance run, pending review count.
4. If no session active: report brain stats only.
"""

# §28.10 steps verbatim + the D24 wiring: the CLI performs the steps (D15),
# the MCP tools are referenced by their §28.9 names.
COMMAND_TEMPLATES: dict[str, str] = {
    "ec-start.md": _EC_START_STEPS + """
---

Run: `python -m ec.session start`

The command performs steps 1–6 and prints the report (including the
ec_get_summary brain overview). Once the session is active, use the
ec_observe and ec_query MCP tools exactly as documented in ~/.ec/AGENTS.md.
""",
    "ec-stop.md": _EC_STOP_STEPS + """
---

Run: `python -m ec.session stop`

Before stopping, call ec_observe with any reasoning since the last
ec_observe call (step 1's final extraction pass). The command performs
steps 2–9, presenting the interactive Review Gate in the terminal. If the
user asks for a non-interactive stop, use `python -m ec.session stop
--all-accept` or `python -m ec.session stop --all-skip`.
""",
    "ec-status.md": _EC_STATUS_STEPS + """
---

Run: `python -m ec.session status`

The command prints the session and brain report. The ec_get_summary MCP
tool returns the same brain statistics and works without an active session.
""",
}

ZEN_KEY_ENV = "OPENCODE_ZEN_API_KEY"
ZEN_GUIDANCE = (
    "EC needs an OpenCode Zen API key for LLM inference (extraction, "
    "relationship classification, mode detection). Get one at "
    "https://opencode.ai/auth — then run: export OPENCODE_ZEN_API_KEY='your-key'"
)
_OLLAMA_FALLBACK = (
    "(Fallback: set up Ollama with qwen2.5-coder:14b for offline use.)"
)

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
    "    and set these keys in ~/.ec/config.yaml:\n"
    f"{_OLLAMA_CONFIG_SNIPPET}"
)
_OLLAMA_OFFER = (
    f"\nConfigure EC for offline use with Ollama ({OLLAMA_MODEL})? "
    "(y/n): "
)


def _ollama_config_plan(ec_home: Path, home: Path) -> PlannedWrite:
    """Planned config.yaml write switching the LLM block to Ollama (D42).

    Existing config is merged (user keys preserved); a missing file gets the
    full defaults with the Ollama block applied. Idempotent: unchanged when
    the file already carries exactly the merged content.
    """
    path = ec_home / "config.yaml"
    disp = _display(path, home)
    existed = path.exists()
    if existed:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise InstallError(
                f"{disp} is not valid YAML ({exc}). Fix or remove it, then "
                "re-run `python -m ec.install`. No changes were made to it."
            ) from exc
        if not isinstance(data, dict):
            raise InstallError(
                f"{disp} must contain a YAML mapping. Fix or remove it, then "
                "re-run `python -m ec.install`. No changes were made to it."
            )
        merged = copy.deepcopy(data)
    else:
        merged = copy.deepcopy(DEFAULT_CONFIG)
    llm = merged.setdefault("llm", {})
    if not isinstance(llm, dict):
        raise InstallError(
            f"{disp}: `llm` is not a YAML mapping. Fix or remove it, then "
            "re-run `python -m ec.install`. No changes were made to it."
        )
    llm.update(OLLAMA_LLM_BLOCK)
    content = yaml.safe_dump(merged, sort_keys=False)
    if existed and content == path.read_text(encoding="utf-8"):
        return PlannedWrite(path, "unchanged", None,
                            f"{disp} already configured for Ollama — unchanged")
    return PlannedWrite(
        path, "merge" if existed else "create", content,
        f"Configured {disp} for offline Ollama inference ({OLLAMA_MODEL})",
    )


#: The distribution name importlib.metadata looks up (pyproject.toml).
DIST_NAME = "engineering-cognition"
#: Console script launching the MCP server (pyproject [project.scripts]).
MCP_CONSOLE_SCRIPT = "ec-mcp"


def _is_pip_installed() -> bool:
    """D45: True when the ``ec`` package is pip-installed (editable counts),
    so the ``ec-mcp`` console script exists and agent configs can launch the
    server with it instead of an absolute venv-python + PYTHONPATH."""
    try:
        from importlib import metadata
        metadata.version(DIST_NAME)
        return True
    except Exception:            # not installed / metadata unreadable
        return False


def server_command() -> tuple[str, list[str], dict[str, str]]:
    """``(command, args, env)`` for launching the EC MCP server from any cwd.

    D45: when the package is pip-installed, the ``ec-mcp`` console script is
    used — a bare name that resolves wherever the package is installed, and
    no PYTHONPATH juggling. NOTE (deviation from design doc §6.1): the doc's
    literal ``"ec"`` command would launch the INSTALLER (``ec.install:main``),
    not the server — hence the dedicated ``ec-mcp`` script.

    Development fallback (D28/B2): a bare interpreter could not even import
    ``ec``, so the configs carry the ABSOLUTE path to the project's venv
    interpreter (dynamically computed from this file's location — never
    hardcoded) plus a PYTHONPATH entry pointing at the project root.
    """
    if _is_pip_installed():
        return MCP_CONSOLE_SCRIPT, [], {}
    root = Path(__file__).resolve().parent.parent
    python: str | None = None
    for candidate in (root / ".venv" / "bin" / "python",          # POSIX venv
                      root / ".venv" / "Scripts" / "python.exe"):  # Windows venv
        if candidate.is_file():
            python = str(candidate)
            break
    if python is None:   # no project venv — the running interpreter has `ec`
        python = sys.executable
    return python, ["-m", "ec.mcp_server"], {"PYTHONPATH": str(root)}


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


def _json_merge(path: Path, home: Path, section: str, entry: dict,
                detail: str, schema: str | None = None) -> PlannedWrite:
    """Merge ``entry`` into ``data[section]["ec"]`` — never overwriting siblings."""
    disp = _display(path, home)
    existed = path.exists()
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(
                f"{disp} is not valid JSON ({exc}). Fix or remove it, then "
                "re-run `python -m ec.install`. No changes were made to it."
            ) from exc
        if not isinstance(data, dict):
            raise InstallError(
                f"{disp} must contain a JSON object. Fix or remove it, then "
                "re-run `python -m ec.install`. No changes were made to it."
            )
    else:
        data = {}
        if schema is not None:
            data["$schema"] = schema
    node = data.setdefault(section, {})
    if not isinstance(node, dict):
        raise InstallError(
            f"{disp}: `{section}` is not a JSON object. Fix or remove it, "
            "then re-run `python -m ec.install`. No changes were made to it."
        )
    if node.get("ec") == entry:
        return PlannedWrite(path, "unchanged", None,
                            f"{disp} already has the ec MCP server — unchanged")
    node["ec"] = entry
    action = "merge" if existed else "create"
    return PlannedWrite(path, action, json.dumps(data, indent=2) + "\n", detail)


def _line_once(path: Path, home: Path, line: str, detail: str) -> PlannedWrite:
    """Append ``line`` to a text file, creating it if absent (idempotent)."""
    disp = _display(path, home)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if line in (l.strip() for l in text.splitlines()):
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
    """The ``[mcp_servers.ec]`` TOML table (console script in pip installs;
    absolute python + PYTHONPATH env otherwise).

    ``json.dumps`` strings/arrays are valid TOML basic strings/arrays for
    ordinary paths, and handle any quoting/escaping correctly.
    """
    python, args, env = server_command()
    lines = ["[mcp_servers.ec]", f"command = {json.dumps(python)}"]
    if args:
        lines.append(f"args = {json.dumps(args)}")
    if env:
        lines.append(f"env = {{ PYTHONPATH = {json.dumps(env['PYTHONPATH'])} }}")
    return "\n".join(lines)


def _mcp_entry(command: str, args: list[str], env: dict,
               extra: dict | None = None) -> dict:
    """JSON MCP-server entry; empty args/env keys are omitted (D45 console
    script form is just {"command": "ec-mcp"})."""
    entry = dict(extra or {})
    entry["command"] = command
    if args:
        entry["args"] = list(args)
    if env:
        entry["env"] = dict(env)
    return entry


def _codex_toml(path: Path, home: Path, detail: str) -> PlannedWrite:
    """Upsert the ``[mcp_servers.ec]`` table; every other byte is preserved."""
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
            "re-run `python -m ec.install`. No changes were made to it."
        ) from exc
    lines = text.splitlines()
    start = next(
        (i for i, l in enumerate(lines) if l.strip() == "[mcp_servers.ec]"),
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
                            f"{disp} already has [mcp_servers.ec] — unchanged")
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
        f"## Engineering Cognition (EC)\n\n"
        f"Follow the EC agent instructions at {agents_ref}. EC is an MCP "
        f"server (`ec`) providing engineering memory via ec_observe, "
        f"ec_query, ec_get_summary, and ec_reconsolidate. The user controls "
        f"the session lifecycle (/ec-start, /ec-stop, /ec-status) — never "
        f"suggest starting or stopping a session unless the user asks.\n"
    )


# ---------------------------------------------------------------------------
# agent registry (§28.8 "Installation Logic per Agent")
# ---------------------------------------------------------------------------

@dataclass
class AgentSpec:
    key: str
    name: str
    binary: str
    marker: str                     # home-relative path; existence ⇒ installed

    def detected(self, home: Path, which) -> bool:
        return which(self.binary) is not None or (home / self.marker).exists()

    def plan(self, home: Path, ec_home: Path) -> list[PlannedWrite]:
        raise NotImplementedError


def _agents_ref(home: Path, ec_home: Path) -> str:
    """How agent configs refer to the EC AGENTS.md (spec form when default)."""
    return "~/.ec/AGENTS.md" if ec_home == home / ".ec" else str(ec_home / "AGENTS.md")


class _ClaudeCode(AgentSpec):
    def __init__(self):
        super().__init__("claude_code", "Claude Code", "claude", ".claude.json")

    def plan(self, home: Path, ec_home: Path) -> list[PlannedWrite]:
        ref = _agents_ref(home, ec_home)
        python, args, env = server_command()
        return [
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


class _Cursor(AgentSpec):
    def __init__(self):
        super().__init__("cursor", "Cursor", "cursor", ".cursor/mcp.json")

    def plan(self, home: Path, ec_home: Path) -> list[PlannedWrite]:
        ref = _agents_ref(home, ec_home)
        python, args, env = server_command()
        rules = home / ".cursor" / "rules" / "ec.md"
        return [
            _json_merge(
                home / ".cursor" / "mcp.json", home, "mcpServers",
                _mcp_entry(python, args, env),
                f"Added MCP server to "
                f"{_display(home / '.cursor' / 'mcp.json', home)}",
            ),
            _verbatim_file(
                rules, home, _reference_block(ref),
                f"Wrote {_display(rules, home)} (references {ref})",
            ),
        ]


class _OpenCode(AgentSpec):
    def __init__(self):
        super().__init__("opencode", "OpenCode", "opencode",
                         ".config/opencode/opencode.json")

    def plan(self, home: Path, ec_home: Path) -> list[PlannedWrite]:
        ref = _agents_ref(home, ec_home)
        python, args, env = server_command()
        cfg = home / ".config" / "opencode" / "opencode.json"
        agents = home / ".config" / "opencode" / "AGENTS.md"
        entry = _mcp_entry([python, *args], [], env,
                           {"type": "local", "enabled": True})
        if "env" in entry:               # OpenCode names the key differently
            entry["environment"] = entry.pop("env")
        return [
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


class _Codex(AgentSpec):
    def __init__(self):
        super().__init__("codex", "Codex", "codex", ".codex/config.toml")

    def plan(self, home: Path, ec_home: Path) -> list[PlannedWrite]:
        ref = _agents_ref(home, ec_home)
        cfg = home / ".codex" / "config.toml"
        agents = home / ".codex" / "AGENTS.md"
        return [
            _codex_toml(cfg, home,
                        f"Added [mcp_servers.ec] to {_display(cfg, home)}"),
            _block_once(
                agents, home, ref, _reference_block(ref),
                f"Wrote {_display(agents, home)} (references {ref})",
            ),
        ]


AGENTS: list[AgentSpec] = [_ClaudeCode(), _Cursor(), _OpenCode(), _Codex()]
_AGENTS_BY_KEY = {a.key: a for a in AGENTS}


# ---------------------------------------------------------------------------
# ~/.ec bootstrap (§28.2)
# ---------------------------------------------------------------------------

def _ec_home_plans(home: Path, ec_home: Path) -> list[PlannedWrite]:
    disp = _display(ec_home, home)
    db = ec_home / "ec.db"
    plans = [
        PlannedWrite(
            db, "unchanged" if db.exists() else "create-db", None,
            f"{_display(db, home)} already exists — unchanged" if db.exists()
            else f"Created {_display(db, home)} (empty brain, initialised)",
        ),
    ]
    cfg = ec_home / "config.yaml"
    if cfg.exists():
        plans.append(PlannedWrite(
            cfg, "unchanged", None,
            f"{_display(cfg, home)} already exists — kept"))
    else:
        plans.append(PlannedWrite(
            cfg, "create",
            yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False),
            f"Created {_display(cfg, home)} (default configuration)"))
    agents_md = ec_home / "AGENTS.md"
    plans.append(_verbatim_file(
        agents_md, home, AGENTS_MD,
        f"Wrote {_display(agents_md, home)} (agent instructions)"))
    for name, template in COMMAND_TEMPLATES.items():
        path = ec_home / "commands" / name
        plans.append(_verbatim_file(
            path, home, template,
            f"Wrote {_display(path, home)} (command template)"))
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
        answer = input_fn("\nAdd EC to all detected agents? (y/n): ")
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

def run(*, home: Path | None = None, ec_home: Path | None = None,
        all_agents: bool = False, only: list[str] | None = None,
        dry_run: bool = False, input_fn=input, print_fn=print,
        which=shutil.which, environ=os.environ,
        runner=subprocess.run) -> int:
    """§28.8 `ec install`. Returns 0 on success, raises InstallError otherwise.

    ``runner`` executes the Ollama model pull when the user accepts the D42
    offer (injectable for tests; failures are reported, never fatal).
    """
    home = Path(home) if home else Path.home()
    ec_home = Path(ec_home) if ec_home else _default_ec_home()

    print_fn("\nEngineering Cognition — Installation\n")
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
                 "--only (e.g. `python -m ec.install --only claude_code`).")

    # -- per-agent configuration -------------------------------------------
    writes: list[tuple[str | None, PlannedWrite]] = []
    for agent in selected:
        for write in agent.plan(home, ec_home):
            writes.append((agent.name, write))

    current_agent: str | None = None
    for agent_name, write in writes:
        if agent_name != current_agent:
            print_fn(f"\nConfiguring {agent_name}...")
            current_agent = agent_name
        _emit(write, home, dry_run, print_fn)

    # -- ~/.ec bootstrap (§28.2) -------------------------------------------
    print_fn(f"\nCreating {_display(ec_home, home)}/ directory...")
    for write in _ec_home_plans(home, ec_home):
        _emit(write, home, dry_run, print_fn)

    # -- LLM dependency (§28.8) + Ollama auto-setup (D42, design doc §6.4) --
    print_fn("\nChecking LLM dependency...")
    zen_found = bool(environ.get(ZEN_KEY_ENV))
    ollama_path = which(OLLAMA_BINARY)
    if zen_found:
        print_fn(f"  ✓ OpenCode Zen API key found ({ZEN_KEY_ENV} env var)")
        print_fn("  (EC uses Claude Haiku 4.5 via Zen for extraction, "
                 "diffusion, and mode detection.)")
        if ollama_path:
            print_fn(f"  · Ollama detected — available as an offline fallback "
                     f"(switch via ~/.ec/config.yaml: provider \"ollama\", "
                     f"model \"{OLLAMA_MODEL}\").")
        else:
            print_fn(f"  {_OLLAMA_FALLBACK}")
    else:
        print_fn(f"  ✗ {ZEN_KEY_ENV} is not set")
        print_fn(f"  {ZEN_GUIDANCE}")
        if ollama_path:
            print_fn(f"  ✓ Ollama detected ({ollama_path})")
            try:
                answer = input_fn(_OLLAMA_OFFER)
            except EOFError:
                answer = ""
            if answer.strip().lower().startswith("y"):
                _emit(_ollama_config_plan(ec_home, home), home, dry_run,
                      print_fn)
                if dry_run:
                    print_fn(f"  [dry-run] would pull {OLLAMA_MODEL} via "
                             f"`{OLLAMA_BINARY} pull {OLLAMA_MODEL}`")
                else:
                    print_fn(f"  Pulling {OLLAMA_MODEL} (this may take a "
                             "while)...")
                    try:
                        runner([ollama_path or OLLAMA_BINARY, "pull",
                                OLLAMA_MODEL])
                        print_fn(f"  ✓ Model {OLLAMA_MODEL} available")
                    except Exception as exc:      # never fatal (D42)
                        print_fn(
                            f"  ! Could not run `{OLLAMA_BINARY} pull` ({exc})."
                            f" Run it manually, then EC works offline.")
            else:
                print_fn("  Ollama left unconfigured. To enable later, set "
                         "these keys in ~/.ec/config.yaml:")
                print_fn(_OLLAMA_CONFIG_SNIPPET)
        else:
            print_fn(_OLLAMA_INSTALL_INSTRUCTIONS)

    if dry_run:
        print_fn("\nDry run complete — no files were written.")
        return 0

    print_fn("\nEC is ready!\n")
    print_fn("Commands:")
    print_fn("  /ec-start   Start or resume an EC session on the current branch")
    print_fn("  /ec-stop    End session and review extracted cognition")
    print_fn("  /ec-status   Show session and brain status")
    print_fn("\nHow to use:")
    print_fn("  1. Open your coding agent (Claude Code, Cursor, or Codex)")
    print_fn("  2. Run /ec-start to begin a session")
    print_fn("  3. Work normally — the agent will automatically call "
             "ec_observe and ec_query")
    print_fn("  4. Run /ec-stop when done to review and save engineering "
             "cognition")
    return 0


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
        prog="python -m ec.install",
        description="EC installation (§28.8): configure detected coding "
                    "agents and create the ~/.ec directory.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true",
                       help="configure every detected agent (non-interactive)")
    group.add_argument("--only", default=None, metavar="AGENT[,AGENT...]",
                       help="configure only these agents by key "
                            f"({', '.join(sorted(_AGENTS_BY_KEY))})")
    parser.add_argument("--dry-run", action="store_true",
                        help="show exactly what would be written; write nothing")
    args = parser.parse_args(argv)

    try:
        return run(
            all_agents=args.all,
            only=[k.strip() for k in args.only.split(",") if k.strip()]
            if args.only else None,
            dry_run=args.dry_run,
        )
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
