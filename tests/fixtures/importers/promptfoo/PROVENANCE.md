# Where these fixtures came from

**`arithmetic-export.json` is a real promptfoo artifact.** It was written by promptfoo,
not by this project, and its shape was read rather than designed. The *evaluation data
inside it is synthetic* — five arithmetic questions and a toy provider, invented for this
fixture. It is not a customer's export, it is not evidence of adoption, and nobody
outside this repository has used it.

## Producer

| | |
|---|---|
| tool | [promptfoo](https://github.com/promptfoo/promptfoo) |
| version | **0.125.1** (pinned; latest at the time of generation) |
| export schema | `results.version: 3` |
| node | v25.9.0 |
| platform | darwin / arm64 |
| generated | 2026-11-12 |
| licence | promptfoo is MIT. This file is output produced by running it, and is covered by this repository's licence |

promptfoo records its own version in the export, at `metadata.promptfooVersion`. The
importer reads it from there rather than taking anyone's word for it.

## Generation command

Run from a directory containing `promptfooconfig.yaml` and `provider.js`, both committed
beside this file:

```bash
npm install --no-audit --no-fund promptfoo@0.125.1
./node_modules/.bin/promptfoo eval \
  -c promptfooconfig.yaml \
  -o export.json \
  --repeat 3 --no-table --no-progress-bar
```

No API key, no network call to any model, no cost. `provider.js` is a deterministic local
provider that answers from a fixed table, so the same command produces the same results
on any machine.

What promptfoo reported:

```
Results:
  ✓ 9 passed (60.00%)
  ✗ 3 failed (20.00%)
  ✗ 3 errors (20.00%)
```

## Digest

```
sha256:8fa9e5f632bb008646fe51c4ab1998afbb0eff2e8bff599d6f4cbb1c61a7a776
61052 bytes
```

This is the digest the importer binds into every receipt it produces from this file.
`tests/test_import_promptfoo.py` asserts it, so the fixture cannot be edited without a
test failing — which is the point: a fixture that can drift is not a fixture.

**Byte-for-byte as promptfoo wrote it.** No field was added, removed, reordered or
reformatted. Timestamps, UUIDs and the `evalId` are promptfoo's own.

## What it deliberately contains

A fixture made only of passing rows tests an importer that has only ever seen the easy
case. This one has five cases × three repeats = 15 rows:

| case | `repeatGroupId` | what it exercises |
|---|---|---|
| addition | `test-0-vars-0` | the ordinary passing row |
| division with a non-integer result | `test-1-vars-0` | an output that is not an integer |
| a question the provider answers wrongly | `test-2-vars-0` | **assertion failure** — `failureReason: 1`, a real graded FAIL |
| two assertions on one case | `test-3-vars-0` | **two `componentResults`** on one row, so a per-row score is not a per-assertion score |
| a case the provider errors on | `test-4-vars-0` | **provider error** — `failureReason: 2`, an empty `gradingResult`, and no output at all |

The last two are the ones that matter. An errored row is not a failed row, and a row
carrying two assertions is not two observations.

## The other files here

| file | genuine? | purpose |
|---|---|---|
| `arithmetic-export.json` | **yes**, promptfoo output | the import fixture |
| `promptfooconfig.yaml` | written by hand | regenerates the export |
| `provider.js` | written by hand | the deterministic provider the config points at |
| `malformed/` | **no**, written by hand | inputs the importer must refuse. Kept in a separate directory so a genuine export and a hand-made one can never be confused |

Nothing in `malformed/` is a promptfoo artifact and none of it is described as one.
