# Quickstart: an existing promptfoo export → a verifiable receipt

Four commands. You need an export you already have and a Python with EvalSeal installed.
No API key, no network, no model call — the export is a file, and everything here reads
it.

If you have no export to hand, this repository ships a real one:
[`tests/fixtures/importers/promptfoo/arithmetic-export.json`](../../../tests/fixtures/importers/promptfoo/arithmetic-export.json),
written by promptfoo 0.125.1 with synthetic evaluation data. Its provenance is
[recorded](../../../tests/fixtures/importers/promptfoo/PROVENANCE.md).

---

## What this gets you, and what it does not

**It gets you:** a sealed receipt that says *these are the numbers promptfoo reported,
and this is the file they came from*, with the export bound by digest so you can later
show the file you kept is the file that was sealed. Plus the per-case view, the gate, and
the ungraded rows called out instead of counted as failures.

**It does not get you:** any evidence about how the export came to exist.

> Verification can show that the retained export matches the sealed bytes.
> **It cannot prove that promptfoo executed the run as reported, or that unpublished
> runs do not exist.**

That sentence is the trust boundary. An export is a file its author can write by hand,
and a receipt over it inherits exactly that much credibility — which is still more than
a number pasted into a pull request description, and much less than a receipt from
`evalseal run`.

---

## 1. Import

```bash
evalseal import your-export.json --ledger .evalseal/promptfoo.jsonl
```

```
Imported promptfoo 0.125.1: 5 case(s), 15 observation(s), mean 0.600
source sha256:8fa9e5f632bb008646fe... (61052 bytes) -> report.json
3 field(s) this export could not fill; `evalseal report` lists them.
```

Three things worth noticing in that output.

- **5 cases, 15 observations.** The export has 15 rows; they are three repeats of five
  cases. `testIdx` runs 0–14 and looks like a case index — it is not, and an importer
  keying on it would report fifteen cases of one repeat each.
- **The source digest.** That is what gets bound into the receipt.
- **3 fields this export could not fill.** Named, not hidden. See step 2.

## 2. Read the receipt

```bash
evalseal report report.json
```

```
- Source: imported from promptfoo 0.125.1, not measured here. sha256:8fa9e5f632bb008646fe...
  gap - repeat independence: promptfoo cached by default for this run and the export
        does not mark which rows were served from cache
  gap - scorer.impl_hash: the grading code is promptfoo's, at a version this export
        names but does not digest, so no evaluator fingerprint can be computed and two
        imports are not comparable
  gap - target.served_model: promptfoo does not record what the provider said it
        served, so an alias resolving to a different snapshot would be invisible
- Observations: Independence unknown: these numbers were imported from promptfoo, which
  does not record whether a repeat was a fresh draw or a cached one being served again.

 case_id                                  runs  pass  fail  flips  flip_rate  majority
 addition                                 3     3     0     0      0%         PASS
 division with a non-integer result       3     3     0     0      0%         PASS
 a question the provider answers wrongly  3     0     3     0      0%         FAIL
 two assertions on one case               3     3     0     0      0%         PASS
 a case the provider errors on            3     0     0     0      0%         -
```

The last row is the point of doing this at all. That case **errored** — the provider
never answered, so nothing was graded. promptfoo reports it as `success: false` with
`score: 0`, exactly like the case above it that was graded and failed. Here they are
different: three ungraded repeats versus three graded failures, and the verdict strip
reads `---` rather than `FFF`.

## 3. Verify

```bash
evalseal verify --ledger .evalseal/promptfoo.jsonl --artifacts
```

```
Chain intact: 1 record(s).
ok           import_source: your-export.json matches the sealed digest
```

Change one result in the export and run it again:

```
Chain intact: 1 record(s).
changed      import_source: your-export.json does not match the digest sealed with
             this record
```

Exit code 1. Note that the **chain** still verifies — `evalseal verify` without
`--artifacts` exits 0. A reader has to be able to tell "someone edited the export" from
"someone edited the receipt", and those are two checks with two answers.

## 4. Gate it, and put the summary on the pull request

```bash
evalseal gate --ledger .evalseal/promptfoo.jsonl --policy policy.json
```

With a policy that asks about gradeability:

```json
{
  "version": 1,
  "run": {"min_score": 0.5, "require_every_case_scored": true,
          "max_unscorable_fraction": 0.1}
}
```

```
ok   run.verify_ledger: Chain intact: 2 record(s).
ok   run.min_score: mean score 0.600 vs floor 0.500 over 5 case(s), 15 repeat(s);
     3 repeat(s) were not gradeable and scored 0.0, so this floor is being applied
     to a partly ungraded run
note origin: imported (0 of 15 call(s) replayed) - these numbers are not a fresh
     measurement of the model
FAIL run.max_unscorable_fraction: 20.0% of 15 repeat(s) ungraded vs limit 10.0%
FAIL run.require_every_case_scored: 1 case(s) have no verdict at all: a case the
     provider errors on. Each scores 0.0 with nothing graded behind it, which no
     score floor can distinguish from a case that failed.
```

Exit 3. **The score floor passed.** A mean of 0.600 clears 0.500 whether those three
repeats failed or were never graded, which is why the gate has rules that ask the other
question.

`report.md` is written beside `report.json` and is what you paste into the pull request.
It leads with the import, the digest and the gaps, before any number.

---

## Comparing two exports

```bash
evalseal diff before.json after.json
```

```
Not directly comparable.

the earlier run and the later run were imported from promptfoo 0.125.1, so the grading
code is not EvalSeal's and cannot be fingerprinted. The field that decides this is
missing: scorer.impl_hash ... Both sides are missing it, which does not make them
comparable: two receipts that cannot say how they were graded agree about nothing. The
per-case verdicts, the case set and the per-row provider are still worth reading, and
the paired test below is over the cases present in both.

 metric              before  after  change
 score               0.600   0.400  -0.200 (non_comparable)
```

This is the honest answer, not a missing feature. EvalSeal's comparability check hashes
the *grading code*; here that code is promptfoo's, at a version the export names but does
not digest. If `equals` changed between the two runs, nothing in either file would show
it. The per-case movement is still worth reading — it just is not a measurement of the
model.

---

## A pilot checklist

Run this on **your own sanitized export**, not on the fixture. Twenty minutes.

- [ ] **Sanitize first.** An export contains prompts and model outputs. If your dataset
      is sensitive, so is the export, and a receipt does not change that. Check before
      you attach one anywhere.
- [ ] `pip install evalseal` in a fresh virtual environment. Note which version.
- [ ] `evalseal import your-export.json --ledger .evalseal/pilot.jsonl`.
      **Did it refuse?** If so, the message names the field. Record it — a refusal on a
      real export is the most useful thing this pilot can produce.
- [ ] Does the case count match what you expect, or did repeats collapse? Compare with
      `promptfoo`'s own output.
- [ ] `evalseal report report.json` — read the gap list. **Is anything in it something
      you assumed the receipt knew?**
- [ ] Find a case you know the answer to and check the receipt agrees.
- [ ] Do you have errored or ungraded rows? Do they read as ungraded rather than failed?
- [ ] `evalseal verify --ledger … --artifacts`, then edit one character of the export
      and run it again. Did it catch it?
- [ ] Write a policy with a threshold you would actually gate on, and run
      `evalseal gate`. **Does the exit code match what you would want CI to do?**
- [ ] If you have two exports: `evalseal diff`. Is "not comparable" the right answer for
      your situation, or does it block something you needed?
- [ ] Note anything that took more than one attempt, and anything the output said that
      you did not believe.

### What would make this worth continuing

A pilot is useful when it produces one of these, and honest when it says which:

1. **A refusal on a real export.** A shape this importer has not seen.
2. **A gap that matters.** A field you need that promptfoo has and this does not read.
3. **A wrong number.** Anything where the receipt and promptfoo disagree.
4. **Nothing.** It worked, and the receipt says less than you hoped — which is
   information about whether the trust boundary is worth the step.

> **The maintainer's demo is not adoption.** Everything on this page was run by the
> person who wrote the importer, against a fixture they generated. No one outside this
> repository has used it, and that is recorded as such rather than implied otherwise.

---

## Limits of this integration

| | |
|---|---|
| promptfoo versions | read against **0.125.1**, export schema `results.version: 3`. A different schema is refused by name, not guessed at |
| export command | `promptfoo eval -o export.json`. A `promptfoo share` payload has no `results` object and is refused |
| scores | pass/fail per row. A tool with continuous scores would need the importer widened |
| providers | a row's own `provider.id` is recorded; a multi-provider export is imported and flagged |
| comparability | never. See above |
| independence | always unknown |
| other tools | none. One adapter exists, and a second is deliberately not guessed at before an export exists to read it from |

The field-by-field mapping, with a column for what each field does not support, is in
[docs/importers/promptfoo.md](../../../docs/importers/promptfoo.md).
