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
from .paired import INCONCLUSIVE, PairedComparison, compare_runs, per_case_halfwidth


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
    # The paired item-by-item comparison. This is the evidence; the two means above are
    # just the headline numbers it explains.
    paired: PairedComparison | None = None

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
    def evaluator_changes(self) -> list[FieldChange]:
        """The subset of config changes that actually affect comparability."""
        return [c for c in self.config_changes if c.name in EVALUATOR_FIELDS]

    @property
    def score_delta(self) -> float:
        return round(self.score_after - self.score_before, 6)

    @property
    def flip_rate_delta(self) -> float:
        return round(self.flip_rate_after - self.flip_rate_before, 6)

    @property
    def per_case_halfwidth(self) -> float:
        """The pre-2.0 "noise floor", under a name that says what it measures.

        It is the widest per-case CI half-width across the two runs: a statement about
        how little N repeats pin down a single item. It was used as a threshold on the
        suite mean until 2.0, which is a different quantity entirely.
        """
        return self.paired.per_case_halfwidth if self.paired else 0.0

    @property
    def score_verdict(self) -> str:
        """What the paired test supports: regression, improvement, or inconclusive.

        Never "no difference". Failing to reach significance says the experiment could
        not tell, which at N=5 is the usual outcome.
        """
        if not self.comparable:
            return "not comparable"
        return self.paired.verdict if self.paired else INCONCLUSIVE

    def to_dict(self) -> dict[str, object]:
        return {
            "comparable": self.comparable,
            "config_changes": [c.to_dict() for c in self.config_changes],
            "provenance": [c.to_dict() for c in self.provenance],
            "score": {
                "before": self.score_before, "after": self.score_after,
                "delta": self.score_delta,
                "verdict": self.score_verdict,
                "per_case_halfwidth": self.per_case_halfwidth,
            },
            "paired": self.paired.to_dict() if self.paired else None,
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


# `per_case_halfwidth` is defined in paired.py, beside the test that took over its old
# role as a threshold. Re-exported here because this is where callers looked for it.
__all__ = ["DiffResult", "FieldChange", "diff_records", "load_receipt",
           "mean_flip_rate", "per_case_halfwidth"]


# Which of the fields below decide comparability. Kept beside `_config_fields` so the
# two cannot drift apart: `evaluator_fingerprint` is the authority, this names the same
# ground in a form a reader can be shown.
EVALUATOR_FIELDS = frozenset({"scorer type", "judge model", "judge prompt", "rubric",
                              "dataset"})


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

    paired = compare_runs(before, after)
    ids_b = {c.case_id for c in before.results}
    ids_a = {c.case_id for c in after.results}
    # Stability movement is only meaningful for items present in both runs. A case that
    # was dropped from the dataset would otherwise show up as "now stable", which reads
    # as an improvement when what happened is that the item is gone.
    shared = ids_b & ids_a
    unstable_b = [c for c in _unstable(before) if c in shared]
    unstable_a = [c for c in _unstable(after) if c in shared]

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
        paired=paired,
        flip_rate_before=mean_flip_rate(before),
        flip_rate_after=mean_flip_rate(after),
        unstable_before=_unstable(before),
        unstable_after=_unstable(after),
        newly_unstable=sorted(set(unstable_a) - set(unstable_b)),
        now_stable=sorted(set(unstable_b) - set(unstable_a)),
        still_unstable=sorted(set(unstable_a) & set(unstable_b)),
        cases_added=sorted(ids_a - ids_b),
        cases_removed=sorted(ids_b - ids_a),
    )
