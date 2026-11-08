# Pre-registration

> **Can EvalSeal stop cherry-picking?**
>
> No. Nothing that runs on the same machine as the eval can prove that a private run
> never happened. A pre-registration makes the *contract* checkable, not the author
> honest.

This page explains what that sentence means in practice, what a pre-registration does
buy, and where the line falls.

## The objection

The question came up as soon as receipts did:

> *"Does EvalSeal stop someone from rerunning until they get a good result and only
> sealing that one?"*

It does not, and it is worth being exact about why. EvalSeal's ledger is a local file.
It records what was appended to it. Someone who runs the suite five times can delete
the ledger four times and seal the fifth run into a fresh one, and the result verifies
perfectly, because there is no observer outside the machine to notice the other four.
A hash chain proves that a sequence was not edited *after* it was written. It says
nothing about sequences that were never written down.

Any tool that claims otherwise on a local-first design is overclaiming.

## What a pre-registration changes

The move is not to detect hidden runs. It is to **fix the contract before any number
exists**, so that the interesting forms of cherry-picking stop being silent edits and
start being visible ones.

```bash
evalseal preregister --suite examples/gsm8k/suite.json \
  --require-artifact cassette --require-artifact dataset \
  --policy evalseal.yml --out prereg.json
git add prereg.json && git commit -m "Pre-register the GSM8K comparison"
```

That file pins, by digest, before the run:

| declared | what it stops being invisible |
|---|---|
| suite digest | swapping the whole configuration and keeping the name |
| dataset digest | editing the data after seeing which items were hard |
| `case_set_hash` over the case ids | dropping the eight items that failed |
| `n_repeats` and `min_repeats_per_case` | reporting a lucky single run as a five-repeat one |
| evaluator fingerprint (with `--pin-from`) | swapping the judge or the rubric after seeing the score |
| required artifact roles | publishing a score with no cassette behind it (**fail-closed**: a role sealed as `external` fails unless allow-listed) |
| an embedded policy | loosening the threshold after a failure |
| `require_ci_claim` | producing the receipt on the laptop of the person being measured (a *claim*, see below) |
| `require_signature` + `trusted_public_keys` | publishing a receipt nobody signed, or one signed by a key you never agreed to trust |

Then, at gate time:

```bash
evalseal gate --ledger .evalseal/ledger.jsonl --prereg prereg.json
```

```console
ok   prereg.suite: suite digest matches (sha256:4af862fcb84ed...)
ok   prereg.dataset: dataset digest matches (sha256:c9623577a124d...)
ok   prereg.case_set: the declared 40 case(s) ran
ok   prereg.n_repeats: ran 5 repeat(s) as declared
ok   prereg.min_repeats_per_case: every case carries at least 5 score(s)
ok   prereg.artifact[cassette]: sealed by digest (sha256:afc4901d87e70...)
```

Each clause is one named check. Every declared clause is reported whether it passed or
failed, for the reason `policy.py` already gives: a gate that speaks only on failure
cannot be told apart from a gate that checked nothing.

## What this does and does not establish

**It establishes:** the published receipt matches an evaluation contract that was
declared, and committed, before the number existed. If the contract was committed to a
repository someone else can read, the declaration has a timestamp EvalSeal did not
write - a commit in a shared history.

**It does not establish:** that this was the only time the suite ran. A
pre-registration cannot prove that no hidden runs happened, and no local tool can. A
determined author can pre-register, run privately until the result is good, and publish
the run that matched the contract. Every clause above still holds for that receipt.

What it costs them is optionality. They cannot narrow the dataset, shorten the runs,
swap the judge or relax the threshold in response to what they saw, because all of
those were fixed by digest beforehand. The remaining move - re-rolling the same
declared evaluation until sampling noise favours them - is exactly what
`evalseal power` is for: a suite powered for the effect it claims to detect does not
swing on a re-roll, and per-case flip rates make an unstable suite visible.

## The signature clause, and the three claims inside it

`require_signature` asks a question with more than one part, and the clause reports
which parts it established:

| claim | question | how it fails |
|---|---|---|
| **presence** | is there a signature at all? | *"no signature: …sig.jsonl is absent or empty"* |
| **validity** | does it verify, and does the record still match the hash that was signed? | *"does not verify"* / *"does not match its own sealed hash"* |
| **coverage** | does it cover **this** receipt? | *"the signature does not cover record 1: signed records are [0]"* |
| **identity** | is the signer one you trust? | *"not one of the trusted keys you supplied"* |

Coverage is the one most easily missed. `evalseal sign` signs the ledger **head**, so
signing and then appending leaves the new receipt unsigned while the sidecar still
holds a perfectly valid signature:

```console
$ evalseal verify --ledger l.jsonl --signed
1 signature(s) valid; records 0-0 of 2 are signed.      # exit 0

$ evalseal gate --ledger l.jsonl --prereg prereg.json
FAIL prereg.require_signature: the signature does not cover record 1: signed records
are [0]. A signature over an earlier prefix of the ledger says nothing about a receipt
appended afterwards
```

Both are correct. `verify --signed` reports that the signatures it has are valid; the
gate reports that none of them covers the receipt being gated.

Identity is the other. A signature is verified with the key stored beside it, so
without a trust anchor it establishes that *someone* signed:

```bash
evalseal gate --ledger l.jsonl --prereg prereg.json --trusted-key ci-signer.pub
```

Declare the keys with `--trusted-key` (repeatable, which is how rotation works) or
`trusted_public_keys` in the contract. The flag wins, because the verifier's trust
should not be decided by the file they are verifying. With none declared the clause
passes on presence, validity and coverage and says plainly *"identity not checked"* -
it does not imply a trusted signer.

**What a signature still does not establish:** that the eval ran, that the inputs were
honest, when any of it happened, or that no other run was discarded. It binds bytes to
a key.

## The CI clause, in particular

`require_ci_claim` deserves its own warning, because it is the clause most likely to be
over-read. It is named `..._claim` rather than `require_ci` on purpose: a rule whose name
promises proof will be read as proof no matter what the docs say underneath it.

Schema 1.5 seals what the environment *claimed* about the CI job: provider, run id, run
URL, repository, ref, commit, event. The field is called `claimed` for a reason. Every
value is an environment variable, and `GITHUB_ACTIONS=true evalseal run` produces a
receipt that claims CI from a laptop. The check's own output says so:

```console
ok   prereg.require_ci_claim: the receipt claims it was produced in CI (github_actions,
     https://github.com/owner/repo/actions/runs/42). This is the environment's own
     claim: check the run URL, and sign the ledger with a key only CI holds
```

Two things make it evidence rather than a claim, and both live outside the receipt:

1. **The run URL resolves.** A reviewer can open it and see a job that ran this suite.
2. **The ledger is signed by a key only CI holds.** If the signing key lives in CI
   secrets and not on developer machines, a locally produced receipt cannot carry a
   valid signature. This is the part that actually moves receipt production off the
   machine of the person being measured, and it is a property of your key management,
   not of EvalSeal.

Only a fixed allowlist of variables is read - named one by one in `provenance.py`,
never by prefix - so `GITHUB_TOKEN` cannot be swept into a record by a rule like
"record every GITHUB_\* variable".

## Honest summary

A pre-registration moves the claim from

> *trust this number*

to

> *this number came from the evaluation that was declared, and here is the declaration.*

That is a real improvement and a bounded one. Anyone who tells you a local tool can
prove the absence of hidden runs is selling something; see
[THREAT_MODEL.md](../THREAT_MODEL.md) for the full list of what receipts do and do not
establish.
