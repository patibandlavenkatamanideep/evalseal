# Results: gemini-2.5-flash vs gemma-4-26b-a4b-it on GSM8K

Recorded 2026-09-22. The design, including the power it had and what it committed to
reporting, was written down in [PREREGISTRATION.md](PREREGISTRATION.md) and committed
before any response was recorded.

**The pre-registered test came back inconclusive.** It is reported here in full, because
the commitment was to report the result whatever it was, and because what the experiment
did show is worth more than the result it was designed to get.

## The primary analysis, as pre-registered

```
comparable:      True
items paired:    150
delta (gemma - flash): -0.0147
95% CI:          [-0.0320, +0.0000]
exact McNemar p: 1
discordant:      2 (flash right / gemma wrong: 1, flash wrong / gemma right: 1)
verdict:         inconclusive
```

Two of 150 items changed majority verdict, one in each direction. They cancel, so the
exact McNemar p-value is 1 - the largest it can be.

## What the two runs looked like

| | gemini-2.5-flash | gemma-4-26b-a4b-it |
|---|---|---|
| accuracy | 0.9760 | 0.9613 |
| items that flipped | **1 of 150** | **9 of 150** |
| mean flip rate | 0.27% | **2.00%** |
| stable / unstable | 149 / 1 | 141 / 9 |
| sealed hash | `sha256:ba4f9167e5333…` | `sha256:2676e3432d85a…` |

Same 150 problems, same `answer_match` grading, five repeats each, temperature left at
the provider default for both. The evaluator fingerprints match, so the two runs are
comparable.

## The finding

**The accuracy comparison is inconclusive. The stability comparison is not.**

A 1.5-point accuracy gap on 150 items is exactly what this design could not resolve: its
pre-registered power table says 4 points or more gives certainty and 3 points or less
gives nothing. Reporting that gap as "gemma is worse" would be reading noise.

Meanwhile the same receipts show gemma flipping on **9 items to flash's 1**, a mean flip
rate 7.4 times higher. That difference needed no test to see, and a single score from
either model would have hidden it completely. Two numbers - 0.976 and 0.961 - look like
a close race. The receipts say one model answers the same way when asked again and the
other often does not.

Which of those matters depends on the use. For a one-shot answer, 1.5 points of accuracy
is the story. For anything run repeatedly, or graded against a threshold, a model that
disagrees with itself on 6% of items is a different proposition from one that disagrees
on 0.7%.

## Every item that flipped, in either run

| item | flash | gemma |
|---|---|---|
| cmp033 | `PPPPP` | `FPFFF` |
| cmp045 | `PPPPP` | `FFPPP` |
| cmp072 | `PPPPP` | `FPPFP` |
| cmp087 | `PPPPP` | `FPPFP` |
| cmp104 | `PPPPP` | `PPFPP` |
| cmp108 | `PPPPP` | `PPPFP` |
| cmp122 | `PPFFF` | `PFFPP` |
| cmp130 | `PPPPP` | `FPFPP` |
| cmp131 | `FFFFF` | `FPPFF` |

The two discordant items are `cmp033`, where flash is unanimous and gemma mostly fails,
and `cmp122`, where flash mostly fails and gemma mostly passes. `cmp131` is worth a look
for the opposite reason: flash is consistently wrong and gemma is sometimes right, which
a majority-verdict test counts as no difference at all.

## What this does not show

- **Not that the models are equally accurate.** The test failed to resolve a 1.5-point
  gap; it did not find the gap to be zero.
- **Not that gemma is worse.** Its lower accuracy is within what this experiment can see,
  and one of the two discordant items goes its way.
- **Nothing about either model beyond GSM8K**, 150 problems, one temperature setting and
  one endpoint on one day.
- **The stability comparison is descriptive.** It was not the pre-registered test, and it
  carries no p-value. It is a difference large enough to read off the receipts, not a
  hypothesis that was tested.

## Reproducing it

Both cassettes are committed, so this replays with no API key and no network:

```bash
evalseal run --suite examples/gsm8k_compare/suite-flash.json --ledger .evalseal/cmp.jsonl
evalseal run --suite examples/gsm8k_compare/suite-gemma.json --ledger .evalseal/cmp.jsonl
python examples/gsm8k_compare/analyze.py .evalseal/cmp.jsonl
```

`tests/test_gsm8k_compare.py` asserts the numbers on this page, so they cannot drift
without a test failing.

## Recording notes

The gemma arm took three attempts: it died on a network read error at 699 of 750
responses, again at 749, and completed on the third. Each resume kept everything already
recorded, which is only true because cassette saves became atomic in the same release -
before that, the first crash would have destroyed all 699.

The gemma arm took about 2.4 hours of wall clock at concurrency 4; its replies average
roughly ten times the length of flash's, which is also why its cassette is 3.6 MB to
flash's 0.8 MB.
