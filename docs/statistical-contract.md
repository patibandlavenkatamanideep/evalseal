# The statistical contract

What each test estimates, what it assumes, what its interval covers, and which decision
it is allowed to drive. [metrics.md](metrics.md) defines every number's unit and
denominator; this page is about the inference built on top of them.

The short version: **two different quantities, two different tests, and a gate that must
name which one it acted on.**

## 1. The estimands

| | §6 — difference in mean score | §7 — change in majority verdicts |
|---|---|---|
| estimand | the mean over paired cases of (after case mean − before case mean) | the difference between the number of cases that crossed pass→fail and fail→pass |
| unit | one paired case, carrying all its repeats | one paired case, reduced to one verdict per run |
| test | paired permutation over per-case differences (continuous), none (binary — the interval carries it) | exact McNemar on the discordant cases |
| interval | cluster bootstrap resampling whole cases, at `1 − alpha` | none; the counts are the evidence |
| sensitive to | a small shift across many cases | a large shift on a few cases |
| blind to | a change that moves a few cases a long way and the average barely at all | a shift that lowers every case's pass rate without crossing 0.5 |

**They are not two views of one thing.** A change that lowers every case's pass rate from
0.90 to 0.85 moves §6 by 0.05 and flips almost no majorities in §7. Running *more*
repeats makes §7 less able to see it, because extra repeats sharpen each case toward its
own majority and remove the discordance the test feeds on.

`PairedComparison.mean_moved_without_majorities` names exactly that disagreement, and
`diff` prints both results with a sentence saying which quantity each is about.

## 2. The assumptions, and which ones this tool can check

| assumption | needed by | checked? |
|---|---|---|
| the two runs share their case set | both | yes — unpaired cases are excluded and **named** |
| the two runs were graded the same way | both | yes — the evaluator fingerprint, reported before any number |
| a case's repeats are separate draws | both intervals | partially — schema 1.8 records the cassette entry per call, so reuse is detectable; a receipt that predates it reports `unknown` |
| cases are independent of each other | the cluster bootstrap, McNemar | **no.** A format-2 cassette can serve one recorded response to two cases that ask the same question; that is reported, not corrected for |
| under the null, a case's difference is as likely to have come out with the opposite sign | the permutation test | not checkable. It is what "the run label carries no information" means |
| under the null, each discordant case is equally likely to have gone either way | McNemar | not checkable, and the reason the test is conditional on the discordant count |
| the grader read every reply | every mean | yes — ungraded repeats are counted, kept in the denominator, and gateable |

Where an assumption cannot be checked it is stated, not assumed away. Where it can be,
the check is reported whether it passed or failed.

## 3. Coverage, and what the intervals are not

The bootstrap interval is a **percentile cluster bootstrap over cases**, 10 000
resamples, seeded, so the interval itself is reproducible. Its nominal coverage is
`1 − alpha`. Three honest caveats:

- At the case counts people run — 40, 150 — a percentile bootstrap's actual coverage is
  approximately, not exactly, nominal. It is not bias-corrected or accelerated.
- With one shared case the interval degenerates to that case's difference, twice. That is
  reported rather than smoothed.
- The interval is over **cases**. It says nothing about a different suite, a different
  day, or the same suite at a different temperature.

`per_case_halfwidth` is kept under a name that says what it measures: the widest
*per-case* interval half-width in either run. It describes how little N repeats pin down
one case. **Until 2.0 it was used as the threshold for a suite-level difference** — a
quantity about a different thing, and at N=5 it is 0.217, larger than most real
regressions, so almost everything passed.

Per-case intervals are Wilson intervals on the pass proportion at 95%, over the
**graded** repeats. Wilson rather than a bootstrap because the bootstrap collapses at the
boundary: five passes out of five resample to a width-zero interval, claiming a certainty
five observations do not support.

## 4. The decision rules

### What `diff` may conclude

```
verdict = regression | improvement | inconclusive        (never "no difference")
```

`regression` or `improvement` requires **both** `p < alpha` **and** the interval to
exclude zero. Two criteria, so the verdict never out-claims either one. Anything else is
`inconclusive`, which is a statement about the experiment and not about the models.

### What a regression budget may conclude

Three states, reported by name in the gate's detail:

| state | means | when |
|---|---|---|
| `evidence_of_regression` | the paired test supports a regression **and** the observed drop exceeds the budget | fails under either policy |
| `evidence_within_budget` | the interval's lower bound sits above `−budget`: the data rule out a drop that large | passes under either policy |
| `insufficient_evidence` | neither | depends on the policy |

```yaml
drift:
  max_score_regression: 0.02
  regression_budget_policy: fail_on_evidence_of_regression   # default
  # regression_budget_policy: require_evidence_within_budget
```

The default is permissive by construction and says so: *"Passing here means no
regression was established, not that the run is within budget."* The strict mode asks the
non-inferiority question instead, and **fails on `insufficient_evidence`** — because
failing to find a regression is not the same as finding there is none, and a tiny
experiment fails to find one every time.

### The floor nothing can get under

Exact McNemar with all `n` discordant cases moving one way gives `p = 2 × 0.5^n`, so
significance at `alpha = 0.05` needs **six** cases able to move. A suite where fewer than
six cases can change verdict cannot produce a significant result at any number of
repeats. `evalseal power` prints this number first, for that reason.

### What `power` is the power for

The paired majority-verdict test (§7) and nothing else. It is **not** the power to detect
a change in mean score (§6), and the two differ in the direction that matters. A
continuous-scored receipt is refused rather than answered, because `diff` would test it
with a permutation test that has a different power curve.

### What `evidence` is about

One run's precision, retrospectively: how tightly its repeats pin each case's flip rate.
`pass_rate_margin` and `flip_rate_margin` are both reported, under those names, because
they differ whenever the interval straddles 0.5 — at three passes in five, 0.326 against
0.191. A policy naming `target_flip_rate_margin` is checked against the second.

## 5. What `drift.kind` may claim

The only field in the product that names a **cause**, admissible only on these grounds:

| kind | admissible when |
|---|---|
| `evaluator_drift` | the evaluator fingerprint differs |
| `target_change` | evaluator identical, requested model differs |
| `target_variance` | evaluator identical, every recorded target field identical, scorer deterministic |
| `judge_variance` | as above but the scorer is an LLM judge |
| `cannot_isolate` | the model name matches and something else about the target does not |
| `stable` | no case changed majority verdict or mean score |

`DriftReport.grounds` lists which grounds held, so the label can be checked rather than
taken. `cannot_isolate` is a first-class answer, not a gap: when a sampling parameter, an
endpoint, an API version or a system prompt changed, a moved verdict is consistent with
the judge, with the target and with that setting, and two receipts cannot separate them.

## 6. Independence, and the effective sample size nobody computes

A replay reproduces recorded draws. Replaying one recorded draw twice is **one**
observation with an interval drawn around it.

Two calls that returned byte-identical text are either two independent draws that agreed
or one draw served twice, and neither the text nor its digest can tell them apart — they
are equal in both cases. The cassette entry that served each call can, and schema 1.8
seals it.

When that evidence is absent the audit says `unknown`. **No effective sample size is
derived from a receipt that cannot support one**, and `run.require_independent_repeats`
fails on `unknown` as well as on reuse, because a rule that skipped would report a pass
for a check that did not happen.

## 7. What none of this supports

- **A causal claim about a model** from any number here. `drift.kind` names a cause only
  where the receipts exclude the alternatives, and `cannot_isolate` is what it says
  otherwise.
- **An equality claim from non-significance.** The word "inconclusive" is used instead,
  everywhere, and there is a test that the string "no difference" is not printed.
- **A variance decomposition** from `decompose`. It reports two measured flip rates and
  their ratio; apportioning them would need a model of how the judge's disagreement
  varies across the target's output distribution, and the design supplies none.
- **Generalisation beyond the suite, the repeat count, the temperature and the day.**
  Everything here is conditional on all four.
