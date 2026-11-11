# PR #12, by behaviour

PR #12 — *"Uncertainty: say how much a run supports (2.3.0 candidate)"*, branch
`uncertainty-v2.3`, head `fb2fac7e604f91426e322917d93aa9461399588e`, draft, 27 files,
+3296/−22, `mergeable: CONFLICTING`.

It was **not merged and not rebased.** Its user-facing behaviour was inventoried,
reviewed against [the metric decision table](metrics.md), and ported item by item. The
branch's own commits are untouched and the PR remains open and draft; nothing here was
pushed to it.

Three of its behaviours were wrong. Each was reproduced against PR #12's own code —
loaded directly from `git show refs/remotes/origin/pr-12:src/evalseal/evidence.py` —
before anything was ported, and each fix carries the reproduction in
`tests/test_evidence.py`.

## Inventory

| # | Behaviour | Disposition | Why |
|---|---|---|---|
| 1 | `evalseal evidence` command, and keeping it separate from `evalseal power` | **keep** | The argument in the PR description is right: one is retrospective and single-run, the other prospective and comparative, and merging them would put `--mdd` and `--target-margin` on one command where each is meaningless to the other. A test pins that the flags stay apart. |
| 2 | `evidence.py`: Wilson interval of the pass proportion, the flip-rate interval derived from it, the five evidence labels, the two repeat recommendations | **keep** | Deriving the flip-rate interval from the pass-rate one rather than estimating it separately is the right call — the two can then never disagree. The labels' thresholds come from observed half-widths at real repeat counts, which is defensible and documented. |
| 3 | The margin a policy is checked against | **adapt — finding D5-2** | The field is `target_flip_rate_margin` and the check compared it against the **pass-rate** half-width. Those differ whenever the interval straddles 0.5: at 3 of 5 passes the pass-rate margin is 0.326 and the flip-rate margin 0.191, so a case whose flip rate was pinned inside a declared 0.25 failed a gate that declared 0.25. Now `pass_rate_margin` and `flip_rate_margin` are both reported under those names and the rule checks the one it names. |
| 4 | `require_powered_flip_rate` on a case with no interval | **adapt — finding D5-1** | It raised `TypeError: unsupported format string passed to NoneType.__format__`: a continuous-scored case is "uncertain" by the old predicate and then its absent margin was formatted into the detail. A case with no flip-rate interval is now reported separately, split by *why* — continuous scores, or no graded repeat at all, which is a different rule's business. |
| 5 | The rendered table's interval label | **adapt — finding D5-3** | `| 95% flip-rate range |` was hardcoded while the summary row beside it printed the real confidence, so `--confidence 0.80` produced a page that contradicted itself. The label is derived from `confidence` now. |
| 6 | Intervals over `n_runs` | **adapt** | Phase D separates a graded FAIL from a repeat the grader could not read ([§8](metrics.md#8-unscorable-observations-resultsunscorable_count-aggregaten_unscorable)). An ungraded repeat is not a Bernoulli trial, so the interval is over `n_graded`, which is reported beside `n_runs`. PR #12 predates that distinction. |
| 7 | The guard on an inconsistent receipt (`pass_count > n_runs`) | **keep** | It replaces a `math domain error` raised three frames inside the Wilson formula with a sentence naming the receipt. Extended to catch `pass_count + fail_count > n_runs`, which schema 1.8 makes reachable. |
| 8 | Policy `uncertainty` block, optional and defaulting to off | **keep** | A policy written before it behaves exactly as it did, which a test asserts. |
| 9 | `report --json`: per-case `evidence` block and suite `uncertainty` block, additive only | **keep** | The 2.2.0 key set is pinned by a test as a contract: keys may appear beside the old ones, never replace them. Both margins are named in the block rather than one being called `margin`. |
| 10 | `docs/uncertainty.md` | **adapt** | Kept, with the two margins named and the ungraded-repeat denominator stated. |
| 11 | `examples/not-comparable-drift/` | **keep** | A 0.600 → 1.000 jump caused entirely by the grader is exactly the comparability claim Phase D is about. |
| 12 | `examples/evidence/` (the 5-vs-31 receipts and README) | **defer** | Its receipts are schema-1.4 fixtures with hand-written scores and its README tabulates pass-rate margins under flip-rate headings — the defect in item 3, in prose. Re-deriving it belongs with the worked CLI fixtures, not with this port. |
| 13 | `agent_artifacts.py`: `attempt_id`, `tool_call_id`, `retry_index`, `describe_paths`, `path_digest` | **defer** | Agent-trace provenance. Nothing to do with scoring, attribution or statistics, and the brief for this phase says not to expand into unrelated work. It is a clean, self-contained addition and should land as its own change. |
| 14 | `examples/agent-retry-path/` | **defer** | Belongs with item 13. |
| 15 | RFC 3161 CMS validation deferred to v2.4, with the reasoning recorded in ROADMAP | **keep the reasoning** | The argument is sound — a recorded TSA response has a signing certificate that expires, so a chain-validating test starts failing on a date nobody chose. Phase B settled the timestamp trust model; nothing more is needed here. |
| 16 | The PR-summary emission order fix in `docs/pr-receipt-workflow.yml` | **defer** | A real documentation-accuracy fix, and it touches the workflow this phase did not otherwise change. Small enough to carry on its own. |

## What was ported, and where

| file | from PR #12 | changed in the port |
|---|---|---|
| `src/evalseal/evidence.py` | whole module | `pass_rate_margin` / `flip_rate_margin` split (D5-2); `flip_margin_at` and `repeats_for_margin(quantity=...)`; `n_graded` and `n_unscorable`; the renderer's confidence label (D5-3); the caveat names both margins and points at the independence audit |
| `src/evalseal/policy.py` | `UncertaintyRules`, `_uncertainty_checks` | the rule compares the flip-rate margin (D5-2); a case with no interval is excluded and named, split by why (D5-1); `min_repeats_per_case` counts graded repeats |
| `src/evalseal/cli.py` | `evidence` command, `_case_evidence_block`, the `report --json` blocks | both margins in the per-case block; `min_graded_repeats_per_case`, `worst_flip_rate_margin` and `n_not_binary` in the suite block; `--target-margin` help says which interval it aims at |

## Numbers in the PR description that no longer hold

The description's table reads:

| repeats | flip rate | 95% flip-rate range | evidence |
|---|---|---|---|
| 5 | 0.20 | 0.04 – 0.50 | directional signal |
| 31 | 0.19 | 0.09 – 0.36 | moderate evidence |

The ranges are right, and the `evidence` labels are cut on the pass-rate margin in the
port exactly as they were in PR #12, so those rows survive. What does not survive is the
claim implied by putting a `0.10` target beside them. Those two cases are 4 of 5 and 25
of 31:

| case | pass-rate margin | flip-rate margin |
|---|---|---|
| 4 / 5 | 0.294 | **0.232** |
| 25 / 31 | 0.135 | **0.135** |

A `target_flip_rate_margin: 0.10` is now compared against the right-hand column. At 31
repeats the two coincide, because the interval no longer straddles 0.5 — which is why
the defect was invisible in the example the PR chose to publish.

## What is still open on PR #12

Items 12, 13, 14 and 16 are deferred, not rejected. The branch is the right place for
them and they are independent of this phase. The PR stays draft and conflicting; nothing
in this phase resolved its conflicts, because the parts worth having were ported by
behaviour rather than by patch.
