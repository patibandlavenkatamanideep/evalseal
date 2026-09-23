# The PR receipt workflow

A pull request that changes a prompt, a model, a rubric or a dataset should come with a
receipt: what ran, how it was graded, how stable it was, whether it can be compared with
what is on the default branch, and why the check passed or failed. This workflow produces
that receipt on every pull request, with no API key.

The workflow file is [pr-receipt-workflow.yml](pr-receipt-workflow.yml). Copy it to
`.github/workflows/evalseal.yml`.

> **Version note: 2.1.0 is not released yet.** The workflow uses `anchor`,
> `anchor-verify` and `gate --expect-evaluator`, which land in 2.1.0. So `EVALSEAL_SPEC`
> installs from the commit that adds them
> (`git+https://github.com/patibandlavenkatamanideep/evalseal@12989ba`), which works today
> and is immutable. **After 2.1.0 is published, change that one line to the released pin:**
> `EVALSEAL_SPEC: "evalseal[yaml]==2.1.0"`. Installing from a commit is fine for trying the
> workflow out; a released version is what you want long term, because it is what the
> receipt records as the tool that sealed it.

## What it does

| step | command | fails the job when |
|---|---|---|
| replay the suite | `evalseal run --suite … --html receipt.html --fail-on none` | the replay breaks, e.g. the cassette no longer matches the dataset |
| verify | `evalseal verify` | the sealed record's hash does not match its content |
| gate | `evalseal gate --policy … --json` | a policy rule fails (recorded here, acted on last) |
| drift, if a baseline exists | `evalseal diff baseline report.json --json` and `--html` | never: `diff` reports, it does not gate |
| anchor | `evalseal anchor report.json --ledger …` | the receipt does not verify against its ledger |
| step summary | a short Python block | never (it runs even when an earlier step failed) |
| upload | `actions/upload-artifact` | never (it runs even when an earlier step failed) |
| final | exit with the gate's code | the gate failed |

Recording the gate's exit code instead of failing on it immediately is deliberate: the
summary and the artifacts are what a reviewer needs *most* when the gate fails, so they
have to be written first.

## No API key, no network, no cost

The run step replays a cassette committed to the repository: every response was recorded
once, and the replay serves them back by request and repeat. A missing entry fails the run
loudly instead of calling the provider. Do not put an API key in this workflow's secrets;
it does not need one, and a pull request from a fork should not be able to reach one.

To change what the suite measures, re-record the cassette locally
(`EVALSEAL_RECORD=1 evalseal run --suite …`) and commit it with the change. The diff of the
cassette then shows reviewers exactly which responses changed.

## Why a policy file rather than flags

With flags, `gate` reports what failed but stays silent about what passed, so a green
check says nothing about which thresholds were applied. A policy file lists every rule it
ran, passed or failed, and that list goes straight into the step summary. The example uses
[examples/evalseal.yml](../examples/evalseal.yml); write your own next to your suite.

## Reading the step summary

The summary is ordered so that nothing misleading comes first:

1. **Gate result**, and the policy it was checked against.
2. **Comparability with the baseline**, when a baseline exists, before any score. If the
   grading setup changed, the summary says **NOT COMPARABLE**, names the grading fields
   that changed, and labels the score difference as not a change in the model.
3. **Every check** the policy ran, with its detail.
4. **The run**: the EvalSeal version sealed in the receipt, the suite, the target model,
   mean score, mean flip rate, unstable cases, and the evaluator fingerprint.
5. **Artifacts**, and what to inspect.

A non-comparable baseline does not fail the gate by itself. The baseline in the workflow's
env is for reporting. To make comparability a requirement, add a drift rule to the policy:

```yaml
drift:
  baseline: ../evals/baseline-receipt.json   # relative to the policy file
  require_comparable: true
  max_score_regression: 0.02                 # fails only if the paired test also agrees
  allow_new_unstable: false
```

`inconclusive` in the drift row means the experiment could not tell, not that the runs are
the same. At N=5 on a small suite it is the usual answer; `evalseal power` says how many
items it would take to decide.

## The baseline

The drift step runs only when `BASELINE_RECEIPT` exists. The simplest way to keep one is to
commit it from the default branch:

```bash
evalseal run --suite examples/gsm8k/suite.json --ledger .evalseal/main.jsonl --fail-on none
mkdir -p evals && cp report.json evals/baseline-receipt.json
git add evals/baseline-receipt.json
```

Refresh it when the default branch's suite changes on purpose. Fetching the baseline from
the default branch's workflow artifacts instead, so nothing has to be committed, is on the
[roadmap](../ROADMAP.md).

## Pinning the grading setup

A pull request that swaps the model under test should still be graded the same way. Pin
the evaluator fingerprint in the policy:

```yaml
run:
  expect_evaluator: sha256:…   # from `evalseal report --json` -> provenance
```

or on the command line with `evalseal gate --expect-evaluator …`. This fails when the
rubric, judge prompt, judge model or parameters, scorer or dataset changed, and passes
when only the target model did. `expect_config` is stricter and fails on any change,
target included; a mismatch there says that something changed, not that the runs became
incomparable.

## What the artifacts contain

`evalseal-receipt` holds `receipt.html`, `report.md`, `report.json`, `receipt-summary.json`,
`gate.json`, `drift.html` and `drift.json` when a baseline exists, `anchor.json`, and the
ledger. They contain scores, verdicts, case ids, hashes and provenance. **They do not
contain prompts or responses**: those stay in the cassette in the repository, and the
receipt refers to them only by hash. Recording with `--store-judge-prompt` would seal the
judge prompt verbatim; this workflow never passes it.

Anyone holding the artifact can re-check it without your machine:

```bash
evalseal verify --ledger .evalseal/pr.jsonl
evalseal anchor-verify anchor.json report.json --ledger .evalseal/pr.jsonl
```

The anchor is local: its `created_at` is the runner's clock, not an independent timestamp.
What an external timestamp would add is in [external-anchoring.md](external-anchoring.md).

## Security notes

- `permissions: contents: read`. Nothing here needs write access.
- `pull_request`, never `pull_request_target`. The latter runs with the base repository's
  secrets against code from the fork.
- Actions are pinned to full commit SHAs, as in this repository's own CI.
- Step `run:` blocks read values from environment variables rather than interpolating
  `${{ }}` expressions into shell, so a crafted branch name or title cannot inject
  commands.

## Tested

`tests/test_pr_workflow.py` parses this workflow file and executes its steps in order
against a copy of the repository, evaluating each `if:` the way Actions does: the default
case with no key, a comparable baseline, a non-comparable baseline, and a failing gate.
It skips the install step and the `uses:` steps, which have no local equivalent.
