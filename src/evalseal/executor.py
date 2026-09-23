from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from .adapters.dataset import Case, Dataset
from .adapters.recording import slot
from .adapters.scorer import Scorer, scorer_config
from .adapters.target import Target, TargetResponse
from .analyze import analyze_case
from .models import (
    Aggregate,
    ArtifactProvenance,
    CaseResult,
    CodeProvenance,
    DatasetProvenance,
    EffectiveParams,
    EnvironmentProvenance,
    ProvenanceManifest,
    RunConfig,
    RunRecord,
    ScorerProvenance,
    SuiteProvenance,
    TargetProvenance,
)
from .provenance import environment_provenance, file_hash, git_provenance, text_hash

_CANONICAL_HOSTS = {"api.openai.com", "api.anthropic.com",
                    "generativelanguage.googleapis.com"}
_LOCAL_SCHEMES = {"local"}


def _artifact(role: str, path: str) -> ArtifactProvenance:
    """Bind one file by digest, or record plainly that there was nothing to bind."""
    digest = file_hash(path)
    if digest is None:
        return ArtifactProvenance(
            role=role, kind="external", path=str(path),
            note="not readable when the record was sealed; nothing to verify against")
    return ArtifactProvenance(role=role, kind="hashed", sha256=digest, path=str(path))


def _to_provenance(tr: TargetResponse) -> TargetProvenance:
    return TargetProvenance(
        provider=tr.provider,
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
            f"{role} NON-CANONICAL ENDPOINT: {tp.base_url} - responses may be proxied/altered."
        )
    if tp.params_source == "provider_default" and tp.effective_params.temperature is None:
        w.append(
            f"{role} TEMPERATURE NOT SET: the provider default (often 1.0) was used silently; "
            "verdicts near the decision boundary may not be reproducible."
        )
    return w


def _truncation_warnings(responses: list[TargetResponse], role: str) -> list[str]:
    """A reply cut off at the token limit is an absent answer, not a wrong one.

    Scored naively it is indistinguishable from the model getting it wrong, which turns
    a configuration mistake into a finding about the model.
    """
    n = sum(1 for r in responses if r.truncated)
    if not n:
        return []
    return [
        f"{role} RESPONSE TRUNCATED: {n} of {len(responses)} call(s) hit the token "
        "limit. Those answers are incomplete, not incorrect; raise max_tokens before "
        "reading anything into the score."
    ]


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


class _Unit:
    """One (case, repeat) measurement: generate once, score once."""

    __slots__ = ("case", "repeat", "score", "binary", "target_resp", "judge_resp", "seconds")

    def __init__(self, case: Case, repeat: int):
        self.case = case
        self.repeat = repeat
        self.score: float = 0.0
        self.binary: bool = True
        self.target_resp: TargetResponse | None = None
        self.judge_resp: TargetResponse | None = None
        self.seconds: float = 0.0

    def run(self, target: Target, scorer: Scorer) -> _Unit:
        # The slot pins this unit's cassette entries to `repeat`, so replays are
        # identical regardless of how many workers run or what order they finish in.
        started = time.perf_counter()
        with slot(self.repeat):
            tr = target.generate(self.case.prompt)
            sr = scorer.score(self.case.prompt, tr.text, self.case.expected)
        self.seconds = time.perf_counter() - started
        self.target_resp = tr
        self.judge_resp = sr.judge_response
        self.score = sr.score
        self.binary = sr.binary
        return self


def run_eval(
    dataset: Dataset,
    target: Target,
    scorer: Scorer,
    n_repeats: int = 5,
    prev_hash: str = "GENESIS",
    concurrency: int = 1,
    on_unit_done: Callable[[], None] | None = None,
    suite: SuiteProvenance | None = None,
    dataset_path: str | None = None,
    store_judge_prompt: bool = False,
    artifact_paths: dict[str, str] | None = None,
) -> RunRecord:
    if not dataset.cases:
        raise ValueError("dataset has no cases")
    if n_repeats < 1:
        raise ValueError("n_repeats must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")

    units = [_Unit(case, r) for case in dataset.cases for r in range(n_repeats)]

    def execute(unit: _Unit) -> _Unit:
        done = unit.run(target, scorer)
        if on_unit_done is not None:
            on_unit_done()
        return done

    if concurrency == 1:
        for unit in units:
            execute(unit)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # list() re-raises the first failure; order of `units` is untouched.
            list(pool.map(execute, units))

    results: list[CaseResult] = []
    for i, case in enumerate(dataset.cases):
        case_units = units[i * n_repeats:(i + 1) * n_repeats]
        scores = [u.score for u in case_units]
        binary = all(u.binary for u in case_units)
        stats = analyze_case(scores, binary=binary)
        seconds = sum(u.seconds for u in case_units)
        verdicts = [int(s) for s in scores] if binary else []
        passes = sum(1 for v in verdicts if v == 1)
        fails = sum(1 for v in verdicts if v == 0)
        others = len(scores) - len(verdicts)
        results.append(CaseResult(
            case_id=case.case_id,
            scores=scores,
            mean=stats.mean,
            ci95=(stats.ci95_low, stats.ci95_high),
            flip_rate=stats.flip_rate,
            stability=stats.stability,
            stability_label=stats.stability_label,
            majority_verdict=stats.majority_verdict,
            seconds=seconds,
            verdicts=verdicts,
            pass_count=passes,
            fail_count=fails,
            other_count=others,
            flip_count=stats.flip_count,
        ))

    target_resps = [u.target_resp for u in units if u.target_resp is not None]
    judge_resps = [u.judge_resp for u in units if u.judge_resp is not None]

    # Every response is checked, not just the last one: a mismatch on any call matters.
    warnings: list[str] = []
    for tr in target_resps:
        warnings += _provenance_warnings(_to_provenance(tr), "TARGET")
    warnings += _drift_warnings(target_resps, "TARGET")
    warnings += _truncation_warnings(target_resps, "TARGET")
    for jr in judge_resps:
        warnings += _provenance_warnings(_to_provenance(jr), "JUDGE")
    warnings += _drift_warnings(judge_resps, "JUDGE")
    warnings += _truncation_warnings(judge_resps, "JUDGE")

    # Hashed here, not by the caller: the cassette is still being written while the
    # units run, so its digest is only meaningful once they have finished.
    artifacts = [_artifact(role, path) for role, path in sorted((artifact_paths or {}).items())]
    git = git_provenance()
    # The template, not an instantiated prompt. Until schema 1.3 this sealed the last
    # judge prompt sent, which embeds the last case's prompt and the target's response:
    # the hash changed whenever the target answered differently, and under concurrency
    # with whichever case happened to finish last. That made the evaluator fingerprint of
    # every judge suite unstable, and two replays of one cassette "not comparable".
    template = getattr(scorer, "judge_prompt_template", None)
    sp = ScorerProvenance(
        type=scorer.kind,
        judge=_to_provenance(judge_resps[0]) if judge_resps else None,
        rubric_hash=getattr(scorer, "rubric_hash", None),
        config_hash=text_hash(
            json.dumps(scorer_config(scorer), sort_keys=True, separators=(",", ":"))),
        judge_prompt_hash=text_hash(template) if template else None,
        # Opt-in all the same: the template carries the rubric verbatim, and a rubric can
        # be as private as the dataset it grades.
        judge_prompt=template if (store_judge_prompt and template) else None,
    )
    manifest = ProvenanceManifest(
        target=_to_provenance(target_resps[0]),
        scorer=sp,
        dataset=DatasetProvenance(
            hash=dataset.hash,
            n_cases=len(dataset.cases),
            path=dataset_path,
            case_ids=[c.case_id for c in dataset.cases],
        ),
        run_config=RunConfig(n_repeats=n_repeats, concurrency=concurrency),
        suite=suite,
        artifacts=artifacts,
        code=CodeProvenance(commit=git["commit"], dirty=git["dirty"]),
        environment=EnvironmentProvenance(**environment_provenance()),
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
