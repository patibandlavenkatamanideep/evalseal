"""Paired comparison of two sealed runs. Pure functions, no I/O, no network.

Two runs of the same suite are not two independent samples. They share every item, so
the comparison is paired, and treating it as unpaired throws away the pairing that
makes small differences detectable at all.

The statistic this replaces was worse than unpaired. It took the widest *per-case*
Wilson half-width - at N=5 a perfect case spans [0.57, 1.00], a half-width of 0.217 -
and used it as a threshold on a *suite-level* mean over 40 items. Those are quantities
about different things. A per-item interval says how little five repeats tell you about
one item; the suite mean over 40 items is far better determined than any item in it. The
old rule therefore declared almost every real shift "within noise", which reads as
reassurance and is not. It is kept, honestly named, as `per_case_halfwidth`.

What replaces it:

*Binary scorers.* Each item gets one verdict per run (its majority across repeats), and
the two runs are compared with an exact McNemar test on the discordant items - the only
items that carry information about a change in direction. Exact, not the chi-square
approximation, because discordant counts here are routinely under ten.

*Float scorers.* A paired permutation test on the per-item differences: under the null
the run label carries no information, so flipping the sign of an item's difference must
leave the mean difference exchangeable.

*Both.* A 95% CI for the difference in mean score from a cluster bootstrap that
resamples whole items, carrying all of an item's repeats together. Resampling individual
repeats would treat the repeats of one item as independent observations of the suite,
which they are not, and would report an interval that is too narrow.

The verdict is never "no difference". A test that fails to reach significance has not
shown the runs are the same; it has failed to show they differ, which at N=5 is the
usual outcome and a statement about the experiment rather than about the models.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field

from .models import RunRecord

_BOOTSTRAP_ITERS = 10_000
_PERMUTATION_ITERS = 10_000
_SEED = 12345           # fixed, so a reported interval is itself reproducible
_EXACT_PERMUTATION_MAX_ITEMS = 14   # 2**14 = 16384 sign patterns; enumerate below this
DEFAULT_ALPHA = 0.05

REGRESSION = "regression"
IMPROVEMENT = "improvement"
INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class PairedComparison:
    """The result of comparing two runs item by item."""
    n_items: int
    delta: float
    ci95: tuple[float, float]
    p_value: float
    test: str
    verdict: str
    alpha: float = DEFAULT_ALPHA

    # Binary only: items where the two runs disagreed, split by direction.
    discordant_regressed: int = 0     # passed before, failed after
    discordant_improved: int = 0      # failed before, passed after
    ambiguous_items: list[str] = field(default_factory=list)

    # Items that are not in both runs take no part in a paired test.
    items_only_in_before: list[str] = field(default_factory=list)
    items_only_in_after: list[str] = field(default_factory=list)

    # The old pre-2.0 number, kept under a name that says what it actually measures.
    per_case_halfwidth: float = 0.0

    @property
    def n_discordant(self) -> int:
        return self.discordant_regressed + self.discordant_improved

    @property
    def ci_excludes_zero(self) -> bool:
        lo, hi = self.ci95
        return not (lo <= 0.0 <= hi)

    @property
    def significant(self) -> bool:
        """Both criteria have to agree, so the verdict never out-claims either one."""
        return self.p_value < self.alpha and self.ci_excludes_zero

    @property
    def mean_moved_without_majorities(self) -> bool:
        """The mean score moved, but no item changed which way it mostly goes.

        This is the blind spot of a test built on majority verdicts, and it is a real
        pattern rather than an edge case: a shift that lowers every item's pass rate
        from 0.90 to 0.85 moves the suite mean by 0.05 and flips almost no majorities.
        Worse, repeating each item more times makes it *harder* to see, because extra
        repeats sharpen each item toward its own majority and remove the discordance
        the test feeds on. Reporting the disagreement is more useful than resolving it
        silently in either direction.
        """
        return (
            self.test == "exact_mcnemar"
            and self.ci_excludes_zero
            and self.p_value >= self.alpha
        )

    def summary(self) -> str:
        """One line a reader can act on, with the reason attached."""
        if self.verdict == INCONCLUSIVE:
            note = (
                f"inconclusive at this N: delta {self.delta:+.4f}, "
                f"95% CI [{self.ci95[0]:+.4f}, {self.ci95[1]:+.4f}], p={self.p_value:.4g}. "
                "Not evidence the runs are the same; evidence this experiment cannot tell."
            )
            if self.mean_moved_without_majorities:
                note += (
                    f" The interval excludes zero but only {self.n_discordant} item(s) "
                    "changed majority verdict, so the shift is within items rather than "
                    "across them. More repeats would make this harder to detect, not "
                    "easier; more items is the dial that helps."
                )
            return note
        return (
            f"{self.verdict}: delta {self.delta:+.4f}, "
            f"95% CI [{self.ci95[0]:+.4f}, {self.ci95[1]:+.4f}], p={self.p_value:.4g}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "n_items": self.n_items,
            "delta": self.delta,
            "ci95": [self.ci95[0], self.ci95[1]],
            "p_value": self.p_value,
            "test": self.test,
            "verdict": self.verdict,
            "alpha": self.alpha,
            "significant": self.significant,
            "n_discordant": self.n_discordant,
            "discordant_regressed": self.discordant_regressed,
            "discordant_improved": self.discordant_improved,
            "ambiguous_items": self.ambiguous_items,
            "items_only_in_before": self.items_only_in_before,
            "items_only_in_after": self.items_only_in_after,
            "per_case_halfwidth": self.per_case_halfwidth,
        }


def exact_mcnemar(regressed: int, improved: int) -> float:
    """Two-sided exact McNemar p-value from the two discordant counts.

    Concordant items carry no information about a change: an item that passed in both
    runs is consistent with any shift that did not reach it. Conditional on the number
    of discordant items n, the null says each is equally likely to have gone either way,
    so the count in one direction is Binomial(n, 1/2) and the exact two-sided p-value is
    twice the smaller tail, capped at 1.

    Exact rather than the chi-square approximation because n is routinely under ten
    here, where the approximation is not trustworthy.
    """
    n = regressed + improved
    if n == 0:
        # Nothing disagreed. That is not evidence of sameness, and p=1 is the honest
        # encoding of "this test saw nothing to go on".
        return 1.0
    k = min(regressed, improved)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5 ** n)
    return min(1.0, 2.0 * tail)


def paired_bootstrap_ci(
    pairs: list[tuple[list[float], list[float]]],
    iters: int = _BOOTSTRAP_ITERS,
    seed: int = _SEED,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[float, float]:
    """Cluster-bootstrap CI for the difference in mean score, resampling whole items.

    `pairs` holds one (before_scores, after_scores) entry per shared item, each a list
    of that item's repeats. An item is the unit of resampling and its repeats travel
    with it: repeats of one item are not independent observations of the suite, and
    resampling them separately would narrow the interval on an assumption that is false.
    """
    n = len(pairs)
    if n == 0:
        return (0.0, 0.0)
    item_deltas = [_mean(after) - _mean(before) for before, after in pairs]
    if n == 1:
        return (item_deltas[0], item_deltas[0])

    rng = random.Random(seed)
    draws = []
    for _ in range(iters):
        total = 0.0
        for _ in range(n):
            total += item_deltas[rng.randrange(n)]
        draws.append(total / n)
    draws.sort()
    lo_i = int((alpha / 2) * iters)
    hi_i = min(iters - 1, int((1 - alpha / 2) * iters))
    return (draws[lo_i], draws[hi_i])


def paired_permutation_p(
    item_deltas: list[float],
    iters: int = _PERMUTATION_ITERS,
    seed: int = _SEED,
) -> float:
    """Two-sided paired permutation p-value for a mean difference.

    Under the null the run label tells you nothing, so an item's difference is as likely
    to have come out with the opposite sign. Enumerate every sign pattern when there are
    few enough items, otherwise sample them; either way the observed assignment is
    included in the null distribution, which is what keeps the p-value from ever
    reaching exactly zero.
    """
    n = len(item_deltas)
    if n == 0:
        return 1.0
    observed = abs(sum(item_deltas) / n)
    if observed == 0.0:
        return 1.0

    if n <= _EXACT_PERMUTATION_MAX_ITEMS:
        at_least = 0
        total = 0
        for signs in itertools.product((1, -1), repeat=n):
            total += 1
            stat = abs(sum(s * d for s, d in zip(signs, item_deltas, strict=True)) / n)
            if stat >= observed - 1e-12:
                at_least += 1
        return at_least / total

    rng = random.Random(seed)
    at_least = 1          # the observed assignment is itself a permutation
    for _ in range(iters):
        stat = abs(
            sum(d if rng.random() < 0.5 else -d for d in item_deltas) / n
        )
        if stat >= observed - 1e-12:
            at_least += 1
    return at_least / (iters + 1)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def is_binary(record: RunRecord) -> bool:
    """Whether every score in the record is 0 or 1.

    Read from the data rather than from the scorer name: a scorer declared binary that
    emitted 0.5 once would otherwise be tested with the wrong machinery silently.
    """
    return all(s in (0.0, 1.0) for c in record.results for s in c.scores)


def _item_verdict(scores: list[float]) -> int | None:
    """One verdict for an item from its repeats. None when the repeats split evenly.

    A tie carries no direction, so it is excluded from McNemar and reported rather than
    broken by a rule that would invent one. With an odd number of repeats, which the
    default N=5 is, a tie cannot occur.
    """
    m = _mean(scores)
    if m > 0.5:
        return 1
    if m < 0.5:
        return 0
    return None


def per_case_halfwidth(record: RunRecord) -> float:
    """Widest per-case CI half-width in the run. The pre-2.0 "noise floor".

    Retained because it is a real property of the run - it says how little N repeats
    pin down a single item - but it is not a threshold for a suite-level difference,
    and it was used as one until 2.0.
    """
    return max(((c.ci95[1] - c.ci95[0]) / 2 for c in record.results), default=0.0)


def compare_runs(
    before: RunRecord,
    after: RunRecord,
    alpha: float = DEFAULT_ALPHA,
) -> PairedComparison:
    """Compare two runs item by item and say what the evidence supports."""
    by_id_b = {c.case_id: c for c in before.results}
    by_id_a = {c.case_id: c for c in after.results}
    shared = sorted(set(by_id_b) & set(by_id_a))
    only_b = sorted(set(by_id_b) - set(by_id_a))
    only_a = sorted(set(by_id_a) - set(by_id_b))
    halfwidth = round(max(per_case_halfwidth(before), per_case_halfwidth(after)), 4)

    if not shared:
        return PairedComparison(
            n_items=0, delta=0.0, ci95=(0.0, 0.0), p_value=1.0,
            test="none", verdict=INCONCLUSIVE, alpha=alpha,
            items_only_in_before=only_b, items_only_in_after=only_a,
            per_case_halfwidth=halfwidth,
        )

    pairs = [(by_id_b[i].scores, by_id_a[i].scores) for i in shared]
    item_deltas = [_mean(a) - _mean(b) for b, a in pairs]
    delta = round(sum(item_deltas) / len(item_deltas), 6)
    lo, hi = paired_bootstrap_ci(pairs, alpha=alpha)

    binary = is_binary(before) and is_binary(after)
    ambiguous: list[str] = []
    regressed = improved = 0

    if binary:
        for item, (b_scores, a_scores) in zip(shared, pairs, strict=True):
            vb, va = _item_verdict(b_scores), _item_verdict(a_scores)
            if vb is None or va is None:
                ambiguous.append(item)
                continue
            if vb == 1 and va == 0:
                regressed += 1
            elif vb == 0 and va == 1:
                improved += 1
        p = exact_mcnemar(regressed, improved)
        test = "exact_mcnemar"
    else:
        p = paired_permutation_p(item_deltas)
        test = "paired_permutation"

    result = PairedComparison(
        n_items=len(shared),
        delta=delta,
        ci95=(round(lo, 6), round(hi, 6)),
        p_value=p,
        test=test,
        verdict=INCONCLUSIVE,
        alpha=alpha,
        discordant_regressed=regressed,
        discordant_improved=improved,
        ambiguous_items=ambiguous,
        items_only_in_before=only_b,
        items_only_in_after=only_a,
        per_case_halfwidth=halfwidth,
    )
    if not result.significant:
        return result
    direction = IMPROVEMENT if delta > 0 else REGRESSION
    return PairedComparison(**{**result.__dict__, "verdict": direction})
