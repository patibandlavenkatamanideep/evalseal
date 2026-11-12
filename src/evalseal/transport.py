"""Bounded HTTP for every outbound call: response size, wall clock, retry budget.

One place, because the two callers - a model provider and a timestamp authority - are
both remote services EvalSeal does not control, and a limit that exists in one of them
is a limit a reader cannot rely on.

## What was unbounded, and what it cost

`httpx` buffers a whole response before `.json()` sees it, and it decompresses
transparently. Against a loopback server returning 200 MB of JSON:

    a 200 MB JSON body     -> accepted 210 MB of content  (peak traced 630 MB, 0.4s)
    a gzip bomb            -> accepted 210 MB of content  (peak traced 629 MB, 0.7s)

The second body is about 200 KB on the wire. Neither is a plausible chat completion;
both are what a misconfigured endpoint, a captive portal, an HTML error page from a
proxy, or a hostile `--base-url` will hand you, and either one can take a CI runner out
of memory before anything gets scored.

So a response is read **in chunks, counting decoded bytes**, and abandoned the moment it
passes the limit. Counting decoded bytes rather than wire bytes is the whole point: a
compressed body is only dangerous after it expands.

Counting them is not enough on its own. `httpx` decodes a whole raw chunk before
handing any of it over, and at a 1000:1 compression ratio one 64 KiB raw read expands to
64 MB - so a limit applied to the decoded stream was still reached only *after* the
expansion had happened, and the same bomb peaked at 270 MB. The body is therefore
inflated here, incrementally, with `zlib`'s own `max_length`, so the decoder is never
asked to produce more than the limit allows. This client advertises exactly the two
encodings it can bound that way and refuses any other, rather than handing an unknown
encoding back to a decoder it cannot meter.

## Three different budgets, named separately

- **`max_bytes`** - how much of one response will be read. Exceeded is an error, never a
  truncation: half a JSON document parsed as if it were whole is worse than no answer.
- **`deadline`** - total wall clock for one logical call, *including* its retries and the
  sleeps between them. A per-attempt `timeout` bounds a single socket; with five retries
  and a 60-second timeout the old worst case was over six minutes, and nothing said so.
  Left unset it is derived from the other two and still enforced, so the bound is a
  number a reader can see rather than an emergent property of the retry loop.
- **`max_retries`** - how many transient failures are absorbed. Unchanged.

The defaults are measured, not invented: see docs/operations.md for the benchmark the
numbers come from and the one real limit they are far above.
"""

from __future__ import annotations

import time
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

#: Generous for a chat completion and nowhere near a runner's memory. The largest
#: response in this repository's committed cassettes is under 40 KB.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
#: An RFC 3161 token is a few kilobytes. A megabyte is already absurd for one.
MAX_TSA_RESPONSE_BYTES = 1024 * 1024
#: Read granularity on the wire. The overshoot past the limit is bounded by one chunk's
#: worth of *decoded* output, which `_inflate` caps rather than letting the ratio decide.
_CHUNK = 64 * 1024
#: The only content encodings this client asks for, because they are the only ones it
#: can decode under a byte cap. `identity` is always acceptable.
ACCEPT_ENCODING = "gzip, deflate"
#: zlib window bits: 16+MAX_WBITS reads a gzip wrapper, -MAX_WBITS a raw deflate
#: stream, MAX_WBITS a zlib-wrapped one. Both spellings of "deflate" are in the wild.
_WBITS = {"gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS}


class UnsupportedEncoding(RuntimeError):
    """The peer used a content encoding this client did not ask for and cannot meter."""


def _inflate(chunks: Iterable[bytes], encoding: str, limit: int,
             what: str) -> Iterator[bytes]:
    """Decompress `chunks` without ever letting the decoder exceed `limit` bytes.

    `decompressobj.decompress(data, max_length)` is the whole point: it returns at most
    `max_length` bytes and keeps the rest in `unconsumed_tail`. So a body that would
    expand to gigabytes stops expanding one byte past the limit, instead of being
    inflated in full and then measured.
    """
    try:
        wbits = _WBITS[encoding]
    except KeyError:
        raise UnsupportedEncoding(
            f"{what} replied with content-encoding {encoding!r}, which this client did "
            f"not ask for (it advertises {ACCEPT_ENCODING!r}) and cannot decode under "
            "a size limit. Refusing rather than inflating an unbounded body."
        ) from None
    decoder = zlib.decompressobj(wbits)
    allowance = limit + 1          # one byte over is enough to know it is over
    for chunk in chunks:
        data = chunk
        while data:
            out = decoder.decompress(data, allowance)
            if out:
                allowance -= len(out)
                yield out
            data = decoder.unconsumed_tail
            if allowance <= 0:
                # Past the limit already. Stop decoding; `read_bounded` raises.
                return
    tail = decoder.flush(max(1, allowance))
    if tail:
        yield tail


class ResponseTooLarge(RuntimeError):
    """The peer sent more than this call was willing to read."""


class DeadlineExceeded(RuntimeError):
    """The total wall-clock budget for this call ran out."""


@dataclass(frozen=True)
class Budget:
    """What one logical call is allowed to consume.

    `deadline` is the total across attempts. None derives it from the per-attempt
    timeout, the retry count and the backoff those retries will sleep for, which is the
    bound the loop already had - stated rather than implied.
    """
    timeout: float = 60.0
    max_retries: int = 5
    max_bytes: int = MAX_RESPONSE_BYTES
    deadline: float | None = None

    def total_deadline(self, backoff: Callable[[int], float]) -> float:
        """The wall clock this call may take, explicit or derived."""
        if self.deadline is not None:
            return self.deadline
        attempts = self.max_retries + 1
        sleeps = sum(backoff(i) for i in range(self.max_retries))
        return self.timeout * attempts + sleeps

    def describe(self, backoff: Callable[[int], float]) -> str:
        """One line for a receipt, a log or an error message."""
        return (f"at most {self.max_bytes / 1024 / 1024:.0f} MiB per response, "
                f"{self.max_retries} retry(s), {self.timeout:.0f}s per attempt, "
                f"{self.total_deadline(backoff):.0f}s in total")


class _Clock:
    """The remaining budget, so the deadline is checked in one place."""

    def __init__(self, total: float) -> None:
        self.total = total
        self._started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def remaining(self) -> float:
        return self.total - self.elapsed

    def check(self, what: str) -> None:
        if self.remaining <= 0:
            raise DeadlineExceeded(
                f"{what} ran out of time: {self.total:.0f}s total budget spent after "
                f"{self.elapsed:.1f}s. Raise --deadline, or lower --max-retries so the "
                "call gives up sooner.")


def read_bounded(chunks: Iterable[bytes], limit: int, what: str) -> bytes:
    """Join `chunks` while they stay under `limit`, or raise `ResponseTooLarge`.

    The chunks are already decompressed by the time they arrive here, so the count is
    of bytes that would land in memory - which is what a limit has to be about.
    """
    parts: list[bytes] = []
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(
                f"{what} sent more than {limit} bytes "
                f"({limit / 1024 / 1024:.1f} MiB) and was abandoned after "
                f"{total} bytes. This is not a plausible response; check the endpoint, "
                "and raise --max-response-bytes if the limit is genuinely too low. "
                "Nothing was parsed: half a document is worse than no answer.")
        parts.append(chunk)
    return b"".join(parts)


def _declared_too_large(response: httpx.Response, limit: int, what: str) -> None:
    """Refuse on `Content-Length` before reading a byte, when the peer declares one.

    A fast path only. An encoded length under the limit says nothing about the decoded
    size, and a chunked response declares nothing at all, so `read_bounded` is still
    the thing that enforces the limit.
    """
    declared = response.headers.get("content-length")
    if declared is None:
        return
    try:
        length = int(declared)
    except ValueError:
        return
    if response.headers.get("content-encoding"):
        return          # the declared length is of the compressed form
    if length > limit:
        raise ResponseTooLarge(
            f"{what} declared a {length}-byte response, over the {limit}-byte limit "
            f"({limit / 1024 / 1024:.1f} MiB). Nothing was read.")


def request_bounded(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    content: bytes | None = None,
    json_body: dict[str, Any] | None = None,
    budget: Budget,
    clock: _Clock,
    what: str,
) -> tuple[httpx.Response, bytes]:
    """One attempt, with the body read under `budget.max_bytes`.

    Returns the response and its bytes. The response is **materialized**: a copy
    carrying the bytes that were read, so a caller may touch `.text` or `.content` the
    way it could before this module existed. A streamed response that has left its
    `with` block raises `ResponseNotRead` on either, and the place that reads them is
    an error path - `HTTP 429: quota exceeded` - which is the worst place to lose the
    message.

    `content-encoding` and `content-length` are dropped from the copy because `body` is
    already decoded: keeping them would have httpx decode it a second time.
    """
    clock.check(what)
    # The socket never gets longer than what is left of the total budget, so a hung
    # connection cannot outlive the deadline by a whole per-attempt timeout.
    timeout = min(budget.timeout, max(0.001, clock.remaining))
    # `iter_raw` and not `iter_bytes`: httpx would decode a whole raw chunk before
    # yielding any of it, and the ratio decides how much that is.
    asked = {**headers, "accept-encoding": ACCEPT_ENCODING}
    with (
        httpx.Client(timeout=timeout) as client,
        client.stream(method, url, headers=asked, content=content,
                      json=json_body) as response,
    ):
        _declared_too_large(response, budget.max_bytes, what)
        encoding = (response.headers.get("content-encoding") or "identity").lower()
        raw = response.iter_raw(_CHUNK)
        chunks = raw if encoding == "identity" else _inflate(
            raw, encoding, budget.max_bytes, what)
        body = read_bounded(chunks, budget.max_bytes, what)
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() not in ("content-encoding", "content-length")}
        materialized = httpx.Response(
            response.status_code, headers=headers, content=body,
            request=response.request)
    return materialized, body
