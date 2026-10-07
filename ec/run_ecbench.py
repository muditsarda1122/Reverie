"""EC-Bench runner — drive the benchmark in both conditions and capture
everything for later scoring.

EC-Bench v2: 30 prompts across 3 sequential sessions on a target repository
(e.g. FastAPI), comparing an agent WITH EC against a stateless baseline.

What the runner does, per condition (``ec`` | ``baseline``):

1. gives the condition its own COPY of the target repo (``workdir/``) so the
   two conditions start from identical state and the original is untouched
2. isolates ``EC_HOME`` per condition (the baseline never gets a brain)
3. generates an agent config and injects it via ``OPENCODE_CONFIG``:
   - ``ec``: the EC MCP server (absolute venv python + PYTHONPATH + EC_HOME,
     the B2 fix from ``ec.install.server_command``) plus the §28.9 AGENTS.md
   - ``baseline``: no EC instructions, and the ``ec`` MCP server explicitly
     disabled (in case EC is installed in the user's global config)
4. per session: ``ec.session start`` → run each prompt headlessly
   (transcripts captured per prompt) → ``ec.session stop --all-accept``
   (the review gate between sessions; ``--review skip`` also available)
5. writes ``manifest.json`` (per-prompt transcripts, exit codes, durations,
   session logs, EC brain statistics) — scoring happens elsewhere

Agent context is FRESH per prompt by default, so memory effects are
attributable to EC rather than to accumulated chat context;
``--continue-within-session`` opts into ``opencode run --continue``.

Spec file (JSON):

    {
      "name": "ec-bench-v2",
      "sessions": [
        {"id": "session-1", "prompts": ["prompt 1", "...", "prompt 10"]},
        {"id": "session-2", "prompts": ["..."]},
        {"id": "session-3", "prompts": ["..."]}
      ]
    }

Re-running with the same ``--run-id`` skips prompts whose transcripts already
completed successfully (crash-safe resume). ``--dry-run`` prints the full
plan and writes nothing.

Example:

    .venv/bin/python -m ec.run_ecbench \\
        --spec bench/ecbench_v2.json --repo ~/src/fastapi --branch main \\
        --runs-dir runs

Requires the agent CLI on PATH (default: OpenCode — ``opencode run``) and
OPENCODE_ZEN_API_KEY in the environment for the EC condition.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .brain import Brain
from .install import AGENTS_MD, server_command

CONDITIONS = ("ec", "baseline")
DEFAULT_AGENT_CMD = "opencode run --dir {repo} --format json --auto {prompt}"
REVIEWS = ("accept", "skip")


def _runner_python() -> str:
    """Absolute interpreter able to import ``ec`` for harness subprocesses.

    D45 moved ``server_command()`` to the ``ec-mcp`` console script for MCP
    CONFIGS; the harness instead spawns real interpreters (session CLI,
    default agent template) and always needs an executable path, so it
    resolves the project venv python directly.
    """
    root = Path(__file__).resolve().parent.parent
    venv = root / ".venv" / ("Scripts/python.exe" if os.name == "nt"
                             else "bin/python")
    return str(venv) if venv.is_file() else sys.executable


class BenchError(RuntimeError):
    """Benchmark setup/orchestration failure with actionable guidance."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# spec
# ---------------------------------------------------------------------------

def load_spec(path: str | Path) -> dict:
    """Load + validate a benchmark spec (see module docstring for the shape)."""
    p = Path(path)
    if not p.is_file():
        raise BenchError(f"spec file not found: {p}")
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchError(f"spec file {p} is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict) or not isinstance(spec.get("sessions"), list):
        raise BenchError(f"spec {p} must be a JSON object with a 'sessions' array")
    if not spec["sessions"]:
        raise BenchError(f"spec {p} has no sessions")
    for i, session in enumerate(spec["sessions"], start=1):
        if not isinstance(session.get("prompts"), list) or not session["prompts"]:
            raise BenchError(f"spec session {i} must have a non-empty 'prompts' array")
        for j, prompt in enumerate(session["prompts"], start=1):
            if not isinstance(prompt, str) or not prompt.strip():
                raise BenchError(f"spec session {i} prompt {j} must be a non-empty string")
        session.setdefault("id", f"session-{i}")
    spec.setdefault("name", p.stem)
    return spec


# ---------------------------------------------------------------------------
# per-condition agent config (injected via OPENCODE_CONFIG)
# ---------------------------------------------------------------------------

def build_agent_config(condition: str, ec_home: Path, repo: Path,
                       branch: str) -> dict:
    """The OpenCode config overlay for one condition.

    ``ec``: the EC MCP server (absolute interpreter + PYTHONPATH + EC_HOME +
    EC_REPO_PATH/EC_BRANCH so session resolution never depends on the spawn
    cwd) and the §28.9 AGENTS.md instructions. ``baseline``: no EC anything,
    and the server explicitly disabled to neutralize a global install.
    """
    python, args, env = server_command()
    if condition == "ec":
        return {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "ec": {
                    "type": "local",
                    "command": [python, *args],
                    "environment": {
                        **env,
                        "EC_HOME": str(ec_home),
                        "EC_REPO_PATH": str(repo),
                        "EC_BRANCH": branch,
                    },
                    "enabled": True,
                },
            },
            "instructions": [str(ec_home / "AGENTS.md")],
        }
    return {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "ec": {
                "type": "local",
                "command": [python, *args],
                "environment": env,
                "enabled": False,      # present but OFF: overrides global config
            },
        },
    }


def _agent_argv(template: str, prompt: str, repo: Path) -> list[str]:
    """Substitute placeholders; the prompt is inserted as a single argv token
    (never re-split, so arbitrary prompt text is safe)."""
    filled = template.replace("{repo}", str(repo)).replace(
        "{python}", _runner_python())
    argv = shlex.split(filled)
    for i, token in enumerate(argv):
        if "{prompt}" in token:
            argv[i] = token.replace("{prompt}", prompt)
            return argv
    return argv + [prompt]


def _run_cli(args: list[str], env: dict, log_path: Path, timeout: int) -> dict:
    """Run the session CLI, capturing output to a log file."""
    proc = subprocess.run(
        args, capture_output=True, text=True, env=env, timeout=timeout,
    )
    log_path.write_text(
        f"$ {' '.join(args)}\n\n[stdout]\n{proc.stdout}\n[stderr]\n{proc.stderr}",
        encoding="utf-8",
    )
    return {
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "",
        "log": str(log_path),
    }


def _run_prompt(argv: list[str], cwd: Path, env: dict, timeout: int,
                transcript: Path, stderr_log: Path) -> dict:
    """One headless agent invocation; stdout (JSON event stream) → transcript."""
    start = time.monotonic()
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        timed_out = True
    duration = time.monotonic() - start
    transcript.write_text(out, encoding="utf-8")
    stderr_log.write_text(err, encoding="utf-8")
    return {
        "exit_code": proc.returncode,
        "duration_s": round(duration, 2),
        "timed_out": timed_out,
        "transcript": str(transcript),
        "stderr_log": str(stderr_log),
    }


# ---------------------------------------------------------------------------
# EC brain statistics (post-run, for the manifest)
# ---------------------------------------------------------------------------

def collect_ec_stats(ec_home: Path) -> dict:
    """Brain stats for the ec condition: what was observed, promoted, queried."""
    db = ec_home / "ec.db"
    if not db.exists():
        return {"brain": "absent"}
    brain = Brain(db)
    try:
        ecus = brain.list_ecus()
        edge_types: dict[str, int] = {}
        n_edges = 0
        retrieval_events = 0
        for ecu in ecus:
            for edge in brain.get_edges_from(ecu["id"]):
                n_edges += 1
                edge_types[edge["type"]] = edge_types.get(edge["type"], 0) + 1
            retrieval_events += int(
                (ecu.get("metadata") or {}).get("retrieval_count") or 0)
        sessions = [
            {
                "id": s["id"], "status": s["status"],
                "ecu_count": s["ecu_count"],
                "started_at": s["started_at"], "ended_at": s["ended_at"],
            }
            for s in brain.list_sessions()
        ]
        return {
            "canonical_ecus": len(ecus),
            "edges": n_edges,
            "edge_types": edge_types,
            "retrieval_events": retrieval_events,
            "sessions": sessions,
        }
    finally:
        brain.close()


# ---------------------------------------------------------------------------
# condition driver
# ---------------------------------------------------------------------------

def run_condition(
    spec: dict,
    condition: str,
    run_dir: Path,
    repo_src: Path,
    branch: str,
    *,
    review: str = "accept",
    agent_cmd: str = DEFAULT_AGENT_CMD,
    timeout: int = 900,
    continue_within_session: bool = False,
    print_fn=print,
) -> dict:
    """Run all sessions of the spec under one condition. Returns the
    condition's manifest fragment (also written incrementally to disk)."""
    cond_dir = run_dir / condition
    ec_home = cond_dir / "ec_home"
    workdir = (cond_dir / "workdir").resolve()
    logs = cond_dir / "logs"
    transcripts_dir = cond_dir / "transcripts"
    for d in (ec_home, logs, transcripts_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1. fresh copy of the target repo (identical starting state; the
    #    original is never touched)
    if not workdir.exists():
        print_fn(f"  [{condition}] copying repo -> {workdir}")
        shutil.copytree(repo_src, workdir, symlinks=True)

    # 2. EC home + agent config
    python, _args, env = server_command()
    python = _runner_python()      # harness spawns interpreters, not scripts
    if condition == "ec":
        (ec_home / "AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
        Brain(ec_home / "ec.db").close()          # create the empty brain
    config_path = cond_dir / "opencode_config.json"
    config_path.write_text(
        json.dumps(build_agent_config(condition, ec_home, workdir, branch),
                   indent=2),
        encoding="utf-8",
    )

    session_cli_env = {
        **os.environ, **env,
        "EC_HOME": str(ec_home),
        "EC_REPO_PATH": str(workdir),
        "EC_BRANCH": branch,
        "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "1"),
    }
    agent_env = {
        **os.environ,
        "OPENCODE_CONFIG": str(config_path),
        # per-condition session history isolation (fresh context per prompt
        # is the default; this also keeps --continue within the condition)
        "XDG_DATA_HOME": str(cond_dir / "xdg_data"),
        "XDG_STATE_HOME": str(cond_dir / "xdg_state"),
    }
    if condition == "ec":
        agent_env["EC_HOME"] = str(ec_home)
    # the isolated XDG data dir must still carry the agent's provider auth
    src_auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if src_auth.exists():
        dst_auth = cond_dir / "xdg_data" / "opencode" / "auth.json"
        dst_auth.parent.mkdir(parents=True, exist_ok=True)
        if not dst_auth.exists():
            shutil.copy2(src_auth, dst_auth)

    manifest_path = cond_dir / "manifest.json"
    manifest: dict = {
        "condition": condition,
        "workdir": str(workdir),
        "ec_home": str(ec_home),
        "agent_config": str(config_path),
        "agent_cmd": agent_cmd,
        "review": review,
        "started_at": _utcnow_iso(),
        "sessions": [],
    }

    def _flush():
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    for session in spec["sessions"]:
        sid = session["id"]
        print_fn(f"  [{condition}] session {sid} "
                 f"({len(session['prompts'])} prompts)")
        session_rec: dict = {"id": sid, "prompts": []}

        if condition == "ec":
            session_rec["start"] = _run_cli(
                [python, "-m", "ec.session", "start",
                 "--repo", str(workdir), "--branch", branch],
                session_cli_env, logs / f"{sid}_start.log", timeout=120)
            if session_rec["start"]["exit_code"] != 0:
                raise BenchError(
                    f"ec.session start failed for {sid} — see "
                    f"{session_rec['start']['log']}")

        for i, prompt in enumerate(session["prompts"], start=1):
            transcript = transcripts_dir / sid / f"{i:02d}.jsonl"
            meta = transcripts_dir / sid / f"{i:02d}.meta.json"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            argv = _agent_argv(agent_cmd, prompt, workdir)
            if continue_within_session and i > 1:
                argv = argv[:-1] + ["--continue"] + argv[-1:]
            if meta.exists():
                prior = json.loads(meta.read_text(encoding="utf-8"))
                if prior.get("exit_code") == 0 and not prior.get("timed_out"):
                    print_fn(f"    [{condition}] {sid}/{i:02d} already done — "
                             "skipping")
                    prior["skipped_existing"] = True
                    session_rec["prompts"].append(
                        {**prior, "prompt": prompt, "argv": argv})
                    _flush()
                    continue
            print_fn(f"    [{condition}] {sid}/{i:02d} running agent…")
            rec = _run_prompt(argv, workdir, agent_env, timeout,
                              transcript, transcript.with_suffix(".stderr.log"))
            meta.write_text(json.dumps(rec, indent=2), encoding="utf-8")
            session_rec["prompts"].append(
                {"prompt": prompt, "argv": argv, **rec})
            _flush()

        if condition == "ec":
            session_rec["stop"] = _run_cli(
                [python, "-m", "ec.session", "stop", f"--all-{review}",
                 "--repo", str(workdir), "--branch", branch],
                session_cli_env, logs / f"{sid}_stop.log", timeout=600)
            stop_log = Path(session_rec["stop"]["log"]).read_text(encoding="utf-8")
            session_rec["stop"]["diffusion_warning"] = \
                "promoted without diffusion" in stop_log
            if session_rec["stop"]["exit_code"] != 0:
                raise BenchError(
                    f"ec.session stop failed for {sid} — see "
                    f"{session_rec['stop']['log']}")

        manifest["sessions"].append(session_rec)
        _flush()

    if condition == "ec":
        manifest["ec_stats"] = collect_ec_stats(ec_home)
    manifest["finished_at"] = _utcnow_iso()
    _flush()
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ec.run_ecbench",
        description="EC-Bench runner: N prompts x M sessions, with-EC vs "
                    "baseline, transcripts + manifest for scoring.",
    )
    parser.add_argument("--spec", required=True, help="benchmark spec JSON")
    parser.add_argument("--repo", required=True, help="target repository path")
    parser.add_argument("--branch", default="main", help="branch (default: main)")
    parser.add_argument("--runs-dir", default="runs", help="output root")
    parser.add_argument("--run-id", default=None,
                        help="run id (default: timestamp); reuse to resume")
    parser.add_argument("--conditions", default=",".join(CONDITIONS),
                        help="comma subset of ec,baseline")
    parser.add_argument("--review", choices=REVIEWS, default="accept",
                        help="review-gate mode between sessions (default: accept)")
    parser.add_argument("--agent-cmd", default=DEFAULT_AGENT_CMD,
                        help="agent command template; placeholders: "
                             "{repo} {python} {prompt}")
    parser.add_argument("--timeout", type=int, default=900,
                        help="per-prompt timeout seconds (default: 900)")
    parser.add_argument("--continue-within-session", action="store_true",
                        help="pass --continue to the agent within a session "
                             "(default: fresh context per prompt)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan; write nothing")
    args = parser.parse_args(argv)

    try:
        spec = load_spec(args.spec)
    except BenchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    repo_src = Path(args.repo).expanduser().resolve()
    if not repo_src.is_dir():
        print(f"target repo not found: {repo_src}", file=sys.stderr)
        return 1
    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown:
        print(f"unknown condition(s): {unknown}; valid: {list(CONDITIONS)}",
              file=sys.stderr)
        return 1
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.runs_dir).expanduser().resolve() / run_id
    total = sum(len(s["prompts"]) for s in spec["sessions"])

    print(f"EC-Bench run {run_id}")
    print(f"  spec:       {args.spec} ({spec['name']}: "
          f"{len(spec['sessions'])} sessions, {total} prompts)")
    print(f"  repo:       {repo_src} (branch {args.branch})")
    print(f"  conditions: {', '.join(conditions)}")
    print(f"  run dir:    {run_dir}")
    print(f"  review:     --all-{args.review} between sessions")
    print(f"  agent cmd:  {args.agent_cmd}")
    if args.dry_run:
        for condition in conditions:
            cond_dir = run_dir / condition
            print(f"\n[dry-run] condition '{condition}':")
            print(f"  config:  {cond_dir / 'opencode_config.json'}")
            print(json.dumps(
                build_agent_config(condition, cond_dir / "ec_home",
                                   cond_dir / "workdir", args.branch),
                indent=2))
            print(f"  workdir: {cond_dir / 'workdir'} (fresh copy of repo)")
            for session in spec["sessions"]:
                print(f"  session {session['id']}: "
                      f"{len(session['prompts'])} prompts")
        print("\ndry run — nothing written")
        return 0

    run_dir.mkdir(parents=True, exist_ok=True)
    top = {
        "run_id": run_id, "spec": str(args.spec), "name": spec["name"],
        "repo": str(repo_src), "branch": args.branch,
        "started_at": _utcnow_iso(), "conditions": {},
    }
    for condition in conditions:
        print(f"\n== condition: {condition} ==")
        manifest = run_condition(
            spec, condition, run_dir, repo_src, args.branch,
            review=args.review, agent_cmd=args.agent_cmd,
            timeout=args.timeout,
            continue_within_session=args.continue_within_session,
        )
        top["conditions"][condition] = str(run_dir / condition / "manifest.json")
        (run_dir / "run.json").write_text(json.dumps(top, indent=2),
                                          encoding="utf-8")
        if condition == "ec":
            stats = manifest.get("ec_stats", {})
            print(f"  [{condition}] canonical ECUs: "
                  f"{stats.get('canonical_ecus')}, edges: "
                  f"{stats.get('edges')}, retrieval events: "
                  f"{stats.get('retrieval_events')}")
    top["finished_at"] = _utcnow_iso()
    (run_dir / "run.json").write_text(json.dumps(top, indent=2),
                                      encoding="utf-8")
    print(f"\nDone. Manifests: {run_dir / 'run.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
