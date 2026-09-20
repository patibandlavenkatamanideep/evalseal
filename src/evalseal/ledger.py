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


# Fields introduced by each schema version. Loading an older record into today's models
# fills these with defaults, which would change its hash and look like tampering. To
# verify an old record we hash it as the version that sealed it would have: strip every
# field added after that version, then hash.
#
# "results[]" applies the named keys to each case result.
_FIELDS_ADDED_IN: dict[str, list[tuple[str, ...]]] = {
    "1.1": [
        ("manifest", "run_config", "concurrency"),
    ],
    "1.2": [
        ("manifest", "suite"),
        ("manifest", "code"),
        ("manifest", "environment"),
        ("manifest", "scorer", "judge_prompt_hash"),
        ("manifest", "scorer", "judge_prompt"),
        ("manifest", "dataset", "path"),
        ("manifest", "dataset", "case_ids"),
        ("results[]", "verdicts"),
        ("results[]", "pass_count"),
        ("results[]", "fail_count"),
        ("results[]", "other_count"),
        ("results[]", "flip_count"),
        ("results[]", "stability_label"),
    ],
}


def _schema_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def _drop(payload: dict, path: tuple[str, ...]) -> None:
    """Remove one field, following a dotted path; `results[]` maps over every case."""
    if path[0] == "results[]":
        for case in payload.get("results", []):
            case.pop(path[1], None)
        return
    node: object = payload
    for key in path[:-1]:
        if not isinstance(node, dict):
            return
        node = node.get(key)
    if isinstance(node, dict):
        node.pop(path[-1], None)


def _content_hash(record: RunRecord, as_schema: str | None = None) -> str:
    """Hash a record as the given schema version would have hashed it.

    `as_schema=None` means today's schema. Passing an older version strips the fields
    that version did not have, which is what makes a 1.1-sealed ledger still verify.
    """
    payload = record.model_dump(mode="json")
    payload.pop("hash", None)
    if as_schema is not None and as_schema != SCHEMA_VERSION:
        target = _schema_tuple(as_schema)
        for version, fields in _FIELDS_ADDED_IN.items():
            if _schema_tuple(version) > target:
                for path in fields:
                    _drop(payload, path)
        payload.setdefault("manifest", {})["schema_version"] = as_schema
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def load_all(path: Path = LEDGER_PATH) -> list[RunRecord]:
    if not path.exists():
        return []
    return [
        RunRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
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
        with path.open("a", encoding="utf-8") as f:
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
    legacy: set[str] = set()
    for i, r in enumerate(recs):
        if r.prev_hash != prev:
            return (False, f"Broken chain at record {i}: prev_hash mismatch.")
        if _content_hash(r) != r.hash:
            # Re-hash it the way its own schema would have, so an older ledger still
            # verifies instead of being mistaken for a tampered one.
            sealed_under = r.manifest.schema_version
            if sealed_under != SCHEMA_VERSION and _content_hash(r, sealed_under) == r.hash:
                legacy.add(sealed_under)
            elif sealed_under not in _FIELDS_ADDED_IN and sealed_under != SCHEMA_VERSION:
                return (False, (
                    f"Record {i} was sealed under schema {sealed_under}, which this build does "
                    f"not know how to re-hash. Verify it with the version that wrote it."
                ))
            else:
                return (False, f"TAMPER DETECTED at record {i}: content hash does not match.")
        prev = r.hash
    if legacy:
        older = ", ".join(sorted(legacy))
        return (True, (
            f"Chain intact: {len(recs)} record(s). "
            f"{len(legacy)} older schema version(s) verified under their own rules ({older})."
        ))
    return (True, f"Chain intact: {len(recs)} record(s).")


def evaluator_fingerprint(record: RunRecord) -> str:
    """Hash of the *judging* side only: scorer, rubric, judge prompt, judge model, inputs.

    Deliberately excludes the target model and its parameters. Swapping the model under
    test is the reason to run a benchmark, and two models graded the same way are
    comparable. Changing how the grading works is what makes a comparison meaningless.
    """
    m = record.manifest
    payload = {
        "scorer_type": m.scorer.type,
        "rubric_hash": m.scorer.rubric_hash,
        "judge_prompt_hash": m.scorer.judge_prompt_hash,
        "judge_model": m.scorer.judge.requested_model if m.scorer.judge else None,
        "judge_params": (
            m.scorer.judge.effective_params.model_dump(mode="json") if m.scorer.judge else None
        ),
        "dataset_hash": m.dataset.hash,
        # The suite file is deliberately not included: it bundles the target model, so
        # hashing it would make "same grading, different model" look incomparable, which
        # is the one comparison a benchmark exists to make.
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def config_fingerprint(record: RunRecord) -> str:
    """Hash of the whole measurement setup, target model included.

    This is what `gate --expect-config` pins: it answers "is anything about this run
    different from the one I approved". For "can these two numbers be compared", use
    `evaluator_fingerprint`, which ignores the target under test.
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
