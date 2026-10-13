"""A timestamp authority built in-process, so every failure mode is testable offline.

A recorded response from a real TSA tells you that the parser handles what the world
emits. It cannot tell you what happens when a signature is tampered with, a certificate
lacks the timeStamping EKU, or `genTime` falls outside the certificate's validity -
because you cannot ask a real authority to misbehave on request.

So this builds the authority: a CA, a TSA certificate signed by it, and tokens signed
with the TSA key, with a knob for each thing that can be wrong. `cryptography` does the
keys, the certificates and the signing; the CMS and TSTInfo structures are assembled
with the DER encoder `rfc3161` already uses for requests, which exercises that encoder
in the bargain.

Keys are generated per call and never written to disk. Nothing here is a secret and
nothing here is a real authority: it exists to make the verifier's negative paths
assertable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from evalseal.rfc3161 import _der, _integer, _oid, _sequence

# Object identifiers, written out so a reader can check them against the RFCs.
ID_SIGNED_DATA = "1.2.840.113549.1.7.2"
ID_CT_TST_INFO = "1.2.840.113549.1.9.16.1.4"
ATTR_CONTENT_TYPE = "1.2.840.113549.1.9.3"
ATTR_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
OID_SHA256 = "2.16.840.1.101.3.4.2.1"
OID_SHA256_RSA = "1.2.840.113549.1.1.11"

# A fixed instant, so a token's genTime and its certificate's validity are both
# decided by the test rather than by when the test happens to run.
GEN_TIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _set_of(*parts: bytes) -> bytes:
    return _der(0x31, b"".join(parts))


def _explicit(tag_number: int, content: bytes) -> bytes:
    """Context-specific constructed tag, e.g. [0] EXPLICIT."""
    return _der(0xA0 | tag_number, content)


def _implicit_set(tag_number: int, content: bytes) -> bytes:
    """Context-specific constructed tag holding a SET's contents, e.g. [0] IMPLICIT."""
    return _der(0xA0 | tag_number, content)


def _algorithm(oid: str) -> bytes:
    return _sequence(_oid(oid), _der(0x05, b""))


def _generalized_time(when: datetime) -> bytes:
    return _der(0x18, when.strftime("%Y%m%d%H%M%SZ").encode("ascii"))


def _attribute(oid: str, value: bytes) -> bytes:
    return _sequence(_oid(oid), _set_of(value))


@dataclass
class TestTsa:
    """A disposable authority: its CA, its signing certificate, and its key."""
    ca_certificate: x509.Certificate
    tsa_certificate: x509.Certificate
    tsa_key: rsa.RSAPrivateKey

    @property
    def ca_pem(self) -> bytes:
        return self.ca_certificate.public_bytes(serialization.Encoding.PEM)

    @property
    def tsa_pem(self) -> bytes:
        return self.tsa_certificate.public_bytes(serialization.Encoding.PEM)


def _key() -> rsa.RSAPrivateKey:
    # 2048 is the smallest size `cryptography` will not complain about and keeps the
    # test suite fast; this key protects nothing.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def build_tsa(
    *,
    timestamping_eku: bool = True,
    eku_critical: bool = True,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> TestTsa:
    """A CA plus a TSA certificate, with the knobs the negative tests need."""
    ca_key, tsa_key = _key(), _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EvalSeal Test CA")])
    tsa_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EvalSeal Test TSA")])

    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(GEN_TIME - timedelta(days=365))
        .not_valid_after(GEN_TIME + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    builder = (
        x509.CertificateBuilder()
        .subject_name(tsa_name).issuer_name(ca_name)
        .public_key(tsa_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or GEN_TIME - timedelta(days=30))
        .not_valid_after(not_after or GEN_TIME + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if timestamping_eku:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]),
            critical=eku_critical)
    tsa = builder.sign(ca_key, hashes.SHA256())
    return TestTsa(ca_certificate=ca, tsa_certificate=tsa, tsa_key=tsa_key)


def build_tst_info(
    digest: str, *, gen_time: datetime | None = None, serial: int = 4242,
    policy: str = "1.2.3.4.1",
) -> bytes:
    """A TSTInfo over `digest`, as the authority would sign it."""
    raw = bytes.fromhex(digest.split(":", 1)[1])
    imprint = _sequence(_algorithm(OID_SHA256), _der(0x04, raw))
    return _sequence(
        _integer(1), _oid(policy), imprint, _integer(serial),
        _generalized_time(gen_time or GEN_TIME))


def build_token(
    tsa: TestTsa,
    digest: str,
    *,
    gen_time: datetime | None = None,
    econtent_type: str = ID_CT_TST_INFO,
    wrong_message_digest: bool = False,
    corrupt_signature: bool = False,
    include_certificate: bool = True,
    sign_with: rsa.RSAPrivateKey | None = None,
    status: int = 0,
    tst_info: bytes | None = None,
) -> bytes:
    """A complete DER `TimeStampResp`, signed, with one knob per failure mode.

    Every default produces a well-formed, verifiable token; each keyword turns exactly
    one thing wrong, so a failing test names a single cause.
    """
    info = tst_info if tst_info is not None else build_tst_info(digest, gen_time=gen_time)

    digest_for_attr = hashlib.sha256(
        b"not the content" if wrong_message_digest else info).digest()
    signed_attrs_content = (
        _attribute(ATTR_CONTENT_TYPE, _oid(econtent_type))
        + _attribute(ATTR_MESSAGE_DIGEST, _der(0x04, digest_for_attr))
    )
    # RFC 5652: the signature covers signedAttrs re-encoded as a SET OF (0x31), not the
    # [0] IMPLICIT tag (0xA0) the structure carries. Getting this wrong is the classic
    # way a CMS verifier fails on every real token.
    to_sign = _set_of(signed_attrs_content)
    signature = (sign_with or tsa.tsa_key).sign(
        to_sign, padding.PKCS1v15(), hashes.SHA256())
    if corrupt_signature:
        signature = bytes([signature[0] ^ 0xFF]) + signature[1:]

    signer_info = _sequence(
        _integer(1),
        _sequence(                                    # IssuerAndSerialNumber
            tsa.tsa_certificate.issuer.public_bytes(),
            _integer(tsa.tsa_certificate.serial_number)),
        _algorithm(OID_SHA256),
        _implicit_set(0, signed_attrs_content),
        _algorithm(OID_SHA256_RSA),
        _der(0x04, signature),
    )

    certificates = b""
    if include_certificate:
        certificates = _implicit_set(
            0, tsa.tsa_certificate.public_bytes(serialization.Encoding.DER))

    signed_data = _sequence(
        _integer(3),
        _set_of(_algorithm(OID_SHA256)),
        _sequence(_oid(econtent_type), _explicit(0, _der(0x04, info))),
        certificates,
        _set_of(signer_info),
    )
    token = _sequence(_oid(ID_SIGNED_DATA), _explicit(0, signed_data))
    return _sequence(_sequence(_integer(status)), token)
