# EvalSeal

**Reproducibility receipts for LLM evals: run it N times, report the score with its noise, and seal what actually ran.**

## The problem

An eval score from a single run is one sample of a random process. Run the same eval again
and borderline items quietly flip from PASS to FAIL, especially when an LLM judge grades
them. Reports also name the model you *asked* for, not the one that *answered*. EvalSeal
measures the flips, records the real provenance, and seals both into a tamper-evident ledger.

## Quickstart

```bash
pip install evalseal

# The recorded demo (examples + cassette) lives in the repo.
git clone https://github.com/patibandlavenkatamanideep/evalseal && cd evalseal

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

## A benchmark, objectively graded

The suite above is graded by an LLM judge. This one is graded by arithmetic: 40 problems
sampled (seed 20260918) from the [GSM8K](https://github.com/openai/grade-school-math) test
split, each with one verified numeric answer, same model, same N=5, `answer_match` scoring.

```bash
evalseal run --suite examples/gsm8k/suite.json
```

**Accuracy 0.975, and every case STABLE — 40 stable, 0 borderline, 0 unstable.** All five
runs scored exactly 0.975. No flips at all, even with temperature left at the provider
default.

Put the two runs side by side:

| suite | grading | cases | flipped |
|---|---|---|---|
| borderline_judge | LLM judge | 20 | **5 (25%)** |
| gsm8k | numeric answer match | 40 | **0** |

That contrast is the finding. On this model and these tasks, the irreproducibility came
from the *judge*, not the model. Which is exactly the kind of claim a single run cannot
make, and the reason to measure rather than assume.

One problem (`gsm016`) was wrong in all five runs — consistently, not randomly. Reading it
shows why: it says ten stalls, then refers to "the twenty stalls". GSM8K's reference answer
assumes ten; the model assumed twenty and answered 176 every time. A stable failure is a
different thing from a flaky one, and worth a different response: here, fix the question.

## An eval built from a real codebase

`examples/codeqa/` is generated, not hand-written: `build_dataset.py` walks a directory of
checked-out repositories, parses each Python file, and asks questions whose answers the
parser already knows — a parameter's default, how many parameters a function takes, the
exception it raises, its declared return type. Ground truth comes from the AST, so the
suite is objectively gradable by `answer_match` with no judge in the loop.

```bash
python examples/codeqa/build_dataset.py ~/src/my-repos 60   # point it at your own code
evalseal run --suite examples/codeqa/suite.json
```

The committed run covers **60 questions drawn from a pool of 3,279 across 7 repositories**,
answered by `gemini-2.5-flash` at N=5:

| question type | accuracy | inconsistent |
|---|---|---|
| default value | 1.00 | 0 |
| parameter count | 1.00 | 0 |
| exception raised | 1.00 | 0 |
| declared return type | 0.97 | **2** |

**Accuracy 0.993 — 58 stable, 2 borderline, 0 unstable.** Both remaining flips are questions
whose answer is a class defined in that codebase (`RetrievalResult`, `EvidenceResult`).
Primitives and counts never wavered; project-specific names did, in every recording.

### What the flips caught was the eval, twice

Building this suite produced two failures that had nothing to do with the model:

- **A case failing all five runs** asked which exception `_call_with_retry` raises, where
  the source says `raise last_exc` — a variable holding an exception, not a type. The
  question was unanswerable; the generator now requires a class-shaped name.
- **A case flipping 40%** asked which exception `_parse` raises. It explicitly raises
  `ValueError`, but also calls `json.loads` and `model_validate`, which propagate others.
  The model alternated between those two readings — both defensible. Rewording the question
  to say "raised explicitly in its own body" took that case to `PPPPP`.

The pattern is worth keeping: **a stable failure usually means the eval is wrong, while a
flip means the model is genuinely unsure.** One run shows you neither.

## Did the score actually change?

The same suite, same N, against a second model — `gemini-3.1-flash-lite` — then asking
evalseal whether the difference is real:

```bash
evalseal run --suite examples/gsm8k/suite.json      --ledger .evalseal/compare.jsonl
evalseal run --suite examples/gsm8k/suite-lite.json --ledger .evalseal/compare.jsonl
evalseal diff --ledger .evalseal/compare.jsonl 0 1
```

```
mean 0.975 -> 0.975  (delta +0.000, noise floor ±0.000) => within noise
```

Both models scored 0.975, both perfectly stable, and both missed the *same* problem — the
broken one. On this suite the two models are indistinguishable, which says less about the
models than about the suite: 40 problems this easy cannot separate them. That is a useful
thing to learn before quoting a benchmark number as evidence one model beats another.

**Read that noise floor carefully.** It is ±0.000 because nothing flipped, and a floor of
zero means `diff` would call a one-case difference (0.025) a REAL CHANGE. Zero flips in 5
runs is not proof of zero variance: by the rule of three, the true flip rate could still be
as high as ~45%. Raise N before trusting a floor this tight.

## Commands

| command | what it does | exit code |
|---|---|---|
| `evalseal run` | Runs each case N times, analyzes variance, seals a record, writes `report.json` + `report.md`. | `0` all stable/borderline · `3` any case UNSTABLE · `1` error |
| `evalseal verify` | Recomputes every hash in `.evalseal/ledger.jsonl` and checks the chain links; `--public-key` or `--signed` also checks signatures. | `0` intact · `1` tampered or broken |
| `evalseal diff A B` | Compares two ledger runs and says whether the mean moved beyond the noise floor. Use `--` for negative indices: `evalseal diff -- 0 -1`. | `0` |
| `evalseal keygen` | Writes an Ed25519 keypair for signing. | `0` |
| `evalseal sign` | Signs the ledger head with your private key. | `0` · `1` if the ledger doesn't verify |

`run` takes `--concurrency` (default 4 requests in flight), `--max-retries` (default 5, on
HTTP 429/408/5xx and connection errors, honouring `Retry-After`), `--timeout`, and
`--quiet`. Concurrency never changes the result: each response is recorded against its own
(case, repeat) slot, so a 16-worker replay is identical to a serial one.

### One suite file instead of many flags

```bash
evalseal run --suite examples/gsm8k/suite.json          # flags still override it
```

```json
{
  "dataset": "dataset.jsonl", "target": "target.json", "scorer": "scorer.json",
  "n_repeats": 5, "concurrency": 4, "fail_on": "unstable",
  "cassette": "../../tests/cassettes/gsm8k.json"
}
```

Paths resolve relative to the suite file, and an unknown key is an error rather than a
silently ignored typo.

### Signing a receipt

The hash chain proves a ledger hasn't been edited. It doesn't prove who made it: anyone
who can rewrite the whole file can rebuild a consistent chain. Signing closes that gap for
anyone holding your public key.

```bash
evalseal keygen                                  # evalseal.key (0600) + evalseal.pub
evalseal run --suite my-suite.json --sign-key evalseal.key
evalseal verify --public-key evalseal.pub        # chain + signatures
```

A signature says *this key signed a ledger whose head was H*, and the head commits to every
record before it. It does not say the run happened as described: a signer can sign whatever
they like. What it rules out is someone else altering your receipt afterwards.

### Gating a pipeline

`--fail-on` decides which stability classes fail the run, and `--junit-xml` writes a report
CI can display next to ordinary tests:

```yaml
- name: Eval reproducibility
  run: |
    evalseal run --dataset evals/dataset.jsonl \
      --target-config evals/target.json --scorer-config evals/scorer.json \
      --n 5 --fail-on borderline --junit-xml junit.xml --quiet
```

| exit code | meaning |
|---|---|
| `0` | every case satisfied `--fail-on` |
| `3` | at least one case violated it |
| `1` | the run itself failed (missing cassette entry, provider error) |
| `2` | bad arguments |
| `130` | interrupted; recorded responses are kept, re-run to resume |

Start with `--fail-on none` to observe flip rates without blocking merges, then tighten.

**Stability classes** are based on the flip rate, the share of a case's N verdicts that
disagree with its majority: `STABLE` (0), `BORDERLINE` (≤ 20%), `UNSTABLE` (> 20%).

**Scorers**: `exact`, `regex`, `answer_match` (extracts the final answer and compares it to
`expected`, numerically when both are numbers), and `llm_judge` (a second model, itself a
tracked target).

**Provenance warnings** show up in the report when:
- the served model differs from the requested one (for the target or the judge),
- the endpoint isn't a canonical provider host,
- temperature was left at the provider default,
- the served model or system fingerprint changed partway through the run.

## Using it as a library

```python
from evalseal import Dataset, LocalCallableTarget, RegexScorer, run_eval

record = run_eval(
    Dataset.from_jsonl("dataset.jsonl"),
    LocalCallableTarget(my_model_fn),
    RegexScorer(r"^yes"),
    n_repeats=5,
)
print(record.aggregate.mean_score, [r.stability for r in record.results])
```

The package ships type information (PEP 561), so mypy and pyright see the annotations.

## How it works

The executor sends each prompt to the target N times and scores every response. An LLM
judge is itself a target, so its own randomness is measured instead of assumed away.
`analyze.py` computes the mean, a seeded bootstrap 95% CI, and the flip rate for each case.
Every request goes through a cassette, keyed by the request plus which repeat it belongs
to. In record mode real responses are saved as they arrive; in replay mode, the default and
what CI uses, they are served back by that key, and a missing entry fails loudly. Each run is saved as a `RunRecord`: its manifest (requested vs.
served model, fingerprint, parameters and whether they were set explicitly, rubric hash,
dataset hash) plus its results. The record is hashed and linked to the previous record's
hash in an append-only JSONL ledger, so editing any past score breaks `verify`.

Security policy:
[SECURITY.md](https://github.com/patibandlavenkatamanideep/evalseal/blob/main/SECURITY.md).
Release notes:
[CHANGELOG.md](https://github.com/patibandlavenkatamanideep/evalseal/blob/main/CHANGELOG.md).
Contributing: see
[CONTRIBUTING.md](https://github.com/patibandlavenkatamanideep/evalseal/blob/main/CONTRIBUTING.md).
See [DESIGN.md](https://github.com/patibandlavenkatamanideep/evalseal/blob/main/DESIGN.md)
for what this does and does not prove.

**Upgrading to 0.2:** cassettes and sealed records written by 0.1.x cannot be read by 0.2,
which keys entries by repeat. The demo cassette in this repo was converted in place, so the
published numbers above are unchanged; your own cassettes need re-recording, and an
existing ledger needs to start fresh. `verify` names an old record rather than calling it
tampered.
