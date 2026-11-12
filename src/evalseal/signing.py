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
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .ledger import LEDGER_PATH, ledger_lock, load_all, verify_chain

ALGORITHM = "ed25519"
SIGNATURES_SUFFIX = ".sig.jsonl"


def signatures_path(ledger: Path = LEDGER_PATH) -> Path:
    return ledger.with_suffix(ledger.suffix + SIGNATURES_SUFFIX)


#: `O_NOFOLLOW` is POSIX. On Windows there is nothing to pass, and `O_EXCL` is still
#: honoured, so exclusive creation works there even though the symlink refusal does not.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
#: Owner read/write at creation. Not applied afterwards: see `write_private_key`.
PRIVATE_KEY_MODE = 0o600


def write_private_key(path: Path, pem: bytes) -> None:
    """Create `path` exclusively, owner-only, without following a symlink.

    Three things were wrong with `exists()` then `write_bytes()` then `chmod(0o600)`,
    and all three are reproduced in tests/test_key_handling.py:

    * **The key was created world-readable.** `write_bytes` honours the umask, so the
      file existed at 0644 until the chmod landed - a window in which any local user
      could read a signing key. `os.open` takes the mode at creation instead.
    * **A dangling symlink passed the check.** `Path.exists()` follows links and is
      False for one pointing at nothing, so the key was created at the link's target,
      somewhere the user never named. `O_NOFOLLOW` refuses the link itself.
    * **Check-then-write is not exclusive.** Anything could appear in between.
      `O_CREAT | O_EXCL` makes the creation the check.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
    try:
        fd = os.open(path, flags, PRIVATE_KEY_MODE)
    except FileExistsError:
        raise FileExistsError(
            f"{path} exists; refusing to overwrite a private key. Delete it on purpose "
            "or choose another path - a signing key that is replaced by accident "
            "invalidates every signature made with it.") from None
    except OSError as e:
        # ELOOP on a symlink, ENOTDIR on a path component that is not a directory.
        raise OSError(
            f"cannot create the private key at {path}: {e}. It must be a path this "
            "process creates itself, not an existing file and not a symbolic link."
        ) from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
    except BaseException:
        # A half-written key is worse than none: it would be loaded and fail to parse
        # at signing time, after a run has already happened.
        path.unlink(missing_ok=True)
        raise


def generate_keypair(private_path: Path, public_path: Path, *,
                     overwrite_public: bool = False) -> str:
    """Write a new keypair. The private key is created readable only by its owner.

    Neither destination is overwritten. The public key used to be replaced silently,
    which is the quieter half of the same mistake: a verifier pinned to the old key
    reports every signature as untrusted, and the file that would have told them which
    key it was is gone. `overwrite_public=True` is the explicit way to replace it,
    which key rotation needs - see docs/operations.md.
    """
    if public_path.exists() and not overwrite_public:
        raise FileExistsError(
            f"{public_path} exists; refusing to overwrite a public key. Anyone "
            "verifying against it would start seeing untrusted signatures with no "
            "record of which key changed. Pass --overwrite-public to replace it, or "
            "choose another path.")
    private = Ed25519PrivateKey.generate()
    write_private_key(private_path, private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    pub_b64 = public_key_b64(private.public_key())
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(pub_b64 + "\n", encoding="utf-8")
    return pub_b64


def public_key_b64(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode()


def public_key_fingerprint(public_b64: str) -> str:
    """sha256 of the raw 32-byte public key, the short name a verifier can compare.

    Over the raw key bytes rather than the base64 text, so the same key always has the
    same fingerprint however it happens to be encoded or wrapped.
    """
    raw = base64.b64decode(public_b64.strip())
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return key


def load_public_key(value: str) -> Ed25519PublicKey:
    """Accept either a path to a key file or the base64 key itself."""
    candidate = Path(value)
    text = candidate.read_text(encoding="utf-8") if candidate.is_file() else value
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(text.strip()))


def sign_head(ledger: Path = LEDGER_PATH, key_path: Path = Path("evalseal.key")) -> dict:
    """Sign the current head of the ledger and append the signature.

    **The whole read-verify-sign-append runs under the ledger lock.** Without it a
    concurrent `evalseal run` could append between the head being read and the
    signature being written, and the entry would claim `record_index` N for a ledger
    whose last record is N+1: a signature that is perfectly valid, over a head that is
    no longer the head, while the newest record is unsigned. The lock is the same one
    `seal_and_append` takes, so the two serialise against each other.
    """
    # The key is loaded before the lock: it is a read of an unrelated file, and a
    # missing or malformed key should fail without making an appender wait.
    private = load_private_key(key_path)
    with ledger_lock(ledger):
        records = load_all(ledger)
        if not records:
            raise ValueError(f"{ledger} has no records to sign")
        ok, message = verify_chain(ledger)
        if not ok:
            raise ValueError(f"refusing to sign a ledger that does not verify: {message}")

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
        line = json.dumps(entry, sort_keys=True) + "\n"
        # One write of one line, flushed and fsynced inside the lock, so a reader
        # never sees half an entry and a crash cannot leave one.
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    return entry


def load_signatures(ledger: Path = LEDGER_PATH) -> list[dict]:
    path = signatures_path(ledger)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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
