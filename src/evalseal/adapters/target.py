from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from ..models import CallOrigin, ParamsSource
from ..provenance import text_hash
from ..transport import MAX_RESPONSE_BYTES, Budget, _Clock, request_bounded
from .recording import CallOutcome, Cassette, digest

# Transient conditions: the same request may well succeed a moment later.
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
MAX_BACKOFF_SECONDS = 30.0


@dataclass
class TargetResponse:
    text: str
    requested_model: str
    served_model: str | None
    system_fingerprint: str | None
    base_url: str
    effective_params: dict[str, Any]
    params_source: ParamsSource
    # A response cut off at the token limit is not a wrong answer, it is an absent one.
    # Scored naively it looks like a model failure, so the run says so instead.
    truncated: bool = False
    # Which wire format answered. Defaulted rather than required: TargetResponse is public,
    # and a custom Target written before 1.4 should keep working and report "unknown"
    # rather than fail to construct.
    provider: str = "unknown"
    # The API version this request declared, where the wire format carries one. Part of
    # the instrument for a judge: the same request can be read differently under a
    # different version.
    api_version: str | None = None
    # Hash of the system prompt, or None when none was sent. Never the text: a judge's
    # system prompt steers its verdicts and can be as private as the rubric.
    system_prompt_hash: str | None = None
    # Where this response came from: a real provider call, a cassette entry, an
    # in-process callable, or a custom Target that does not say. Defaulted to
    # "unknown" rather than "live", because "we called the model" is the strongest
    # claim in a receipt and must never be the one a silence produces.
    origin: CallOrigin = "unknown"
    # Digests of the request as sent and the response as used. Empty strings would make
    # an absent digest look like a computed one, so these are None when there is none.
    request_digest: str | None = None
    response_digest: str | None = None
    # The provider's own id for this call, when it gives one: the only handle for
    # checking a receipt against the provider's logs. On a replay it is the id of the
    # call that produced the recorded bytes.
    provider_request_id: str | None = None
    # How many HTTP attempts this call took. 1 means it succeeded first time.
    attempts: int = 1
    # The cassette entry that served this call, or that it was recorded into. None when
    # no cassette was involved. Two calls that returned identical text are two draws
    # when their keys differ and one draw counted twice when they do not, which is a
    # distinction the text alone cannot carry. See docs/metrics.md §12.
    source_key: str | None = None


@runtime_checkable
class Target(Protocol):
    def generate(self, prompt: str) -> TargetResponse: ...


@dataclass
class LocalCallableTarget:
    """Wrap any Python function as a target. Great for offline tests: the function
    can return scripted-variance outputs so the executor is testable with no network."""
    fn: Callable[[str], str]
    name: str = "local"

    def generate(self, prompt: str) -> TargetResponse:
        text = self.fn(prompt)
        return TargetResponse(
            text=text,
            provider="local",
            # Not "live": no provider was involved. Not "replay" either - there is no
            # recording behind it. A third value is the only honest answer.
            origin="local",
            request_digest=digest({"prompt": prompt, "target": self.name}),
            response_digest=digest({"text": text}),
            attempts=0,
            requested_model=self.name,
            served_model=self.name,
            system_fingerprint=None,
            base_url="local://",
            # A Python callable has no sampling parameters at all. Saying "inherited"
            # would claim a provider default that nothing applied.
            effective_params={"unset": dict.fromkeys(
                ("temperature", "seed", "top_p", "max_tokens"), "unsupported")},
            params_source="explicit",
        )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Providers often say exactly how long to wait; prefer that over guessing."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form: fall back to exponential backoff


def backoff_seconds(attempt: int, response: httpx.Response | None = None) -> float:
    """Wait before retry `attempt` (0-based). Retry-After wins; otherwise 1s, 2s, 4s… capped."""
    if response is not None:
        after = _retry_after_seconds(response)
        if after is not None:
            return min(after, MAX_BACKOFF_SECONDS)
    return min(2.0 ** attempt, MAX_BACKOFF_SECONDS)


def _require_key(env_var: str) -> str:
    key = os.environ.get(env_var)
    if not key:
        raise RuntimeError(f"Recording needs an API key in ${env_var}.")
    return key


@dataclass(frozen=True)
class ProviderCall:
    """A provider response and what it took to get it.

    `attempts` is here because a call that failed twice and then succeeded used to be
    recorded exactly like one that succeeded first time. A run that retried its way
    through a rate limit is a different run from one that did not - slower, later, and
    possibly served by a different backend - and the receipt could not say so.
    """
    body: dict
    attempts: int


def post_with_retries(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    max_retries: int,
    timeout: float,
    sleep: Callable[[float], None],
    max_bytes: int = MAX_RESPONSE_BYTES,
    deadline: float | None = None,
) -> ProviderCall:
    """One real HTTP call, retrying transient failures with backoff.

    Shared by every provider adapter. A second copy of this loop would drift, and the
    copy that drifted would be the provider with the fewest tests pointed at it.

    The response body is read under `max_bytes` and the whole call under a total
    deadline - see transport.py for what was unbounded and what it cost. A body over
    the limit is an error, not a truncation: this returns a parsed response or nothing.
    """
    budget = Budget(timeout=timeout, max_retries=max_retries, max_bytes=max_bytes,
                    deadline=deadline)
    clock = _Clock(budget.total_deadline(lambda i: backoff_seconds(i)))
    what = f"the provider at {url}"
    for attempt in range(max_retries + 1):
        last_response: httpx.Response | None = None
        try:
            response, raw = request_bounded(
                method="POST", url=url, headers=headers, json_body=body,
                budget=budget, clock=clock, what=what)
            if response.status_code not in RETRY_STATUSES:
                response.raise_for_status()
                return ProviderCall(_parse_json(raw, url, response), attempt + 1)
            last_response = response
        except httpx.TransportError:
            if attempt == max_retries:
                raise
        if attempt == max_retries:
            assert last_response is not None
            last_response.raise_for_status()
        wait = backoff_seconds(attempt, last_response)
        # Never sleep past the deadline: waiting out the budget and *then* failing
        # wastes the time the budget exists to bound.
        clock.check(what)
        sleep(min(wait, max(0.0, clock.remaining)))
    raise AssertionError("unreachable")  # pragma: no cover


def _parse_json(raw: bytes, url: str, response: httpx.Response) -> dict:
    """Parse a provider response, or say what arrived instead of JSON.

    `response.json()` on an HTML error page from a proxy raised a `JSONDecodeError`
    pointing at character 0, which tells a reader nothing about which endpoint handed
    them a login page.
    """
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        kind = response.headers.get("content-type", "no content-type")
        head = raw[:120].decode("utf-8", "replace").replace("\n", " ")
        raise RuntimeError(
            f"{url} returned {len(raw)} byte(s) of {kind}, which is not JSON: {e}. "
            f"It starts: {head!r}"
        ) from None
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"{url} returned a JSON {type(parsed).__name__}, not an object. A chat "
            "completion is an object; check the endpoint.")
    return parsed


def _request_id(raw: dict) -> str | None:
    """The provider's id for this call, if it gave a usable one."""
    value = raw.get("id")
    return value if isinstance(value, str) and value else None


def _through_cassette(
    cassette: Cassette, payload: dict, post: Callable[[], ProviderCall],
) -> tuple[CallOutcome, int]:
    """Serve `payload` from the cassette or by calling the provider.

    Returns the outcome and how many HTTP attempts it took - 0 for a replay, because no
    attempt was made. Reporting 1 there would quietly turn "we read a file" into "we
    called the model once and it worked".
    """
    attempts = 0

    def real_call() -> dict:
        nonlocal attempts
        call = post()
        attempts = call.attempts
        return call.body

    return cassette.call_detail(payload, real_call), attempts


@dataclass
class OpenAICompatibleTarget:
    """Calls any OpenAI-compatible /chat/completions endpoint, through the cassette."""
    model: str
    cassette: Cassette
    base_url: str = "https://api.openai.com/v1"
    temperature: float | None = None      # None means "we did not set it"
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    # Sent as a leading system message, which is how this wire format carries one.
    # It had no field here at all, so a judge configured with a `system` prompt was
    # graded without it and the receipt said nothing.
    system: str | None = None
    api_key_env: str = "EVALSEAL_API_KEY"
    max_retries: int = 5                     # transient failures only; 0 disables
    timeout: float = 60.0
    #: How much of one response will be read, and how long the whole call may take
    #: including its retries. See transport.py: an unbounded body peaked at 630 MB
    #: against a loopback server returning 200 MB of JSON.
    max_response_bytes: int = MAX_RESPONSE_BYTES
    deadline: float | None = None
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def generate(self, prompt: str) -> TargetResponse:
        # Derived from every sampling parameter this adapter can set, not from
        # `temperature` alone: a run that set only `top_p` was reported as inheriting
        # every default.
        params_source: ParamsSource = (
            "explicit"
            if any(v is not None for v in
                   (self.temperature, self.top_p, self.max_tokens, self.seed))
            else "provider_default"
        )
        messages: list[dict[str, str]] = []
        if self.system is not None:
            messages.append({"role": "system", "content": self.system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.seed is not None:
            body["seed"] = self.seed
        # Transmitted, not dropped. This adapter had no fields for these two, so a
        # config asking for `top_p: 0.1` ran unconstrained and the receipt recorded
        # `top_p: null` as though that were the effective value.
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens

        # Cassette key is the full effective body (never the API key).
        outcome, attempts = _through_cassette(
            self.cassette, {"url": self.base_url, "body": body}, lambda: self._post(body))
        raw = outcome.response

        return TargetResponse(
            text=raw["choices"][0]["message"]["content"] or "",
            provider="openai_compatible",
            requested_model=self.model,
            served_model=raw.get("model"),
            system_fingerprint=raw.get("system_fingerprint"),
            base_url=self.base_url,
            effective_params={
                "temperature": self.temperature,
                "seed": self.seed,
                "top_p": self.top_p,
                "max_tokens": self.max_tokens,
                # Every parameter this adapter can send but did not. The provider
                # applied its own default and this receipt does not know which.
                "unset": {
                    name: "inherited" for name, value in (
                        ("temperature", self.temperature), ("seed", self.seed),
                        ("top_p", self.top_p), ("max_tokens", self.max_tokens))
                    if value is None
                },
            },
            params_source=params_source,
            # This wire format declares no version, so there is none to record. None
            # here means "the format carries none", not "we failed to look".
            api_version=None,
            system_prompt_hash=text_hash(self.system) if self.system is not None else None,
            truncated=raw["choices"][0].get("finish_reason") == "length",
            origin=outcome.origin,
            request_digest=outcome.request_digest,
            response_digest=outcome.response_digest,
            source_key=outcome.entry_key,
            provider_request_id=_request_id(raw),
            attempts=attempts,
        )

    def _post(self, body: dict[str, Any]) -> ProviderCall:
        key = _require_key(self.api_key_env)
        return post_with_retries(
            url=f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            body=body,
            max_retries=self.max_retries,
            timeout=self.timeout,
            sleep=self.sleep,
            max_bytes=self.max_response_bytes,
            deadline=self.deadline,
        )


@dataclass
class AnthropicTarget:
    """Calls the Anthropic Messages API natively, through the cassette.

    Not an OpenAI shim. The wire format differs in ways that matter to a receipt:
    `max_tokens` is required rather than optional, the system prompt is a top-level
    field rather than a message, the reply is a list of content blocks rather than a
    string, there is no `system_fingerprint` to pin a backend with, and an overload is
    529 rather than 503. A shim papers over each of those, and a receipt that records
    what a shim assumed is a receipt about the shim.

    Implemented on httpx rather than the SDK: the cassette records the raw response
    envelope, and that is also what gets replayed, so there is no client library
    sitting between the recording and what a reader can verify.
    """
    model: str
    cassette: Cassette
    base_url: str = "https://api.anthropic.com/v1"
    max_tokens: int = 1024                # required by the API; never a silent default
    temperature: float | None = None      # None means "we did not set it"
    top_p: float | None = None
    system: str | None = None
    api_key_env: str = "ANTHROPIC_API_KEY"
    anthropic_version: str = "2023-06-01"
    max_retries: int = 5
    timeout: float = 60.0
    #: As on the OpenAI-compatible adapter: one bound per adapter, not one per wire
    #: format, or the limit a reader relies on depends on which provider answered.
    max_response_bytes: int = MAX_RESPONSE_BYTES
    deadline: float | None = None
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    @property
    def kind(self) -> str:
        return "anthropic"

    def generate(self, prompt: str) -> TargetResponse:
        # `max_tokens` is always sent here because the API requires it, so it says
        # nothing about whether the caller chose the sampling parameters.
        params_source: ParamsSource = (
            "explicit"
            if any(v is not None for v in (self.temperature, self.top_p))
            else "provider_default"
        )
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.system is not None:
            body["system"] = self.system
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.top_p is not None:
            body["top_p"] = self.top_p

        # Keyed on the full effective body, exactly as the OpenAI adapter is, so a
        # cassette entry means the same thing whichever provider recorded it.
        outcome, attempts = _through_cassette(
            self.cassette, {"url": self.base_url, "body": body}, lambda: self._post(body))
        raw = outcome.response

        return TargetResponse(
            text=extract_text(raw),
            provider="anthropic",
            requested_model=self.model,
            served_model=raw.get("model"),
            # Anthropic exposes no backend identifier. Reporting None is honest;
            # inventing one from the model name would fake a pin that does not exist.
            system_fingerprint=None,
            base_url=self.base_url,
            effective_params={
                "temperature": self.temperature,
                "seed": None,             # the API has no seed parameter
                "top_p": self.top_p,
                "max_tokens": self.max_tokens,
                # `seed` is absent because this API has no such parameter, not because
                # we declined to set one. `max_tokens` is always sent, so it is never
                # absent. Both distinctions were lost in a bare `null`.
                "unset": {
                    "seed": "unsupported",
                    **{name: "inherited"
                       for name, value in (("temperature", self.temperature),
                                           ("top_p", self.top_p))
                       if value is None},
                },
            },
            params_source=params_source,
            api_version=self.anthropic_version,
            system_prompt_hash=text_hash(self.system) if self.system is not None else None,
            truncated=raw.get("stop_reason") == "max_tokens",
            origin=outcome.origin,
            request_digest=outcome.request_digest,
            response_digest=outcome.response_digest,
            source_key=outcome.entry_key,
            provider_request_id=_request_id(raw),
            attempts=attempts,
        )

    def _post(self, body: dict[str, Any]) -> ProviderCall:
        key = _require_key(self.api_key_env)
        return post_with_retries(
            url=f"{self.base_url.rstrip('/')}/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": self.anthropic_version,
                "content-type": "application/json",
            },
            body=body,
            max_retries=self.max_retries,
            timeout=self.timeout,
            sleep=self.sleep,
            max_bytes=self.max_response_bytes,
            deadline=self.deadline,
        )


def extract_text(raw: dict) -> str:
    """Join the text blocks of a Messages reply, skipping any other block type.

    The reply is a list, not a string. Taking `content[0]` works until the first
    response that opens with a thinking or tool_use block, and then it silently scores
    an empty answer.
    """
    blocks = raw.get("content") or []
    return "".join(
        b.get("text", "") for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    )
