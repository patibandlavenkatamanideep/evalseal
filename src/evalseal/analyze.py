"""Variance analysis for repeated eval runs. Pure functions, no I/O, no network.

A "verdict" is a binary pass(1)/fail(0). A "score" may be binary or a float in [0,1].
We quantify reproducibility; we never claim determinism.

Binary verdicts get a Wilson score interval; float scores get a seeded percentile
bootstrap. Both are deterministic, so the interval itself is reproducible.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from dataclasses import dataclass

from .models import Stability

# Stability is decided by the Wilson interval of the pass proportion, not by a
# threshold on the flip rate. See `classify_stability` for why.

_BOOTSTRAP_ITERS = 2000
_RNG_SEED = 12345  # fixed so the CI computation itself is reproducible
_Z95 = 1.959963984540054  # standard normal quantile for a two-sided 95% interval


# Below this many runs a flip rate is not worth reading: at N=3 one dissent is 33%.
MIN_RUNS_FOR_STABILITY = 5


@dataclass(frozen=True)
class CaseStats:
    mean: float
    ci95_low: float
    ci95_high: float
    flip_rate: float
    stability: str  # "STABLE" | "BORDERLINE" | "UNSTABLE"
    majority_verdict: int | None  # for binary; None for pure-float scores
    flip_count: int = 0
    stability_label: Stability = "insufficient_runs"


def _bootstrap_ci(scores: list[float], iters: int = _BOOTSTRAP_ITERS) -> tuple[float, float]:
    """Percentile bootstrap 95% CI of the mean. Deterministic via fixed seed."""
    n = len(scores)
    if n == 0:
        return (0.0, 0.0)
    if n == 1:
        return (scores[0], scores[0])
    rng = random.Random(_RNG_SEED)
    means = []
    for _ in range(iters):
        sample = [scores[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * iters)]
    hi = means[int(0.975 * iters)]
    return (lo, hi)


def wilson_ci(successes: int, n: int, z: float = _Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Used instead of the bootstrap for binary verdicts because the bootstrap collapses
    at the boundary: five passes out of five resample to a width-zero interval, which
    would claim certainty the data does not support. Wilson gives [0.57, 1.00] there —
    consistent with the rule of three, which bounds an unseen failure rate near 3/n.
    """
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


def flip_rate(verdicts: list[int]) -> tuple[float, int]:
    """Fraction of verdicts disagreeing with the majority. Returns (rate, majority).

    **Definition (one of two reasonable ones):** flip rate is
    `non-majority verdicts / total runs`, *not* `transitions / (runs - 1)`. So
    PASS,PASS,FAIL,PASS scores 1/4 = 0.25 under this definition and 2/3 under the
    transition one. Chosen because it does not depend on the order runs happened to
    execute in, which matters once runs are concurrent.
    """
    if not verdicts:
        return (0.0, 0)
    counts = Counter(verdicts)
    majority, majority_count = counts.most_common(1)[0]
    disagree = len(verdicts) - majority_count
    return (disagree / len(verdicts), majority)


def classify_stability_label(
    flip_count: int, n_runs: int, majority: int | None
) -> Stability:
    """The finer taxonomy: separates a stable pass from a stable fail, and says when
    there were simply too few runs to judge.

    Note this stays stricter than `classify_stability`: any flip at all is `unstable`
    here, where the coarse label asks only whether the majority direction is
    established at this N.
    """
    if n_runs < MIN_RUNS_FOR_STABILITY:
        return "insufficient_runs"
    if flip_count:
        return "unstable"
    if majority == 1:
        return "stable_pass"
    if majority == 0:
        return "stable_fail"
    return "unstable"


def classify_stability(successes: int, n_runs: int, flip_count: int = 0) -> str:
    """Classify an item by where the Wilson interval of its pass proportion sits.

    Until 2.0 this was a threshold on the flip rate: 0 was STABLE, anything up to 0.20
    BORDERLINE, more UNSTABLE. At the default N=5 those cutoffs cannot mean what they
    look like they mean. The only flip rates five runs can produce are 0, 0.2 and 0.4,
    so "flip rate at most 20%" is not a tolerance band - it is the single outcome
    "exactly one of five runs disagreed". Reading it as a 20% error tolerance, which is
    what a percentage invites, attributes a precision to five observations that five
    observations do not carry.

    The rule now asks the question the classes are supposed to answer: at this N, do we
    know which way this item goes?

    - UNSTABLE   the 95% Wilson interval for the pass proportion contains 0.5, so the
                 direction of the verdict is not established at all.
    - BORDERLINE the interval excludes 0.5 - we can name the majority verdict - but the
                 item did disagree with itself at least once.
    - STABLE     the interval excludes 0.5 and every run agreed.

    A consequence worth stating plainly: at N=5, BORDERLINE is unreachable. 5/5 gives
    [0.57, 1.00] and 0/5 gives [0.00, 0.43], both clear of 0.5, but 4/5 gives
    [0.38, 0.96], which straddles it. So five runs can only say "unanimous" or "not
    established", and the middle class needs more runs to exist. That is not a defect
    in the rule; it is what five observations support. At N=20, 18/20 gives
    [0.70, 0.97] and lands in BORDERLINE as intended.
    """
    if n_runs == 0:
        return "UNSTABLE"
    lo, hi = wilson_ci(successes, n_runs)
    if lo <= 0.5 <= hi:
        return "UNSTABLE"
    return "BORDERLINE" if flip_count else "STABLE"


def analyze_case(scores: list[float], *, binary: bool) -> CaseStats:
    """Turn N per-run scores into a CaseStats. If binary, scores must be 0/1."""
    if not scores:
        raise ValueError("analyze_case requires at least one score")
    mean = sum(scores) / len(scores)
    if binary:
        verdicts = [int(s) for s in scores]
        lo, hi = wilson_ci(sum(verdicts), len(verdicts))
        rate, majority = flip_rate(verdicts)
        flips = len(verdicts) - Counter(verdicts).most_common(1)[0][1]
        return CaseStats(
            mean, lo, hi, rate,
            classify_stability(sum(verdicts), len(verdicts), flips), majority,
            flip_count=flips,
            stability_label=classify_stability_label(flips, len(verdicts), majority),
        )
    lo, hi = _bootstrap_ci(scores)
    # Float scores: define a "flip" as crossing the run-set's own median.
    # This gives a variance signal without a fixed threshold assumption.
    med = statistics.median(scores)
    pseudo = [1 if s >= med else 0 for s in scores]
    rate, _ = flip_rate(pseudo)
    pseudo_flips = len(pseudo) - Counter(pseudo).most_common(1)[0][1]
    # The same Wilson rule, applied to the median-crossing pseudo-verdicts. It inherits
    # whatever the pseudo-verdict heuristic is worth, which DESIGN.md is explicit about.
    return CaseStats(
        mean, lo, hi, rate,
        classify_stability(sum(pseudo), len(pseudo), pseudo_flips), None,
    )
