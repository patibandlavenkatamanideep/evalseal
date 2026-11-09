# Fingerprint schemes, and how to move between them

An evaluator fingerprint answers one question: **can these two numbers be compared?**
It is a hash of the measuring instrument - everything that decides how a response
scores - and it deliberately ignores the model under test, because comparing model A
with model B is what a benchmark is for.

Every fingerprint carries the scheme that produced it:

```
evalseal-fp/3:sha256:8191ca03a8b49da5b5f872f106c07a0d1aad2fae2180aaad6ba6eb93f89f4c29
            ^
            the scheme
```

A scheme is bumped when something that moves a score turns out not to move the
fingerprint. That is a defect in the fingerprint, not a feature of the run, so the fix
cannot be backward compatible: the new value covers inputs the old one never hashed.

## What each scheme covers

| | 1 | 2 | 3 |
|---|---|---|---|
| scorer type | ✓ | ✓ | ✓ |
| rubric hash, judge prompt template hash | ✓ | ✓ | ✓ |
| dataset hash, exact set of case ids | ✓ | ✓ | ✓ |
| judge provider, model | ✓ | ✓ | ✓ |
| judge endpoint | | ✓ | ✓ |
| judge `max_tokens` | | ✓ | ✓ |
| scorer's own settings (regex, tolerance) | | ✓ | ✓ |
| **the grading code itself** | | | ✓ |
| **judge system prompt (by hash)** | | | ✓ |
| **judge API version** | | | ✓ |
| **which parameters were inherited rather than set** | | | ✓ |

Each row in bold was a way to be misled. The first is the one that motivated scheme 3:

```
$ evalseal run --suite examples/gsm8k/suite.json      # mean 1.00
# edit how `extract_answer` picks the answer out of a reply
$ evalseal run --suite examples/gsm8k/suite.json      # mean 0.00
# both receipts: evalseal-fp/2:sha256:1488ed19...
```

Two runs that measured opposite things, one fingerprint. `--expect-evaluator` passed
and `evalseal diff` reported `comparable: true` with no configuration change at all.

Scheme 3 takes the grading code's identity from the **normalized syntax tree** of the
scoring method and of everything outside the standard library it reaches - helpers,
and the module constants they read, such as the regexes that find a model's answer.
Comments, docstrings and line wrapping are normalized away, so improving a docstring
does not invalidate anybody's pins. See `src/evalseal/identity.py`.

What it does *not* claim: a digest of the shipped source says what the code in this
build is. It is not proof that this code ran - a sealed digest is a record made by the
same process it describes. It makes an undeclared change to the grading algorithm
visible to someone comparing two receipts, which is the gap it closes.

## What happens to an old pin

Nothing silent. A pin made under an older scheme cannot match, and every place that
compares one says so in those terms:

```
$ evalseal gate --ledger .evalseal/ledger.jsonl --expect-evaluator evalseal-fp/2:sha256:105552a7...
FAIL run.expect_evaluator: The pinned evaluator fingerprint was made under scheme 2;
     this build computes scheme 3, which covers fields the older scheme did not. The
     two cannot be compared, and this is not evidence that anything about the run
     changed. Re-pin deliberately: check what moved with `evalseal diff`, then take the
     new value from `evalseal report --json`. See docs/fingerprint-schemes.md.
```

The gate **fails** rather than passing with a warning, and it fails rather than
accepting the pin as a match. Both directions matter: a pin that silently passed would
be a false green, and a mismatch reported as "the grading setup changed" would send
someone hunting a change nobody made.

This applies to all three places a fingerprint is pinned - `gate --expect-evaluator`,
`run.expect_evaluator` in a policy file, and `evaluator_fingerprint` in a
pre-registration - because all three route through one explanation.

## Re-pinning

EvalSeal will not rewrite a pin for you. A pin is a policy decision: it says "this is
the instrument I approved", and a tool that updates it on your behalf has quietly
approved a new instrument.

The workflow is two deliberate steps.

**1. Find out what actually moved.** Upgrading EvalSeal and editing your rubric are
different events, and a scheme bump hides the difference if you skip this.

```
evalseal diff --before evals/baseline-receipt.json --after report.json
```

Under scheme 3 the diff names the new fields, so `scorer implementation`,
`judge system prompt` or `judge api version` appear by name if one of them changed.
If the only difference is the scheme, the diff says that too, rather than blaming a
field.

**2. Take the new value from a receipt you have inspected.**

```
# the fingerprint of a run you have looked at
evalseal report --ledger .evalseal/ledger.jsonl --json | \
  python -c 'import json,sys; print(json.load(sys.stdin)["provenance"]["evaluator_fingerprint"])'

# or, for a pre-registration, pin it from that receipt
evalseal preregister --suite examples/gsm8k/suite.json --pin-from report.json \
  --require-artifact cassette --require-artifact dataset --require-artifact suite \
  --out examples/prereg.json
```

Then commit the change on its own, so the new pin is reviewable as a decision. This
repository's own example contract was migrated that way; `git log -p examples/prereg.json`
shows the diff a reviewer sees - one field, one scheme, one new value.

## Old receipts

A receipt sealed before the bump stays verifiable. The record schema is versioned
separately from the fingerprint scheme, and `evalseal verify` re-hashes a record as the
schema that sealed it would have, so a 1.0 ledger from an early release still reports an
intact chain. Fields a record's own schema never covered are reported as unsealed and
quarantined rather than read as evidence.

What an old receipt cannot do is take part in a *field-level* comparison against a new
one. A record sealed before 1.6 has no digest of the grading code, no judge API version
and no judge system prompt hash, so a fingerprint difference against a 1.6 record may be
an artefact of the older schema rather than a change anyone made. `evalseal diff` says
exactly that instead of guessing:

> the two runs were sealed under different schema versions (1.5, 1.6). Fields the
> evaluator fingerprint covers [...] are absent from the older record, so they cannot be
> compared. Re-record the older run to compare these directly.

Re-recording is possible offline whenever the run's cassette was sealed with it, which is
what `--require-artifact cassette` is for.

## Schema versions, for reference

| schema | adds |
|---|---|
| 1.1 | `run_config.concurrency` |
| 1.2 | suite, code and environment provenance; per-case verdict detail |
| 1.3 | no new field; `scorer.judge_prompt_hash` changes meaning from the last prompt sent to the prompt *template* |
| 1.4 | provider and `max_tokens` for target and judge; scorer `config_hash`; artifact digests |
| 1.5 | the CI claim; a v2 suite's evaluator and target section digests |
| 1.6 | `scorer.impl_hash`; judge `api_version` and `system_prompt_hash`; per-parameter `unset` reasons |
