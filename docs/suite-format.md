# Suite file format

A suite file names the dataset, the target, the scorer and the run settings in one
place. There are two formats. **Both work; neither is going away without notice.**

## Why a split exists

The original format is flat:

```json
{
  "dataset": "dataset.jsonl",
  "target": "target.json",
  "scorer": "scorer.json",
  "n_repeats": 5,
  "cassette": "../../tests/cassettes/gsm8k.json",
  "fail_on": "none"
}
```

That file mixes two different kinds of thing, and the mixture has a real consequence.
The **evaluator** is the instrument: the scorer, the rubric, the judge. The **target**
is what is being measured. A change to the first means two runs are no longer
measuring the same thing. A change to the second is *the entire point of running a
benchmark*.

Because the flat file carries both, its whole-file digest cannot be used to decide
comparability: pointing `target` at a different model changes the suite hash while the
grading is untouched. So the suite hash is recorded in every receipt and deliberately
left out of the evaluator fingerprint - and a reader of the file has no way to see
which lines matter for comparability and which do not.

Version 2 makes that visible in the file itself.

## Version 2

```json
{
  "version": 2,
  "evaluator": { "scorer": "scorer.json" },
  "target":    { "config": "target.json" },
  "dataset":   { "path": "dataset.jsonl" },
  "cases":     { "only": ["gsm001", "gsm002"] },
  "run": {
    "n_repeats": 5,
    "concurrency": 4,
    "cassette": "../../tests/cassettes/gsm8k.json",
    "fail_on": "none"
  },
  "policy": { "file": "evalseal.yml" }
}
```

| section | holds | changing it means |
|---|---|---|
| `evaluator` | the scorer config: type, pattern, rubric, judge | the instrument moved; runs are **not comparable** |
| `target` | the model under test and its parameters | the thing being measured changed; runs **stay comparable** |
| `dataset` | the dataset file | a different population; not comparable |
| `cases` | `only`, a subset of case ids to run | a different case set; not comparable |
| `run` | repeats, concurrency, cassette, `fail_on`, retries, timeout, ledger, outputs | how it executed, not what it measured |
| `policy` | `file`, a policy to apply at gate time | the thresholds |

Every section is optional, and command-line flags still override whatever the suite
says. A suite is a default, not a cage.

A typo is an error in both formats, at both levels: an unknown *section* is refused,
and so is an unknown key inside a known section. `n_repeat` in the `run` section fails
the command rather than silently running five repeats because that is the default.

## What the receipt records

A version-2 suite seals two extra digests alongside the whole-file hash:

```json
"suite": {
  "name": "suite-v2",
  "path": "examples/gsm8k/suite-v2.json",
  "hash": "sha256:…",
  "evaluator_hash": "sha256:…",
  "target_hash": "sha256:…"
}
```

They are canonical-JSON digests of the two sections, so reformatting the file or
reordering its keys does not change them. A flat-format suite records `null` for both,
because it has no sections to hash apart and a made-up digest would be worse than an
absent one.

## What did *not* change: the fingerprint

**The evaluator fingerprint is unchanged. It is still scheme 2, and a v2 suite produces
the same fingerprint as the flat suite it replaces.** A test asserts exactly that, by
running both formats and comparing.

This is deliberate, and it is the opposite of what you might expect from a split whose
whole motivation was the fingerprint. Two reasons:

1. **It would add nothing.** Scheme 2 already covers the evaluator's substance
   directly: the scorer type, the scorer's own config hash, the judge's provider,
   endpoint, model and sampling parameters, the rubric hash, the judge prompt template
   hash, the dataset hash and the case-set hash. The `evaluator` section points at the
   scorer file, whose contents are already hashed. Adding the section digest would
   fingerprint the *path* to the scorer alongside the scorer itself.
2. **It would break every pin for that gain.** A fingerprint change is a scheme change,
   and a scheme change makes every `--expect-evaluator` pin and every stored baseline
   re-pin. That cost is worth paying to close a real hole - scheme 2 was, because two
   opposite regexes fingerprinted identically. It is not worth paying for a
   redundancy.

So the section digests are **recorded, not fingerprinted**. They let a reader of a
receipt see which half of a changed suite moved, without changing what "comparable"
means.

If a future version *does* fingerprint them, it will be scheme 3, with the same
migration story scheme 2 had: a pin made under an older scheme fails with an
explanation telling you to re-pin, rather than reporting an unexplained mismatch.

## Migrating

There is no rush and no deprecation. To move a suite:

1. Wrap `scorer` in `evaluator`, `target` in `target.config`, `dataset` in
   `dataset.path`.
2. Move `n_repeats`, `concurrency`, `cassette`, `fail_on`, `max_retries`, `timeout`,
   `ledger`, `junit_xml` and `sign_key` into `run`.
3. Add `"version": 2`.
4. Run the suite once and confirm the evaluator fingerprint is the value you had
   before:

```bash
evalseal report --json | jq -r .provenance.evaluator_fingerprint
```

It should be unchanged. If it is not, something other than the format moved.

`examples/gsm8k/suite.json` and `examples/gsm8k/suite-v2.json` are the same evaluation
in both formats, and a test asserts they stay that way.
