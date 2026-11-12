# Results: gemini-2.5-flash vs gemma-4-26b-a4b-it on GSM8K

Recorded 2026-09-22. The design, including the power it had and what it committed to
reporting, was written down in [PREREGISTRATION.md](PREREGISTRATION.md) and committed
before any response was recorded.

> **Re-graded under 2.3.** The 1500 recorded replies are untouched - the cassettes are
> byte-identical to the ones sealed in September - but the `answer_match` grader was
> corrected in 2.3 (see [the answer format](../../docs/metrics.md#11-the-supported-answer-format-answer_match)),
> and **49 of those replies are now read differently**: 12 of flash's and 37 of gemma's.
> Every number on this page is the 2.3 reading. The September reading is kept beside it
> wherever the two differ, because the change to the grader is itself the most
> interesting thing this comparison has produced. §"What the grader was contributing"
> has the detail.

**The pre-registered test came back inconclusive.** It is reported here in full, because
the commitment was to report the result whatever it was, and because what the experiment
did show is worth more than the result it was designed to get.

## The primary analysis, as pre-registered

```
comparable:      True
items paired:    150
delta (gemma - flash): -0.0027
95% CI:          [-0.0093, +0.0027]
exact McNemar p: 1
discordant:      1 (flash right / gemma wrong: 0, flash wrong / gemma right: 1)
verdict:         inconclusive
```

One of 150 items changed majority verdict, and it went gemma's way, so the exact McNemar
p-value is 1 - the largest it can be. **The pre-registered verdict is unchanged:
inconclusive.** Under the September grading it was inconclusive from two discordant items
that cancelled and a delta of -0.0147; it is inconclusive now from one discordant item
and a delta of -0.0027. The experiment could not resolve the gap either way, and the gap
it could not resolve turns out to be five times smaller than it looked.

## What the two runs looked like

| | gemini-2.5-flash | gemma-4-26b-a4b-it |
|---|---|---|
| accuracy | 0.9760 | 0.9733 |
| items that flipped | **1 of 150** | **3 of 150** |
| mean flip rate | 0.27% | 0.67% |
| stable / unstable | 149 / 1 | 147 / 3 |
| unscorable replies | 0 of 750 | 0 of 750 |
| *accuracy, September grading* | *0.9760* | *0.9613* |
| *items that flipped, September grading* | *1 of 150* | *9 of 150* |

Same 150 problems, same `answer_match` grading, five repeats each, temperature left at
the provider default for both. The evaluator fingerprints match, so the two runs are
comparable.

## The finding

**The accuracy comparison is inconclusive, and most of the stability difference was the
grader.**

A 0.3-point accuracy gap on 150 items is far inside what this design could resolve: its
pre-registered power table says 4 points or more gives certainty and 3 points or less
gives nothing. Reporting that gap as "gemma is worse" would be reading noise.

Gemma still flips on more items than flash - 3 to 1 - and that remains descriptive
rather than tested. But under the September grading the same receipts showed **9 to 1**,
and six of gemma's nine unstable items were not the model disagreeing with itself at all.
They were the grader reading a different number out of the same reply.

## What the grader was contributing

Gemma answers inside a long `<thought>` block and then states its answer on the final
line, as the prompt asks. The September extractor preferred the phrase "the answer is X"
wherever it appeared over the reply's own final line - so on 37 of 750 replies it graded
gemma on a number the model had written down mid-reasoning and then moved past. Flash,
whose replies are about a tenth as long and rarely rehearse an answer mid-stream, was
affected on 12.

The effect on the per-item picture is the whole point:

| item | flash | gemma, 2.3 grading | gemma, September grading |
|---|---|---|---|
| cmp033 | `PPPPP` | `PFPPF` | `FPFFF` |
| cmp072 | `PPPPP` | `PPPFP` | `FPPFP` |
| cmp122 | `PPFFF` | `FFPPP` | `PFFPP` |
| cmp045 | `PPPPP` | `PPPPP` | `FFPPP` |
| cmp087 | `PPPPP` | `PPPPP` | `FPPFP` |
| cmp104 | `PPPPP` | `PPPPP` | `PPFPP` |
| cmp108 | `PPPPP` | `PPPPP` | `PPPFP` |
| cmp130 | `PPPPP` | `PPPPP` | `FPFPP` |
| cmp131 | `FFFFF` | `FFFFF` | `FPPFF` |

The last six rows are items where gemma gave the same answer all five times and the
grader did not. `cmp131` moves the other way: gemma is consistently wrong there, where
September made it look occasionally right.

This is the failure mode the project exists to expose, found in its own flagship
example. A model was reported as 7.4 times less reproducible than its competitor, and
two thirds of that difference was an artefact of answer extraction - a part of the
instrument that the receipts of the time sealed a digest of but that nobody had a reason
to question until the numbers were pulled apart. The cassettes made the correction
possible without recording a single new response.

## Every item that flipped, in either run

| item | flash | gemma |
|---|---|---|
| cmp033 | `PPPPP` | `PFPPF` |
| cmp072 | `PPPPP` | `PPPFP` |
| cmp122 | `PPFFF` | `FFPPP` |

The one discordant item is `cmp122`, where flash mostly fails and gemma mostly passes.
`cmp131` is worth a look for the opposite reason: both models are consistently wrong
there, which a majority-verdict test counts as no difference at all.

## What this does not show

- **Not that the models are equally accurate.** The test failed to resolve a 1.5-point
  gap; it did not find the gap to be zero.
- **Not that gemma is worse.** Its lower accuracy is far inside what this experiment can
  see, and the single discordant item goes its way.
- **Not that the September numbers were dishonest.** They were what that grader produced
  on those bytes, and the receipts that sealed them still verify. What changed is the
  grader, which the receipts identify by digest - which is how the change was found.
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
without a test failing. It also pins the September readings that this page quotes beside
them, so the comparison between the two gradings cannot rot either.

## Recording notes

The gemma arm took three attempts: it died on a network read error at 699 of 750
responses, again at 749, and completed on the third. Each resume kept everything already
recorded, which is only true because cassette saves became atomic in the same release -
before that, the first crash would have destroyed all 699.

The gemma arm took about 2.4 hours of wall clock at concurrency 4; its replies average
roughly ten times the length of flash's, which is also why its cassette is 3.6 MB to
flash's 0.8 MB.
