"""Did the judge behave the same way when the suite was run again?

`diff` answers "did the score move". This answers a different question that a score
cannot: when the same suite is re-run later, do the same cases come back with the same
verdicts, and if not, what changed - the instrument or the thing being measured?

## Why the distinction has to be made explicitly

A verdict that moved between two runs has three possible causes, and they call for
three different responses:

- **Evaluator drift.** Someone edited the rubric, swapped the judge model, moved the
  endpoint, changed a sampling parameter. The measurement changed, so the numbers are
  not two readings of the same thing. Nothing here says the model got better or worse,
  and this module refuses to phrase it that way.
- **Judge variance.** The instrument is identical on paper and still disagrees with
  itself, because an LLM judge is a sampler. This is the case the project exists to
  make visible.
- **Target variance.** The grader is deterministic, so a changed verdict can only mean
  the target answered differently.

The label is decided from the receipts, never guessed. Where two causes cannot be told
apart from two receipts alone, the report says so and points at `evalseal decompose`,
which holds the target's response fixed and re-judges it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .diffing import EVALUATOR_FIELDS, FieldChange, _config_fields
from .ledger import evaluator_fingerprint, records_share_fingerprint_inputs
from .models import CaseResult, RunRecord
from .report import verdict_sequence

EVALUATOR_DRIFT = "evaluator_drift"
JUDGE_VARIANCE = "judge_variance"
TARGET_VARIANCE = "target_variance"
TARGET_CHANGE = "target_change"
STABLE = "stable"


@dataclass
class CaseDrift:
    """One case, as it came out in each run."""
    case_id: str
    before_sequence: str
    after_sequence: str
    before_counts: dict[str, int]
    after_counts: dict[str, int]
    before_flip_rate: float
    after_flip_rate: float
    before_stability: str
    after_stability: str

    @property
    def verdict_changed(self) -> bool:
        """Whether the case's majority verdict moved, not merely its sequence."""
        return _majority(self.before_counts) != _majority(self.after_counts)

    @property
    def stability_changed(self) -> bool:
        return self.before_stability != self.after_stability

    @property
    def flip_rate_delta(self) -> float:
        return round(self.after_flip_rate - self.before_flip_rate, 4)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "before": {"sequence": self.before_sequence, "counts": self.before_counts,
                       "flip_rate": self.before_flip_rate, "stability": self.before_stability},
            "after": {"sequence": self.after_sequence, "counts": self.after_counts,
                      "flip_rate": self.after_flip_rate, "stability": self.after_stability},
            "verdict_changed": self.verdict_changed,
            "stability_changed": self.stability_changed,
            "flip_rate_delta": self.flip_rate_delta,
        }


@dataclass
class DriftReport:
    kind: str
    comparable: bool
    before_fingerprint: str
    after_fingerprint: str
    evaluator_changes: list[FieldChange] = field(default_factory=list)
    cases: list[CaseDrift] = field(default_factory=list)
    only_in_before: list[str] = field(default_factory=list)
    only_in_after: list[str] = field(default_factory=list)
    schema_note: str = ""

    @property
    def verdict_changes(self) -> list[CaseDrift]:
        return [c for c in self.cases if c.verdict_changed]

    @property
    def stability_changes(self) -> list[CaseDrift]:
        return [c for c in self.cases if c.stability_changed]

    @property
    def before_flip_rate(self) -> float:
        return _mean(c.before_flip_rate for c in self.cases)

    @property
    def after_flip_rate(self) -> float:
        return _mean(c.after_flip_rate for c in self.cases)

    def top_unstable(self, n: int = 5) -> list[CaseDrift]:
        """The cases worth reading first: most flipping in the later run."""
        return sorted(self.cases, key=lambda c: (-c.after_flip_rate, c.case_id))[:n]

    def summary(self) -> str:
        """One paragraph a reviewer can act on, in plain English."""
        moved = len(self.verdict_changes)
        if self.kind == EVALUATOR_DRIFT:
            changed = ", ".join(c.name for c in self.evaluator_changes) or "unnamed fields"
            return (
                f"Evaluator drift: the grading setup changed ({changed}), so these two runs "
                f"did not measure the same thing. {moved} of {len(self.cases)} shared case(s) "
                "came back with a different verdict, and none of that can be attributed to "
                "the model under test. Re-run the baseline with the current evaluator "
                "before comparing scores."
            )
        if self.kind == TARGET_CHANGE:
            return (
                f"The grading is identical and the target model changed, so this is a "
                f"comparison, not drift: {moved} of {len(self.cases)} shared case(s) "
                "returned a different verdict. Use `evalseal diff` for the paired test."
            )
        if self.kind == STABLE:
            return (
                f"No drift: the grading setup is identical and all {len(self.cases)} shared "
                "case(s) returned the same majority verdict. Flip rates may still differ "
                "within cases; the per-case strips show that."
            )
        if self.kind == JUDGE_VARIANCE:
            return (
                f"Judge variance: the grading setup is byte-identical and the target model "
                f"is unchanged, yet {moved} of {len(self.cases)} shared case(s) came back "
                "with a different verdict. An LLM judge is a sampler, so re-running it is "
                "not free of consequence. This cannot be separated from the target "
                "answering differently using two receipts alone - `evalseal decompose` "
                "holds one response fixed and re-judges it, which can."
            )
        return (
            f"Target variance: the grading is deterministic and unchanged, so the "
            f"{moved} changed verdict(s) of {len(self.cases)} can only come from the target "
            "answering differently."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "comparable": self.comparable,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "evaluator_changes": [c.to_dict() for c in self.evaluator_changes],
            "schema_note": self.schema_note,
            "n_shared_cases": len(self.cases),
            "n_verdict_changes": len(self.verdict_changes),
            "n_stability_changes": len(self.stability_changes),
            "flip_rate": {"before": self.before_flip_rate, "after": self.after_flip_rate},
            "only_in_before": self.only_in_before,
            "only_in_after": self.only_in_after,
            "cases": [c.to_dict() for c in self.cases],
            "summary": self.summary(),
        }


def _mean(values) -> float:
    items = list(values)
    return round(sum(items) / len(items), 4) if items else 0.0


def _majority(counts: dict[str, int]) -> int | None:
    if counts["pass"] > counts["fail"]:
        return 1
    if counts["fail"] > counts["pass"]:
        return 0
    return None


def _counts(case: CaseResult) -> dict[str, int]:
    return {"pass": case.pass_count, "fail": case.fail_count, "other": case.other_count}


def analyze_drift(before: RunRecord, after: RunRecord) -> DriftReport:
    """Compare two runs of the same suite and name what moved."""
    by_before = {c.case_id: c for c in before.results}
    by_after = {c.case_id: c for c in after.results}
    shared = sorted(set(by_before) & set(by_after))

    cases = [
        CaseDrift(
            case_id=cid,
            before_sequence=verdict_sequence(by_before[cid]),
            after_sequence=verdict_sequence(by_after[cid]),
            before_counts=_counts(by_before[cid]),
            after_counts=_counts(by_after[cid]),
            before_flip_rate=by_before[cid].flip_rate,
            after_flip_rate=by_after[cid].flip_rate,
            before_stability=by_before[cid].stability,
            after_stability=by_after[cid].stability,
        )
        for cid in shared
    ]

    fp_before, fp_after = evaluator_fingerprint(before), evaluator_fingerprint(after)
    comparable = fp_before == fp_after
    changes = [
        FieldChange(name, b, a)
        for (name, b), a in zip(_config_fields(before).items(),
                                _config_fields(after).values(), strict=True)
        if b != a and name in EVALUATOR_FIELDS
    ]
    schema_note = "" if records_share_fingerprint_inputs(before, after) else (
        "One of these runs was sealed before schema 1.4, which records fields the other "
        "has. A fingerprint difference may be an artefact of that rather than a change."
    )

    kind = _classify(before, after, comparable, cases)
    return DriftReport(
        kind=kind, comparable=comparable,
        before_fingerprint=fp_before, after_fingerprint=fp_after,
        evaluator_changes=changes, cases=cases,
        only_in_before=sorted(set(by_before) - set(by_after)),
        only_in_after=sorted(set(by_after) - set(by_before)),
        schema_note=schema_note,
    )


def _classify(
    before: RunRecord, after: RunRecord, comparable: bool, cases: list[CaseDrift]
) -> str:
    """Name the cause from the receipts, never from a guess about what someone did."""
    if not comparable:
        return EVALUATOR_DRIFT
    if before.manifest.target.requested_model != after.manifest.target.requested_model:
        return TARGET_CHANGE
    if not any(c.verdict_changed for c in cases):
        return STABLE
    # The grader is identical. Only an LLM judge can disagree with itself; a regex
    # cannot, so with a deterministic scorer the target is the only thing left.
    return JUDGE_VARIANCE if after.manifest.scorer.type == "llm_judge" else TARGET_VARIANCE


def render(report: DriftReport) -> str:
    """Markdown, leading with what changed rather than with a number."""
    lines = [f"# Judge drift: {report.kind.replace('_', ' ')}", ""]
    lines.append(report.summary())
    lines.append("")
    if report.schema_note:
        lines += [f"> {report.schema_note}", ""]

    lines += ["| | before | after |", "|---|---|---|",
              f"| evaluator fingerprint | `{report.before_fingerprint[:30]}…` "
              f"| `{report.after_fingerprint[:30]}…` |",
              f"| comparable | {'yes' if report.comparable else '**no**'} | |",
              f"| mean flip rate | {report.before_flip_rate:.1%} | {report.after_flip_rate:.1%} |",
              f"| cases with a changed verdict | {len(report.verdict_changes)} "
              f"of {len(report.cases)} | |",
              f"| cases with a changed stability class | {len(report.stability_changes)} | |",
              ""]

    if not report.comparable and report.evaluator_changes:
        lines += ["## What changed in the evaluator", "",
                  "| field | before | after |", "|---|---|---|"]
        for change in report.evaluator_changes:
            lines.append(f"| {change.name} | `{change.before}` | `{change.after}` |")
        lines.append("")

    moved = report.verdict_changes
    if moved:
        lines += ["## Cases whose verdict moved", "",
                  "| case | before | after | flip rate |", "|---|---|---|---|"]
        for c in moved:
            lines.append(
                f"| {c.case_id} | `{c.before_sequence}` | `{c.after_sequence}` "
                f"| {c.before_flip_rate:.0%} to {c.after_flip_rate:.0%} |")
        lines.append("")

    top = [c for c in report.top_unstable() if c.after_flip_rate > 0]
    if top:
        lines += ["## Least stable cases in the later run", "",
                  "| case | sequence | flip rate | stability |", "|---|---|---|---|"]
        for c in top:
            lines.append(
                f"| {c.case_id} | `{c.after_sequence}` | {c.after_flip_rate:.0%} "
                f"| {c.after_stability} |")
        lines.append("")

    if report.only_in_before or report.only_in_after:
        lines.append(
            f"Not compared: {len(report.only_in_before)} case(s) only in the earlier run, "
            f"{len(report.only_in_after)} only in the later one.")
    return "\n".join(lines)
