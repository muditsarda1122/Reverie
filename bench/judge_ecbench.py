#!/usr/bin/env python3
"""
EC-Bench v2 Judge — evaluates agent answers using GLM-5.2 via OpenCode Zen API.

Reads JSONL transcripts from a benchmark run directory, extracts the final answer
from each, calls GLM-5.2 with the judge system prompt + engineering prompt +
answer, and stores scores in scores.json.

Supports resume: skips prompts that already have scores.

Usage:
    # Judge all conditions in a run
    python judge_ecbench.py \\
        --run-dir runs/20260813-141550 \\
        --spec ecbench_v2.json

    # Judge only EC condition
    python judge_ecbench.py \\
        --run-dir runs/20260813-141550 \\
        --spec ecbench_v2.json \\
        --conditions ec

    # Use a different model
    python judge_ecbench.py \\
        --run-dir runs/20260813-141550 \\
        --spec ecbench_v2.json \\
        --model glm-5.2

    # Generate a summary report after judging
    python judge_ecbench.py \\
        --run-dir runs/20260813-141550 \\
        --spec ecbench_v2.json \\
        --report

Requires:
    - OPENCODE_ZEN_API_KEY environment variable
    - pip install requests
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ============================================================
# CONFIGURATION
# ============================================================

ZEN_BASE = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "glm-5.2"
DEFAULT_TIMEOUT = 300        # Zen API call timeout (seconds)
MAX_RETRIES = 3             # Retry on API failures
RETRY_DELAY = 5             # Seconds between retries
MAX_ANSWER_CHARS = 50_000   # Truncate very long answers to avoid token limits

# ============================================================
# METRIC DEFINITIONS (embedded — no external metrics.md needed)
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
# ADAPTED JUDGE SYSTEM PROMPT
# ============================================================

JUDGE_SYSTEM_PROMPT = """\
# Judge Agent

## Purpose

You are the Judge Agent for EC-Bench. You evaluate the engineering quality of \
one completed response produced by a coding agent.

You exist solely to evaluate engineering behaviour. You must never generate \
solutions, propose improvements, or rewrite the response. Your responsibility \
is evaluation only.

## Important: API-Based Evaluation

You are evaluating via an API call. You do NOT have direct access to the \
repository file system or any .repo-memory/ directory. Evaluate the submitted \
answer based on:
- The engineering prompt (what the agent was asked to do)
- The submitted answer (the agent's final response)
- The answer's own internal evidence

The answer should reference actual repository elements (modules, functions, \
patterns). Evaluate whether these references are consistent and grounded, \
even though you cannot directly inspect the repository.

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

### Step 4: Read the Submitted Answer
Evaluate only the submitted answer. Do not rewrite it. Do not improve it. Do \
not speculate about alternative implementations.

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

Every assigned score must be supported by observable evidence. When evidence \
is ambiguous, prefer a conservative score. The burden of proof always lies \
with the submitted answer. Never infer Engineering Cognition Reuse or \
Architectural Continuity unless clear evidence exists.

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
# ANSWER EXTRACTION
# ============================================================

def extract_answer(transcript_path: Path) -> tuple[str | None, dict]:
    """Extract the final answer from a JSONL transcript.

    Returns (answer_text, metadata) where metadata includes event count,
    text block count, and whether the transcript appears complete.

    The final answer is the last text event (part.type == "text") that is
    not a step marker. If the transcript ends mid-step with no text output,
    returns None.
    """
    if not transcript_path.exists():
        return None, {"error": "transcript_not_found", "path": str(transcript_path)}

    raw = transcript_path.read_text(encoding="utf-8")
    lines = [l for l in raw.strip().split("\n") if l.strip()]
    if not lines:
        return None, {"error": "empty_transcript", "path": str(transcript_path)}

    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    if not events:
        return None, {"error": "no_valid_json", "path": str(transcript_path)}

    # Collect all text blocks (non-step-marker)
    text_blocks = []
    for evt in events:
        part = evt.get("part", {})
        if part.get("type") == "text":
            text = part.get("text", "")
            if text and text.strip() != "--- step start ---":
                text_blocks.append(text)

    # Check if the transcript ends mid-step (step_start without step_finish)
    last_event = events[-1]
    last_type = last_event.get("type", "")
    ends_mid_step = last_type in ("step_start", "step-start")

    meta = {
        "event_count": len(events),
        "text_block_count": len(text_blocks),
        "ends_mid_step": ends_mid_step,
        "text_block_lengths": [len(t) for t in text_blocks[-5:]],  # last 5
    }

    if not text_blocks:
        return None, {**meta, "error": "no_text_blocks"}

    # Return the last text block as the final answer
    answer = text_blocks[-1]

    # Truncate if too long
    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[:MAX_ANSWER_CHARS] + "\n\n[... truncated for length ...]"
        meta["truncated"] = True

    return answer, meta


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
                endpoint, headers=headers, json=payload, timeout=timeout
            )
            if response.status_code == 429:
                # Rate limited — wait and retry
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
                # Sometimes the model wraps JSON in markdown code blocks
                import re
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

    raise RuntimeError(f"API call failed after {MAX_RETRIES} attempts: {last_error}")


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

def build_user_message(prompt_text: str, answer_text: str) -> str:
    """Build the user message for the judge."""
    return f"""## Engineering Prompt

{prompt_text}

---

## Submitted Answer

{answer_text}

---

Evaluate this answer according to the metric definitions. Return JSON only."""


def judge_one(
    model: str,
    api_key: str,
    prompt_text: str,
    answer_text: str,
    timeout: int = 300,
) -> dict:
    """Judge a single prompt/answer pair. Returns the score dict."""
    user_message = build_user_message(prompt_text, answer_text)
    score = call_judge(model, api_key, JUDGE_SYSTEM_PROMPT, user_message,
                       timeout=timeout)
    return score


def run_judge(
    run_dir: Path,
    spec: dict,
    conditions: list[str],
    model: str,
    api_key: str,
    sessions_filter: list[str] | None = None,
    timeout: int = 300,
) -> dict:
    """Run the judge on all specified conditions and sessions."""
    scores = load_scores(run_dir)
    scores.setdefault("conditions", {})
    scores["run_dir"] = str(run_dir)
    scores["model"] = model
    scores["started_at"] = datetime.now(timezone.utc).isoformat()

    total = 0
    already = 0
    judged = 0
    errors = 0

    for condition in conditions:
        cond_dir = run_dir / condition
        transcripts_dir = cond_dir / "transcripts"
        if not transcripts_dir.exists():
            print(f"\n  [{condition}] no transcripts directory — skipping")
            continue

        scores["conditions"].setdefault(condition, {})
        print(f"\n  [{condition}] judging...")

        for session in spec["sessions"]:
            sid = session["id"]
            if sessions_filter and sid not in sessions_filter:
                continue

            scores["conditions"][condition].setdefault(sid, {})
            sess_dir = transcripts_dir / sid
            if not sess_dir.exists():
                print(f"    [{condition}/{sid}] directory not found — skipping")
                continue

            n_prompts = len(session["prompts"])
            print(f"    [{condition}/{sid}] {n_prompts} prompts")

            for i, prompt_text in enumerate(session["prompts"], start=1):
                idx = f"{i:02d}"
                total += 1

                # Resume check
                if is_scored(scores, condition, sid, idx):
                    already += 1
                    print(f"      {idx} already scored — skipping")
                    continue

                transcript = sess_dir / f"{idx}.jsonl"
                meta_file = sess_dir / f"{idx}.meta.json"

                # Load meta for context
                meta = {}
                if meta_file.exists():
                    try:
                        meta = json.loads(
                            meta_file.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        pass

                # Extract answer
                answer, extract_meta = extract_answer(transcript)

                entry = {
                    "prompt": prompt_text[:200] + "..."
                    if len(prompt_text) > 200 else prompt_text,
                    "answer_length": len(answer) if answer else 0,
                    "meta": {
                        "exit_code": meta.get("exit_code"),
                        "duration_s": meta.get("duration_s"),
                        "timed_out": meta.get("timed_out", False),
                        **extract_meta,
                    },
                }

                if answer is None:
                    entry["error"] = extract_meta.get(
                        "error", "no_answer")
                    entry["judged_at"] = datetime.now(
                        timezone.utc).isoformat()
                    scores["conditions"][condition][sid][idx] = entry
                    save_scores(run_dir, scores)
                    errors += 1
                    print(f"      {idx} no answer found — "
                          f"({extract_meta.get('error')})")
                    continue

                # Judge it
                print(f"      {idx} judging ({len(answer)} chars)...")
                try:
                    score = judge_one(model, api_key, prompt_text, answer,
                                      timeout=timeout)
                    entry["score"] = score
                    entry["answer_preview"] = answer[:500]
                    entry["judged_at"] = datetime.now(
                        timezone.utc).isoformat()
                    scores["conditions"][condition][sid][idx] = entry
                    save_scores(run_dir, scores)
                    judged += 1
                    overall = score.get("overall_score", "?")
                    print(f"      {idx} done — overall: {overall}")
                except Exception as exc:
                    entry["error"] = str(exc)
                    entry["judged_at"] = datetime.now(
                        timezone.utc).isoformat()
                    scores["conditions"][condition][sid][idx] = entry
                    save_scores(run_dir, scores)
                    errors += 1
                    print(f"      {idx} FAILED — {exc}")

    scores["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_scores(run_dir, scores)

    print(f"\n  Judging complete: {judged} judged, {already} skipped, "
          f"{errors} errors, {total} total")
    return scores


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
    lines.append("EC-Bench v2 Judge — Summary Report")
    lines.append("=" * 70)
    lines.append(f"Run: {scores.get('run_dir', '?')}")
    lines.append(f"Model: {scores.get('model', '?')}")
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
        lines.append(f"{'Condition':<15} {'Avg':>8} {'N':>5}")
        lines.append("-" * 30)
        for c in sorted(conds_with_scores.keys()):
            vals = conds_with_scores[c]
            avg = sum(vals) / len(vals)
            lines.append(f"{c:<15} {avg:>8.2f} {len(vals):>5}")
        lines.append("")

        # Metric-by-metric comparison
        lines.append("--- Metric Comparison ---")
        header = f"{'Metric':<40}"
        for c in sorted(conds_with_scores.keys()):
            header += f" {c:>10}"
        header += f" {'Weight':>7}"
        lines.append(header)
        lines.append("-" * (40 + 11 * len(conds_with_scores) + 8))

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
                    row += f" {sum(vals)/len(vals):>10.2f}"
                else:
                    row += f" {'N/A':>10}"
            row += f" {w:>7.2f}"
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
        prog="judge_ecbench.py",
        description="EC-Bench v2 Judge: evaluate agent answers via GLM-5.2.",
    )
    parser.add_argument(
        "--run-dir", required=True,
        help="Path to the run directory (e.g., runs/20260813-141550)")
    parser.add_argument(
        "--spec", required=True,
        help="Path to the benchmark spec JSON (ecbench_v2.json)")
    parser.add_argument(
        "--conditions", default="ec,baseline",
        help="Comma-separated conditions to judge (default: ec,baseline)")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Judge model name (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--sessions", default=None,
        help="Comma-separated session IDs to judge (default: all)")
    parser.add_argument(
        "--report", action="store_true",
        help="Generate a summary report after judging")
    parser.add_argument(
        "--report-only", action="store_true",
        help="Only generate the report from existing scores (no API calls)")
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help=f"API call timeout in seconds (default: {DEFAULT_TIMEOUT})")
    args = parser.parse_args(argv)

    # Load spec
    spec_path = Path(args.spec).expanduser().resolve()
    if not spec_path.is_file():
        print(f"Error: spec file not found: {spec_path}", file=sys.stderr)
        return 1
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Error: spec is not valid JSON: {exc}", file=sys.stderr)
        return 1

    run_dir = Path(args.run_dir).expanduser().resolve()
    if not run_dir.is_dir():
        print(f"Error: run directory not found: {run_dir}", file=sys.stderr)
        return 1

    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]

    sessions_filter = None
    if args.sessions:
        sessions_filter = [s.strip() for s in args.sessions.split(",")
                           if s.strip()]

    # Report-only mode
    if args.report_only:
        scores = load_scores(run_dir)
        report = generate_report(scores, run_dir)
        print(report)
        return 0

    # Check API key
    api_key = os.environ.get("OPENCODE_ZEN_API_KEY", "")
    if not api_key:
        print("Error: OPENCODE_ZEN_API_KEY environment variable not set.",
              file=sys.stderr)
        print("Get your key from https://opencode.ai/auth", file=sys.stderr)
        return 1

    print(f"EC-Bench v2 Judge")
    print(f"  run dir:    {run_dir}")
    print(f"  spec:       {spec_path}")
    print(f"  model:      {args.model}")
    print(f"  conditions: {', '.join(conditions)}")
    if sessions_filter:
        print(f"  sessions:   {', '.join(sessions_filter)}")
    print()

    scores = run_judge(
        run_dir, spec, conditions, args.model, api_key, sessions_filter,
        timeout=args.timeout,
    )

    if args.report:
        report = generate_report(scores, run_dir)
        print()
        print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
