from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

ParamsSource = Literal["explicit", "provider_default"]
ScorerKind = Literal["exact", "regex", "llm_judge", "answer_match"]
FailOn = Literal["none", "unstable", "borderline"]

SCHEMA_VERSION = "1.1"  # 1.1 added run_config.concurrency; it changes record hashes


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


class DatasetProvenance(BaseModel):
    hash: str
    n_cases: int


class RunConfig(BaseModel):
    n_repeats: int = 5
    concurrency: int = 1
    harness_version: str = "evalseal/1.1.0"
    started_at: str = Field(default_factory=_now)


class ProvenanceManifest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    target: TargetProvenance
    scorer: ScorerProvenance
    dataset: DatasetProvenance
    run_config: RunConfig


class CaseResult(BaseModel):
    case_id: str
    scores: list[float]
    mean: float
    ci95: tuple[float, float]
    flip_rate: float
    stability: str
    majority_verdict: int | None = None
    seconds: float = 0.0


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
