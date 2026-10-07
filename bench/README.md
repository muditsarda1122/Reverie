# EC-Bench — Benchmark Harness for Reverie

EC-Bench measures whether a coding agent that accumulates **engineering
cognition** across sequential sessions produces better work than a stateless
baseline. It drives a real coding agent through N sequential sessions × M
prompts on a target repository, in two conditions — with Reverie (EC) and
without — and judges the transcripts with an LLM judge.

> **Status:** EC-Bench has **not** yet shown a clear advantage for the EC
> condition. It is published as an honest research harness, not as proof.
> See the [research page](https://ems-gold-seven.vercel.app/research) for the
> current picture.

## Layout

| File | Purpose |
|---|---|
| `ecbench_v2.json` | The benchmark spec: sessions and prompts for a target repository |
| `run_and_judge.py` | Full harness: run the agent (both conditions) and judge the transcripts |
| `judge_ecbench.py` | Judge only — scores JSONL transcripts from a run directory |
| `judge_ecbench_v3_fulltranscript.py` | Judge variant that scores from full transcripts |

No benchmark run outputs are included in this repository.

## Running

The runner lives in the `ec` package (`python -m ec.run_ecbench`); the
harness in this folder wraps it. Both need the target repository locally and,
for the judge, an API key:

```bash
export OPENCODE_ZEN_API_KEY=...   # judge + agent need an LLM

# Full run + judge (one command, resumable)
python bench/run_and_judge.py \
    --spec bench/ecbench_v2.json \
    --repo /path/to/target-repo \
    --branch main \
    --runs-dir runs

# Run only (judge later)
python -m ec.run_ecbench \
    --spec bench/ecbench_v2.json \
    --repo /path/to/target-repo

# Judge only, from an existing run directory
python bench/judge_ecbench.py --run-dir runs/<run-id> --spec bench/ecbench_v2.json
```

Outputs go to `runs/` (gitignored): per-session transcripts, the EC brain
state, and judge scores. A full run takes tens of hours (agent runs dominate).

## Notes

- Conditions: `ec` (Reverie installed and wired into the agent) and
  `baseline` (stateless).
- The judge model and scoring prompts are defined in the judge scripts.
- Specs are repository-specific: `ecbench_v2.json` targets a FastAPI-like
  codebase. Write a new spec JSON for other target repositories.
