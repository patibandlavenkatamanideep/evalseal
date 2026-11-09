"""Strict schemas for the target and scorer configuration files.

## Why this module exists

`_build_target` read the keys it knew about with `cfg.get(...)` and ignored the rest.
Three things followed, all of them silent:

- **`top_p` and `max_tokens` were dropped for OpenAI-compatible targets.** The adapter
  had no fields for them and never sent them. A config asking for `top_p: 0.1` ran
  unconstrained, and the receipt recorded `top_p: null` - which reads as an effective
  value rather than as "we never set one".
- **A typo was indistinguishable from a decision.** `tempreature: 0.9` beside
  `temperature: 0.0` is accepted, ignored, and the run uses the value that *was*
  spelled correctly. Nothing in the output mentions the other key.
- **An unsupported key looked supported.** Any key a future provider might take could
  be written into a config today and would do nothing.

This is the same failure `policy.py` was written to avoid, one layer out: a setting
that quietly does nothing is worse than an unsupported one, because the run completes
and the receipt looks like it describes what was asked for.

So the configuration files are now validated against models that **forbid unknown
keys**, and the error names the nearest legal key when one is close. Every field here
is one the adapters actually transmit.

## What is deliberately not here

The judge's `base_url`, `system` and `api_key_env` are read from the scorer file
*without* the `judge_` prefix, because that is how they have always been read and
renaming them would break existing configs. They are declared on `ScorerConfig` with
that history stated rather than quietly accepted by an `extra="allow"`.
"""

from __future__ import annotations

import difflib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import ScorerKind


class ConfigError(ValueError):
    """A configuration file asks for something this build does not support."""


class _Strict(BaseModel):
    # A key this build does not read must not look like a setting that took effect.
    model_config = ConfigDict(extra="forbid")


class OpenAITargetConfig(_Strict):
    """An OpenAI-compatible `/chat/completions` target.

    `top_p` and `max_tokens` are supported and transmitted. They were previously
    neither: the adapter had no field for them, so a config naming them ran without
    them.
    """

    provider: Literal["openai"] = "openai"
    model: str
    base_url: str = "https://api.openai.com/v1"
    #: Ranges are the provider's documented ones. A value outside them is rejected
    #: here rather than by an HTTP 400 halfway through a run.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, gt=0)
    seed: int | None = None
    api_key_env: str = "EVALSEAL_API_KEY"


class AnthropicTargetConfig(_Strict):
    """An Anthropic Messages API target.

    `max_tokens` is required by the API and so has a default here; the adapter always
    sends one. Anthropic's `temperature` range is 0..1, not 0..2 - a difference worth
    enforcing rather than passing through to a 400.
    """

    provider: Literal["anthropic"]
    model: str
    base_url: str = "https://api.anthropic.com/v1"
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1024, gt=0)
    system: str | None = None
    api_key_env: str = "ANTHROPIC_API_KEY"


TargetConfig = Annotated[
    OpenAITargetConfig | AnthropicTargetConfig, Field(discriminator="provider")]


class ScorerConfig(_Strict):
    """A scorer file, including the judge configured with `judge_`-prefixed keys."""

    type: ScorerKind
    pattern: str | None = None
    tolerance: float | None = Field(default=None, ge=0.0)
    rubric: str | None = None

    judge_provider: Literal["openai", "anthropic"] | None = None
    judge_model: str | None = None
    judge_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    judge_top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    judge_max_tokens: int | None = Field(default=None, gt=0)
    judge_seed: int | None = None

    # Historically unprefixed, and shared with the judge. Kept as they are, because
    # renaming them would reject every scorer file that works today.
    base_url: str | None = None
    system: str | None = None
    api_key_env: str | None = None


def _suggest(unknown: str, legal: list[str]) -> str:
    close = difflib.get_close_matches(unknown, legal, n=2, cutoff=0.7)
    if not close:
        return ""
    return f"; did you mean {' or '.join(repr(c) for c in close)}?"


def _report(error: ValidationError, what: str, legal: list[str]) -> ConfigError:
    """Turn a pydantic error into a message that names the key and what to do."""
    lines = []
    for item in error.errors():
        location = ".".join(str(p) for p in item["loc"]) or "(root)"
        if item["type"] == "extra_forbidden":
            lines.append(
                f"{location}: {what} does not support this key{_suggest(location, legal)}. "
                f"Supported: {', '.join(legal)}")
        else:
            lines.append(f"{location}: {item['msg']}")
    return ConfigError(f"{what} is not usable:\n- " + "\n- ".join(lines))


def load_target_config(data: dict, *, what: str = "the target config") -> TargetConfig:
    """Validate a target configuration, rejecting keys this build does not transmit."""
    provider = data.get("provider", "openai")
    model = (AnthropicTargetConfig if provider == "anthropic" else OpenAITargetConfig)
    if provider not in ("openai", "anthropic"):
        # Wording kept as it was, because it is pinned by a test and read by users:
        # an error message is part of the interface.
        raise ConfigError(
            f"unknown provider {provider!r}; expected 'openai' (any OpenAI-compatible "
            f"endpoint) or 'anthropic'. From {what}")
    try:
        return model.model_validate({**data, "provider": provider})
    except ValidationError as e:
        raise _report(e, what, sorted(model.model_fields)) from None


def load_scorer_config(data: dict, *, what: str = "the scorer config") -> ScorerConfig:
    """Validate a scorer configuration, judge keys included."""
    try:
        return ScorerConfig.model_validate(data)
    except ValidationError as e:
        raise _report(e, what, sorted(ScorerConfig.model_fields)) from None


def judge_config(scorer: ScorerConfig) -> dict:
    """The judge's own configuration, as a target config would be written.

    Translates the `judge_`-prefixed keys into target vocabulary, so the judge is
    validated by the same schemas as a target instead of a second, looser path.
    """
    provider = scorer.judge_provider or "openai"
    out: dict[str, object] = {"provider": provider, "model": scorer.judge_model}
    for source, name in (("judge_temperature", "temperature"),
                         ("judge_top_p", "top_p"),
                         ("judge_max_tokens", "max_tokens"),
                         ("judge_seed", "seed")):
        value = getattr(scorer, source)
        if value is not None:
            out[name] = value
    if scorer.base_url is not None:
        out["base_url"] = scorer.base_url
    if scorer.api_key_env is not None:
        out["api_key_env"] = scorer.api_key_env
    if provider == "anthropic" and scorer.system is not None:
        out["system"] = scorer.system
    if provider == "anthropic" and "max_tokens" not in out:
        out["max_tokens"] = 1024
    if provider == "openai":
        out.pop("system", None)
    return out


__all__ = [
    "AnthropicTargetConfig",
    "ConfigError",
    "OpenAITargetConfig",
    "ScorerConfig",
    "TargetConfig",
    "judge_config",
    "load_scorer_config",
    "load_target_config",
]
