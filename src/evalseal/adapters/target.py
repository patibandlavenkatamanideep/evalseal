from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from ..models import ParamsSource
from .recording import Cassette

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
        return TargetResponse(
            text=self.fn(prompt),
            provider="local",
            requested_model=self.name,
            served_model=self.name,
            system_fingerprint=None,
            base_url="local://",
            effective_params={},
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


def post_with_retries(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    max_retries: int,
    timeout: float,
    sleep: Callable[[float], None],
) -> dict:
    """One real HTTP call, retrying transient failures with backoff.

    Shared by every provider adapter. A second copy of this loop would drift, and the
    copy that drifted would be the provider with the fewest tests pointed at it.
    """
    for attempt in range(max_retries + 1):
        last_response: httpx.Response | None = None
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, headers=headers, json=body)
            if response.status_code not in RETRY_STATUSES:
                response.raise_for_status()
                return response.json()
            last_response = response
        except httpx.TransportError:
            if attempt == max_retries:
                raise
        if attempt == max_retries:
            assert last_response is not None
            last_response.raise_for_status()
        sleep(backoff_seconds(attempt, last_response))
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class OpenAICompatibleTarget:
    """Calls any OpenAI-compatible /chat/completions endpoint, through the cassette."""
    model: str
    cassette: Cassette
    base_url: str = "https://api.openai.com/v1"
    temperature: float | None = None      # None means "we did not set it"
    seed: int | None = None
    api_key_env: str = "EVALSEAL_API_KEY"
    max_retries: int = 5                     # transient failures only; 0 disables
    timeout: float = 60.0
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def generate(self, prompt: str) -> TargetResponse:
        # Record whether we explicitly set temperature or are inheriting the default.
        params_source: ParamsSource = (
            "explicit" if self.temperature is not None else "provider_default"
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.seed is not None:
            body["seed"] = self.seed

        # Cassette key is the full effective body (never the API key).
        raw = self.cassette.call({"url": self.base_url, "body": body}, lambda: self._post(body))

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
                "top_p": None,
                "max_tokens": None,     # this adapter never sets one
            },
            params_source=params_source,
            truncated=raw["choices"][0].get("finish_reason") == "length",
        )

    def _post(self, body: dict[str, Any]) -> dict:
        key = _require_key(self.api_key_env)
        return post_with_retries(
            url=f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            body=body,
            max_retries=self.max_retries,
            timeout=self.timeout,
            sleep=self.sleep,
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
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    @property
    def kind(self) -> str:
        return "anthropic"

    def generate(self, prompt: str) -> TargetResponse:
        params_source: ParamsSource = (
            "explicit" if self.temperature is not None else "provider_default"
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
        raw = self.cassette.call({"url": self.base_url, "body": body}, lambda: self._post(body))

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
            },
            params_source=params_source,
            truncated=raw.get("stop_reason") == "max_tokens",
        )

    def _post(self, body: dict[str, Any]) -> dict:
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
