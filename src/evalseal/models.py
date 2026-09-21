from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

ParamsSource = Literal["explicit", "provider_default"]
ScorerKind = Literal["exact", "regex", "llm_judge", "answer_match"]
FailOn = Literal["none", "unstable", "borderline"]

# 1.2 seals the evaluator configuration (suite, judge prompt, code, environment) and
# per-case verdict detail. Any added field changes record hashes, hence the bump.
SCHEMA_VERSION = "1.2"

Stability = Literal["stable_pass", "stable_fail", "unstable", "insufficient_runs"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class EffectiveParams(BaseModel):
    temperature: float | None = None
    seed: int | None = None
    top_p: float | None = None


class TargetProvenance(BaseModel):
    requested_model: str
    served_model: str | None = None        # from response; WARN if != requested
    system_fingerprint: str | None = None
    base_url: str
    effective_params: EffectiveParams
    params_source: ParamsSource = "explicit"


class ScorerProvenance(BaseModel):
    type: ScorerKind
    judge: TargetProvenance | None = None  # the judge is a target too
    rubric_hash: str | None = None
    # The rubric is what a user edits; the judge prompt is what the model actually saw,
    # including the instruction wrapper. Sealing both means a wrapper change is visible.
    judge_prompt_hash: str | None = None
    judge_prompt: str | None = None        # only when --store-judge-prompt is passed


class SuiteProvenance(BaseModel):
    name: str | None = None
    path: str | None = None
    hash: str | None = None


class CodeProvenance(BaseModel):
    commit: str | None = None
    dirty: bool | None = None              # True means the commit does not describe the run


class EnvironmentProvenance(BaseModel):
    evalseal_version: str | None = None
    python_version: str | None = None
    platform: str | None = None
    implementation: str | None = None


class DatasetProvenance(BaseModel):
    hash: str
    n_cases: int
    path: str | None = None
    case_ids: list[str] = Field(default_factory=list)


class RunConfig(BaseModel):
    n_repeats: int = 5
    concurrency: int = 1
    harness_version: str = "evalseal/2.0.0"
    started_at: str = Field(default_factory=_now)


class ProvenanceManifest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    target: TargetProvenance
    scorer: ScorerProvenance
    dataset: DatasetProvenance
    run_config: RunConfig
    suite: SuiteProvenance | None = None
    code: CodeProvenance = Field(default_factory=CodeProvenance)
    environment: EnvironmentProvenance = Field(default_factory=EnvironmentProvenance)


class CaseResult(BaseModel):
    case_id: str
    scores: list[float]
    mean: float
    ci95: tuple[float, float]
    flip_rate: float
    stability: str                         # STABLE | BORDERLINE | UNSTABLE (unchanged)
    stability_label: Stability = "insufficient_runs"   # the finer taxonomy
    majority_verdict: int | None = None
    seconds: float = 0.0
    # Per-case verdict detail. `verdicts` is the observed sequence in run order, so a
    # reader can see *when* a case flipped rather than only how often.
    verdicts: list[int] = Field(default_factory=list)
    pass_count: int = 0
    fail_count: int = 0
    other_count: int = 0                   # non-binary or unparseable outcomes
    flip_count: int = 0                    # verdicts disagreeing with the majority

    @property
    def n_runs(self) -> int:
        return len(self.scores)


class Aggregate(BaseModel):
    n_cases: int
    mean_score: float
    n_stable: int
    n_borderline: int
    n_unstable: int
    warnings: list[str] = []


class RunRecord(BaseModel):
    manifest: ProvenanceManifest
    results: list[CaseResult]
    aggregate: Aggregate
    prev_hash: str
    hash: str = ""            # filled by the ledger at seal time
    created_at: str = Field(default_factory=_now)
