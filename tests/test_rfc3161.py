"""The RFC 3161 backend: real DER both ways, no network, and no overclaiming.

Tokens here are built with the module's own encoder rather than recorded from a
service, so the tests run offline and in CI. That is a real limitation and it is the
point of `test_check_never_claims_the_signature_was_verified`: the thing EvalSeal
must never do is imply it validated a signature it cannot validate.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from tsa_fixture import build_token, build_tsa, build_tst_info

from evalseal.anchor import AnchorError, available_backends, get_backend
from evalseal.rfc3161 import (
    Rfc3161Backend,
    _integer,
    _oid,
    _sequence,
    _walk,
    build_timestamp_request,
    extract_tst_info,
    parse_gen_time,
    response_status,
)

DIGEST = "sha256:" + hashlib.sha256(b"subject").hexdigest()
OTHER = "sha256:" + hashlib.sha256(b"something else").hexdigest()


# One authority for the whole file. Tokens are now genuinely signed CMS rather than a
# hand-made envelope shaped to satisfy the old heuristic: `parse_timestamp_response`
# navigates the structure properly and correctly rejects anything that only looks like
# a token from the outside, which the previous double did.
_TSA = build_tsa()


def _tst_info(digest: str, gen_time: str = "20260923120000Z", serial: int = 42) -> bytes:
    """A TSTInfo with the structure RFC 3161 specifies."""
    return build_tst_info(digest, gen_time=_as_datetime(gen_time), serial=serial)


def _as_datetime(gen_time: str) -> datetime:
    return datetime.strptime(gen_time.rstrip("Z"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def _token(digest: str, **kwargs) -> bytes:
    """A full, signed TimeStampResp for `digest`."""
    if "gen_time" in kwargs and isinstance(kwargs["gen_time"], str):
        kwargs["gen_time"] = _as_datetime(kwargs["gen_time"])
    return build_token(_TSA, digest, **kwargs)


def _response(digest: str, status: int = 0, **kwargs) -> bytes:
    """The same thing: `build_token` already produces the TimeStampResp wrapper."""
    return _token(digest, status=status, **kwargs)


# --- the request ---------------------------------------------------------------------

def test_a_request_is_der_and_carries_the_digest():
    request = build_timestamp_request(DIGEST, nonce=7)
    assert request[0] == 0x30                                  # SEQUENCE
    assert bytes.fromhex(DIGEST.split(":", 1)[1]) in request   # the imprint is in there
    assert len(request) > 40


def test_a_request_is_byte_stable_for_a_fixed_nonce():
    assert build_timestamp_request(DIGEST, nonce=7) == build_timestamp_request(
        DIGEST, nonce=7)


def test_the_nonce_varies_by_default():
    """A nonce is what tells a fresh answer from a replayed one."""
    assert build_timestamp_request(DIGEST) != build_timestamp_request(DIGEST)


@pytest.mark.parametrize("bad", ["sha256:nothex", "sha256:abcd", "", "sha1:" + "aa" * 20])
def test_a_request_refuses_anything_that_is_not_a_sha256_digest(bad):
    with pytest.raises(AnchorError):
        build_timestamp_request(bad)


def test_oid_encoding_matches_the_known_sha256_identifier():
    # 2.16.840.1.101.3.4.2.1 encodes as 60 86 48 01 65 03 04 02 01.
    assert _oid("2.16.840.1.101.3.4.2.1") == bytes.fromhex("0609608648016503040201")


# --- reading a reply -------------------------------------------------------------------

def test_tst_info_is_found_inside_a_nested_token():
    info = extract_tst_info(_token(DIGEST))
    assert info is not None
    assert info["hashed_message"] == DIGEST
    assert info["gen_time"] == "20260923120000Z"
    assert info["serial_number"] == "42"


def test_a_granted_status_is_read():
    assert response_status(_response(DIGEST, status=0))[0] == 0
    assert response_status(_response(DIGEST, status=1))[0] == 1


def test_a_rejection_is_named():
    status, text = response_status(_response(DIGEST, status=2))
    assert status == 2 and text == "rejection"


def test_garbage_is_not_mistaken_for_a_token():
    assert extract_tst_info(b"\x00\x01\x02not der at all") is None
    assert extract_tst_info(b"") is None


def test_a_truncated_token_does_not_hang_or_throw():
    full = _token(DIGEST)
    for cut in (1, 5, len(full) // 2, len(full) - 1):
        assert extract_tst_info(full[:cut]) is None


@pytest.mark.parametrize("text,expected", [
    ("20260923120000Z", "2026-09-23T12:00:00+00:00"),
    ("20260923120000.123Z", "2026-09-23T12:00:00.123000+00:00"),
    ("not a time", None),
])
def test_gen_time_parsing(text, expected):
    assert parse_gen_time(text) == expected


# --- submit ------------------------------------------------------------------------------

def test_submit_refuses_without_an_authority():
    with pytest.raises(AnchorError) as e:
        Rfc3161Backend().submit(DIGEST)
    assert "--tsa" in str(e.value)
    assert "trust root" in str(e.value)          # says why there is no default


def test_submit_stores_the_token_and_the_asserted_time(monkeypatch):
    posted = {}

    def fake_post(url, **kwargs):
        posted["url"] = url
        posted["body"] = kwargs["content"]
        return httpx.Response(200, content=_response(DIGEST),
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    proof = Rfc3161Backend("https://tsa.example/tsr").submit(DIGEST)

    assert posted["url"] == "https://tsa.example/tsr"
    assert bytes.fromhex(DIGEST.split(":", 1)[1]) in posted["body"]   # digest only
    assert proof["backend"] == "rfc3161"
    assert proof["attested_time"] == "2026-09-23T12:00:00+00:00"
    assert proof["verified_by_evalseal"] is False
    assert base64.b64decode(proof["proof"])


def test_submit_sends_only_the_digest(monkeypatch):
    """The authority must never see the receipt or anything it hashes."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["body"] = kwargs["content"]
        return httpx.Response(200, content=_response(DIGEST),
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    Rfc3161Backend("https://tsa.example/tsr").submit(DIGEST)
    assert len(captured["body"]) < 128        # a request is tens of bytes, not a receipt


def test_submit_refuses_a_token_for_a_different_digest(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        200, content=_response(OTHER), request=httpx.Request("POST", url)))
    with pytest.raises(AnchorError) as e:
        Rfc3161Backend("https://tsa.example/tsr").submit(DIGEST)
    assert "timestamped a different digest" in str(e.value)


def test_submit_reports_a_refusal_rather_than_storing_nothing(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        200, content=_sequence(_sequence(_integer(2))),
        request=httpx.Request("POST", url)))
    with pytest.raises(AnchorError) as e:
        Rfc3161Backend("https://tsa.example/tsr").submit(DIGEST)
    assert "refused to timestamp" in str(e.value)


def test_submit_refuses_an_unreadable_reply_rather_than_recording_it(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(
        200, content=b"<html>error</html>", request=httpx.Request("POST", url)))
    with pytest.raises(AnchorError) as e:
        Rfc3161Backend("https://tsa.example/tsr").submit(DIGEST)
    assert "could not read as an" in str(e.value)


def test_a_network_failure_is_reported_as_one(monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(AnchorError) as e:
        Rfc3161Backend("https://tsa.example/tsr").submit(DIGEST)
    assert "failed" in str(e.value)


# --- check, offline ------------------------------------------------------------------------

def _proof(digest: str = DIGEST, **kwargs) -> dict:
    return {"backend": "rfc3161", "authority": "https://tsa.example/tsr",
            "proof": base64.b64encode(_token(digest, **kwargs)).decode()}


def test_check_confirms_the_token_is_about_this_digest():
    ok, detail = Rfc3161Backend().check(DIGEST, _proof())
    assert ok
    assert "matches this anchor's subject digest" in detail
    assert "2026-09-23T12:00:00+00:00" in detail
    # The boundary leads, before anything the token establishes.
    assert detail.startswith("message imprint checked, TSA signature NOT verified")


def test_check_never_claims_the_signature_was_verified():
    """The one thing this backend must not imply, in the exact agreed phrasing."""
    _, detail = Rfc3161Backend().check(DIGEST, _proof())
    assert "message imprint checked, TSA signature NOT verified by EvalSeal" in detail
    assert "EXPERIMENTAL" in detail
    assert "openssl ts -verify" in detail
    for overclaim in ("signature verified", "cryptographically verified", "proven"):
        assert overclaim not in detail.lower()


def test_check_fails_when_the_token_is_for_another_digest():
    ok, detail = Rfc3161Backend().check(DIGEST, _proof(OTHER))
    assert not ok
    assert "not this anchor's subject digest" in detail


def test_check_fails_on_a_missing_or_corrupt_token():
    assert Rfc3161Backend().check(DIGEST, {})[0] is False
    assert Rfc3161Backend().check(DIGEST, {"proof": "not base64!!"})[0] is False
    assert Rfc3161Backend().check(
        DIGEST, {"proof": base64.b64encode(b"junk").decode()})[0] is False


def test_check_makes_no_network_call(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("check() must work offline")

    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    assert Rfc3161Backend().check(DIGEST, _proof())[0]


# --- registration ---------------------------------------------------------------------------

def test_the_backend_is_registered_and_local_is_still_the_default():
    assert "rfc3161" in available_backends()
    assert isinstance(get_backend("rfc3161"), Rfc3161Backend)
    assert get_backend("local").name == "local"


def test_the_local_backend_still_says_it_attests_nothing():
    ok, detail = get_backend("local").check(DIGEST, {})
    assert ok
    assert "no external party attested this" in detail


# --- end to end, through an anchor ------------------------------------------------------

def test_an_anchor_carries_the_proof_and_verifies_offline(tmp_path, monkeypatch):
    """The whole path: seal, anchor --with rfc3161, then anchor-verify with no network."""
    from conftest import make_dataset, scripted

    from evalseal.adapters.scorer import RegexScorer
    from evalseal.adapters.target import LocalCallableTarget
    from evalseal.anchor import anchor_passed, build_anchor, verify_anchor
    from evalseal.executor import run_eval
    from evalseal.ledger import seal_and_append
    from evalseal.report import write_json

    record = run_eval(make_dataset("a"), LocalCallableTarget(scripted({"a": ["yes"]})),
                      RegexScorer(r"^yes$"), n_repeats=5)
    ledger = tmp_path / "ledger.jsonl"
    record = seal_and_append(record, ledger, relink=True)
    receipt = tmp_path / "report.json"
    write_json(record, receipt)

    # The authority answers about whatever digest it is asked for, read out of the
    # request the way a real TSA would rather than by guessing at byte offsets.
    def fake_post(url, **kwargs):
        body = kwargs["content"]
        imprint = next(body[b:e] for tag, b, e in _walk(body)
                       if tag == 0x04 and e - b == 32)
        return httpx.Response(200, content=_response("sha256:" + imprint.hex()),
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    from evalseal.anchor import register_backend

    register_backend(Rfc3161Backend("https://tsa.example/tsr"))
    try:
        anchor = build_anchor(record, receipt, ledger, backends=["rfc3161"])
    finally:
        register_backend(Rfc3161Backend())     # leave the registry as we found it

    assert len(anchor["external_proofs"]) == 1
    proof = anchor["external_proofs"][0]
    assert proof["subject_digest"] == anchor["subject_digest"]

    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("verify hit network"))
    checks = verify_anchor(anchor, record, ledger=ledger)
    assert anchor_passed(checks)
    proof_check = next(c for c in checks if "rfc3161" in c.name)
    assert proof_check.passed
    # Verified with no trust root, so the claim stays the smaller one.
    assert ("message imprint checked, TSA signature NOT verified by EvalSeal"
            in proof_check.detail)
    # The stored boundary describes the token at rest and must point at how to check
    # the signature, rather than reading as "this can never be verified".
    from evalseal.rfc3161 import BOUNDARY_AT_REST

    assert proof["boundary"] == BOUNDARY_AT_REST
    assert "--tsa-ca" in proof["boundary"]
    assert proof["verified_by_evalseal"] is False


def test_a_tampered_anchor_breaks_the_proof_binding(tmp_path, monkeypatch):
    """Editing the anchor changes its subject digest, so the token no longer matches."""
    proof = _proof()
    anchor = {"subject_digest": OTHER, "external_proofs": [proof]}
    ok, detail = get_backend("rfc3161").check(anchor["subject_digest"], proof)
    assert not ok
    assert "not this anchor's subject digest" in detail


# --- the import cycle, which shipped broken in 2.2.0 ------------------------------------

@pytest.mark.parametrize("snippet", [
    # The order that failed: rfc3161 before anchor, in a fresh interpreter.
    "from evalseal.rfc3161 import Rfc3161Backend",
    # And this failed too, because evalseal/__init__ does not import anchor.
    "import evalseal; from evalseal.rfc3161 import Rfc3161Backend",
    "import evalseal.rfc3161; from evalseal.anchor import available_backends",
    "from evalseal.anchor import available_backends; available_backends()",
])
def test_every_import_order_works(snippet):
    """Registering the backend at anchor.py's module scope made the cycle resolvable
    only when `anchor` happened to be imported first, so
    `from evalseal.rfc3161 import Rfc3161Backend` raised a partially-initialized-module
    ImportError. A subprocess, because the cycle only bites on a cold import."""
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def test_the_registry_is_complete_however_it_is_reached():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c",
         "from evalseal.anchor import available_backends; print(available_backends())"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "rfc3161" in proc.stdout and "local" in proc.stdout


def test_a_caller_registered_instance_is_not_clobbered_by_the_lazy_load():
    """`--tsa URL` registers a configured backend; first use of the registry must not
    replace it with an unconfigured one."""
    from evalseal import anchor as anchor_module

    saved, saved_flag = dict(anchor_module._BACKENDS), anchor_module._BUILTINS_LOADED
    try:
        anchor_module._BACKENDS.clear()
        anchor_module._BACKENDS["local"] = saved["local"]
        anchor_module._BUILTINS_LOADED = False
        configured = Rfc3161Backend("https://tsa.example/tsr")
        anchor_module.register_backend(configured)
        assert anchor_module.get_backend("rfc3161") is configured
        assert anchor_module.get_backend("rfc3161").url == "https://tsa.example/tsr"
    finally:
        anchor_module._BACKENDS.clear()
        anchor_module._BACKENDS.update(saved)
        anchor_module._BUILTINS_LOADED = saved_flag


# --- CMS signature validation: the matrix a real authority cannot be asked for ---------

def _verify(token: bytes, digest: str = DIGEST, *, ca: bytes | None = None):
    from evalseal.rfc3161 import verify_token

    return verify_token(token, digest, ca_pem=_TSA.ca_pem if ca is None else ca)


def _failing(result) -> list[str]:
    return [c.name for c in result.checks if c.ran and not c.passed]


def test_a_valid_token_reaches_verified():
    from evalseal.rfc3161 import BOUNDARY_VERIFIED, VERIFIED

    result = _verify(_token(DIGEST))
    assert result.state == VERIFIED
    assert result.passed
    assert _failing(result) == []
    assert result.boundary == BOUNDARY_VERIFIED
    assert "revocation not checked" in result.boundary


def test_without_a_trust_root_the_claim_stays_what_it_always_was():
    """A caller who supplies no root must get the old, smaller claim - not a silent
    upgrade, and not a failure either."""
    from evalseal.rfc3161 import BOUNDARY_IMPRINT_ONLY, IMPRINT_ONLY, verify_token

    result = verify_token(_token(DIGEST), DIGEST)
    assert result.state == IMPRINT_ONLY
    assert result.boundary == BOUNDARY_IMPRINT_ONLY
    assert "NOT verified by EvalSeal" in result.boundary
    # The signature check is reported as not run, never as passed.
    signature = next(c for c in result.checks if c.name == "signature")
    assert signature.ran is False
    assert "no trust root supplied" in signature.detail


@pytest.mark.parametrize("kwargs,expected_check", [
    ({"corrupt_signature": True}, "signature"),
    ({"wrong_message_digest": True}, "message_digest_attribute"),
    ({"econtent_type": "1.2.3.4.5"}, "econtent_type"),
    ({"include_certificate": False}, "signing_certificate"),
    ({"status": 2}, "status"),
])
def test_each_tampering_fails_exactly_one_named_check(kwargs, expected_check):
    """A matrix where every failure said "invalid" would teach nothing."""
    from evalseal.rfc3161 import FAILED

    result = _verify(_token(DIGEST, **kwargs))
    assert result.state == FAILED
    assert expected_check in _failing(result)


def test_a_token_for_another_digest_fails_the_imprint_check():
    from evalseal.rfc3161 import FAILED

    result = _verify(_token(OTHER))
    assert result.state == FAILED
    assert _failing(result) == ["message_imprint"]


def test_a_signature_from_a_foreign_key_fails():
    """The certificate is the authority's; the signature is not."""
    foreign = build_tsa()
    result = _verify(_token(DIGEST, sign_with=foreign.tsa_key))
    assert "signature" in _failing(result)


def test_a_certificate_from_an_untrusted_root_fails_the_chain():
    result = _verify(_token(DIGEST), ca=build_tsa().ca_pem)
    assert _failing(result) == ["certificate_chain"]
    chain = next(c for c in result.checks if c.name == "certificate_chain")
    assert "not issued by any root you supplied" in chain.detail
    assert "no path building" in chain.detail


def test_a_certificate_without_the_timestamping_eku_fails():
    tsa = build_tsa(timestamping_eku=False)
    result = _verify(build_token(tsa, DIGEST), ca=tsa.ca_pem)
    assert "timestamping_eku" in _failing(result)
    eku = next(c for c in result.checks if c.name == "timestamping_eku")
    assert "no extendedKeyUsage" in eku.detail


def test_a_non_critical_timestamping_eku_fails():
    """RFC 3161 requires it critical: a non-critical EKU may be ignored by a verifier,
    which defeats the point of requiring it."""
    tsa = build_tsa(eku_critical=False)
    result = _verify(build_token(tsa, DIGEST), ca=tsa.ca_pem)
    assert "timestamping_eku" in _failing(result)
    eku = next(c for c in result.checks if c.name == "timestamping_eku")
    assert "not marked critical" in eku.detail


def test_a_gen_time_outside_the_certificate_validity_fails():
    from datetime import timedelta

    from tsa_fixture import GEN_TIME

    tsa = build_tsa(not_before=GEN_TIME + timedelta(days=10),
                    not_after=GEN_TIME + timedelta(days=20))
    result = _verify(build_token(tsa, DIGEST), ca=tsa.ca_pem)
    assert "validity" in _failing(result)
    assert "outside the signing certificate's validity" in next(
        c for c in result.checks if c.name == "validity").detail


def test_validity_is_checked_against_gen_time_not_the_clock_now():
    """A recorded fixture's certificate expires. A verifier that used `now` would start
    failing on a date nobody chose, so the asserted time is what gets checked."""
    from datetime import timedelta

    from tsa_fixture import GEN_TIME

    # A certificate that was valid when the token was made and has long since expired.
    tsa = build_tsa(not_before=GEN_TIME - timedelta(days=2),
                    not_after=GEN_TIME + timedelta(days=2))
    result = _verify(build_token(tsa, DIGEST), ca=tsa.ca_pem)
    assert result.state == "verified", _failing(result)


def test_garbage_fails_the_structure_check_rather_than_crashing():
    from evalseal.rfc3161 import FAILED

    for blob in (b"", b"\x00\x01\x02", b"<html>error</html>"):
        result = _verify(blob)
        assert result.state == FAILED
        assert _failing(result) == ["structure"]


def test_the_result_serialises_for_ci():
    payload = _verify(_token(DIGEST)).to_dict()
    assert set(payload) == {"state", "boundary", "passed", "checks"}
    assert all(set(c) == {"name", "passed", "ran", "detail"} for c in payload["checks"])
    json.dumps(payload)


def test_no_state_claims_more_than_it_checked():
    from evalseal.rfc3161 import BOUNDARY_FAILED, BOUNDARY_IMPRINT_ONLY, verify_token

    verified = _verify(_token(DIGEST)).boundary
    imprint = verify_token(_token(DIGEST), DIGEST).boundary
    failed = _verify(_token(DIGEST, corrupt_signature=True)).boundary

    assert verified != imprint != failed
    assert "revocation not checked" in verified      # names what it skipped
    assert "NOT verified" in imprint
    assert failed == BOUNDARY_FAILED
    for boundary in (verified, imprint, failed):
        assert "proven" not in boundary.lower()
        assert "proof of" not in boundary.lower()
    assert BOUNDARY_IMPRINT_ONLY == (
        "message imprint checked, TSA signature NOT verified by EvalSeal")


def test_the_backend_check_reports_verified_when_a_root_is_configured():
    ok, detail = Rfc3161Backend(ca_pem=_TSA.ca_pem).check(DIGEST, _proof())
    assert ok
    assert "signature verified against the certificate you supplied" in detail
    assert "Revocation was not checked" in detail


def test_the_backend_check_fails_loudly_on_a_tampered_token():
    ok, detail = Rfc3161Backend(ca_pem=_TSA.ca_pem).check(
        DIGEST, {"proof": base64.b64encode(
            _token(DIGEST, corrupt_signature=True)).decode()})
    assert not ok
    assert detail.startswith("verification FAILED")
    assert "does not verify against the signing certificate" in detail


def test_the_trust_root_is_never_read_from_the_proof():
    """A root carried inside the anchor would be a root the author chose, which defeats
    the point of an external attestation."""
    import inspect

    from evalseal import rfc3161

    source = inspect.getsource(rfc3161.Rfc3161Backend.check)
    assert 'proof.get("ca' not in source
    assert "proof['ca" not in source
    assert "self.ca_pem" in source


# --- the anchoring posture, enforced rather than only documented ------------------------

def test_no_public_log_backend_is_registered():
    """A transparency log entry is public and irrevocable. If one is ever implemented,
    choosing it must be an explicit act - not a default, not a fallback, and not
    something `--with external` quietly resolves to. This test fails the day a backend
    named for a public log appears in the registry, so that decision cannot be made by
    accident."""
    from evalseal.anchor import available_backends

    assert available_backends() == ["local", "rfc3161"]
    for public in ("rekor", "sigstore", "opentimestamps", "ots", "external"):
        assert public not in available_backends()


def test_the_default_backend_publishes_nothing():
    """`anchor` with no `--with` uses the local backend, which contacts nobody."""
    from evalseal.anchor import get_backend

    local = get_backend("local")
    ok, detail = local.check(DIGEST, {})
    assert ok
    assert "no external party attested this" in detail


def test_the_privacy_section_makes_the_cadence_argument():
    """The leak that matters for a public log is cadence and existence, not content.
    If this section is ever trimmed to the digest-guessing warning alone, it has lost
    the part a user actually needs."""
    doc = (Path(__file__).resolve().parent.parent
           / "docs" / "external-anchoring.md").read_text(encoding="utf-8")
    lowered = " ".join(doc.lower().split())

    assert "leaks cadence and existence, not content" in lowered
    assert "irrevocable" in lowered
    assert "never selected for you" in lowered
    assert "rfc 3161 is the default" in lowered
    # And the comparison a reader decides from.
    assert "choosing between them" in lowered
    assert "everyone, permanently" in lowered


def test_the_privacy_section_no_longer_claims_the_cassette_is_unsealed():
    """It said "a receipt does not currently seal the cassette's hash", which has been
    false since schema 1.4."""
    doc = (Path(__file__).resolve().parent.parent
           / "docs" / "external-anchoring.md").read_text(encoding="utf-8")
    lowered = " ".join(doc.lower().split())
    assert "does not currently seal the cassette" not in lowered
    assert "responses are not in the receipt yet" not in lowered
    assert "the cassette is bound into the record by digest" in lowered


# --- algorithms a real authority actually uses -----------------------------------------

@pytest.mark.parametrize("signature_kind", ["rsa", "ecdsa"])
@pytest.mark.parametrize("digest_algorithm", ["sha256", "sha512"])
def test_every_supported_algorithm_combination_verifies(signature_kind,
                                                        digest_algorithm):
    """SHA-256 with RSA was the only combination this verifier handled, which is not
    what the world uses: freetsa.org signs with ECDSA and SHA-512, and a recorded
    token from it failed twice - a wrong-length messageDigest and an unrecognised
    signature algorithm. Both are now read from the token."""
    from evalseal.rfc3161 import VERIFIED, verify_token

    tsa = build_tsa(key_kind=signature_kind)
    token = build_token(tsa, DIGEST, digest_algorithm=digest_algorithm,
                        signature_kind=signature_kind)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    failing = [c.name for c in result.checks if c.ran and not c.passed]
    assert result.state == VERIFIED, failing


def test_the_reported_algorithm_is_the_one_that_was_used():
    """The detail must name what actually verified, not a hardcoded guess."""
    from evalseal.rfc3161 import verify_token

    tsa = build_tsa(key_kind="ecdsa")
    result = verify_token(
        build_token(tsa, DIGEST, digest_algorithm="sha512", signature_kind="ecdsa"),
        DIGEST, ca_pem=tsa.ca_pem)
    signature = next(c for c in result.checks if c.name == "signature")
    assert "ECDSA with SHA512" in signature.detail
    digest = next(c for c in result.checks if c.name == "message_digest_attribute")
    assert "SHA512 digest of eContent" in digest.detail


def test_an_unknown_signature_algorithm_is_named_rather_than_assumed():
    from evalseal.rfc3161 import _SIGNATURES, _algorithm_name, _der, _oid

    # An AlgorithmIdentifier for something not in the table.
    unknown = _der(0x30, _oid("1.2.3.4.5.6.7"))
    assert _algorithm_name(unknown, _SIGNATURES) is None


def test_the_algorithm_lookup_tolerates_a_missing_parameters_field():
    """RSA carries an explicit NULL and ECDSA omits parameters entirely, so comparing
    whole AlgorithmIdentifiers byte for byte would miss one of them."""
    from evalseal.rfc3161 import _SIGNATURES, _algorithm_name, _der, _oid

    with_null = _der(0x30, _oid("1.2.840.113549.1.1.11") + _der(0x05, b""))
    without = _der(0x30, _oid("1.2.840.10045.4.3.4"))
    assert _algorithm_name(with_null, _SIGNATURES) == ("rsa", "sha256")
    assert _algorithm_name(without, _SIGNATURES) == ("ecdsa", "sha512")
