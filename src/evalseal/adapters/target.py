from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, runtime_checkable

import httpx

from .recording import Cassette


@dataclass
class TargetResponse:
    text: str
    requested_model: str
    served_model: Optional[str]
    system_fingerprint: Optional[str]
    base_url: str
    effective_params: dict[str, Any]
    params_source: str  # "explicit" | "provider_default"


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


@dataclass
class OpenAICompatibleTarget:
    """Calls any OpenAI-compatible /chat/completions endpoint, through the cassette."""
    model: str
    cassette: Cassette
    base_url: str = "https://api.openai.com/v1"
    temperature: Optional[float] = None      # None means "we did not set it"
    seed: Optional[int] = None
    api_key_env: str = "EVALSEAL_API_KEY"

    def generate(self, prompt: str) -> TargetResponse:
        # Record whether we explicitly set temperature or are inheriting the default.
        params_source = "explicit" if self.temperature is not None else "provider_default"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.seed is not None:
            body["seed"] = self.seed

        def do_real_call() -> dict:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise RuntimeError(f"Recording needs an API key in ${self.api_key_env}.")
            with httpx.Client(timeout=60) as c:
                r = c.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=body,
                )
                r.raise_for_status()
                return r.json()

        # Cassette key is the full effective body (never the API key).
        raw = self.cassette.call({"url": self.base_url, "body": body}, do_real_call)

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
