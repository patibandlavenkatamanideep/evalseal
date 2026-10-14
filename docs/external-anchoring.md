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

### RFC 3161 timestamp authority — **implemented**

> The boundary is now one of three sentences, depending on what the verifier was given:
>
> | supplied | reported |
> |---|---|
> | `--tsa-ca issuer.pem` | *signature verified against the certificate you supplied; revocation not checked* |
> | nothing | *message imprint checked, TSA signature NOT verified by EvalSeal* |
> | a failing token | *verification FAILED*, naming the checks that failed |
>
> Revocation is **never** checked, and that is named in the verified sentence rather
> than left out, because a verifier silent about what it skipped is read as having
> skipped nothing.

This one exists now, as the `rfc3161` backend:

```bash
evalseal anchor report.json --ledger .evalseal/ledger.jsonl \
  --with rfc3161 --tsa https://timestamp.example/tsr --out anchor.json
evalseal anchor-verify anchor.json report.json --ledger .evalseal/ledger.jsonl
```

The client sends the subject digest in a real DER `TimeStampReq`; the authority returns
a signed `TimeStampToken` binding the digest to a time, which is stored base64 in the
anchor's `external_proofs`.

**There is no default authority.** `--tsa` is required. A default would make whichever
service EvalSeal picked part of this project's trust root, and would make `anchor` a
command that contacts a third party without being asked.

**What verification does, exactly.** `anchor-verify` re-reads the stored token offline
and checks two things: that the token's `messageImprint` is *this anchor's subject
digest*, so the token is about this receipt and not another one, and what time the
authority asserts in `genTime`.

**What verification checks, with a root.** The signature over the re-tagged
`signedAttrs` (RFC 5652 requires `0xA0 → 0x31`, and getting it wrong is the classic way
a CMS verifier fails on every real token), the `messageDigest` attribute against
`eContent`, that `eContentType` is `id-ct-TSTInfo`, that the signing certificate carries
a **critical** `id-kp-timeStamping` EKU, that `genTime` falls inside that certificate's
validity, and that the certificate was issued by the root you supplied.

**No new dependency.** The CMS navigation is hand-rolled on the TLV walker already in
`rfc3161.py`; `cryptography` — already a dependency, maintained, audited — does every
cryptographic operation and all certificate parsing. OIDs are compared as encoded bytes,
which is exact and needs no OID decoder.

**What it still does not do:** revocation (no OCSP, no CRL — a certificate revoked after
signing verifies here), path building (one hop: supply the issuer directly), or choosing
a root for you. The root is never read from the proof, since a root the author chose
would defeat the point of an external attestation. `openssl` remains a fine independent
second opinion:

```bash
openssl ts -verify -digest <subject-digest-hex> -in token.tsr -CAfile <tsa-ca.pem>
```

So with a root supplied, a stored token establishes that **this authority signed for
this digest at this time** — resting on the authority's honesty and on the root being
the right one, not on EvalSeal. Without a root it establishes only that a token about
this digest exists.

- **Establishes:** the digest existed no later than the stated time, on the authority's
  word, once the signature is checked against a root you trust.
- **Trust rests on:** the authority and its certificate chain. Anchoring with two
  unrelated authorities reduces reliance on either.
- **Privacy:** the authority sees a 32-byte digest and a request time, nothing else. A
  test asserts the request body is under 128 bytes, so a receipt cannot leak through it.

### Choosing between them

The three designs differ on more than convenience, so the trade-offs are worth seeing
side by side before reaching for one:

| | RFC 3161 | Rekor | OpenTimestamps |
|---|---|---|---|
| **status in EvalSeal** | implemented | not implemented | not implemented |
| **establishes** | this authority signed for this digest at this time | this entry was integrated into a public append-only log at this time | this digest existed before this Bitcoin block |
| **trust rests on** | the authority, and the root you chose | the log operator's key, and the log being monitored for split views | Bitcoin block headers, from a source you trust |
| **single party?** | yes - mitigate by anchoring with two unrelated authorities | yes, the log operator | no |
| **who sees the digest** | the authority only | **everyone, permanently** | calendar servers; the chain sees only a Merkle root |
| **time granularity** | seconds | seconds | hours (block time) |
| **proof ready** | immediately | immediately | pending until the block confirms, typically hours |
| **revocable** | n/a - nothing was published | **no** | no, but nothing identifying was published |
| **verifies offline** | yes, with the issuer certificate | yes, with the signed checkpoint | yes, with block headers |

**The default is RFC 3161**, because it is the only one of the three that publishes
nothing. Rekor should be an opt-in a user makes knowingly, for the reason in the next
section — and EvalSeal should never select it on someone's behalf.

### Sigstore / Rekor transparency log — not implemented

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

### OpenTimestamps-style anchoring — not implemented

The digest is aggregated with others into a Merkle tree whose root is committed to the
Bitcoin blockchain. The proof starts out pending and becomes complete once the block
confirms, typically hours later.

- **Establishes:** the digest existed no later than the block's time.
- **Trust rests on:** Bitcoin block headers, which the verifier has to obtain from a
  source they trust. No single timestamping party.
- **Privacy:** calendar servers see the digest; the chain sees only a Merkle root.
- **Caveat:** block time has a tolerance of hours, which is coarse for some disputes.

### A local proof file — not implemented

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
- **Responses live in the cassette, whose digest the receipt seals.** Since schema 1.4
  the cassette is bound into the record by digest and `evalseal verify --artifacts`
  re-hashes it, so a swapped response is a failed check rather than a silent difference.
  The responses themselves are not in the receipt, and committing a cassette publishes
  the prompts and replies in it: fine for public benchmark data, not fine for a private
  dataset.
- **Semantic disputes need the artifacts themselves.** A hash proves identity, not
  content: it can show that a disclosed response is the one that was recorded, but only if
  someone kept it. Anyone who may need to prove what a model said must retain the raw
  artifacts - the cassette, the dataset, the rubric - securely and outside EvalSeal.
  EvalSeal keeps what makes them checkable, not the things themselves.

### What a public transparency log reveals

The usual warning about digests is that a digest of low-entropy content can be confirmed
by guessing. True, and beside the point here: a `subject_digest` is a SHA-256 over an
anchor's whole content, so it is not guessable and reveals nothing about the eval.

The argument that actually matters is different. **A public log leaks cadence and
existence, not content.**

Publishing a digest to Rekor records, permanently and for anyone, that *some* commitment
was made at *this* time under *this* key. Nobody learns what was evaluated. They learn
that you were evaluating something, how often, and when you stopped. For a team running
evals before a launch, that pattern is competitive information in its own right - a
sudden burst of anchors, or a long gap, says something whether or not the digests ever
resolve.

Two consequences worth stating plainly:

- **It is irrevocable.** A transparency log is append-only by design; that is the
  property that makes it useful. There is no un-publishing, no correction, and no
  expiry. A digest published by mistake stays published.
- **The key is an identity.** Entries are signed, so a log is also a record of which key
  was active when. Rotating keys to avoid that correlation undermines the signature's
  value as continuity of custody.

So the posture:

- **RFC 3161 is the default.** The authority sees one 32-byte digest and a request
  time, and publishes nothing. A test asserts the request body stays under 128 bytes, so
  a receipt cannot leak through it.
- **Rekor is opt-in, and never selected for you.** When it is implemented, choosing it
  must be an explicit act by someone who has read this section - not a default, not a
  fallback, and not something `--with external` resolves to.
- **Anchor the `subject_digest`, never a hash of a short value.** The guessing attack
  that does not apply to an anchor digest applies immediately to a digest of a single
  answer.

## Building the rest of it

Steps 1 to 3 are done: `external_proofs` is a list, `AnchorBackend` lives in
`anchor.py` with a registry, `--with NAME` submits only when a backend is named, and
`anchor-verify` treats a proof it cannot check as not checked rather than passed.

What is left, for a contributor picking this up:

1. **Verify the RFC 3161 signature inside EvalSeal.** This is the gap that keeps the
   backend experimental. It needs CMS `SignedData` validation against the TSA
   certificate chain stored with the proof. `asn1crypto` or `pyasn1-modules` would do
   it; both would be a new dependency, which is the trade-off to weigh.
2. **A Rekor backend**, for teams who want a public log rather than a private
   authority. Note the privacy difference below before reaching for it.
3. **A local proof-file backend**, for teams anchoring through their own notary, whose
   `established` text says plainly that EvalSeal did not verify it.
4. Adapters with non-trivial dependencies should ship as extras
   (`evalseal[rfc3161]`, `evalseal[rekor]`), so the base install stays small.
5. Tests use synthetic DER built by the module's own encoder, so CI verifies proofs
   without calling any service. A recorded response from a real TSA would be better
   and needs one live run to capture.
