from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import SCHEMA_VERSION, RunRecord

LEDGER_PATH = Path(".evalseal/ledger.jsonl")
GENESIS = "GENESIS"


def _content_hash(record: RunRecord) -> str:
    # Hash everything except the hash field itself.
    payload = record.model_dump(mode="json")
    payload.pop("hash", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def load_all(path: Path = LEDGER_PATH) -> list[RunRecord]:
    if not path.exists():
        return []
    return [
        RunRecord.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def last_hash(path: Path = LEDGER_PATH) -> str:
    recs = load_all(path)
    return recs[-1].hash if recs else GENESIS


def seal_and_append(record: RunRecord, path: Path = LEDGER_PATH) -> RunRecord:
    expected_prev = last_hash(path)
    if record.prev_hash != expected_prev:
        raise ValueError(
            f"record.prev_hash {record.prev_hash[:20]} does not link to ledger head "
            f"{expected_prev[:20]}; refusing to append a broken chain."
        )
    record.hash = _content_hash(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(record.model_dump_json() + "\n")
    return record


def verify_chain(path: Path = LEDGER_PATH) -> tuple[bool, str]:
    """Returns (ok, message). Detects a tampered score or a broken prev-link."""
    recs = load_all(path)
    prev = GENESIS
    for i, r in enumerate(recs):
        if r.prev_hash != prev:
            return (False, f"Broken chain at record {i}: prev_hash mismatch.")
        if _content_hash(r) != r.hash:
            # An older schema hashes different fields, so say that rather than cry tamper.
            if r.manifest.schema_version != SCHEMA_VERSION:
                return (False, (
                    f"Record {i} was sealed under schema {r.manifest.schema_version}; this build "
                    f"seals {SCHEMA_VERSION} and cannot verify it. Start a new ledger, or verify "
                    f"it with the version that wrote it."
                ))
            return (False, f"TAMPER DETECTED at record {i}: content hash does not match.")
        prev = r.hash
    return (True, f"Chain intact: {len(recs)} record(s).")
