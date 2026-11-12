# Reviewer checklist

For reviewing [PR #27](https://github.com/patibandlavenkatamanideep/evalseal/pull/27),
and for reviewing changes to this project afterwards. It is short on purpose: a
checklist nobody finishes is a checklist nobody uses.

## Review it in this order

The diff is large and most of it is tests and prose. **Read the claims before the
code** — almost every defect this audit found was a claim the code could not support,
not an algorithm that was wrong.

1. **[docs/metrics.md](metrics.md)** — every number's unit, denominator and direction.
   If a row looks wrong to you, that is the most valuable thing you can say.
2. **[docs/readiness.md](readiness.md)** — what each capability is claimed to be ready
   for. Disagreement here matters more than disagreement about code.
3. **The commit messages**, in order. Each carries its reproduction. You can review the
   argument without reading the diff.
4. **The code**, for anything the first three left you unsure about.

## What to push back on

- **A number without a denominator.** "20% ungraded" of what?
- **A cause named from two receipts.** `drift.kind` may only name one on the grounds in
  metrics.md §10. If you can construct a case where it names the wrong one, that is a
  bug.
- **A check that passes when it could not run.** The one deliberate exception is
  `drift.max_score_regression` in its default mode, which says so in words.
- **A claim about time, execution or independence** that the receipt cannot support.
- **A test that would pass if the feature were deleted.** Several here assert that
  something is *refused*; make sure the refusal is the thing being tested.

## Specific things worth your attention

| | why |
|---|---|
| The declared answer format in `scorer.py` | It is deliberately narrower than what models emit. A multi-word answer needs a marker. Is that the right trade for your suites? |
| `examples/gsm8k_compare/RESULTS.md` | A published finding changed: gemma's instability went from 9 items to 3 when the grader was corrected. Both readings are kept. Check the framing is fair to both |
| `docs/pr-receipt-workflow.md#the-fork-trust-boundary` | A fork can still spin the job. Three mitigations, none a fix. Is that acceptable for your repository? |
| `regression_budget_policy` default | Permissive, for compatibility. Should the default change? |
| `importers/base.py` | Deliberately not a framework. One adapter. Is that the right place to stop? |
| Schema 1.9 (`manifest.imported`) | Adds a field; nine versions still verify. Check the strip-table entry |

## Before approving

- [ ] The CI checks on the PR are green — eleven lanes, including Windows and macOS,
      the built wheel from a clean environment, and every dependency at its lower bound.
- [ ] `docs/readiness.md` matches what you would tell a colleague about this tool.
- [ ] The **Not in this release** section of
      [the release-note draft](release-notes-2.3.0-draft.md) is honest. In particular:
      no independent adoption.
- [ ] You are content with the two upgrade consequences —
      [changed `answer_match` scores and a failing evaluator pin](compatibility.md#upgrading-to-23-from-220).

## If you are reviewing a later change

Four questions that would have caught most of what this audit found:

1. **What does this let someone claim that they could not claim before?** If the answer
   is "nothing", the change is a refactor and should say so.
2. **What happens when the thing it checks cannot be checked?** A skip is a pass that
   nobody noticed.
3. **Would this pass if the feature were deleted?** Then it is not tested.
4. **Does the prose still match?** Six of this audit's findings were documentation that
   had drifted from the code, including one page telling people to run a command that
   printed nothing and exited 0.
