# Pre-registration: gemini-2.5-flash vs gemma-4-26b-a4b-it on GSM8K

Written and committed **before any response in this experiment was recorded**. The git
commit that adds this file precedes the commit that adds the cassettes, so the order is
checkable rather than asserted.

## Why this experiment exists

Every `evalseal diff` shown in the README on real data returns `inconclusive` or
`not comparable`. The paired test's positive path - a verdict of `regression` or
`improvement` - had only ever been exercised on scripted data. That leaves the most
important claim about 2.0 untested against a real model: that the paired test can detect
a real difference when there is one.

The existing 40-item GSM8K suite cannot do it. `evalseal power` puts the floor at six
items changing verdict in the same direction, and that suite has one failing item.

## Question

Do `gemini-2.5-flash` and `gemma-4-26b-a4b-it` differ in accuracy on grade-school math
word problems?

## Design

| | |
|---|---|
| items | 150 problems from the GSM8K test split, drawn with seed `20260922` by `build_dataset.py` |
| exclusions | the 40 problems in `examples/gsm8k` (zero overlap, checked) |
| prompt | the problem text plus the same instruction `examples/gsm8k` uses |
| scorer | `answer_match`, unchanged |
| baseline | `gemini-2.5-flash` |
| candidate | `gemma-4-26b-a4b-it` |
| repeats | N = 5 per item per model |
| temperature | left at the provider default for both, as in every other suite here |
| endpoint | Gemini's OpenAI-compatible API, same key, both models |
| cassettes | `tests/cassettes/gsm8k_compare_{flash,gemma}.json`, one per model |

## Primary analysis

Exactly one test, fixed now:

```bash
evalseal diff <flash record> <gemma record>
```

That is an exact two-sided McNemar test on items whose majority verdict differs, with a
95% cluster-bootstrap interval over items for the difference in mean score, at
alpha = 0.05. The verdict is `regression` or `improvement` only if **both** the p-value is
below 0.05 **and** the interval excludes zero; otherwise `inconclusive`.

The comparable-ness check must pass (same scorer, same dataset, same judge - there is no
judge). If it does not, the experiment is void and that is what gets reported.

## Power, stated before the result

From `evalseal power`, deterministic item model, N = 5, 150 items, baseline 0.95:

| true gap | power |
|---|---|
| 0.02 | 0% |
| 0.03 | 0% |
| 0.04 | 100% |
| 0.05 | 100% |
| 0.08 | 100% |

The step is the six-item floor: a 4-point gap on 150 items is six items. Real items are
not perfectly deterministic, and discordant items in the opposite direction dilute the
test, so the power against a real 4-5 point gap is lower than this table says. **A gap of
3 points or less cannot be detected by this design, and an `inconclusive` result must be
read that way** - not as evidence that the models are equally good.

## Commitments

- The result is reported whatever it is, including `inconclusive`.
- No second sample, seed, item count or N will be tried if this one disappoints.
- No items are dropped after responses are seen. If a response exposes a scorer defect,
  the defect is reported and both the original and corrected analyses are published.
- Secondary, descriptive only, not tested: each model's accuracy, stability counts and
  flip rates, and which items were discordant.

## Disclosed before recording

A feasibility probe on 2026-09-22 sent three problems (`gsm001`-`gsm003` from
`examples/gsm8k`, not from this sample) to both Gemma models, to check that their
replies are gradable and to gauge latency. `gemma-4-26b-a4b-it` answered 2 of 3
correctly. Three items are too few to estimate a gap, and none of them is in this sample.
Latency was 16-78 seconds per call, hence the 180-second timeout.
