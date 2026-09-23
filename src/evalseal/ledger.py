from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from .locking import ledger_lock
from .models import SCHEMA_VERSION, ProvenanceManifest, RunRecord

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
    "1.4": [
        ("manifest", "target", "provider"),
        ("manifest", "target", "effective_params", "max_tokens"),
        ("manifest", "scorer", "config_hash"),
        ("manifest", "scorer", "judge", "provider"),
        ("manifest", "scorer", "judge", "effective_params", "max_tokens"),
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


def check_record_hash(record: RunRecord) -> tuple[str, str | None]:
    """Does a record's stored hash match its content? One definition for every caller.

    Returns (status, schema): "ok" under today's schema; "legacy" when it matches only
    under the older schema it was sealed with; "unknown_schema" when it was sealed under
    a schema this build cannot re-hash; "tampered" otherwise. `verify` and `anchor` both
    ask this question, and two answers to it would eventually disagree.
    """
    if _content_hash(record) == record.hash:
        return ("ok", SCHEMA_VERSION)
    sealed_under = record.manifest.schema_version
    if sealed_under != SCHEMA_VERSION and _content_hash(record, sealed_under) == record.hash:
        return ("legacy", sealed_under)
    if sealed_under not in _FIELDS_ADDED_IN and sealed_under != SCHEMA_VERSION:
        return ("unknown_schema", sealed_under)
    return ("tampered", sealed_under)


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
        # Re-hash it the way its own schema would have, so an older ledger still
        # verifies instead of being mistaken for a tampered one.
        status, sealed_under = check_record_hash(r)
        if status == "legacy" and sealed_under is not None:
            legacy.add(sealed_under)
        elif status == "unknown_schema":
            return (False, (
                f"Record {i} was sealed under schema {sealed_under}, which this build does "
                f"not know how to re-hash. Verify it with the version that wrote it."
            ))
        elif status == "tampered":
            return (False, f"TAMPER DETECTED at record {i}: content hash does not match.")
        prev = r.hash
    if legacy:
        older = ", ".join(sorted(legacy))
        return (True, (
            f"Chain intact: {len(recs)} record(s). "
            f"{len(legacy)} older schema version(s) verified under their own rules ({older})."
        ))
    return (True, f"Chain intact: {len(recs)} record(s).")


FINGERPRINT_SCHEME = 2
FINGERPRINT_PREFIX = f"evalseal-fp/{FINGERPRINT_SCHEME}"


def _case_set_hash(record: RunRecord) -> str:
    """Hash of the case ids actually scored, sorted so order cannot change it."""
    ids = sorted(c.case_id for c in record.results)
    return "sha256:" + hashlib.sha256(
        json.dumps(ids, separators=(",", ":")).encode()).hexdigest()


def _judge_identity(m: ProvenanceManifest) -> dict:
    """Everything about the judge that can change a verdict."""
    judge = m.scorer.judge
    if judge is None:
        return {"provider": None, "endpoint": None, "model": None, "params": None}
    return {
        "provider": judge.provider,
        # The endpoint, not only the model name: the same model reached through a proxy
        # or a different deployment is not self-evidently the same grader.
        "endpoint": judge.base_url,
        "model": judge.requested_model,
        "params": judge.effective_params.model_dump(mode="json"),
    }


def evaluator_fingerprint(record: RunRecord) -> str:
    """Hash of the measuring instrument: everything that decides how a response scores.

    Scheme 2 covers the scorer type and its own settings, the judge's provider,
    endpoint, model and sampling parameters including `max_tokens`, the rubric and
    prompt template hashes, and the identity of the inputs - the dataset and the exact
    set of case ids. Scheme 1 missed the endpoint, `max_tokens` and the scorer's
    settings, so two runs graded by different regexes fingerprinted identically.

    Deliberately excluded: the target model and its parameters, and the suite file's
    hash. Swapping the model under test is the reason to run a benchmark, and a suite
    file bundles the target, so hashing it would make "same grading, different model"
    look incomparable. Both are sealed as provenance and both are in
    `config_fingerprint`.

    The returned value carries its scheme, so a pin made under an older scheme fails
    with an explanation rather than an unexplained mismatch.
    """
    m = record.manifest
    payload = {
        "scheme": FINGERPRINT_SCHEME,
        "scorer_type": m.scorer.type,
        "scorer_config_hash": m.scorer.config_hash,
        "judge": _judge_identity(m),
        "rubric_hash": m.scorer.rubric_hash,
        "judge_prompt_hash": m.scorer.judge_prompt_hash,
        "dataset_hash": m.dataset.hash,
        "case_set_hash": _case_set_hash(record),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{FINGERPRINT_PREFIX}:sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def fingerprint_scheme(value: str) -> int | None:
    """The scheme a fingerprint string was produced under, or None if it carries none."""
    if value.startswith("evalseal-fp/"):
        head = value.split(":", 1)[0]
        try:
            return int(head.split("/", 1)[1])
        except ValueError:
            return None
    return None


def records_share_fingerprint_inputs(before: RunRecord, after: RunRecord) -> bool:
    """Whether both records recorded the fields scheme 2 hashes.

    A record sealed before schema 1.4 has no provider, no `max_tokens` and no scorer
    config hash. Fingerprinting it under scheme 2 is still well defined, but a
    difference against a 1.4 record may be an artefact of the older schema rather than a
    real change, and a reader has to be told which.
    """
    return all(_schema_tuple(r.manifest.schema_version) >= (1, 4) for r in (before, after))


def config_fingerprint(record: RunRecord) -> str:
    """Hash of the whole measurement setup, target model included.

    This is what `gate --expect-config` pins: it answers "is anything about this run
    different from the one I approved". For "can these two numbers be compared", use
    `evaluator_fingerprint`, which ignores the target under test.
    """
    m = record.manifest
    payload = {
        "scheme": FINGERPRINT_SCHEME,
        "evaluator": evaluator_fingerprint(record),
        "target": {
            "provider": m.target.provider,
            "endpoint": m.target.base_url,
            "model": m.target.requested_model,
            "params": m.target.effective_params.model_dump(mode="json"),
        },
        "suite_hash": m.suite.hash if m.suite else None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{FINGERPRINT_PREFIX}:sha256:" + hashlib.sha256(blob.encode()).hexdigest()
