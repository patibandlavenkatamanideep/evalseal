from __future__ import annotations

from dataclasses import dataclass, field

from conftest import make_dataset

from evalseal.adapters.scorer import LLMJudgeScorer, RegexScorer
from evalseal.adapters.target import TargetResponse
from evalseal.executor import run_eval


@dataclass
class FakeProviderTarget:
    """Returns canned provider metadata, as a real API response would."""
    requested: str = "gpt-4o-mini"
    served: list[str] = field(default_factory=lambda: ["gpt-4o-mini"])
    fingerprints: list[str | None] = field(default_factory=lambda: [None])
    base_url: str = "https://api.openai.com/v1"
    temperature: float | None = 0.0
    text: str = "yes"
    _i: int = 0

    def generate(self, prompt: str) -> TargetResponse:
        i = self._i
        self._i += 1
        return TargetResponse(
            text=self.text,
            requested_model=self.requested,
            served_model=self.served[i % len(self.served)],
            system_fingerprint=self.fingerprints[i % len(self.fingerprints)],
            base_url=self.base_url,
            effective_params={"temperature": self.temperature, "seed": None, "top_p": None},
            params_source="explicit" if self.temperature is not None else "provider_default",
        )


def _warnings(target, scorer=None):
    rec = run_eval(make_dataset("q"), target, scorer or RegexScorer("yes"), n_repeats=5)
    return rec.aggregate.warnings


def test_served_model_mismatch_warns():
    w = _warnings(FakeProviderTarget(requested="gpt-4o", served=["gpt-4o-mini"]))
    assert any("TARGET SERVED MODEL MISMATCH" in x and "'gpt-4o-mini'" in x for x in w)


def test_mismatch_on_any_call_warns_not_just_the_last():
    w = _warnings(FakeProviderTarget(served=["gpt-3.5-turbo", "gpt-4o-mini"]))
    assert any("SERVED MODEL MISMATCH" in x for x in w)
    assert any("SERVED MODEL CHANGED DURING RUN" in x for x in w)


def test_dated_snapshot_of_requested_model_is_not_a_mismatch():
    w = _warnings(FakeProviderTarget(served=["gpt-4o-mini-2024-07-18"]))
    assert w == []


def test_clean_run_has_no_warnings():
    assert _warnings(FakeProviderTarget()) == []


def test_non_canonical_endpoint_warns():
    w = _warnings(FakeProviderTarget(base_url="https://cheap-proxy.example.com/v1"))
    assert any("NON-CANONICAL ENDPOINT" in x for x in w)


def test_unset_temperature_warns():
    w = _warnings(FakeProviderTarget(temperature=None))
    assert any("TARGET TEMPERATURE NOT SET" in x for x in w)


def test_fingerprint_drift_warns():
    w = _warnings(FakeProviderTarget(fingerprints=["fp_a", "fp_b"]))
    assert any("SYSTEM FINGERPRINT CHANGED DURING RUN" in x for x in w)


def test_judge_provenance_gaps_are_reported_separately():
    judge = FakeProviderTarget(requested="gpt-4o", served=["gpt-4o-mini"], temperature=None,
                               text="PASS")
    w = _warnings(FakeProviderTarget(), LLMJudgeScorer(judge=judge, rubric="r"))
    assert any(x.startswith("JUDGE SERVED MODEL MISMATCH") for x in w)
    assert any(x.startswith("JUDGE TEMPERATURE NOT SET") for x in w)
    assert not any(x.startswith("TARGET") for x in w)
    assert len(w) == len(set(w))  # deduplicated across repeats
