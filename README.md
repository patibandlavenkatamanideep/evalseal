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

Recorded 2026-09-15 and committed in `tests/cassettes/run.json`: `gemini-2.5-flash` as both
the target and the judge, temperature left at the provider default, 20 arguable prompts,
5 runs each. The quickstart above replays exactly this run.

**Mean score: 0.92, but 5 of 20 cases did not get the same verdict every time.**

| case | prompt | verdicts | mean | 95% CI | flip rate | stability |
|---|---|---|---|---|---|---|
| b01 | Is a hot dog a sandwich? | `FPPFP` | 0.60 | [0.20, 1.00] | 40% | UNSTABLE |
| b10 | Blockchain for a child in exactly 20 words | `FPFPP` | 0.60 | [0.20, 1.00] | 40% | UNSTABLE |
| b16 | "Do we only use 10% of our brains?" in a jokey tone | `PPFFP` | 0.60 | [0.20, 1.00] | 40% | UNSTABLE |
| b05 | A borderline-polite refusal to a coworker | `PFPPP` | 0.80 | [0.40, 1.00] | 20% | BORDERLINE |
| b19 | A technically accurate haiku about recursion | `PPFPP` | 0.80 | [0.40, 1.00] | 20% | BORDERLINE |
| 15 others | | `PPPPP` | 1.00 | [1.00, 1.00] | 0% | STABLE |

Treat the k-th repeat of every case as one ordinary single-run eval, and the five
"single runs" of this identical eval scored **0.90, 0.95, 0.85, 0.90 and 1.00**. A single
run can't tell you which of those numbers you got.

The report also flagged `TEMPERATURE NOT SET` for both the target and the judge, which is
the reason these borderline verdicts can come out differently from run to run.

To re-record with your own key, copy `.env.example` to `.env`, add a free
[Google AI Studio](https://aistudio.google.com/apikey) key, and run the quickstart with
`EVALSEAL_RECORD=1`. If the free tier rate-limits you, run the same command again later;
responses already recorded are kept.

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
