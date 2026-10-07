#!/usr/bin/env python3
"""
EC-Bench v2 — Unified Runner + Judge

Runs the benchmark (interactive or headless) and judges each transcript
inline using the full-transcript judge v3 (GLM-5.2 via OpenCode Zen API).

Primary use case: baseline interactive condition
  - Each prompt within a session continues the conversation (chat memory)
  - Fresh chat between sessions (new session = no --continue)
  - EC MCP tools disabled
  - JSONL transcripts captured per prompt
  - Each transcript judged immediately after capture

Can also run EC headless condition (with --condition ec, needs ec package).

To compare against existing EC results, use the same --run-dir:
  python run_and_judge.py \\
      --spec ecbench_v2.json \\
      --repo ~/src/fastapi \\
      --runs-dir runs \\
      --run-id 20260813-141550 \\
      --condition baseline

  This creates runs/20260813-141550/baseline/ alongside existing ec/.
  Scores for both conditions are stored in runs/20260813-141550/scores.json.
  Use --report to generate a comparison report.

Usage:
    # Baseline interactive (default — chat memory within sessions)
    python run_and_judge.py \\
        --spec ecbench_v2.json \\
        --repo ~/src/fastapi \\
        --runs-dir runs

    # Baseline headless (no chat memory)
    python run_and_judge.py \\
        --spec ecbench_v2.json \\
        --repo ~/src/fastapi \\
        --runs-dir runs \\
        --no-continue

    # Skip judging (just capture transcripts)
    python run_and_judge.py \\
        --spec ecbench_v2.json \\
        --repo ~/src/fastapi \\
        --no-judge

    # Judge only (skip running, judge existing transcripts)
    python run_and_judge.py \\
        --spec ecbench_v2.json \\
        --repo ~/src/fastapi \\
        --runs-dir runs \\
        --run-id 20260813-141550 \\
        --judge-only

    # Generate report from existing scores
    python run_and_judge.py \\
        --spec ecbench_v2.json \\
        --repo ~/src/fastapi \\
        --runs-dir runs \\
        --run-id 20260813-141550 \\
        --report-only

Requires:
    - OPENCODE_ZEN_API_KEY environment variable (for judging)
    - opencode CLI on PATH
    - pip install requests
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ============================================================
# Optional EC imports (only needed for --condition ec)
# ============================================================

try:
    from ec.brain import Brain
    from ec.install import AGENTS_MD, server_command
    EC_AVAILABLE = True
except ImportError:
    EC_AVAILABLE = False
    AGENTS_MD = None
    server_command = None
    Brain = None


# ============================================================
# CONFIGURATION
# ============================================================

ZEN_BASE = "https://opencode.ai/zen/v1"
DEFAULT_JUDGE_MODEL = "glm-5.2"
DEFAULT_AGENT_CMD = "opencode run --dir {repo} --format json --auto {prompt}"
DEFAULT_TIMEOUT = 900           # Per-prompt agent timeout (seconds)
DEFAULT_JUDGE_TIMEOUT = 300     # Judge API call timeout (seconds)
MAX_RETRIES = 3                 # Retry on API failures
RETRY_DELAY = 5                 # Seconds between retries
MAX_TRANSCRIPT_CHARS = 80_000  # Max formatted transcript length for judge
TOOL_OUTPUT_LIMIT = 500        # Truncate non-EC tool outputs
EC_TOOL_OUTPUT_LIMIT = 1000    # EC tool outputs get more room


# ============================================================
# METRIC DEFINITIONS
# ============================================================

METRIC_DEFINITIONS = """\
# Metric Definitions

## Architectural Continuity (weight: 0.30)

Evaluates whether the answer maintains consistency with the repository's existing \
architecture, module boundaries, and abstractions.

High scores require:
- The answer respects existing architectural patterns and conventions
- Changes are minimal and consistent with the repository's engineering style
- No unnecessary architectural disruptions

Low scores indicate:
- The answer introduces redundant or conflicting abstractions
- The answer ignores existing patterns in favour of new ones without justification
- Unnecessary architectural changes

---

## Engineering Cognition Reuse (weight: 0.30)

Evaluates whether the answer demonstrates reuse of accumulated engineering \
understanding — prior conclusions, architectural decisions, or debugging \
discoveries that influenced the current approach.

The central question: "Would this answer likely have been different if \
accumulated engineering cognition did not exist?"

High scores require:
- Evidence that prior engineering conclusions influenced current decisions
- Reuse of previously established architectural decisions
- Avoidance of rediscovering known information
- Engineering decisions justified by accumulated understanding

Do not reward:
- Merely mentioning previous knowledge
- Quoting canonical memory
- Copying documentation

Reward only situations where accumulated engineering understanding clearly \
influenced engineering decisions.

Do not assume cognition should exist. If no prior cognition is available, \
evaluate the answer on its own engineering merits — a strong answer that \
reasons from first principles is not penalised for lacking cognition reuse.

---

## Repository Groundedness (weight: 0.15)

Evaluates whether the answer is grounded in the actual repository implementation.

High scores require:
- References to actual modules, functions, classes, and patterns
- Correct understanding of repository structure
- Consistency with the repository's engineering conventions

Low scores indicate:
- Generic answers that could apply to any repository
- Incorrect references to non-existent modules or functions
- Answers that ignore the repository's actual structure

---

## Engineering Quality (weight: 0.15)

Evaluates the overall engineering quality of the answer.

High scores require:
- Correct engineering judgement
- Minimal changes that solve the problem
- Engineering discipline (testing, documentation, error handling)
- Consistency with repository architecture

Low scores indicate:
- Over-engineering or unnecessary complexity
- Missing edge cases
- Poor engineering decisions

---

## Debugging & Investigation Efficiency (weight: 0.10)

Evaluates whether the debugging or investigation process is systematic and \
efficient.

High scores require:
- Systematic investigation process
- Efficient use of prior understanding
- Avoidance of redundant investigation
- Clear reasoning about root causes

Low scores indicate:
- Redundant or unfocused investigation
- Missing obvious debugging steps
- Inefficient exploration
"""


# ============================================================
# JUDGE SYSTEM PROMPT (from judge v3 — full transcript evaluation)
# ============================================================

JUDGE_SYSTEM_PROMPT = """\
# Judge Agent

## Purpose

You are the Judge Agent for EC-Bench. You evaluate the engineering quality of \
one completed response produced by a coding agent.

You exist solely to evaluate engineering behaviour. You must never generate \
solutions, propose improvements, or rewrite the response. Your responsibility \
is evaluation only.

## Important: Transcript-Based Evaluation

You are evaluating via an API call. You do NOT have direct access to the \
repository file system. You receive the FULL AGENT TRANSCRIPT — every text \
block the agent produced, every tool call it made (with inputs and truncated \
outputs), organised by step.

Evaluate the agent session based on:
- The engineering prompt (what the agent was asked to do)
- The full transcript (the agent's reasoning, tool calls, and outputs)
- Both the process AND the final output

The transcript contains:
- [TEXT] blocks: the agent's reasoning, explanations, and final answer
- [TOOL: name] blocks: tool calls with their inputs and outputs

Tool calls show what the agent actually did — which files it read, what \
commands it ran, what it searched for, and what information it retrieved. \
Use this evidence to evaluate investigation efficiency, repository \
groundedness, and cognition reuse.

The transcript is flagged as "CUT-OFF" if the agent was interrupted before \
completing. In that case, evaluate based on what the agent did accomplish.

## Benchmark Philosophy

Every answer is evaluated independently. You must remain completely blind to:
- whether Engineering Cognition was enabled
- whether this is the EMS run or the stateless run
- previous benchmark scores
- expected benchmark outcome

Evaluate only the current response against the engineering prompt.

## Evaluation Pipeline

### Step 1: Read Metric Definitions
Internalise every evaluation metric before evaluating the answer. Metric \
definitions are provided below. Metric definitions must remain fixed throughout \
the evaluation.

### Step 2: Understand the Context
The target repository for this benchmark is FastAPI, a Python web framework. \
The agent was asked to perform engineering tasks on this repository. Evaluate \
the answer in the context of a FastAPI codebase.

### Step 3: Read the Engineering Prompt
Understand the engineering objective, requested task, and engineering \
constraints.

### Step 4: Read the Agent Transcript

Evaluate the full agent transcript. The transcript shows the agent's complete \
session: its reasoning (text blocks), its actions (tool calls), and the results \
of those actions (tool outputs). Evaluate both the process and the output. Do \
not rewrite or improve the agent's work.

### Step 5: Score Every Metric Independently
Score every metric defined below. Do not invent additional metrics. Do not \
merge metrics. Every metric must be scored independently — a low score in one \
metric must not automatically lower another.

### Step 6: Generate Benchmark Summary
Identify the most important engineering observations — strengths and weaknesses.

### Step 7: Compute Weighted Score
Compute the overall score using the weighted formula.

## Evaluation Principles

Never reward:
- longer responses
- more confident wording
- excessive detail
- unnecessary code
- unnecessary architectural changes

Always reward:
- correct engineering judgement
- minimal changes
- consistency with repository architecture
- appropriate reuse
- engineering discipline

## Metric Independence

Every metric must be scored as though every other metric does not exist. \
Perfect scores are allowed. Very low scores are allowed. The score distribution \
should emerge naturally from the engineering evidence.

## Score Scale

Every metric uses continuous scores between 0.0 and 10.0. Use decimal scores \
whenever appropriate (e.g., 3.4, 5.5, 8.7). Do not round unnecessarily.

## Weighted Scoring

| Metric | Weight |
|---------|--------|
| Architectural Continuity | 0.30 |
| Engineering Cognition Reuse | 0.30 |
| Repository Groundedness | 0.15 |
| Engineering Quality | 0.15 |
| Debugging & Investigation Efficiency | 0.10 |

overall_score =
  (architectural_continuity x 0.30)
  + (engineering_cognition_reuse x 0.30)
  + (repository_groundedness x 0.15)
  + (engineering_quality x 0.15)
  + (debugging_investigation_efficiency x 0.10)

Round the final value to two decimal places.

## Judge Behaviour

The Judge must never:
- reward verbosity
- reward unnecessary complexity
- reward creativity that violates repository architecture
- assume cognition should exist
- penalise missing cognition if the repository itself supports the answer

## Engineering Cognition Principle

Engineering Cognition should only receive credit when it changes engineering \
behaviour. Ask: "Would this answer likely have been different if accumulated \
engineering cognition did not exist?"

Evidence includes:
- reuse of previous engineering conclusions
- reuse of previous architectural decisions
- reuse of previous debugging discoveries
- avoidance of rediscovering known information
- engineering decisions justified by accumulated understanding

Do not reward:
- merely mentioning previous knowledge
- quoting canonical memory
- copying documentation

Reward only situations where accumulated engineering understanding clearly \
influenced engineering decisions.

## Engineering Minimality Principle

More code does not imply better engineering. More reasoning does not imply \
better engineering. Prefer answers that solve the task with fewer unnecessary \
changes and better reuse of prior understanding.

## Explain Every Score

Every metric must include a short explanation referencing concrete engineering \
evidence. Avoid vague statements. Prefer specific observations like "Reused the \
existing Repository abstraction instead of introducing a second service layer."

## Evidence Principle

Every assigned score must be supported by observable evidence in the transcript. \
Tool calls and their outputs are primary evidence — they show what the agent \
actually investigated, what files it read, what commands it ran, and what \
information it retrieved. Text blocks show the agent's reasoning and conclusions.

When evidence is ambiguous, prefer a conservative score. The burden of proof \
always lies with the agent's work. Never infer cognition reuse or architectural \
continuity unless clear evidence exists in the transcript.

## Final Output

Return JSON only. Do not include markdown. Do not include prose outside JSON. \
Use the following schema:

```json
{
  "architectural_continuity": {
    "score": 0.0,
    "reason": ""
  },
  "repository_groundedness": {
    "score": 0.0,
    "reason": ""
  },
  "engineering_cognition_reuse": {
    "score": 0.0,
    "reason": ""
  },
  "engineering_quality": {
    "score": 0.0,
    "reason": ""
  },
  "debugging_investigation_efficiency": {
    "score": 0.0,
    "reason": ""
  },
  "overall_score": 0.00,
  "strengths": ["...", "..."],
  "weaknesses": ["...", "..."]
}
```

## Determinism

If the same prompt and same answer are evaluated twice, you should produce \
substantially identical scores. Minimise subjective variation.

---

""" + METRIC_DEFINITIONS


# ============================================================
# SPEC LOADING
# ============================================================

class BenchError(RuntimeError):
    """Benchmark setup/orchestration failure with actionable guidance."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_spec(path: str | Path) -> dict:
    """Load + validate a benchmark spec."""
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


# ============================================================
# AGENT CONFIG BUILDER
# ============================================================

def build_agent_config(
    condition: str,
    ec_home: Path,
    repo: Path,
    branch: str,
    model: str | None = None,
    small_model: str | None = None,
) -> dict:
    """Build the OpenCode config overlay for one condition.

    baseline: EC MCP server explicitly disabled (overrides global config).
    ec: EC MCP server enabled with full environment.
    """
    config: dict = {
        "$schema": "https://opencode.ai/config.json",
    }
    if model:
        config["model"] = model
    if small_model:
        config["small_model"] = small_model

    if condition == "ec":
        if not EC_AVAILABLE:
            raise BenchError(
                "EC condition requires the ec package. Run from the EMS-v2 "
                "directory where 'ec' is installed, or use --condition baseline."
            )
        python, args, env = server_command()
        config["mcp"] = {
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
        }
        config["instructions"] = [str(ec_home / "AGENTS.md")]
    else:
        # Baseline: disable EC to neutralise any global install
        config["mcp"] = {
            "ec": {
                "type": "local",
                "command": ["true"],  # dummy — never runs since disabled
                "enabled": False,
            },
        }
    return config


# ============================================================
# AGENT COMMAND BUILDER + RUNNER
# ============================================================

def _agent_argv(template: str, prompt: str, repo: Path) -> list[str]:
    """Substitute placeholders; prompt is a single argv token (never re-split)."""
    filled = template.replace("{repo}", str(repo))
    filled = filled.replace("{python}", sys.executable or "python3")
    argv = shlex.split(filled)
    for i, token in enumerate(argv):
        if "{prompt}" in token:
            argv[i] = token.replace("{prompt}", prompt)
            return argv
    return argv + [prompt]


def _run_prompt(
    argv: list[str],
    cwd: Path,
    env: dict,
    timeout: int,
    transcript: Path,
    stderr_log: Path,
) -> dict:
    """One agent invocation; stdout (JSON event stream) -> transcript file."""
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


def _run_cli(args: list[str], env: dict, log_path: Path, timeout: int) -> dict:
    """Run a session CLI command, capturing output to a log file."""
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


# ============================================================
# FULL TRANSCRIPT FORMATTING (from judge v3)
# ============================================================

def format_tool_input(name: str, input_data: dict) -> str:
    """Format tool input as a compact readable string."""
    if not input_data:
        return "(none)"

    if name == "bash":
        return f"command: {input_data.get('command', '?')}"

    if name == "read":
        fp = input_data.get("filePath", "?")
        fp_short = fp.split("/")[-1] if "/" in fp else fp
        offset = input_data.get("offset", "")
        limit = input_data.get("limit", "")
        parts = [f"file: {fp_short}"]
        if offset:
            parts.append(f"offset: {offset}")
        if limit:
            parts.append(f"limit: {limit}")
        return ", ".join(parts)

    if name == "grep":
        return f"pattern: {input_data.get('pattern', '?')}, path: {input_data.get('path', '?')}"

    if name == "write":
        fp = input_data.get("filePath", "?")
        fp_short = fp.split("/")[-1] if "/" in fp else fp
        content_len = len(input_data.get("content", ""))
        return f"file: {fp_short} ({content_len} chars written)"

    if name == "edit":
        fp = input_data.get("filePath", "?")
        fp_short = fp.split("/")[-1] if "/" in fp else fp
        old_len = len(input_data.get("oldString", ""))
        new_len = len(input_data.get("newString", ""))
        return f"file: {fp_short} (old: {old_len} chars, new: {new_len} chars)"

    if name == "todowrite":
        todos = input_data.get("todos", [])
        return f"{len(todos)} todos"

    if name == "task":
        desc = input_data.get("description", "?")
        stype = input_data.get("subagent_type", "?")
        return f"desc: {desc}, type: {stype}"

    if name == "ec_ec_get_summary":
        return "(none)"

    if name == "ec_ec_query":
        return f"query: {input_data.get('query', '?')}, mode: {input_data.get('mode', '?')}"

    if name == "ec_ec_observe":
        prompt = input_data.get("user_prompt", "?")
        trace_len = len(input_data.get("reasoning_trace", ""))
        output_len = len(input_data.get("final_output", ""))
        prompt_preview = prompt[:150] + "..." if len(prompt) > 150 else prompt
        return (f"prompt: {prompt_preview} "
                f"(trace: {trace_len} chars, output: {output_len} chars)")

    # Generic fallback
    return json.dumps(input_data, ensure_ascii=False)[:300]


def format_transcript(transcript_path: Path) -> tuple[str | None, dict]:
    """Format a full JSONL transcript as readable text for the judge.

    Renders the complete agent session: every text block, every tool call
    with its arguments and truncated output, organized by step.
    """
    if not transcript_path.exists():
        return None, {"error": "transcript_not_found",
                      "path": str(transcript_path)}

    raw = transcript_path.read_text(encoding="utf-8")
    lines = [l for l in raw.strip().split("\n") if l.strip()]
    if not lines:
        return None, {"error": "empty_transcript"}

    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    if not events:
        return None, {"error": "no_valid_json"}

    # Parse into steps with full detail
    steps = []
    current = None
    for evt in events:
        part = evt.get("part", {})
        ptype = part.get("type", "")

        if ptype in ("step-start", "step_start"):
            current = {"texts": [], "tools": [], "finish_reason": None,
                       "tokens": {}}
        elif ptype in ("step-finish", "step_finish"):
            if current is not None:
                current["finish_reason"] = part.get("reason", "unknown")
                current["tokens"] = part.get("tokens", {})
                steps.append(current)
                current = None
        elif ptype == "text":
            text = part.get("text", "")
            if text and text.strip() != "--- step start ---":
                if current is not None:
                    current["texts"].append(text)
                else:
                    steps.append({"texts": [text], "tools": [],
                                  "finish_reason": "unknown",
                                  "tokens": {}})
        elif ptype == "tool":
            if current is not None:
                current["tools"].append({
                    "name": part.get("tool", "?"),
                    "state": part.get("state", {}),
                })

    if current is not None:
        current["finish_reason"] = "unknown"
        steps.append(current)

    if not steps:
        return None, {"error": "no_steps_found", "event_count": len(events)}

    has_stop = any(s["finish_reason"] == "stop" for s in steps)
    total_tokens = sum(s.get("tokens", {}).get("total", 0) for s in steps)

    # Format as readable text
    parts = []
    parts.append(f"[{len(steps)} steps | finish: {'stop' if has_stop else 'CUT-OFF'} | "
                 f"tokens: {total_tokens}]")
    parts.append("")

    for i, step in enumerate(steps):
        reason = step["finish_reason"] or "unknown"
        step_tokens = step.get("tokens", {}).get("total", 0)

        header = f"### Step {i + 1} (reason: {reason}"
        if step_tokens:
            header += f", tokens: {step_tokens}"
        header += ")"
        parts.append(header)

        # Text blocks
        for text in step["texts"]:
            parts.append(f"[TEXT] {text}")

        # Tool calls
        for tool in step["tools"]:
            name = tool["name"]
            state = tool.get("state", {})
            status = state.get("status", "?")
            input_data = state.get("input", {})
            output = state.get("output", "")

            is_ec = name.startswith("ec_")
            limit = EC_TOOL_OUTPUT_LIMIT if is_ec else TOOL_OUTPUT_LIMIT

            input_summary = format_tool_input(name, input_data)

            if isinstance(output, str):
                output_text = output
            else:
                try:
                    output_text = json.dumps(output, indent=2,
                                             ensure_ascii=False)
                except (TypeError, ValueError):
                    output_text = str(output)

            if len(output_text) > limit:
                output_text = (output_text[:limit]
                               + f"\n[... {len(output_text) - limit} "
                                 f"more chars ...]")

            parts.append(f"[TOOL: {name}] ({status})")
            parts.append(f"  input: {input_summary}")
            if output_text:
                parts.append(f"  output: {output_text}")

        parts.append("")

    result = "\n".join(parts)

    truncated = False
    if len(result) > MAX_TRANSCRIPT_CHARS:
        result = (result[:MAX_TRANSCRIPT_CHARS]
                  + "\n\n[... transcript truncated for length ...]")
        truncated = True

    meta = {
        "event_count": len(events),
        "step_count": len(steps),
        "has_stop": has_stop,
        "potentially_incomplete": not has_stop,
        "transcript_length": len(result),
        "truncated": truncated,
        "total_tokens": total_tokens,
    }

    return result, meta


# ============================================================
# ZEN API CALL
# ============================================================

def call_judge(
    model: str,
    api_key: str,
    system_prompt: str,
    user_message: str,
    timeout: int = 300,
) -> dict:
    """Call GLM (or any OpenAI-compatible model) via OpenCode Zen API.

    Returns the parsed JSON score dict, or raises RuntimeError on failure.
    """
    endpoint = f"{ZEN_BASE}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "response_format": {"type": "json_object"},
        "temperature": 0.0,   # maximise determinism
        "max_tokens": 4096,
    }

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                endpoint, headers=headers, json=payload, timeout=timeout,
            )
            if response.status_code == 429:
                wait = RETRY_DELAY * attempt
                print(f"    Rate limited (429). Waiting {wait}s before retry "
                      f"({attempt}/{MAX_RETRIES})...")
                time.sleep(wait)
                last_error = f"HTTP 429: {response.text[:200]}"
                continue

            if response.status_code != 200:
                last_error = (f"HTTP {response.status_code}: "
                              f"{response.text[:500]}")
                print(f"    API error (attempt {attempt}/{MAX_RETRIES}): "
                      f"{last_error}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            data = response.json()
            content = data["choices"][0]["message"]["content"]

            # Parse the JSON response
            score = json.loads(content)
            return score

        except requests.exceptions.Timeout:
            last_error = f"Request timed out after {timeout}s"
            print(f"    Timeout (attempt {attempt}/{MAX_RETRIES}): {last_error}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        except json.JSONDecodeError as exc:
            last_error = f"Invalid JSON in response: {exc}"
            print(f"    JSON parse error (attempt {attempt}/{MAX_RETRIES}): "
                  f"{last_error}")
            # Try to extract JSON from the content
            try:
                match = re.search(r'\{[\s\S]*\}', content)
                if match:
                    score = json.loads(match.group())
                    return score
            except Exception:
                pass
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        except Exception as exc:
            last_error = str(exc)
            print(f"    Error (attempt {attempt}/{MAX_RETRIES}): {last_error}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    raise RuntimeError(
        f"API call failed after {MAX_RETRIES} attempts: {last_error}")


# ============================================================
# SCORE STORAGE
# ============================================================

SCORES_FILENAME = "scores.json"


def load_scores(run_dir: Path) -> dict:
    """Load existing scores for resume support."""
    path = run_dir / SCORES_FILENAME
    if not path.exists():
        return {"conditions": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"conditions": {}}


def save_scores(run_dir: Path, scores: dict) -> None:
    """Save scores atomically (write to temp then rename)."""
    path = run_dir / SCORES_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(scores, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    tmp.replace(path)


def is_scored(scores: dict, condition: str, session_id: str,
              prompt_idx: str) -> bool:
    """Check if a specific prompt already has a score."""
    cond = scores.get("conditions", {}).get(condition, {})
    sess = cond.get(session_id, {})
    entry = sess.get(prompt_idx, {})
    return "score" in entry and "error" not in entry


# ============================================================
# JUDGING
# ============================================================

def build_user_message(prompt_text: str, transcript_text: str) -> str:
    """Build the user message for the judge."""
    return f"""## Engineering Prompt

{prompt_text}

---

## Agent Transcript

{transcript_text}

---

Evaluate this agent session according to the metric definitions. Return JSON only."""


def judge_one(
    model: str,
    api_key: str,
    prompt_text: str,
    transcript_text: str,
    timeout: int = 300,
) -> dict:
    """Judge a single prompt/transcript pair. Returns the score dict."""
    user_message = build_user_message(prompt_text, transcript_text)
    score = call_judge(model, api_key, JUDGE_SYSTEM_PROMPT, user_message,
                       timeout=timeout)
    return score


# ============================================================
# EC BRAIN STATS (EC condition only)
# ============================================================

def collect_ec_stats(ec_home: Path) -> dict:
    """Brain stats for the ec condition."""
    if not EC_AVAILABLE:
        return {"brain": "ec_package_not_available"}
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


# ============================================================
# CONDITION DRIVER (run + judge inline)
# ============================================================

def run_condition(
    spec: dict,
    condition: str,
    run_dir: Path,
    repo_src: Path,
    branch: str,
    *,
    interactive: bool = True,
    agent_cmd: str = DEFAULT_AGENT_CMD,
    timeout: int = 900,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    judge_timeout: int = DEFAULT_JUDGE_TIMEOUT,
    api_key: str = "",
    do_judge: bool = True,
    force_judge: bool = False,
    review: str = "accept",
    model: str | None = None,
    small_model: str | None = None,
    judge_only: bool = False,
    print_fn=print,
) -> dict:
    """Run all sessions of the spec under one condition, judging inline.

    If judge_only=True, skips running agents and only judges existing transcripts.
    """
    cond_dir = run_dir / condition
    ec_home = cond_dir / "ec_home"
    workdir = (cond_dir / "workdir").resolve()
    logs = cond_dir / "logs"
    transcripts_dir = cond_dir / "transcripts"
    for d in (ec_home, logs, transcripts_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1. Fresh copy of the target repo (skip if judge_only or already exists)
    if not judge_only and not workdir.exists():
        print_fn(f"  [{condition}] copying repo -> {workdir}")
        shutil.copytree(repo_src, workdir, symlinks=True)

    # 2. EC home + agent config (skip if judge_only)
    if not judge_only:
        if condition == "ec" and EC_AVAILABLE:
            (ec_home / "AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
            Brain(ec_home / "ec.db").close()  # create empty brain

        config_path = cond_dir / "opencode_config.json"
        config_path.write_text(
            json.dumps(
                build_agent_config(condition, ec_home, workdir, branch,
                                   model=model, small_model=small_model),
                indent=2),
            encoding="utf-8",
        )

    # 3. Environment setup
    session_cli_env = {
        **os.environ,
        "EC_HOME": str(ec_home),
        "EC_REPO_PATH": str(workdir),
        "EC_BRANCH": branch,
        "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "1"),
    }
    agent_env = {
        **os.environ,
        "OPENCODE_CONFIG": str(cond_dir / "opencode_config.json"),
        # Per-condition session history isolation
        "XDG_DATA_HOME": str(cond_dir / "xdg_data"),
        "XDG_STATE_HOME": str(cond_dir / "xdg_state"),
    }
    if condition == "ec":
        agent_env["EC_HOME"] = str(ec_home)
    # Copy agent auth to isolated XDG dir
    src_auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if src_auth.exists():
        dst_auth = cond_dir / "xdg_data" / "opencode" / "auth.json"
        dst_auth.parent.mkdir(parents=True, exist_ok=True)
        if not dst_auth.exists():
            shutil.copy2(src_auth, dst_auth)

    # 4. Load scores for resume support
    scores = load_scores(run_dir)
    scores.setdefault("conditions", {})
    scores["run_dir"] = str(run_dir)
    scores["model"] = judge_model

    # 5. Manifest
    manifest_path = cond_dir / "manifest.json"
    manifest: dict = {
        "condition": condition,
        "workdir": str(workdir),
        "ec_home": str(ec_home),
        "agent_config": str(cond_dir / "opencode_config.json"),
        "agent_cmd": agent_cmd,
        "interactive": interactive,
        "review": review,
        "started_at": _utcnow_iso(),
        "sessions": [],
    }

    def _flush_manifest():
        manifest_path.write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")

    # 6. Run sessions
    for session in spec["sessions"]:
        sid = session["id"]
        n_prompts = len(session["prompts"])
        print_fn(f"  [{condition}] session {sid} ({n_prompts} prompts)")
        session_rec: dict = {"id": sid, "prompts": []}

        # EC session start
        if condition == "ec" and EC_AVAILABLE and not judge_only:
            session_rec["start"] = _run_cli(
                [sys.executable, "-m", "ec.session", "start",
                 "--repo", str(workdir), "--branch", branch],
                session_cli_env, logs / f"{sid}_start.log", timeout=120)
            if session_rec["start"]["exit_code"] != 0:
                raise BenchError(
                    f"ec.session start failed for {sid} — see "
                    f"{session_rec['start']['log']}")

        # Run/judge each prompt
        for i, prompt in enumerate(session["prompts"], start=1):
            idx = f"{i:02d}"
            transcript = transcripts_dir / sid / f"{idx}.jsonl"
            meta_file = transcripts_dir / sid / f"{idx}.meta.json"
            transcript.parent.mkdir(parents=True, exist_ok=True)

            # --- Step A: Run agent (or skip if already done) ---
            rec = None
            if meta_file.exists():
                prior = json.loads(meta_file.read_text(encoding="utf-8"))
                if prior.get("exit_code") == 0 or prior.get("timed_out"):
                    status = "timed out" if prior.get("timed_out") else "done"
                    print_fn(f"    [{condition}] {sid}/{idx} already {status} "
                             f"— skipping run")
                    rec = {**prior, "prompt": prompt, "skipped_existing": True}
                else:
                    rec = None  # incomplete, re-run

            if rec is None and not judge_only:
                argv = _agent_argv(agent_cmd, prompt, workdir)
                # Interactive mode: add --continue for prompts 2+ within session
                if interactive and i > 1:
                    argv = argv[:-1] + ["--continue"] + argv[-1:]

                print_fn(f"    [{condition}] {sid}/{idx} running agent…")
                rec = _run_prompt(
                    argv, workdir, agent_env, timeout,
                    transcript, transcript.with_suffix(".stderr.log"))
                rec["prompt"] = prompt
                meta_file.write_text(json.dumps(rec, indent=2),
                                     encoding="utf-8")

                status = "TIMED OUT" if rec.get("timed_out") else "done"
                print_fn(f"    [{condition}] {sid}/{idx} {status} "
                         f"({rec['duration_s']}s)")

            elif judge_only:
                # Just check transcript exists
                if not transcript.exists():
                    print_fn(f"    [{condition}] {sid}/{idx} no transcript "
                             f"— skipping")
                    continue
                rec = {"prompt": prompt, "judge_only": True}

            session_rec["prompts"].append(rec or {"prompt": prompt})
            _flush_manifest()

            # --- Step B: Judge inline (if enabled) ---
            if not do_judge:
                continue

            # Check if already scored (resume support)
            if not force_judge and is_scored(scores, condition, sid, idx):
                existing = (scores["conditions"][condition][sid][idx]
                            .get("score", {}))
                overall = existing.get("overall_score", "?")
                print_fn(f"    [{condition}] {sid}/{idx} already judged "
                         f"— overall: {overall}")
                continue

            # Check API key
            if not api_key:
                print_fn(f"    [{condition}] {sid}/{idx} skipping judge "
                         f"— no API key")
                continue

            # Format transcript for judge
            transcript_text, extract_meta = format_transcript(transcript)

            if transcript_text is None:
                print_fn(f"    [{condition}] {sid}/{idx} no transcript text "
                         f"— ({extract_meta.get('error')})")
                entry = {
                    "prompt": prompt[:200] + "..." if len(prompt) > 200
                             else prompt,
                    "error": extract_meta.get("error", "no_transcript"),
                    "judged_at": _utcnow_iso(),
                }
                scores.setdefault("conditions", {}).setdefault(
                    condition, {}).setdefault(sid, {})[idx] = entry
                save_scores(run_dir, scores)
                continue

            # Judge it
            n_chars = len(transcript_text)
            n_steps = extract_meta.get("step_count", "?")
            print_fn(f"    [{condition}] {sid}/{idx} judging ({n_chars} chars, "
                     f"{n_steps} steps)…")

            try:
                score = judge_one(
                    judge_model, api_key, prompt, transcript_text,
                    timeout=judge_timeout)
                overall = score.get("overall_score", "?")
                # Print full per-metric breakdown
                metric_labels = {
                    "architectural_continuity": "arch_cont",
                    "engineering_cognition_reuse": "cog_reuse",
                    "repository_groundedness": "repo_grnd",
                    "engineering_quality": "eng_qual",
                    "debugging_investigation_efficiency": "dbg_eff",
                }
                metric_strs = []
                for mk, label in metric_labels.items():
                    val = score.get(mk, {}).get("score", "?")
                    metric_strs.append(f"{label}={val}")
                metrics_line = "  ".join(metric_strs)
                print_fn(f"    [{condition}] {sid}/{idx} done — overall: {overall}")
                print_fn(f"      {metrics_line}")
                # Print strengths/weaknesses if available
                strengths = score.get("strengths", [])
                weaknesses = score.get("weaknesses", [])
                if strengths:
                    print_fn(f"      strengths: {'; '.join(strengths[:2])}")
                if weaknesses:
                    print_fn(f"      weaknesses: {'; '.join(weaknesses[:2])}")

                entry = {
                    "prompt": prompt[:200] + "..." if len(prompt) > 200
                             else prompt,
                    "transcript_length": n_chars,
                    "meta": {
                        "exit_code": rec.get("exit_code"),
                        "duration_s": rec.get("duration_s"),
                        "timed_out": rec.get("timed_out", False),
                        **extract_meta,
                    },
                    "score": score,
                    "transcript_preview": transcript_text[:500],
                    "judged_at": _utcnow_iso(),
                }
            except Exception as exc:
                print_fn(f"    [{condition}] {sid}/{idx} JUDGE FAILED — {exc}")
                entry = {
                    "prompt": prompt[:200] + "..." if len(prompt) > 200
                             else prompt,
                    "transcript_length": n_chars,
                    "error": str(exc),
                    "judged_at": _utcnow_iso(),
                }

            scores.setdefault("conditions", {}).setdefault(
                condition, {}).setdefault(sid, {})[idx] = entry
            save_scores(run_dir, scores)

        # EC session stop
        if condition == "ec" and EC_AVAILABLE and not judge_only:
            session_rec["stop"] = _run_cli(
                [sys.executable, "-m", "ec.session", "stop",
                 f"--all-{review}",
                 "--repo", str(workdir), "--branch", branch],
                session_cli_env, logs / f"{sid}_stop.log", timeout=600)
            stop_log = Path(session_rec["stop"]["log"]).read_text(
                encoding="utf-8")
            session_rec["stop"]["diffusion_warning"] = \
                "promoted without diffusion" in stop_log
            if session_rec["stop"]["exit_code"] != 0:
                raise BenchError(
                    f"ec.session stop failed for {sid} — see "
                    f"{session_rec['stop']['log']}")

        manifest["sessions"].append(session_rec)
        _flush_manifest()

    # 7. EC brain stats
    if condition == "ec" and not judge_only:
        manifest["ec_stats"] = collect_ec_stats(ec_home)

    manifest["finished_at"] = _utcnow_iso()
    _flush_manifest()

    # 8. Print condition summary
    cond_scores = scores.get("conditions", {}).get(condition, {})
    all_scores = []
    for sid_data in cond_scores.values():
        for entry in sid_data.values():
            if "score" in entry:
                all_scores.append(entry["score"].get("overall_score", 0))

    if all_scores:
        avg = sum(all_scores) / len(all_scores)
        print_fn(f"\n  [{condition}] {len(all_scores)}/{sum(len(s['prompts']) for s in spec['sessions'])} "
                 f"judged — avg: {avg:.2f}")

    return manifest


# ============================================================
# SUMMARY REPORT
# ============================================================

WEIGHTS = {
    "architectural_continuity": 0.30,
    "engineering_cognition_reuse": 0.30,
    "repository_groundedness": 0.15,
    "engineering_quality": 0.15,
    "debugging_investigation_efficiency": 0.10,
}


def generate_report(scores: dict, run_dir: Path) -> str:
    """Generate a human-readable summary report."""
    lines = []
    lines.append("=" * 70)
    lines.append("EC-Bench v2 — Summary Report")
    lines.append("=" * 70)
    lines.append(f"Run: {scores.get('run_dir', '?')}")
    lines.append(f"Judge Model: {scores.get('model', '?')}")
    lines.append(f"Finished: {scores.get('finished_at', '?')}")
    lines.append("")

    conditions = scores.get("conditions", {})

    for cond_name, cond_data in sorted(conditions.items()):
        lines.append(f"--- Condition: {cond_name} ---")
        cond_scores = []
        cond_metrics = {k: [] for k in WEIGHTS}

        for sid in sorted(cond_data.keys()):
            sess_data = cond_data[sid]
            sess_scores = []
            sess_metrics = {k: [] for k in WEIGHTS}
            n_judged = 0
            n_errors = 0

            for idx in sorted(sess_data.keys()):
                entry = sess_data[idx]
                if "score" in entry:
                    s = entry["score"]
                    overall = s.get("overall_score", 0)
                    sess_scores.append(overall)
                    cond_scores.append(overall)
                    n_judged += 1
                    for k in WEIGHTS:
                        val = s.get(k, {}).get("score")
                        if val is not None:
                            sess_metrics[k].append(val)
                            cond_metrics[k].append(val)
                elif "error" in entry:
                    n_errors += 1

            if sess_scores:
                avg = sum(sess_scores) / len(sess_scores)
                lines.append(f"  {sid}: avg={avg:.2f} "
                             f"({n_judged} judged, {n_errors} errors)")
                for k in WEIGHTS:
                    if sess_metrics[k]:
                        m_avg = sum(sess_metrics[k]) / len(sess_metrics[k])
                        lines.append(f"    {k}: {m_avg:.2f}")
            else:
                lines.append(f"  {sid}: no scores ({n_errors} errors)")

        if cond_scores:
            avg = sum(cond_scores) / len(cond_scores)
            lines.append(f"  OVERALL: avg={avg:.2f} ({len(cond_scores)} prompts)")
            for k in WEIGHTS:
                if cond_metrics[k]:
                    m_avg = sum(cond_metrics[k]) / len(cond_metrics[k])
                    lines.append(f"    {k}: {m_avg:.2f}")
        lines.append("")

    # Comparison table
    conds_with_scores = {
        c: [s["score"]["overall_score"]
            for sid in conditions.get(c, {}).values()
            for s in sid.values()
            if "score" in s]
        for c in conditions
    }
    conds_with_scores = {k: v for k, v in conds_with_scores.items() if v}

    if len(conds_with_scores) >= 2:
        lines.append("--- Comparison ---")
        lines.append(f"{'Condition':<25} {'Avg':>8} {'N':>5}")
        lines.append("-" * 40)
        for c in sorted(conds_with_scores.keys()):
            vals = conds_with_scores[c]
            avg = sum(vals) / len(vals)
            lines.append(f"{c:<25} {avg:>8.2f} {len(vals):>5}")
        lines.append("")

        # Metric-by-metric comparison
        lines.append("--- Metric Comparison ---")
        header = f"{'Metric':<40}"
        for c in sorted(conds_with_scores.keys()):
            header += f" {c:>15}"
        header += f" {'Weight':>7}"
        lines.append(header)
        lines.append("-" * (40 + 16 * len(conds_with_scores) + 8))

        for k, w in WEIGHTS.items():
            row = f"{k:<40}"
            for c in sorted(conds_with_scores.keys()):
                vals = []
                for sid in conditions.get(c, {}).values():
                    for entry in sid.values():
                        if "score" in entry:
                            v = entry["score"].get(k, {}).get("score")
                            if v is not None:
                                vals.append(v)
                if vals:
                    row += f" {sum(vals)/len(vals):>15.2f}"
                else:
                    row += f" {'N/A':>15}"
            row += f" {w:>7.2f}"
            lines.append(row)
        lines.append("")

        # Per-prompt comparison
        lines.append("--- Per-Prompt Comparison ---")
        # Get all session/prompt indices
        all_sessions = set()
        for c in conds_with_scores:
            for sid in conditions.get(c, {}):
                all_sessions.add(sid)
        for sid in sorted(all_sessions):
            lines.append(f"  {sid}:")
            header = f"    {'Prompt':<8}"
            for c in sorted(conds_with_scores.keys()):
                header += f" {c:>15}"
            header += f" {'Delta':>8}"
            lines.append(header)
            lines.append("    " + "-" * (8 + 16 * len(conds_with_scores) + 9))

            # Get all prompt indices for this session
            all_idxs = set()
            for c in conds_with_scores:
                for idx in conditions.get(c, {}).get(sid, {}):
                    all_idxs.add(idx)
            for idx in sorted(all_idxs):
                row = f"    {idx:<8}"
                vals = {}
                for c in sorted(conds_with_scores.keys()):
                    entry = conditions.get(c, {}).get(sid, {}).get(idx, {})
                    if "score" in entry:
                        v = entry["score"].get("overall_score", 0)
                        row += f" {v:>15.2f}"
                        vals[c] = v
                    else:
                        row += f" {'—':>15}"
                if len(vals) >= 2:
                    sorted_vals = sorted(vals.values())
                    delta = sorted_vals[-1] - sorted_vals[0]
                    row += f" {delta:>+8.2f}"
                else:
                    row += f" {'—':>8}"
                lines.append(row)
            lines.append("")

    lines.append("--- Fairness Note ---")
    lines.append(
        "The 'Engineering Cognition Reuse' metric (30% weight) evaluates whether "
        "the answer shows evidence of reusing accumulated engineering "
        "understanding. In a no-EC baseline condition, there is no accumulated "
        "cognition to reuse. The judge is blind to condition, so baseline "
        "answers may receive lower scores on this metric by design. This is "
        "the central experimental variable — interpret comparison results with "
        "this context."
    )
    lines.append("")

    report = "\n".join(lines)

    # Save report
    report_path = run_dir / "judge_report.txt"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved to: {report_path}")
    return report


# ============================================================
# CLI
# ============================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_and_judge.py",
        description="EC-Bench v2: run benchmark + judge transcripts inline.",
    )
    parser.add_argument("--spec", required=True,
                        help="Benchmark spec JSON (ecbench_v2.json)")
    parser.add_argument("--repo", required=True,
                        help="Target repository path")
    parser.add_argument("--branch", default="main",
                        help="Branch (default: main)")
    parser.add_argument("--runs-dir", default="runs",
                        help="Output root (default: runs)")
    parser.add_argument("--run-id", default=None,
                        help="Run ID (default: timestamp; reuse to resume)")
    parser.add_argument("--condition", default="baseline",
                        help="Condition name (default: baseline)")
    parser.add_argument("--no-continue", action="store_true",
                        help="Disable --continue (headless mode, no chat memory)")
    parser.add_argument("--agent-cmd", default=DEFAULT_AGENT_CMD,
                        help="Agent command template; placeholders: "
                             "{repo} {python} {prompt}")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Per-prompt timeout seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                        help=f"Judge model (default: {DEFAULT_JUDGE_MODEL})")
    parser.add_argument("--judge-timeout", type=int, default=DEFAULT_JUDGE_TIMEOUT,
                        help=f"Judge API timeout (default: {DEFAULT_JUDGE_TIMEOUT})")
    parser.add_argument("--no-judge", action="store_true",
                        help="Skip judging (just run transcripts)")
    parser.add_argument("--force-judge", action="store_true",
                        help="Re-judge all prompts, even already scored")
    parser.add_argument("--judge-only", action="store_true",
                        help="Skip running agents; only judge existing transcripts")
    parser.add_argument("--report", action="store_true",
                        help="Generate summary report after completion")
    parser.add_argument("--report-only", action="store_true",
                        help="Only generate report from existing scores")
    parser.add_argument("--model", default=None,
                        help="Override coding model in agent config")
    parser.add_argument("--small-model", default=None,
                        help="Override small model in agent config")
    parser.add_argument("--review", choices=("accept", "skip"), default="accept",
                        help="EC review-gate mode (default: accept)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan; write nothing")
    args = parser.parse_args(argv)

    # Load spec
    try:
        spec = load_spec(args.spec)
    except BenchError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    repo_src = Path(args.repo).expanduser().resolve()
    if not repo_src.is_dir():
        print(f"target repo not found: {repo_src}", file=sys.stderr)
        return 1

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.runs_dir).expanduser().resolve() / run_id
    total = sum(len(s["prompts"]) for s in spec["sessions"])
    interactive = not args.no_continue

    # Report-only mode
    if args.report_only:
        scores = load_scores(run_dir)
        report = generate_report(scores, run_dir)
        print(report)
        return 0

    # Check API key for judging
    api_key = os.environ.get("OPENCODE_ZEN_API_KEY", "")
    do_judge = not args.no_judge
    if do_judge and not api_key and not args.judge_only:
        print("Warning: OPENCODE_ZEN_API_KEY not set. Transcripts will be "
              "captured but NOT judged.", file=sys.stderr)
        do_judge = False
    if args.judge_only and not api_key:
        print("Error: OPENCODE_ZEN_API_KEY not set. Needed for judging.",
              file=sys.stderr)
        return 1

    # Print plan
    mode = "interactive (chat memory)" if interactive else "headless (no chat memory)"
    print(f"EC-Bench run {run_id}")
    print(f"  spec:       {args.spec} ({spec['name']}: "
          f"{len(spec['sessions'])} sessions, {total} prompts)")
    print(f"  repo:       {repo_src} (branch {args.branch})")
    print(f"  condition:  {args.condition}")
    print(f"  mode:       {mode}")
    print(f"  run dir:    {run_dir}")
    print(f"  agent cmd:  {args.agent_cmd}")
    print(f"  timeout:   {args.timeout}s per prompt")
    if do_judge:
        print(f"  judge:      {args.judge_model} (timeout {args.judge_timeout}s)")
    else:
        print(f"  judge:      skipped")
    if args.judge_only:
        print(f"  judge-only: existing transcripts will be judged")

    if args.dry_run:
        cond_dir = run_dir / args.condition
        print(f"\n[dry-run] condition '{args.condition}':")
        print(f"  config:    {cond_dir / 'opencode_config.json'}")
        print(f"  workdir:   {cond_dir / 'workdir'} (fresh copy of repo)")
        print(f"  transcripts: {cond_dir / 'transcripts'}")
        for session in spec["sessions"]:
            print(f"  session {session['id']}: {len(session['prompts'])} prompts")
        print("\ndry run — nothing written")
        return 0

    # Create run directory
    run_dir.mkdir(parents=True, exist_ok=True)

    # Write run.json
    top = {
        "run_id": run_id, "spec": str(args.spec), "name": spec["name"],
        "repo": str(repo_src), "branch": args.branch,
        "started_at": _utcnow_iso(), "conditions": {},
    }
    (run_dir / "run.json").write_text(json.dumps(top, indent=2),
                                      encoding="utf-8")

    # Run the condition
    print(f"\n== condition: {args.condition} ==")
    manifest = run_condition(
        spec, args.condition, run_dir, repo_src, args.branch,
        interactive=interactive,
        agent_cmd=args.agent_cmd,
        timeout=args.timeout,
        judge_model=args.judge_model,
        judge_timeout=args.judge_timeout,
        api_key=api_key,
        do_judge=do_judge,
        force_judge=args.force_judge,
        review=args.review,
        model=args.model,
        small_model=args.small_model,
        judge_only=args.judge_only,
    )

    top["conditions"][args.condition] = str(
        run_dir / args.condition / "manifest.json")
    top["finished_at"] = _utcnow_iso()
    (run_dir / "run.json").write_text(json.dumps(top, indent=2),
                                      encoding="utf-8")

    # Print EC stats if available
    if args.condition == "ec":
        stats = manifest.get("ec_stats", {})
        print(f"\n  [{args.condition}] canonical ECUs: "
              f"{stats.get('canonical_ecus')}, edges: "
              f"{stats.get('edges')}, retrieval events: "
              f"{stats.get('retrieval_events')}")

    # Generate report if requested
    if args.report:
        scores = load_scores(run_dir)
        scores["finished_at"] = _utcnow_iso()
        save_scores(run_dir, scores)
        report = generate_report(scores, run_dir)
        print()
        print(report)

    print(f"\nDone. Run dir: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
