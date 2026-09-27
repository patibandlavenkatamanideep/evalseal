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
from datetime import UTC, datetime

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

    def __init__(self, url: str | None = None, *, timeout: float = 15.0) -> None:
        self.url = url
        self.timeout = timeout

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
            "verified_by_evalseal": False,
            "boundary": _BOUNDARY,
            "established": (
                f"{_BOUNDARY}. {self.url} asserts this digest existed no later than "
                f"{parse_gen_time(info['gen_time']) or info['gen_time']}, on that "
                f"authority's word alone. {_UNVERIFIED}"),
        }

    def check(self, subject_digest: str, proof: dict) -> tuple[bool, str]:
        """Re-read the stored token offline. Network is never touched here."""
        blob = proof.get("proof")
        if not blob:
            return (False, "not checked: the proof entry carries no token")
        try:
            der = base64.b64decode(blob, validate=True)
        except (ValueError, TypeError):
            return (False, "not checked: the stored token is not valid base64")

        info = extract_tst_info(der)
        if info is None:
            return (False, "the stored token could not be read as an RFC 3161 token")
        if info["hashed_message"] != subject_digest:
            return (False,
                    f"the token timestamps {info['hashed_message']}, not this anchor's "
                    f"subject digest {subject_digest}")

        when = parse_gen_time(info["gen_time"]) or info["gen_time"]
        authority = proof.get("authority", "the authority")
        return (True,
                f"{_BOUNDARY}. The imprint matches this anchor's subject digest, and "
                f"{authority} asserts it existed no later than {when} - on that "
                f"authority's word, unverified here. {_UNVERIFIED}")
