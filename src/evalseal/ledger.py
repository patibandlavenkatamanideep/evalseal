from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

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
        ("manifest", "artifacts"),
        ("manifest", "target", "provider"),
        ("manifest", "target", "effective_params", "max_tokens"),
        ("manifest", "scorer", "config_hash"),
        ("manifest", "scorer", "judge", "provider"),
        ("manifest", "scorer", "judge", "effective_params", "max_tokens"),
    ],
    "1.5": [
        ("manifest", "environment", "ci"),
        ("manifest", "suite", "evaluator_hash"),
        ("manifest", "suite", "target_hash"),
    ],
    "1.7": [
        ("manifest", "execution"),
    ],
    "1.6": [
        ("manifest", "scorer", "impl_hash"),
        ("manifest", "target", "api_version"),
        ("manifest", "target", "system_prompt_hash"),
        ("manifest", "target", "effective_params", "unset"),
        ("manifest", "scorer", "judge", "api_version"),
        ("manifest", "scorer", "judge", "system_prompt_hash"),
        ("manifest", "scorer", "judge", "effective_params", "unset"),
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


#: Every schema version this build can re-hash, oldest first. The last entry is
#: today's. A version outside this tuple is rejected rather than guessed at: see
#: `schema_is_supported` for why guessing is unsafe in both directions.
SUPPORTED_SCHEMAS: tuple[str, ...] = (
    "1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7")


def _schema_tuple(version: str) -> tuple[int, ...] | None:
    """Parse a version, or None when it is not a version at all.

    This returned `(0,)` for anything unparseable, which made an unparseable version
    mean "older than every field". A record claiming `schema_version: "nonsense"` then
    had the whole strip table applied before hashing - `manifest.suite`,
    `manifest.code`, `manifest.environment`, `manifest.artifacts`, `results[].verdicts`
    and the rest - so every one of those fields could be written to freely while the
    hash still matched and the ledger reported an intact chain.
    """
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def schema_is_supported(version: str | None) -> bool:
    """Whether this build can honestly verify a record sealed under `version`.

    Unknown in either direction is unsupported. An unparseable version cannot be
    placed against the strip table. A *newer* version cannot be either: this build
    does not know which fields it added, nor whether it changed what an existing field
    means - 1.3 added no field and only changed the meaning of
    `scorer.judge_prompt_hash`, so a format change need not be visible in the data at
    all. Verifying such a record would be vouching for a format we cannot read.
    """
    return version in SUPPORTED_SCHEMAS


def unsealed_paths(sealed_under: str | None) -> tuple[tuple[str, ...], ...]:
    """The field paths a record sealed under `sealed_under` did not cover.

    These are the fields stripped before hashing, which is exactly what makes them
    unsealed: they are absent from the bytes the hash commits to, so any value may be
    written into them without disturbing it. Empty for an unsupported version, because
    stripping a field is what makes it injectable.
    """
    if not schema_is_supported(sealed_under):
        return ()
    target = _schema_tuple(sealed_under or "")
    if target is None:
        return ()
    out: list[tuple[str, ...]] = []
    for version, fields in _FIELDS_ADDED_IN.items():
        added_in = _schema_tuple(version)
        if added_in is not None and added_in > target:
            out.extend(fields)
    return tuple(out)


def _value_at(payload: dict, path: tuple[str, ...]) -> object:
    """The value at a dotted path; `results[]` collects the value across every case."""
    if path[0] == "results[]":
        return [case.get(path[1]) for case in payload.get("results", [])]
    node: object = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def quarantine_unsealed(record: RunRecord) -> tuple[RunRecord, tuple[str, ...]]:
    """Remove the fields the sealing schema did not cover. Returns (record, removed).

    A legitimate old receipt is unchanged: the fields its schema lacked are already
    at their defaults, so nothing is removed and `removed` is empty. A receipt with a
    value written into one of them comes back with that value gone and the path named,
    because the signature and the chain say nothing about it.

    The record still verifies afterwards - quarantine is not an accusation. A real 1.1
    receipt with `manifest.suite` filled in is still a real 1.1 receipt; what changes
    is that the unsealed field cannot be read as evidence.
    """
    paths = unsealed_paths(record.manifest.schema_version)
    if not paths:
        return (record, ())
    before = record.model_dump(mode="json")
    payload = record.model_dump(mode="json")
    for path in paths:
        _drop(payload, path)
    cleaned = RunRecord.model_validate(payload)
    after = cleaned.model_dump(mode="json")
    # Compared after a round trip rather than against None, because most of these
    # fields default to something non-None (`code` and `environment` to populated
    # models, `verdicts` to [], `pass_count` to 0), so "is it set?" cannot be asked
    # directly. What matters is whether dropping it changed anything.
    removed = tuple(
        ".".join(path) for path in paths
        if _value_at(before, path) != _value_at(after, path))
    if not removed:
        return (record, ())
    cleaned.hash = record.hash
    return (cleaned, removed)


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
        for path in unsealed_paths(as_schema):
            _drop(payload, path)
        payload.setdefault("manifest", {})["schema_version"] = as_schema
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def load_all(path: Path = LEDGER_PATH) -> list[RunRecord]:
    """Every record in a ledger, parsed strictly.

    Strictly, because `model_validate_json` accepts two things a receipt must not
    contain. A duplicate member is resolved silently in favour of the last one, so the
    value a reader sees and the value a gate uses can differ. And `NaN`, `Infinity` and
    `-Infinity` - which are not JSON at all - parse as floats, and every comparison
    against them is false, so such a score would clear any threshold it was tested
    against.

    `InvalidInput` is a `ValueError`, which the callers that already handle a malformed
    ledger catch.
    """
    from .validation import InvalidInput, strict_json_loads

    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = strict_json_loads(line, what=f"{path} line {number}")
        if not isinstance(payload, dict):
            raise InvalidInput(f"{path} line {number} is not a record object")
        try:
            records.append(RunRecord.model_validate(payload))
        except ValidationError as e:
            raise InvalidInput(f"{path} line {number} is not a valid record:\n{e}") from None
    return records


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
    from .validation import require_valid

    # Refused before the lock is taken. Sealing an invalid record would put an
    # authoritative-looking hash on a number that cannot be a measurement, and a
    # non-finite score additionally serialises as `null`, so the sealed receipt could
    # never be loaded again: the measurement lost, and every later command failing on
    # a file that verifies as nothing.
    require_valid(record)

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

    Returns (status, schema): "unsupported_schema" when the version it claims is not
    one this build can verify; "ok" under today's schema; "legacy" when it matches only
    under the older schema it was sealed with; "unknown_schema" when the hash matches
    nothing and the version is one the strip table has no entry for, which is the
    likeliest explanation; "tampered" otherwise. `verify` and `anchor` both ask this
    question, and two answers to it would eventually disagree.

    Support is checked **first**, before any hash is compared. It used to be a
    fallback, reached only when no hash matched, which meant a record claiming a
    version this build does not understand could still come back verified: `9.9` was
    reported as `("ok", "1.5")`, asserting it was sealed under today's schema, and
    `nonsense` as `("legacy", "nonsense")` with every field in the strip table left
    unsealed.
    """
    sealed_under = record.manifest.schema_version
    if not schema_is_supported(sealed_under):
        return ("unsupported_schema", sealed_under)
    if _content_hash(record) == record.hash:
        return ("ok", SCHEMA_VERSION)
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
        elif status == "unsupported_schema":
            return (False, (
                f"Record {i} claims schema version {sealed_under!r}, which this build "
                f"cannot verify. Supported: {', '.join(SUPPORTED_SCHEMAS)}. A version "
                "this build does not know may have added fields it cannot see, or "
                "changed what an existing one means, so nothing here can vouch for it."
            ))
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


FINGERPRINT_SCHEME = 3
FINGERPRINT_PREFIX = f"evalseal-fp/{FINGERPRINT_SCHEME}"


def _hash_ids(ids: list[str]) -> str:
    """Hash a set of case ids, sorted so order cannot change it.

    Split out from `_case_set_hash` so a pre-registration can pin the ids it declares
    *before* any run exists and get the same value the receipt will produce. Two
    definitions of this hash would eventually disagree, and the disagreement would
    read as a dropped case.
    """
    return "sha256:" + hashlib.sha256(
        json.dumps(sorted(ids), separators=(",", ":")).encode()).hexdigest()


def _case_set_hash(record: RunRecord) -> str:
    """Hash of the case ids actually scored."""
    return _hash_ids([c.case_id for c in record.results])


def _judge_identity(m: ProvenanceManifest) -> dict:
    """Everything about the judge that can change a verdict."""
    judge = m.scorer.judge
    if judge is None:
        return {"provider": None, "endpoint": None, "model": None, "params": None,
                "api_version": None, "system_prompt_hash": None}
    return {
        "provider": judge.provider,
        # The endpoint, not only the model name: the same model reached through a proxy
        # or a different deployment is not self-evidently the same grader.
        "endpoint": judge.base_url,
        "model": judge.requested_model,
        # Includes `effective_params.unset` from schema 1.6, so a judge that inherited
        # a provider default is not confused with one whose API has no such parameter.
        "params": judge.effective_params.model_dump(mode="json"),
        # The version decides how the request is read; the system prompt steers the
        # verdict. Scheme 2 sealed neither, so "be lenient" and "be harsh" fingerprinted
        # identically.
        "api_version": judge.api_version,
        "system_prompt_hash": judge.system_prompt_hash,
    }


def evaluator_fingerprint(record: RunRecord) -> str:
    """Hash of the measuring instrument: everything that decides how a response scores.

    Scheme 3 covers the scorer type, its own settings and *the code that grades*, the
    judge's provider, endpoint, model, API version, system prompt and sampling
    parameters including `max_tokens` and which of them were merely inherited, the
    rubric and prompt template hashes, and the identity of the inputs - the dataset and
    the exact set of case ids.

    Each earlier scheme was superseded because something that moved a score did not
    move the fingerprint. Scheme 1 missed the endpoint, `max_tokens` and the scorer's
    settings, so two runs graded by different regexes fingerprinted identically.
    Scheme 2 missed the grading algorithm, the judge's system prompt and its API
    version: a fork that changed answer extraction took a benchmark from 1.0 to 0.0
    while `--expect-evaluator` kept passing.

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
        "scorer_impl_hash": m.scorer.impl_hash,
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


def explain_fingerprint_mismatch(kind: str, pinned: str, actual: str) -> str:
    """Why a pinned fingerprint did not match, scheme difference first.

    A pin written under an older scheme cannot match a scheme-2 fingerprint, and nothing
    about the run has to have changed for that to happen. Saying "the grading setup
    changed" would send someone hunting a change nobody made, so the scheme is checked
    before the contents are blamed.

    Lives here rather than in the CLI because both the `--expect-*` flags and a policy
    file's `run.expect_*` rules ask the same question, and two answers to it would
    eventually disagree.
    """
    pinned_scheme = fingerprint_scheme(pinned)
    if pinned_scheme != FINGERPRINT_SCHEME:
        named = f"scheme {pinned_scheme}" if pinned_scheme else "an unversioned scheme"
        return (
            f"The pinned {kind} fingerprint was made under {named}; this build computes "
            f"scheme {FINGERPRINT_SCHEME}, which covers fields the older scheme did not. "
            "The two cannot be compared, and this is not evidence that anything about "
            "the run changed. Re-pin deliberately: check what moved with `evalseal diff`, "
            "then take the new value from `evalseal report --json`. "
            "See docs/fingerprint-schemes.md."
        )
    if kind == "evaluator":
        return (
            "Not directly comparable: the evaluator fingerprint differs from the pinned "
            f"one (expected {pinned[:28]}..., got {actual[:28]}...). The grading setup "
            "changed, so this score cannot be compared with the baseline."
        )
    return (
        f"Configuration differs from the pinned one (expected {pinned[:28]}..., got "
        f"{actual[:28]}...). Something about what ran changed; this alone does not mean "
        "the runs are incomparable, since a different target model also changes it. To "
        "require the same grading setup, pin the evaluator fingerprint instead: "
        "`--expect-evaluator`, or `run.expect_evaluator` in a policy file."
    )


#: The oldest schema that recorded every field the current fingerprint scheme hashes.
#: A record sealed before this is still fingerprinted - the computation is well defined
#: - but a *difference* against a newer record may be an artefact of the older schema
#: rather than a change anyone made, which a reader has to be told.
FINGERPRINT_INPUTS_FROM = (1, 6)


def records_share_fingerprint_inputs(before: RunRecord, after: RunRecord) -> bool:
    """Whether both records recorded the fields the current scheme hashes.

    A record sealed before 1.4 has no provider, no `max_tokens` and no scorer config
    hash; one sealed before 1.6 has no digest of the grading code, no judge API version
    and no judge system prompt hash. Either way the older record's fingerprint is
    missing inputs the newer one has, so the two cannot be compared field for field.
    """
    # An unparseable version is not a version at all, so the comparison must not be
    # attempted. Such a record is rejected before this runs.
    parsed = [_schema_tuple(r.manifest.schema_version) for r in (before, after)]
    return all(v is not None and v >= FINGERPRINT_INPUTS_FROM for v in parsed)


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
            # The API version and the system prompt change what the target was asked,
            # so a run that changed either is not the run that was approved. Scheme 2
            # covered neither, which made `--expect-config` pass across both changes.
            "api_version": m.target.api_version,
            "system_prompt_hash": m.target.system_prompt_hash,
        },
        "suite_hash": m.suite.hash if m.suite else None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{FINGERPRINT_PREFIX}:sha256:" + hashlib.sha256(blob.encode()).hexdigest()
