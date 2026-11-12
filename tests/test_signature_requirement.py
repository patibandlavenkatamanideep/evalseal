"""A pre-registration that requires a signature must not pass an unsigned receipt.

`Preregistration.require_signature` existed as a field, was accepted by the builder,
was written into `examples/prereg.json` - and was never read. A contract could declare
it and `gate --prereg` would pass an unsigned receipt silently. That is the exact
failure `policy.py` opens by naming: a threshold that quietly does nothing is worse
than no threshold, because the build going green is read as evidence.

Three separate claims are kept apart here, because satisfying one does not satisfy the
others:

- **presence** - a signature exists at all
- **coverage** - it covers *the selected receipt*, not merely some earlier prefix of
  the ledger
- **identity** - the signing key is one the verifier decided to trust, not whichever
  key the producer happened to embed beside the ledger
"""
from __future__ import annotations

import json

from conftest import make_dataset, scripted

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.executor import run_eval
from evalseal.ledger import seal_and_append
from evalseal.prereg import build_preregistration, check_preregistration
from evalseal.signing import generate_keypair, sign_head, signatures_path

CLAUSE = "prereg.require_signature"


def _record(answer: str = "yes"):
    target = LocalCallableTarget(scripted({"a": [answer], "b": ["yes"]}))
    return run_eval(make_dataset("a", "b"), target, RegexScorer(r"^yes$"), n_repeats=5)


def _ledger(tmp_path, *, records: int = 1):
    """A ledger with `records` sealed receipts, newest last."""
    path = tmp_path / "ledger.jsonl"
    sealed = [seal_and_append(_record(), path, relink=True) for _ in range(records)]
    return path, sealed


def _keypair(tmp_path, name: str = "k"):
    return generate_keypair(tmp_path / f"{name}.key", tmp_path / f"{name}.pub")


def _by_rule(checks):
    return {c.rule: c for c in checks}


def _signature_state(ledger, record_index, trusted=None):
    """The typed outcome the gate consumes. Imported lazily so the reproduction below
    reports a missing clause rather than a missing import on the baseline."""
    from evalseal.verification import verify_receipt_signature

    return verify_receipt_signature(ledger, record_index, trusted_keys=trusted)


# --- presence: the reproduction -------------------------------------------------------

def test_an_unsigned_receipt_fails_a_contract_that_requires_a_signature(tmp_path):
    """The baseline false green: this clause did not exist, so the gate passed."""
    ledger, sealed = _ledger(tmp_path)
    contract = build_preregistration(n_repeats=5, require_signature=True)

    checks = _by_rule(check_preregistration(
        contract, sealed[-1],
        signature=_signature_state(ledger, len(sealed) - 1)))

    assert CLAUSE in checks, (
        "require_signature produced no check at all; a contract declaring it would "
        "pass an unsigned receipt")
    assert not checks[CLAUSE].passed
    assert "no signature" in checks[CLAUSE].detail.lower()


def test_a_missing_signature_sidecar_is_named_rather_than_crashing(tmp_path):
    ledger, sealed = _ledger(tmp_path)
    assert not signatures_path(ledger).exists()
    state = _signature_state(ledger, 0)
    assert not state.present
    assert "no signature" in state.detail.lower()


def test_not_requiring_a_signature_produces_no_clause(tmp_path):
    """Unrequested checks must stay absent, not appear as vacuous passes."""
    ledger, sealed = _ledger(tmp_path)
    contract = build_preregistration(n_repeats=5, require_signature=False)
    checks = _by_rule(check_preregistration(
        contract, sealed[-1], signature=_signature_state(ledger, 0)))
    assert CLAUSE not in checks


# --- coverage: a prefix signature is not a signature over a later receipt -------------

def test_a_prefix_only_signature_does_not_cover_a_later_receipt(tmp_path):
    """Sign record 0, append record 1, gate on record 1.

    `verify_signatures` returns ok=True here - every signature entry is internally
    valid - and says in prose that records 0-0 of 2 are signed. A requirement that
    read only the boolean would pass a receipt nobody signed.
    """
    path = tmp_path / "ledger.jsonl"
    first = seal_and_append(_record(), path, relink=True)
    _keypair(tmp_path)
    sign_head(path, tmp_path / "k.key")
    second = seal_and_append(_record("no"), path, relink=True)
    assert first.hash != second.hash

    from evalseal.signing import verify_signatures
    ok, message = verify_signatures(path)
    assert ok, "precondition: the signature itself is valid"
    assert "0-0 of 2" in message, message

    contract = build_preregistration(n_repeats=5, require_signature=True)
    checks = _by_rule(check_preregistration(
        contract, second, signature=_signature_state(path, 1)))

    assert not checks[CLAUSE].passed
    assert "does not cover" in checks[CLAUSE].detail


def test_a_signature_covering_the_selected_record_passes(tmp_path):
    path = tmp_path / "ledger.jsonl"
    seal_and_append(_record(), path, relink=True)
    second = seal_and_append(_record("no"), path, relink=True)
    public = _keypair(tmp_path)
    sign_head(path, tmp_path / "k.key")          # signs record 1, the selected one

    contract = build_preregistration(n_repeats=5, require_signature=True)
    contract.trusted_public_keys = [public]
    checks = _by_rule(check_preregistration(
        contract, second, signature=_signature_state(path, 1, trusted=[public])))
    assert checks[CLAUSE].passed, checks[CLAUSE].detail


# --- identity: validity is not trust --------------------------------------------------

def test_a_valid_signature_from_an_untrusted_key_fails(tmp_path):
    """Never trust a key merely because the producer embedded it beside the ledger."""
    ledger, sealed = _ledger(tmp_path)
    _keypair(tmp_path, "producer")
    sign_head(ledger, tmp_path / "producer.key")
    verifiers_key = _keypair(tmp_path, "verifier")

    from evalseal.signing import verify_signatures
    assert verify_signatures(ledger)[0], "precondition: the signature is valid"

    contract = build_preregistration(n_repeats=5, require_signature=True)
    contract.trusted_public_keys = [verifiers_key]
    checks = _by_rule(check_preregistration(
        contract, sealed[-1],
        signature=_signature_state(ledger, 0, trusted=[verifiers_key])))

    assert not checks[CLAUSE].passed
    assert "not one of the trusted" in checks[CLAUSE].detail


def test_with_no_trusted_keys_declared_the_clause_says_identity_is_unchecked(tmp_path):
    """A signature with no trust anchor establishes that *someone* signed. The clause
    must not present that as the same thing as a trusted signer."""
    ledger, sealed = _ledger(tmp_path)
    _keypair(tmp_path)
    sign_head(ledger, tmp_path / "k.key")

    contract = build_preregistration(n_repeats=5, require_signature=True)
    checks = _by_rule(check_preregistration(
        contract, sealed[-1], signature=_signature_state(ledger, 0)))

    detail = checks[CLAUSE].detail
    assert "identity not checked" in detail
    assert "trusted_public_keys" in detail


def test_key_rotation_is_supported_by_trusting_several_keys(tmp_path):
    ledger, sealed = _ledger(tmp_path)
    old = _keypair(tmp_path, "old")
    new = _keypair(tmp_path, "new")
    sign_head(ledger, tmp_path / "new.key")

    contract = build_preregistration(n_repeats=5, require_signature=True)
    checks = _by_rule(check_preregistration(
        contract, sealed[-1],
        signature=_signature_state(ledger, 0, trusted=[old, new])))
    assert checks[CLAUSE].passed, checks[CLAUSE].detail


# --- malformed and unsupported inputs fail closed -------------------------------------

def test_a_tampered_signature_fails(tmp_path):
    ledger, sealed = _ledger(tmp_path)
    public = _keypair(tmp_path)
    sign_head(ledger, tmp_path / "k.key")

    sidecar = signatures_path(ledger)
    entry = json.loads(sidecar.read_text(encoding="utf-8").strip())
    entry["signature"] = "A" * len(entry["signature"])
    sidecar.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    state = _signature_state(ledger, 0, trusted=[public])
    assert not state.valid
    assert state.present


def test_an_unsupported_algorithm_fails_closed(tmp_path):
    ledger, sealed = _ledger(tmp_path)
    public = _keypair(tmp_path)
    sign_head(ledger, tmp_path / "k.key")

    sidecar = signatures_path(ledger)
    entry = json.loads(sidecar.read_text(encoding="utf-8").strip())
    entry["algorithm"] = "rot13"
    sidecar.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    state = _signature_state(ledger, 0, trusted=[public])
    assert not state.valid
    assert "algorithm" in state.detail.lower()


def test_a_ledger_rewritten_after_signing_fails(tmp_path):
    """The signature covers a hash the ledger no longer holds."""
    ledger, sealed = _ledger(tmp_path)
    public = _keypair(tmp_path)
    sign_head(ledger, tmp_path / "k.key")

    record = json.loads(ledger.read_text(encoding="utf-8").strip())
    record["results"][0]["scores"][0] = 0.0
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")

    state = _signature_state(ledger, 0, trusted=[public])
    assert not state.valid


# --- independence from the anchor requirement -----------------------------------------

def test_requiring_a_signature_works_without_requiring_an_anchor(tmp_path):
    """The two requirements are separate claims and must not be coupled."""
    ledger, sealed = _ledger(tmp_path)
    contract = build_preregistration(n_repeats=5, require_signature=True)
    assert contract.require_anchor is False

    checks = _by_rule(check_preregistration(
        contract, sealed[-1], signature=_signature_state(ledger, 0)))
    assert CLAUSE in checks
    assert not checks[CLAUSE].passed
    assert "prereg.require_anchor" not in checks


def test_the_clause_is_absent_when_no_signature_state_was_supplied(tmp_path):
    """A gate that cannot look at the ledger must fail the requirement, not skip it."""
    _, sealed = _ledger(tmp_path)
    contract = build_preregistration(n_repeats=5, require_signature=True)
    checks = _by_rule(check_preregistration(contract, sealed[-1]))
    assert CLAUSE in checks
    assert not checks[CLAUSE].passed
    assert "not checked" in checks[CLAUSE].detail.lower()


# --- the field is reachable from the CLI ----------------------------------------------

def test_preregister_can_declare_the_requirement_from_the_command_line(tmp_path):
    """It was settable only by hand-editing JSON, which is how a field ends up
    declared-but-unread in the first place."""
    from typer.testing import CliRunner

    from evalseal.cli import app
    from evalseal.prereg import load_preregistration

    dataset = tmp_path / "d.jsonl"
    dataset.write_text(json.dumps({"case_id": "c0", "prompt": "a"}) + "\n", "utf-8")
    out = tmp_path / "prereg.json"
    result = CliRunner().invoke(app, [
        "preregister", "--dataset", str(dataset), "--require-signature",
        "--trusted-key", "AAAA", "--out", str(out)])
    assert result.exit_code == 0, result.output

    contract = load_preregistration(out)
    assert contract.require_signature is True
    assert contract.trusted_public_keys == ["AAAA"]
