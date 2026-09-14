from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, runtime_checkable


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
