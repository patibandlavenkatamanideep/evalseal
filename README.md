# EvalSeal

**Reproducibility receipts for LLM evals: run it N times, report the score with its noise, and seal what actually ran.**

## The problem

An eval score from a single run is one sample of a random process. Run the same eval again
and borderline items quietly flip from PASS to FAIL, especially when an LLM judge grades
them. Reports also name the model you *asked* for, not the one that *answered*. EvalSeal
measures the flips, records the real provenance, and seals both into a tamper-evident ledger.

## Quickstart

```bash
pip install -e ".[dev]"          # from source until the PyPI release

# Replays the committed cassette: no API key, no network.
evalseal run \
  --dataset examples/borderline_judge/dataset.jsonl \
  --target-config examples/borderline_judge/target.json \
  --scorer-config examples/borderline_judge/scorer.json \
  --n 5
```

## Real flip rates

<!-- TODO: replace with the table from the recorded run (report.md). Do not invent numbers. -->
> **Not recorded yet.** Capture the demo cassette once with a real key:
>
> ```bash
> EVALSEAL_RECORD=1 EVALSEAL_API_KEY=... evalseal run \
>   --dataset examples/borderline_judge/dataset.jsonl \
>   --target-config examples/borderline_judge/target.json \
>   --scorer-config examples/borderline_judge/scorer.json --n 5
> ```
>
> Then paste the per-case table from `report.md` here and commit `tests/cassettes/run.json`.

Each row of the report looks like:

| case | verdicts | mean | 95% CI | flip rate | stability |
|---|---|---|---|---|---|
| b01 | `PFPPF` | … | … | … | … |

## Commands

| command | what it does | exit code |
|---|---|---|
| `evalseal run` | Runs each case N times, analyzes variance, seals a record, writes `report.json` + `report.md`. | `0` all stable/borderline · `3` any case UNSTABLE · `1` error |
| `evalseal verify` | Recomputes every hash in `.evalseal/ledger.jsonl` and checks the chain links. | `0` intact · `1` tampered or broken |
| `evalseal diff A B` | Compares two ledger runs and says whether the mean moved beyond the noise floor. Use `--` for negative indices: `evalseal diff -- 0 -1`. | `0` |

**Stability classes** are based on the flip rate, the share of a case's N verdicts that
disagree with its majority: `STABLE` (0), `BORDERLINE` (≤ 20%), `UNSTABLE` (> 20%).

**Provenance warnings** show up in the report when:
- the served model differs from the requested one (for the target or the judge),
- the endpoint isn't a canonical provider host,
- temperature was left at the provider default,
- the served model or system fingerprint changed partway through the run.

## How it works

The executor sends each prompt to the target N times and scores every response. An LLM
judge is itself a target, so its own randomness is measured instead of assumed away.
`analyze.py` computes the mean, a seeded bootstrap 95% CI, and the flip rate for each case.
Every request goes through a cassette. In record mode, real responses are saved in call
order; in replay mode, which is the default and what CI uses, they are served back, and a
missing entry fails loudly. Each run is saved as a `RunRecord`: its manifest (requested vs.
served model, fingerprint, parameters and whether they were set explicitly, rubric hash,
dataset hash) plus its results. The record is hashed and linked to the previous record's
hash in an append-only JSONL ledger, so editing any past score breaks `verify`.

See [DESIGN.md](DESIGN.md) for what this does and does not prove.
