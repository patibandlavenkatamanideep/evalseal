"""Variance analysis for repeated eval runs. Pure functions, no I/O, no network.

A "verdict" is a binary pass(1)/fail(0). A "score" may be binary or a float in [0,1].
We quantify reproducibility; we never claim determinism.
"""
from __future__ import annotations

import random
import statistics
from collections import Counter
from dataclasses import dataclass

# Stability thresholds (constants so they're auditable, not magic numbers).
BORDERLINE_MAX_FLIP = 0.20  # flip_rate in (0, 0.20]  -> BORDERLINE
# flip_rate == 0 -> STABLE ; flip_rate > BORDERLINE_MAX_FLIP -> UNSTABLE

_BOOTSTRAP_ITERS = 2000
_RNG_SEED = 12345  # fixed so the CI computation itself is reproducible


@dataclass(frozen=True)
class CaseStats:
    mean: float
    ci95_low: float
    ci95_high: float
    flip_rate: float
    stability: str  # "STABLE" | "BORDERLINE" | "UNSTABLE"
    majority_verdict: int | None  # for binary; None for pure-float scores


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


def flip_rate(verdicts: list[int]) -> tuple[float, int]:
    """Fraction of verdicts disagreeing with the majority. Returns (rate, majority)."""
    if not verdicts:
        return (0.0, 0)
    counts = Counter(verdicts)
    majority, majority_count = counts.most_common(1)[0]
    disagree = len(verdicts) - majority_count
    return (disagree / len(verdicts), majority)


def classify_stability(rate: float) -> str:
    if rate == 0.0:
        return "STABLE"
    if rate <= BORDERLINE_MAX_FLIP:
        return "BORDERLINE"
    return "UNSTABLE"


def analyze_case(scores: list[float], *, binary: bool) -> CaseStats:
    """Turn N per-run scores into a CaseStats. If binary, scores must be 0/1."""
    if not scores:
        raise ValueError("analyze_case requires at least one score")
    mean = sum(scores) / len(scores)
    lo, hi = _bootstrap_ci(scores)
    if binary:
        verdicts = [int(s) for s in scores]
        rate, majority = flip_rate(verdicts)
        return CaseStats(mean, lo, hi, rate, classify_stability(rate), majority)
    # Float scores: define a "flip" as crossing the run-set's own median.
    # This gives a variance signal without a fixed threshold assumption.
    med = statistics.median(scores)
    pseudo = [1 if s >= med else 0 for s in scores]
    rate, _ = flip_rate(pseudo)
    return CaseStats(mean, lo, hi, rate, classify_stability(rate), None)
