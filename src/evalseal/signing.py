"""Ed25519 signatures over the ledger head.

The hash chain proves a ledger has not been edited. It does not prove *who* produced
it: anyone who can rewrite the whole file can rebuild a consistent chain. Signing the
head hash closes that gap for anyone holding the public key, because the head commits
to every record before it.

What a signature proves: this key signed a ledger whose head was H, and the records in
front of you hash to H. What it does not prove: that the run happened as described. A
signer can sign a record containing whatever they like.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .ledger import LEDGER_PATH, load_all, verify_chain

ALGORITHM = "ed25519"
SIGNATURES_SUFFIX = ".sig.jsonl"


def signatures_path(ledger: Path = LEDGER_PATH) -> Path:
    return ledger.with_suffix(ledger.suffix + SIGNATURES_SUFFIX)


def generate_keypair(private_path: Path, public_path: Path) -> str:
    """Write a new keypair. The private key is written readable only by its owner."""
    if private_path.exists():
        raise FileExistsError(f"{private_path} exists; refusing to overwrite a private key")
    private = Ed25519PrivateKey.generate()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    private_path.chmod(0o600)
    pub_b64 = public_key_b64(private.public_key())
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(pub_b64 + "\n")
    return pub_b64


def public_key_b64(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode()


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return key


def load_public_key(value: str) -> Ed25519PublicKey:
    """Accept either a path to a key file or the base64 key itself."""
    candidate = Path(value)
    text = candidate.read_text() if candidate.is_file() else value
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(text.strip()))


def sign_head(ledger: Path = LEDGER_PATH, key_path: Path = Path("evalseal.key")) -> dict:
    """Sign the current head of the ledger and append the signature."""
    records = load_all(ledger)
    if not records:
        raise ValueError(f"{ledger} has no records to sign")
    ok, message = verify_chain(ledger)
    if not ok:
        raise ValueError(f"refusing to sign a ledger that does not verify: {message}")

    private = load_private_key(key_path)
    head = records[-1].hash
    entry = {
        "record_index": len(records) - 1,
        "hash": head,
        "algorithm": ALGORITHM,
        "public_key": public_key_b64(private.public_key()),
        "signature": base64.b64encode(private.sign(head.encode())).decode(),
        "signed_at": datetime.now(UTC).isoformat(),
    }
    path = signatures_path(ledger)
    with path.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def load_signatures(ledger: Path = LEDGER_PATH) -> list[dict]:
    path = signatures_path(ledger)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def verify_signatures(ledger: Path = LEDGER_PATH, expected_public_key: str | None = None
                      ) -> tuple[bool, str]:
    """Check every signature against the records it claims to cover."""
    records = load_all(ledger)
    entries = load_signatures(ledger)
    if not entries:
        return (False, f"No signatures found at {signatures_path(ledger)}.")

    expected_b64 = None
    if expected_public_key is not None:
        expected_b64 = public_key_b64(load_public_key(expected_public_key))

    for i, entry in enumerate(entries):
        if entry.get("algorithm") != ALGORITHM:
            return (False, f"Signature {i}: unsupported algorithm {entry.get('algorithm')!r}.")
        index = entry["record_index"]
        if index >= len(records):
            return (False, f"Signature {i} covers record {index}, which the ledger lacks.")
        if records[index].hash != entry["hash"]:
            return (False, (
                f"Signature {i} covers a different record {index} than the ledger holds; "
                f"the ledger was rewritten after signing."
            ))
        if expected_b64 is not None and entry["public_key"] != expected_b64:
            return (False, f"Signature {i} was made by a different key than the one given.")
        try:
            load_public_key(entry["public_key"]).verify(
                base64.b64decode(entry["signature"]), entry["hash"].encode()
            )
        except InvalidSignature:
            return (False, f"Signature {i} is invalid for record {index}.")

    covered = max(e["record_index"] for e in entries)
    return (True, (
        f"{len(entries)} signature(s) valid; records 0-{covered} of {len(records)} "
        f"are signed."
    ))
