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

### Two minutes, no API key

```console
$ evalseal run --suite examples/borderline_judge/suite.json --show-cases --unstable-only
Aggregate: 20 cases · mean 0.92 · 15 stable / 2 borderline / 3 unstable

 case_id  runs  pass  fail  flips  flip_rate  majority  stability  judge_prompt_hash
 b01      5     3     2     2      40%        PASS      unstable   sha256:457d7c3
 b10      5     3     2     2      40%        PASS      unstable   sha256:457d7c3
 b16      5     3     2     2      40%        PASS      unstable   sha256:457d7c3
 b05      5     4     1     1      20%        PASS      unstable   sha256:457d7c3
 b19      5     4     1     1      20%        PASS      unstable   sha256:457d7c3

3 case(s) fail --fail-on unstable: b01, b10, b16

$ evalseal verify
Chain intact: 1 record(s).

$ evalseal gate --min-score 0.85 --max-flip-rate 0.10
FAIL 5 case(s) exceed --max-flip-rate 0.10 (worst: b01 at 40%)
```

Exit codes: `0` passed, `3` a gate or stability policy failed, `1` the tool failed.
A ready-to-copy workflow is in [docs/github-action-example.yml](docs/github-action-example.yml).

## Real flip rates

Recorded 2026-09-15 and committed in `tests/cassettes/run.json`: `gemini-2.5-flash` as both
the target and the judge, temperature left at the provider default, 20 arguable prompts,
5 runs each. The quickstart above replays exactly this run.

**Mean score: 0.92, but 5 of 20 cases did not get the same verdict every time.**

| case | prompt | verdicts | mean | 95% CI | flip rate | stability |
|---|---|---|---|---|---|---|
| b01 | Is a hot dog a sandwich? | `FPPFP` | 0.60 | [0.23, 0.88] | 40% | UNSTABLE |
| b10 | Blockchain for a child in exactly 20 words | `FPFPP` | 0.60 | [0.23, 0.88] | 40% | UNSTABLE |
| b16 | "Do we only use 10% of our brains?" in a jokey tone | `PPFFP` | 0.60 | [0.23, 0.88] | 40% | UNSTABLE |
| b05 | A borderline-polite refusal to a coworker | `PFPPP` | 0.80 | [0.38, 0.96] | 20% | BORDERLINE |
| b19 | A technically accurate haiku about recursion | `PPFPP` | 0.80 | [0.38, 0.96] | 20% | BORDERLINE |
| 15 others | | `PPPPP` | 1.00 | [0.57, 1.00] | 0% | STABLE |

A perfect 5-for-5 case reports `[0.57, 1.00]`, not `[1.00, 1.00]`: five identical runs do
not establish certainty, and the interval says so.

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

**Accuracy 0.975, and every case STABLE - 40 stable, 0 borderline, 0 unstable.** All five
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

One problem (`gsm016`) was wrong in all five runs - consistently, not randomly. Reading it
shows why: it says ten stalls, then refers to "the twenty stalls". GSM8K's reference answer
assumes ten; the model assumed twenty and answered 176 every time. A stable failure is a
different thing from a flaky one, and worth a different response: here, fix the question.

## An eval built from a real codebase

`examples/codeqa/` is generated, not hand-written: `build_dataset.py` walks a directory of
checked-out repositories, parses each Python file, and asks questions whose answers the
parser already knows - a parameter's default, how many parameters a function takes, the
exception it raises, its declared return type. Ground truth comes from the AST, so the
suite is objectively gradable by `answer_match` with no judge in the loop.

```bash
python examples/codeqa/build_dataset.py ~/src/my-repos 60   # point it at your own code
evalseal run --suite examples/codeqa/suite.json
```

The committed run covers **60 questions drawn from a pool of 3,279 across 7 repositories**,
answered by `gemini-2.5-flash` at N=5: **accuracy 1.000, all 60 cases STABLE**, with every
question type at 1.00.

That clean number took three corrections, and every one of them was a defect in the eval
rather than in the model.

### Three times the variance caught the eval, not the model

- **A case failing all five runs** asked which exception `_call_with_retry` raises, where
  the source says `raise last_exc` - a variable holding an exception, not a type. The
  question was unanswerable; the generator now requires a class-shaped name.
- **A case flipping 40%** asked which exception `_parse` raises. It explicitly raises
  `ValueError`, but also calls `json.loads` and `model_validate`, which propagate others.
  The model alternated between those two readings - both defensible. Rewording the question
  to "raised explicitly in its own body" took that case to `PPPPP`.
- **Two cases flipping 20%** looked like genuine model uncertainty about class names
  defined in the codebase. They were not. Re-running just those two at N=20 (40 API calls)
  showed the model answered correctly every time; it simply wrote `` `RetrievalResult` ``
  with backticks in some runs, and the scorer counted markdown decoration as a wrong
  answer. `answer_match` now normalizes surrounding backticks and quotes before comparing.
  Both cases are 20-for-20 STABLE.

```bash
evalseal run --suite examples/codeqa/suite.json --only cq043,cq052 --n 20
```

`--only` re-examines named cases; a flip at N=5 is worth a closer look, because five runs
cannot tell 20% from 30% - or, as here, from 0%.

The rule that survives all three: **a stable failure means your eval item is broken, and a
flip means either the model is unsure or your question and scorer are.** Look before
concluding. Normalizing decoration did not paper over real variance - the judge-graded
suite above still flips on 5 of 20 cases, because that disagreement is real.

## Did the score actually change?

The same suite, same N, against a second model - `gemini-3.1-flash-lite` - then asking
evalseal whether the difference is real:

```bash
evalseal run --suite examples/gsm8k/suite.json      --ledger .evalseal/compare.jsonl
evalseal run --suite examples/gsm8k/suite-lite.json --ledger .evalseal/compare.jsonl
evalseal diff --ledger .evalseal/compare.jsonl -- 0 1
```

```
Comparable: yes. Same grading setup, so the scores mean the same thing.

 metric              before  after  change
 ───────────────────────────────────────────────────────────────────────────────
 score               0.975   0.975  no change (within noise, noise floor ±0.217)
 mean flip rate      0.0%    0.0%   +0.0%
 cases that flipped  0       0      +0

Other differences: served model gemini-2.5-flash to gemini-3.1-flash-lite, sealed
hash sha256:44c37b257f73e to sha256:cf13839f41540
```

Both models scored 0.975, both perfectly stable, and both missed the *same* problem - the
broken one. On this suite the two models are indistinguishable, which says less about the
models than about the suite: 40 problems this easy cannot separate them. That is a useful
thing to learn before quoting a benchmark number as evidence one model beats another.

**That noise floor is why N matters.** At N=5 a perfectly stable case still carries a
±0.217 interval, so a one-case difference (0.025) is correctly reported as noise rather
than a finding. Raising N narrows the floor and buys the power to call smaller differences
real: ±0.217 at N=5 becomes roughly ±0.08 at N=20.

### Comparability comes before the delta

The first line is the one that matters. A score delta means nothing until you know the two
runs measured the same thing, so `diff` answers that first and the number second.

Comparability is decided by the **evaluator fingerprint**: scorer type, rubric, judge
prompt, judge model and parameters, and the dataset. It deliberately excludes the *target*
model, because comparing two target models is the entire point of a benchmark. Change the
rubric and the score moves while the model sits still - that is the comparison EvalSeal
refuses to let you make silently.

Any two sealed records work, whether they are receipt files or ledgers:

```bash
evalseal diff report-before.json report-after.json     # two receipts
evalseal diff --ledger .evalseal/runs.jsonl 0 -1       # first vs latest
evalseal diff runs-a.jsonl runs-b.jsonl                # heads of two ledgers
evalseal diff before.json after.json --json            # for CI
```

```
Not directly comparable: evaluator configuration changed.

 what changed  before                            after
 ──────────────────────────────────────────────────────────────────────────────────
 dataset       sha256:b2639589bcf0b3161b5fc144…  sha256:95c3248301dd733c4e3c24cf07…

 metric              before  after  change
 ──────────────────────────────────────────────────────────────────────────────
 score               0.987   0.993  +0.007 (not comparable, noise floor ±0.400)
 mean flip rate      1.3%    0.7%   -0.7%
 cases that flipped  3       2      -1

 • Now stable: cq015
 • Still unstable: cq043, cq052
```

The score went up by 0.007 and EvalSeal declines to call that an improvement, because the
dataset underneath it changed. What it will tell you is which *cases* moved: `cq015`
stopped flipping, `cq043` and `cq052` still flip. That case-level drift is the part worth
acting on - it points at a specific question to go read, which is where eval defects
actually live.

`--json` emits the same content as a machine-readable object (`comparable`, `score`,
`flip_rate`, `unstable_cases`, `cases`, `config_changes`, `provenance`) for a pipeline to
assert on. `diff` always exits 0: it reports, it does not gate. Gating is `gate`'s job.

## A receipt you can attach to a pull request

Terminal output is for the person who ran the eval. `--html` is for everyone else.

```bash
evalseal run --suite examples/gsm8k/suite.json --html receipt.html
evalseal report receipt.json --html receipt.html      # from a receipt you already have
evalseal diff 0 -1 --html drift.html                  # the drift report, same styling
```

Both pages are a single file with no stylesheet, script, font or image to fetch, so a
report archived as a CI artifact renders the same way a year later on a machine with no
network. Nothing in them reads the clock: the same record produces byte-identical HTML,
which means the receipt can itself be hashed and attached to the chain. The only
timestamp shown is the one sealed into the record.

The receipt leads with four numbers - mean score, noise floor, mean flip rate, case
counts - then gives every case a verdict strip: one cell per repeat, in run order, red
where the verdict disagreed with the majority. `FPPFP` tells you a case flipped; the strip
tells you *when*, which is what you need to know before blaming the model.

Prompts and responses are never written into either page, only their hashes. A receipt is
meant to be forwarded, and a file that quietly carries your dataset is a leak waiting to
happen. Verify what the page claims with `evalseal verify`.

## Commands

| command | what it does | exit code |
|---|---|---|
| `evalseal run` | Runs each case N times, analyzes variance, seals a record, writes `report.json` + `report.md`; `--html` also writes a receipt. | `0` all stable/borderline · `3` any case UNSTABLE · `1` error |
| `evalseal verify` | Recomputes every hash in `.evalseal/ledger.jsonl` and checks the chain links; `--public-key` or `--signed` also checks signatures. | `0` intact · `1` tampered or broken |
| `evalseal diff A B` | Compares two sealed runs - receipt files or ledger indices - and reports comparability, score change against the noise floor, and which cases started or stopped flipping. `--json` for CI, `--html` to share. | `0` |
| `evalseal keygen` | Writes an Ed25519 keypair for signing. | `0` |
| `evalseal sign` | Signs the ledger head with your private key. | `0` · `1` if the ledger doesn't verify |
| `evalseal report` | Prints the per-case verdict distribution for a sealed record; takes a receipt path or ledger index, `--html` writes a shareable page. | `0` |
| `evalseal gate` | Applies CI thresholds to a sealed record. | `0` passed · `3` gate failed |

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

### Gating a pipeline with `gate`

`gate` applies CI thresholds to a sealed record and exits 3 when one is violated:

```bash
evalseal verify --ledger .evalseal/ledger.jsonl      # chain, siblings, schema
evalseal report --unstable-only                      # which cases flipped
evalseal gate --min-score 0.85 --max-flip-rate 0.10  # thresholds
evalseal gate --critical b01,b02                     # these must never flip
evalseal gate --expect-config sha256:407033d3836b8   # evaluator must not have changed
```

`--expect-config` compares the evaluator fingerprint - target model and parameters, scorer
type, rubric hash, judge prompt hash, judge model and parameters, dataset and suite hashes.
A change there moves the score without the model changing, so a gate that ignores it will
eventually mistake evaluator drift for model drift.

### Gating a run directly

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

### Per-case instability

An aggregate flip rate hides which cases are unstable. `--show-cases` prints the
distribution, worst first, stamped with the judge prompt that produced it:

```
| case_id | runs | pass | fail | flips | flip_rate | majority | stability | judge_prompt_hash |
| b01     | 5    | 3    | 2    | 2     | 40%       | PASS     | unstable  | sha256:457d7c3    |
| b05     | 5    | 4    | 1    | 1     | 20%       | PASS     | unstable  | sha256:457d7c3    |
```

`evalseal report --json` emits the same data machine-readably, with
`summary.flip_rate`, `cases[].verdict_distribution`, `cases[].verdict_sequence` and a
`provenance` block including the config fingerprint.

**Flip rate is `non-majority verdicts / total runs`** - not transitions between runs. So
`PPFP` is 1/4 = 25%, not 2 transitions out of 3. The definition was chosen because it does
not depend on the order runs happened to execute in, which matters once runs are concurrent.

**Stability classes** are based on the flip rate, the share of a case's N verdicts that
disagree with its majority: `STABLE` (0), `BORDERLINE` (≤ 20%), `UNSTABLE` (> 20%). Each
case also carries a finer `stability_label`: `stable_pass`, `stable_fail`, `unstable`, or
`insufficient_runs` when N < 5, because a flip rate from three runs is not worth reading.

**Confidence intervals** use the Wilson score interval for binary verdicts and a seeded
percentile bootstrap for float scores. Both are deterministic. Wilson is used because the
bootstrap collapses to zero width when every run agrees, which would report certainty that
five runs cannot support.

**Scorers**: `exact`, `regex`, `answer_match` (extracts the final answer and compares it to
`expected`, numerically when both are numbers), and `llm_judge` (a second model, itself a
tracked target).

**Provenance warnings** show up in the report when:
- the served model differs from the requested one (for the target or the judge),
- the endpoint isn't a canonical provider host,
- temperature was left at the provider default,
- the served model or system fingerprint changed partway through the run.

## What EvalSeal guarantees

- **Hash-chain integrity.** Every record commits to the previous one; editing a past score
  breaks that record's hash, and re-hashing it breaks the next record's link.
- **Linear appends under concurrent writers.** The head is read and the record appended
  inside one file lock, so two simultaneous runs cannot create sibling records. `verify`
  reports siblings explicitly if a ledger acquires them another way.
- **Repeat-run instability measurement.** Per-case verdict sequences, counts, flip rates
  and Wilson intervals, rather than one aggregate number.
- **Evaluator config fingerprinting.** The receipt seals the judge prompt hash, rubric
  hash, scorer type, judge model and parameters, dataset and suite hashes, EvalSeal
  version, git commit and dirty flag, Python version and platform.

## What EvalSeal does not guarantee

- **It cannot prove the provider behaved the same way later.** A replay reproduces recorded
  responses; it says nothing about what the model would return today.
- **It does not make a nondeterministic judge deterministic.** It measures the
  nondeterminism instead.
- **It does not replace human review of borderline cases.** A 40% flip rate tells you where
  to look, not what the right answer is.
- **Local file locking is not distributed consensus.** `fcntl`/`msvcrt` coordinate
  processes on one machine. On NFS or SMB the guarantee weakens or disappears.
- **Hashes detect change, not incorrectness.** A sealed record with a wrong rubric is
  sealed just as firmly as one with a right rubric.

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

Comparing two sealed runs, with the same rules the CLI applies:

```python
from evalseal import diff_records, load_receipt

result = diff_records(load_receipt("before.json"), load_receipt("after.json"))
if result.comparable and result.score_verdict == "REAL CHANGE":
    print(result.score_delta, result.newly_unstable)
```

`load_receipt` reads either shape: a `report.json` receipt, or a ledger, whose head it
takes. `DiffResult.to_dict()` is what `--json` prints.

`to_html(record)` and `diff_to_html(result)` return those pages as strings, with
`write_html` / `write_diff_html` to put them on disk. `mean_flip_rate(record)` and
`noise_floor(record)` are the two summary statistics every surface shares, so a dashboard
built on the library prints the same numbers the CLI does.

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
