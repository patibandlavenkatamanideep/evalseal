# What every number means

Written before the Phase D changes to the arithmetic, and used to review both the
implementation and PR #12. Several rows here disagreed with the code; those
disagreements are the Phase D findings, and each is noted in place.

A number is only usable in CI if a reviewer can answer four questions about it without
reading the source: **what is one observation, what is the denominator, what happens to
ties and to missing observations, and what question does it answer.** Every row below
answers those four. Where a row says *was wrong*, the code has since been changed to
match the row — the row is the specification, not a description of history.

Three terms are used precisely throughout:

- **repeat** — one `(case, repeat, role)` measurement: one target call, scored once.
- **case** (used interchangeably with *item*) — one dataset row, with all its repeats.
- **paired case** — one case present in *both* of two runs being compared. A case in
  only one run is not an observation of a difference and is never averaged into one.

## 1. Per-repeat score

| | |
|---|---|
| unit | one repeat |
| denominator | none; this is the raw observation |
| range | binary scorers: 0.0 or 1.0. Continuous scorers: any float, by convention in [0, 1] |
| ties | not applicable |
| missing | a repeat that could not be scored carries `score=0.0` **and** `unscorable=True`. It is never silently dropped |
| question | did this one attempt satisfy the grader |
| direction | higher is better |

A score of 0.0 therefore means one of two different things, and the receipt now says
which: *the grader judged this attempt wrong*, or *the grader could not judge it*. See
§8.

## 2. Case mean (`results[].mean`)

| | |
|---|---|
| unit | one case |
| denominator | **every repeat of that case, including unscorable ones** |
| ties | not applicable |
| missing | an unscorable repeat contributes 0.0 to the mean and is counted in `unscorable_count` |
| question | what fraction of this case's attempts the grader accepted |
| direction | higher is better |

**The denominator is fixed deliberately.** Dropping unscorable repeats would make a
case's mean rise as its graded answers got harder to parse, and a suite could improve
its score by becoming less legible. A reviewer who wants the conditional-on-scorable
rate can compute it from `mean`, `n_runs` and `unscorable_count`; the gate never does.

## 3. Suite mean score (`aggregate.mean_score`)

| | |
|---|---|
| unit | one case (the mean of case means, **not** the mean of repeats) |
| denominator | the number of cases in *this* run |
| ties | not applicable |
| missing | a case absent from the dataset is absent from the denominator, which is why §10 exists |
| question | on average, how often does a case of this suite pass |
| direction | higher is better |

Means of case means, so a case with more repeats does not weigh more than a case with
fewer. The two coincide at a fixed repeat count, which is the normal configuration.

**This number is only comparable between two runs over the same case set.** A run that
dropped its four hardest cases scores higher on a suite that got easier, and the number
alone cannot show that. `diff` therefore reports `cases.removed` and the paired test
uses only paired cases (§10).

## 4. Flip count and flip rate (`results[].flip_count`, `flip_rate`)

| | |
|---|---|
| unit | one case |
| denominator | **all** repeats of the case: `flip_rate = flip_count / n_runs` |
| definition | repeats whose verdict disagrees with the case's majority verdict |
| ties | an even split has no majority. `flip_rate` is then 0.5 and `majority_verdict` is `None`; nothing invents a direction |
| missing | an unscorable repeat has no verdict and is **excluded from the flip accounting** entirely — it cannot "disagree" with a majority. It stays visible in `unscorable_count` |
| question | how often does this case contradict itself under repetition |
| direction | **lower is better** |

Not `transitions / (runs - 1)`: that definition depends on the order the repeats
happened to execute in, which is meaningless once runs are concurrent.

A flip rate below five repeats is a smoke detector, not a rate — at N=5 the only
attainable values are 0, 0.2 and 0.4, so "20%" means *exactly one of five disagreed*.
§9 is the interval that says so.

## 5. Stability class (`results[].stability`) and label (`stability_label`)

| | |
|---|---|
| unit | one case |
| denominator | the repeats of that case |
| ties | a straddling interval is `UNSTABLE`; it is the class for "we cannot say" |
| missing | computed over scorable repeats; a case with no scorable repeat is `UNSTABLE` and `insufficient_runs` |
| question | at this repeat count, do we know which way this case goes |
| direction | `STABLE` is better, but it is a class, not a quantity: do not average it |

`stability` is decided by where the 95% Wilson interval of the pass proportion sits
relative to 0.5 — `UNSTABLE` when it contains 0.5, `BORDERLINE` when it excludes 0.5 but
the case disagreed with itself at least once, `STABLE` when it excludes 0.5 and every
repeat agreed. `stability_label` is the finer taxonomy (`stable_pass`, `stable_fail`,
`unstable`, `insufficient_runs`) and is stricter: any flip at all is `unstable` there.

At N=5, `BORDERLINE` is unreachable. That is a property of five observations, not a bug.

## 6. Difference in mean score (`diff.score.delta`, `paired.delta`)

| | |
|---|---|
| unit | one **paired case** |
| denominator | the number of paired cases |
| statistic | mean over paired cases of (after case mean − before case mean) |
| ties | a case with zero difference contributes 0.0 and stays in the denominator — it is evidence of no change for that case |
| missing | a case in only one run is excluded and **named** in `items_only_in_before` / `items_only_in_after` |
| question | by how much did the average case's pass rate move |
| direction | higher is better |

Interval: cluster bootstrap resampling **whole cases**, carrying each case's repeats
together. Test: paired permutation over per-case differences for continuous scorers.

## 7. Change in majority verdicts (`paired.discordant_regressed` / `_improved`)

| | |
|---|---|
| unit | one **paired case**, reduced to one majority verdict per run |
| denominator | the discordant cases only — concordant cases carry no information about a change in direction, which is what McNemar conditions on |
| ties | a case whose repeats split evenly has no majority, is excluded, and is listed in `ambiguous_items` |
| missing | as §6 |
| question | did more cases cross the pass/fail line in one direction than the other |
| direction | fewer regressed is better |

**§6 and §7 are different claims and must never be collapsed.** A change that lowers
every case's pass rate from 0.90 to 0.85 moves §6 by 0.05 and flips almost no majorities
in §7. Running more repeats makes §7 *less* able to see it, because extra repeats sharpen
each case toward its own majority. When the two disagree, both are reported and the
report names which one the gate acted on.

The exact two-sided McNemar p-value is `min(1, 2 * P[Binomial(n, ½) ≤ min(r, i)])`, and
`n = 0` gives `p = 1.0` — nothing disagreed, which is not evidence of sameness.

> **Finding D3-1 (was wrong).** `exact_mcnemar(1000, 1000)` raised
> `OverflowError: int too large to convert to float` instead of returning 1.0, because
> the binomial tail was summed as exact integers and then multiplied by a float that had
> already underflowed to zero. A paired test over a large suite crashed the gate.

## 8. Unscorable observations (`results[].unscorable_count`, `aggregate.n_unscorable`)

| | |
|---|---|
| unit | one repeat |
| denominator | reported as a count and as a fraction of all repeats |
| question | how many observations did the grader fail to interpret |
| direction | lower is better |

A repeat is unscorable when the grader could not reach a verdict:

- an LLM judge's reply contains no `PASS` / `FAIL` token (including an empty reply, an
  apology, or a provider error string);
- an `answer_match` target reply contains no answer in the declared format (§11);
- the target reply was truncated at the token limit, so the answer is absent rather
  than wrong.

> **Finding D1-3 (was wrong).** All of these produced `score=0.0` and were
> indistinguishable from a graded FAIL. `parse_verdict` mapped `"PASS"`→1 and *everything
> else*, including `""` and `"ERROR: 503"`, →0. A rate-limited judge therefore looked
> like a model that failed every case.

An unscorable repeat is a **failure of the measurement**, so it stays in the score
denominator (§2) and is additionally counted and gateable on its own. It is never
converted into a pass and never removed from a denominator.

## 9. Evidence margin and labels (`evidence.*`)

| | |
|---|---|
| unit | one case |
| denominator | the scorable repeats of that case |
| quantity | the half-width of the confidence interval of the quantity being gated on |
| ties | none; this is a width, not a comparison |
| missing | a case with fewer than five scorable repeats is `insufficient_runs` and carries no margin |
| question | is this case's flip rate precise enough to threshold, or only precise enough to read a direction from |
| direction | lower margin is better |

> **Finding D5-2 (PR #12, adapted).** The policy field is `target_flip_rate_margin` and
> the check compared it against the half-width of the **pass-rate** interval. Those are
> different widths whenever the interval straddles 0.5: at 3 passes in 5 the pass-rate
> half-width is 0.31 while the flip-rate half-width is 0.155, so a case whose flip rate
> was pinned inside a declared 0.2 margin failed a gate that said 0.2. A policy must be
> checked against the number it names.

> **Finding D5-1 (PR #12, adapted).** `require_powered_flip_rate` crashed with
> `TypeError: unsupported format string passed to NoneType.__format__` when the widest
> uncertain case was a continuous-scored one, because such a case has no margin to
> format.

## 10. Comparability (`diff.comparable`, `drift.kind`)

| | |
|---|---|
| unit | a pair of runs |
| question | do these two runs measure the same thing |
| values | `comparable` / `non_comparable`; `drift.kind` names the cause |
| direction | not a quantity |

Comparability is decided by the evaluator fingerprint, never guessed. A target-only
change leaves two runs comparable; any evaluator change blocks comparison, and unknown
provenance is reported as unknown.

`drift.kind` answers a different question — *what moved underneath* — and is the one
place in the product that names a **cause**. A cause may only be named when the
receipts exclude the alternatives:

| kind | admissible only when |
|---|---|
| `evaluator_drift` | the evaluator fingerprint differs |
| `target_change` | evaluator identical **and** the requested model differs |
| `target_variance` | evaluator identical, target identical, and the scorer is deterministic |
| `judge_variance` | evaluator identical, target identical **including its sampling parameters**, and the scorer is an LLM judge |
| `cannot_isolate` | something else about the target or the execution differs, so two receipts cannot separate the explanations |
| `stable` | nothing moved |

> **Finding D2-1 (was wrong).** `judge_variance` was returned whenever the model *name*
> matched and the scorer was an LLM judge. Two runs of one model at temperature 0.0 and
> 1.0 were reported as "the grading setup is byte-identical and the target model is
> unchanged, yet ... came back with a different verdict" — attributing to the judge a
> change that was made to the target.

> **Finding D2-2 (was wrong).** `verdict_changed` compared `pass_count` against
> `fail_count`, which are both 0 for a continuous scorer. Every case therefore compared
> equal and a run whose mean moved from 0.10 to 0.90 reported **`stable` — "No drift"**.

## 11. The supported answer format (`answer_match`)

A scorer that guesses at an answer turns a formatting habit into model variance, and a
scorer that guesses *generously* turns a non-answer into a pass. The format is therefore
declared, and anything outside it is unscorable (§8) rather than scored on a guess:

1. `#### <answer>` on its own — the GSM8K convention. Last occurrence wins.
2. `\boxed{<answer>}` — the MATH convention. Last occurrence wins.
3. `final answer: <answer>` / `the answer is <answer>` — last occurrence wins.
4. A reply whose **last non-empty line is the answer alone**, which is what the
   `examples/gsm8k*` prompts ask for.

Decoration is stripped (backticks, quotes, a trailing period, `$`, `%`, thousands
separators). Numbers compare numerically within `tolerance`; everything else compares
case-folded.

> **Finding D1-1 (was wrong).** Rule 5 was "otherwise, the last number appearing
> anywhere in the reply". A refusal that restated the problem — *"I can't determine that
> … it says he eats 4 on each of 5 weekdays, which is 20, and then 30"* — scored a PASS
> against an expected answer of 30, identically to a reply that worked the problem and
> concluded 30. A reviewer reading the receipt could not tell the two apart.

> **Finding D1-2 (was wrong).** Rule 5 also scored a reply truncated mid-calculation on
> whatever digit the cut landed on, producing a graded FAIL for an answer that was never
> given.

## 12. Independence of observations

Every interval and test above assumes a case's repeats are separate draws. A replay
reproduces the draws that were recorded; **replaying one recorded draw twice is one
observation, not two.**

| | |
|---|---|
| unit | one `(case, repeat, role)` call |
| evidence | the cassette entry that served the call (`execution.calls[].source_key`) |
| question | how many independent draws are behind this number |
| direction | more independent draws is better |

Two calls served from **different** cassette entries are two draws even when the text
they returned is byte-identical — two independent samples that happened to agree. Two
calls served from the **same** entry are one draw counted twice, however many repeats
the receipt reports.

A receipt sealed before schema 1.8 carries no `source_key`. Independence is then
**unknown**, and is reported as unknown; no effective sample size is derived from a
receipt that cannot support one.

## What none of these numbers claim

No row above is a causal claim about a model, and none is a claim that a run happened as
described. A sealed receipt binds bytes to a record; it does not establish that the
provider served what it said, that no other run happened, or that the inputs were
honest. `drift.kind` is the only field that names a cause, it names one only in the
cases tabulated in §10, and `cannot_isolate` is a first-class answer.
