# How much does this run actually tell you?

Two receipts of the same two cases, one at 5 repeats and one at 31. Nothing else
differs: the target is a local function with no network and no API key, and one case is
unanimous while the other flips exactly one run in five by construction.

Run it:

```bash
evalseal evidence examples/evidence/receipt-n5.json
evalseal evidence examples/evidence/receipt-n31.json
```

## What you get

**At 5 repeats** — nothing here can be read as a rate:

| case | n | flip rate | 95% flip-rate range | evidence | action |
|---|---|---|---|---|---|
| `c0` | 5 | 0.00 | 0.00 – 0.43 | directional signal | increase repeats |
| `c1` | 5 | 0.20 | 0.04 – 0.50 | directional signal | inspect case |

**At 31 repeats** — the same behaviour, now pinned down:

| case | n | flip rate | 95% flip-rate range | evidence | action |
|---|---|---|---|---|---|
| `c0` | 31 | 0.00 | 0.00 – 0.11 | stronger evidence | stable enough for configured threshold |
| `c1` | 31 | 0.19 | 0.09 – 0.36 | moderate evidence | inspect case |

## The point

`c1`'s true flip rate is 0.20, because the example constructs it that way: one failing
run in every five.

At 5 repeats the receipt reports `0.20`, which looks exactly right - and the interval
says the truth could be anywhere from **0.04 to 0.50**. The number being correct was
luck. Quoting "a 20% flip rate" from five runs is quoting a coin toss that happened to
land on the answer.

At 31 repeats the point estimate is `0.19`, slightly *further* from the truth, and the
interval has narrowed to **0.09 to 0.36**. The estimate did not get luckier; it got
more honest about what it knows.

Look at `c0` too. Five unanimous passes give a flip-rate range of **0.00 to 0.43**. A
case that never flipped in five tries could still flip two times in five over the long
run. That is not pessimism, it is the [rule of
three](https://en.wikipedia.org/wiki/Rule_of_three_(statistics)): unseen events are
bounded near 3/n, and 3/5 is not a small number.

## How many repeats, then?

Ask for a margin:

```bash
evalseal evidence examples/evidence/receipt-n5.json --target-margin 0.10 --json \
  | jq '.cases[] | {case_id, repeats_for_target_margin}'
```

You get two answers, because one of them alone would mislead:

- **`worst_case`** - the count that reaches your margin whatever the true rate turns out
  to be, computed where the interval is widest. For a 0.10 margin that is 93 repeats.
- **`at_observed_rate`** - the count that reaches it *if the rate you have seen is
  roughly right*. 16 repeats for a unanimous case.

Reporting only the first reads as "give up". Reporting only the second is optimistic
about the very thing you are trying to measure. The honest answer is the pair.

## What this does not tell you

Nothing about whether the model is good, and nothing about whether the eval is
measuring the right thing. It is guidance on where to spend repeats. A case with a tight
interval around a wrong rubric is precisely measured nonsense.

See also `evalseal power`, which answers the other question: how large an experiment
would have to be to *detect a difference* between two runs.
