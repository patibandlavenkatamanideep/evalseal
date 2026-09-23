# Integrations

EvalSeal is not an eval platform, and is not trying to become one. Tools like promptfoo
and Braintrust already run evals, store traces, draw dashboards and organize human review,
and they do it well. EvalSeal is the layer around them that answers different questions:
*was this result stable when repeated, can it be compared with the last one, what exactly
produced it, and can someone else check that it has not been changed?*

This page describes how the two fit together. **Importing another tool's results is not
built yet** - it is the [v2.3 milestone](../ROADMAP.md). What follows says what works
today, and the proposed shape of each wrapper, marked as proposed.

## Who owns what

| | the eval tool | EvalSeal |
|---|---|---|
| running evals at scale, providers, caching | owns it | only for its own repeated runs |
| storing prompts, responses and traces | owns it | stores hashes, never the text |
| dashboards, history, search | owns it | a self-contained HTML receipt per run |
| human review and labelling | owns it | points at which cases to review |
| **repeated-run instability** | usually one run per case | flip rate and a Wilson interval per case |
| **a sealed receipt** | results live in its store | a hash-linked, optionally signed record |
| **provenance** | varies | requested vs served model, parameters, rubric and judge prompt hashes, dataset hash, code and environment |
| **comparability** | usually compares scores directly | refuses to compare runs graded differently, and pairs runs item by item |
| **a PR artifact and CI gate** | varies | [a workflow](pr-receipt-workflow.md), a policy file, JSON and JUnit output |

The division that matters: the eval tool keeps the data; EvalSeal keeps what makes a
claim about that data checkable.

## What works today

**Run the tool's cases through EvalSeal.** If a tool's cases can be written as JSONL of
`{"case_id", "prompt", "expected"}`, EvalSeal can run them itself against any
OpenAI-compatible endpoint or the Anthropic Messages API, repeat each one N times, and
seal the result. The tool keeps doing what it does; EvalSeal adds a repeated, sealed
measurement of the same cases.

**Wrap a model call from Python.** `LocalCallableTarget` wraps any function from prompt
to text, so a harness that already knows how to call its model can hand that function to
`run_eval` and get a sealed record back:

```python
from evalseal import Dataset, LocalCallableTarget, RegexScorer, run_eval

record = run_eval(
    Dataset.from_jsonl("cases.jsonl"),
    LocalCallableTarget(my_existing_model_call, name="my-model"),
    RegexScorer(r"^PASS"),
    n_repeats=5,
)
```

**Bind an exported results file to a receipt.** Whatever a tool exports can be tied to an
EvalSeal receipt by hash, so a later edit to either is detectable:

```bash
evalseal anchor report.json --ledger .evalseal/ledger.jsonl \
  --artifact exported-results.json --out anchor.json
```

This does not interpret the exported file. It proves the file is unchanged since it was
anchored alongside the receipt.

## Proposed: importing another tool's results

The v2.3 importer would turn results a tool has already produced into a sealed record,
without re-running anything. The generic shape, which a tool-specific importer would
produce from that tool's export:

```jsonl
{"case_id": "refund-policy-3", "repeat": 0, "score": 1.0}
{"case_id": "refund-policy-3", "repeat": 1, "score": 0.0}
```

```bash
# proposed - not implemented
evalseal import results.jsonl --provenance export-metadata.json --ledger .evalseal/l.jsonl
```

The importer would seal the SHA-256 of the exported file as provenance, compute flip rates
and intervals from the repeats, and produce an ordinary record that `verify`, `diff`,
`gate`, `report` and `anchor` all accept. What it could not do is vouch for how the tool
produced the scores: the receipt would say "these are the scores in this file", and its
provenance would name the file, not a model call EvalSeal observed. That distinction
would be recorded in the manifest rather than blurred.

### promptfoo (proposed wrapper shape)

- **promptfoo owns:** the test configuration, providers, assertions, running the matrix,
  caching, and its result viewer.
- **EvalSeal would own:** repeating each test and measuring flips, sealing the result,
  checking that a comparison keeps the same assertions, and the PR artifact.
- **Shape:** run the suite several times, or with repetition if the promptfoo version in
  use supports it; export the per-test results as JSON; map each test to a `case_id` and
  each pass or fail to a score; import and seal. The assertion configuration's hash
  would stand in for the rubric hash, so a changed assertion makes two runs incomparable.

- **Trust boundary:** EvalSeal would seal promptfoo's own record of the results. It can
  prove that export has not changed since it was sealed, and that two exports were
  produced under the same assertion configuration. It cannot confirm the provider calls
  behind them happened, because promptfoo made them.

The exact export fields will be taken from promptfoo's documented output format and
verified against a real export when the importer is built. None are assumed here.

### Braintrust (proposed wrapper shape)

- **Braintrust owns:** experiments, datasets, scorers, traces, the comparison UI, and
  human review.
- **EvalSeal would own:** repeat-level instability, a sealed and portable receipt of an
  experiment, comparability evidence, and the CI gate.
- **Shape:** export an experiment's per-row scores; map rows to `case_id` and repeats to
  `repeat`; import and seal, with the export's hash and the experiment identifier as
  provenance. A scorer change between two experiments would make them incomparable in
  EvalSeal's terms even if their scores look comparable in the UI.

- **Trust boundary:** the same. A sealed Braintrust export is evidence about the export,
  not about the experiment that produced it, and the receipt would say which.

As with promptfoo, field names will come from Braintrust's documented export and a real
export, not from this page.

### LangSmith / Langfuse traces (proposed wrapper shape)

- **They own:** tracing, spans, latency and token accounting, datasets, annotation
  queues, and the UI where a team actually reads runs.
- **EvalSeal would own:** turning N traced runs of the same cases into one sealed
  receipt with per-case flip rates, and refusing to compare two of them if the scoring
  configuration moved.
- **Shape:** export the runs for one dataset over one project; map each traced run to a
  `case_id` and a `repeat`; take the score from whichever evaluator the platform ran.
  The evaluator's identity - its name and version, the prompt it used, the model behind
  it - becomes the evaluator fingerprint's inputs.
- **Trust boundary:** EvalSeal would be sealing *their* record of what happened. It can
  prove the export has not changed since it was sealed. It cannot confirm the trace
  describes a call that was made, because it did not make the call.

### Replay-style workflows (Kitaru and similar)

- **They own:** capturing a session and replaying it deterministically, including tool
  calls and environment state.
- **EvalSeal would own:** the receipt for a replay - what was replayed, under which
  grader, how stable the verdicts were across repeats, and whether two replays are
  comparable.
- **Shape:** the replay tool emits per-case outcomes; EvalSeal seals them with the
  session digest as an artifact. EvalSeal's own cassettes are a narrow version of the
  same idea, so the natural boundary is: their tool replays the world, EvalSeal replays
  the grading and seals the result.
- **Trust boundary:** a deterministic replay is not a live run. A receipt over a replay
  proves the analysis is reproducible from the recording, never that the model would
  behave that way today. EvalSeal already says this about its own cassettes and would
  say it here.

## Performance and systems benchmarks

The same receipt logic applies to latency and throughput benchmarks, where run-to-run
variance is often larger than the differences being reported. There, the provenance that
decides comparability is different, and has to be sealed for a result to mean anything:

- inference backend and version,
- hardware: accelerator model and count, driver, memory,
- batch size and concurrency,
- prompt and output length distribution,
- cache state: warm or cold, prefix caching on or off,
- and the variance across repeated runs, not a single best run.

This is the BetterBench-style critique applied to throughput rather than accuracy: a
benchmark number without its configuration is not reproducible, and the configuration is
exactly what tends to go unrecorded.

EvalSeal today seals the Python version and platform, the provider, endpoint, model and
sampling parameters, and the digests of the dataset, suite and cassette. It does not seal
hardware, batch size or cache state.
A performance importer would carry these as provenance and bring them into a
comparability fingerprint of their own, so that a faster number from a different batch
size is reported as incomparable rather than as an improvement. That is not built; it is
the direction, and contributions are welcome under the roadmap's integration milestone.

## Principles for any integration

- **Complementary, not competing.** An integration should never require moving data out
  of the tool that owns it. EvalSeal needs hashes and scores, not the traces.
- **No fabricated provenance.** An importer records what it was given. It must not claim
  EvalSeal observed a model call it did not observe.
- **Comparability before deltas.** Whatever defines grading in the source tool - an
  assertion set, a scorer version, a rubric - goes into the evaluator fingerprint.
- **Verified against real exports.** Importers are built against documented formats and
  tested on recorded exports, like every other external format in EvalSeal.
