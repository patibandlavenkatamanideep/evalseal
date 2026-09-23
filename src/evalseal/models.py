from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

ParamsSource = Literal["explicit", "provider_default"]
ScorerKind = Literal["exact", "regex", "llm_judge", "answer_match"]
FailOn = Literal["none", "unstable", "borderline"]

# 1.2 seals the evaluator configuration (suite, judge prompt, code, environment) and
# per-case verdict detail. Any added field changes record hashes, hence the bump.
# 1.4 adds the fields the evaluator fingerprint was missing: the provider and
# `max_tokens` for the target and the judge, and a hash of the scorer's own settings.
# Older records still verify, re-hashed under the schema that sealed them.
# 1.3 adds no field; it changes what `scorer.judge_prompt_hash` means. Under 1.2 it
# hashed the last judge prompt sent, which varied with the target's responses and with
# scheduling. Under 1.3 it hashes the judge prompt template. A 1.2 judge record and a
# 1.3 one therefore disagree on this hash for that reason alone.
SCHEMA_VERSION = "1.4"

Stability = Literal["stable_pass", "stable_fail", "unstable", "insufficient_runs"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class EffectiveParams(BaseModel):
    """Sampling parameters that change what a model returns, and so what it scores.

    `max_tokens` is here from schema 1.4. The Anthropic adapter always sends it, but
    nothing recorded it, so a receipt could not explain a truncated verdict.
    """
    temperature: float | None = None
    seed: int | None = None
    top_p: float | None = None
    max_tokens: int | None = None


class TargetProvenance(BaseModel):
    provider: str | None = None            # openai_compatible | anthropic | local
    requested_model: str
    served_model: str | None = None        # from response; WARN if != requested
    system_fingerprint: str | None = None
    base_url: str
    effective_params: EffectiveParams
    params_source: ParamsSource = "explicit"


class ScorerProvenance(BaseModel):
    type: ScorerKind
    # Hash of the scorer's own verdict-affecting settings: a regex pattern, a numeric
    # tolerance. Two runs graded by different regexes used to fingerprint identically.
    config_hash: str | None = None
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


ArtifactKind = Literal["hashed", "embedded", "external"]


class ArtifactProvenance(BaseModel):
    """A file the run depended on, bound to the receipt by hash.

    `kind` says what the receipt holds, so a reader is never left guessing:
      hashed   - the digest is here, the bytes are not. The default, and the only kind
                 that is safe for a file holding prompts or responses.
      embedded - the content itself is in the receipt, because someone asked for it.
      external - the file was named but not readable when the record was sealed, so
                 there is nothing to check it against.
    """
    role: str                              # cassette | dataset | suite | ...
    kind: ArtifactKind = "hashed"
    sha256: str | None = None
    path: str | None = None
    note: str | None = None


class DatasetProvenance(BaseModel):
    hash: str
    n_cases: int
    path: str | None = None
    case_ids: list[str] = Field(default_factory=list)


class RunConfig(BaseModel):
    n_repeats: int = 5
    concurrency: int = 1
    harness_version: str = "evalseal/2.0.1"
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
    # The files this run depended on, by digest. The cassette is the one that matters:
    # it holds the responses the verdicts came from, and until 1.4 nothing bound it to
    # the receipt, so the responses could be swapped without the receipt noticing.
    artifacts: list[ArtifactProvenance] = Field(default_factory=list)


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
