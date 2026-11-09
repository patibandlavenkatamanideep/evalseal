"""`require_external_anchor` must mean verified external evidence, not a non-empty list.

The clause was satisfied by `bool(anchor["external_proofs"])`. That made the strongest
requirement in a contract the easiest to satisfy: a `local` backend proof passes it,
although the local backend's own `check` says *"no external party attested this"*; so
does an entry naming a backend that does not exist; so does `[{}]`.

The states the brief distinguishes are kept distinct here, because collapsing them is
exactly what produced the false green:

| state | meaning |
|---|---|
| `verified` | an approved backend checked the proof against verifier-supplied trust |
| `imprint_only` | the proof is about this digest; its signature was not verified |
| `unsupported` | no backend here can check this proof format |
| `missing` | no external proof at all |
| `failed` | a backend checked it and it did not hold |

Only `verified` satisfies the requirement. The others stay readable as diagnostics
without being promoted to assurance.
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest
from conftest import make_dataset, scripted
from tsa_fixture import build_token, build_tsa

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.anchor import build_anchor, subject_digest, temporary_backends
from evalseal.executor import run_eval
from evalseal.ledger import seal_and_append
from evalseal.prereg import (
    AnchorState,
    build_preregistration,
    check_preregistration,
)
from evalseal.report import write_json
from evalseal.rfc3161 import Rfc3161Backend

CLAUSE = "prereg.require_external_anchor"


@pytest.fixture
def sealed(tmp_path):
    record = run_eval(make_dataset("a"), LocalCallableTarget(scripted({"a": ["yes"]})),
                      RegexScorer(r"^yes$"), n_repeats=5)
    ledger = tmp_path / "ledger.jsonl"
    record = seal_and_append(record, ledger, relink=True)
    receipt = tmp_path / "report.json"
    write_json(record, receipt)
    return record, receipt, ledger


def _anchor(sealed, proofs: list[dict]) -> dict:
    record, receipt, ledger = sealed
    anchor = build_anchor(record, receipt, ledger)
    anchor["external_proofs"] = proofs
    return anchor


def _evidence(anchor, **kwargs):
    from evalseal.verification import verify_external_evidence

    return verify_external_evidence(anchor, **kwargs)


def _by_rule(checks):
    return {c.rule: c for c in checks}


def _contract():
    return build_preregistration(
        n_repeats=5, require_anchor=True, require_external_anchor=True)


def _verified_anchor():
    """An anchor that already verified, so only the external clause is under test."""
    return AnchorState(present=True, verified=True, detail="anchor verified")


def _check(sealed, anchor, **kwargs):
    record, _, _ = sealed
    return _by_rule(check_preregistration(
        _contract(), record, anchor=_verified_anchor(),
        external=_evidence(anchor, **kwargs)))


# --- the reproductions ----------------------------------------------------------------

def test_a_local_proof_does_not_satisfy_external_anchoring(sealed):
    """The local backend attests nothing and says so. It must not pass this clause."""
    anchor = _anchor(sealed, [{"backend": "local", "established": "nothing", "proof": None}])
    checks = _check(sealed, anchor)
    assert not checks[CLAUSE].passed
    assert "local" in checks[CLAUSE].detail


def test_a_bogus_non_empty_list_does_not_satisfy_it(sealed):
    anchor = _anchor(sealed, [{}])
    checks = _check(sealed, anchor)
    assert not checks[CLAUSE].passed


def test_an_unknown_backend_is_unsupported_not_verified(sealed):
    anchor = _anchor(sealed, [{"backend": "notarius-maximus", "proof": "AAAA"}])
    evidence = _evidence(anchor)
    assert evidence.state == "unsupported"
    assert not evidence.satisfies_requirement
    assert "notarius-maximus" in evidence.detail


def test_a_proof_that_is_not_an_object_is_unsupported(sealed):
    """`["no"]` satisfied the clause on the baseline."""
    anchor = _anchor(sealed, ["no"])
    checks = _check(sealed, anchor)
    assert not checks[CLAUSE].passed
    assert _evidence(anchor).state == "unsupported"


def test_no_proofs_at_all_is_missing(sealed):
    evidence = _evidence(_anchor(sealed, []))
    assert evidence.state == "missing"
    assert not evidence.satisfies_requirement


# --- rfc3161: verified only under verifier-supplied trust ------------------------------

def _rfc3161_proof(anchor, tsa) -> dict:
    digest = anchor["subject_digest"]
    return {
        "backend": "rfc3161",
        "authority": "https://fixture.invalid/tsr",
        "subject_digest": digest,
        "proof": base64.b64encode(build_token(tsa, digest)).decode(),
        "verified_by_evalseal": False,
    }


def test_a_real_proof_without_a_trust_root_is_imprint_only(sealed):
    """The proof is about this digest, and nothing about its signature was checked."""
    tsa = build_tsa()
    anchor = _anchor(sealed, [])
    anchor["external_proofs"] = [_rfc3161_proof(anchor, tsa)]

    evidence = _evidence(anchor)
    assert evidence.state == "imprint_only"
    assert not evidence.satisfies_requirement
    assert "NOT verified" in evidence.detail


def test_a_real_proof_with_the_trust_root_is_verified(sealed):
    tsa = build_tsa()
    anchor = _anchor(sealed, [])
    anchor["external_proofs"] = [_rfc3161_proof(anchor, tsa)]

    evidence = _evidence(anchor, tsa_ca_pem=tsa.ca_pem)
    assert evidence.state == "verified"
    assert evidence.satisfies_requirement
    checks = _check(sealed, anchor, tsa_ca_pem=tsa.ca_pem)
    assert checks[CLAUSE].passed, checks[CLAUSE].detail


def test_a_proof_for_another_digest_fails(sealed):
    tsa = build_tsa()
    anchor = _anchor(sealed, [])
    other = "sha256:" + hashlib.sha256(b"something else").hexdigest()
    proof = _rfc3161_proof(anchor, tsa)
    proof["proof"] = base64.b64encode(build_token(tsa, other)).decode()
    anchor["external_proofs"] = [proof]

    evidence = _evidence(anchor, tsa_ca_pem=tsa.ca_pem)
    assert evidence.state == "failed"
    assert not evidence.satisfies_requirement


def test_a_tampered_token_fails(sealed):
    tsa = build_tsa()
    anchor = _anchor(sealed, [])
    proof = _rfc3161_proof(anchor, tsa)
    proof["proof"] = base64.b64encode(
        build_token(tsa, anchor["subject_digest"], corrupt_signature=True)).decode()
    anchor["external_proofs"] = [proof]

    evidence = _evidence(anchor, tsa_ca_pem=tsa.ca_pem)
    assert evidence.state == "failed"


def test_the_proof_must_be_about_this_anchors_subject_digest(sealed):
    """A proof copied from another anchor must not satisfy this one."""
    tsa = build_tsa()
    anchor = _anchor(sealed, [])
    anchor["external_proofs"] = [_rfc3161_proof(anchor, tsa)]
    anchor["notes"] = "edited after anchoring"
    # subject_digest excludes notes, so recompute and confirm it is unchanged...
    assert subject_digest(anchor) == anchor["subject_digest"]
    # ...then change something it *does* cover.
    anchor["receipt_hash"] = "sha256:" + "0" * 64
    evidence = _evidence(anchor, tsa_ca_pem=tsa.ca_pem)
    assert evidence.state == "failed"
    assert "subject digest" in evidence.detail.lower()


# --- approval is explicit -------------------------------------------------------------

def test_only_approved_backends_can_satisfy_the_requirement(sealed):
    tsa = build_tsa()
    anchor = _anchor(sealed, [])
    anchor["external_proofs"] = [_rfc3161_proof(anchor, tsa)]

    evidence = _evidence(anchor, tsa_ca_pem=tsa.ca_pem, approved_backends=["rekor"])
    assert not evidence.satisfies_requirement
    assert "not approved" in evidence.detail


def test_local_is_never_approvable_for_external_evidence(sealed):
    """Even if a caller lists it, the local backend attests nothing by construction."""
    anchor = _anchor(sealed, [{"backend": "local", "proof": None}])
    evidence = _evidence(anchor, approved_backends=["local", "rfc3161"])
    assert not evidence.satisfies_requirement


# --- diagnostics survive without being promoted ---------------------------------------

def test_every_proof_is_reported_individually(sealed):
    tsa = build_tsa()
    anchor = _anchor(sealed, [])
    anchor["external_proofs"] = [
        {"backend": "local", "proof": None},
        _rfc3161_proof(anchor, tsa),
    ]
    evidence = _evidence(anchor, tsa_ca_pem=tsa.ca_pem)
    assert len(evidence.proofs) == 2
    assert {p.backend for p in evidence.proofs} == {"local", "rfc3161"}
    # The aggregate takes the strongest, but the weaker one stays readable.
    assert evidence.state == "verified"
    assert any(p.state == "imprint_only" or p.state == "unsupported"
               for p in evidence.proofs if p.backend == "local")


def test_the_evidence_serialises_for_ci(sealed):
    evidence = _evidence(_anchor(sealed, []))
    payload = evidence.to_dict()
    assert set(payload) >= {"state", "satisfies_requirement", "detail", "proofs"}
    json.dumps(payload)


# --- trust configuration must not leak between invocations ----------------------------

def test_verification_does_not_mutate_the_global_backend_registry(sealed):
    """A trust root supplied for one verification must not silently apply to the next."""
    from evalseal import anchor as anchor_module

    tsa = build_tsa()
    anchor = _anchor(sealed, [])
    anchor["external_proofs"] = [_rfc3161_proof(anchor, tsa)]

    before = dict(anchor_module._BACKENDS)
    assert _evidence(anchor, tsa_ca_pem=tsa.ca_pem).state == "verified"
    assert dict(anchor_module._BACKENDS) == before, "registry was mutated"
    # And with no root this time, the weaker state must come back - not the cached one.
    assert _evidence(anchor).state == "imprint_only"


def test_temporary_backends_restores_the_registry():
    """`register_backend` is permanent; a verifier's trust root must not be."""
    from evalseal import anchor as anchor_module
    from evalseal.anchor import available_backends, get_backend, temporary_backends
    from evalseal.rfc3161 import Rfc3161Backend

    before = dict(anchor_module._BACKENDS)
    configured = Rfc3161Backend(ca_pem=b"not a real root")
    with temporary_backends(configured):
        assert get_backend("rfc3161") is configured
    assert get_backend("rfc3161") is not configured
    assert dict(anchor_module._BACKENDS) == before
    assert "local" in available_backends()


def test_temporary_backends_restores_after_an_exception():
    from evalseal import anchor as anchor_module
    from evalseal.anchor import temporary_backends
    from evalseal.rfc3161 import Rfc3161Backend

    before = dict(anchor_module._BACKENDS)
    with pytest.raises(RuntimeError), temporary_backends(Rfc3161Backend(ca_pem=b"x")):
        raise RuntimeError("boom")
    assert dict(anchor_module._BACKENDS) == before


# --- end to end through the CLI -------------------------------------------------------

def _flat(text: str) -> str:
    """Rich wraps the console output, so assertions collapse whitespace first."""
    return " ".join(text.split())


def _gate_run(tmp_path, args):
    from typer.testing import CliRunner

    from evalseal.cli import app

    return CliRunner().invoke(app, args)


def test_the_gate_requires_a_trust_root_to_pass_the_clause(tmp_path, sealed):
    """Without --tsa-ca the signature cannot be checked, so the requirement fails."""
    record, receipt, ledger = sealed
    tsa = build_tsa()
    anchor = build_anchor(record, receipt, ledger)
    anchor["external_proofs"] = [_rfc3161_proof(anchor, tsa)]
    anchor_path = tmp_path / "anchor.json"
    anchor_path.write_text(json.dumps(anchor), encoding="utf-8")

    prereg_path = tmp_path / "prereg.json"
    prereg_path.write_text(_contract().to_json(), encoding="utf-8")

    base = ["gate", "--ledger", str(ledger), "--prereg", str(prereg_path),
            "--anchor", str(anchor_path)]
    from evalseal.cli import EXIT_UNSTABLE

    without = _gate_run(tmp_path, base)
    assert without.exit_code == EXIT_UNSTABLE, without.output
    assert "FAIL prereg.require_external_anchor" in _flat(without.output)

    ca = tmp_path / "root.pem"
    ca.write_bytes(tsa.ca_pem)
    with_root = _gate_run(tmp_path, [*base, "--tsa-ca", str(ca)])
    assert "externally verified by rfc3161" in with_root.output, with_root.output


def test_the_gate_will_not_accept_local_as_an_approved_backend(tmp_path, sealed):
    record, receipt, ledger = sealed
    anchor = build_anchor(record, receipt, ledger)
    anchor["external_proofs"] = [{"backend": "local", "proof": None}]
    anchor_path = tmp_path / "anchor.json"
    anchor_path.write_text(json.dumps(anchor), encoding="utf-8")
    prereg_path = tmp_path / "prereg.json"
    prereg_path.write_text(_contract().to_json(), encoding="utf-8")

    result = _gate_run(tmp_path, [
        "gate", "--ledger", str(ledger), "--prereg", str(prereg_path),
        "--anchor", str(anchor_path), "--approved-backend", "local"])
    from evalseal.cli import EXIT_UNSTABLE

    assert result.exit_code == EXIT_UNSTABLE, result.output
    assert "FAIL prereg.require_external_anchor" in _flat(result.output)


# --- the diagnostic keeps its information without keeping its assurance label ---------

def test_anchor_verify_does_not_mark_an_unverified_imprint_as_required(sealed):
    """`anchor-verify` showed a required passing check for a proof whose signature was
    never checked. The prose said "NOT verified" beside a green required tick."""
    from evalseal.anchor import verify_anchor

    record, receipt, ledger = sealed
    tsa = build_tsa()
    anchor = build_anchor(record, receipt, ledger)
    anchor["external_proofs"] = [_rfc3161_proof(anchor, tsa)]

    by_name = {c.name: c for c in verify_anchor(anchor, record, ledger=ledger)}
    check = by_name["external proof (rfc3161)"]
    assert check.passed, "the imprint does match, and that stays visible"
    assert not check.required, "but an unverified signature is not a required pass"
    assert "NOT verified" in check.detail


def test_anchor_verify_still_fails_required_on_a_broken_proof(sealed):
    from evalseal.anchor import anchor_passed, verify_anchor

    record, receipt, ledger = sealed
    tsa = build_tsa()
    anchor = build_anchor(record, receipt, ledger)
    proof = _rfc3161_proof(anchor, tsa)
    proof["proof"] = base64.b64encode(
        build_token(tsa, anchor["subject_digest"], corrupt_signature=True)).decode()
    anchor["external_proofs"] = [proof]

    with temporary_backends(Rfc3161Backend(ca_pem=tsa.ca_pem)):
        checks = verify_anchor(anchor, record, ledger=ledger)
    by_name = {c.name: c for c in checks}
    assert not by_name["external proof (rfc3161)"].passed
    assert by_name["external proof (rfc3161)"].required
    assert not anchor_passed(checks)


# --- third-party backends, and malformed input ----------------------------------------

class _BoolOnlyBackend:
    """A backend written against the original protocol: it can only say ok/not ok."""

    name = "bool-only"

    def submit(self, subject_digest: str) -> dict:
        return {"backend": self.name, "proof": "x"}

    def check(self, subject_digest: str, proof: dict) -> tuple[bool, str]:
        return (True, "re-checked, and I am not telling you how far I got")


class _RaisingBackend:
    name = "explodes"

    def submit(self, subject_digest: str) -> dict:
        return {"backend": self.name}

    def check(self, subject_digest: str, proof: dict) -> tuple[bool, str]:
        raise RuntimeError("the authority's library threw")


def test_a_backend_that_only_returns_a_bool_cannot_reach_verified(sealed):
    """`check() -> True` means "re-checkable", not "verified". Capping it at
    imprint-only is what keeps a third-party adapter from asserting more than it
    established, even if its author approves it."""
    anchor = _anchor(sealed, [{"backend": "bool-only", "proof": "x"}])
    with temporary_backends(_BoolOnlyBackend()):
        evidence = _evidence(anchor, approved_backends=["bool-only"])
    assert evidence.state == "imprint_only"
    assert not evidence.satisfies_requirement
    assert "imprint-only" in evidence.detail
    checks = _by_rule(check_preregistration(
        _contract(), sealed[0], anchor=_verified_anchor(), external=evidence))
    assert not checks[CLAUSE].passed


def test_a_backend_that_raises_is_a_failure_not_a_crash(sealed):
    anchor = _anchor(sealed, [{"backend": "explodes"}])
    with temporary_backends(_RaisingBackend()):
        evidence = _evidence(anchor, approved_backends=["explodes"])
    assert evidence.state == "failed"
    assert not evidence.satisfies_requirement
    assert "RuntimeError" in evidence.detail


def test_a_non_string_backend_name_is_unsupported(sealed):
    evidence = _evidence(_anchor(sealed, [{"backend": 7}]))
    assert evidence.state == "unsupported"
    assert not evidence.satisfies_requirement


def test_an_anchor_that_cannot_be_digested_fails_rather_than_raising(sealed):
    """A gate must not abort on a malformed anchor; the clause has to fail visibly."""
    evidence = _evidence({"external_proofs": [{"backend": "rfc3161"}],
                          "unserialisable": {1, 2, 3}})
    assert evidence.state == "failed"
    assert "could not be digested" in evidence.detail


def test_trust_cannot_be_given_twice(sealed):
    from evalseal.verification import ExternalTrust, verify_external_evidence

    with pytest.raises(ValueError, match="either"):
        verify_external_evidence(
            _anchor(sealed, []), trust=ExternalTrust(), tsa_ca_pem=b"x")


def test_each_proof_result_serialises(sealed):
    tsa = build_tsa()
    anchor = _anchor(sealed, [])
    anchor["external_proofs"] = [{"backend": "local", "proof": None},
                                 _rfc3161_proof(anchor, tsa)]
    payload = _evidence(anchor, tsa_ca_pem=tsa.ca_pem).to_dict()
    assert len(payload["proofs"]) == 2
    for proof in payload["proofs"]:
        assert set(proof) == {"backend", "state", "approved",
                              "satisfies_requirement", "detail"}
    json.dumps(payload)
