"""Retry/backoff on transient provider failures. No real network, no real sleeping."""
from __future__ import annotations

import httpx
import pytest

from evalseal.adapters.recording import Cassette
from evalseal.adapters.target import MAX_BACKOFF_SECONDS, OpenAICompatibleTarget, backoff_seconds


def _response(status: int, *, text: str = "hi", headers: dict | None = None) -> httpx.Response:
    body = {"model": "m", "choices": [{"message": {"content": text}}]}
    return httpx.Response(
        status, json=body, headers=headers or {}, request=httpx.Request("POST", "https://x/y")
    )


def _target(tmp_path, monkeypatch, responses, **kw):
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    queue = list(responses)
    sent = []

    def fake_post(self, url, headers=None, json=None):
        sent.append(json)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    slept: list[float] = []
    target = OpenAICompatibleTarget(
        model="m", cassette=Cassette(tmp_path / "c.json", record=True),
        sleep=slept.append, **kw,
    )
    return target, slept, sent


def test_retries_429_then_succeeds(tmp_path, monkeypatch):
    target, slept, sent = _target(
        tmp_path, monkeypatch, [_response(429), _response(429), _response(200, text="ok")]
    )
    assert target.generate("p").text == "ok"
    assert slept == [1.0, 2.0]   # exponential, no wall-clock delay in tests
    assert len(sent) == 3


def test_retry_after_header_is_honored(tmp_path, monkeypatch):
    target, slept, _ = _target(
        tmp_path, monkeypatch,
        [_response(429, headers={"Retry-After": "7"}), _response(200, text="ok")],
    )
    assert target.generate("p").text == "ok"
    assert slept == [7.0]


def test_gives_up_after_max_retries(tmp_path, monkeypatch):
    target, slept, sent = _target(
        tmp_path, monkeypatch, [_response(429)] * 3, max_retries=2
    )
    with pytest.raises(httpx.HTTPStatusError) as e:
        target.generate("p")
    assert e.value.response.status_code == 429
    assert len(sent) == 3          # initial attempt + 2 retries
    assert slept == [1.0, 2.0]


def test_transport_errors_are_retried(tmp_path, monkeypatch):
    target, slept, _ = _target(
        tmp_path, monkeypatch,
        [httpx.ConnectError("boom"), _response(200, text="ok")],
    )
    assert target.generate("p").text == "ok"
    assert slept == [1.0]


def test_non_transient_errors_are_not_retried(tmp_path, monkeypatch):
    target, slept, sent = _target(tmp_path, monkeypatch, [_response(400)])
    with pytest.raises(httpx.HTTPStatusError):
        target.generate("p")
    assert len(sent) == 1 and slept == []


def test_retries_disabled(tmp_path, monkeypatch):
    target, slept, sent = _target(tmp_path, monkeypatch, [_response(429)], max_retries=0)
    with pytest.raises(httpx.HTTPStatusError):
        target.generate("p")
    assert len(sent) == 1 and slept == []


def test_backoff_is_capped():
    assert backoff_seconds(0) == 1.0
    assert backoff_seconds(3) == 8.0
    assert backoff_seconds(99) == MAX_BACKOFF_SECONDS
    assert backoff_seconds(0, _response(429, headers={"Retry-After": "999"})) == MAX_BACKOFF_SECONDS
    # Unparseable (HTTP-date) Retry-After falls back to exponential.
    http_date = _response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert backoff_seconds(2, http_date) == 4.0
