"""Bounded HTTP: response size, decompression, wall clock, retry budget.

Driven against a **loopback HTTP server**, not a mock, because the thing under test is
what happens to bytes between a socket and `json.loads` - buffering, transparent
decompression, early refusal on a declared length. A mock that hands over a finished
`httpx.Response` has already done the part that was unbounded.

No live calls: the server binds 127.0.0.1 on an ephemeral port inside the test process.

**Baseline reproduction (2.2.0).** Against a loopback server returning 200 MB of JSON,
and the same body gzipped to about 200 KB on the wire:

    a 200 MB JSON body     -> accepted 210 MB of content  (peak traced 630 MB, 0.4s)
    a gzip bomb            -> accepted 210 MB of content  (peak traced 629 MB, 0.7s)

After the first fix - counting decoded bytes - the bomb still peaked at 270 MB, because
`httpx` decodes a whole raw chunk before yielding any of it and the compression ratio
decides how large that is. Inflating under `zlib`'s own `max_length` brought it to 17 MB:

    a 200 MB JSON body     -> ResponseTooLarge (peak traced 1 MB, declared length)
    a gzip bomb            -> ResponseTooLarge (peak traced 17 MB)
"""
from __future__ import annotations

import contextlib
import gzip
import http.server
import io
import json
import threading
import time
import zlib

import httpx
import pytest

from evalseal.adapters.target import ProviderCall, post_with_retries
from evalseal.transport import (
    ACCEPT_ENCODING,
    MAX_RESPONSE_BYTES,
    MAX_TSA_RESPONSE_BYTES,
    Budget,
    DeadlineExceeded,
    ResponseTooLarge,
    UnsupportedEncoding,
    _Clock,
    read_bounded,
)

_OK = {"model": "m", "choices": [{"message": {"content": "yes"}}]}


def _body(size: int) -> bytes:
    return ('{"model":"m","choices":[{"message":{"content":"'
            + "x" * size + '"}}]}').encode()


def _gzipped(raw: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as z:
        z.write(raw)
    return buf.getvalue()


# Built once, outside any test, so a memory measurement is of the client and not of the
# in-process server's payload.
_SMALL = json.dumps(_OK).encode()
_BIG = _body(32 * 1024 * 1024)
_BOMB = _gzipped(_BIG)
_SMALL_GZIP = _gzipped(_SMALL)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):          # keep the test output readable
        pass

    def handle_one_request(self):
        # The client hangs up as soon as it has read enough, which is the point: the
        # server noticing is not a failure of anything under test.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            super().handle_one_request()

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None,
              *, declare_length: bool = True):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("content-type", "application/json")
        if declare_length:
            self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        route = self.path
        self.server.seen.append({"path": route,                 # type: ignore[attr-defined]
                                 "accept_encoding":
                                     self.headers.get("accept-encoding")})
        if route == "/ok":
            self._send(200, _SMALL)
        elif route == "/big":
            self._send(200, _BIG)
        elif route == "/big-undeclared":
            # Chunked, so there is no content-length to refuse on: the limit has to be
            # enforced while reading.
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for i in range(0, len(_BIG), 65536):
                piece = _BIG[i:i + 65536]
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif route == "/bomb":
            self._send(200, _BOMB, {"content-encoding": "gzip"})
        elif route == "/gzip":
            self._send(200, _SMALL_GZIP, {"content-encoding": "gzip"})
        elif route == "/deflate":
            self._send(200, zlib.compress(_SMALL), {"content-encoding": "deflate"})
        elif route == "/brotli":
            self._send(200, b"\x00" * 32, {"content-encoding": "br"})
        elif route == "/html":
            self._send(200, b"<html><body>Sign in to continue</body></html>")
        elif route == "/jsonlist":
            self._send(200, b"[1, 2, 3]")
        elif route == "/429":
            self._send(429, b'{"error":"quota exceeded"}', {"retry-after": "0"})
        elif route == "/slow":
            time.sleep(5.0)
            self._send(200, _SMALL)
        else:                                                   # pragma: no cover
            self._send(404, b"{}")


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen = []                          # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def base(server):
    server.seen.clear()
    return f"http://127.0.0.1:{server.server_address[1]}"


def _post(url: str, **kw) -> ProviderCall:
    kw.setdefault("max_retries", 0)
    kw.setdefault("timeout", 10.0)
    return post_with_retries(url=url, headers={}, body={"a": 1},
                             sleep=lambda s: None, **kw)


# --- the normal case still works -----------------------------------------------------

def test_an_ordinary_response_is_returned_unchanged(base):
    assert _post(base + "/ok").body == _OK


@pytest.mark.parametrize("route", ["/gzip", "/deflate"])
def test_a_legitimately_compressed_response_is_decoded(base, route):
    """Both encodings this client advertises round-trip. A limit that broke gzip
    would be a limit everyone turns off."""
    assert _post(base + route).body == _OK


def test_the_client_advertises_only_what_it_can_meter(base, server):
    _post(base + "/ok")
    assert server.seen[-1]["accept_encoding"] == ACCEPT_ENCODING
    assert "br" not in ACCEPT_ENCODING and "zstd" not in ACCEPT_ENCODING


# --- large bodies --------------------------------------------------------------------

def test_a_declared_length_over_the_limit_is_refused_before_reading(base):
    with pytest.raises(ResponseTooLarge) as caught:
        _post(base + "/big", max_bytes=1024 * 1024)
    assert "declared a" in str(caught.value)
    assert "Nothing was read" in str(caught.value)


def test_a_chunked_body_over_the_limit_is_refused_while_reading(base):
    """No content-length to refuse on, so the limit has to hold during the read."""
    with pytest.raises(ResponseTooLarge) as caught:
        _post(base + "/big-undeclared", max_bytes=1024 * 1024)
    assert "sent more than 1048576 bytes" in str(caught.value)
    assert "Nothing was parsed" in str(caught.value)


def test_the_overshoot_past_the_limit_is_bounded(base):
    """The error names how much was read, and it is within a chunk of the limit."""
    limit = 1024 * 1024
    with pytest.raises(ResponseTooLarge) as caught:
        _post(base + "/big-undeclared", max_bytes=limit)
    read = int(str(caught.value).split("abandoned after ")[1].split(" bytes")[0])
    assert limit < read <= limit + 128 * 1024


def test_a_gzip_bomb_does_not_inflate_past_the_limit(base):
    """The decoder is metered, so the ratio cannot decide how much memory is used."""
    import tracemalloc

    limit = 1024 * 1024
    tracemalloc.start()
    with pytest.raises(ResponseTooLarge):
        _post(base + "/bomb", max_bytes=limit)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    # 32 MiB of payload compressed to a few KB. Before the decoder was metered this
    # peaked at roughly the whole decompressed body.
    assert peak < 12 * 1024 * 1024, f"peaked at {peak / 1e6:.0f} MB"


def test_an_encoding_the_client_did_not_ask_for_is_refused(base):
    with pytest.raises(UnsupportedEncoding) as caught:
        _post(base + "/brotli")
    assert "content-encoding 'br'" in str(caught.value)
    assert "cannot decode under a size limit" in str(caught.value)


# --- error bodies --------------------------------------------------------------------

def test_an_html_error_page_names_the_endpoint_and_what_arrived(base):
    """A proxy login page used to raise a JSONDecodeError pointing at character 0."""
    with pytest.raises(RuntimeError) as caught:
        _post(base + "/html")
    message = str(caught.value)
    assert "which is not JSON" in message
    assert "Sign in to continue" in message
    assert base + "/html" in message


def test_a_json_array_is_refused_as_not_a_chat_completion(base):
    with pytest.raises(RuntimeError, match="returned a JSON list, not an object"):
        _post(base + "/jsonlist")


def test_a_rate_limited_response_still_carries_its_body_to_the_error(base):
    """The response is materialized, so the error path can read what the peer said.

    A streamed response that has left its `with` block raises `ResponseNotRead` on
    `.text`, and the place that reads it is the message a user acts on.
    """
    with pytest.raises(httpx.HTTPStatusError) as caught:
        _post(base + "/429")
    assert caught.value.response.status_code == 429
    assert "quota exceeded" in caught.value.response.text


def test_a_rate_limited_response_is_retried_within_the_budget(base, server):
    with pytest.raises(httpx.HTTPStatusError):
        _post(base + "/429", max_retries=2, timeout=5.0)
    assert len([s for s in server.seen if s["path"] == "/429"]) == 3


# --- the wall clock ------------------------------------------------------------------

def test_a_slow_peer_is_cut_off_by_the_total_deadline(base):
    started = time.monotonic()
    with pytest.raises((DeadlineExceeded, httpx.TimeoutException)):
        _post(base + "/slow", timeout=10.0, deadline=1.0, max_retries=0)
    # The per-attempt timeout is 10s; the deadline is what stops it.
    assert time.monotonic() - started < 5.0


def test_the_deadline_is_derived_when_it_is_not_given():
    """The bound the retry loop already had, stated rather than implied."""
    budget = Budget(timeout=60.0, max_retries=5)
    derived = budget.total_deadline(lambda i: min(2.0 ** i, 30.0))
    assert derived == 60.0 * 6 + (1 + 2 + 4 + 8 + 16)
    assert Budget(timeout=60.0, max_retries=5, deadline=90.0).total_deadline(
        lambda i: 1.0) == 90.0


def test_the_budget_describes_itself_for_a_log_or_an_error():
    line = Budget(timeout=30.0, max_retries=2).describe(lambda i: 1.0)
    assert "8 MiB per response" in line
    assert "2 retry(s)" in line
    assert "30s per attempt" in line
    assert "92s in total" in line


def test_the_clock_reports_what_ran_out_and_what_to_change():
    clock = _Clock(0.0)
    with pytest.raises(DeadlineExceeded) as caught:
        clock.check("the provider at http://x/")
    assert "ran out of time" in str(caught.value)
    assert "--deadline" in str(caught.value) and "--max-retries" in str(caught.value)


# --- the reader in isolation ---------------------------------------------------------

def test_read_bounded_accepts_exactly_the_limit():
    assert read_bounded([b"a" * 10], 10, "x") == b"a" * 10


def test_read_bounded_refuses_one_byte_over():
    with pytest.raises(ResponseTooLarge):
        read_bounded([b"a" * 6, b"a" * 5], 10, "x")


def test_read_bounded_names_the_limit_in_mib_and_the_fix():
    with pytest.raises(ResponseTooLarge) as caught:
        read_bounded([b"a" * 2_000_000], 1_048_576, "the provider at http://x/")
    message = str(caught.value)
    assert "1.0 MiB" in message
    assert "--max-response-bytes" in message


# --- the defaults are the documented ones --------------------------------------------

def test_the_defaults_are_the_ones_the_docs_quote():
    assert MAX_RESPONSE_BYTES == 8 * 1024 * 1024
    assert MAX_TSA_RESPONSE_BYTES == 1024 * 1024
    assert Budget().max_bytes == MAX_RESPONSE_BYTES
    assert Budget().max_retries == 5


def test_the_largest_committed_cassette_response_is_far_under_the_default():
    """The default is not a guess: it is orders of magnitude above the real maximum."""
    from pathlib import Path

    cassettes = Path(__file__).resolve().parent / "cassettes"
    largest = 0
    for path in cassettes.glob("*.json"):
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries", {})
        for entry in entries.values():
            largest = max(largest, len(json.dumps(entry).encode()))
    assert 0 < largest < MAX_RESPONSE_BYTES / 50, largest


# --- the in-flight bound -------------------------------------------------------------

def test_concurrency_is_capped_with_the_arithmetic_in_the_message():
    """64 bodies at the response limit is 512 MiB; a typo should not start 1000 threads."""
    from evalseal.adapters.dataset import Case, Dataset
    from evalseal.adapters.scorer import RegexScorer
    from evalseal.adapters.target import LocalCallableTarget
    from evalseal.executor import MAX_CONCURRENCY, run_eval

    dataset = Dataset([Case("a", "q")], hash="sha256:t")
    target = LocalCallableTarget(lambda p: "yes", name="m")
    with pytest.raises(ValueError) as caught:
        run_eval(dataset, target, RegexScorer("yes"), n_repeats=1,
                 concurrency=MAX_CONCURRENCY + 1)
    assert f"above the limit of {MAX_CONCURRENCY}" in str(caught.value)
    assert "split the suite across runs" in str(caught.value)
    # And the limit itself is usable.
    run_eval(dataset, target, RegexScorer("yes"), n_repeats=1,
             concurrency=MAX_CONCURRENCY)


def test_the_cli_refuses_an_out_of_range_concurrency_as_bad_input(tmp_path):
    from typer.testing import CliRunner

    from evalseal.cli import app
    from evalseal.executor import MAX_CONCURRENCY

    (tmp_path / "ds.jsonl").write_text('{"case_id": "a", "prompt": "p"}\n')
    (tmp_path / "t.json").write_text('{"model": "m"}')
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}')
    result = CliRunner().invoke(app, [
        "run", "--dataset", str(tmp_path / "ds.jsonl"),
        "--target-config", str(tmp_path / "t.json"),
        "--scorer-config", str(tmp_path / "s.json"),
        "--cassette", str(tmp_path / "c.json"), "--n", "1",
        "--concurrency", str(MAX_CONCURRENCY + 1),
        "--ledger", str(tmp_path / "l.jsonl")])
    assert result.exit_code == 2
