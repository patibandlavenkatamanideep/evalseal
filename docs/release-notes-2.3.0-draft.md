# EvalSeal 2.3.0 — draft release notes

**Draft. Nothing is tagged, built or published.** The package still reports `2.2.0`;
the version bump is part of releasing, which has not happened. This is the text a
release would use, written while the work is fresh.

---

## The short version

This release is mostly **removals of things EvalSeal used to claim**. A grader that
scored replies it could not read, a comparison that reported "no drift" when the score
moved from 0.10 to 0.90, a gate that passed a 60-point regression, a ratio presented as
a share of variance, a receipt that said "15 independent draws" beside a warning that
caching was on. Each is now either correct or reported as unknown.

Two things are added rather than removed: `evalseal import`, which turns another tool's
export into a verifiable receipt, and four contract documents that say what every number
means and who has to be trusted for each claim.

**If you only read one thing:** scores from `answer_match` may change, and an evaluator
pin on such a suite will fail until you re-declare it. Both are explained under
*Upgrading*.

---

## Scoring is narrower, and says when it could not read something

`answer_match` used to end with "otherwise, the last number appearing anywhere in the
reply". Against an expected answer of `30`:

```
genuine answer 30      -> extracted '30'  score 1.0
refusal, no answer     -> extracted '30'  score 1.0     <- the problem
genuine wrong answer   -> extracted '28'  score 0.0
```

The refusal was *"I can't determine that from the information given … it says he eats 4
on each of 5 weekdays, which is 20, and then 30"*. It scored exactly like a reply that
worked the problem out.

The answer format is now **declared**: a `####` or `\boxed{}` marker, an "answer is"
phrase, or the answer alone on the final line — whichever appears **last** in the reply.
Anything else gives no answer, and a repeat with no answer is **unscorable**: it scores
0.0, stays in the mean so nothing improves by becoming less legible, and leaves the flip
rate and stability class, because a repeat with no verdict cannot disagree with a
majority.

The same distinction now applies to a judge that returns no `PASS`/`FAIL` token and to a
reply truncated at the token limit. A rate-limited judge used to look like a model that
failed every case.

## Comparisons say what the evidence supports

- **A continuous scorer's drift was invisible.** `verdict_changed` compared `pass_count`
  with `fail_count`, both 0 when there are no verdicts, so a run whose mean moved from
  0.10 to 0.90 reported `stable` — *"No drift"*. Cases now carry their means and the
  report counts majority moves and score moves separately.
- **`judge_variance` was returned whenever the model name matched.** Two runs of one
  model at temperature 0.0 and 1.0 read as *"the target model is unchanged, yet …"*.
  There is a `cannot_isolate` label now, and `drift.grounds` lists what the label rests
  on so it can be checked rather than taken.
- **`exact_mcnemar(1000, 1000)` raised `OverflowError`** instead of returning 1.0.
- **Every interval was labelled "95% CI"** whatever `alpha` was.
- **A 0.600 drop against a 0.050 budget passed the gate.** Three states are named now —
  `evidence_of_regression`, `evidence_within_budget`, `insufficient_evidence` — and
  `regression_budget_policy: require_evidence_within_budget` fails on the third.
- **`decompose` called a clipped ratio a share of variance.** A judge-only flip rate of
  0.4 against a full rate of 0.2 was clipped from 2.0 to 1.0 and rendered as *"the judge
  alone reproduces 100%"*. The ratio is unclipped and named as a ratio.

## Receipts say how they were produced

- **`evalseal import`** reads a promptfoo export into a sealed receipt, binding the
  export's digest and naming every field it could not fill. An imported receipt is never
  comparable, because the grading code belongs to another tool —
  [quickstart](../examples/integrations/promptfoo/README.md).
- **Independence** is recorded: `execution.calls[].source_key` names the cassette entry
  behind each call, which is the only evidence that separates two draws that agreed from
  one draw counted twice. Anything older, and every import, reports `unknown`.
- **`evalseal recover`** keeps the complete history when an append is interrupted. One
  partial line used to make the whole ledger unreadable.
- **`evalseal evidence`** says how precisely a run's repeats pin down each case.

## Operations

Response bodies are bounded at 8 MiB on decoded bytes, with decompression metered so a
compression ratio cannot decide how much memory is used — a 200 KB gzip bomb used to
peak at 630 MB. Calls carry a total deadline. A grading pattern that nests unbounded
quantifiers is refused before a run starts. Private keys are created owner-only and
exclusively, without following a symlink. Concurrency is capped at 64.

Limits are **measured**, not asserted: [docs/operations.md](operations.md) quotes
`scripts/measure_limits.py` on a machine it names.

## Documentation

Four pages that did not exist: [metrics](metrics.md),
[statistical contract](statistical-contract.md),
[verification policy](verification-policy.md), [compatibility](compatibility.md), plus
[operations](operations.md) and [readiness](readiness.md).

---

## Upgrading from 2.2.0

Read [compatibility.md](compatibility.md#upgrading-to-23-from-220) in full. The two that
will affect you:

**1. `answer_match` scores may change.** Precedence is now by position in the reply
rather than by marker class. On the 1500 recorded replies in `examples/gsm8k_compare`,
49 are read differently — and six of gemma's nine "unstable" items turned out to be the
grader reading a different number out of a reply the model had not changed. Its apparent
instability was 9 items to flash's 1, and is 3 to 1. No recording was regenerated;
`RESULTS.md` carries both readings side by side.

**2. An evaluator pin on an `answer_match` suite will fail.** The scoring code is part of
the fingerprint and it changed. Re-declare with
`evalseal preregister --pin-from report.json …`. The pin failing is the pin working.

Then, in decreasing order of likelihood of mattering:

- A reply the grader cannot read is no longer a FAIL. A case that was `stable_fail` on an
  erroring judge becomes `insufficient_runs`.
- A second writer on one cassette exits 1.
- `keygen` will not overwrite a public key without `--overwrite-public`.
- Responses over 8 MiB and encodings other than gzip/deflate are refused.
- **`typer<0.19` never worked** and the bound now says so. If you pinned it below that
  alongside EvalSeal, that combination could not build the CLI.
- `runs/<id>/` appears in the working directory; `report.json` and `report.md` remain as
  aliases.

Nothing requires a file to be rewritten. Old receipts verify under the schema that
sealed them, as they always will.

## Not in this release

No independent adoption — the importer has been used by one person against a fixture
they generated. No second importer, no universal results format, no dashboard. RFC 3161
chain validation still needs a CA you supply. The known-open items are each named in
[readiness.md](readiness.md) with what would move them.
