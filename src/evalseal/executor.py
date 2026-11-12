from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .adapters.dataset import Case, Dataset
from .adapters.recording import FORMAT_VERSION as CASSETTE_FORMAT
from .adapters.recording import unit as recording_unit
from .adapters.scorer import Scorer, scorer_config
from .adapters.target import Target, TargetResponse
from .analyze import analyze_case
from .identity import scorer_implementation
from .inputs import InputSnapshot
from .models import (
    Aggregate,
    ArtifactProvenance,
    CallProvenance,
    CallRole,
    CallStatus,
    CaseResult,
    CIProvenance,
    CodeProvenance,
    DatasetProvenance,
    EffectiveParams,
    EnvironmentProvenance,
    ExecutionOrigin,
    ExecutionProvenance,
    ProvenanceManifest,
    RunConfig,
    RunRecord,
    ScorerProvenance,
    SuiteProvenance,
    TargetProvenance,
)
from .provenance import (
    ci_provenance,
    environment_provenance,
    file_hash,
    git_provenance,
    text_hash,
)

_CANONICAL_HOSTS = {"api.openai.com", "api.anthropic.com",
                    "generativelanguage.googleapis.com"}
_LOCAL_SCHEMES = {"local"}


#: Artifacts the next run is expected to write to. Sealing their digest still says
#: which file this run used, but a later mismatch means the file moved on, not that the
#: bytes behind this receipt were altered. See docs/adr/0002-cassette-writer-strategy.md.
_MUTABLE_ROLES = frozenset({"cassette"})

#: The most requests this build will keep in flight. It is the in-flight bound in both
#: senses that matter: that many sockets, and that many response bodies being read at
#: `--max-response-bytes` each - 512 MiB at the default limit, which is already more
#: than a CI runner should be asked for. There is no provider that rewards more, and a
#: typo in a suite file ("concurrency": 1000) would otherwise start a thousand threads.
#: docs/operations.md records the measured effect of raising it, which for a replay is
#: none.
MAX_CONCURRENCY = 64


def _bind(role: str, path: str, digest: str, note: str | None = None) -> ArtifactProvenance:
    """One artifact, with the kind its role implies."""
    if role in _MUTABLE_ROLES:
        return ArtifactProvenance(
            role=role, kind="mutable", sha256=digest, path=str(path),
            note=note or
                 "later runs record into this file, so a digest mismatch here is "
                 "expected and is not a tamper signal; the immutable copy of what this "
                 "run used is the cassette_snapshot artifact")
    return ArtifactProvenance(role=role, kind="hashed", sha256=digest, path=str(path),
                              note=note)


def _unreadable(role: str, path: str, note: str) -> ArtifactProvenance:
    return ArtifactProvenance(role=role, kind="external", path=str(path), note=note)


def _artifact(role: str, path: str) -> ArtifactProvenance:
    """Bind one file by digest **as it stands now**, or say there was nothing to bind.

    Only for a file the run itself writes, where "now" is the only time that means
    anything - the cassette. Every input goes through `_captured_artifact` instead, so
    that what is sealed is what was read.
    """
    digest = file_hash(path)
    if digest is None:
        return _unreadable(
            role, path,
            "not readable when the record was sealed; nothing to verify against")
    return _bind(role, path, digest)


def _captured_artifact(snapshot: InputSnapshot) -> tuple[ArtifactProvenance, str | None]:
    """Bind an input from the bytes the run read, and report it if the file has moved.

    The digest is the capture's, never a fresh one: an input used at the start and
    hashed at the end left receipts sealing bytes that produced no score, and
    re-hashing such an artifact afterwards reported `ok` against the wrong file.
    """
    drift = snapshot.drift()
    label = f"{snapshot.role.upper()} CHANGED DURING RUN: {drift}."
    if snapshot.sha256 is None:
        # Nothing was read, so nothing is bound - but if the file turned up while the
        # run was in flight, saying so is the difference between "there was no tool
        # manifest" and "there was one, a moment too late to be the one that applied".
        return _unreadable(
            snapshot.role, snapshot.path,
            "not readable when this run read it; nothing to verify against",
        ), None if drift is None else f"{label} Nothing in this receipt is bound to it."
    if drift is None:
        return _bind(snapshot.role, snapshot.path, snapshot.sha256,
                     "digest of the bytes this run read, taken before use"), None
    return (
        _bind(snapshot.role, snapshot.path, snapshot.sha256,
              f"digest of the bytes this run read; the file changed while the run was "
              f"in flight, and {drift}"),
        f"{label} The scores come from the bytes this run read at the start, which is "
        f"what the receipt seals - so the file as it stands now cannot reproduce them.",
    )


def _cassette_snapshots(
    target: Target, scorer: Scorer, directory: str | Path,
) -> list[ArtifactProvenance]:
    """Finalize every cassette behind this run and bind the snapshots by digest.

    A receipt used to bind only the live cassette, which later runs rewrite: an honest
    run that added repeats turned an earlier receipt's artifact check red, and a `fresh`
    re-draw destroyed the responses the earlier receipt's scores came from. The snapshot
    is those responses, in a file nothing appends to.
    """
    bound: list[ArtifactProvenance] = []
    seen: set[str] = set()
    for holder in (target, getattr(scorer, "judge", None)):
        cassette = getattr(holder, "cassette", None)
        finalize = getattr(cassette, "finalize", None)
        if finalize is None:
            continue
        # One cassette shared by the target and the judge is finalized once: the
        # snapshot is of the file, not of the role that happened to reach it first.
        where = str(getattr(cassette, "path", id(cassette)))
        if where in seen:
            continue
        seen.add(where)
        snap = finalize(directory)
        role = "cassette_snapshot" if not bound else f"cassette_snapshot_{len(bound) + 1}"
        bound.append(ArtifactProvenance(
            role=role, kind="hashed", sha256=snap.sha256, path=str(snap.path),
            note=f"{snap.n_entries} cassette format-{snap.format} "
                 f"{'entry' if snap.n_entries == 1 else 'entries'} used by this run, "
                 f"copied from {where}; replayable on its own"))
    return bound


def _to_provenance(tr: TargetResponse) -> TargetProvenance:
    return TargetProvenance(
        provider=tr.provider,
        requested_model=tr.requested_model,
        served_model=tr.served_model,
        system_fingerprint=tr.system_fingerprint,
        base_url=tr.base_url,
        effective_params=EffectiveParams(**tr.effective_params),
        params_source=tr.params_source,
        api_version=tr.api_version,
        system_prompt_hash=tr.system_prompt_hash,
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


def _cassette_warnings(target: Target, scorer: Scorer) -> list[str]:
    """Whether any cassette behind this run was keyed by an older, weaker rule.

    A format-2 cassette cannot tell two cases apart that ask the same question, so its
    replay serves one draw to both. That caveat changes what the numbers mean, which
    makes it a line in the receipt rather than a note in a changelog.
    """
    seen: list[str] = []
    for role, holder in (("TARGET", target), ("JUDGE", getattr(scorer, "judge", None))):
        cassette = getattr(holder, "cassette", None)
        version = getattr(cassette, "format", None)
        if version is not None and version != CASSETTE_FORMAT:
            seen.append(
                f"{role} CASSETTE FORMAT {version}: its entries are keyed without case "
                "identity, so two cases asking the same question replay one draw and "
                f"their scores agree by construction. Re-record it to get format "
                f"{CASSETTE_FORMAT}.")
    return seen


def _unscorable_warnings(
    results: list[CaseResult], n_unscorable: int, n_observations: int,
) -> list[str]:
    """Say how many observations the grader could not read, and out of how many.

    A count with no denominator cannot be acted on, and a run whose grader failed on
    the hard cases used to look like a run that graded them wrong. Named loudly when
    every repeat of some case failed, because that case contributes a 0.0 to the suite
    mean without a single verdict behind it.
    """
    if not n_unscorable:
        return []
    share = n_unscorable / n_observations if n_observations else 0.0
    out = [
        f"UNSCORABLE OBSERVATIONS: {n_unscorable} of {n_observations} repeat(s) "
        f"({share:.1%}) could not be graded. They score 0.0 and are excluded from flip "
        f"rates and stability, so they are a measurement failure rather than a model "
        f"result. First reason: "
        + next((r.unscorable_reason for r in results if r.unscorable_reason), "unknown")
    ]
    blind = sorted(r.case_id for r in results
                   if r.scores and r.unscorable_count == len(r.scores))
    if blind:
        out.append(
            f"CASES WITH NO VERDICT AT ALL: {', '.join(blind[:10])}"
            + (f" and {len(blind) - 10} more" if len(blind) > 10 else "")
            + ". Each contributes 0.0 to the mean score with nothing graded behind it; "
            "a floor on the mean cannot distinguish that from a case that failed."
        )
    return out


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _call_status(response: TargetResponse) -> CallStatus:
    """How this call ended. Truncation is checked before emptiness: a reply cut off at
    the first token is truncated, which explains the emptiness."""
    if response.truncated:
        return "truncated"
    return "empty" if not response.text else "complete"


def _call_provenance(
    unit: _Unit, response: TargetResponse, role: CallRole,
) -> CallProvenance:
    """One call, bound to the measurement it was made for.

    The binding is the point: `manifest.target` recorded call number one and let it
    stand for every call, so a served-model change mid-run could be warned about but
    not located, and nobody could re-run just the affected cases.
    """
    return CallProvenance(
        case_id=unit.case.case_id,
        repeat=unit.repeat,
        role=role,
        origin=response.origin,
        request_digest=response.request_digest,
        response_digest=response.response_digest,
        provider_request_id=response.provider_request_id,
        requested_model=response.requested_model,
        served_model=response.served_model,
        reported_fingerprint=response.system_fingerprint,
        status=_call_status(response),
        attempts=response.attempts,
        source_key=response.source_key,
    )


def _unit_calls(units: list[_Unit]) -> list[CallProvenance]:
    """Every provider call these units made, bound to the unit that made it."""
    calls: list[CallProvenance] = []
    for unit in units:
        roles: tuple[tuple[CallRole, TargetResponse | None], ...] = (
            ("target", unit.target_resp), ("judge", unit.judge_resp))
        for role, response in roles:
            if response is not None:
                calls.append(_call_provenance(unit, response, role))
    return calls


def _execution_provenance(
    calls: list[CallProvenance], started_at: str, ended_at: str, session_id: str,
) -> ExecutionProvenance:
    """The summary, derived from the calls so it cannot disagree with them."""
    origins: set[ExecutionOrigin] = {c.origin for c in calls}
    # One value when they agree. A run that replayed half its calls is neither live nor
    # replayed, and calling it either would be the more useful-sounding lie.
    origin: ExecutionOrigin = origins.pop() if len(origins) == 1 else "mixed"
    return ExecutionProvenance(
        session_id=session_id,
        origin=origin,
        started_at=started_at,
        ended_at=ended_at,
        n_calls=len(calls),
        n_live=sum(1 for c in calls if c.origin == "live"),
        n_replayed=sum(1 for c in calls if c.origin == "replay"),
        attempts=sum(c.attempts for c in calls),
        n_truncated=sum(1 for c in calls if c.status == "truncated"),
        calls=calls,
    )


class _Unit:
    """One (case, repeat) measurement: generate once, score once."""

    __slots__ = ("case", "repeat", "score", "binary", "target_resp", "judge_resp",
                 "seconds", "unscorable", "reason")

    def __init__(self, case: Case, repeat: int):
        self.case = case
        self.repeat = repeat
        self.score: float = 0.0
        self.binary: bool = True
        self.target_resp: TargetResponse | None = None
        self.judge_resp: TargetResponse | None = None
        self.seconds: float = 0.0
        #: The grader reached no verdict on this repeat. Not a FAIL: see
        #: docs/metrics.md §8.
        self.unscorable: bool = False
        self.reason: str | None = None

    def run(self, target: Target, scorer: Scorer) -> _Unit:
        # Each call is pinned to this measurement - case, repeat and role - so replays
        # are identical regardless of how many workers run or what order they finish
        # in, and so two cases asking the same question do not share one draw. The
        # role is named per call because a target call and a judge call for one unit
        # are two different measurements.
        started = time.perf_counter()
        with recording_unit(self.case.case_id, self.repeat, "target"):
            tr = target.generate(self.case.prompt)
        with recording_unit(self.case.case_id, self.repeat, "judge"):
            sr = scorer.score(self.case.prompt, tr.text, self.case.expected)
        self.seconds = time.perf_counter() - started
        self.target_resp = tr
        self.judge_resp = sr.judge_response
        self.score = sr.score
        self.binary = sr.binary
        self.unscorable = sr.unscorable
        self.reason = sr.reason
        if tr.truncated:
            # A reply cut off at the token limit has not given a wrong answer; it has
            # not given one. Scored as a FAIL it turns a max_tokens mistake into a
            # finding about the model. The score is forced to 0.0 so a truncated reply
            # that matched by luck cannot lift a mean either.
            self.score = 0.0
            self.unscorable = True
            self.reason = ("the target reply hit the token limit, so its answer is "
                           "absent rather than wrong")
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
    session_id: str | None = None,
    snapshot_dir: str | Path | None = None,
    captured: dict[str, InputSnapshot] | None = None,
) -> RunRecord:
    """Run `dataset` N times through `target`, score with `scorer`, seal the result.

    `snapshot_dir`, when given, is where each cassette behind the run is finalized: the
    responses this run used are copied there and bound into the receipt as
    `cassette_snapshot`, so a later run rewriting the live cassette cannot invalidate
    the bytes behind this receipt. Without it the receipt binds only the live cassette,
    and says so by having no snapshot artifact rather than by binding something weaker.

    `session_id` names this run; the caller supplies one when it needs the receipt and
    its output directory to carry the same name.

    `captured` maps an artifact role to the `InputSnapshot` the caller read it from.
    Those roles are sealed from the bytes that were read rather than re-hashed at the
    end, and a file that changed while the run was in flight becomes a warning in the
    receipt instead of a digest nothing ran on.
    """
    if not dataset.cases:
        raise ValueError("dataset has no cases")
    if n_repeats < 1:
        raise ValueError("n_repeats must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    if concurrency > MAX_CONCURRENCY:
        raise ValueError(
            f"concurrency {concurrency} is above the limit of {MAX_CONCURRENCY}. That "
            f"many requests in flight is {MAX_CONCURRENCY} response bodies being read "
            "at once, and no provider rewards it. If you genuinely need more, split "
            "the suite across runs with their own cassettes.")

    session = session_id or uuid.uuid4().hex
    units = [_Unit(case, r) for case in dataset.cases for r in range(n_repeats)]

    def execute(unit: _Unit) -> _Unit:
        done = unit.run(target, scorer)
        if on_unit_done is not None:
            on_unit_done()
        return done

    # Around the calls, not around the function: parsing arguments and writing reports
    # are not measurement time, and counting them would overstate both ends.
    started_at = datetime.now(UTC).isoformat()
    if concurrency == 1:
        for unit in units:
            execute(unit)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # list() re-raises the first failure; order of `units` is untouched.
            list(pool.map(execute, units))
    ended_at = datetime.now(UTC).isoformat()

    results: list[CaseResult] = []
    for i, case in enumerate(dataset.cases):
        case_units = units[i * n_repeats:(i + 1) * n_repeats]
        scores = [u.score for u in case_units]
        binary = all(u.binary for u in case_units)
        unscorable = [u.unscorable for u in case_units]
        stats = analyze_case(scores, binary=binary, unscorable=unscorable)
        seconds = sum(u.seconds for u in case_units)
        # Only the repeats that got a verdict. An unscorable repeat is not a FAIL, so
        # it is neither a pass nor a fail and cannot flip against a majority.
        verdicts = ([int(u.score) for u in case_units if not u.unscorable]
                    if binary else [])
        passes = sum(1 for v in verdicts if v == 1)
        fails = sum(1 for v in verdicts if v == 0)
        others = len(scores) - len(verdicts)
        reason = next((u.reason for u in case_units if u.unscorable and u.reason), None)
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
            unscorable_count=sum(unscorable),
            unscorable_reason=reason,
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
    warnings += _cassette_warnings(target, scorer)

    # Hashed here, not by the caller: the cassette is still being written while the
    # units run, so its digest is only meaningful once they have finished. For the same
    # reason the snapshot is taken here - after the last response is recorded, before
    # the record that binds it is sealed.
    artifacts: list[ArtifactProvenance] = []
    for role, path in sorted((artifact_paths or {}).items()):
        # A captured input is sealed from what it was read as; anything else - the
        # cassette - is hashed here, because it was still being written a moment ago.
        snapshot = (captured or {}).get(role)
        if snapshot is None:
            artifacts.append(_artifact(role, path))
            continue
        bound, moved = _captured_artifact(snapshot)
        artifacts.append(bound)
        if moved is not None:
            warnings.append(moved)
    if snapshot_dir is not None:
        artifacts += _cassette_snapshots(target, scorer, snapshot_dir)
    git = git_provenance()
    # The template, not an instantiated prompt. Until schema 1.3 this sealed the last
    # judge prompt sent, which embeds the last case's prompt and the target's response:
    # the hash changed whenever the target answered differently, and under concurrency
    # with whichever case happened to finish last. That made the evaluator fingerprint of
    # every judge suite unstable, and two replays of one cassette "not comparable".
    template = getattr(scorer, "judge_prompt_template", None)
    sp = ScorerProvenance(
        type=scorer.kind,
        # The grading code itself, not only its settings. `config_hash` covers what the
        # user wrote; this covers what the algorithm does with it. A fork that changed
        # answer extraction moved every score and left the fingerprint untouched.
        impl_hash=scorer_implementation(scorer),
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
        execution=_execution_provenance(_unit_calls(units), started_at, ended_at, session),
        code=CodeProvenance(commit=git["commit"], dirty=git["dirty"]),
        environment=EnvironmentProvenance(
            **environment_provenance(),
            ci=CIProvenance.model_validate(ci) if (ci := ci_provenance()) else None,
        ),
    )
    n_unscorable = sum(r.unscorable_count for r in results)
    n_observations = sum(len(r.scores) for r in results)
    warnings += _unscorable_warnings(results, n_unscorable, n_observations)
    agg = Aggregate(
        n_cases=len(results),
        mean_score=sum(r.mean for r in results) / len(results),
        n_stable=sum(r.stability == "STABLE" for r in results),
        n_borderline=sum(r.stability == "BORDERLINE" for r in results),
        n_unstable=sum(r.stability == "UNSTABLE" for r in results),
        warnings=_dedupe(warnings),
        n_unscorable=n_unscorable,
        n_observations=n_observations,
    )
    return RunRecord(manifest=manifest, results=results, aggregate=agg, prev_hash=prev_hash)
