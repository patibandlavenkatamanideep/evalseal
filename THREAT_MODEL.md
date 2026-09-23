# Threat model

What a receipt proves, what it does not, and who has to be trusted for each claim.

The short version: **EvalSeal proves integrity, provenance and comparability. It does not
prove truth.** A sealed receipt of a run that never happened seals just as cleanly as one
of a run that did. Everything below is an attempt to say exactly where that line falls.

## What EvalSeal proves

| claim | mechanism | who you must trust |
|---|---|---|
| this receipt has not been edited since it was sealed | SHA-256 over the record's content | nobody |
| this record is the one that was in the ledger, in this position | hash chain, each record committing to the previous | nobody |
| these files are the ones the run used | artifact digests sealed in the receipt | nobody |
| this receipt came from the holder of this key | Ed25519 signature over the ledger head | that the private key was not shared or stolen |
| these two runs were graded the same way | evaluator fingerprint scheme 2 | that the fingerprint covers what matters, which this file lists |
| the score moved by more than noise | paired test over the same items | the statistics, which are in `analyze.py` and `paired.py` |
| this anchor existed before time T | an external proof, when one is attached | the attesting party |

## What EvalSeal does not prove

- **That the eval ran at all.** Every number comes from a cassette or a live call that
  EvalSeal itself made, but a receipt is a record of bytes, not of events. Someone able
  to write a cassette can seal a receipt for an eval that never happened.
- **That the provider served the model it named.** `served_model` is whatever the
  response said. A provider that silently routes to a different model produces a receipt
  that faithfully records the lie.
- **That the inputs were honest.** A rubric that grades nonsense, a dataset with wrong
  answers, a judge prompt designed to pass everything: all seal cleanly.
- **When anything happened.** See "if the machine lies about time" below.
- **That the model is good.** A receipt measures reproducibility and provenance. It says
  nothing about whether the task matters or the scoring is sensible.

## What each layer protects

### Ledger hashes

Each record's SHA-256 covers its whole content plus the previous record's hash. Editing a
past score breaks that record's hash; re-hashing it breaks the next record's link.
`evalseal verify` recomputes all of it.

**Protects against:** a single edited record, a reordered ledger, a silently dropped
record.
**Does not protect against:** someone rewriting the whole file. A complete rebuild
produces a valid chain. That is what signing is for.

### Signing

`evalseal sign` signs the ledger head with Ed25519. The head commits to every earlier
record through the chain, so one signature covers the ledger up to that point.

**Protects against:** anyone without the private key altering the ledger undetectably.
**Does not protect against:** the key holder. They can rebuild the ledger and re-sign it.
A verifier who only ever sees the new signature cannot tell. It also says nothing about
*when* the signature was made - `signed_at` is the signer's own clock.

### Anchoring

An anchor binds the receipt, the ledger head, the signature and the artifact digests into
one file a third party can re-check. Today's anchors are **local**: `created_at` is the
anchoring machine's clock and `external_proofs` is empty.

**A local anchor protects against:** a receipt being swapped for a different one, or an
artifact changing, between you and whoever you hand it to.
**It does not protect against:** backdating, or the key holder re-anchoring a rebuilt
ledger. Both need an external attestation, which is designed in
[docs/external-anchoring.md](docs/external-anchoring.md) and not yet implemented against
a real service.

### Artifact digests

The receipt seals digests of the cassette, dataset and suite. `evalseal verify
--artifacts` re-hashes them.

**Protects against:** responses being swapped after the fact, a dataset changing under a
saved score.
**Does not protect against:** an artifact that no longer exists. A digest can only be
matched against a file someone kept, which is why anyone who may need to prove what a
model said must retain the raw artifacts themselves, securely, outside EvalSeal.

## Specific questions

### What happens if the judge changes?

The evaluator fingerprint changes, and every comparison against an older run is reported
`non_comparable` before any score is shown. `evalseal drift` labels it `evaluator_drift`
and states in words that none of the movement can be attributed to the model under test.

Scheme 2 covers the scorer type and its settings, the judge's provider, endpoint, model,
temperature, top_p, max_tokens and seed, the rubric hash, the prompt template hash, the
dataset hash and the exact set of case ids. A change to any of those makes runs
non-comparable.

**What it still misses:** anything the provider changes behind a stable model name. A
judge model silently updated server-side has the same fingerprint and different
behaviour. That is the case `evalseal drift` exists to make visible: identical
fingerprint, moved verdicts.

### What happens if the dataset changes?

The dataset hash covers the file's bytes and the case-set hash covers the ids actually
scored, so both a changed file and a changed subset make runs non-comparable. Cases
present in only one run are listed and excluded from the paired test rather than silently
compared.

### What happens if two CI jobs write concurrently?

The ledger head is read and the record appended inside one file lock, so two runs cannot
create sibling records claiming the same predecessor. `verify` reports siblings
explicitly if a ledger acquires them another way.

This is `fcntl` on POSIX and `msvcrt` on Windows: **one machine**. On NFS or SMB the
guarantee weakens or disappears, and two runners on different machines sharing a ledger
over a network filesystem are outside what this protects.

### What happens if the machine lies about time?

Every timestamp in a receipt, a signature and a local anchor comes from the machine that
wrote it. A backdated clock produces a backdated receipt, and nothing in the current
implementation detects it. An external anchor is the only fix, and it is future work.

Treat `created_at`, `started_at` and `signed_at` as claims by the author, not as evidence.

### What happens if the target model provider silently changes behaviour?

Nothing in the fingerprint changes, because nothing the receipt can see changed. The
provenance records `served_model` and the provider's `system_fingerprint` when one is
returned, and a run warns if either moves mid-run, but a provider that changes behaviour
without changing those is invisible to a single receipt.

What does show it: running the suite again and comparing. Verdicts that move with an
identical evaluator fingerprint are exactly the signal, and `evalseal drift` reports
them. Whether that movement is the provider or ordinary sampling variance cannot be
separated from two receipts alone.

### What requires retaining the original private artifacts?

Any dispute about *content* rather than identity. A hash proves that a disclosed file is
the one that was sealed; it cannot reconstruct a file nobody kept. If you may need to
show what a model actually said, keep the cassette.

## Privacy posture

- **Hashes by default.** Receipts store digests of the dataset, rubric and judge prompt
  template, plus case ids, scores and verdicts. They do not store prompts or responses.
- **Raw content only behind an explicit flag.** `--store-judge-prompt` seals the judge
  prompt template verbatim. Since schema 1.3 that template carries no case text, but it
  does carry the rubric, so it stays opt-in.
- **Cassettes are the exception, and they are not receipts.** A cassette holds full
  request bodies and response envelopes in plain text. It never holds an API key, which
  is sent in a header and never recorded. Committing a cassette publishes the prompts and
  responses in it: fine for public benchmark data, not fine for a private dataset.
- **HTML receipts and CI artifacts carry hashes and scores only**, so a receipt can be
  attached to a pull request without leaking the dataset.

## Reporting a weakness

If you find a way to make EvalSeal vouch for something it should not, please report it
through [SECURITY.md](SECURITY.md). A receipt that overclaims is a bug of the most
serious kind this project has.
