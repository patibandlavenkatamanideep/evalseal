"""How much a run actually tells you, and how many repeats would tell you more.

## The question this answers, and the one it does not

`evalseal power` asks a *prospective, comparative* question: how many items or repeats
would it take for a paired test to detect a difference of a given size between two runs.
This module asks a *retrospective, single-run* question: given the repeats that already
happened, how precisely is each case's behaviour pinned down?

They are different questions and they are deliberately different commands. Conflating
them would put `--mdd` and `--target-margin` on one command where each is meaningless to
the other, in the same way `diff` and `drift` are separate because "did the score move"
and "what moved underneath it" are different questions.

## Why a flip rate needs an interval at all

At N=5 the only flip rates that exist are 0, 0.2 and 0.4. A case reported at "20%" did
not exhibit a twenty-percent tendency; exactly one of five runs disagreed. Reading the
percentage as a rate attributes a precision to five observations that five observations
do not carry - the same trap `classify_stability` was rewritten to avoid.

So this module reports the **Wilson interval of the pass proportion** and its half-width
(the *margin*), reusing `analyze.wilson_ci` rather than inventing a second interval. The
flip-rate interval is then derived from it, because the flip rate of a binary case is
`min(p, 1-p)` - a function of the same proportion, so its interval is the image of the
pass-rate interval under that function, not a separate estimate.

## What the labels mean, and where the thresholds come from

The thresholds are the observed Wilson half-widths at repeat counts people actually use,
not round numbers chosen for looking tidy:

| repeats | half-width, unanimous | half-width, near 0.5 |
|---|---|---|
| 5 | 0.217 | 0.326 |
| 11 | 0.129 | 0.254 |
| 21 | 0.077 | 0.196 |
| 31 | 0.055 | 0.166 |

- `insufficient_runs` - fewer than five repeats. The flip rate is a smoke detector: a
  non-zero value is worth looking at, and its magnitude means nothing.
- `directional_signal` - margin wider than 0.15. You can often read which way a case
  goes; you cannot read *how often* it flips.
- `moderate_evidence` - margin from 0.075 to 0.15.
- `stronger_evidence` - margin at or below 0.075.
- `not_binary` - a float scorer with no majority verdict, where the binomial framing
  does not apply at all. Said plainly rather than given a number that looks like one.

A consequence worth stating: a genuinely flaky case stays `directional_signal` far
longer than a unanimous one, because the interval is widest near p=0.5. That is not a
defect. The cases you most want to quantify are the ones that take the most repeats to
quantify.

## Two repeat recommendations, not one

Asking "how many repeats for a margin of 0.10" has two honest answers, and reporting
only one of them misleads in a predictable direction:

- **Worst case** - the count that reaches the margin *whatever the true rate is*,
  computed at p=0.5 where the interval is widest. Large, and it cannot over-promise.
- **At the observed rate** - the count that reaches the margin *if the rate seen so far
  is roughly right*. Much smaller for a unanimous case, and conditional on an assumption
  that the small sample is exactly what is in question.

Reporting only the worst-case number reads as "give up"; reporting only the observed one
is optimistic about the very thing being measured. Both, labelled.

Everything here is approximate guidance for deciding where to spend repeats. None of it
is a claim about the model.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .analyze import MIN_RUNS_FOR_STABILITY, wilson_ci
from .models import CaseResult, RunRecord

INSUFFICIENT = "insufficient_runs"
DIRECTIONAL = "directional_signal"
MODERATE = "moderate_evidence"
STRONGER = "stronger_evidence"
NOT_BINARY = "not_binary"

EVIDENCE_LABELS = (INSUFFICIENT, DIRECTIONAL, MODERATE, STRONGER, NOT_BINARY)

# Margin boundaries. See the module docstring for the half-widths these come from.
DIRECTIONAL_ABOVE = 0.15
MODERATE_ABOVE = 0.075

DEFAULT_CONFIDENCE = 0.95
DEFAULT_TARGET_MARGIN = 0.10

# A hard stop for the repeat search. At p=0.5 and a 0.01 margin the answer is in the
# tens of thousands, and a number that large is advice to change the question instead.
_MAX_REPEATS = 2001

ACTION_INCREASE = "increase repeats"
ACTION_INSPECT = "inspect case"
ACTION_DO_NOT_GATE = "do not gate on this yet"
ACTION_STABLE_ENOUGH = "stable enough for configured threshold"
ACTION_NOT_APPLICABLE = "no binomial framing: read the scores directly"


def z_for_confidence(confidence: float) -> float:
    """Two-sided normal quantile. 0.95 gives 1.95996..., matching `analyze._Z95`."""
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    return statistics.NormalDist().inv_cdf(1 - (1 - confidence) / 2)


def flip_rate_interval(pass_lo: float, pass_hi: float) -> tuple[float, float]:
    """Map a pass-rate interval to a flip-rate interval.

    A binary case's flip rate is `min(p, 1-p)`, which rises on [0, 0.5] and falls on
    [0.5, 1]. So the image of [lo, hi] peaks at 0.5 when the interval straddles it, and
    otherwise at whichever endpoint is nearer. Derived rather than estimated separately,
    so the two intervals can never disagree.
    """
    def f(p: float) -> float:
        return min(p, 1.0 - p)

    low = min(f(pass_lo), f(pass_hi))
    high = 0.5 if pass_lo <= 0.5 <= pass_hi else max(f(pass_lo), f(pass_hi))
    return (round(low, 4), round(high, 4))


def margin_at(successes: int, n: int, z: float) -> float:
    """Half-width of the Wilson interval: the number the labels are cut on."""
    lo, hi = wilson_ci(successes, n, z)
    return (hi - lo) / 2


def repeats_for_margin(
    target_margin: float, z: float, *, pass_rate: float | None = None
) -> int | None:
    """Smallest repeat count whose Wilson half-width is within `target_margin`.

    `pass_rate=None` searches the worst case p=0.5, which bounds the answer whatever
    the true rate is. Passing an observed rate answers the conditional question
    instead. Returns None when no count under `_MAX_REPEATS` reaches the margin.
    """
    if target_margin <= 0:
        raise ValueError("target_margin must be positive")
    for n in range(1, _MAX_REPEATS):
        rate = 0.5 if pass_rate is None else pass_rate
        successes = round(rate * n)
        if margin_at(successes, n, z) <= target_margin:
            return n
    return None


def classify_evidence(n_runs: int, margin: float | None) -> str:
    """One label per case, from the repeat count and the interval width."""
    if margin is None:
        return NOT_BINARY
    if n_runs < MIN_RUNS_FOR_STABILITY:
        return INSUFFICIENT
    if margin > DIRECTIONAL_ABOVE:
        return DIRECTIONAL
    if margin > MODERATE_ABOVE:
        return MODERATE
    return STRONGER


def recommend(label: str, flip_count: int) -> str:
    """What to do about one case. Deliberately one of a small fixed set."""
    if label == NOT_BINARY:
        return ACTION_NOT_APPLICABLE
    if label == INSUFFICIENT:
        # A flip at N<5 is still worth a human look even though its rate means nothing.
        return ACTION_INSPECT if flip_count else ACTION_DO_NOT_GATE
    if label == DIRECTIONAL:
        return ACTION_INSPECT if flip_count else ACTION_INCREASE
    return ACTION_INSPECT if flip_count else ACTION_STABLE_ENOUGH


@dataclass(frozen=True)
class CaseEvidence:
    """What one case's repeats support, and what they do not."""
    case_id: str
    n_runs: int
    flip_count: int
    flip_rate: float
    pass_count: int
    pass_rate: float | None
    pass_ci: tuple[float, float] | None
    flip_rate_ci: tuple[float, float] | None
    margin: float | None
    label: str
    action: str
    repeats_worst_case: int | None
    repeats_at_observed_rate: int | None

    @property
    def underpowered(self) -> bool:
        """Too few repeats, or an interval too wide to read a magnitude from."""
        return self.label in (INSUFFICIENT, DIRECTIONAL)

    @property
    def additional_repeats_worst_case(self) -> int | None:
        if self.repeats_worst_case is None:
            return None
        return max(0, self.repeats_worst_case - self.n_runs)

    @property
    def additional_repeats_at_observed_rate(self) -> int | None:
        if self.repeats_at_observed_rate is None:
            return None
        return max(0, self.repeats_at_observed_rate - self.n_runs)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "n_runs": self.n_runs,
            "flip_count": self.flip_count,
            "flip_rate": self.flip_rate,
            "pass_count": self.pass_count,
            "pass_rate": self.pass_rate,
            "pass_ci": list(self.pass_ci) if self.pass_ci else None,
            "flip_rate_ci": list(self.flip_rate_ci) if self.flip_rate_ci else None,
            "margin": self.margin,
            "label": self.label,
            "underpowered": self.underpowered,
            "action": self.action,
            "repeats_for_target_margin": {
                "worst_case": self.repeats_worst_case,
                "at_observed_rate": self.repeats_at_observed_rate,
                "additional_worst_case": self.additional_repeats_worst_case,
                "additional_at_observed_rate": self.additional_repeats_at_observed_rate,
            },
        }


CAVEAT = (
    "Approximate guidance for deciding where to spend repeats, not a claim about the "
    "model. A margin is the half-width of the Wilson interval on the pass proportion; "
    "a flip rate below five repeats is a smoke detector, not a rate."
)


@dataclass
class SuiteEvidence:
    """The per-case view plus the one number a gate can act on."""
    cases: list[CaseEvidence] = field(default_factory=list)
    confidence: float = DEFAULT_CONFIDENCE
    target_margin: float = DEFAULT_TARGET_MARGIN

    @property
    def counts(self) -> dict[str, int]:
        return {label: sum(1 for c in self.cases if c.label == label)
                for label in EVIDENCE_LABELS}

    @property
    def underpowered(self) -> list[CaseEvidence]:
        return [c for c in self.cases if c.underpowered]

    @property
    def min_runs(self) -> int:
        return min((c.n_runs for c in self.cases), default=0)

    @property
    def worst_margin(self) -> float | None:
        margins = [c.margin for c in self.cases if c.margin is not None]
        return max(margins) if margins else None

    @property
    def label(self) -> str:
        """The suite is only as well-evidenced as its weakest case."""
        order = {INSUFFICIENT: 0, DIRECTIONAL: 1, MODERATE: 2, STRONGER: 3}
        ranked = [order[c.label] for c in self.cases if c.label in order]
        if not ranked:
            return NOT_BINARY
        return next(k for k, v in order.items() if v == min(ranked))

    def worst(self, n: int = 5) -> list[CaseEvidence]:
        """Cases to spend repeats on first: widest interval, then fewest runs."""
        return sorted(
            self.cases,
            key=lambda c: (-(c.margin if c.margin is not None else -1), c.n_runs,
                           c.case_id))[:n]

    def summary(self) -> str:
        """One paragraph that does not overstate what the repeats support."""
        counts = self.counts
        if not self.cases:
            return "No cases, so there is nothing to say about the evidence."
        weak = len(self.underpowered)
        if weak == 0:
            return (
                f"All {len(self.cases)} case(s) clear a {self.target_margin:.2f} margin "
                f"at {self.confidence:.0%} confidence, the widest being "
                f"{self.worst_margin:.3f}. Flip rates here can be read as rates."
            )
        return (
            f"{weak} of {len(self.cases)} case(s) are underpowered at "
            f"{self.confidence:.0%} confidence: {counts[INSUFFICIENT]} with fewer than "
            f"{MIN_RUNS_FOR_STABILITY} repeats, {counts[DIRECTIONAL]} whose interval is "
            f"wider than {DIRECTIONAL_ABOVE:.2f}. For those cases the flip rate shows "
            "direction, not magnitude - a non-zero value is worth inspecting and its "
            "size is not worth quoting."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "confidence": self.confidence,
            "target_margin": self.target_margin,
            "label": self.label,
            "n_cases": len(self.cases),
            "n_underpowered": len(self.underpowered),
            "min_runs_per_case": self.min_runs,
            "worst_margin": self.worst_margin,
            "counts": self.counts,
            "summary": self.summary(),
            "caveat": CAVEAT,
            "cases": [c.to_dict() for c in self.cases],
        }


def case_evidence(
    case: CaseResult, *, confidence: float = DEFAULT_CONFIDENCE,
    target_margin: float = DEFAULT_TARGET_MARGIN,
) -> CaseEvidence:
    """Evidence for one case, reusing the interval `analyze` already computes."""
    z = z_for_confidence(confidence)
    n = case.n_runs
    if case.pass_count > n:
        # A hand-edited or truncated receipt. Without this the Wilson formula takes the
        # square root of a negative number and raises `math domain error` three frames
        # down, which says nothing about the receipt.
        raise ValueError(
            f"case {case.case_id!r} reports {case.pass_count} passes in {n} run(s), "
            "which cannot happen; the receipt is inconsistent")
    binary = case.majority_verdict is not None and n > 0

    pass_rate = case.pass_count / n if binary else None
    pass_ci = wilson_ci(case.pass_count, n, z) if binary else None
    margin = (pass_ci[1] - pass_ci[0]) / 2 if pass_ci else None
    label = classify_evidence(n, margin)

    return CaseEvidence(
        case_id=case.case_id,
        n_runs=n,
        flip_count=case.flip_count,
        flip_rate=case.flip_rate,
        pass_count=case.pass_count,
        pass_rate=round(pass_rate, 4) if pass_rate is not None else None,
        pass_ci=(round(pass_ci[0], 4), round(pass_ci[1], 4)) if pass_ci else None,
        flip_rate_ci=flip_rate_interval(*pass_ci) if pass_ci else None,
        margin=round(margin, 4) if margin is not None else None,
        label=label,
        action=recommend(label, case.flip_count),
        repeats_worst_case=(
            repeats_for_margin(target_margin, z) if binary else None),
        repeats_at_observed_rate=(
            repeats_for_margin(target_margin, z, pass_rate=pass_rate)
            if binary and pass_rate is not None else None),
    )


def analyze_evidence(
    record: RunRecord, *, confidence: float = DEFAULT_CONFIDENCE,
    target_margin: float = DEFAULT_TARGET_MARGIN, case_id: str | None = None,
) -> SuiteEvidence:
    """Evidence for a sealed run, or for one case of it."""
    results = record.results
    if case_id is not None:
        results = [c for c in results if c.case_id == case_id]
        if not results:
            known = ", ".join(sorted(c.case_id for c in record.results)[:10])
            raise ValueError(f"no case {case_id!r} in this record; it has: {known}")
    return SuiteEvidence(
        cases=[case_evidence(c, confidence=confidence, target_margin=target_margin)
               for c in results],
        confidence=confidence,
        target_margin=target_margin,
    )


def render(evidence: SuiteEvidence) -> str:
    """Markdown, leading with what the repeats do not support."""
    lines = ["# Evidence", "", evidence.summary(), ""]
    counts = evidence.counts
    lines += [
        "| | |", "|---|---|",
        f"| confidence | {evidence.confidence:.0%} |",
        f"| target margin | {evidence.target_margin:.2f} |",
        f"| cases | {len(evidence.cases)} |",
        f"| underpowered | {len(evidence.underpowered)} |",
        f"| fewest repeats on any case | {evidence.min_runs} |",
        "| widest margin | "
        + (f"{evidence.worst_margin:.3f}" if evidence.worst_margin is not None else "-")
        + " |",
        "",
        "| evidence | cases |", "|---|---|",
    ]
    lines += [f"| {label.replace('_', ' ')} | {counts[label]} |"
              for label in EVIDENCE_LABELS if counts[label]]
    lines.append("")

    worst = evidence.worst()
    if worst:
        lines += ["## Where to spend repeats first", "",
                  "| case | runs | flips | flip rate | 95% flip-rate range | margin "
                  "| evidence | +repeats |", "|---|---|---|---|---|---|---|---|"]
        for c in worst:
            rng = (f"{c.flip_rate_ci[0]:.2f}-{c.flip_rate_ci[1]:.2f}"
                   if c.flip_rate_ci else "-")
            extra = c.additional_repeats_at_observed_rate
            lines.append(
                f"| {c.case_id} | {c.n_runs} | {c.flip_count} | {c.flip_rate:.0%} "
                f"| {rng} | " + (f"{c.margin:.3f}" if c.margin is not None else "-")
                + f" | {c.label.replace('_', ' ')} | "
                + (f"+{extra}" if extra else "-") + " |")
        lines.append("")

    lines += [f"> {CAVEAT}"]
    return "\n".join(lines)


__all__ = [
    "CAVEAT", "DIRECTIONAL", "EVIDENCE_LABELS", "INSUFFICIENT", "MODERATE", "NOT_BINARY",
    "STRONGER", "CaseEvidence", "SuiteEvidence", "analyze_evidence", "case_evidence",
    "classify_evidence", "flip_rate_interval", "margin_at", "recommend", "render",
    "repeats_for_margin", "z_for_confidence",
]
