"""``reverie install`` — one command configures Reverie for all detected coding agents.

Spec §28.8 (detection + per-agent installation logic + LLM provider step),
§28.9 (``~/.reverie/AGENTS.md``, written verbatim), §28.10 (command templates),
§28.2 (the ``~/.reverie/`` directory structure).

Safety contract: the installer writes ONLY inside the Reverie home (``~/.reverie`` or
``$REVERIE_HOME``) and the supported integrations' own config locations. Existing agent
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
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import yaml

from . import providers
from .brain import Brain
from .config import DEFAULT_CONFIG, load_config
from .config import reverie_home as _default_reverie_home
from .integrations import AGENTS, Integration
from .integrations import _is_reverie_hook_handler as _is_reverie_hook_handler
from .integrations import configured_servers as _registry_configured_servers
from .integrations.common import (
    ConfiguredServer,
    InstallError,
    PlannedWrite,
    _display,
)
from .integrations.common import (
    _reference_block as _reference_block,
)
from .launch import (
    _is_pip_installed,
    _script_path,
)
from .llm import call_llm
from .policy import USAGE_POLICY

# ---------------------------------------------------------------------------
# Verbatim spec texts (§28.9, §28.10) — tests assert equality with SPEC.md
# ---------------------------------------------------------------------------

AGENTS_MD = f"""# Reverie — Agent Instructions

Reverie is an MCP server providing engineering memory for your codebase.
It stores engineering conclusions, not facts, and retrieves them when
you need them.

Reverie provides seven MCP tools:
- observe: Extract engineering conclusions from your reasoning
- query: Retrieve relevant engineering conclusions
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

{USAGE_POLICY}

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

Pass the conclusion ID (from the query result), your evidence (your reasoning about
what changed), and optionally the relationship (supports/contradicts/supersedes).
Evidence must reference something the user can verify — a file path with
line numbers, a test name, or a commit hash — not free-floating narrative.

Do NOT call reconsolidate for:
- New insights unrelated to a previously retrieved conclusion (use observe)
- Conclusions you haven't retrieved in this session (query first)
- Simple code changes with no impact on engineering conclusions

If reconsolidate is not available, use observe to record
the new finding — the Diffuser will handle the relationship at review time.

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
(see "When to call each command"). Only accepted observations are promoted
to the Canonical Brain for future use. Don't hesitate to observe something
uncertain — the review gate filters it.
"""


# ---------------------------------------------------------------------------
# user-level slash commands (Phase 3) — installed in each harness's own
# location and format. Two commands: /reverie-review (ask the agent to list
# review candidates and relay decisions) and /reverie-status (the session +
# brain report through summary).
# ---------------------------------------------------------------------------

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


def console_script_path(executable: str | None = None, which=shutil.which,
                        ) -> str | None:
    """Absolute path of the installed ``reverie-mcp`` console script, or None."""
    return _script_path(MCP_CONSOLE_SCRIPT, executable, which)


def server_command(executable: str | None = None, which=shutil.which,
                   client: str | None = None,
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

    ``client`` (an integration's harness key) is appended as
    ``--client <key>`` so the server records which client launched it. It is
    optional: configs written before it existed keep working unchanged.
    """
    client_args = ["--client", client] if client else []
    if _is_pip_installed():
        script = console_script_path(executable, which)
        if script is not None:
            return script, client_args, {}
        return executable or sys.executable, ["-m", "reverie.mcp_server",
                                              *client_args], {}
    root = Path(__file__).resolve().parent.parent
    python: str | None = None
    for candidate in (root / ".venv" / "bin" / "python",          # POSIX venv
                      root / ".venv" / "Scripts" / "python.exe"):  # Windows venv
        if candidate.is_file():
            python = str(candidate)
            break
    if python is None:   # no project venv — the running interpreter has `reverie`
        python = sys.executable
    return python, ["-m", "reverie.mcp_server", *client_args], {
        "PYTHONPATH": str(root)}


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


def configured_servers(home: Path | None = None) -> list[ConfiguredServer]:
    """Every harness config's reverie server entry, in AGENTS order.

    Used by the install-time check (launch what was just written) and by
    ``reverie status`` (does that command still exist?)."""
    return _registry_configured_servers(Path(home) if home else Path.home())


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


#: Integration lookup for the installer's --only validation and status.
_AGENTS_BY_KEY = {agent.key: agent for agent in AGENTS}

# ---------------------------------------------------------------------------
# ~/.reverie bootstrap (§28.2)
# ---------------------------------------------------------------------------

#: The retired installer default for the opencode_zen llm block (review fix
#: 2 follow-up). Configs written before the default moved to Haiku 5.5 are
#: migrated — only when they still carry exactly this model.
OLD_DEFAULT_LLM_MODEL = "claude-haiku-4-5"

#: One `model:` scalar inside the top-level `llm:` mapping.
_LLM_MODEL_LINE = re.compile(r"^(\s+model\s*:\s*)(\S+)(.*)$")


def _migrate_old_llm_default(text: str) -> str | None:
    """Migrate config.yaml whose opencode_zen block still names the retired
    installer default (review fix 2, follow-up).

    Returns the rewritten text, or None when the migration does not apply.
    Only that one scalar is replaced: comments, key order, quoting, spacing
    and every other byte survive untouched. Any other provider or model is
    the user's explicit choice and is left alone.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    llm = data.get("llm")
    if not isinstance(llm, dict):
        return None
    if llm.get("provider") != "opencode_zen":
        return None
    if llm.get("model") != OLD_DEFAULT_LLM_MODEL:
        return None
    new_model = DEFAULT_CONFIG["llm"]["model"]
    lines = text.splitlines(keepends=True)
    in_block = False
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if body and not body[0].isspace() and not body.startswith("#"):
            in_block = bool(re.match(r"llm\s*:", body))
            continue
        if not in_block:
            continue
        match = _LLM_MODEL_LINE.match(body)
        if match is None:
            continue
        token = match.group(2)
        quote, value = "", token
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
            quote, value = token[0], token[1:-1]
        if value != OLD_DEFAULT_LLM_MODEL:
            return None           # a hand-picked model line — keep the file
        lines[i] = (f"{match.group(1)}{quote}{new_model}{quote}"
                    f"{match.group(3)}{ending}")
        return "".join(lines)
    return None


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
        migrated = _migrate_old_llm_default(cfg.read_text(encoding="utf-8"))
        if migrated is not None:
            plans.append(PlannedWrite(
                cfg, "update", migrated,
                f"Updated {_display(cfg, home)}: llm.model "
                f"{OLD_DEFAULT_LLM_MODEL} → {DEFAULT_CONFIG['llm']['model']} "
                "(the old installer default; only this value changed)"))
        else:
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

def _select_interactive(detected: list[Integration], input_fn, print_fn
                        ) -> list[Integration]:
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
    config_existed = (reverie_home / "config.yaml").exists()

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
    if config_existed and not dry_run:
        # review fix 2 (follow-up): the plan above may have migrated an old
        # installer-default llm.model. Keep the snapshot in step with disk so
        # the messages below and the re-verify ping use the migrated config.
        existing_llm = _existing_llm_config(reverie_home) or existing_llm

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
