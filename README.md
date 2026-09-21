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
Aggregate: 20 cases · mean 0.92 · 15 stable / 0 borderline / 5 unstable

 case_id  runs  pass  fail  flips  flip_rate  majority  stability  judge_prompt_hash
 b01      5     3     2     2      40%        PASS      unstable   sha256:457d7c3
 b10      5     3     2     2      40%        PASS      unstable   sha256:457d7c3
 b16      5     3     2     2      40%        PASS      unstable   sha256:457d7c3
 b05      5     4     1     1      20%        PASS      unstable   sha256:457d7c3
 b19      5     4     1     1      20%        PASS      unstable   sha256:457d7c3

5 case(s) fail --fail-on unstable: b01, b05, b10, b16, b19

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
| b05 | A borderline-polite refusal to a coworker | `PFPPP` | 0.80 | [0.38, 0.96] | 20% | UNSTABLE |
| b19 | A technically accurate haiku about recursion | `PPFPP` | 0.80 | [0.38, 0.96] | 20% | UNSTABLE |
| 15 others | | `PPPPP` | 1.00 | [0.57, 1.00] | 0% | STABLE |

A perfect 5-for-5 case reports `[0.57, 1.00]`, not `[1.00, 1.00]`: five identical runs do
not establish certainty, and the interval says so.

Every case that disagreed with itself is UNSTABLE, including the two that dissented only
once. Stability is decided by where the 95% Wilson interval of the pass proportion sits:
4/5 gives [0.38, 0.96], which straddles 0.5, so five runs do not establish which way that
case goes. **BORDERLINE cannot occur at N=5 at all** - only 5/5 and 0/5 clear 0.5. Before
2.0 a single dissent in five was called BORDERLINE because the flip rate was 0.20 and the
cutoff was 0.20, which reads as a 20% tolerance band. At five runs the only reachable flip
rates are 0, 0.2 and 0.4, so that cutoff was never a band; it was the single outcome "one
of five runs disagreed" wearing a percentage.

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

**This contrast cannot tell you where the variance came from, and until 2.0 the README
claimed it could.** It said the irreproducibility came from the judge rather than the
model. The two suites differ in the grader *and* in the task - arguable open-ended prompts
against GSM8K arithmetic - so the comparison is confounded. A difference in flip rate is
equally consistent with an unreliable judge, with open-ended prompts drawing more variable
answers out of the model, or with both. The design cannot separate them, so the claim was
not supported by the evidence offered for it.

`evalseal decompose` runs the experiment that can, on one suite with the task held fixed.

## Where the variance actually comes from

Two arms over `borderline_judge`, same 20 cases, N=5 each. The first samples the target
**once** and then judges that single frozen response five times, so nothing but the judge
can vary. The second samples the target five times and judges each once, which is an
ordinary run and contains both sources.

```bash
evalseal decompose --dataset examples/borderline_judge/dataset.jsonl \
  --target-config examples/borderline_judge/target.json \
  --scorer-config examples/borderline_judge/scorer.json \
  --cassette tests/cassettes/decompose_borderline.json --n 5
```

Recorded live on 2026-09-21 against `gemini-2.5-flash` and committed, so the replay above
needs no key:

| arm | what varies | mean flip rate | cases that flipped |
|---|---|---|---|
| judge_only | the judge only, on one frozen response | **6.0%** | 3 of 20 |
| target_and_judge | the target and the judge | **5.0%** | 3 of 20 |

| case | judge_only | target_and_judge |
|---|---|---|
| b16 | 40% (`PFPFF`) | 40% (`PFFPP`) |
| b19 | 40% (`PFPFP`) | 40% (`PFFFP`) |
| b01 | 40% (`PFPFP`) | 0% (`PPPPP`) |
| b10 | 0% (`PPPPP`) | 20% (`FFFPF`) |

**The judge, grading one unchanging string five times, disagrees with itself about as
often as the whole pipeline does.** That is the claim the old two-suite table was reaching
for, and this time the task is held fixed while it is made.

What this does *not* show, stated plainly because the previous version of this section
over-read its evidence:

- **The arms are not ordered.** judge_only came out higher than target_and_judge (6.0% vs
  5.0%). They are two independent five-run samples of 20 cases, so that gap is noise, not
  a finding that freezing the target increases variance. The same goes for b01 flipping in
  one arm and b10 in the other.
- **judge_only is a lower bound, not a share.** It measures the judge's variance at one
  particular response. Another draw from the target might be easier or harder to grade
  consistently, so this does not average over the target's output distribution.
- **Three flipping cases is below the floor for a significant paired test.** At alpha 0.05
  six items must move in the same direction, so nothing here is significant - it is a
  description of one recorded experiment, not an inference about judges generally.

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
 ───────────────────────────────────────────────────────────
 score               0.975   0.975  no change (inconclusive)
 mean flip rate      0.0%    0.0%   +0.0%
 cases that flipped  0       0      +0

Paired test over 40 shared item(s).

 quantity                               value
 ──────────────────────────────────────────────────────────────────
 difference in mean score               +0.0000
 95% CI (cluster bootstrap over items)  [+0.0000, +0.0000]
 p-value (exact mcnemar)                1
 discordant items                       0 (0 regressed, 0 improved)
 verdict                                inconclusive

Inconclusive is not the same as no difference. The test did not reach
significance, which is a statement about this experiment's power, not about
the two runs being the same.

 • Widest per-case CI half-width (per_case_halfwidth): ±0.217. This describes
   one item at this N, not the suite mean; before 2.0 it was used as the
   threshold for a change.
```

Both models scored 0.975, both perfectly stable, and both missed the *same* problem - the
broken one. On this suite the two models are indistinguishable, which says less about the
models than about the suite: 40 problems this easy cannot separate them. That is a useful
thing to learn before quoting a benchmark number as evidence one model beats another.

**The comparison is paired, because both runs cover the same items.** Every item is
measured twice, so the question is which items changed verdict, not how two independent
samples compare. Binary scorers get an exact McNemar test on the discordant items; the
interval comes from a cluster bootstrap that resamples whole items and carries all of an
item's repeats with them.

Here nothing was discordant: the two models produced identical per-item results, so there
is no evidence of a difference and none of sameness either. That is what `inconclusive`
means, and it is why the word "noise" is gone.

**Before 2.0 this line was wrong in a way worth naming.** `diff` compared the suite-level
mean difference against the widest *per-case* Wilson half-width - 0.217 at N=5 - and
called anything smaller "within noise". Those are quantities about different things: a
per-item interval says how little five repeats pin down one item, while a mean over 40
items is far better determined than any item in it. The effect was to report almost every
real shift as noise. Eight of forty items regressing is a difference of 0.20 with
p = 0.0078, and the old rule called it noise. The number itself still has a meaning and is
still reported, under the name `per_case_halfwidth`.

The old text here also advised raising N to narrow that floor. **That advice was wrong**,
and `evalseal power` is in the tool partly to keep it from being given again - see below.

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

Pointing `diff` at the judge suite and the arithmetic suite, both replayed from committed
cassettes, gets the refusal it should:

```
Not directly comparable: evaluator configuration changed.

 what changed  before                          after
 ──────────────────────────────────────────────────────────────────────────────
 scorer type   llm_judge                       answer_match
 judge model   gemini-2.5-flash                None
 judge prompt  sha256:457d7c3841ce0f2d1e2b03…  None
 rubric        sha256:8d95f8c65711cb262abcaf…  None
 dataset       sha256:1d94ddad418a97f1e39e86…  sha256:c9623577a124d8688bd81ba6…
 suite         sha256:271f373da83684958c57ef…  sha256:4af862fcb84ed2e65e635ef6…

 metric              before  after  change
 ──────────────────────────────────────────────────────────
 score               0.920   0.975  +0.055 (not comparable)
 mean flip rate      8.0%    0.0%   -8.0%
 cases that flipped  5       0      -5

 • Cases added: gsm001, gsm002, …            (40 ids, elided here)
 • Cases removed: b01, b02, …                (20 ids, elided here)
```

0.920 to 0.975 looks like an improvement and is not one: the grader changed from an LLM
judge to arithmetic and the dataset changed with it. The two runs also share no items at
all, so there is nothing to pair and no test to run. `diff` reports the delta because
hiding it would be its own kind of lie, and marks it `not comparable` so it cannot be
quoted as a result.

Cases that leave the dataset are reported as removed, never as "now stable". Dropping a
flaky case is not the same as fixing it, and a diff that conflates them rewards deleting
the hard items.

`--json` emits the same content as a machine-readable object (`comparable`, `score`,
`flip_rate`, `unstable_cases`, `cases`, `config_changes`, `provenance`) for a pipeline to
assert on. `diff` always exits 0: it reports, it does not gate. Gating is `gate`'s job.

## "Inconclusive" - so how big would the experiment need to be?

`diff` returns `inconclusive` often, and honestly. `evalseal power` answers the question
that follows:

```console
$ evalseal power --from-receipt 0 --ledger .evalseal/compare.jsonl --mdd 0.05
Power 0.0% to detect a 0.050 drop from 0.975, with 40 item(s) at N=5 (model:
from_receipt, 2000 simulated experiments, alpha 0.05).

A paired test needs at least 6 items to change verdict in the same direction
before any result can be significant at alpha 0.05: 2 x 0.5^6 = 0.0312. A suite
where fewer than 6 items can move cannot produce a significant result at any
number of repeats.

For 80% power you would need about 110 items at N=5.
Adding repeats does not help here. Repeats sharpen each item toward its own
majority verdict; when a shift does not move items across that boundary, more
repeats remove the disagreement the test feeds on. More items is the dial that
works.
```

**The six-item floor is the most useful number here.** Under an exact paired test, n
items all moving the same way gives p = 2 x 0.5^n. That is 0.0625 at five items and
0.03125 at six, so six is the smallest count that can clear alpha 0.05 - and a 40-item
suite scoring 0.975 has only one failing item to begin with. No amount of repeating will
make such a suite able to separate two models.

**Raising N is usually the wrong dial, and the README used to advise it.** Repeats sharpen
each item toward its own majority verdict. That helps only when a change pushes items
across the 50% line; when it lowers every item's pass rate from 0.90 to 0.85, extra
repeats *remove* the disagreement the test feeds on and power falls. The simulator shows
both regimes, and the tests assert both.

The item model is an explicit choice rather than a hidden default, because it drives the
answer more than the arithmetic does: `deterministic` (items pass or fail every time - what
gsm8k and codeqa actually look like, and where repeats do nothing), `bernoulli` (every
repeat an independent coin flip - the optimistic end), or `--from-receipt` to use a real
run's observed per-item rates. Method and assumptions are in `src/evalseal/power.py`.

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

The receipt leads with four numbers - mean score, per-case halfwidth, mean flip rate, case
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
| `evalseal diff A B` | Compares two sealed runs - receipt files or ledger indices - item by item: comparability, a paired test on the score difference, and which cases started or stopped flipping. `--json` for CI, `--html` to share. | `0` |
| `evalseal power` | Estimates how many items or repeats it would take to detect a difference you care about. `--from-receipt` uses a real run's per-item rates. | `0` |
| `evalseal decompose` | For `llm_judge` suites: splits flips into the judge's share and the target's, by judging one fixed response N times versus sampling the target N times. | `0` |
| `evalseal keygen` | Writes an Ed25519 keypair for signing. | `0` |
| `evalseal sign` | Signs the ledger head with your private key. | `0` · `1` if the ledger doesn't verify |
| `evalseal report` | Prints the per-case verdict distribution for a sealed record; takes a receipt path or ledger index, `--html` writes a shareable page. | `0` |
| `evalseal gate` | Applies CI thresholds to a sealed record, from flags or a `--policy` file (thresholds, critical cases, drift rules against a baseline). `--json` for CI. | `0` passed · `3` gate failed · `2` broken policy |

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

### A policy file instead of a wall of flags

A gate spelled out in CI flags is a gate nobody reads. `--policy` moves the thresholds into
a file that lives in the repo, so loosening one shows up in review and `git log` says who
did it:

```yaml
# evalseal.yml
version: 1

run:
  min_score: 0.95
  max_flip_rate: 0.20        # per case, not the average
  max_unstable_cases: 1
  verify_ledger: true        # the hash chain has to check out (default)

cases:
  critical: [gsm016]         # these must not flip at all
  min_score:
    gsm001: 1.0

drift:
  baseline: baselines/main.json   # relative to this file
  require_comparable: true        # refuse to compare across a changed rubric or dataset
  max_score_regression: 0.02      # and the paired test must support a regression
  allow_new_unstable: false       # a case that starts flipping fails the build
  allow_removed_cases: false      # quietly dropping a hard case is not an improvement
```

```console
$ evalseal gate --policy evalseal.yml
ok   run.verify_ledger: Chain intact: 2 record(s).
ok   run.min_score: mean score 0.975 vs floor 0.950
ok   run.max_flip_rate: 0 case(s) over 20%
ok   run.max_unstable_cases: 0 unstable vs limit 1
ok   cases.critical: 0 critical case(s) flipped
ok   cases.min_score[gsm001]: 1.000 vs floor 1.000
PASS mean 0.975 · 0 unstable case(s) · config sha256:5a5c7d55d5368...
```

Every rule that ran is printed, passed or failed. A gate that speaks up only on failure
cannot be told apart from a gate that checked nothing, and the second one is the common
case once a policy has been in a repo for a year.

Two refusals are deliberate, because the usual way a quality gate fails is not that it
fails wrongly, it is that it silently checks nothing:

- **An unknown key is an error.** `min_scor: 0.9` is refused with exit 2, not ignored. A
  threshold that quietly does nothing is worse than no threshold, because the build going
  green gets read as evidence.
- **A rule that cannot run is a failure, not a skip.** If `drift.baseline` names a file
  that is not there, or `cases.critical` names a case the dataset no longer has, the gate
  fails and says so.

`max_score_regression` fails a build only when the drop exceeds the budget *and* the paired
test supports a regression, so a drop the experiment cannot distinguish from noise never
fails one. Before 2.0 the allowance was the budget plus a per-case half-width of 0.217,
which passed almost anything. Policy rules
and command-line flags are additive: a flag can tighten a checked-in policy, never silently
loosen it. `--json` emits every check for a pipeline to assert on. YAML needs
`pip install 'evalseal[yaml]'`; JSON policies need nothing extra.

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

**Stability classes** follow the 95% Wilson interval of the case's pass proportion, not a
flip-rate cutoff. `UNSTABLE` when the interval contains 0.5, so the direction of the
verdict is not established at this N. `STABLE` when it excludes 0.5 and every run agreed.
`BORDERLINE` in between: the majority is established, but the case did disagree with
itself. At N=5 only 5/5 and 0/5 clear 0.5, so **BORDERLINE is unreachable there** and
becomes reachable at larger N (18/20 gives [0.70, 0.97]). Each case also carries a finer
`stability_label`: `stable_pass`, `stable_fail`, `unstable`, or `insufficient_runs` when
N < 5.

Before 2.0 these were flip-rate thresholds - 0 for STABLE, up to 0.20 for BORDERLINE. At
five runs the only reachable flip rates are 0, 0.2 and 0.4, so "at most 20%" was never a
tolerance band; it was the single outcome "one of five runs disagreed", written as a
percentage that invites reading it as a 20% error budget.

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
- **A paired comparison between two runs.** Both runs cover the same items, so `diff`
  tests them item by item: an exact McNemar test on the items that changed verdict, and a
  cluster bootstrap over items for the interval.
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
- **A paired test cannot see a shift that leaves majorities alone.** McNemar works on
  items that change verdict. A change lowering every item's pass rate from 0.90 to 0.85
  moves the suite mean and flips almost nothing, and raising N makes it harder to see
  rather than easier. `diff` reports the disagreement when the bootstrap interval
  excludes zero and McNemar does not agree, instead of resolving it silently.
- **"Inconclusive" is not "the same".** It means this experiment lacked the power to
  tell, which at N=5 on a small suite is the usual outcome. `evalseal power` says what it
  would take.

## Two providers, two wire formats

`provider` in a target config picks the wire format. Left out, it means `openai`, so
every config written before this existed keeps working.

```json
{ "provider": "anthropic", "model": "claude-haiku-4-5-20251001", "max_tokens": 512 }
```

The Anthropic adapter speaks the Messages API directly rather than through an
OpenAI-compatible shim, because the differences are exactly the kind a receipt is
supposed to record: `max_tokens` is required rather than optional, the system prompt is
a top-level field rather than a message, the reply is a list of content blocks rather
than a string, there is no `system_fingerprint` to pin a backend with, and an overload
is HTTP 529 rather than 503. A shim papers over each of those, and a receipt that
records what a shim assumed is a receipt about the shim.

It is built on httpx rather than the SDK, so the cassette records the raw response
envelope and that is also what replays. No client library sits between the recording and
what a reader can verify, and the base install gains no dependency.

Two things this reports that a shim cannot:

- **`system_fingerprint` is `None`, and stays `None`.** Anthropic publishes no backend
  identifier. Reporting nothing is honest; deriving one from the model name would fake a
  pin, and `diff` would then claim two runs shared a backend on no evidence.
- **A reply cut off at `max_tokens` is flagged as truncated.** An incomplete answer
  scored naively is indistinguishable from a wrong one, which turns a configuration
  mistake into a finding about the model. The run warns instead. The same check now
  covers the OpenAI shape, where `finish_reason: "length"` means the same thing.

Status, stated plainly: the adapter is implemented and covered by 23 tests against the
documented wire format, but unlike the suites above it is **not yet backed by a recorded
live run**. See [examples/anthropic/](examples/anthropic/) to record one.

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

Policies are a library too, so a harness can apply the same rules the CLI does:

```python
from pathlib import Path
from evalseal import evaluate, load_policy

policy = load_policy("evalseal.yml")
result = evaluate(policy, record, ledger=Path(".evalseal/ledger.jsonl"))
for check in result.checks:
    print("ok " if check.passed else "FAIL", check.rule, check.detail)
```

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
