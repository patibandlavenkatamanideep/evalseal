from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from .locking import ledger_lock
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


def seal_and_append(
    record: RunRecord, path: Path = LEDGER_PATH, *, relink: bool = False
) -> RunRecord:
    """Seal `record` and append it, holding the ledger lock for the whole operation.

    The head is read *inside* the lock. Locking only the append would leave the classic
    race: two processes read the same head, both validate, and both append a record
    claiming the same predecessor.

    `relink=False` keeps the caller's `prev_hash` and refuses when the ledger has moved
    on, which is what a caller replaying a specific chain position wants. `relink=True`
    re-points the record at whatever the head is once the lock is held, which is what a
    concurrent `evalseal run` wants: every writer appends, in some serial order.
    """
    path = Path(path)
    with ledger_lock(path):
        head = last_hash(path)                     # read under the lock, not before it
        if record.prev_hash != head:
            if not relink:
                raise ValueError(
                    f"record.prev_hash {record.prev_hash[:20]} does not link to ledger head "
                    f"{head[:20]}; refusing to append a broken chain."
                )
            record.prev_hash = head
        record.hash = _content_hash(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(record.model_dump_json() + "\n")
            f.flush()
            os.fsync(f.fileno())                   # survive a crash, not just a close
    return record


def verify_chain(path: Path = LEDGER_PATH) -> tuple[bool, str]:
    """Returns (ok, message). Detects tampering, broken links and sibling records."""
    recs = load_all(path)

    # Siblings: two records claiming the same predecessor. A linear ledger has none,
    # and their presence means concurrent appends raced or the file was rewritten.
    parents = Counter(r.prev_hash for r in recs if r.prev_hash != GENESIS)
    duplicates = [h for h, n in parents.items() if n > 1]
    if duplicates:
        return (False, (
            f"Sibling records detected: {len(duplicates)} hash(es) claimed by more than one "
            f"record (first: {duplicates[0][:20]}...). The ledger is no longer linear."
        ))
    genesis_count = sum(1 for r in recs if r.prev_hash == GENESIS)
    if genesis_count > 1:
        return (False, f"{genesis_count} records claim GENESIS; the ledger is not linear.")

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


def config_fingerprint(record: RunRecord) -> str:
    """Hash of the parts of a manifest that decide whether two runs are comparable.

    Scores from runs with different fingerprints measure different things: a changed
    judge prompt or rubric moves the number without the target model changing at all.
    """
    m = record.manifest
    payload = {
        "target_model": m.target.requested_model,
        "target_params": m.target.effective_params.model_dump(mode="json"),
        "scorer_type": m.scorer.type,
        "rubric_hash": m.scorer.rubric_hash,
        "judge_prompt_hash": m.scorer.judge_prompt_hash,
        "judge_model": m.scorer.judge.requested_model if m.scorer.judge else None,
        "judge_params": (
            m.scorer.judge.effective_params.model_dump(mode="json") if m.scorer.judge else None
        ),
        "dataset_hash": m.dataset.hash,
        "suite_hash": m.suite.hash if m.suite else None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()
