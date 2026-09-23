"""The RFC 3161 backend: real DER both ways, no network, and no overclaiming.

Tokens here are built with the module's own encoder rather than recorded from a
service, so the tests run offline and in CI. That is a real limitation and it is the
point of `test_check_never_claims_the_signature_was_verified`: the thing EvalSeal
must never do is imply it validated a signature it cannot validate.
"""
from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from evalseal.anchor import AnchorError, available_backends, get_backend
from evalseal.rfc3161 import (
    Rfc3161Backend,
    _der,
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


def _tst_info(digest: str, gen_time: str = "20260923120000Z", serial: int = 42) -> bytes:
    """A TSTInfo with the structure RFC 3161 specifies."""
    raw = bytes.fromhex(digest.split(":", 1)[1])
    imprint = _sequence(
        _sequence(_oid("2.16.840.1.101.3.4.2.1"), _der(0x05, b"")), _der(0x04, raw))
    return _sequence(
        _integer(1),                                   # version
        _oid("1.2.3.4.1"),                             # policy
        imprint,
        _integer(serial),
        _der(0x18, gen_time.encode("ascii")),          # genTime
    )


def _token(digest: str, **kwargs) -> bytes:
    """A TSTInfo wrapped the way a real token nests it: inside CMS-ish envelopes.

    The parser finds TSTInfo by shape rather than by navigating CMS, so the exact
    envelope does not matter - which is what makes this test double legitimate rather
    than a reimplementation of what is being tested.
    """
    return _sequence(
        _oid("1.2.840.113549.1.7.2"),                  # id-signedData
        _der(0xA0, _sequence(
            _integer(1),
            _der(0x31, b""),
            _sequence(_oid("1.2.840.113549.1.9.16.1.4"),
                      _der(0xA0, _der(0x04, _tst_info(digest, **kwargs)))),
        )),
    )


def _response(digest: str, status: int = 0, **kwargs) -> bytes:
    return _sequence(_sequence(_integer(status)), _token(digest, **kwargs))


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
    assert "about this anchor's subject digest" in detail
    assert "2026-09-23T12:00:00+00:00" in detail


def test_check_never_claims_the_signature_was_verified():
    """The one thing this backend must not imply."""
    _, detail = Rfc3161Backend().check(DIGEST, _proof())
    assert "did not verify the authority's signature" in detail
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
    assert "did not verify the authority's signature" in proof_check.detail


def test_a_tampered_anchor_breaks_the_proof_binding(tmp_path, monkeypatch):
    """Editing the anchor changes its subject digest, so the token no longer matches."""
    proof = _proof()
    anchor = {"subject_digest": OTHER, "external_proofs": [proof]}
    ok, detail = get_backend("rfc3161").check(anchor["subject_digest"], proof)
    assert not ok
    assert "not this anchor's subject digest" in detail
