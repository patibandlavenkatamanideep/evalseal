"""A receipt must not be read for fields the schema that sealed it did not have.

`_content_hash(record, sealed_under)` **strips** the fields added after
`sealed_under` before hashing, which is what lets a 1.1-era ledger still verify under
today's build. The same strip makes those fields unsealed: they are absent from the
bytes the hash covers, so anything may be written into them and the hash still
matches.

Three defects, each reproduced on the baseline:

**A3-1 - injection into a legitimate old receipt.** Take a genuine 1.1 receipt, write
`manifest.suite = {"hash": <forged>}` into it. `check_record_hash` returns
`("legacy", "1.1")`, `verify_chain` says *"Chain intact"*, and a pre-registration
pinning that suite digest reports **"suite digest matches"** - on a digest that was
never sealed.

**A3-2 - an unparseable version strips everything.** `_schema_tuple` returned `(0,)`
for anything it could not parse, so a record claiming `schema_version: "nonsense"` had
every post-1.0 field stripped before hashing: `manifest.suite`, `manifest.code`,
`manifest.environment`, `manifest.artifacts`, `results[].verdicts`,
`results[].pass_count`, `results[].stability_label` - all injectable at once, while
the ledger still reported an intact chain.

**A3-3 - a future schema reported as today's.** A record claiming `9.9` came back as
`("ok", "1.5")`: the status asserted it was sealed under the schema this build
understands, which is false. This build cannot know what a later format means and must
not vouch for one.

The rule this file pins: an old receipt stays verifiable, and a field the sealing
schema did not cover is **quarantined** - removed before anything reads it, and named
in the report - so it can never be mistaken for sealed evidence.
"""
from __future__ import annotations

import json

import pytest
from conftest import make_dataset, scripted

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.executor import run_eval
from evalseal.ledger import (
    _FIELDS_ADDED_IN,
    _content_hash,
    _drop,
    check_record_hash,
    verify_chain,
)
from evalseal.models import RunRecord, SuiteProvenance
from evalseal.prereg import build_preregistration, check_preregistration

FORGED = "sha256:" + "de" * 32


def _fresh():
    return run_eval(make_dataset("a"), LocalCallableTarget(scripted({"a": ["yes"]})),
                    RegexScorer(r"^yes$"), n_repeats=5)


def _sealed_under(version: str, *, strip: bool = True) -> RunRecord:
    """A receipt as a build of `version` would have written it.

    With `strip`, the fields added after `version` are removed, which is what a genuine
    old receipt looks like. The hash is then computed the way that build would have.
    """
    payload = json.loads(_fresh().model_dump_json())
    payload.pop("hash", None)
    if strip:
        target = tuple(int(p) for p in version.split(".")) if version[0].isdigit() \
            else (0,)
        for added_in, fields in _FIELDS_ADDED_IN.items():
            if tuple(int(p) for p in added_in.split(".")) > target:
                for path in fields:
                    _drop(payload, path)
    payload["manifest"]["schema_version"] = version
    record = RunRecord.model_validate(payload)
    record.hash = _content_hash(record, as_schema=version)
    return record


def _as_ledger(tmp_path, record: RunRecord, name: str = "l.jsonl"):
    path = tmp_path / name
    path.write_text(json.dumps(json.loads(record.model_dump_json())) + "\n",
                    encoding="utf-8")
    return path


def _by_rule(checks):
    return {c.rule: c for c in checks}


# --- what must keep working -----------------------------------------------------------

def test_a_genuine_old_receipt_still_verifies(tmp_path):
    """The whole point of the strip table. This must not regress."""
    for version in ("1.1", "1.2", "1.4"):
        record = _sealed_under(version)
        status, sealed = check_record_hash(record)
        assert status == "legacy", (version, status)
        assert sealed == version
        ok, msg = verify_chain(_as_ledger(tmp_path, record, f"{version}.jsonl"))
        assert ok, msg
        assert version in msg


def test_quarantine_leaves_a_genuine_old_receipt_alone(tmp_path):
    """Nothing is removed that was not there, and the record stays usable."""
    from evalseal.ledger import quarantine_unsealed

    record = _sealed_under("1.1")
    cleaned, removed = quarantine_unsealed(record)
    assert removed == (), removed
    assert cleaned.manifest.schema_version == "1.1"
    assert check_record_hash(cleaned)[0] == "legacy"


def test_a_current_receipt_is_untouched():
    from evalseal.ledger import quarantine_unsealed

    record = _fresh()
    record.hash = _content_hash(record)
    cleaned, removed = quarantine_unsealed(record)
    assert removed == ()
    assert cleaned.manifest.suite == record.manifest.suite
    assert check_record_hash(cleaned)[0] == "ok"


# --- A3-1: injection into a legitimate old receipt ------------------------------------

def _inject_suite(version: str) -> RunRecord:
    """A genuine receipt of `version`, with a suite digest written in afterwards."""
    record = _sealed_under(version)
    sealed_hash = record.hash
    record.manifest.suite = SuiteProvenance(hash=FORGED)
    record.hash = sealed_hash
    return record


def test_the_injected_field_is_quarantined():
    from evalseal.ledger import quarantine_unsealed

    record = _inject_suite("1.1")
    assert record.manifest.suite is not None, "precondition: it is present"
    cleaned, removed = quarantine_unsealed(record)
    assert cleaned.manifest.suite is None, "it was never sealed, so it must not be read"
    assert "manifest.suite" in removed


def test_an_injected_suite_digest_does_not_satisfy_a_pinned_contract():
    """The reproduction. On the baseline this reported "suite digest matches"."""
    from evalseal.ledger import quarantine_unsealed

    record = _inject_suite("1.1")
    contract = build_preregistration(n_repeats=5)
    contract.suite.sha256 = FORGED

    cleaned, _ = quarantine_unsealed(record)
    check = _by_rule(check_preregistration(contract, cleaned))["prereg.suite"]
    assert not check.passed, check.detail
    assert "sealed no suite" in check.detail


def test_the_injection_is_still_reported_as_verifying_under_its_own_schema():
    """Quarantine is not an accusation: the receipt is a real 1.1 receipt. What
    changes is that the unsealed field is not readable, not that the record fails."""
    from evalseal.ledger import quarantine_unsealed

    cleaned, removed = quarantine_unsealed(_inject_suite("1.1"))
    assert check_record_hash(cleaned) == ("legacy", "1.1")
    assert removed


# --- A3-2: an unparseable version must not mean "older than everything" ---------------

@pytest.mark.parametrize("version", ["nonsense", "", "1.x", "../../etc", "1.5.0.0.0"])
def test_an_unparseable_schema_version_is_rejected(version):
    from evalseal.ledger import schema_is_supported

    assert not schema_is_supported(version)


def test_a_record_claiming_an_unparseable_schema_does_not_verify(tmp_path):
    """It claimed to predate every field, which stripped them all before hashing and
    left the whole manifest injectable."""
    record = _sealed_under("nonsense", strip=False)
    status, _ = check_record_hash(record)
    assert status == "unsupported_schema", status
    ok, msg = verify_chain(_as_ledger(tmp_path, record))
    assert not ok
    assert "nonsense" in msg


def test_nothing_is_stripped_for_an_unsupported_version():
    """Belt and braces: even if such a record is hashed, the strip must not run, or
    the fields it removes become injectable."""
    from evalseal.ledger import unsealed_paths

    assert unsealed_paths("nonsense") == ()
    assert unsealed_paths("1.1") != ()


# --- A3-3: a future schema is not today's schema --------------------------------------

@pytest.mark.parametrize("version", ["9.9", "2.0", "1.6"])
def test_a_future_schema_version_is_rejected(tmp_path, version):
    """On the baseline `9.9` came back as ("ok", "1.5")."""
    record = _sealed_under(version, strip=False)
    status, sealed = check_record_hash(record)
    assert status == "unsupported_schema", (version, status, sealed)
    ok, msg = verify_chain(_as_ledger(tmp_path, record))
    assert not ok
    assert version in msg


def test_the_supported_set_is_the_strip_table_plus_today():
    from evalseal.ledger import SCHEMA_VERSION, SUPPORTED_SCHEMAS

    assert SCHEMA_VERSION in SUPPORTED_SCHEMAS
    for version in _FIELDS_ADDED_IN:
        assert version in SUPPORTED_SCHEMAS
    assert "1.0" in SUPPORTED_SCHEMAS, "the base version predates the table"


# --- verified loading -----------------------------------------------------------------

def test_load_verified_quarantines_and_reports(tmp_path):
    from evalseal.verification import load_verified

    path = _as_ledger(tmp_path, _inject_suite("1.1"))
    result = load_verified(path)
    assert result.ok, result.detail
    assert result.records[0].manifest.suite is None
    assert "manifest.suite" in result.results[0].quarantined
    assert result.results[0].sealed_under == "1.1"
    assert "manifest.suite" in result.detail


def test_load_verified_rejects_an_unsupported_schema(tmp_path):
    from evalseal.verification import load_verified

    result = load_verified(_as_ledger(tmp_path, _sealed_under("9.9", strip=False)))
    assert not result.ok
    assert result.results[0].status == "unsupported_schema"


def test_load_verified_rejects_a_rewritten_record(tmp_path):
    from evalseal.verification import load_verified

    record = _fresh()
    record.hash = _content_hash(record)
    path = _as_ledger(tmp_path, record)
    payload = json.loads(path.read_text())
    payload["results"][0]["scores"][0] = 0.0
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    result = load_verified(path)
    assert not result.ok
    assert result.results[0].status == "tampered"


def test_load_verified_serialises(tmp_path):
    from evalseal.verification import load_verified

    result = load_verified(_as_ledger(tmp_path, _inject_suite("1.1")))
    payload = result.to_dict()
    assert payload["results"][0]["quarantined"] == ["manifest.suite"]
    json.dumps(payload)


# --- the gate, end to end -------------------------------------------------------------

def _flat(text: str) -> str:
    return " ".join(text.split())


def _run(args):
    from typer.testing import CliRunner

    from evalseal.cli import app

    return CliRunner().invoke(app, args)


def test_the_gate_refuses_a_receipt_with_an_injected_suite_digest(tmp_path):
    """The acceptance criterion: the contract pinned this digest, the receipt verifies
    as a real 1.1 receipt, and the digest was never sealed."""
    from evalseal.cli import EXIT_UNSTABLE

    ledger = _as_ledger(tmp_path, _inject_suite("1.1"))
    contract = build_preregistration(n_repeats=5)
    contract.suite.sha256 = FORGED
    prereg = tmp_path / "prereg.json"
    prereg.write_text(contract.to_json(), encoding="utf-8")

    result = _run(["gate", "--ledger", str(ledger), "--prereg", str(prereg)])
    out = _flat(result.output)
    assert result.exit_code == EXIT_UNSTABLE, result.output
    assert "FAIL ledger.unsealed_fields" in out
    assert "manifest.suite" in out
    assert "FAIL prereg.suite" in out, "the pinned digest must not be read as sealed"


def test_the_gate_reports_a_clean_ledger_as_fully_sealed(tmp_path):
    from evalseal.ledger import seal_and_append

    ledger = tmp_path / "clean.jsonl"
    seal_and_append(_fresh(), ledger, relink=True)
    result = _run(["gate", "--ledger", str(ledger), "--json"])
    payload = json.loads(result.output)
    rules = {c["rule"]: c for c in payload["checks"]}
    assert rules["ledger.unsealed_fields"]["passed"]
    assert rules["ledger.record_verified"]["passed"]


def test_the_gate_refuses_an_unsupported_schema(tmp_path):
    ledger = _as_ledger(tmp_path, _sealed_under("9.9", strip=False))
    result = _run(["gate", "--ledger", str(ledger)])
    out = _flat(result.output)
    assert result.exit_code != 0
    assert "FAIL ledger.record_verified" in out
    assert "9.9" in out


def test_a_genuine_old_ledger_still_passes_the_gate(tmp_path):
    """Backward compatibility, through the CLI: no quarantine, no failure."""
    ledger = _as_ledger(tmp_path, _sealed_under("1.4"))
    result = _run(["gate", "--ledger", str(ledger), "--json"])
    payload = json.loads(result.output)
    rules = {c["rule"]: c for c in payload["checks"]}
    assert rules["ledger.unsealed_fields"]["passed"], rules["ledger.unsealed_fields"]
    assert rules["ledger.record_verified"]["passed"]
    assert "1.4" in rules["ledger.record_verified"]["detail"]


def test_verify_says_when_a_verified_record_carries_unsealed_fields(tmp_path):
    """"Chain intact" is true and not the whole truth: the chain does not cover a
    field the sealing schema did not have."""
    ledger = _as_ledger(tmp_path, _inject_suite("1.1"))
    result = _run(["verify", "--ledger", str(ledger)])
    out = _flat(result.output)
    assert result.exit_code == 0, "the chain really is intact"
    assert "Chain intact" in out
    assert "Unsealed field(s) in record 0" in out
    assert "manifest.suite" in out


def test_verify_stays_quiet_on_a_clean_ledger(tmp_path):
    from evalseal.ledger import seal_and_append

    ledger = tmp_path / "clean.jsonl"
    seal_and_append(_fresh(), ledger, relink=True)
    result = _run(["verify", "--ledger", str(ledger)])
    assert "Unsealed" not in result.output
    assert result.exit_code == 0
