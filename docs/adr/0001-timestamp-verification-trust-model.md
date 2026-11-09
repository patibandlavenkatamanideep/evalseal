# ADR 0001 — What a verified timestamp token means here

- **Status:** accepted, with one open decision (§7) that needs a maintainer call
- **Date:** 2026-11-09
- **Context:** `evalseal anchor --with rfc3161` stores an RFC 3161 token; `anchor-verify`
  and `gate --require-external-anchor` read it back. Before this ADR, "what exactly
  does a verified token establish" was answered in prose scattered across
  `rfc3161.py`, `THREAT_MODEL.md` and `docs/external-anchoring.md`, and the code did
  not always match the prose.

## 1. The claim a token is allowed to support

A verified token supports exactly one sentence:

> This authority asserts that **this digest** existed no later than **this time**, and
> the assertion is signed by a certificate that chains to a root **the verifier
> supplied**.

It does not support, and the code must never phrase it as supporting:

- that the evaluation ran, or ran as described
- that the digest is of honest inputs
- that no other run happened before or after
- that the time is *correct* — only that the authority asserted it. A compromised or
  dishonest authority can assert any time; that is the trust being placed in it
- **freshness.** A token proves a digest existed *no later than* `genTime`. It says
  nothing about how recently it was obtained. Only a nonce echoed in a response we
  just requested establishes that the response is not a replay, and only for that
  submission — never for a token read back from a file.

## 2. Trust model: direct trusted issuer, not path validation

**Decision: one hop.** The verifier supplies the issuing certificate (`--tsa-ca`), and
the token's signing certificate must be issued by it and verify under its public key.

EvalSeal does **not** do certificate path building: no intermediate discovery, no AIA
fetching, no name-constraint or policy-mapping evaluation, no cross-certificate
handling. A token whose signer is two hops from the supplied root fails, and the
failure says so rather than reporting an unverified signature.

Rejected: full RFC 5280 path validation. Implementing it correctly is a large piece of
security-critical work, and implementing it *incorrectly* is worse than not offering it,
because the result would carry a stronger label than it earns. Anyone needing it has
`openssl ts -verify` with a real trust store, which §6 keeps as the independent check.

## 3. Root provenance: the verifier's, never the file's

The trust root is read only from what the verifier passes on the command line for that
invocation. It is never read from the anchor, never cached between invocations, and
never defaulted to a system trust store.

A root carried inside the anchor would be a root the *author* chose, which defeats the
point of an external attestation. A root defaulted from the system store would silently
accept any of hundreds of CAs for an assertion the verifier never considered. A root
cached across invocations was an actual defect: `register_backend()` installed it
process-wide, so a root supplied for one verification decided later ones.

## 4. Revocation: not checked, and therefore not claimed

**No OCSP. No CRL.** A certificate revoked after signing verifies here.

This is a real limitation and it is stated in every place the result is reported, rather
than omitted. A revoked signing key means the authority's assertions can no longer be
trusted, and nothing in an offline check can discover that. Callers who need it should
run the §6 cross-check with a revocation-aware verifier.

Not claiming it is the point: a token whose verdict says `verified` here means §1's
sentence, which does not include "and the signer was still trusted at verification
time".

## 5. Algorithms and offline operation

Supported and enumerated in code, not inferred:

| role | supported |
|---|---|
| message imprint digest | SHA-256, SHA-384, SHA-512 |
| CMS `digestAlgorithm` | SHA-256, SHA-384, SHA-512 |
| signature | RSA PKCS#1 v1.5, ECDSA |

The imprint's algorithm OID and the digest length are validated **together**: an OID
naming SHA-512 beside a 32-byte imprint is rejected, because accepting it would let a
token about one digest be read as a token about another. There is no implicit default:
an absent or unrecognised algorithm fails closed, and the failure names the OID.

An earlier version hardcoded SHA-256 with RSA and failed on the first real authority it
met, because freetsa.org signs with ECDSA and SHA-512. The algorithms above are read
from the token and checked against this list; the list is the contract.

**Verification is entirely offline.** `verify_token` opens no socket. The only network
operation in the module is `submit`, which is the producer's act, not the verifier's.

## 6. Independent cross-check

A second implementation agreeing is the only evidence that this one reads real tokens
correctly. Synthetic fixtures prove the negative paths; they prove nothing about
interoperability, because the same misunderstanding that produced the parser produced
the fixture.

So: `openssl ts -verify` is run against the recorded corpus in the test suite where
OpenSSL is available, and must agree. This is a **test-time** check — never a runtime
dependency, and never a fallback that would make a verdict depend on whether a binary
happened to be installed.

When OpenSSL is absent the cross-check skips and says so. A skipped cross-check is
reported as skipped, not as agreement.

It has already paid for itself: OpenSSL rejected a token this parser accepted, because
the parser did not require the RFC 5035 signing-certificate attribute (§8). That gap
was invisible to every synthetic test, for exactly the reason above.

Two differences are known and deliberate, and are pinned by tests so they stay visible:

- **EvalSeal is stricter about the signed `contentType`.** RFC 5652 §5.3 makes its
  value a MUST-match against `eContentType`; `openssl ts -verify` accepts a mismatch.
  EvalSeal refuses, and does not relax to match.
- **Certificate validity is judged at different instants.** EvalSeal checks `genTime`
  against the certificate's validity window; OpenSSL checks the window against the
  clock now. For an assertion about the past, `genTime` is the right instant, so a
  token whose certificate has since expired still verifies here and does not under
  OpenSSL. The cross-check builds its own fixtures around the current clock so this
  difference does not masquerade as a disagreement about a token.

## 7. Open decision: a standards-aware parser (needs a maintainer call)

The CMS and TSTInfo structures are currently parsed by about 400 lines of hand-written
DER walking in `rfc3161.py`. That code has now produced three interoperability defects:

1. a hardcoded SHA-256/RSA assumption, found only by a real authority
2. `_integer()` stripping the leading zero byte that keeps a high-bit value positive,
   so `255` encoded as `-1` and **492 of 1000** random 64-bit nonces encoded as a
   different, negative integer — about half of all real requests malformed
3. certificate selection by set order rather than by matching the signer identifier —
   and the recorded real token embeds **two** certificates, so this was live against
   the one real authority in the repository
4. no RFC 5035 signing-certificate check at all, found by the §6 cross-check rather
   than by any test written here

Each was invisible to the existing tests because the tests were built from the same
understanding as the code.

**Evaluation**

| option | pros | cons |
|---|---|---|
| keep hand-rolled | no new dependency | every strictness rule in §8 is ours to get right; the three defects above are the track record |
| `asn1crypto` | pure Python, BSD, maintained, has first-class `tsp` and `cms` modules; length, tag, cardinality and trailing-data rules come for free; deletes ~400 lines | one new required runtime dependency |
| `cryptography` alone | already a dependency | no TSP support and no general CMS parsing; cannot do this |
| `openssl` subprocess at runtime | reuses a battle-tested implementation | makes a verdict depend on an external binary and its version; argv/timeout/temp-file handling becomes security-relevant; rejected for runtime use, kept for §6 |

**Recommendation: adopt `asn1crypto` for parsing**, keep `cryptography` for signature
and X.509 work, and delete the hand-written CMS parser. It is pure Python with no native
build, so it does not complicate installation, and most of §8's rules stop being our
code to maintain.

**Not done in this change.** It adds a required runtime dependency, which is the
maintainer's decision and not one to slip into a defect-fixing commit. The defects in §8
are fixed in the existing parser now, because the request *builder* and the verdict
*composition* stay ours either way, and a parser swap on top of unfixed defects would be
unreviewable. Recorded here so the decision is visible rather than deferred by silence.

## 8. Parser profile

What this parser accepts is declared, so "malformed" has a meaning:

- DER, not BER: definite lengths only, minimal length encoding, no indefinite-length
  constructions
- a length that overruns its container is an error, never a truncation
- trailing material after a structure's declared end is an error, not ignored
- `signedAttrs` must contain exactly one `contentType` and exactly one `messageDigest`;
  zero or several of either is an error, not a first-match
- `contentType` must be `id-ct-TSTInfo`; `eContentType` must match it
- the signer certificate is selected by matching the `SignerIdentifier` against the
  certificate set, never by position in that set
- the signature covers `signedAttrs` re-encoded as `SET OF` (`0x31`), not the `[0]
  IMPLICIT` tag (`0xA0`) the structure carries on the wire
- the SignerInfo must carry an RFC 5035 `signingCertificate` or `signingCertificateV2`
  signed attribute, and its hash must be the certificate the signature was checked
  against. This is **required**, not checked-only-if-present: `SignerIdentifier` sits
  outside `signedAttrs` and so is not covered by the signature, meaning without this
  attribute nothing signed says which certificate signed the token. `openssl ts
  -verify` refuses a token without it, and the recorded freetsa token carries the v1
  form, so requiring it is what a real authority already does
- the signing certificate must carry a **critical** `id-kp-timeStamping` EKU, alone;
  `genTime` must fall inside that certificate's validity, checked against `genTime`
  rather than the clock at verification time — the question is whether the certificate
  was valid *when the authority signed*, which is the right one for an assertion about
  the past

Anything outside this profile fails closed and the failure names the rule.

## 9. Consequences

- `verified` is a narrow, stated claim (§1), reachable only with a verifier-supplied
  root (§3). Without one the result is `imprint_only` and cannot satisfy a required
  gate clause.
- Revocation and path validation are permanent, documented gaps (§2, §4) rather than
  silent ones.
- A cryptographically valid signature alone does not make the token's verdict
  `verified`: the imprint, the algorithms, the attributes, the EKU and `genTime` are all
  separate checks, and the verdict is the conjunction.
- §7 stays open and visible until the maintainer decides.
