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
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
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
            requested_model=self.model,
            served_model=raw.get("model"),
            system_fingerprint=raw.get("system_fingerprint"),
            base_url=self.base_url,
            effective_params={
                "temperature": self.temperature,
                "seed": self.seed,
                "top_p": None,
            },
            params_source=params_source,
        )

    def _post(self, body: dict[str, Any]) -> dict:
        """One real HTTP call, retrying transient failures with backoff."""
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(f"Recording needs an API key in ${self.api_key_env}.")
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}

        for attempt in range(self.max_retries + 1):
            last_response: httpx.Response | None = None
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, headers=headers, json=body)
                if response.status_code not in RETRY_STATUSES:
                    response.raise_for_status()
                    return response.json()
                last_response = response
            except httpx.TransportError:
                if attempt == self.max_retries:
                    raise
            if attempt == self.max_retries:
                assert last_response is not None
                last_response.raise_for_status()
            self.sleep(backoff_seconds(attempt, last_response))
        raise AssertionError("unreachable")  # pragma: no cover
