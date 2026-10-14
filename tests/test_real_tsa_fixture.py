"""Verify a response recorded from a real timestamp authority.

Every other RFC 3161 test runs against `tsa_fixture`, an authority built in-process.
That is deliberate — it is offline and it can be asked to misbehave, which is the only
way the failure matrix exists. What it cannot establish is that the parser handles what
real authorities emit: they differ in field order, in whether the chain is embedded, in
signature algorithm, and in how much optional TSTInfo they populate.

**No response has been recorded yet**, so these tests skip. The procedure is in
`tests/fixtures/tsa/RECORDING.md` and needs one network call. The tests are written
first so that landing the fixture is the only remaining step, and so the requirements
on it — above all the pinned verification time — are stated where the fixture is used
rather than only in prose.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "tsa"
RESPONSE = FIXTURES / "response.tsr"
CA = FIXTURES / "ca.pem"
METADATA = FIXTURES / "recorded.json"

#: Every file the procedure produces. All three, or the fixture is half-landed.
_REQUIRED = (RESPONSE, CA, METADATA)

recorded = pytest.mark.skipif(
    not all(p.exists() for p in _REQUIRED),
    reason="no real TSA response recorded; see tests/fixtures/tsa/RECORDING.md")


@pytest.fixture
def meta() -> dict:
    return json.loads(METADATA.read_text(encoding="utf-8"))


# --- the procedure's own documentation, which is present now ---------------------------

def test_the_recording_procedure_exists_and_names_the_expiry_trap():
    """The procedure is checked even while the fixture is absent, because the trap it
    warns about is the one that would break a future build for no visible reason."""
    text = " ".join((FIXTURES / "RECORDING.md").read_text(encoding="utf-8").split())
    assert "A signing certificate expires" in text
    assert "as of the token's own `genTime`" in text
    assert "long-expired certificate, and that is **fine and intended**" in text
    # And the honesty about what has not happened.
    assert "has not been run" in text
    assert "only a 32-byte digest" in text


def test_the_procedure_says_evalseal_ships_no_default_authority():
    text = " ".join((FIXTURES / "RECORDING.md").read_text(encoding="utf-8").split())
    assert "ships no default authority" in text
    assert "a test input, not a recommendation" in text


def test_the_fixture_directory_is_protected_from_line_ending_translation():
    """These bytes are load-bearing: a CRLF checkout would break the signature."""
    import subprocess

    repo = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["git", "check-attr", "text", "--", "tests/fixtures/tsa/response.tsr"],
        cwd=repo, capture_output=True, text=True, check=True).stdout
    assert out.strip().endswith(": unset"), out


# --- the fixture itself, once it lands -------------------------------------------------

@recorded
def test_the_recorded_response_is_der():
    blob = RESPONSE.read_bytes()
    assert blob[:1] == b"\x30", "not a DER SEQUENCE; an HTML error page was saved"
    assert len(blob) > 200


@recorded
def test_the_recorded_metadata_names_what_was_submitted(meta):
    assert set(meta) >= {"subject_digest", "authority", "recorded"}
    assert meta["subject_digest"].startswith("sha256:")
    assert len(meta["subject_digest"].removeprefix("sha256:")) == 64


@recorded
def test_the_parser_reads_what_a_real_authority_emitted():
    """The whole reason this fixture exists."""
    from evalseal.rfc3161 import parse_timestamp_response

    token = parse_timestamp_response(RESPONSE.read_bytes())
    assert token.econtent, "no eContent: the signature covers nothing"
    assert token.signers, "no SignerInfo"
    assert token.certificates, "no embedded certificate; certReq should have set this"


@recorded
def test_the_real_token_verifies_against_its_own_certificate(meta):
    from evalseal.rfc3161 import VERIFIED, verify_token

    result = verify_token(RESPONSE.read_bytes(), meta["subject_digest"],
                          ca_pem=CA.read_bytes())
    failing = [c.name for c in result.checks if c.ran and not c.passed]
    assert result.state == VERIFIED, failing
    assert "revocation not checked" in result.boundary


@recorded
def test_the_real_token_fails_for_a_different_digest(meta):
    """A fixture that passes whatever it is asked is not testing anything."""
    from evalseal.rfc3161 import FAILED, verify_token

    other = "sha256:" + "0" * 64
    result = verify_token(RESPONSE.read_bytes(), other, ca_pem=CA.read_bytes())
    assert result.state == FAILED
    assert "message_imprint" in [c.name for c in result.checks
                                 if c.ran and not c.passed]


@recorded
def test_verification_does_not_depend_on_todays_date(meta):
    """The expiry trap, pinned where the fixture is used.

    `verify_token` checks `genTime` against the signing certificate's validity, never
    the clock now. So this must keep passing after the certificate expires - which it
    eventually will, and which is fine. If this test ever starts failing with a
    validity complaint, something reintroduced a dependency on the current date.
    """
    from evalseal.rfc3161 import VERIFIED, verify_token

    result = verify_token(RESPONSE.read_bytes(), meta["subject_digest"],
                          ca_pem=CA.read_bytes())
    validity = next(c for c in result.checks if c.name == "validity")
    assert validity.passed, validity.detail
    assert result.state == VERIFIED
    # And the detail quotes the asserted time, not a current timestamp.
    assert "genTime" in validity.detail


@recorded
def test_without_the_authority_certificate_the_claim_shrinks(meta):
    from evalseal.rfc3161 import IMPRINT_ONLY, verify_token

    result = verify_token(RESPONSE.read_bytes(), meta["subject_digest"])
    assert result.state == IMPRINT_ONLY
    assert "NOT verified by EvalSeal" in result.boundary
