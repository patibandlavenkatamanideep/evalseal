"""Compare two sealed runs and say whether the comparison means anything.

The question this answers is not "did the number move" but "can I compare these two
numbers without fooling myself". A changed rubric or judge prompt moves a score while the
target model sits still, so comparability is reported first and the score second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .ledger import evaluator_fingerprint
from .models import RunRecord


@dataclass
class FieldChange:
    """One provenance field that differs between the two runs."""
    name: str
    before: str | None
    after: str | None

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def to_dict(self) -> dict[str, object]:
        return {"field": self.name, "before": self.before, "after": self.after,
                "changed": self.changed}


@dataclass
class DiffResult:
    comparable: bool
    config_changes: list[FieldChange] = field(default_factory=list)
    provenance: list[FieldChange] = field(default_factory=list)

    score_before: float = 0.0
    score_after: float = 0.0
    noise_floor: float = 0.0

    flip_rate_before: float = 0.0
    flip_rate_after: float = 0.0

    unstable_before: list[str] = field(default_factory=list)
    unstable_after: list[str] = field(default_factory=list)
    newly_unstable: list[str] = field(default_factory=list)
    now_stable: list[str] = field(default_factory=list)
    still_unstable: list[str] = field(default_factory=list)

    cases_added: list[str] = field(default_factory=list)
    cases_removed: list[str] = field(default_factory=list)

    @property
    def score_delta(self) -> float:
        return round(self.score_after - self.score_before, 6)

    @property
    def flip_rate_delta(self) -> float:
        return round(self.flip_rate_after - self.flip_rate_before, 6)

    @property
    def score_verdict(self) -> str:
        """Whether the score move is larger than the noise the runs themselves show."""
        if not self.comparable:
            return "not comparable"
        return "within noise" if abs(self.score_delta) <= self.noise_floor else "REAL CHANGE"

    def to_dict(self) -> dict[str, object]:
        return {
            "comparable": self.comparable,
            "config_changes": [c.to_dict() for c in self.config_changes],
            "provenance": [c.to_dict() for c in self.provenance],
            "score": {
                "before": self.score_before, "after": self.score_after,
                "delta": self.score_delta, "noise_floor": self.noise_floor,
                "verdict": self.score_verdict,
            },
            "flip_rate": {
                "before": self.flip_rate_before, "after": self.flip_rate_after,
                "delta": self.flip_rate_delta,
            },
            "unstable_cases": {
                "before": self.unstable_before, "after": self.unstable_after,
                "newly_unstable": self.newly_unstable, "now_stable": self.now_stable,
                "still_unstable": self.still_unstable,
            },
            "cases": {"added": self.cases_added, "removed": self.cases_removed},
        }


def load_receipt(source: str | Path) -> RunRecord:
    """Read a sealed record from either a receipt file or a ledger.

    `report.json` is one pretty-printed record; a ledger is one compact record per line.
    Try the whole file first, and only then treat it as JSONL and take the head, so a
    multi-line receipt is not mistaken for a ledger whose last line is a lone brace.
    """
    text = Path(source).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{source} is empty")
    try:
        return RunRecord.model_validate_json(text)
    except ValidationError:
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise
        return RunRecord.model_validate_json(lines[-1])


def mean_flip_rate(record: RunRecord) -> float:
    """Mean per-case flip rate. One definition, so every surface prints one number."""
    if not record.results:
        return 0.0
    return round(sum(c.flip_rate for c in record.results) / len(record.results), 4)


def _unstable(record: RunRecord) -> list[str]:
    """Cases whose verdict was not unanimous.

    Keyed on `flip_rate`, not `flip_count`: the count was added in schema 1.2 and
    defaults to 0 on older receipts, which would silently report an old run as having
    nothing unstable in it.
    """
    return sorted(c.case_id for c in record.results if c.flip_rate > 0)


def noise_floor(record: RunRecord) -> float:
    """Widest per-case CI half-width: the noise the run itself exhibits.

    Deliberately the widest and not the mean. A score move smaller than the shakiest
    case in the run is not evidence of anything, and the conservative floor is the one
    that keeps a reader from over-reading a delta.
    """
    return max(((c.ci95[1] - c.ci95[0]) / 2 for c in record.results), default=0.0)


def _config_fields(record: RunRecord) -> dict[str, str | None]:
    m = record.manifest
    return {
        "target model": m.target.requested_model,
        "target temperature": str(m.target.effective_params.temperature),
        "scorer type": m.scorer.type,
        "judge model": m.scorer.judge.requested_model if m.scorer.judge else None,
        "judge prompt": m.scorer.judge_prompt_hash,
        "rubric": m.scorer.rubric_hash,
        "dataset": m.dataset.hash,
        "suite": m.suite.hash if m.suite else None,
    }


def _provenance_fields(record: RunRecord) -> dict[str, str | None]:
    m = record.manifest
    return {
        "served model": m.target.served_model,
        "n_repeats": str(m.run_config.n_repeats),
        "evalseal version": m.environment.evalseal_version,
        "git commit": (m.code.commit or "")[:12] or None,
        "sealed hash": (record.hash or "")[:20] or None,
    }


def diff_records(before: RunRecord, after: RunRecord) -> DiffResult:
    """Compare two sealed runs across score, stability and evaluator configuration."""
    config = [
        FieldChange(name, b, a)
        for (name, b), a in zip(
            _config_fields(before).items(), _config_fields(after).values(), strict=True
        )
    ]
    # Comparability is about the grader, not the subject: a different target model is
    # exactly what a benchmark compares.
    comparable = evaluator_fingerprint(before) == evaluator_fingerprint(after)

    # The floor is the noisier of the two runs: comparing against the quieter one would
    # call a move real on the strength of the run that happened to behave.
    floor = round(max(noise_floor(before), noise_floor(after)), 4)
    unstable_b, unstable_a = _unstable(before), _unstable(after)
    ids_b = {c.case_id for c in before.results}
    ids_a = {c.case_id for c in after.results}

    return DiffResult(
        comparable=comparable,
        config_changes=[c for c in config if c.changed],
        provenance=[
            FieldChange(name, b, a)
            for (name, b), a in zip(
                _provenance_fields(before).items(), _provenance_fields(after).values(),
                strict=True,
            )
        ],
        score_before=before.aggregate.mean_score,
        score_after=after.aggregate.mean_score,
        noise_floor=floor,
        flip_rate_before=mean_flip_rate(before),
        flip_rate_after=mean_flip_rate(after),
        unstable_before=unstable_b,
        unstable_after=unstable_a,
        newly_unstable=sorted(set(unstable_a) - set(unstable_b)),
        now_stable=sorted(set(unstable_b) - set(unstable_a)),
        still_unstable=sorted(set(unstable_a) & set(unstable_b)),
        cases_added=sorted(ids_a - ids_b),
        cases_removed=sorted(ids_b - ids_a),
    )
