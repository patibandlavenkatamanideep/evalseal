# Compatibility and migration

Four things version independently, and conflating them is how an upgrade breaks
something nobody expected:

| | current | this build reads | this build writes |
|---|---|---|---|
| **package** | 2.2.0 (unreleased work toward 2.3) | — | — |
| **record schema** | `1.8` | 1.0 … 1.8 | 1.8 |
| **evaluator fingerprint scheme** | `evalseal-fp/3` | scheme 3; an older pin fails with an explanation | scheme 3 |
| **cassette format** | `3` | 2 and 3 | 3 |

A receipt carries its own `schema_version`, so the package version that reads it and the
one that wrote it need not match. The rules below are what make that true.

## Record schema

### The rule

**A record is re-hashed the way its own schema would have hashed it.** Loading an older
record into today's models fills the fields that schema lacked with defaults, which would
change its hash and look like tampering. So before hashing, every field added after the
sealing version is stripped — the table in `ledger._FIELDS_ADDED_IN` — and the record
verifies as `legacy`, naming the version that sealed it.

Two consequences worth knowing:

- **An unsealed field cannot be read as evidence.** A field the sealing schema did not
  cover is absent from the bytes the hash commits to, so any value may be written into
  it without disturbing the chain. `verify` quarantines those fields, names them, and
  reports the record as verified — because a real old receipt *is* real; what changes is
  that the uncovered field is not treated as sealed.
- **A newer schema is refused, not guessed at.** A version this build does not know may
  have added fields it cannot see, or changed what an existing field means — 1.3 added no
  field at all and only changed what `scorer.judge_prompt_hash` means. Verifying such a
  record would be vouching for a format this build cannot read.

### What each version added

| version | added | effect on a pin |
|---|---|---|
| 1.0 | the original record | — |
| 1.1 | `run_config.concurrency` | none |
| 1.2 | suite, code, environment and judge-prompt provenance, per-case verdict detail (13 paths) | none |
| 1.3 | **no field.** `scorer.judge_prompt_hash` changed from the last judge prompt sent to the judge prompt *template* | a 1.2 and a 1.3 judge record disagree on this hash for that reason alone |
| 1.4 | provider, `max_tokens`, scorer config hash, `artifacts` (6 paths) | evaluator fingerprint gained inputs |
| 1.5 | CI claim, version-2 suite section digests (3 paths) | none |
| 1.6 | scorer `impl_hash`, judge API version and system prompt hash, per-parameter absence reasons (7 paths) | **evaluator fingerprint gained inputs.** 1.6 is the first version recording everything scheme 3 hashes |
| 1.7 | the execution manifest: per-call origin, digests, attempts, truncation | none — it describes what ran, not how it was graded |
| 1.8 | `results[].unscorable_count` / `unscorable_reason`, `aggregate.n_unscorable` / `n_observations`, `execution.calls[].source_key` (5 paths) | none |

A pair of records sealed before 1.6 cannot be compared field-for-field against one sealed
after it; `drift` says so by name rather than reporting a fingerprint difference as a
change.

### Migrating a receipt

You do not. There is no rewrite step and there should not be: rewriting a record changes
its hash, which is the whole point of having one. Old receipts verify as they are,
forever, under the schema that sealed them.

## Evaluator fingerprint

A fingerprint answers *can these two numbers be compared*. It carries its scheme in the
string — `evalseal-fp/3:sha256:…` — so a pin made under an older scheme **fails with an
explanation** rather than silently mismatching, and fails rather than passing.

| scheme | hashed | the gap it closed |
|---|---|---|
| 1 | scorer type, judge model, rubric, dataset | — |
| 2 | + the scorer's own settings | `regex "^yes$"` and `regex "^no$"` fingerprinted identically |
| 3 | + the scoring **code**, the judge's API version and system prompt, per-parameter absence | a fork that changed answer extraction moved every score and left the pin untouched |

Re-pinning is a deliberate two-step workflow, documented in
[fingerprint-schemes.md](fingerprint-schemes.md): read the new fingerprint, then commit
it. EvalSeal never re-pins for you — a tool that updates a pin when it stops matching has
removed the pin.

**A change to the grading code moves the fingerprint.** That is the feature, and it has a
cost: 2.3's corrected `answer_match` extraction moved it, so `examples/prereg.json` was
re-declared. A pre-registration pinning a 2.2.0 evaluator will fail against a 2.3 run of
the same suite, by design — see the migration note below.

## Cassette format

| format | key | status |
|---|---|---|
| 2 | `sha256(request, repeat)` | **readable, not appendable.** Two cases asking the same question share one entry: one provider draw served both and their scores agree by construction |
| 3 | `sha256(request, case, repeat, role)` | written by this build |

A format-2 cassette replays, because a recording someone kept is evidence and breaking it
would throw that evidence away. Every run that touches one gets a receipt warning naming
the consequence, and the independence audit can detect the cross-case collision it
causes. It cannot be appended to: new entries would be keyed by a rule the existing ones
were not.

**To migrate:** re-record. `evalseal run --cassette-mode fresh` draws again and writes
format 3. There is no in-place converter, because the missing information — which case
each entry belonged to — is not in the file.

## Policy, suite and pre-registration files

| file | compatibility |
|---|---|
| policy | additive. Every key added in 2.3 (`run.max_unscorable`, `run.max_unscorable_fraction`, `run.require_every_case_scored`, `run.require_independent_repeats`, `drift.regression_budget_policy`, the whole `uncertainty` block) defaults to off or to the previous behaviour. **An unknown key is refused**, so a policy from a *newer* version fails loudly on an older build |
| suite v1 (flat) | still read. `max_response_bytes` and `deadline` are new optional keys |
| suite v2 (sectioned) | still read; the same two keys live under `run` |
| pre-registration | additive. A contract written by 2.2.0 is honoured by 2.3 **except** for its evaluator pin, if the grading code it pinned has changed |

## `report --json` and the machine-readable surface

**Additive only.** Keys may appear beside the existing ones; they are never renamed or
removed. A test pins the 2.2.0 key set as a contract.

2.3 adds `summary.n_unscorable`, `summary.n_observations`, `independence`, `uncertainty`,
per-case `unscorable_count` / `unscorable_reason` / `evidence`. In `diff --json`,
`paired.ci`, `paired.confidence`, `paired.ci_label`, `paired.summary` and
`paired.mean_moved_without_majorities` are new; `paired.ci95` is still there and still
carries the same numbers, which are a 95% interval only when `alpha` is 0.05.

## Python and dependencies

Python 3.11, 3.12 and 3.13, tested on all three plus macOS and Windows. Dependencies
carry **lower bounds only** — `pydantic>=2.6`, `typer>=0.12`, `httpx>=0.27`,
`rich>=13.7`, `cryptography>=42` — and a CI lane installs each at exactly its lower bound
and runs the suite, so the bounds are claims rather than guesses. Nothing transitive is
pinned: a library that pins its tree cannot be installed beside anything else.

## Upgrading to 2.3 from 2.2.0

Read in this order; the first two will affect you.

1. **Scores may change.** `answer_match` no longer falls back to "the last number
   anywhere in the reply", and precedence is now by position rather than by marker class.
   On the 1 500 recorded replies of `examples/gsm8k_compare`, 49 are read differently. Not
   a receipt is altered — re-grading the same bytes is the point — but a *replay* of an
   old cassette may produce a different score than it did. `examples/gsm8k_compare/RESULTS.md`
   carries both readings, and the difference is worth understanding before you assume it
   is noise.
2. **An evaluator pin on an `answer_match` suite will fail.** The scoring code is part of
   scheme 3, and it changed. Re-declare: `evalseal preregister --pin-from report.json …`.
   The pin failing is the pin working.
3. **A reply the grader cannot read is no longer a FAIL.** It is `unscorable`, scores 0.0,
   stays in the mean, and leaves the flip rate and stability class. A case that was
   `stable_fail` on a judge that kept erroring becomes `insufficient_runs`.
4. **`parse_verdict` returns `None`** instead of 0 for a reply with no PASS/FAIL token.
   Only relevant if you call it directly.
5. **A second writer on one cassette now exits 1** with the path and the holding pid.
6. **`keygen` refuses to overwrite a public key** without `--overwrite-public`.
7. **A grading pattern with nested unbounded quantifiers is refused.** `(a+)+` and
   friends; fifteen patterns a real scorer uses are tested to still load.
8. **Responses over 8 MiB are refused**, as are content encodings other than gzip and
   deflate. Raise `--max-response-bytes` if you genuinely need more.
9. **`runs/<id>/` appears** in the working directory. `./report.json` and `./report.md`
   are still written, as aliases for the latest run.
10. **New commands:** `evalseal evidence` and `evalseal recover`.

Nothing in that list requires a file to be rewritten. The only *action* is step 2, and
only if you pin an evaluator fingerprint.
