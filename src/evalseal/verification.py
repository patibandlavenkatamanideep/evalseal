"""Typed verification outcomes, so a gate cannot mistake one claim for another.

## Why this module exists

`require_signature` was a field on `Preregistration` that nothing read. A contract
could declare it, `gate --prereg` would pass an unsigned receipt, and the green tick
was read as evidence of a signature nobody had made. The field was reachable only by
hand-editing JSON, which is how a setting ends up declared and unread.

Wiring it to `verify_signatures()` and reading the boolean would have replaced one
false green with a subtler one, because that function answers a *different* question
than a gate needs:

- it returns `True` when every signature entry is internally valid, **whatever records
  they cover**. Sign record 0, append record 1, and it still returns `True` - with an
  honest message saying "records 0-0 of 2 are signed" that a boolean read discards. A
  gate selecting record 1 would pass a receipt nobody signed.
- it trusts whichever public key the producer wrote into the sidecar unless a caller
  passes `expected_public_key`. A valid signature by an unknown key establishes that
  *someone* signed, which is not the same as the signer you decided to trust.

So three claims are kept apart, and a requirement says which it established:

| claim | question | established by |
|---|---|---|
| **presence** | is there a signature at all? | a sidecar with at least one entry |
| **validity** | does it verify, over records the ledger holds? | Ed25519 over the hash |
| **coverage** | does it cover *this* receipt? | a valid entry at the selected index |
| **identity** | is the signer one we trust? | the key matching a verifier-supplied list |

None of them establishes that the eval ran, that the inputs were honest, or when any
of it happened. A signature binds bytes to a key.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import RunRecord

from .signing import (
    ALGORITHM,
    load_public_key,
    load_signatures,
    public_key_b64,
    public_key_fingerprint,
)


def _normalise(keys: list[str] | None) -> list[str] | None:
    """Trusted keys as canonical base64, accepting a path, PEM or raw base64.

    Returns None when no trust anchor was supplied, which is a different state from an
    empty list: the first means identity was not checked, the second means nothing is
    trusted and every signer must fail.
    """
    if keys is None:
        return None
    out = []
    for key in keys:
        try:
            out.append(public_key_b64(load_public_key(key)))
        except Exception:
            # An unparseable trusted key must not silently widen trust; keep it as
            # given so it simply matches nothing and the failure names it.
            out.append(key)
    return out


@dataclass(frozen=True)
class SignatureVerification:
    """What the signature state of one selected receipt actually supports."""

    present: bool = False
    valid: bool = False
    covers_selected: bool = False
    #: None when no trust anchor was supplied, so identity could not be checked.
    trusted: bool | None = None
    signer_fingerprints: tuple[str, ...] = ()
    covered_indexes: tuple[int, ...] = ()
    selected_index: int | None = None
    detail: str = "not checked: no signature state was supplied"

    @property
    def satisfies_requirement(self) -> bool:
        """Whether a `require_signature` clause may pass on this.

        Presence, validity and coverage are all required. Identity is required only
        when a trust anchor was supplied - `trusted is None` means it was not checked,
        and the detail says so rather than implying a trusted signer.
        """
        return (self.present and self.valid and self.covers_selected
                and self.trusted is not False)

    def to_dict(self) -> dict[str, object]:
        return {
            "present": self.present,
            "valid": self.valid,
            "covers_selected": self.covers_selected,
            "trusted": self.trusted,
            "signer_fingerprints": list(self.signer_fingerprints),
            "covered_indexes": list(self.covered_indexes),
            "selected_index": self.selected_index,
            "satisfies_requirement": self.satisfies_requirement,
            "detail": self.detail,
        }


def verify_receipt_signature(
    ledger: Path | str,
    record_index: int,
    *,
    trusted_keys: list[str] | None = None,
) -> SignatureVerification:
    """Check the signature state of one selected record, by index.

    `record_index` is resolved against the ledger, so -1 selects the head exactly as
    `gate --index` does. Every failure is reported as a state rather than raised: a
    requirement that cannot be evaluated has to fail visibly, not abort the gate.
    """
    from .ledger import load_all

    path = Path(ledger)
    try:
        records = load_all(path)
    except (OSError, ValueError) as e:
        return SignatureVerification(
            detail=f"the ledger could not be read, so no signature could be checked: {e}")
    if not records:
        return SignatureVerification(detail=f"{path} holds no records")

    if not -len(records) <= record_index < len(records):
        return SignatureVerification(
            selected_index=record_index,
            detail=f"record {record_index} is not in a ledger of {len(records)}")
    selected = record_index % len(records)

    try:
        entries = load_signatures(path)
    except (OSError, ValueError) as e:
        return SignatureVerification(
            selected_index=selected,
            detail=f"the signature sidecar could not be read: {e}")

    from .signing import signatures_path

    if not entries:
        return SignatureVerification(
            selected_index=selected,
            detail=f"no signature: {signatures_path(path)} is absent or empty. "
                   "Sign the ledger with `evalseal sign`")

    trusted = _normalise(trusted_keys)
    fingerprints: list[str] = []
    covered: list[int] = []
    problems: list[str] = []

    for position, entry in enumerate(entries):
        algorithm = entry.get("algorithm")
        if algorithm != ALGORITHM:
            problems.append(
                f"signature {position} uses unsupported algorithm {algorithm!r}; "
                f"this build verifies {ALGORITHM}")
            continue
        index = entry.get("record_index")
        if not isinstance(index, int) or not 0 <= index < len(records):
            problems.append(
                f"signature {position} claims record {index!r}, which the ledger lacks")
            continue
        if records[index].hash != entry.get("hash"):
            problems.append(
                f"signature {position} covers a hash the ledger no longer holds at "
                f"record {index}; the ledger was rewritten after signing")
            continue
        try:
            key = load_public_key(entry["public_key"])
            key.verify(base64.b64decode(entry["signature"]), entry["hash"].encode())
        except Exception:
            problems.append(f"signature {position} does not verify for record {index}")
            continue
        fingerprints.append(public_key_fingerprint(entry["public_key"]))
        covered.append(index)

    if not covered:
        return SignatureVerification(
            present=True, selected_index=selected,
            detail="no usable signature: " + "; ".join(problems))

    # A signature covers a *hash*. Whether the record's content still hashes to that
    # value is a different claim, and comparing the stored `hash` field against the
    # signature only compares two copies of the same number: edit a score, leave the
    # field, and the signature would still "cover" it. So the content is re-hashed.
    from .ledger import check_record_hash

    status, sealed_under = check_record_hash(records[selected])
    if status not in ("ok", "legacy"):
        return SignatureVerification(
            present=True, valid=False, selected_index=selected,
            signer_fingerprints=tuple(fingerprints),
            covered_indexes=tuple(sorted(set(covered))),
            detail=(
                f"record {selected} does not match its own sealed hash ({status}), so "
                "no signature over that hash says anything about this content. Run "
                "`evalseal verify` to see where the chain broke"))

    covers = selected in covered
    # Identity is judged on the signatures that actually cover the selected record,
    # not on any valid signature anywhere in the sidecar.
    covering_keys = [
        public_key_fingerprint(e["public_key"]) for e in entries
        if e.get("record_index") == selected and e.get("algorithm") == ALGORITHM
    ]
    is_trusted: bool | None = None
    if trusted is not None:
        wanted = {public_key_fingerprint(k) if not k.startswith("sha256:") else k
                  for k in trusted}
        is_trusted = bool(covering_keys) and all(f in wanted for f in covering_keys)

    if not covers:
        detail = (
            f"the signature does not cover record {selected}: signed records are "
            f"{sorted(set(covered))}. A signature over an earlier prefix of the ledger "
            "says nothing about a receipt appended afterwards")
    elif is_trusted is False:
        detail = (
            f"record {selected} is signed, but by {', '.join(covering_keys)}, which is "
            "not one of the trusted keys you supplied. A valid signature by an unknown "
            "key shows that someone signed, not that your signer did")
    elif is_trusted is True:
        detail = (f"record {selected} is signed by a trusted key "
                  f"({', '.join(covering_keys)}), and its content matches the hash "
                  f"that was signed (schema {sealed_under})")
    else:
        detail = (
            f"record {selected} carries a valid signature by {', '.join(covering_keys)}, "
            "but identity not checked: declare trusted_public_keys to require a "
            "particular signer")
    if problems:
        detail += f". Also, {len(problems)} unusable signature entry(ies): " + \
                  "; ".join(problems)

    return SignatureVerification(
        present=True,
        valid=True,
        covers_selected=covers,
        trusted=is_trusted,
        signer_fingerprints=tuple(fingerprints),
        covered_indexes=tuple(sorted(set(covered))),
        selected_index=selected,
        detail=detail,
    )


# --------------------------------------------------------------------------------------
# External evidence
# --------------------------------------------------------------------------------------
#
# `prereg.require_external_anchor` - the strongest clause a contract can declare - was
# evaluated as `bool(anchor["external_proofs"])`. That made it the easiest clause to
# satisfy in the whole contract. On the baseline it passed for:
#
#   [{"backend": "local", "proof": None}]   the backend whose own check() says
#                                          "no external party attested this"
#   [{}]                                   an empty object
#   [{"backend": "notarius-maximus"}]      a backend that does not exist
#   ["no"]                                 the string "no"
#
# Only a completely empty list failed. Any producer could satisfy the requirement by
# writing one character into their own anchor.
#
# Reading the backend's `check()` instead would not have fixed it: that returns
# `(True, ...)` for an imprint-only token too, because it answers "could this be
# re-checked at all?". A token whose signature was never verified - because no trust
# root was supplied - is "ok" to that boolean. So the five states below are kept
# distinct, and only one of them satisfies a requirement.

VERIFIED = "verified"
IMPRINT_ONLY = "imprint_only"
UNSUPPORTED = "unsupported"
MISSING = "missing"
FAILED = "failed"

#: Backends that can produce externally verified evidence in this build. `local` is
#: absent and cannot be added: it attests nothing by construction, and says so.
DEFAULT_APPROVED_BACKENDS: tuple[str, ...] = ("rfc3161",)

#: Never approvable, whatever a caller passes. The point of external evidence is that
#: it rests on somebody other than the party being audited.
_NEVER_EXTERNAL = frozenset({"local", "?"})


@dataclass(frozen=True)
class ExternalTrust:
    """The verifier's trust configuration, supplied per invocation.

    Held in a value object and passed down rather than registered globally. The CLI
    used to do `register_backend(Rfc3161Backend(ca_pem=...))`, which mutates a
    process-wide dict: one `verify --tsa-ca` call then silently configured the trust
    root for every later call in that process, including later tests. A root that
    arrives from somewhere other than this invocation is a root nobody chose here.
    """

    tsa_ca_pem: bytes | None = None
    approved_backends: tuple[str, ...] = DEFAULT_APPROVED_BACKENDS

    def approves(self, backend: str) -> bool:
        return backend not in _NEVER_EXTERNAL and backend in self.approved_backends


@dataclass(frozen=True)
class ExternalProofResult:
    """What one entry in `external_proofs` established."""

    backend: str
    state: str
    detail: str
    approved: bool = False

    @property
    def satisfies_requirement(self) -> bool:
        return self.state == VERIFIED and self.approved

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "state": self.state,
            "approved": self.approved,
            "satisfies_requirement": self.satisfies_requirement,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ExternalEvidence:
    """The external-evidence state of one anchor, per proof and in aggregate."""

    state: str = MISSING
    proofs: tuple[ExternalProofResult, ...] = ()
    detail: str = "no external proof was supplied"
    subject_digest: str | None = None

    @property
    def satisfies_requirement(self) -> bool:
        """Whether `require_external_anchor` may pass on this.

        One approved backend must have reached `verified`, and no proof may have
        failed. A failed proof is not routed around: a token in the anchor that does
        not verify is evidence of tampering or of a proof copied from elsewhere, and
        passing on a second proof while ignoring the broken one would be the same
        false green in a new place.
        """
        return (any(p.satisfies_requirement for p in self.proofs)
                and not any(p.state == FAILED for p in self.proofs))

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "satisfies_requirement": self.satisfies_requirement,
            "detail": self.detail,
            "subject_digest": self.subject_digest,
            "proofs": [p.to_dict() for p in self.proofs],
        }


def _resolve_backend(name: str, trust: ExternalTrust):
    """A backend instance for this invocation, configured from `trust`.

    rfc3161 is constructed fresh with the caller's root rather than looked up, so the
    trust root used is the one supplied here and the global registry is left alone.
    Any other backend comes from the registry as registered, and is read through the
    boolean protocol, which caps it at `imprint_only`.
    """
    if name == "rfc3161":
        from .rfc3161 import Rfc3161Backend

        return Rfc3161Backend(ca_pem=trust.tsa_ca_pem)
    from .anchor import AnchorError, get_backend

    try:
        return get_backend(name)
    except AnchorError:
        return None


def _verify_one(proof: object, digest: str, trust: ExternalTrust) -> ExternalProofResult:
    if not isinstance(proof, dict):
        return ExternalProofResult(
            "?", UNSUPPORTED,
            f"this entry is not a proof object but {type(proof).__name__} "
            f"({proof!r:.40}); nothing can be checked against it")
    name = proof.get("backend") or "?"
    if not isinstance(name, str):
        return ExternalProofResult(
            "?", UNSUPPORTED, f"this entry names no usable backend ({name!r})")

    if name in _NEVER_EXTERNAL:
        return ExternalProofResult(
            name, UNSUPPORTED,
            "the local backend attests nothing: this anchor was made by the same party "
            "that produced the receipt, so it establishes no external evidence and "
            "cannot satisfy a requirement for any"
            if name == "local" else
            "this entry names no backend, so nothing can re-check it")

    backend = _resolve_backend(name, trust)
    if backend is None:
        return ExternalProofResult(
            name, UNSUPPORTED,
            f"no backend named {name!r} is available here, so this proof was not "
            "checked. An unchecked proof is not evidence")

    approved = trust.approves(name)
    verify = getattr(backend, "verify", None)
    try:
        if callable(verify):
            state, detail = verify(digest, proof)
        else:
            # The boolean protocol cannot say "verified", so it does not get to.
            ok, detail = backend.check(digest, proof)
            state = IMPRINT_ONLY if ok else FAILED
            if ok:
                detail += (f". {name} reports only pass/fail, so this is recorded as "
                           "imprint-only: nothing here established a signature")
    except Exception as e:                                   # a backend must not abort
        return ExternalProofResult(
            name, FAILED, f"{name} raised while checking this proof: {e!r}", approved)

    if state == VERIFIED and not approved:
        detail += (f". However, {name} is not approved as a source of external "
                   f"evidence here (approved: {', '.join(trust.approved_backends) or 'none'})")
    return ExternalProofResult(name, state, detail, approved)


def verify_external_evidence(
    anchor: dict,
    *,
    tsa_ca_pem: bytes | None = None,
    approved_backends: list[str] | tuple[str, ...] | None = None,
    trust: ExternalTrust | None = None,
) -> ExternalEvidence:
    """Verify every external proof an anchor carries, under explicit trust config.

    The proofs are checked against the subject digest *recomputed from the anchor*,
    never against the `subject_digest` field the anchor carries. Those differ exactly
    when the anchor was edited after it was stamped, and checking the stored field
    would confirm a proof over content that is no longer there.
    """
    if trust is None:
        trust = ExternalTrust(
            tsa_ca_pem=tsa_ca_pem,
            approved_backends=(tuple(approved_backends)
                               if approved_backends is not None
                               else DEFAULT_APPROVED_BACKENDS))
    elif tsa_ca_pem is not None or approved_backends is not None:
        raise ValueError("pass either `trust` or the individual trust arguments")

    from .anchor import subject_digest as recompute

    try:
        digest = recompute(anchor)
    except (TypeError, ValueError) as e:
        return ExternalEvidence(
            FAILED, (),
            f"the anchor could not be digested, so no proof could be checked: {e}")

    stored = anchor.get("subject_digest")
    proofs = anchor.get("external_proofs") or []
    if not isinstance(proofs, list) or not proofs:
        return ExternalEvidence(
            MISSING, (), subject_digest=digest,
            detail="the anchor carries no external proof, so nothing establishes when "
                   "it was made. A local anchor is this machine's clock")

    if isinstance(stored, str) and stored != digest:
        # Every proof in the anchor was made over `stored`, and the anchor no longer
        # hashes to it. Checking them against the recomputed digest would report an
        # imprint mismatch per proof; saying it once, by name, is clearer.
        return ExternalEvidence(
            FAILED,
            tuple(ExternalProofResult(
                (p.get("backend") or "?") if isinstance(p, dict) else "?", FAILED,
                "not checked: the anchor's subject digest no longer matches its content")
                for p in proofs),
            subject_digest=digest,
            detail=f"the anchor's subject digest is {stored[:20]}... but its content "
                   f"hashes to {digest[:20]}...: it was edited after being stamped, so "
                   "its proofs are about content this anchor no longer holds")

    results = tuple(_verify_one(p, digest, trust) for p in proofs)

    if any(r.state == FAILED for r in results):
        state = FAILED
    elif any(r.state == VERIFIED for r in results):
        state = VERIFIED
    elif any(r.state == IMPRINT_ONLY for r in results):
        state = IMPRINT_ONLY
    else:
        state = UNSUPPORTED

    satisfying = [r.backend for r in results if r.satisfies_requirement]
    if satisfying and state == VERIFIED:
        detail = (f"externally verified by {', '.join(satisfying)}"
                  + (f" ({len(results) - len(satisfying)} further proof(s) established "
                     "less; see each)" if len(satisfying) != len(results) else ""))
    else:
        detail = _unsatisfied_detail(state, results, trust)
    return ExternalEvidence(state, results, detail, digest)


def _unsatisfied_detail(
    state: str, results: tuple[ExternalProofResult, ...], trust: ExternalTrust,
) -> str:
    """One sentence saying which state was reached and why it is not enough."""
    lead = {
        FAILED: "external verification FAILED",
        VERIFIED: "verified, but by no approved backend",
        IMPRINT_ONLY: "imprint only: NOT verified",
        UNSUPPORTED: "no proof here could be checked",
    }.get(state, state)
    if state == VERIFIED:
        lead += (f" (approved: {', '.join(trust.approved_backends) or 'none'})")
    return lead + " - " + "; ".join(f"{r.backend}: {r.detail}" for r in results)


# --------------------------------------------------------------------------------------
# Verified loading
# --------------------------------------------------------------------------------------
#
# `load_all` returns records. It does not verify them, and nothing forced a caller to.
# A record sealed under an older schema is re-hashed with the fields that schema did
# not have stripped out, which is what keeps an old ledger verifiable - and is also
# what leaves those fields unsealed. Writing a value into one of them does not disturb
# the hash, so on the baseline a genuine 1.1 receipt could be given a
# `manifest.suite.hash` and a pre-registration pinning that digest reported "suite
# digest matches" while the ledger reported an intact chain.
#
# So loading for the purpose of deciding anything goes through here: the chain is
# verified, each record's sealing schema is established, unsealed fields are
# quarantined before anything can read them, and what was removed is named.


@dataclass(frozen=True)
class RecordVerification:
    """What one record in a ledger turned out to be."""

    index: int
    status: str
    sealed_under: str | None = None
    quarantined: tuple[str, ...] = ()
    #: Data invariants that do not hold, as "field: why". Kept apart from `verified`
    #: on purpose: that the hash matches and that the numbers can be measurements are
    #: different claims, and a fabricated summary satisfies the first one, because it
    #: was sealed along with everything else.
    problems: tuple[str, ...] = ()
    detail: str = ""

    @property
    def verified(self) -> bool:
        """Whether the content matches the hash, under today's schema or its own."""
        return self.status in ("ok", "legacy")

    @property
    def internally_consistent(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "status": self.status,
            "sealed_under": self.sealed_under,
            "quarantined": list(self.quarantined),
            "problems": list(self.problems),
            "verified": self.verified,
            "internally_consistent": self.internally_consistent,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class VerifiedLedger:
    """Records that verified, with unsealed fields already removed."""

    records: tuple[RunRecord, ...] = ()
    results: tuple[RecordVerification, ...] = ()
    chain_ok: bool = False
    detail: str = "the ledger was not read"

    @property
    def ok(self) -> bool:
        return (self.chain_ok and bool(self.results)
                and all(r.verified for r in self.results))

    @property
    def quarantined(self) -> tuple[str, ...]:
        """Every unsealed field removed anywhere in the ledger, deduplicated."""
        seen: dict[str, None] = {}
        for result in self.results:
            for path in result.quarantined:
                seen[path] = None
        return tuple(seen)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "chain_ok": self.chain_ok,
            "detail": self.detail,
            "quarantined": list(self.quarantined),
            "results": [r.to_dict() for r in self.results],
        }


def load_verified(ledger: Path | str) -> VerifiedLedger:
    """Load a ledger, verify it, and quarantine every field it did not seal.

    Nothing is raised for a ledger that fails: a gate has to report that as a failed
    check, not abort. The returned records are safe to read in the sense that every
    field still present was covered by the hash that was verified.
    """
    from .ledger import check_record_hash, load_all, quarantine_unsealed, verify_chain
    from .validation import validate_record

    path = Path(ledger)
    try:
        records = load_all(path)
    except (OSError, ValueError) as e:
        return VerifiedLedger(detail=f"{path} could not be read: {e}")
    if not records:
        return VerifiedLedger(detail=f"{path} holds no records")

    chain_ok, chain_detail = verify_chain(path)
    cleaned: list[RunRecord] = []
    results: list[RecordVerification] = []
    for index, record in enumerate(records):
        status, sealed_under = check_record_hash(record)
        safe, removed = quarantine_unsealed(record)
        cleaned.append(safe)
        # Validated after quarantine, so a value the schema never sealed cannot make
        # the summary look consistent, nor inconsistent.
        problems = tuple(f"{p.name}: {p.detail}" for p in validate_record(safe))
        detail = {
            "ok": f"verified under schema {sealed_under}",
            "legacy": f"verified under the schema that sealed it ({sealed_under})",
            "tampered": "content does not match its stored hash",
            "unsealed": "carries no hash at all, so there is nothing to check its "
                        "content against - it was never sealed, which is not the same "
                        "as having been altered",
            "unknown_schema": f"sealed under schema {sealed_under}, which this build "
                              "cannot re-hash",
            "unsupported_schema": f"claims schema version {sealed_under!r}, which this "
                                  "build cannot verify",
        }.get(status, status)
        if removed:
            detail += (
                f". {len(removed)} field(s) carried a value its schema did not seal and "
                f"were removed before anything read them: {', '.join(removed)}")
        if problems:
            detail += (f". {len(problems)} data invariant(s) do not hold: "
                       + "; ".join(problems[:3])
                       + ("" if len(problems) <= 3 else f" (and {len(problems) - 3} more)"))
        results.append(RecordVerification(
            index=index, status=status, sealed_under=sealed_under,
            quarantined=removed, problems=problems, detail=detail))

    result = VerifiedLedger(tuple(cleaned), tuple(results), chain_ok, chain_detail)
    failed = [r for r in results if not r.verified]
    if failed:
        detail = (f"{len(failed)} of {len(results)} record(s) did not verify: "
                  + "; ".join(f"record {r.index}: {r.detail}" for r in failed))
    elif result.quarantined:
        detail = (f"{chain_detail} Unsealed field(s) removed before reading: "
                  f"{', '.join(result.quarantined)}")
    else:
        detail = chain_detail
    return VerifiedLedger(tuple(cleaned), tuple(results), chain_ok, detail)


__all__ = [
    "DEFAULT_APPROVED_BACKENDS",
    "ExternalEvidence",
    "ExternalProofResult",
    "ExternalTrust",
    "RecordVerification",
    "SignatureVerification",
    "VerifiedLedger",
    "load_verified",
    "verify_external_evidence",
    "verify_receipt_signature",
]
