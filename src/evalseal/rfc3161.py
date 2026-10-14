"""An RFC 3161 timestamp backend: a real request, and an honest account of the reply.

## Status: experimental, and deliberately partial

This builds a genuine `TimeStampReq`, submits it to a timestamp authority you name,
and stores the returned token. On verification it parses the token far enough to
confirm two things offline:

1. the token's `messageImprint` is **this anchor's subject digest**, so the token is
   about this receipt and not some other one, and
2. the time the authority asserts in `genTime`.

**It does not verify the authority's signature.** Doing that means validating a CMS
`SignedData` against a certificate chain, and neither the parser here nor
`cryptography`'s public API does it. EvalSeal will not imply a cryptographic check it
did not perform, so every message this module produces says which half was done, and
`check` hands back the `openssl` command that does the other half.

That makes a stored token worth something short of proof: it is evidence that an
authority was asked about this digest and answered, checkable by anyone who runs the
openssl command with the authority's certificate. `docs/external-anchoring.md` states
the same boundary in the same words.

## Why no default authority

`submit` refuses to run without an explicit `--tsa URL`. Shipping a default would make
whichever service EvalSeal picked into part of this project's trust root, and would
turn `evalseal anchor` into a command that silently contacts a third party. Both are
things an audit tool should not do.

Only the subject digest ever leaves the machine. The authority sees 32 bytes and a
request time; it never sees the receipt, the ledger, the dataset or anything they hash.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

import httpx

from .anchor import AnchorError

_SHA256_OID = "2.16.840.1.101.3.4.2.1"
_CONTENT_TYPE = "application/timestamp-query"
_ACCEPT = "application/timestamp-reply"

# RFC 3161 PKIStatus values. 0 and 1 carry a token; the rest are refusals.
_STATUS_GRANTED = {0: "granted", 1: "granted with modifications"}
_STATUS_REJECTED = {
    2: "rejection", 3: "waiting", 4: "revocation warning", 5: "revocation notification",
}


# --- DER, written out rather than pulled in -------------------------------------------
# A TimeStampReq is a handful of nested TLVs. Encoding them here keeps the dependency
# list at five packages; an ASN.1 library would be a sixth for about sixty lines.

def _der(tag: int, content: bytes) -> bytes:
    if len(content) < 0x80:
        return bytes([tag, len(content)]) + content
    length = len(content).to_bytes((len(content).bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(length)]) + length + content


def _integer(value: int) -> bytes:
    if value == 0:
        return _der(0x02, b"\x00")
    raw = value.to_bytes((value.bit_length() + 8) // 8, "big")
    return _der(0x02, raw.lstrip(b"\x00") or b"\x00")


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    body = bytearray([40 * parts[0] + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append((part & 0x7F) | 0x80)
            part >>= 7
        body.extend(reversed(chunk))
    return _der(0x06, bytes(body))


def _sequence(*parts: bytes) -> bytes:
    return _der(0x30, b"".join(parts))


def build_timestamp_request(digest: str, *, nonce: int | None = None,
                            cert_req: bool = True) -> bytes:
    """Build a DER `TimeStampReq` for a `sha256:...` digest.

    The nonce is random by default and echoed by a well-behaved authority, which is
    what distinguishes a fresh answer from a replayed one.
    """
    raw = _digest_bytes(digest)
    imprint = _sequence(_sequence(_oid(_SHA256_OID), _der(0x05, b"")), _der(0x04, raw))
    parts = [_integer(1), imprint]
    if nonce is None:
        nonce = int.from_bytes(os.urandom(8), "big")
    parts.append(_integer(nonce))
    parts.append(_der(0x01, b"\xff" if cert_req else b"\x00"))
    return _sequence(*parts)


def _digest_bytes(digest: str) -> bytes:
    hex_part = digest.split(":", 1)[-1]
    try:
        raw = bytes.fromhex(hex_part)
    except ValueError:
        raise AnchorError(f"not a hex digest: {digest!r}") from None
    if len(raw) != 32:
        raise AnchorError(f"expected a sha256 digest, got {len(raw)} bytes")
    return raw


# --- reading a reply -------------------------------------------------------------------

def _tlvs(data: bytes, start: int = 0, end: int | None = None):
    """Yield (tag, content_start, content_end) for each TLV at this level."""
    end = len(data) if end is None else end
    i = start
    while i < end:
        if i + 2 > end:
            return
        tag = data[i]
        length = data[i + 1]
        i += 2
        if length & 0x80:
            count = length & 0x7F
            if count == 0 or i + count > end:
                return
            length = int.from_bytes(data[i:i + count], "big")
            i += count
        if i + length > end:
            return
        yield tag, i, i + length
        i += length


def _walk(data: bytes, start: int = 0, end: int | None = None, depth: int = 0):
    """Every TLV in the tree. Depth-limited: a malformed token must not spin."""
    if depth > 20:
        return
    for tag, begin, finish in _tlvs(data, start, end):
        yield tag, begin, finish
        if tag in (0x30, 0x31) or tag & 0xA0 == 0xA0 or tag == 0x04:      # SEQUENCE, SET, context
            yield from _walk(data, begin, finish, depth + 1)


def response_status(der: bytes) -> tuple[int | None, str]:
    """The authority's PKIStatus, so a refusal is reported as a refusal."""
    for tag, begin, end in _tlvs(der):
        if tag != 0x30:
            continue
        for inner_tag, inner_begin, inner_end in _tlvs(der, begin, end):
            if inner_tag == 0x30:       # PKIStatusInfo
                for s_tag, s_begin, s_end in _tlvs(der, inner_begin, inner_end):
                    if s_tag == 0x02:
                        value = int.from_bytes(der[s_begin:s_end], "big")
                        return value, _STATUS_GRANTED.get(
                            value, _STATUS_REJECTED.get(value, f"status {value}"))
            break
    return None, "no PKIStatus found"


def extract_tst_info(der: bytes) -> dict[str, str] | None:
    """Pull the signed timestamp's own fields out of a token.

    Finds TSTInfo by shape rather than by navigating CMS: the structure is
    SEQUENCE { INTEGER 1, OID policy, MessageImprint, INTEGER serial,
    GeneralizedTime genTime, ... }, which nothing else in the token matches.
    """
    for tag, begin, end in _walk(der):
        if tag != 0x30:
            continue
        fields = list(_tlvs(der, begin, end))
        if len(fields) < 5:
            continue
        (t0, b0, e0), (t1, _, _), (t2, b2, e2), (t3, _, _), (t4, b4, e4) = fields[:5]
        if not (t0 == 0x02 and der[b0:e0] == b"\x01" and t1 == 0x06
                and t2 == 0x30 and t3 == 0x02 and t4 == 0x18):
            continue
        imprint = _imprint_of(der, b2, e2)
        if imprint is None:
            continue
        return {
            "hashed_message": "sha256:" + imprint.hex(),
            "gen_time": der[b4:e4].decode("ascii", "replace"),
            "serial_number": str(int.from_bytes(der[fields[3][1]:fields[3][2]], "big")),
        }
    return None


def _imprint_of(der: bytes, start: int, end: int) -> bytes | None:
    """The hashedMessage OCTET STRING inside a MessageImprint."""
    parts = list(_tlvs(der, start, end))
    if len(parts) != 2 or parts[0][0] != 0x30 or parts[1][0] != 0x04:
        return None
    raw = der[parts[1][1]:parts[1][2]]
    return raw if len(raw) == 32 else None


def parse_gen_time(text: str) -> str | None:
    """GeneralizedTime (`YYYYMMDDHHMMSS[.fff]Z`) as an ISO-8601 string."""
    cleaned = text.strip().rstrip("Z")
    for fmt in ("%Y%m%d%H%M%S.%f", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=UTC).isoformat()
        except ValueError:
            continue
    return None


# --- CMS signature validation ---------------------------------------------------------
# Hand-rolled navigation over the TLV walker above, with `cryptography` - already a
# dependency, actively maintained, audited - doing every cryptographic operation and all
# certificate parsing. An ASN.1 library would be a sixth dependency; the parser here
# only has to walk a known, narrow structure, and the alternative candidate's last
# release was 2022, which is a poor trade for a tool whose job is trust.
#
# OIDs are compared as *encoded bytes* rather than decoded to dotted strings. Encoding a
# known OID and comparing is exact and needs no OID decoder.

_ID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_ID_CT_TST_INFO = "1.2.840.113549.1.9.16.1.4"
_ATTR_CONTENT_TYPE = "1.2.840.113549.1.9.3"
_ATTR_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
# Authorities are not all SHA-256. freetsa.org signs with ECDSA-P384/SHA-512, and an
# implementation that assumed SHA-256 - as this one did until a real token was
# recorded - fails on it twice: the messageDigest attribute is the wrong length, and
# the signature algorithm is unrecognised. The algorithms are read from the token.
_T = TypeVar("_T")

_DIGESTS: dict[str, str] = {
    "2.16.840.1.101.3.4.2.1": "sha256",
    "2.16.840.1.101.3.4.2.2": "sha384",
    "2.16.840.1.101.3.4.2.3": "sha512",
}
_SIGNATURES: dict[str, tuple[str, str]] = {
    # RSA PKCS#1 v1.5
    "1.2.840.113549.1.1.11": ("rsa", "sha256"),
    "1.2.840.113549.1.1.12": ("rsa", "sha384"),
    "1.2.840.113549.1.1.13": ("rsa", "sha512"),
    # ECDSA
    "1.2.840.10045.4.3.2": ("ecdsa", "sha256"),
    "1.2.840.10045.4.3.3": ("ecdsa", "sha384"),
    "1.2.840.10045.4.3.4": ("ecdsa", "sha512"),
}

_EKU_TIMESTAMPING = "1.3.6.1.5.5.7.3.8"


def _algorithm_name(algorithm: bytes, table: dict[str, _T]) -> _T | None:
    """Look an AlgorithmIdentifier up in `table` by its OID.

    Compares the OID TLV on its own rather than the whole AlgorithmIdentifier, because
    the parameters field is an explicit NULL for RSA and absent for ECDSA, and a
    byte-equality test on the wrapper would miss one of them.
    """
    for tag, begin, end in _tlvs(algorithm):
        if tag != 0x30:
            continue
        for inner_tag, inner_begin, inner_end in _tlvs(algorithm, begin, end):
            if inner_tag != 0x06:
                continue
            oid = algorithm[inner_begin - 2:inner_end] if inner_begin >= 2 else b""
            for dotted, value in table.items():
                if _oid(dotted) == oid:
                    return value
            return None
    return None


class TokenFormatError(ValueError):
    """The token is not shaped like an RFC 3161 token, so nothing can be checked."""


def _first(data: bytes, start: int, end: int, tag: int) -> tuple[int, int] | None:
    for t, b, e in _tlvs(data, start, end):
        if t == tag:
            return (b, e)
    return None


@dataclass(frozen=True)
class SignerInfo:
    """The parts of one SignerInfo a signature check needs."""
    signed_attrs: bytes          # the [0] IMPLICIT content, un-retagged
    signature: bytes
    signature_algorithm: bytes   # the encoded AlgorithmIdentifier OID TLV
    digest_algorithm: bytes

    @property
    def signed_data(self) -> bytes:
        """What the signature actually covers.

        RFC 5652 section 5.4: the signature is over the DER encoding of signedAttrs as
        a SET OF (tag 0x31), *not* over the [0] IMPLICIT tag (0xA0) the structure
        carries. Re-tagging is the single most common way a CMS verifier fails on every
        real-world token while passing on hand-made ones.
        """
        return _der(0x31, self.signed_attrs)

    def attribute(self, oid: str) -> bytes | None:
        """The DER of one signed attribute's single value, or None."""
        want = _oid(oid)
        for _, b, e in _tlvs(self.signed_attrs):          # each Attribute SEQUENCE
            parts = list(_tlvs(self.signed_attrs, b, e))
            if len(parts) != 2 or self.signed_attrs[b:parts[0][2]] != want:
                continue
            values = list(_tlvs(self.signed_attrs, parts[1][1], parts[1][2]))
            if len(values) == 1:
                vb, ve = values[0][1], values[0][2]
                return self.signed_attrs[vb:ve]
        return None


@dataclass(frozen=True)
class ParsedToken:
    """A structurally parsed TimeStampResp: the pieces, located properly."""
    status: int | None
    econtent_type: bytes         # encoded OID TLV
    econtent: bytes              # the TSTInfo octets the signature commits to
    certificates: list[bytes]    # DER certificates, in the order the token carried them
    signers: list[SignerInfo]


def parse_timestamp_response(der: bytes) -> ParsedToken:
    """Navigate ContentInfo -> SignedData, rather than finding TSTInfo by shape.

    `extract_tst_info` locates TSTInfo heuristically, which is fine for reading a field
    and wrong as the basis of a signature check: a signature covers the actual
    `eContent` octets, not a structure that merely looks like a TSTInfo.
    """
    top = _first(der, 0, len(der), 0x30)
    if top is None:
        raise TokenFormatError("not a DER SEQUENCE")
    resp_b, resp_e = top

    children = list(_tlvs(der, resp_b, resp_e))
    if not children:
        raise TokenFormatError("empty TimeStampResp")
    status, _ = response_status(der)

    # The ContentInfo is the second element; a refusal carries no token at all.
    info = next((c for c in children[1:] if c[0] == 0x30), None)
    if info is None:
        raise TokenFormatError("the response carries no timeStampToken")
    _, ci_b, ci_e = info

    ci_parts = list(_tlvs(der, ci_b, ci_e))
    if len(ci_parts) < 2 or der[ci_b:ci_parts[0][2]] != _oid(_ID_SIGNED_DATA):
        raise TokenFormatError("timeStampToken is not a CMS SignedData")
    inner = _first(der, ci_parts[1][1], ci_parts[1][2], 0x30)
    if inner is None:
        raise TokenFormatError("SignedData is missing")
    sd_b, sd_e = inner

    # SignedData ::= SEQUENCE { version INTEGER, digestAlgorithms SET,
    #                            encapContentInfo SEQUENCE, [0] certs?, [1] crls?,
    #                            signerInfos SET }
    # So encapContentInfo is the first SEQUENCE that follows the digestAlgorithms SET.
    sd = list(_tlvs(der, sd_b, sd_e))
    after_set = False
    encap = None
    for child in sd:
        if child[0] == 0x31 and not after_set:
            after_set = True
        elif child[0] == 0x30 and after_set:
            encap = child
            break
    if encap is None:
        raise TokenFormatError("encapContentInfo is missing")
    _, en_b, en_e = encap
    en = list(_tlvs(der, en_b, en_e))
    if len(en) < 2:
        raise TokenFormatError("encapContentInfo is malformed")
    econtent_type = der[en_b:en[0][2]]
    octets = _first(der, en[1][1], en[1][2], 0x04)
    if octets is None:
        raise TokenFormatError("eContent is missing")
    econtent = der[octets[0]:octets[1]]

    # certificates is [0] IMPLICIT, so its children are bare Certificate SEQUENCEs.
    # Each has to be handed to x509 with its own header, which `_reslice` restores.
    certificates: list[bytes] = []
    certs = next((c for c in sd if c[0] == 0xA0), None)
    if certs is not None:
        _, cb, ce = certs
        certificates = [_reslice(der, b, e)
                        for tag, b, e in _tlvs(der, cb, ce) if tag == 0x30]

    signer_set = next((c for c in reversed(sd) if c[0] == 0x31), None)
    signers: list[SignerInfo] = []
    if signer_set is not None:
        _, ssb, sse = signer_set
        for tag, b, e in _tlvs(der, ssb, sse):
            if tag == 0x30:
                signers.append(_parse_signer_info(der, b, e))
    return ParsedToken(status=status, econtent_type=econtent_type, econtent=econtent,
                       certificates=certificates, signers=signers)


def _reslice(der: bytes, content_start: int, content_end: int) -> bytes:
    """Re-encode a TLV from its content, so a certificate keeps its own header."""
    return _der(0x30, der[content_start:content_end])


def _parse_signer_info(der: bytes, start: int, end: int) -> SignerInfo:
    parts = list(_tlvs(der, start, end))
    signed_attrs = b""
    attrs = next((c for c in parts if c[0] == 0xA0), None)
    if attrs is not None:
        signed_attrs = der[attrs[1]:attrs[2]]
    algs = [c for c in parts if c[0] == 0x30]
    signature = b""
    sig = next((c for c in reversed(parts) if c[0] == 0x04), None)
    if sig is not None:
        signature = der[sig[1]:sig[2]]
    digest_alg = _der(0x30, der[algs[1][1]:algs[1][2]]) if len(algs) > 1 else b""
    sig_alg = _der(0x30, der[algs[-1][1]:algs[-1][2]]) if algs else b""
    return SignerInfo(signed_attrs=signed_attrs, signature=signature,
                      signature_algorithm=sig_alg, digest_algorithm=digest_alg)


# --- what a verification establishes, stated per check --------------------------------

VERIFIED = "verified"
IMPRINT_ONLY = "imprint_only"
FAILED = "failed"

# Three states, three sentences. The middle one is where this backend lived until the
# CMS check existed, and it has to stay reachable: a caller who supplies no root gets
# the old, smaller claim rather than a silent upgrade.
BOUNDARY_VERIFIED = (
    "signature verified against the certificate you supplied; revocation not checked")
BOUNDARY_IMPRINT_ONLY = (
    "message imprint checked, TSA signature NOT verified by EvalSeal")
BOUNDARY_FAILED = "verification FAILED"

# What a stored proof can say about itself before anyone verifies it. Not a verdict:
# whether the signature checks out depends on a trust root the verifier supplies.
BOUNDARY_AT_REST = (
    "To check the signature, verify with a trust root you supply: "
    "`evalseal anchor-verify --tsa-ca <issuer.pem>`, or "
    "`openssl ts -verify -digest <subject-digest-hex> -in token.tsr -CAfile <ca.pem>`. "
    "Without a root, only the message imprint is checked.")


@dataclass(frozen=True)
class TokenCheck:
    name: str
    passed: bool
    detail: str
    # False for a check that could not run, as opposed to one that ran and failed.
    ran: bool = True


@dataclass
class TokenVerification:
    """Every check that ran, and the one sentence the result is allowed to claim."""
    checks: list[TokenCheck]
    state: str

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.ran)

    @property
    def boundary(self) -> str:
        return {VERIFIED: BOUNDARY_VERIFIED, IMPRINT_ONLY: BOUNDARY_IMPRINT_ONLY}.get(
            self.state, BOUNDARY_FAILED)

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "boundary": self.boundary,
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "ran": c.ran, "detail": c.detail}
                for c in self.checks
            ],
        }


def _tst_info_fields(econtent: bytes) -> dict[str, str] | None:
    """Parse TSTInfo positionally from the eContent the signature commits to."""
    seq = _first(econtent, 0, len(econtent), 0x30)
    if seq is None:
        return None
    fields = list(_tlvs(econtent, *seq))
    if len(fields) < 5:
        return None
    (t0, b0, e0), (t1, _, _), (t2, b2, e2), (t3, b3, e3), (t4, b4, e4) = fields[:5]
    if not (t0 == 0x02 and t1 == 0x06 and t2 == 0x30 and t3 == 0x02 and t4 == 0x18):
        return None
    imprint = _imprint_of(econtent, b2, e2)
    if imprint is None:
        return None
    assert e0 >= b0
    return {
        "hashed_message": "sha256:" + imprint.hex(),
        "gen_time": econtent[b4:e4].decode("ascii", "replace"),
        "serial_number": str(int.from_bytes(econtent[b3:e3], "big")),
    }


_HASHES = {"sha256": "SHA256", "sha384": "SHA384", "sha512": "SHA512"}


def _hash(name: str):
    from cryptography.hazmat.primitives import hashes

    return getattr(hashes, _HASHES[name])()


def _verify_signature(signer: SignerInfo, certificate) -> tuple[bool, str]:
    """Check the signature over the re-tagged signedAttrs. All crypto via cryptography."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ec, padding

    algorithm = _algorithm_name(signer.signature_algorithm, _SIGNATURES)
    if algorithm is None:
        return (False, "unsupported signature algorithm; EvalSeal checks RSA PKCS#1 "
                       "v1.5 and ECDSA, each with SHA-256, SHA-384 or SHA-512")
    kind, digest = algorithm

    public_key = certificate.public_key()
    payload = signer.signed_data
    try:
        if kind == "rsa":
            public_key.verify(signer.signature, payload, padding.PKCS1v15(),
                              _hash(digest))
        else:
            public_key.verify(signer.signature, payload, ec.ECDSA(_hash(digest)))
    except InvalidSignature:
        return (False, "the signature does not verify against the signing certificate")
    except Exception as e:                                    # unusable key, bad DER
        return (False, f"the signature could not be checked: {type(e).__name__}: {e}")
    return (True, f"the signature over signedAttrs verifies against the signing "
                  f"certificate ({kind.upper()} with {digest.upper()})")


def verify_token(
    der: bytes, subject_digest: str, *, ca_pem: bytes | None = None,
) -> TokenVerification:
    """Check a stored token as far as the inputs allow, and say exactly how far.

    With no `ca_pem` the imprint and structure are checked and the result stays
    `imprint_only` - the claim this backend made before CMS validation existed. With a
    root supplied, the signature and the signing certificate are checked too and the
    result may reach `verified`.

    Revocation is never checked. That is stated in `BOUNDARY_VERIFIED` rather than
    omitted, because a verifier that is silent about what it skipped is read as having
    skipped nothing.
    """
    import hashlib as _hashlib

    from cryptography import x509

    checks: list[TokenCheck] = []
    try:
        token = parse_timestamp_response(der)
    except TokenFormatError as e:
        return TokenVerification(
            [TokenCheck("structure", False, f"not an RFC 3161 token: {e}")], FAILED)
    checks.append(TokenCheck("structure", True,
                             "parsed as a CMS SignedData carrying a TSTInfo"))

    granted = token.status is None or token.status in _STATUS_GRANTED
    checks.append(TokenCheck(
        "status", granted,
        "the authority granted the request" if granted
        else f"the authority refused: status {token.status}"))

    right_type = token.econtent_type == _oid(_ID_CT_TST_INFO)
    checks.append(TokenCheck(
        "econtent_type", right_type,
        "eContentType is id-ct-TSTInfo" if right_type
        else "eContentType is not id-ct-TSTInfo, so the signed content is not a "
             "timestamp"))

    info = _tst_info_fields(token.econtent)
    if info is None:
        checks.append(TokenCheck("tst_info", False,
                                 "the signed content is not a readable TSTInfo"))
        return TokenVerification(checks, FAILED)

    matches = info["hashed_message"] == subject_digest
    checks.append(TokenCheck(
        "message_imprint", matches,
        f"the imprint is this anchor's subject digest ({subject_digest[:24]}...)"
        if matches else
        f"the token timestamps {info['hashed_message']}, not this anchor's subject "
        f"digest {subject_digest}"))

    # The attribute that ties the signature to the content: without it a valid
    # signature over unrelated signedAttrs would pass.
    if not token.signers:
        checks.append(TokenCheck("signer", False, "the token carries no SignerInfo"))
        return TokenVerification(checks, FAILED)
    signer = token.signers[0]
    attr = signer.attribute(_ATTR_MESSAGE_DIGEST)
    # The SignerInfo names its own digest algorithm, and it is not always SHA-256:
    # freetsa.org uses SHA-512. Defaulting to SHA-256 made every such token fail.
    digest_name = _algorithm_name(signer.digest_algorithm, _DIGESTS) or "sha256"
    expected = _hashlib.new(digest_name, token.econtent).digest()
    attr_ok = attr == expected
    checks.append(TokenCheck(
        "message_digest_attribute", attr_ok,
        f"the signed messageDigest attribute is the {digest_name.upper()} digest of "
        "eContent" if attr_ok
        else "the signed messageDigest attribute does not match eContent, so the "
             f"signature does not cover this timestamp (checked as {digest_name})"))

    if ca_pem is None:
        # No root, so nothing about the signature can be established. Reported as not
        # run rather than as passed.
        checks.append(TokenCheck(
            "signature", False,
            "not checked: no trust root supplied, so the signature establishes "
            "nothing. Pass --tsa-ca to check it", ran=False))
        state = IMPRINT_ONLY if all(c.passed for c in checks if c.ran) else FAILED
        return TokenVerification(checks, state)

    if not token.certificates:
        checks.append(TokenCheck(
            "signing_certificate", False,
            "the token embeds no certificate, so the signature cannot be checked "
            "against the supplied root"))
        return TokenVerification(checks, FAILED)

    try:
        cert = x509.load_der_x509_certificate(token.certificates[0])
        roots = x509.load_pem_x509_certificates(ca_pem)
    except ValueError as e:
        checks.append(TokenCheck("signing_certificate", False,
                                 f"a certificate could not be parsed: {e}"))
        return TokenVerification(checks, FAILED)

    sig_ok, sig_detail = _verify_signature(signer, cert)
    checks.append(TokenCheck("signature", sig_ok, sig_detail))

    checks.append(_eku_check(cert))
    checks.append(_validity_check(cert, info["gen_time"]))
    checks.append(_chain_check(cert, roots))

    state = VERIFIED if all(c.passed for c in checks if c.ran) else FAILED
    return TokenVerification(checks, state)


def _eku_check(certificate) -> TokenCheck:
    """RFC 3161 requires the timeStamping EKU, and requires it to be critical."""
    from cryptography import x509

    try:
        ext = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound:
        return TokenCheck(
            "timestamping_eku", False,
            "the signing certificate has no extendedKeyUsage, so it is not a "
            "timestamping certificate")
    oids = [o.dotted_string for o in ext.value]
    if _EKU_TIMESTAMPING not in oids:
        return TokenCheck(
            "timestamping_eku", False,
            f"the signing certificate's EKU is {', '.join(oids)}, which does not "
            "include id-kp-timeStamping")
    if not ext.critical:
        # RFC 3161 2.3: the field must be marked critical. A non-critical EKU can be
        # ignored by a verifier, which defeats the point of requiring it.
        return TokenCheck(
            "timestamping_eku", False,
            "the signing certificate's extendedKeyUsage is not marked critical, which "
            "RFC 3161 requires")
    return TokenCheck("timestamping_eku", True,
                      "the signing certificate carries a critical timeStamping EKU")


def _validity_check(certificate, gen_time: str) -> TokenCheck:
    """The asserted time must fall inside the signing certificate's validity window.

    Checked against the token's own `genTime`, never against the clock now. A recorded
    fixture's certificate expires, and a verifier that used `now` would start failing
    on a date nobody chose.
    """
    when = parse_gen_time(gen_time)
    if when is None:
        return TokenCheck("validity", False, f"genTime {gen_time!r} is unreadable")
    from datetime import datetime

    asserted = datetime.fromisoformat(when)
    not_before, not_after = (certificate.not_valid_before_utc,
                             certificate.not_valid_after_utc)
    inside = not_before <= asserted <= not_after
    return TokenCheck(
        "validity", inside,
        f"genTime {when} is inside the signing certificate's validity" if inside
        else f"genTime {when} is outside the signing certificate's validity "
             f"({not_before.isoformat()} to {not_after.isoformat()})")


def _chain_check(certificate, roots: list) -> TokenCheck:
    """The signing certificate against the root the caller supplied. No path building."""
    for root in roots:
        try:
            certificate.verify_directly_issued_by(root)
        except Exception:
            continue
        return TokenCheck(
            "certificate_chain", True,
            f"the signing certificate was issued by {root.subject.rfc4514_string()}, "
            "which you supplied. Revocation was not checked")
    names = ", ".join(r.subject.rfc4514_string() for r in roots) or "none"
    return TokenCheck(
        "certificate_chain", False,
        "the signing certificate was not issued by any root you supplied "
        f"(tried: {names}). EvalSeal does no path building: supply the issuing "
        "certificate directly")


# --- the backend -------------------------------------------------------------------------

# The boundary, in one phrase, leading every message this module produces. For a tool
# whose whole claim is "here is exactly what was established", a partial check that
# reads like a full one is the worst possible output.
_BOUNDARY = "message imprint checked, TSA signature NOT verified by EvalSeal"

_UNVERIFIED = (
    f"EXPERIMENTAL - {_BOUNDARY}. Verifying the signature needs the authority's "
    "certificate chain, which EvalSeal does not validate. Finish the check yourself: "
    "openssl ts -verify -digest <subject-digest-hex> -in token.tsr -CAfile <tsa-ca.pem>"
)


class Rfc3161Backend:
    """Timestamp a subject digest with an authority the caller names.

    Experimental. See this module's docstring for exactly which half of the check is
    performed offline and which half is left to openssl.
    """

    name = "rfc3161"

    def __init__(self, url: str | None = None, *, timeout: float = 15.0,
                 ca_pem: bytes | None = None) -> None:
        self.url = url
        self.timeout = timeout
        # The trust root is held by whoever is *verifying*, never read from the proof.
        # A root carried inside the anchor would be a root the author chose, which
        # defeats the point of an external attestation.
        self.ca_pem = ca_pem

    def submit(self, subject_digest: str) -> dict:
        if not self.url:
            raise AnchorError(
                "the rfc3161 backend needs a timestamp authority: pass --tsa URL. "
                "EvalSeal ships no default, because a default would make one service "
                "part of this tool's trust root.")
        request = build_timestamp_request(subject_digest)
        try:
            response = httpx.post(
                self.url, content=request, timeout=self.timeout,
                headers={"Content-Type": _CONTENT_TYPE, "Accept": _ACCEPT})
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise AnchorError(f"timestamp request to {self.url} failed: {e}") from None

        der = response.content
        status, status_text = response_status(der)
        if status is not None and status not in _STATUS_GRANTED:
            raise AnchorError(f"{self.url} refused to timestamp this digest: {status_text}")

        info = extract_tst_info(der)
        if info is None:
            raise AnchorError(
                f"{self.url} returned something this parser could not read as an "
                "RFC 3161 token; nothing was recorded rather than recording a proof "
                "EvalSeal cannot describe.")
        if info["hashed_message"] != subject_digest:
            raise AnchorError(
                "the authority timestamped a different digest than the one submitted "
                f"({info['hashed_message']} vs {subject_digest})")

        return {
            "backend": self.name,
            "id": info["serial_number"],
            "authority": self.url,
            "digest_algorithm": "sha256",
            "subject_digest": subject_digest,
            "attested_time": parse_gen_time(info["gen_time"]) or info["gen_time"],
            "proof": base64.b64encode(der).decode("ascii"),
            # Describes the token at rest, not a verdict. Submission verifies nothing,
            # and the text must not read as "this can never be verified" now that
            # `anchor-verify --tsa-ca` can check the signature.
            "verified_by_evalseal": False,
            "boundary": BOUNDARY_AT_REST,
            "established": (
                f"{self.url} asserts this digest existed no later than "
                f"{parse_gen_time(info['gen_time']) or info['gen_time']}, on that "
                f"authority's word. Submission checked the imprint and nothing else. "
                f"{BOUNDARY_AT_REST}"),
        }

    def check(self, subject_digest: str, proof: dict) -> tuple[bool, str]:
        """Re-read the stored token offline. Network is never touched here.

        How far this gets depends on whether a trust root was supplied. With one, the
        signature and the signing certificate are checked and the result can say
        "signature verified". Without one, it establishes what it always did - the
        imprint - and says so in the same words as before.
        """
        blob = proof.get("proof")
        if not blob:
            return (False, "not checked: the proof entry carries no token")
        try:
            der = base64.b64decode(blob, validate=True)
        except (ValueError, TypeError):
            return (False, "not checked: the stored token is not valid base64")

        result = verify_token(der, subject_digest, ca_pem=self.ca_pem)
        authority = proof.get("authority", "the authority")
        info = _tst_info_fields(
            parse_timestamp_response(der).econtent
        ) if result.state != FAILED else None
        when = (parse_gen_time(info["gen_time"]) or info["gen_time"]) if info else "?"

        if result.state == VERIFIED:
            return (True,
                    f"{BOUNDARY_VERIFIED}. {authority} asserts this digest existed no "
                    f"later than {when}, and the token's signature checks out against "
                    "the certificate you supplied. Revocation was not checked.")
        if result.state == IMPRINT_ONLY:
            return (True,
                    f"{BOUNDARY_IMPRINT_ONLY}. The imprint matches this anchor's "
                    f"subject digest, and {authority} asserts it existed no later than "
                    f"{when} - on that authority's word, unverified here. {_UNVERIFIED}")
        failing = "; ".join(c.detail for c in result.checks if c.ran and not c.passed)
        return (False, f"{BOUNDARY_FAILED}: {failing}")
