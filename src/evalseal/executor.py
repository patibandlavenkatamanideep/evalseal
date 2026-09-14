from __future__ import annotations

import re
from urllib.parse import urlparse

from .adapters.dataset import Dataset
from .adapters.scorer import Scorer
from .adapters.target import Target, TargetResponse
from .analyze import analyze_case
from .models import (
    Aggregate, CaseResult, DatasetProvenance, EffectiveParams,
    ProvenanceManifest, RunConfig, RunRecord, ScorerProvenance, TargetProvenance,
)

_CANONICAL_HOSTS = {"api.openai.com", "generativelanguage.googleapis.com"}
_LOCAL_SCHEMES = {"local"}


def _to_provenance(tr: TargetResponse) -> TargetProvenance:
    return TargetProvenance(
        requested_model=tr.requested_model,
        served_model=tr.served_model,
        system_fingerprint=tr.system_fingerprint,
        base_url=tr.base_url,
        effective_params=EffectiveParams(**tr.effective_params),
        params_source=tr.params_source,
    )


def _is_same_model(requested: str, served: str) -> bool:
    # Providers resolve aliases to dated snapshots (gpt-4o-mini -> gpt-4o-mini-2024-07-18,
    # gpt-4 -> gpt-4-0613). That is the same model; the snapshot is still recorded in
    # served_model. A bare prefix check is not enough: gpt-4o-mini is not gpt-4o.
    snapshot = re.compile(rf"{re.escape(requested)}-(\d{{4}}-\d{{2}}-\d{{2}}|\d{{4}})")
    return served == requested or snapshot.fullmatch(served) is not None


def _provenance_warnings(tp: TargetProvenance, role: str = "TARGET") -> list[str]:
    w = []
    if tp.served_model and tp.requested_model and not _is_same_model(
        tp.requested_model, tp.served_model
    ):
        w.append(
            f"{role} SERVED MODEL MISMATCH: requested '{tp.requested_model}' but served "
            f"'{tp.served_model}'. The report's model name may not be what ran."
        )
    parsed = urlparse(tp.base_url)
    if parsed.scheme not in _LOCAL_SCHEMES and parsed.hostname not in _CANONICAL_HOSTS:
        w.append(
            f"{role} NON-CANONICAL ENDPOINT: {tp.base_url} — responses may be proxied/altered."
        )
    if tp.params_source == "provider_default" and tp.effective_params.temperature is None:
        w.append(
            f"{role} TEMPERATURE NOT SET: the provider default (often 1.0) was used silently; "
            "verdicts near the decision boundary may not be reproducible."
        )
    return w


def _drift_warnings(responses: list[TargetResponse], role: str) -> list[str]:
    """The backend can change under you mid-run. Surface it instead of averaging over it."""
    w = []
    served = sorted({r.served_model for r in responses if r.served_model})
    if len(served) > 1:
        w.append(f"{role} SERVED MODEL CHANGED DURING RUN: {', '.join(served)}")
    fps = sorted({r.system_fingerprint for r in responses if r.system_fingerprint})
    if len(fps) > 1:
        w.append(f"{role} SYSTEM FINGERPRINT CHANGED DURING RUN: {', '.join(fps)}")
    return w


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def run_eval(
    dataset: Dataset,
    target: Target,
    scorer: Scorer,
    n_repeats: int = 5,
    prev_hash: str = "GENESIS",
) -> RunRecord:
    if not dataset.cases:
        raise ValueError("dataset has no cases")
    if n_repeats < 1:
        raise ValueError("n_repeats must be >= 1")

    results: list[CaseResult] = []
    target_resps: list[TargetResponse] = []
    judge_resps: list[TargetResponse] = []

    for case in dataset.cases:
        scores: list[float] = []
        binary = True
        for _ in range(n_repeats):
            tr = target.generate(case.prompt)
            target_resps.append(tr)
            sr = scorer.score(case.prompt, tr.text, case.expected)
            if sr.judge_response is not None:
                judge_resps.append(sr.judge_response)
            scores.append(sr.score)
            binary = binary and sr.binary
        stats = analyze_case(scores, binary=binary)
        results.append(CaseResult(
            case_id=case.case_id,
            scores=scores,
            mean=stats.mean,
            ci95=(stats.ci95_low, stats.ci95_high),
            flip_rate=stats.flip_rate,
            stability=stats.stability,
            majority_verdict=stats.majority_verdict,
        ))

    # Every response is checked, not just the last one: a mismatch on any call matters.
    warnings: list[str] = []
    for tr in target_resps:
        warnings += _provenance_warnings(_to_provenance(tr), "TARGET")
    warnings += _drift_warnings(target_resps, "TARGET")
    for jr in judge_resps:
        warnings += _provenance_warnings(_to_provenance(jr), "JUDGE")
    warnings += _drift_warnings(judge_resps, "JUDGE")

    sp = ScorerProvenance(
        type=scorer.kind,
        judge=_to_provenance(judge_resps[0]) if judge_resps else None,
        rubric_hash=getattr(scorer, "rubric_hash", None),
    )
    manifest = ProvenanceManifest(
        target=_to_provenance(target_resps[0]),
        scorer=sp,
        dataset=DatasetProvenance(hash=dataset.hash, n_cases=len(dataset.cases)),
        run_config=RunConfig(n_repeats=n_repeats),
    )
    agg = Aggregate(
        n_cases=len(results),
        mean_score=sum(r.mean for r in results) / len(results),
        n_stable=sum(r.stability == "STABLE" for r in results),
        n_borderline=sum(r.stability == "BORDERLINE" for r in results),
        n_unstable=sum(r.stability == "UNSTABLE" for r in results),
        warnings=_dedupe(warnings),
    )
    return RunRecord(manifest=manifest, results=results, aggregate=agg, prev_hash=prev_hash)
