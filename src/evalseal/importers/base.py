"""Turning another tool's results into a sealed receipt, without inventing anything.

This is the shared half of an import: a normalized observation, the list of things the
export could not say, and the one function that assembles a `RunRecord` from them.
Everything that knows what promptfoo's JSON looks like lives in `promptfoo.py`.

**It is deliberately not a framework.** There is one adapter. A registry, a plugin
protocol and a field-mapping DSL would all be written against a single example, which is
the way to get an abstraction that fits nothing else either. The second adapter is the
one that should decide what is shared.

## The rules an importer follows

1. **Never manufacture.** A field the export does not carry is `None` and is named in
   `gaps`. Not a default, not a guess from a neighbouring field, not an empty string.
2. **`origin` is `imported`.** No call in an imported receipt claims a provider was
   contacted, because none was by EvalSeal.
3. **No evaluator fingerprint.** `scorer.impl_hash` stays `None`: the grading code
   belongs to the other tool. Two imports are therefore not comparable, and *two imports
   missing the same field are not thereby comparable* - `diffing` refuses explicitly
   rather than letting two identical absences hash to one value.
4. **Errors are kept.** A row the other tool could not grade becomes an unscorable
   repeat, scoring 0.0 and counted. Dropping it would raise the mean by removing the
   cases that failed hardest.
5. **Time is not borrowed.** The export's timestamps are recorded under
   `claimed_times`, as claims. `execution.started_at` is when the import ran.
6. **The bytes are bound.** The receipt carries the digest of exactly the bytes that
   were read, so `verify --artifacts` can check that the retained export still matches.
   That is all it can check: see docs/importers/promptfoo.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..analyze import analyze_case
from ..models import (
    Aggregate,
    ArtifactProvenance,
    CallProvenance,
    CaseResult,
    DatasetProvenance,
    EffectiveParams,
    ExecutionProvenance,
    ImportProvenance,
    ProvenanceManifest,
    RunConfig,
    RunRecord,
    ScorerProvenance,
    SuiteProvenance,
    TargetProvenance,
)

#: The artifact role an imported export is bound under. One name, so `verify
#: --artifacts` and the docs agree.
EXPORT_ROLE = "import_source"


class ImportRefused(ValueError):
    """The export cannot be imported, and the message says which field is to blame."""


@dataclass(frozen=True)
class Observation:
    """One repeat of one case, as the exporting tool reported it.

    `score` is 0.0 or 1.0 - every tool integrated so far reports a pass/fail per row.
    A tool with continuous scores would widen this, and that is the second adapter's
    problem to raise, not this one's to pre-solve.
    """
    case_id: str
    repeat: int
    score: float
    #: The grader reached no verdict: an error, a crash, a timeout. Not a failure.
    unscorable: bool = False
    reason: str | None = None
    #: Digest of the response text, never the text. An export can carry a dataset's
    #: worth of responses and a receipt is meant to be forwarded.
    response_digest: str | None = None
    #: Whatever the export called the model or provider for this row. Not a revision.
    provider_id: str = "unknown"
    #: The row's own identifier in the export, for tracing one case back.
    source_row_id: str | None = None


@dataclass
class Gaps:
    """EvalSeal fields the export could not fill, with why, in the order found."""
    entries: list[str] = field(default_factory=list)

    def add(self, field_name: str, why: str) -> None:
        line = f"{field_name}: {why}"
        if line not in self.entries:
            self.entries.append(line)

    def names(self) -> list[str]:
        return [entry.split(":", 1)[0] for entry in self.entries]

    def __bool__(self) -> bool:
        return bool(self.entries)


def _case_results(observations: list[Observation]) -> list[CaseResult]:
    """Group observations into cases, keeping repeat order and ungraded rows."""
    by_case: dict[str, list[Observation]] = {}
    for item in observations:
        by_case.setdefault(item.case_id, []).append(item)

    results: list[CaseResult] = []
    for case_id, items in by_case.items():
        ordered = sorted(items, key=lambda o: o.repeat)
        scores = [o.score for o in ordered]
        unscorable = [o.unscorable for o in ordered]
        stats = analyze_case(scores, binary=True, unscorable=unscorable)
        verdicts = [int(o.score) for o in ordered if not o.unscorable]
        results.append(CaseResult(
            case_id=case_id,
            scores=scores,
            mean=stats.mean,
            ci95=(stats.ci95_low, stats.ci95_high),
            flip_rate=stats.flip_rate,
            stability=stats.stability,
            stability_label=stats.stability_label,
            majority_verdict=stats.majority_verdict,
            verdicts=verdicts,
            pass_count=sum(1 for v in verdicts if v == 1),
            fail_count=sum(1 for v in verdicts if v == 0),
            other_count=len(scores) - len(verdicts),
            flip_count=stats.flip_count,
            unscorable_count=sum(unscorable),
            unscorable_reason=next(
                (o.reason for o in ordered if o.unscorable and o.reason), None),
        ))
    return results


def build_record(
    *,
    observations: list[Observation],
    imported: ImportProvenance,
    gaps: Gaps,
    dataset_hash: str,
    suite: SuiteProvenance | None,
    scorer: ScorerProvenance,
    target: TargetProvenance,
    n_repeats: int,
    warnings: list[str],
    prev_hash: str = "GENESIS",
    started_at: str | None = None,
) -> RunRecord:
    """Assemble a receipt from normalized observations. No tool-specific knowledge.

    The caller has already decided what every field means; this turns that into a
    record and makes sure the parts an importer must not fill stay empty.
    """
    if not observations:
        raise ImportRefused(
            "the export contains no observations. An empty receipt would verify "
            "perfectly and say nothing, which is worse than refusing to make one.")

    results = _case_results(observations)
    now = datetime.now(UTC).isoformat()
    calls = [
        CallProvenance(
            case_id=item.case_id,
            repeat=item.repeat,
            role="target",
            # The field that keeps an imported receipt from ever reading as a live one.
            origin="imported",
            response_digest=item.response_digest,
            requested_model=item.provider_id,
            # Left null on purpose: the export does not say what the provider served,
            # and filling this from `requested_model` would forge the one field that
            # exists to catch an alias resolving somewhere else.
            served_model=None,
            attempts=0,
            # Deliberately not the export's row id. `source_key` means "the cassette
            # entry that served this call", and the independence audit reads it as
            # evidence that two calls were separate draws. A promptfoo row id is
            # per-row whether or not the row came from promptfoo's cache, so using it
            # here would turn "we do not know" into "fifteen separate draws".
            source_key=None,
        )
        for item in sorted(observations, key=lambda o: (o.case_id, o.repeat))
    ]
    execution = ExecutionProvenance(
        session_id=imported.source_sha256.split(":")[-1][:32],
        origin="imported",
        # When the *import* ran. The export's own timestamps are claims and live in
        # `imported.claimed_times`.
        started_at=started_at or now,
        ended_at=now,
        n_calls=len(calls),
        n_live=0,
        n_replayed=0,
        attempts=0,
        n_truncated=0,
        calls=calls,
    )

    n_unscorable = sum(r.unscorable_count for r in results)
    n_observations = sum(len(r.scores) for r in results)
    notes = [
        f"IMPORTED FROM {imported.tool.upper()}"
        + (f" {imported.tool_version}" if imported.tool_version else "")
        + ": these numbers were read out of an export, not measured here. Nothing in "
        "this receipt establishes that the run happened as the export describes, or "
        "that no other run was made and discarded.",
        *warnings,
    ]
    if gaps:
        notes.append(
            f"IMPORT GAPS ({len(gaps.entries)}): fields this export could not fill - "
            + "; ".join(gaps.entries)
        )

    manifest = ProvenanceManifest(
        target=target,
        scorer=scorer,
        dataset=DatasetProvenance(
            hash=dataset_hash,
            n_cases=len(results),
            path=imported.source_path,
            case_ids=[r.case_id for r in results],
        ),
        run_config=RunConfig(n_repeats=n_repeats, concurrency=1),
        suite=suite,
        artifacts=[ArtifactProvenance(
            role=EXPORT_ROLE,
            kind="hashed",
            sha256=imported.source_sha256,
            path=imported.source_path,
            note=f"the {imported.tool} export these numbers were read from. Verifying "
                 "it shows the retained file still matches what was sealed; it "
                 "establishes nothing about how the file came to exist.",
        )],
        execution=execution,
        imported=imported,
    )
    aggregate = Aggregate(
        n_cases=len(results),
        mean_score=sum(r.mean for r in results) / len(results),
        n_stable=sum(r.stability == "STABLE" for r in results),
        n_borderline=sum(r.stability == "BORDERLINE" for r in results),
        n_unstable=sum(r.stability == "UNSTABLE" for r in results),
        warnings=list(dict.fromkeys(notes)),
        n_unscorable=n_unscorable,
        n_observations=n_observations,
    )
    return RunRecord(manifest=manifest, results=results, aggregate=aggregate,
                     prev_hash=prev_hash)


def empty_params() -> EffectiveParams:
    """Sampling parameters an export does not carry.

    Every one is marked `unsupported` rather than `inherited`: "the provider applied
    its own default" is a claim about a request EvalSeal never made. The honest reading
    is that this wire format, as exported, has no such parameter to report.
    """
    return EffectiveParams(unset=dict.fromkeys(
        ("temperature", "seed", "top_p", "max_tokens"), "unsupported"))


def source_digest(path: Path) -> tuple[str, bytes]:
    """`(sha256:…, bytes)` read once, so the digest is of what was parsed."""
    import hashlib

    raw = path.read_bytes()
    return ("sha256:" + hashlib.sha256(raw).hexdigest(), raw)
