# External anchoring

A reasonable question to ask of any receipt: *if the person who produced it wanted to lie,
what would stop them?* This page answers it for EvalSeal as it stands, says exactly what
an external anchor would add, and lays out a design for it that a contributor can build.

The short answer: today, a signed receipt stops anyone **except the key holder** from
altering it undetected. It does not stop the key holder, and it does not say when anything
happened. External anchoring closes the second gap and narrows the first. Nothing closes
it entirely, and nothing here claims to.

## What exists today

### The hash chain

Each sealed record's SHA-256 covers its whole content plus the previous record's hash, so
editing a past record breaks its hash, and re-hashing it breaks the next record's link.
`evalseal verify` recomputes all of it. Anyone who can rewrite the *whole* ledger can still
build a new, internally consistent chain.

### Signatures

`evalseal sign` signs the ledger head with an Ed25519 key. The signed message is the head
record's hash, and the head commits to every earlier record through the chain, so one
signature covers the whole ledger up to that point. The signature is appended to
`<ledger>.sig.jsonl` with the record index, the public key, and a `signed_at` time.

**What a signature proves.** Holding the public key, a third party can confirm that the
holder of the matching private key signed a ledger whose head was H, and that the records
in front of them hash to H. Nobody without the key can alter those records afterwards
without the signature failing.

**What a signature does not prove.**

- **An independent time.** `signed_at` is the signer's own clock. A signer can backdate it,
  and nothing in the signature constrains it.
- **External custody.** The signature and the ledger sit wherever their owner keeps them.
  Nothing shows that the signed state was ever seen by anyone else.
- **Truthful inputs.** A signer can sign a record of a run that never happened, or seal a
  dataset hash for a dataset they then swap. The signature binds bytes to a key, not bytes
  to reality.
- **That the eval ran when claimed.** Nothing in a signature or a record fixes when the
  model was called.
- **That the key holder did not re-sign.** The key holder can rebuild a ledger and sign the
  new head. The old signature then no longer matches, but a verifier who only ever sees
  the new one cannot tell.
- **That private raw artifacts still exist.** A receipt stores hashes of the dataset,
  rubric and judge prompt template, not the text. If the text is discarded, the hash can
  no longer be matched against anything.

### The local anchor

`evalseal anchor` (added alongside this document) writes one JSON file binding a receipt
to the ledger it was sealed into and to that ledger's signature state:

```bash
evalseal anchor report.json --ledger .evalseal/ledger.jsonl --out anchor.json
evalseal anchor-verify anchor.json report.json --ledger .evalseal/ledger.jsonl
```

It records the receipt's content hash, the ledger head and length at anchoring time, the
receipt's position in the ledger, the signature (bytes, public key and its fingerprint)
when the ledger is signed, hashes of any extra artifacts, and a `subject_digest` over all
of that. `anchor-verify` re-checks every claim and reports anything it could not check as
not checked, never as passed.

A local anchor makes a receipt **portable**: a third party with the anchor and the receipt
can confirm, without the author's machine, that this is the receipt that was sealed and,
if it was signed, which key signed it. It adds **no independent time and no external
custody**. Its `created_at` is the anchoring machine's clock, and its `external_proof` is
`null`. The rest of this page is about filling that field.

`subject_digest` deserves one precise sentence, because it is easy to over-read: it
detects accidental edits and gives an external proof a single value to attest, but anyone
can recompute it after editing the anchor. On its own it is not tamper evidence. The
signature is, and an external proof would be.

## What an external anchor adds

An external anchor is an attestation, by a party other than the author, that a specific
digest existed at or before a specific time. Applied to `subject_digest`, it establishes:

- **A time the author cannot move.** The anchored state existed no later than the
  attested time. Backdating becomes impossible; the author can still anchor late.
- **A commitment the author cannot silently replace.** Once a digest is attested, a
  rebuilt ledger with a different head produces a different digest, and the original
  attestation no longer matches it. For a public log, anyone can see that the earlier
  digest was committed.

It still does not establish that the eval ran as described, that the provider behaved
truthfully, or that the inputs were honest. An attested lie is an attested lie.

## The anchor object

This is the local anchor's shape, with `external_proof` as the extension point. Fields
marked *today* are written by `evalseal anchor` now.

| field | meaning | today |
|---|---|---|
| `anchor_version`, `kind` | format identity: `1`, `evalseal.local-anchor` | yes |
| `evalseal_version` | the version that wrote the anchor | yes |
| `receipt_hash` | the receipt's canonical content hash: SHA-256 over its sorted-key JSON, excluding `hash` itself - the value the ledger uses | yes |
| `receipt_schema_version`, `receipt_evalseal_version` | what sealed the receipt | yes |
| `sealed.evaluator_fingerprint`, `sealed.config_fingerprint` | comparability and exact-configuration identity | yes |
| `sealed.dataset_hash` | SHA-256 of the dataset file | yes |
| `sealed.case_set_hash` | SHA-256 of the sorted ids of the cases that ran | yes |
| `sealed.rubric_hash`, `sealed.judge_prompt_hash`, `sealed.judge_model` | how the run was graded | yes |
| `ledger.head_hash`, `ledger.length`, `ledger.receipt_index` | the ledger state at anchoring | yes |
| `signature_present`, `public_key_fingerprint` | SHA-256 of the raw 32-byte Ed25519 key | yes |
| `signature` | index, signed hash, algorithm, public key, signature bytes | yes |
| `artifact_hashes` | SHA-256 of the receipt file, the ledger, the signatures file, and anything passed with `--artifact` | yes |
| `subject_digest` | SHA-256 over all of the above, excluding `created_at`, `notes`, `external_proof` | yes |
| `created_at` | the anchoring machine's clock | yes |
| `external_proof` | one or more independent attestations of `subject_digest` | `null` |

Everything except `created_at` is a pure function of the inputs, so the same receipt and
ledger state produce the same `subject_digest` - the property that lets two parties
compare anchors, and the value an external service attests.

## Adapter-first design

External anchoring should be a set of small adapters over one interface, not one
hard-wired service. Different teams trust different authorities, and a tool that picks
one for everybody has quietly made itself part of the trust root.

```python
class AnchorAdapter(Protocol):
    name: str                                   # "rfc3161", "rekor", "ots", "file"

    def attest(self, subject_digest: str) -> dict:
        """Obtain a proof that subject_digest existed. Returns one external_proof entry."""

    def verify(self, subject_digest: str, proof: dict) -> AdapterResult:
        """Check a proof offline where the format allows, and say what it established."""
```

`external_proof` becomes a list, so one anchor can carry several attestations:

```json
{
  "adapter": "rfc3161",
  "digest_algorithm": "sha256",
  "subject_digest": "sha256:…",
  "attested_time": "2026-10-01T12:00:00Z",
  "authority": "https://timestamp.example",
  "proof": "<base64 of the adapter's native proof>",
  "established": "existence of subject_digest no later than attested_time, per the authority"
}
```

Every adapter's `verify` must say in `established` what the proof shows *and whose word it
rests on*. Only the digest is sent to any service, never the anchor, the receipt, or
anything they hash.

### RFC 3161 timestamp authority

The client sends the digest in a `TimeStampReq`; the authority returns a signed
`TimeStampToken` binding the digest to a time. Verification checks the token's signature
against the authority's certificate chain, which can be stored with the proof, so it works
offline.

- **Establishes:** the digest existed no later than the stated time, on the authority's
  word.
- **Trust rests on:** the authority and its certificate chain. Anchoring with two
  unrelated authorities reduces reliance on either.
- **Privacy:** the authority sees a digest and a request time, nothing else.

### Sigstore / Rekor transparency log

The digest, a signature over it and the public key are submitted as a log entry. Rekor
returns a signed entry timestamp and an inclusion proof against a signed checkpoint of an
append-only Merkle tree that anyone can monitor.

- **Establishes:** the entry was integrated into a public, append-only log at the stated
  time, and it stays visible to anyone who audits the log.
- **Trust rests on:** the log operator's signing key, and on the log being monitored for
  split views. The inclusion proof and signed checkpoint verify offline.
- **Privacy:** the entry is **public**. It reveals that a given key committed to a digest
  at a given time. The digest reveals nothing about content that cannot be guessed, but
  a digest of low-entropy content can be confirmed by guessing, so anchor the
  `subject_digest`, never a hash of a short value like a single answer.

### OpenTimestamps-style anchoring

The digest is aggregated with others into a Merkle tree whose root is committed to the
Bitcoin blockchain. The proof starts out pending and becomes complete once the block
confirms, typically hours later.

- **Establishes:** the digest existed no later than the block's time.
- **Trust rests on:** Bitcoin block headers, which the verifier has to obtain from a
  source they trust. No single timestamping party.
- **Privacy:** calendar servers see the digest; the chain sees only a Merkle root.
- **Caveat:** block time has a tolerance of hours, which is coarse for some disputes.

### A local proof file

For teams that anchor through their own process - a notary, an internal records system -
an adapter that stores a proof file produced elsewhere, with `established` stating
plainly that EvalSeal did not verify it. This keeps the object usable without pretending
to a verification it cannot do.

### What EvalSeal will not do

Run its own timestamp service, transparency log or verification server. The value of an
external anchor is that it rests on someone other than the party being audited. An
EvalSeal-operated authority would make this project a trust root it has no standing to
be, and a receipt that can only be verified by asking its author is not a receipt.

## Privacy

- **Hashes by default.** A receipt stores hashes of the dataset file, the rubric and the
  judge prompt template, and case ids, never prompts or responses. The anchor stores only
  hashes and non-secret identifiers such as model names. An external service receives
  only `subject_digest`.
- **Later disclosure is matched against sealed hashes.** In a dispute, the dataset file
  can be produced and hashed against `sealed.dataset_hash`, the rubric against
  `sealed.rubric_hash`, and so on. A match shows the artifact is the one that was sealed.
- **Responses are not in the receipt yet.** The model's responses live in the cassette,
  and a receipt does not currently seal the cassette's hash. Until it does, bind the
  cassette to an anchor explicitly: `evalseal anchor report.json --artifact
  tests/cassettes/run.json`. Sealing a cassette digest into the record is on the
  [roadmap](../ROADMAP.md).
- **Semantic disputes need the artifacts themselves.** A hash proves identity, not
  content: it can show that a disclosed response is the one that was recorded, but only if
  someone kept it. Anyone who may need to prove what a model said must retain the raw
  artifacts - the cassette, the dataset, the rubric - securely and outside EvalSeal.
  EvalSeal keeps what makes them checkable, not the things themselves.

## Building it

For a contributor picking up v2.2:

1. Turn `external_proof` into a list and define `AnchorAdapter` in `anchor.py`, with a
   registry keyed by name.
2. `evalseal anchor --with rfc3161 --tsa URL` calls `attest(subject_digest)` and appends
   the result. Network access happens only when an adapter is named.
3. `anchor-verify` calls each adapter's `verify`, reports its `established` text, and
   treats a proof it cannot check as not checked rather than passed.
4. Adapters with non-trivial dependencies ship as extras (`evalseal[rfc3161]`,
   `evalseal[rekor]`), so the base install stays small.
5. Tests use recorded service responses, like every other network path in EvalSeal, so
   CI verifies proofs without calling any service.
