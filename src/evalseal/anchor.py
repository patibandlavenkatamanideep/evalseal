"""A local anchor: one file that binds a receipt to its ledger and signature state.

A receipt, the ledger it was sealed into, and the signature over that ledger live in
separate files. An anchor names all three by hash in one small JSON object, so a third
party handed the anchor can check - with no access to the machine that made it - that
the receipt in front of them is the one that was sealed, that the ledger still holds it
in the same position, and whether a key signed that ledger.

## What a local anchor is not

**It is not a timestamp.** `created_at` comes from the clock of whoever ran `evalseal
anchor`, which is exactly as trustworthy as that person. `external_proof` is null here
on purpose: an independent timestamp or transparency-log entry has to come from a party
other than the one being audited, and is the subject of docs/external-anchoring.md.

**It does not prove the eval ran as described.** It proves that these bytes are the
bytes that were anchored, and that they are consistent with the ledger and signature
named. A sealed record of a run that never happened anchors just as well.

## Determinism

Everything except `created_at` is a pure function of the inputs. `subject_digest` is the
hash of the anchor's content with `created_at`, `notes` and `external_proof` left out, so
two anchors of the same receipt and ledger state share it. It is also the single value an
external timestamp authority or transparency log would later attest.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .ledger import (
    check_record_hash,
    config_fingerprint,
    evaluator_fingerprint,
    load_all,
    verify_chain,
)
from .models import RunRecord
from .provenance import environment_provenance
from .signing import (
    ALGORITHM,
    load_public_key,
    load_signatures,
    public_key_fingerprint,
    signatures_path,
    verify_signatures,
)

ANCHOR_VERSION = 1
KIND = "evalseal.local-anchor"

# Fields that are about the anchor rather than the thing anchored, and so stay out of
# the subject digest: the clock reading, the prose, and any proof added later.
_UNHASHED = ("created_at", "notes", "external_proof", "subject_digest")

NOTES = (
    "Local anchor. It binds a receipt to its ledger head and signature state by hash. "
    "created_at is the anchoring machine's own clock and is not an independent "
    "timestamp; external_proof is null because no third party has attested this "
    "anchor. It does not prove the evaluation ran as described."
)


class AnchorError(ValueError):
    """The inputs cannot be anchored, or the anchor file itself is unusable."""


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def subject_digest(anchor: dict) -> str:
    """Hash of everything an anchor asserts, excluding fields about the anchor itself."""
    return _sha256_bytes(_canonical({k: v for k, v in anchor.items() if k not in _UNHASHED}))


def _case_set_hash(record: RunRecord) -> str | None:
    """Hash of the case ids that actually ran, sorted, so order cannot change it."""
    ids = [c.case_id for c in record.results]
    return _sha256_bytes(_canonical(sorted(ids))) if ids else None


def _artifact(path: Path) -> dict[str, str]:
    return {"path": path.as_posix(), "sha256": _sha256_bytes(path.read_bytes())}


def build_anchor(
    receipt: RunRecord,
    receipt_path: Path,
    ledger: Path | None = None,
    extra_artifacts: list[Path] | None = None,
    *,
    created_at: str | None = None,
) -> dict:
    """Build a local anchor for a sealed receipt. Refuses a receipt that does not verify.

    Anchoring a receipt whose stored hash no longer matches its content would put a
    fresh, authoritative-looking stamp on an edited record, so that is an error rather
    than something an anchor records.
    """
    status, sealed_under = check_record_hash(receipt)
    if status not in ("ok", "legacy"):
        raise AnchorError(
            f"refusing to anchor {receipt_path}: its stored hash does not match its content "
            f"({status}). An anchor must not vouch for an edited receipt."
        )

    m = receipt.manifest
    anchor: dict = {
        "anchor_version": ANCHOR_VERSION,
        "kind": KIND,
        "evalseal_version": environment_provenance()["evalseal_version"],
        "receipt_hash": receipt.hash,
        "receipt_schema_version": sealed_under,
        "receipt_evalseal_version": m.environment.evalseal_version,
        "sealed": {
            "evaluator_fingerprint": evaluator_fingerprint(receipt),
            "config_fingerprint": config_fingerprint(receipt),
            "dataset_hash": m.dataset.hash,
            "case_set_hash": _case_set_hash(receipt),
            "n_cases": len(receipt.results),
            "rubric_hash": m.scorer.rubric_hash,
            "judge_prompt_hash": m.scorer.judge_prompt_hash,
            "judge_model": m.scorer.judge.requested_model if m.scorer.judge else None,
            "target_model": m.target.requested_model,
        },
        "ledger": None,
        "signature_present": False,
        "public_key_fingerprint": None,
        "artifact_hashes": {"receipt": _artifact(receipt_path)},
        "external_proof": None,
    }

    if ledger is not None:
        records = load_all(ledger)
        if not records:
            raise AnchorError(f"{ledger} has no records")
        ok, message = verify_chain(ledger)
        if not ok:
            raise AnchorError(
                f"refusing to anchor against a ledger that does not verify: {message}")
        positions = [i for i, r in enumerate(records) if r.hash == receipt.hash]
        if not positions:
            raise AnchorError(
                f"{receipt_path} is not in {ledger}: no record there has hash "
                f"{receipt.hash[:20]}... Anchor it against the ledger it was sealed into."
            )
        anchor["ledger"] = {
            "path": ledger.as_posix(),
            "receipt_index": positions[0],
            "length": len(records),
            "head_hash": records[-1].hash,
        }
        anchor["artifact_hashes"]["ledger"] = _artifact(ledger)

        entries = load_signatures(ledger)
        if entries:
            sig_ok, sig_message = verify_signatures(ledger)
            if not sig_ok:
                raise AnchorError(
                    f"refusing to anchor: the ledger's signatures do not verify: {sig_message}")
            covering = [e for e in entries if e["record_index"] >= positions[0]]
            if covering:
                latest = max(covering, key=lambda e: e["record_index"])
                anchor["signature_present"] = True
                anchor["public_key_fingerprint"] = public_key_fingerprint(latest["public_key"])
                # The signature itself, not only a note that one exists: with it a
                # third party can check the signature from the anchor alone, without the
                # ledger's .sig.jsonl. Neither the signature nor the public key is secret.
                anchor["signature"] = {
                    "record_index": latest["record_index"],
                    "signed_hash": latest["hash"],
                    "algorithm": latest["algorithm"],
                    "public_key": latest["public_key"],
                    "signature": latest["signature"],
                }
            anchor["artifact_hashes"]["signatures"] = _artifact(signatures_path(ledger))

    for path in extra_artifacts or []:
        anchor["artifact_hashes"][f"artifact:{path.name}"] = _artifact(path)

    anchor["subject_digest"] = subject_digest(anchor)
    anchor["created_at"] = created_at or datetime.now(UTC).isoformat()
    anchor["notes"] = NOTES
    return anchor


def write_anchor(anchor: dict, path: Path) -> None:
    """Sorted keys and a fixed indent, so the file is byte-stable apart from created_at."""
    path.write_text(json.dumps(anchor, sort_keys=True, indent=2) + "\n", encoding="utf-8")


@dataclass
class ArtifactCheck:
    """One sealed artifact, re-hashed against the file on disk now."""
    role: str
    status: str          # ok | changed | missing | unverifiable
    detail: str


def check_artifacts(record: RunRecord, base_dir: Path | None = None) -> list[ArtifactCheck]:
    """Re-hash every artifact a record was sealed against.

    A file that is absent is reported as missing, never as passing: a verification that
    quietly skips what it cannot find reads as more than it proved.
    """
    base = base_dir or Path(".")
    checks: list[ArtifactCheck] = []
    for art in record.manifest.artifacts:
        if art.sha256 is None or art.path is None:
            checks.append(ArtifactCheck(
                art.role, "unverifiable",
                art.note or "sealed without a digest, so there is nothing to check"))
            continue
        path = Path(art.path)
        if not path.is_absolute():
            path = base / path
        if not path.exists():
            checks.append(ArtifactCheck(
                art.role, "missing", f"{path} is not here; its digest cannot be checked"))
            continue
        actual = _sha256_bytes(path.read_bytes())
        if actual == art.sha256:
            checks.append(ArtifactCheck(art.role, "ok", f"{path} matches the sealed digest"))
        else:
            checks.append(ArtifactCheck(
                art.role, "changed",
                f"{path} does not match the digest sealed with this record"))
    return checks


@dataclass
class AnchorCheck:
    name: str
    passed: bool
    detail: str
    required: bool = True


def verify_anchor(
    anchor: dict,
    receipt: RunRecord,
    *,
    ledger: Path | None = None,
    base_dir: Path | None = None,
) -> list[AnchorCheck]:
    """Re-check every claim an anchor makes, and say which ones could not be checked.

    A missing optional file is reported as not checked rather than as passed: a
    verification that quietly skips what it cannot see reads as more than it proved.
    """
    base = base_dir or Path(".")
    checks: list[AnchorCheck] = []

    if anchor.get("anchor_version") != ANCHOR_VERSION or anchor.get("kind") != KIND:
        return [AnchorCheck(
            "anchor format", False,
            f"unsupported anchor (version {anchor.get('anchor_version')!r}, "
            f"kind {anchor.get('kind')!r})")]

    recomputed = subject_digest(anchor)
    checks.append(AnchorCheck(
        "anchor integrity", recomputed == anchor.get("subject_digest"),
        "the anchor's content matches its subject digest" if recomputed ==
        anchor.get("subject_digest") else "the anchor file was edited after it was made"))

    status, _ = check_record_hash(receipt)
    checks.append(AnchorCheck(
        "receipt integrity", status in ("ok", "legacy"),
        "the receipt's content matches its sealed hash" if status in ("ok", "legacy")
        else f"the receipt's content does not match its sealed hash ({status})"))
    checks.append(AnchorCheck(
        "receipt identity", receipt.hash == anchor.get("receipt_hash"),
        "this is the receipt that was anchored" if receipt.hash == anchor.get("receipt_hash")
        else f"this receipt ({receipt.hash[:20]}...) is not the anchored one "
             f"({str(anchor.get('receipt_hash'))[:20]}...)"))

    sig = anchor.get("signature")
    if anchor.get("signature_present") and sig:
        checks.append(_embedded_signature_check(anchor, sig, receipt))

    anchored_ledger = anchor.get("ledger")
    if anchored_ledger is not None:
        ledger_path = ledger or (base / anchored_ledger["path"])
        if not ledger_path.exists():
            checks.append(AnchorCheck(
                "ledger", False, f"not checked: {ledger_path} is not available", required=False))
        else:
            records = load_all(ledger_path)
            ok, message = verify_chain(ledger_path)
            checks.append(AnchorCheck("ledger chain", ok, message))
            n = anchored_ledger["length"]
            idx = anchored_ledger["receipt_index"]
            prefix_ok = len(records) >= n and records[n - 1].hash == anchored_ledger["head_hash"]
            checks.append(AnchorCheck(
                "ledger head", prefix_ok,
                f"record {n - 1} is still the anchored head; later appends are allowed"
                if prefix_ok else "the ledger no longer has the anchored head at the anchored "
                                  "position; its history was rewritten"))
            placed = len(records) > idx and records[idx].hash == receipt.hash
            checks.append(AnchorCheck(
                "receipt in ledger", placed,
                f"the receipt is record {idx} of the ledger" if placed
                else f"record {idx} of the ledger is not this receipt"))

            if anchor.get("signature_present"):
                sig_ok, sig_message = verify_signatures(ledger_path)
                fingerprints = {public_key_fingerprint(e["public_key"])
                                for e in load_signatures(ledger_path)}
                same_key = anchor.get("public_key_fingerprint") in fingerprints
                checks.append(AnchorCheck(
                    "signature", sig_ok and same_key,
                    sig_message if not sig_ok
                    else "signed by the anchored key" if same_key
                    else "no signature by the anchored key is present"))

    for role, art in sorted(anchor.get("artifact_hashes", {}).items()):
        if role == "receipt":
            continue          # checked above by content, which survives re-serialisation
        if role == "ledger" or role == "signatures":
            continue          # appends are legitimate; the head check covers history
        path = base / art["path"]
        if not path.exists():
            checks.append(AnchorCheck(
                role, False, f"not checked: {path} is not available", required=False))
            continue
        same = _sha256_bytes(path.read_bytes()) == art["sha256"]
        checks.append(AnchorCheck(
            role, same, "unchanged" if same else f"{path} changed after it was anchored"))

    checks.append(AnchorCheck(
        "external proof", True,
        "none: this is a local anchor, so nothing here establishes when it was made"
        if anchor.get("external_proof") is None
        else "present but not verified by this version", required=False))
    return checks


def _embedded_signature_check(anchor: dict, sig: dict, receipt: RunRecord) -> AnchorCheck:
    """Verify the signature carried in the anchor, with no other file.

    When the signature covers the receipt's own record, this proves the key signed this
    receipt. When it covers a later record, it proves the key signed that later head;
    tying the head back to this receipt then needs the ledger, and the ledger checks do.
    """
    from cryptography.exceptions import InvalidSignature

    if sig.get("algorithm") != ALGORITHM:
        return AnchorCheck("embedded signature", False,
                           f"unsupported algorithm {sig.get('algorithm')!r}")
    if public_key_fingerprint(sig["public_key"]) != anchor.get("public_key_fingerprint"):
        return AnchorCheck("embedded signature", False,
                           "the embedded public key does not match the recorded fingerprint")
    try:
        load_public_key(sig["public_key"]).verify(
            base64.b64decode(sig["signature"]), sig["signed_hash"].encode())
    except (InvalidSignature, ValueError):
        return AnchorCheck("embedded signature", False,
                           "the embedded signature is not valid for the signed hash")
    if sig["signed_hash"] == receipt.hash:
        return AnchorCheck("embedded signature", True,
                           "valid, and it signs this receipt's own record")
    return AnchorCheck("embedded signature", True,
                       f"valid for ledger record {sig['record_index']}, a later head; "
                       "the ledger checks tie that head back to this receipt")


def anchor_passed(checks: list[AnchorCheck]) -> bool:
    """Every required check passed. Optional checks that could not run do not count."""
    return all(c.passed for c in checks if c.required)
