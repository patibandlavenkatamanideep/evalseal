# Importing a promptfoo export

Written against a real export from **promptfoo 0.125.1**, export schema
`results.version: 3` — read from the artifact, not designed. The fixture and the command
that produced it are in
[`tests/fixtures/importers/promptfoo/`](../../tests/fixtures/importers/promptfoo/PROVENANCE.md).

An import produces a receipt that says **"these are the numbers promptfoo reported, and
this is the file they came from"**. That is a narrower claim than a receipt from
`evalseal run`, and the difference is the whole of this page.

## The field mapping

Three columns, because the third is the one that gets skipped and is the one that
matters.

### Case identity

| promptfoo field | EvalSeal interpretation | what cannot be concluded |
|---|---|---|
| `results.results[].repeatGroupId` | the grouping of repeats into one case. Five groups of three in the fixture | **not a stable identity.** It is positional — `test-0-vars-0`, `test-1-vars-0` — so inserting a test at the top renames every group after it. Two exports' `repeatGroupId`s are not comparable |
| `results.results[].testCase.description` | the case id, when every case has a distinct one | nothing, where it is present and unique. Where it is absent or repeated, the importer falls back to a digest of `vars` and **says which it used** |
| `results.results[].vars` | the case's inputs; their canonical digest is the fallback case id | that two cases with the same vars are the same case. They may be the same question asked of different prompts |
| `results.results[].testIdx` | **nothing.** A flat row index across all rows | it looks like a case index and is not. With `--repeat 3` the first case occupies `testIdx` 0, 1 and 2 |
| `results.results[].id` | recorded as the row's own id | it is per *row*, regenerated every run; it identifies nothing across exports |

> The `testIdx` trap is worth stating twice. In the fixture, rows 0, 1 and 2 are three
> repeats of one case and carry `testIdx` 0, 1 and 2. An importer keying on it would
> report fifteen cases of one repeat each, and every interval in the receipt would be
> over a sample of one.

### Provider and model identity

| promptfoo field | EvalSeal interpretation | what cannot be concluded |
|---|---|---|
| `results.results[].provider.id` | `requested_model` | **not a model revision.** It is whatever string the config named. `file://provider.js` is a path; `openai:gpt-4o-mini` is an alias that resolves to a dated snapshot nobody recorded |
| `results.results[].provider.label` | provenance only, displayed | nothing about what answered |
| — | `served_model` is **left null** | promptfoo does not record what the provider said it served. An importer filling this in from `provider.id` would be manufacturing the one field that exists to catch an alias resolving elsewhere |
| — | `system_fingerprint`, `api_version`, sampling parameters are **left null** | they are not in the export. A null here means "the export did not say", and `params_source` is recorded as `unknown` rather than `explicit` |
| `config.providers[]` | recorded in the receipt's artifact note | that the provider in the config is the provider that answered each row. For a multi-provider export they differ per row, which is why the row's own `provider` is used |

### Assertion and scorer identity

| promptfoo field | EvalSeal interpretation | what cannot be concluded |
|---|---|---|
| `gradingResult.componentResults[].assertion.type` | the assertion kinds this case was graded by, recorded as scorer config | **not an EvalSeal scorer.** `equals` is promptfoo's `equals`, and nothing here re-implements it |
| `gradingResult.componentResults[]` | how many assertions ran, and which passed | that a row with two assertions is two observations. The fixture's fourth case has two, and it is **one** measurement |
| `testCase.assert` | the declared assertions, recorded | that they all ran. An errored row has an empty `gradingResult` and ran none of them |
| — | `scorer.impl_hash` is **left null** | this is the big one. EvalSeal's evaluator fingerprint hashes the *grading code*; the grading code here is promptfoo's, at a version this receipt records but cannot digest. See **Comparability** below |

### Result

| promptfoo field | EvalSeal interpretation | what cannot be concluded |
|---|---|---|
| `results.results[].success` | the repeat's verdict: pass or fail | that `false` means the model was wrong. It is also `false` for a provider error |
| `results.results[].score` | ignored in favour of `success` and `failureReason` | that it is a graded score. It is 0 for an error as well as for a failure |
| `failureReason: 0` | the assertions passed → score 1.0 | — |
| `failureReason: 1` | the assertions failed → score 0.0, a **graded FAIL** | — |
| `failureReason: 2` | a provider error → score 0.0, **unscorable** | that the model answered badly. Nothing was graded; `gradingResult` is empty |
| `gradingResult.pass` | cross-checked against `success`; a disagreement is refused | — |
| `response.output` | its digest is recorded as `response_digest` | the text is **not** stored. An export can carry a dataset's worth of responses, and a receipt is meant to be forwarded |
| `prompts[].metrics` | ignored | aggregates are not observations. `testPassCount: 9` is not nine measurements, and an importer that trusted it could not tell nine passes from three cases passing three times |

### Errors and skipped cases

| promptfoo field | EvalSeal interpretation | what cannot be concluded |
|---|---|---|
| `results.results[].error` | the `unscorable_reason` for an errored row, verbatim | — |
| `response.error` | the same, where the row-level one is absent | — |
| an errored row | **kept**, as an unscorable repeat scoring 0.0 | that it can be dropped. Dropping it would raise the suite's mean by removing the cases that failed hardest — the denominator rule in [metrics.md §8](../metrics.md#8-unscorable-observations-resultsunscorable_count-aggregaten_unscorable) |
| a case where *every* repeat errored | kept, with no verdict at all, and named in the receipt's warnings | that it passed or failed. It contributes 0.0 to the mean with nothing graded behind it |
| — | promptfoo has no "skipped" state in this schema | an importer inventing one would be describing a field that does not exist |

### Repeats

| promptfoo field | EvalSeal interpretation | what cannot be concluded |
|---|---|---|
| `runtimeOptions.repeat` | the declared repeat count, recorded | that the rows match it. The importer counts rows per group and reports what it finds |
| `repeatIndex` | the repeat number within a case | **that the repeats are independent draws.** promptfoo caches by default (`runtimeOptions.cache: true`), and a cached repeat is one draw served twice |
| `results.results[].metadata.cached`, `repeatStability.groups[].cached` | where present, a cached repeat | that absence means not cached. The fixture's provider is local and uncached, so this is untested against a cached export and the importer reports independence as **unknown** either way |
| rows sharing a `repeatGroupId` | repeats of one case | that two rows with identical output are two observations. See [statistical-contract.md §6](../statistical-contract.md#6-independence-and-the-effective-sample-size-nobody-computes) |

### Configuration available

| promptfoo field | EvalSeal interpretation | what cannot be concluded |
|---|---|---|
| `metadata.promptfooVersion` | the producer, recorded in the receipt | that the same version would reproduce it. It is what wrote the file |
| `results.version` | the export schema version; refused if unknown | — |
| `metadata.exportedAt`, `evaluationCreatedAt` | **promptfoo's claims about time**, recorded as claims | that the evaluation ran then. They are strings in a file the author controls, exactly like `CIProvenance.claimed` |
| `config.description`, `config.prompts`, `config.tests` | recorded as the suite's provenance | that the config in the export is the config that ran. It is what the export says |
| `runtimeOptions.maxConcurrency` | recorded | — |
| — | `execution.started_at` / `ended_at` | **not taken from the export.** They are when the *import* ran, and `origin` is `imported`, so nothing reads them as measurement time |

## Missing versus defaulted

The distinction that makes the rest of this honest.

| | meaning | how it is recorded |
|---|---|---|
| **missing** | the export does not contain this field | `null`, plus a named entry in the receipt's `import_gaps` |
| **defaulted** | the export contains it with a value promptfoo chose | the value, plus `params_source: "unknown"` where it is a sampling parameter |
| **absent by design** | EvalSeal has a field promptfoo has no equivalent for | `null`, and the gap list says why |

A receipt from an import carries `import_gaps` naming every field it could not fill.
`evalseal report` prints it. **Two imports missing the same fields are not thereby
comparable** — see below.

## Comparability: the field that is always missing

EvalSeal's evaluator fingerprint (scheme 3) hashes the scorer type, its settings, **the
grading code's digest**, the judge's identity and parameters, the rubric, the dataset and
the case set. A promptfoo export supplies some of that and cannot supply the grading
code: the code is promptfoo's, in Node, at a version the export names but does not digest.

So **an imported receipt has no evaluator fingerprint**, and `evalseal diff` between two
imports reports `non_comparable` with the reason naming `scorer.impl_hash`. That is not a
limitation to work around; it is the honest answer:

> Two exports from promptfoo 0.125.1 with the same assertions were graded by code this
> receipt cannot identify. If that code changed between them — a bug fix in `equals`, a
> different default — the two scores are not two readings of the same instrument, and
> nothing in either file would show it.

**Identical missing fields do not make two receipts comparable.** Two imports that are
both missing `impl_hash` are two receipts that both cannot say how they were graded, and
`diff` says so rather than treating a shared absence as a match. There is a test for
exactly that.

What *does* survive an import, and is worth running `diff` for:

- the per-case verdicts and which cases changed
- the dataset digest and the case set, so a changed question is visible
- the provider id per row, so a changed target is visible
- the paired test over the cases present in both, reported as the evidence it is

Run it with `--allow-incomparable` and read the reason first.

## What an imported receipt does not establish

- **That promptfoo ran what it says it ran.** The export is a file its author can edit.
  Verification shows the retained export still hashes to what the receipt sealed; it says
  nothing about how the export came to exist.
- **That no other run happened.** An export is one run someone chose to keep.
- **That the repeats were independent.** promptfoo caches by default and the export does
  not reliably say which rows were served from cache.
- **That the provider served what the config named.** `served_model` is null because the
  export has no equivalent.
- **Live-call provenance.** `origin` is `imported`, never `live`. Nothing in an imported
  receipt claims a provider was contacted by EvalSeal, because none was.
