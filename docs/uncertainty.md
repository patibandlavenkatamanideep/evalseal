# How much should I trust this eval result?

Eight questions, answered directly. The short version of all of them: a receipt tells
you what ran and how reproducible it was, and this page is about how much the second
half is worth reading.

---

## 1. How many repeats are enough?

**It depends on what you want to say, and the honest answer is usually "more than five".**

Ask for a margin and get a number:

```bash
evalseal evidence report.json --target-margin 0.10 --json \
  | jq '.cases[] | {case_id, repeats_for_target_margin}'
```

Two answers come back, because either alone misleads:

| | meaning | for a 0.10 margin |
|---|---|---|
| `worst_case` | reaches the margin **whatever** the true rate is, computed at p=0.5 where the interval is widest | 93 repeats |
| `at_observed_rate` | reaches it **if** the rate seen so far is roughly right | 16 repeats, unanimous case |

Reporting only the first reads as "give up". Reporting only the second is optimistic
about the very thing in question. Use the pair.

Rules of thumb that follow from the arithmetic, not from taste:

- **Below 5 repeats**, a flip rate has no readable magnitude at all. `evalseal` labels
  those cases `insufficient_runs` and `MIN_RUNS_FOR_STABILITY` is 5 for this reason.
- **5 repeats** tells you whether a case was unanimous. It cannot tell you a rate, and
  `BORDERLINE` is literally unreachable there.
- **~11–21 repeats** starts to pin a unanimous case within ±0.13 to ±0.08.
- **A genuinely flaky case needs far more than a stable one**, because the interval is
  widest near p=0.5. The cases most worth quantifying are the most expensive to
  quantify. At 21 repeats a near-coin-flip case is still ±0.196.

## 2. What does a flip rate estimate?

The fraction of runs whose verdict disagreed with the majority -
`non-majority verdicts / total runs`, not transitions, so it does not depend on the
order concurrent runs happened to finish in.

What it is **an estimate of** is the long-run tendency of that case under this
evaluator. What it **is not** is a measured property of the model: it is one sample of
a random process, and at small N a very noisy one.

Concretely, from `examples/evidence/`: a case constructed to fail exactly one run in
five reports `0.20` at five repeats — correct to the second decimal, and pure luck. The
interval says the truth is somewhere in **0.04 to 0.50**. At 31 repeats the point
estimate drifts to `0.19`, slightly *further* from the truth, while the interval narrows
to **0.09 to 0.36**. The estimate did not get luckier; it got more honest.

## 3. When is instability only directional?

When the interval is too wide to read a magnitude from. EvalSeal labels this, and the
thresholds are cut at the half-widths real repeat counts produce rather than at round
numbers:

| label | condition | what you may say |
|---|---|---|
| `insufficient_runs` | fewer than 5 repeats | "this case flipped" — nothing about how often |
| `directional_signal` | margin > 0.15 | "this case is less stable than that one", cautiously |
| `moderate_evidence` | 0.075 < margin ≤ 0.15 | a rate, with its interval attached |
| `stronger_evidence` | margin ≤ 0.075 | a rate |
| `not_binary` | float scorer, no majority verdict | read the scores directly |

A `directional_signal` flip rate is a **smoke detector**: a non-zero value is worth
investigating and its size is not worth quoting.

## 4. When should CI refuse to make a strong claim?

When the gate's green tick would imply more than the repeats support. Add an
`uncertainty` block to your policy:

```yaml
uncertainty:
  min_repeats_per_case: 5
  require_powered_flip_rate: true
  max_uncertain_cases: 0
  target_flip_rate_margin: 0.10
  confidence: 0.95
```

Each rule fails by name, with the case and the numbers:

```
FAIL uncertainty.min_repeats_per_case: case b01 has 5 repeats, policy requires 11;
     ... and 15 more
FAIL uncertainty.powered_flip_rate: 20 case(s) carry a flip-rate estimate that is
     directional only; increase repeats before gating on it. Widest interval: b01 at
     +/-0.326 against a target of 0.10
```

**The block is optional and every field defaults to off**, so a policy written before
this existed behaves exactly as it did.

One subtlety worth knowing: `require_powered_flip_rate` is checked against *your*
`target_flip_rate_margin`, not against the display thresholds in the table above. Those
thresholds are a reading aid for humans; the policy gates on the number the policy
declares. (It did not always — that was a bug, and there is a regression test for it.)

## 5. What can a receipt prove?

Integrity, provenance and comparability:

- this receipt has not been edited since it was sealed — **nobody** need be trusted
- this record was in the ledger, in this position — nobody
- these files are the ones the run used — nobody
- this came from the holder of this key — that the key was not shared or stolen
- these two runs were graded the same way — that the fingerprint covers what matters
- the score moved by more than the paired test can resolve — the statistics

## 6. What can it not prove?

- **That the eval ran at all.** A receipt records bytes, not events.
- **That the provider served the model it named.** `served_model` is whatever the
  response said.
- **That the inputs were honest.** A rubric that grades nonsense seals cleanly.
- **When anything happened.** Every timestamp is the author's own clock.
- **That no private run happened first.** See
  [pre-registration.md](pre-registration.md).
- **That a thin estimate is a real rate.** This page.

## 7. What can a third party verify?

Given a receipt, an anchor and the ledger, with no access to your machine:

```bash
evalseal verify --ledger ledger.jsonl --artifacts
evalseal anchor-verify anchor.json report.json --ledger ledger.jsonl
evalseal evidence report.json
```

They can confirm the hash chain, that the receipt is the one anchored, which key signed
it, that the sealed artifacts still match their digests, and recompute every interval
and label here — all of it deterministic and offline.

They **cannot** confirm when it happened. `created_at` is the anchoring machine's clock
and `anchor-verify` prints it on a line marked *not verified*. An RFC 3161 proof narrows
this, and is currently **experimental: message imprint checked, TSA signature NOT
verified by EvalSeal** — see [external-anchoring.md](external-anchoring.md).

## 8. What remains dependent on your harness?

Everything EvalSeal did not itself execute:

- **Agent tool traces.** EvalSeal validates and binds what the harness records. It does
  not know whether the harness omitted calls or lied about calls. It has no tool loop
  and does not replay agents. See
  [agent-eval-receipts.md](agent-eval-receipts.md).
- **CI provenance.** `require_ci_claim` is named a claim because that is what it is: any
  shell can export `GITHUB_ACTIONS=true`.
- **Artifact retention.** A digest can only be matched against a file someone kept.
- **Whether the eval measures anything worth measuring.** A tight interval around a bad
  rubric is precisely measured nonsense.

---

## Where this sits beside `evalseal power`

Two commands, two questions, deliberately not merged:

| | `evalseal evidence` | `evalseal power` |
|---|---|---|
| asks | how precisely does this run pin each case down? | how big must an experiment be to detect a difference? |
| when | after a run | before one |
| scope | one run, per case | two arms, whole suite |
| method | Wilson interval on the pass proportion | simulated exact McNemar |

Putting both behind one name would mean `--mdd` and `--target-margin` sitting on the
same command, each meaningless to the other — the reason `diff` and `drift` are also
separate.
