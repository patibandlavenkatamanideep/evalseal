from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EffectiveParams(BaseModel):
    temperature: Optional[float] = None
    seed: Optional[int] = None
    top_p: Optional[float] = None


class TargetProvenance(BaseModel):
    requested_model: str
    served_model: Optional[str] = None        # from response; WARN if != requested
    system_fingerprint: Optional[str] = None
    base_url: str
    effective_params: EffectiveParams
    params_source: Literal["explicit", "provider_default"] = "explicit"


class ScorerProvenance(BaseModel):
    type: Literal["exact", "regex", "llm_judge"]
    judge: Optional[TargetProvenance] = None  # the judge is a target too
    rubric_hash: Optional[str] = None


class DatasetProvenance(BaseModel):
    hash: str
    n_cases: int


class RunConfig(BaseModel):
    n_repeats: int = 5
    harness_version: str = "evalseal/0.1.0"
    started_at: str = Field(default_factory=_now)


class ProvenanceManifest(BaseModel):
    schema_version: str = "1.0"
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
    majority_verdict: Optional[int] = None


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
