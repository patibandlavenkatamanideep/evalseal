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


__all__ = ["SignatureVerification", "verify_receipt_signature"]
