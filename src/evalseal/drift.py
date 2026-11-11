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

- **Something else about the target moved.** The model name is the same and its
  sampling parameters, endpoint, API version or system prompt are not. A verdict that
  moved is then consistent with the judge, with the target, and with the setting that
  changed, and no two receipts can separate them. The answer is `cannot_isolate`, and
  it is a first-class answer rather than a gap in the taxonomy.

The label is decided from the receipts, never guessed. Where two causes cannot be told
apart from two receipts alone, the report says so and points at `evalseal decompose`,
which holds the target's response fixed and re-judges it.

## Observed, then inferred - and the two are not mixed

`CaseDrift` and the counts on `DriftReport` are **observations**: what each run
returned. `kind` is an **inference** about the cause, admissible only on the grounds
tabulated in docs/metrics.md §10. `DriftReport.grounds` states which of those grounds
held, so a reader can check the inference rather than take it.

Two findings this structure exists to prevent, both reproduced on 2.2.0:

**D2-1.** `judge_variance` was returned whenever the model *name* matched and the
scorer was an LLM judge. Two runs of one model at temperature 0.0 and 1.0 reported
*"the grading setup is byte-identical and the target model is unchanged, yet 1 of 1
shared case(s) came back with a different verdict"* - attributing to the judge a change
that had been made to the target.

**D2-2.** `verdict_changed` compared `pass_count` with `fail_count`, and both are 0 for
a continuous scorer, so every case compared equal. A run whose mean score moved from
0.10 to 0.90 reported `stable` - **"No drift"**.
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
#: Something moved that two receipts cannot attribute. Not a failure to classify: it is
#: the correct answer when the evidence admits more than one explanation.
CANNOT_ISOLATE = "cannot_isolate"
STABLE = "stable"

#: The target-side fields `_config_fields` names. A change in any of them changes what
#: the target did, so a verdict that moved cannot be laid at the judge's door.
TARGET_FIELDS = (
    "target provider", "target endpoint", "target model", "target api version",
    "target system prompt", "target temperature", "target top_p", "target seed",
    "target max_tokens", "target unset params",
)
#: How close two case means have to be to count as unmoved. Scores are floats and a
#: bootstrap interval is not exact; this is a display tolerance, not a significance
#: test - `evalseal diff` is where a difference is tested.
_MEAN_TOLERANCE = 1e-9


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
    #: The case's mean score in each run. The only signal a continuous scorer has:
    #: `pass_count` and `fail_count` are both 0 there, so the majority comparison below
    #: finds every case equal however far its score moved.
    before_mean: float = 0.0
    after_mean: float = 0.0

    @property
    def verdict_changed(self) -> bool:
        """Whether the case's majority verdict moved, not merely its sequence.

        False for a continuous scorer, which has no majority verdict. Use `moved`.
        """
        return _majority(self.before_counts) != _majority(self.after_counts)

    @property
    def mean_delta(self) -> float:
        return round(self.after_mean - self.before_mean, 6)

    @property
    def score_changed(self) -> bool:
        return abs(self.after_mean - self.before_mean) > _MEAN_TOLERANCE

    @property
    def moved(self) -> bool:
        """Whether this case came back differently at all, by either reading.

        A majority verdict is the sharper signal and only a binary scorer has one; a
        mean score is the one every scorer has. A case that moved by either is a case
        that moved, and the classifier asks this rather than the majority alone.
        """
        return self.verdict_changed or self.score_changed

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
            "score_changed": self.score_changed,
            "moved": self.moved,
            "mean": {"before": self.before_mean, "after": self.after_mean,
                     "delta": self.mean_delta},
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
    #: Target-side fields that differ. Named separately from `evaluator_changes`
    #: because they do not block comparison - they decide what a moved verdict can be
    #: attributed to.
    target_changes: list[FieldChange] = field(default_factory=list)
    #: Why `kind` is admissible, as a sentence per ground that held. The inference is
    #: shown with its reasons so a reader can check it instead of trusting it.
    grounds: list[str] = field(default_factory=list)
    #: Observations that bear on reading the comparison but name no cause.
    notes: list[str] = field(default_factory=list)

    @property
    def verdict_changes(self) -> list[CaseDrift]:
        return [c for c in self.cases if c.verdict_changed]

    @property
    def score_changes(self) -> list[CaseDrift]:
        """Cases whose mean moved, whether or not a majority verdict did.

        The superset that matters for a continuous scorer, and the blind spot that made
        an 0.10 -> 0.90 shift report as "No drift".
        """
        return [c for c in self.cases if c.score_changed]

    @property
    def moved_cases(self) -> list[CaseDrift]:
        return [c for c in self.cases if c.moved]

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
        moved = len(self.moved_cases)
        if self.kind == CANNOT_ISOLATE:
            changed = ", ".join(c.name for c in self.target_changes) or "the target"
            return (
                f"Cannot isolate the cause: the grading setup is identical and the "
                f"target model name is unchanged, but {changed} differs between these "
                f"two runs. {moved} of {len(self.cases)} shared case(s) came back "
                "differently, and that is consistent with the judge disagreeing with "
                "itself, with the target answering differently, and with the setting "
                "that changed. Two receipts cannot separate those. Hold the target's "
                "response fixed with `evalseal decompose`, or re-run with the setting "
                "put back, before attributing this to anything."
            )
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
                f"No drift: the grading setup is identical and all {len(self.cases)} "
                "shared case(s) returned the same majority verdict and the same mean "
                "score. Flip rates may still differ within cases; the per-case strips "
                "show that. This is not a claim that the runs would agree again - it "
                "is what these two runs did."
            )
        if self.kind == JUDGE_VARIANCE:
            return (
                f"Judge variance: the grading setup is byte-identical and every "
                f"recorded target setting - model, endpoint, sampling parameters, "
                f"system prompt - is identical, yet {moved} of {len(self.cases)} shared "
                "case(s) came back differently. An LLM judge is a sampler, so re-running "
                "it is not free of consequence. This still cannot be separated from the "
                "target answering differently on a fresh draw using two receipts alone - "
                "`evalseal decompose` holds one response fixed and re-judges it, which "
                "can."
            )
        return (
            f"Target variance: the grading is deterministic and unchanged and every "
            f"recorded target setting is identical, so the {moved} changed case(s) of "
            f"{len(self.cases)} can only come from the target answering differently."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "comparable": self.comparable,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "evaluator_changes": [c.to_dict() for c in self.evaluator_changes],
            "target_changes": [c.to_dict() for c in self.target_changes],
            # The inference, and the grounds it rests on. Separated from the counts
            # below, which are observations and carry no attribution.
            "grounds": self.grounds,
            "notes": self.notes,
            "schema_note": self.schema_note,
            "n_shared_cases": len(self.cases),
            "n_verdict_changes": len(self.verdict_changes),
            "n_score_changes": len(self.score_changes),
            "n_moved_cases": len(self.moved_cases),
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
            before_mean=by_before[cid].mean,
            after_mean=by_after[cid].mean,
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

    before_fields, after_fields = _config_fields(before), _config_fields(after)
    target_changes = [
        FieldChange(name, before_fields[name], after_fields[name])
        for name in TARGET_FIELDS
        if before_fields.get(name) != after_fields.get(name)
    ]
    kind, grounds = _classify(before, after, comparable, cases, target_changes)
    return DriftReport(
        kind=kind, comparable=comparable,
        before_fingerprint=fp_before, after_fingerprint=fp_after,
        evaluator_changes=changes, cases=cases,
        only_in_before=sorted(set(by_before) - set(by_after)),
        only_in_after=sorted(set(by_after) - set(by_before)),
        schema_note=schema_note,
        target_changes=target_changes,
        grounds=grounds,
        notes=_notes(before, after),
    )


def _notes(before: RunRecord, after: RunRecord) -> list[str]:
    """Observations that change how the comparison reads but name no cause."""
    notes: list[str] = []
    origins = [r.manifest.execution.origin if r.manifest.execution else None
               for r in (before, after)]
    if None in origins:
        notes.append(
            "One of these runs was sealed before schema 1.7, so it does not record "
            "whether its numbers came from a provider or from a cassette. A replayed "
            "run and a live one are not the same evidence about a model.")
    elif origins[0] != origins[1]:
        notes.append(
            f"These runs have different execution origins ({origins[0]} and "
            f"{origins[1]}). A replayed run reproduces recorded draws; it is not a "
            "fresh measurement, so a difference between the two is not a difference "
            "the model produced today.")
    unscorable = [r.aggregate.n_unscorable for r in (before, after)]
    if any(unscorable):
        notes.append(
            f"Ungraded observations: {unscorable[0]} in the earlier run and "
            f"{unscorable[1]} in the later one. Those repeats score 0.0 with no verdict "
            "behind them, so a case mean that moved may reflect gradeability rather "
            "than behaviour.")
    return notes


def _classify(
    before: RunRecord,
    after: RunRecord,
    comparable: bool,
    cases: list[CaseDrift],
    target_changes: list[FieldChange],
) -> tuple[str, list[str]]:
    """Name the cause from the receipts, and say on what grounds.

    Each branch returns the grounds that admitted it, in the order
    docs/metrics.md §10 tabulates them. A label with no grounds is not reachable.
    """
    if not comparable:
        return EVALUATOR_DRIFT, ["the evaluator fingerprint differs"]

    grounds = ["the evaluator fingerprint is identical"]
    changed = {c.name for c in target_changes}
    if "target model" in changed:
        # A deliberate comparison, not drift. Other target fields may differ too and
        # that is expected: two models are not required to share an endpoint.
        return TARGET_CHANGE, [*grounds, "the requested target model differs"]
    if changed:
        # The model name matches and something else about the target does not. Every
        # remaining explanation is still open, so none of them may be named. Until
        # Phase D this fell through to the judge: two runs of one model at temperature
        # 0.0 and 1.0 were reported as "the target model is unchanged, yet ... came
        # back with a different verdict".
        return CANNOT_ISOLATE, [
            *grounds,
            "the requested target model is identical",
            "but " + ", ".join(sorted(changed)) + " differs, so a changed verdict is "
            "consistent with the judge, with the target, and with that setting",
        ]
    grounds.append("every recorded target field is identical")
    if not any(c.moved for c in cases):
        # Asked of `moved`, not of the majority verdict alone: a continuous scorer has
        # no majority, so the majority comparison called every case equal and an
        # 0.10 -> 0.90 shift in the mean reported as "No drift".
        return STABLE, [*grounds, "no shared case changed majority verdict or mean score"]
    # Only an LLM judge can disagree with itself; a regex cannot, so with a
    # deterministic scorer the target is the only thing left.
    if after.manifest.scorer.type == "llm_judge":
        return JUDGE_VARIANCE, [*grounds, "the scorer is an LLM judge, which samples"]
    return TARGET_VARIANCE, [*grounds, "the scorer is deterministic, so it cannot "
                                       "disagree with itself"]


def render(report: DriftReport) -> str:
    """Markdown, leading with what changed rather than with a number."""
    lines = [f"# Judge drift: {report.kind.replace('_', ' ')}", ""]
    lines.append(report.summary())
    lines.append("")
    if report.grounds:
        # The inference is shown with its grounds, so a reader can check the label
        # rather than take it. docs/metrics.md §10 is the table these come from.
        lines += ["**Why this label is admissible:**", ""]
        lines += [f"- {ground}" for ground in report.grounds]
        lines.append("")
    if report.schema_note:
        lines += [f"> {report.schema_note}", ""]
    for note in report.notes:
        lines += [f"> {note}", ""]

    lines += ["| | before | after |", "|---|---|---|",
              f"| evaluator fingerprint | `{report.before_fingerprint[:30]}…` "
              f"| `{report.after_fingerprint[:30]}…` |",
              f"| comparable | {'yes' if report.comparable else '**no**'} | |",
              f"| mean flip rate | {report.before_flip_rate:.1%} | {report.after_flip_rate:.1%} |",
              f"| cases with a changed majority verdict | {len(report.verdict_changes)} "
              f"of {len(report.cases)} | |",
              # Listed beside the verdict count rather than instead of it: a shift that
              # moves every case's score a little changes no majority at all.
              f"| cases with a changed mean score | {len(report.score_changes)} "
              f"of {len(report.cases)} | |",
              f"| cases with a changed stability class | {len(report.stability_changes)} | |",
              ""]

    if report.target_changes:
        lines += ["## What changed about the target", "",
                  "These do not block comparison. They decide what a moved verdict may "
                  "be attributed to.", "",
                  "| field | before | after |", "|---|---|---|"]
        for change in report.target_changes:
            lines.append(f"| {change.name} | `{change.before}` | `{change.after}` |")
        lines.append("")

    if not report.comparable and report.evaluator_changes:
        lines += ["## What changed in the evaluator", "",
                  "| field | before | after |", "|---|---|---|"]
        for change in report.evaluator_changes:
            lines.append(f"| {change.name} | `{change.before}` | `{change.after}` |")
        lines.append("")

    moved = report.moved_cases
    if moved:
        lines += ["## Cases that came back differently", "",
                  "| case | before | after | mean | majority moved |",
                  "|---|---|---|---|---|"]
        for c in moved:
            lines.append(
                f"| {c.case_id} | `{c.before_sequence}` | `{c.after_sequence}` "
                f"| {c.before_mean:.2f} to {c.after_mean:.2f} ({c.mean_delta:+.2f}) "
                f"| {'yes' if c.verdict_changed else 'no'} |")
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
