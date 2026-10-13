# A score that moved 40 points without the model changing

Two receipts. **Identical target, identical answers, identical dataset.** The only
difference is the grader: `baseline.json` accepts `^yes$`, `candidate.json` accepts
anything non-empty.

```bash
evalseal diff  examples/not-comparable-drift/baseline.json \
               examples/not-comparable-drift/candidate.json
evalseal drift examples/not-comparable-drift/baseline.json \
               examples/not-comparable-drift/candidate.json
```

## What a score alone would have said

```
0.600  ->  1.000      (+0.400)
```

A 40-point jump. On a dashboard that is a launch announcement. The model did not change
and its answers are byte-identical in both runs.

## What EvalSeal says instead

`diff` reports comparability *before* the number, and names what moved:

```
Comparable: no
verdict:    non_comparable
evaluator fields that changed: scorer settings
score:      0.600 -> 1.000 (+0.400, not comparable)
```

`drift` names the cause:

```
Judge drift: evaluator drift

Evaluator drift: the grading setup changed (scorer settings), so these two runs did
not measure the same thing. 2 of 5 shared case(s) came back with a different verdict,
and none of that can be attributed to the model under test.
```

## Why this is the whole thesis

The most misleading eval comparison is not a wrong number. It is a *right* number
compared against something graded differently. Both scores here are correct. The delta
between them is meaningless, and nothing in the two numbers reveals that.

The evaluator fingerprint is what catches it. It hashes the grading side only - scorer
type and its settings, the judge's provider, endpoint, model and sampling parameters,
the rubric and prompt-template hashes, the dataset and the exact case set - and
deliberately **excludes the target model**, because swapping the model under test is
the reason to run a benchmark in the first place.

So:

- change the model → still comparable, and `drift` calls it `target_change`
- change the rubric, the judge, or the scorer → `non_comparable`, before any delta

The failure this prevents is specific and real: until scheme 2, two *opposite* regex
graders fingerprinted identically, because the scorer's own settings were sealed
nowhere. `regex "^yes$"` and `regex "^no$"` looked like the same instrument. This
example is the smaller version of that bug, caught.

## Gating on it

A policy can refuse a comparison it cannot make:

```yaml
drift:
  baseline: ../not-comparable-drift/baseline.json
  require_comparable: true
```

That fails the build rather than reporting a delta nobody should act on.

No API key. Both receipts were produced by a local function with no network.
