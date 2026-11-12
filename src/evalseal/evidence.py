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
(`pass_rate_margin`), reusing `analyze.wilson_ci` rather than inventing a second
interval. The flip-rate interval is then derived from it, because the flip rate of a
binary case is `min(p, 1-p)` - a function of the same proportion, so its interval is the
image of the pass-rate interval under that function, not a separate estimate. Its
half-width is `flip_rate_margin`.

**Both are reported, under names that say which they are.** They are not the same
number. At three passes in five the pass-rate interval is [0.231, 0.882], a half-width
of 0.326, while the flip-rate interval is [0.118, 0.500], a half-width of 0.191. A gate
field called `target_flip_rate_margin` compared against the first is checking something
other than what it says, and that was the state of the code this module was ported
from: a case whose flip rate was pinned inside a declared 0.25 margin failed a gate
that declared 0.25. The policy rule now compares the margin it names.

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
- `not_binary` - a continuous scorer with no majority verdict, where the binomial
  framing does not apply at all. Said plainly rather than given a number that looks
  like one. A case like this has **no margin**, and a rule that gates on a margin
  excludes it and says so: formatting its absent margin into a message raised
  `TypeError: unsupported format string passed to NoneType.__format__` and took the
  gate down whenever the widest uncertain case was a continuous one.

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

## Ungraded repeats are not observations

A case's interval is over the repeats that **got a verdict**. A repeat the grader could
not read (docs/metrics.md §8) contributes 0.0 to the case mean and nothing to this
interval: there is no Bernoulli trial behind it. So `n_graded` is the denominator here
while `n_runs` is reported beside it, and a case with five repeats of which three went
ungraded is treated as the two observations it is.

## Independence is assumed and checked elsewhere

Every interval here assumes a case's repeats are separate draws. `evalseal report
--json` carries the audit that says whether they were, and `independence.py` explains
what distinguishes two draws that agreed from one draw counted twice. Nothing in this
module can detect it.

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
    """Half-width of the Wilson interval on the **pass proportion**."""
    lo, hi = wilson_ci(successes, n, z)
    return (hi - lo) / 2


def flip_margin_at(successes: int, n: int, z: float) -> float:
    """Half-width of the derived **flip-rate** interval.

    A different number from `margin_at` whenever the pass-rate interval straddles 0.5,
    because `min(p, 1-p)` folds the interval there. Both exist so that a threshold can
    be applied to the quantity it names.
    """
    lo, hi = flip_rate_interval(*wilson_ci(successes, n, z))
    return (hi - lo) / 2


#: Which quantity a target margin is about.
PASS_RATE = "pass_rate"
FLIP_RATE = "flip_rate"


def repeats_for_margin(
    target_margin: float, z: float, *, pass_rate: float | None = None,
    quantity: str = FLIP_RATE,
) -> int | None:
    """Smallest repeat count whose half-width is within `target_margin`.

    `quantity` says which interval's half-width is being aimed at, because the two are
    different numbers and `--target-margin` is documented as a flip-rate margin.

    `pass_rate=None` searches the worst case p=0.5, which bounds the answer whatever
    the true rate is. Passing an observed rate answers the conditional question
    instead. Returns None when no count under `_MAX_REPEATS` reaches the margin.
    """
    if target_margin <= 0:
        raise ValueError("target_margin must be positive")
    if quantity not in (PASS_RATE, FLIP_RATE):
        raise ValueError(f"unknown quantity {quantity!r}")
    width = margin_at if quantity == PASS_RATE else flip_margin_at
    for n in range(1, _MAX_REPEATS):
        rate = 0.5 if pass_rate is None else pass_rate
        successes = round(rate * n)
        if width(successes, n, z) <= target_margin:
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
    #: Repeats that got a verdict. The denominator of every interval below: a repeat
    #: the grader could not read is not a Bernoulli trial. docs/metrics.md §8.
    n_graded: int
    #: Repeats the grader could not read. With `n_graded` it says *why* a case has no
    #: interval: `n_graded == 0` with ungraded repeats is a hole in the measurement,
    #: and `n_graded == 0` with none is a continuous scorer, which never had one.
    n_unscorable: int
    flip_count: int
    flip_rate: float
    pass_count: int
    pass_rate: float | None
    pass_ci: tuple[float, float] | None
    flip_rate_ci: tuple[float, float] | None
    #: Half-width of the pass-rate interval. The labels are cut on this.
    pass_rate_margin: float | None
    #: Half-width of the derived flip-rate interval. A policy naming a flip-rate
    #: margin is checked against **this**, not against the one above.
    flip_rate_margin: float | None
    label: str
    action: str
    repeats_worst_case: int | None
    repeats_at_observed_rate: int | None

    @property
    def margin(self) -> float | None:
        """The pass-rate margin, under its old name. Prefer the explicit fields."""
        return self.pass_rate_margin

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
            "n_graded": self.n_graded,
            "n_unscorable": self.n_unscorable,
            "flip_count": self.flip_count,
            "flip_rate": self.flip_rate,
            "pass_count": self.pass_count,
            "pass_rate": self.pass_rate,
            "pass_ci": list(self.pass_ci) if self.pass_ci else None,
            "flip_rate_ci": list(self.flip_rate_ci) if self.flip_rate_ci else None,
            "pass_rate_margin": self.pass_rate_margin,
            "flip_rate_margin": self.flip_rate_margin,
            # The old key, carrying the pass-rate margin it always carried.
            "margin": self.pass_rate_margin,
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
    "model. `pass_rate_margin` is the half-width of the Wilson interval on the pass "
    "proportion and `flip_rate_margin` the half-width of the flip-rate interval "
    "derived from it - they differ whenever the interval straddles 0.5, and a "
    "threshold applies to the one it names. Intervals are over graded repeats only, "
    "and they assume those repeats were separate draws, which `report --json` reports "
    "on and this module cannot see. A flip rate below five repeats is a smoke "
    "detector, not a rate."
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
        """Fewest **graded** repeats on any case: the labels are cut on those."""
        return min((c.n_graded for c in self.cases), default=0)

    @property
    def worst_margin(self) -> float | None:
        """The widest pass-rate margin, or None when no case has one."""
        margins = [c.pass_rate_margin for c in self.cases
                   if c.pass_rate_margin is not None]
        return max(margins) if margins else None

    @property
    def worst_flip_margin(self) -> float | None:
        margins = [c.flip_rate_margin for c in self.cases
                   if c.flip_rate_margin is not None]
        return max(margins) if margins else None

    @property
    def not_binary(self) -> list[CaseEvidence]:
        """Cases with no interval at all, so no margin rule can apply to them."""
        return [c for c in self.cases if c.flip_rate_margin is None]

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
            key=lambda c: (-(c.pass_rate_margin if c.pass_rate_margin is not None
                             else -1), c.n_graded, c.case_id))[:n]

    def summary(self) -> str:
        """One paragraph that does not overstate what the repeats support."""
        counts = self.counts
        if not self.cases:
            return "No cases, so there is nothing to say about the evidence."
        weak = len(self.underpowered)
        if weak == 0:
            return (
                f"All {len(self.cases)} case(s) clear a {self.target_margin:.2f} "
                f"pass-rate margin at {self.confidence:.0%} confidence, the widest "
                f"being {self.worst_margin:.3f}. Flip rates here can be read as rates."
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
            "worst_pass_rate_margin": self.worst_margin,
            "worst_flip_rate_margin": self.worst_flip_margin,
            "n_not_binary": len(self.not_binary),
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
    # The graded repeats are the trials. A repeat the grader could not read carries no
    # verdict, so it is not an observation of the pass proportion - it is counted in
    # `n_runs` and excluded here, exactly as `analyze_case` excludes it from the
    # interval it seals.
    graded = case.pass_count + case.fail_count
    if case.pass_count > n or graded > n:
        # A hand-edited or truncated receipt. Without this the Wilson formula takes the
        # square root of a negative number and raises `math domain error` three frames
        # down, which says nothing about the receipt.
        raise ValueError(
            f"case {case.case_id!r} reports {case.pass_count} pass(es) and "
            f"{case.fail_count} fail(s) in {n} run(s), which cannot happen; the "
            "receipt is inconsistent")
    binary = case.majority_verdict is not None and graded > 0

    pass_rate = case.pass_count / graded if binary else None
    pass_ci = wilson_ci(case.pass_count, graded, z) if binary else None
    pass_margin = (pass_ci[1] - pass_ci[0]) / 2 if pass_ci else None
    flip_ci = flip_rate_interval(*pass_ci) if pass_ci else None
    flip_margin = (flip_ci[1] - flip_ci[0]) / 2 if flip_ci else None
    label = classify_evidence(graded, pass_margin)

    return CaseEvidence(
        case_id=case.case_id,
        n_runs=n,
        n_graded=graded,
        n_unscorable=case.unscorable_count,
        flip_count=case.flip_count,
        flip_rate=case.flip_rate,
        pass_count=case.pass_count,
        pass_rate=round(pass_rate, 4) if pass_rate is not None else None,
        pass_ci=(round(pass_ci[0], 4), round(pass_ci[1], 4)) if pass_ci else None,
        flip_rate_ci=flip_ci,
        pass_rate_margin=round(pass_margin, 4) if pass_margin is not None else None,
        flip_rate_margin=round(flip_margin, 4) if flip_margin is not None else None,
        label=label,
        action=recommend(label, case.flip_count),
        # Aimed at the flip-rate margin, which is what `--target-margin` is documented
        # to be: "how tightly you want a flip rate pinned down".
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
        f"| fewest graded repeats on any case | {evidence.min_runs} |",
        "| widest pass-rate margin | "
        + (f"{evidence.worst_margin:.3f}" if evidence.worst_margin is not None else "-")
        + " |",
        "| widest flip-rate margin | "
        + (f"{evidence.worst_flip_margin:.3f}"
           if evidence.worst_flip_margin is not None else "-")
        + " |",
        "",
        "| evidence | cases |", "|---|---|",
    ]
    lines += [f"| {label.replace('_', ' ')} | {counts[label]} |"
              for label in EVIDENCE_LABELS if counts[label]]
    lines.append("")

    worst = evidence.worst()
    if worst:
        label = f"{evidence.confidence:.0%}"
        lines += ["## Where to spend repeats first", "",
                  f"| case | graded | flips | {label} flip-rate range "
                  f"| flip margin | evidence | +repeats |",
                  "|---|---|---|---|---|---|---|"]
        for c in worst:
            rng = (f"{c.flip_rate_ci[0]:.2f}-{c.flip_rate_ci[1]:.2f}"
                   if c.flip_rate_ci else "-")
            extra = c.additional_repeats_at_observed_rate
            graded = (f"{c.n_graded}" if c.n_graded == c.n_runs
                      else f"{c.n_graded} of {c.n_runs}")
            lines.append(
                f"| {c.case_id} | {graded} | {c.flip_count} | {rng} | "
                + (f"{c.flip_rate_margin:.3f}" if c.flip_rate_margin is not None else "-")
                + f" | {c.label.replace('_', ' ')} | "
                + (f"+{extra}" if extra else "-") + " |")
        lines.append("")

    lines += [f"> {CAVEAT}"]
    return "\n".join(lines)


__all__ = [
    "CAVEAT", "DIRECTIONAL", "EVIDENCE_LABELS", "FLIP_RATE", "INSUFFICIENT", "MODERATE",
    "NOT_BINARY", "PASS_RATE", "STRONGER", "CaseEvidence", "SuiteEvidence",
    "analyze_evidence", "case_evidence", "classify_evidence", "flip_margin_at",
    "flip_rate_interval", "margin_at", "recommend", "render", "repeats_for_margin",
    "z_for_confidence",
]
