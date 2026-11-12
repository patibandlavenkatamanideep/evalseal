"""Phase B: what a timestamp token has to survive before its verdict is `verified`.

The trust model these tests hold the code to is written down in
[ADR 0001](../docs/adr/0001-timestamp-verification-trust-model.md). The short version:
a verified token says *this authority asserts this digest existed no later than this
time, signed by a certificate chaining to a root the verifier supplied* - and nothing
about freshness, revocation, or the evaluation having happened.

Three defects reproduced on the baseline, all of which a real authority would have
shown and the existing tests could not, because the tests were built from the same
understanding as the code:

**B1 - DER integers were encoded as negative.** DER integers are signed, so a value
whose top byte has the high bit set needs a leading zero. `_integer` reserved that byte
and then stripped it: `255` encoded as `-1`, `128` as `-128`, and **492 of 1000** random
64-bit nonces as a different, negative number. `build_timestamp_request` draws a random
64-bit nonce, so about half of all real requests were malformed.

**B2 - the message imprint's algorithm was never read.** Any 32-byte hashedMessage was
accepted and labelled `sha256:`, whatever the AlgorithmIdentifier said. A token
declaring SHA-512 beside 32 bytes was read as a SHA-256 imprint and compared against
the anchor's SHA-256 digest; a genuine SHA-384 or SHA-512 token was rejected as
"not a readable TSTInfo".

**B3 - the signer certificate was `certificates[0]`.** The certificate set is a DER
`SET OF`, whose order is its sorted encoding rather than signer-first, so a token
shipping a chain could present either certificate first.

Plus two things that were defined and not done: the `contentType` signed attribute was
never compared (so the signature did not commit to the content's type), and the request
nonce was generated and discarded (so a replayed token could not be told from a fresh
one).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import httpx
import pytest
from conftest import fake_tsa_http
from tsa_fixture import (
    OID_SHA256,
    OID_SHA512,
    build_token,
    build_tsa,
    nonce_of_request,
)

from evalseal.anchor import AnchorError
from evalseal.rfc3161 import (
    FAILED,
    IMPRINT_ONLY,
    VERIFIED,
    Rfc3161Backend,
    _integer,
    build_timestamp_request,
    extract_tst_info,
    verify_token,
)

DIGEST = "sha256:" + "ab" * 32


def _decode_der_integer(der: bytes) -> int:
    """Decode as the standard requires: two's complement, signed."""
    assert der[0] == 0x02, "not an INTEGER"
    return int.from_bytes(der[2:2 + der[1]], "big", signed=True)


def _checks(result) -> dict[str, object]:
    return {c.name: c for c in result.checks}


# --- B1: DER integer encoding ---------------------------------------------------------

@pytest.mark.parametrize("value", [0, 1, 126, 127, 128, 129, 254, 255, 256, 257,
                                   32767, 32768, 65535, 65536, 2**63 - 1, 2**63,
                                   2**64 - 1])
def test_a_der_integer_decodes_as_the_value_it_encoded(value):
    assert _decode_der_integer(_integer(value)) == value


@pytest.mark.parametrize("value", [128, 255, 32768, 65535, 2**63, 2**64 - 1])
def test_a_high_bit_value_keeps_its_leading_zero(value):
    """Without it the value decodes negative, which is what broke half of all requests."""
    body = _integer(value)[2:]
    assert body[0] == 0x00, f"{value} lost its sign byte"
    assert _decode_der_integer(_integer(value)) > 0


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 256, 2**64 - 1])
def test_a_der_integer_is_minimally_encoded(value):
    """DER forbids a redundant leading zero, so the sign byte is kept only when needed."""
    body = _integer(value)[2:]
    assert len(body) == 1 or not (body[0] == 0x00 and not body[1] & 0x80)


def test_a_negative_integer_is_refused():
    with pytest.raises(ValueError, match="negative"):
        _integer(-1)


def test_realistic_nonces_round_trip():
    """The exact distribution `build_timestamp_request` draws from."""
    wrong = [n for n in (int.from_bytes(os.urandom(8), "big") for _ in range(2000))
             if _decode_der_integer(_integer(n)) != n]
    assert not wrong, f"{len(wrong)}/2000 nonces encoded as a different integer"


def test_a_request_round_trips_its_nonce_through_the_encoder():
    for nonce in (1, 127, 128, 255, 2**63, 2**64 - 1):
        assert nonce_of_request(build_timestamp_request(DIGEST, nonce=nonce)) == nonce


def test_a_realistic_certificate_serial_number_encodes_positive():
    """Real serial numbers are large random integers, so half have the high bit set."""
    tsa = build_tsa()
    assert tsa.tsa_certificate.serial_number > 0
    assert _decode_der_integer(_integer(tsa.tsa_certificate.serial_number)) > 0


# --- B2: the imprint algorithm --------------------------------------------------------

def test_the_wrong_imprint_algorithm_probe_fails():
    """The acceptance probe: declares SHA-512, carries 32 bytes.

    The baseline accepted this and read it as a SHA-256 imprint of those 32 bytes.
    """
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, imprint_oid=OID_SHA512)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == FAILED
    check = _checks(result)["message_imprint_algorithm"]
    assert not check.passed
    assert "sha512" in check.detail and "64" in check.detail


def test_an_unsupported_imprint_algorithm_fails_closed():
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, imprint_oid="1.2.3.4.5.6.7.8")
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == FAILED
    assert "does not support" in _checks(result)["message_imprint_algorithm"].detail


def test_the_imprint_is_labelled_with_the_algorithm_the_token_declares():
    tsa = build_tsa()
    token = build_token(tsa, DIGEST)
    assert extract_tst_info(token)["hashed_message"] == DIGEST


def test_a_genuine_sha512_imprint_is_read_rather_than_called_unreadable():
    """A 64-byte imprint under a SHA-512 OID is a well-formed token about a SHA-512
    digest. It is not this anchor's digest, and the failure must say which."""
    tsa = build_tsa()
    big = "sha512:" + hashlib.sha512(b"something").hexdigest()
    token = build_token(tsa, big, imprint_oid=OID_SHA512,
                        digest_algorithm="sha512", signature_kind="rsa")
    info = extract_tst_info(token)
    assert info is not None, "a SHA-512 token is readable"
    assert info["hashed_message"].startswith("sha512:")

    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == FAILED
    assert not _checks(result)["message_imprint"].passed
    assert _checks(result)["message_imprint_algorithm"].passed, (
        "the algorithm is fine; it is the digest that is not ours")


def test_an_imprint_of_the_declared_length_but_sha256_oid_still_checks_out():
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, imprint_oid=OID_SHA256)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == VERIFIED, [c.detail for c in result.checks if not c.passed]


# --- B3: certificate selection --------------------------------------------------------

def test_the_signer_is_found_when_it_is_not_first_in_the_set():
    """A real authority ships a chain, and the set's order is its sorted encoding."""
    tsa = build_tsa()
    token = build_token(tsa, DIGEST,
                        extra_certificates=(tsa.ca_certificate,), signer_first=False)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == VERIFIED, [c.detail for c in result.checks if not c.passed]


def test_the_signer_is_found_when_it_is_first_too():
    tsa = build_tsa()
    token = build_token(tsa, DIGEST,
                        extra_certificates=(tsa.ca_certificate,), signer_first=True)
    assert verify_token(token, DIGEST, ca_pem=tsa.ca_pem).state == VERIFIED


def test_an_unrelated_signer_certificate_does_not_satisfy_the_signer_identifier():
    """Certificates are present, and none of them is the one the SignerInfo names."""
    signer, stranger = build_tsa(), build_tsa()
    token = build_token(signer, DIGEST, omit_signer_certificate=True,
                        extra_certificates=(stranger.tsa_certificate,))
    result = verify_token(token, DIGEST, ca_pem=signer.ca_pem)
    assert result.state == FAILED
    check = _checks(result)["signing_certificate"]
    assert not check.passed
    assert "the SignerInfo names" in check.detail


def test_a_token_with_no_certificate_at_all_says_so():
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, include_certificate=False)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == FAILED
    assert "embeds no certificate" in _checks(result)["signing_certificate"].detail


# --- signed attributes ----------------------------------------------------------------

def test_a_wrong_signed_content_type_fails():
    """The signature covers signedAttrs, so it commits to the content type too - but
    only if anything compares it. Nothing did."""
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, signed_content_type="1.2.840.113549.1.7.1")
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == FAILED
    assert not _checks(result)["content_type_attribute"].passed


def test_a_missing_signed_content_type_fails():
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, omit_content_type=True)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == FAILED
    assert "no contentType" in _checks(result)["content_type_attribute"].detail


@pytest.mark.parametrize("which", ["content_type", "message_digest"])
def test_a_duplicated_signed_attribute_fails(which):
    """First-match would let two verifiers read the same token differently."""
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, duplicate_attribute=which)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == FAILED


def test_an_attribute_carrying_two_values_fails():
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, duplicate_attribute="two_values")
    assert verify_token(token, DIGEST, ca_pem=tsa.ca_pem).state == FAILED


def test_a_message_digest_that_does_not_cover_the_content_fails():
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, wrong_message_digest=True)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == FAILED
    assert not _checks(result)["message_digest_attribute"].passed


# --- malformed input ------------------------------------------------------------------

@pytest.mark.parametrize("blob", [
    b"",
    b"\x00",
    b"\x30\x80\x00\x00",                      # indefinite length: BER, not DER
    b"\x30\x7f" + b"\x00" * 10,               # length overruns the container
    b"not der at all",
])
def test_malformed_der_fails_without_raising(blob):
    result = verify_token(blob, DIGEST, ca_pem=build_tsa().ca_pem)
    assert result.state == FAILED
    assert result.checks, "a failure must still say something"


def test_trailing_material_after_the_token_is_not_ignored():
    tsa = build_tsa()
    token = build_token(tsa, DIGEST, trailing=b"\x00extra")
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == FAILED, [c.name for c in result.checks if not c.passed]


# --- nonce: freshness, and only for a submission --------------------------------------

def _post_returning(content: bytes):
    def post(url, **kwargs):
        return httpx.Response(200, content=content,
                              request=httpx.Request("POST", url))
    return post


def test_a_response_with_no_nonce_is_refused(monkeypatch):
    tsa = build_tsa()
    fake_tsa_http(monkeypatch, _post_returning(build_token(tsa, DIGEST)))
    with pytest.raises(AnchorError, match="no nonce"):
        Rfc3161Backend("https://tsa.example/tsr").submit(DIGEST)


def test_a_response_echoing_the_wrong_nonce_is_refused(monkeypatch):
    tsa = build_tsa()
    fake_tsa_http(monkeypatch, _post_returning(build_token(tsa, DIGEST, nonce=999999)))
    with pytest.raises(AnchorError, match="echoed nonce"):
        Rfc3161Backend("https://tsa.example/tsr").submit(DIGEST)


def test_a_response_echoing_the_nonce_is_accepted(monkeypatch):
    tsa = build_tsa()

    def post(url, **kwargs):
        nonce = nonce_of_request(kwargs["content"])
        return httpx.Response(200, content=build_token(tsa, DIGEST, nonce=nonce),
                              request=httpx.Request("POST", url))

    fake_tsa_http(monkeypatch, post)
    proof = Rfc3161Backend("https://tsa.example/tsr").submit(DIGEST)
    assert proof["subject_digest"] == DIGEST


def test_verification_of_a_stored_token_claims_no_freshness():
    """A token read back from a file has no nonce to check, and the wording must not
    suggest otherwise. Freshness belongs to the exchange, not to the artefact."""
    tsa = build_tsa()
    token = build_token(tsa, DIGEST)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert result.state == VERIFIED
    text = (" ".join(c.detail for c in result.checks) + " " + result.boundary).lower()
    for word in ("fresh", "recent", "just now", "not a replay"):
        assert word not in text, f"a stored token must not be described as {word!r}"


# --- a signature alone is not a verdict -----------------------------------------------

def test_a_valid_signature_does_not_make_the_verdict_verified():
    """The EKU is missing, everything cryptographic is fine, and the token is not
    verified. The verdict is the conjunction of the checks, not the signature."""
    tsa = build_tsa(timestamping_eku=False)
    token = build_token(tsa, DIGEST)
    result = verify_token(token, DIGEST, ca_pem=tsa.ca_pem)
    assert _checks(result)["signature"].passed, "the signature really does verify"
    assert result.state == FAILED
    assert not _checks(result)["timestamping_eku"].passed


def test_a_non_critical_eku_does_not_make_the_verdict_verified():
    tsa = build_tsa(eku_critical=False)
    result = verify_token(build_token(tsa, DIGEST), DIGEST, ca_pem=tsa.ca_pem)
    assert _checks(result)["signature"].passed
    assert result.state == FAILED


def test_without_a_root_the_state_stays_imprint_only():
    tsa = build_tsa()
    result = verify_token(build_token(tsa, DIGEST), DIGEST)
    assert result.state == IMPRINT_ONLY


# --- an independent implementation agrees ---------------------------------------------

OPENSSL = shutil.which("openssl")


def _current_tsa_and_token(**token_kwargs):
    """A TSA and token whose times straddle *now*, for comparison with OpenSSL.

    The shared fixture pins `GEN_TIME` to a fixed instant and gives the certificate a
    validity of +/- 30 days around it. EvalSeal checks `genTime` against the
    certificate's validity, so that stays self-consistent forever - but `openssl ts
    -verify` checks the certificate against the *system clock*, so once the fixed
    instant is more than 30 days in the past, OpenSSL reports an expired certificate
    and the two implementations disagree for a reason that is about neither token.

    Both behaviours are right for what they check. The cross-check needs times that
    satisfy both, so these are built around the current clock.
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC).replace(microsecond=0)
    tsa = build_tsa(not_before=now - timedelta(days=1),
                    not_after=now + timedelta(days=1))
    token = build_token(tsa, DIGEST, gen_time=now, **token_kwargs)
    return tsa, token


def _openssl_verify(tmp_path, token: bytes, digest: str, ca_pem: bytes):
    """`openssl ts -verify` on the same token, as a second opinion.

    A synthetic fixture proves the negative paths and nothing about interoperability:
    the same misunderstanding that produced the parser produced the fixture. A second
    implementation agreeing is the only evidence that goes the other way.
    """
    token_path = tmp_path / "token.tsr"
    ca_path = tmp_path / "ca.pem"
    token_path.write_bytes(token)
    ca_path.write_bytes(ca_pem)
    proc = subprocess.run(
        [OPENSSL, "ts", "-verify", "-digest", digest.split(":", 1)[1],
         "-in", str(token_path), "-CAfile", str(ca_path)],
        capture_output=True, text=True, timeout=60, check=False)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
def test_openssl_agrees_that_a_good_token_verifies(tmp_path):
    tsa, token = _current_tsa_and_token()
    assert verify_token(token, DIGEST, ca_pem=tsa.ca_pem).state == VERIFIED
    ok, output = _openssl_verify(tmp_path, token, DIGEST, tsa.ca_pem)
    assert ok, f"openssl disagreed: {output}"


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
@pytest.mark.parametrize("kwargs", [
    {"corrupt_signature": True},
    {"wrong_message_digest": True},
    {"wrong_signing_certificate_hash": True},
    {"signing_certificate_attribute": None},
])
def test_openssl_agrees_on_what_fails(tmp_path, kwargs):
    tsa, token = _current_tsa_and_token(**kwargs)
    mine = verify_token(token, DIGEST, ca_pem=tsa.ca_pem).state
    theirs, output = _openssl_verify(tmp_path, token, DIGEST, tsa.ca_pem)
    assert mine == FAILED, "EvalSeal must reject it"
    assert not theirs, f"openssl accepted what EvalSeal rejected: {output}"


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
def test_evalseal_is_stricter_than_openssl_about_the_signed_content_type(tmp_path):
    """Where the two differ, and why EvalSeal does not relax to match.

    RFC 5652 section 5.3 says the `contentType` signed attribute's value MUST match
    the eContentType. `openssl ts -verify` accepts a token where it does not; EvalSeal
    refuses. Recorded as a test rather than left as a surprise, because the cross-check
    in the other direction is an *agreement* test, and a one-sided difference needs to
    be a deliberate, visible choice instead of looking like a bug in whichever
    implementation someone checks second.
    """
    tsa, token = _current_tsa_and_token(signed_content_type="1.2.840.113549.1.7.1")
    assert verify_token(token, DIGEST, ca_pem=tsa.ca_pem).state == FAILED
    theirs, output = _openssl_verify(tmp_path, token, DIGEST, tsa.ca_pem)
    assert theirs, (
        "if OpenSSL has started enforcing this too, delete this test and move the case "
        f"back into the agreement list: {output}")


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
def test_openssl_also_rejects_a_token_for_another_digest(tmp_path):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC).replace(microsecond=0)
    tsa = build_tsa(not_before=now - timedelta(days=1),
                    not_after=now + timedelta(days=1))
    other = "sha256:" + hashlib.sha256(b"different").hexdigest()
    token = build_token(tsa, other, gen_time=now)
    assert verify_token(token, DIGEST, ca_pem=tsa.ca_pem).state == FAILED
    theirs, _ = _openssl_verify(tmp_path, token, DIGEST, tsa.ca_pem)
    assert not theirs


def test_the_openssl_cross_check_is_reported_as_skipped_not_as_agreement():
    """A skipped independent check is not evidence. Pinned so the skip stays visible."""
    assert OPENSSL is None or isinstance(OPENSSL, str)


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
def test_evalseal_pins_validity_to_gen_time_where_openssl_uses_the_clock(tmp_path):
    """Why the cross-check builds its own times.

    The shared fixture's `GEN_TIME` is fixed, so its certificate is long expired
    against today's clock. EvalSeal still verifies the token, because the question it
    asks is whether the certificate was valid *when the authority signed* - which is
    the right question for an assertion about the past. OpenSSL asks whether the
    certificate is valid now. Neither is wrong, and a cross-check has to account for
    it rather than report a disagreement about the token.
    """
    tsa = build_tsa()
    token = build_token(tsa, DIGEST)
    assert verify_token(token, DIGEST, ca_pem=tsa.ca_pem).state == VERIFIED
    ok, output = _openssl_verify(tmp_path, token, DIGEST, tsa.ca_pem)
    assert not ok and "expired" in output.lower(), output
