"""How big an experiment would have to be to detect a difference you care about.

`diff` keeps returning "inconclusive at this N", which is the honest answer and not a
useful one on its own. This module answers the follow-up: how many items, or how many
repeats, would it take?

## Method

Simulation, not a closed form. For each trial the simulator draws N repeats per item for
a baseline arm and a candidate arm, reduces each item to a majority verdict, and applies
the same exact McNemar test that `evalseal diff` applies. Power is the fraction of
trials that reach significance at alpha. Everything is seeded, so an estimate is
reproducible; it still carries Monte Carlo error of roughly sqrt(p(1-p)/trials), about
one percentage point at the default 2000 trials.

Power is computed from the test alone. `diff` additionally requires the bootstrap CI to
exclude zero, which is very slightly stricter, so these numbers are a mild upper bound
on what `diff` will declare. Running a 10,000-resample bootstrap inside every trial
would cost far more than that distinction is worth.

## The item model matters more than the arithmetic

Power here is driven almost entirely by how the suite's items behave, so the model is an
explicit choice rather than a default buried in the code:

**deterministic** (the default). Each item either passes every time or fails every time,
and the suite's accuracy is the share of items that pass. The candidate arm converts a
fraction `mdd` of the passing items into failing ones. This is what the recorded suites
in this repo actually look like: gsm8k and codeqa are almost entirely 5/5 or 0/5.
Its defining consequence is that **repeats buy nothing** - every repeat of a
deterministic item returns the same verdict - so only more items help. That is a real
finding about this kind of suite, not a modelling artefact.

**bernoulli**. Every item passes independently with probability `baseline` on every
repeat, and the candidate shifts that to `baseline - mdd`. Here repeats do help, because
averaging repeats sharpens each item's majority verdict. Few real suites look like this;
it is the optimistic end.

**from a receipt** (`--from-receipt`). Item probabilities are the pass rates actually
observed per item in a sealed run. This is the best available estimate of a real suite's
mixture, and it is what you should use when you have a run to hand.

Real suites sit between the two synthetic models, so treat deterministic as the
pessimistic bound and bernoulli as the optimistic one.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .models import RunRecord
from .paired import DEFAULT_ALPHA, exact_mcnemar

DEFAULT_TRIALS = 2000
_SEED = 12345
DETERMINISTIC = "deterministic"
BERNOULLI = "bernoulli"


@dataclass(frozen=True)
class PowerEstimate:
    power: float
    n_items: int
    n_repeats: int
    mdd: float
    baseline: float
    alpha: float
    model: str
    trials: int

    def to_dict(self) -> dict[str, object]:
        return {
            "power": self.power, "n_items": self.n_items,
            "n_repeats": self.n_repeats, "mdd": self.mdd,
            "baseline": self.baseline, "alpha": self.alpha,
            "model": self.model, "trials": self.trials,
        }


def _majority(p: float, n: int, rng: random.Random) -> int:
    """Draw n Bernoulli(p) repeats and return the majority verdict, ties resolved low.

    A tie is only reachable at even n and is treated as a fail, matching nothing in
    `paired.py` - which excludes ties instead. The difference cannot matter at the odd
    repeat counts anyone runs, and modelling an exclusion would understate the item
    count rather than overstate it.
    """
    hits = sum(1 for _ in range(n) if rng.random() < p)
    return 1 if hits * 2 > n else 0


def _deterministic_arms(baseline: float, n_items: int, mdd: float) -> tuple[list, list]:
    """Items that always pass or always fail; `mdd` of the passers flip in the candidate."""
    n_pass = round(baseline * n_items)
    before = [1.0] * n_pass + [0.0] * (n_items - n_pass)
    n_flip = round(mdd * n_items)
    after = list(before)
    flipped = 0
    for i in range(n_items):
        if flipped >= n_flip:
            break
        if after[i] == 1.0:
            after[i] = 0.0
            flipped += 1
    return before, after


def simulate_power(
    probs_before: list[float],
    probs_after: list[float],
    n_repeats: int,
    *,
    alpha: float = DEFAULT_ALPHA,
    trials: int = DEFAULT_TRIALS,
    seed: int = _SEED,
) -> float:
    """Fraction of simulated experiments in which the paired test reaches significance."""
    if not probs_before:
        return 0.0
    rng = random.Random(seed)
    hits = 0
    for _ in range(trials):
        regressed = improved = 0
        for pb, pa in zip(probs_before, probs_after, strict=True):
            vb = _majority(pb, n_repeats, rng)
            va = _majority(pa, n_repeats, rng)
            if vb == 1 and va == 0:
                regressed += 1
            elif vb == 0 and va == 1:
                improved += 1
        if exact_mcnemar(regressed, improved) < alpha:
            hits += 1
    return hits / trials


def min_discordant_items(alpha: float = DEFAULT_ALPHA) -> int:
    """Fewest one-directional discordant items that can reach significance.

    Exact McNemar with all n discordant items moving the same way gives p = 2 * 0.5**n,
    so this is the smallest n with 2 * 0.5**n < alpha: six at alpha=0.05, because
    2/64 = 0.031 and 2/32 = 0.063.

    It is a hard floor on any paired comparison. A suite where fewer than six items can
    move cannot produce a significant result however many times each item is repeated,
    which is the single most useful number in this module.
    """
    n = 1
    while 2.0 * (0.5 ** n) >= alpha:
        n += 1
    return n


def estimate(
    baseline: float,
    n_items: int,
    n_repeats: int,
    mdd: float,
    *,
    model: str = DETERMINISTIC,
    alpha: float = DEFAULT_ALPHA,
    trials: int = DEFAULT_TRIALS,
    seed: int = _SEED,
) -> PowerEstimate:
    """Power of one configuration."""
    if model == DETERMINISTIC:
        before, after = _deterministic_arms(baseline, n_items, mdd)
    elif model == BERNOULLI:
        before = [baseline] * n_items
        after = [max(0.0, min(1.0, baseline - mdd))] * n_items
    else:
        raise ValueError(f"unknown model {model!r}; expected {DETERMINISTIC!r} or {BERNOULLI!r}")

    power = simulate_power(
        before, after, n_repeats, alpha=alpha, trials=trials, seed=seed)
    return PowerEstimate(
        power=round(power, 4), n_items=n_items, n_repeats=n_repeats, mdd=mdd,
        baseline=baseline, alpha=alpha, model=model, trials=trials,
    )


def estimate_from_record(
    record: RunRecord,
    mdd: float,
    n_repeats: int | None = None,
    *,
    alpha: float = DEFAULT_ALPHA,
    trials: int = DEFAULT_TRIALS,
    seed: int = _SEED,
) -> PowerEstimate:
    """Power using the per-item pass rates a real run actually showed.

    The candidate arm lowers each item's pass rate by `mdd`, clipped at zero. This keeps
    the suite's real mixture of easy, hard and flaky items instead of assuming one.

    Note what that shift model implies. Lowering a probability turns an item that always
    passed into one that usually passes, so a suite of deterministic items becomes a
    suite of flaky ones, and more repeats then *reduce* power for the same reason they
    do under `bernoulli`: they sharpen each item back toward a single majority verdict.
    The alternative model - flip a fraction of items outright, leaving the rest
    deterministic - is what `--model deterministic` does. Which one fits depends on
    whether you expect a change to make items unreliable or to make them wrong, and
    those are different failures with different remedies.
    """
    probs = [sum(c.scores) / len(c.scores) for c in record.results if c.scores]
    if not probs:
        raise ValueError("record has no scored cases")
    repeats = n_repeats or record.manifest.run_config.n_repeats
    after = [max(0.0, p - mdd) for p in probs]
    power = simulate_power(probs, after, repeats, alpha=alpha, trials=trials, seed=seed)
    return PowerEstimate(
        power=round(power, 4), n_items=len(probs), n_repeats=repeats, mdd=mdd,
        baseline=round(sum(probs) / len(probs), 4), alpha=alpha,
        model="from_receipt", trials=trials,
    )


def required_items(
    baseline: float,
    n_repeats: int,
    mdd: float,
    target_power: float = 0.8,
    *,
    model: str = DETERMINISTIC,
    alpha: float = DEFAULT_ALPHA,
    trials: int = 400,
    max_items: int = 5000,
    seed: int = _SEED,
) -> int | None:
    """Smallest item count reaching `target_power`, or None within `max_items`.

    Coarse trial count during the search and a doubling-then-bisection walk, because the
    answer only needs to be good to the nearest item or so and each trial is a full
    simulated experiment.
    """
    def power_at(n: int) -> float:
        return estimate(baseline, n, n_repeats, mdd, model=model,
                        alpha=alpha, trials=trials, seed=seed).power

    lo = max(2, min_discordant_items(alpha))
    if power_at(lo) >= target_power:
        return lo
    hi = lo
    while hi <= max_items:
        hi = min(max_items, hi * 2)
        if power_at(hi) >= target_power:
            break
        if hi >= max_items:
            return None
    while lo < hi:
        mid = (lo + hi) // 2
        if power_at(mid) >= target_power:
            hi = mid
        else:
            lo = mid + 1
    return lo


def required_repeats(
    baseline: float,
    n_items: int,
    mdd: float,
    target_power: float = 0.8,
    *,
    model: str = BERNOULLI,
    alpha: float = DEFAULT_ALPHA,
    trials: int = 400,
    max_repeats: int = 201,
    seed: int = _SEED,
) -> int | None:
    """Smallest odd repeat count reaching `target_power`, or None within `max_repeats`.

    Odd only, so an item's majority verdict is always defined. Under the deterministic
    model this returns None for any target it cannot already meet at N=1: repeating a
    deterministic item tells you nothing new, and the honest answer is that repeats are
    the wrong dial.
    """
    for n in range(1, max_repeats + 1, 2):
        got = estimate(baseline, n_items, n, mdd, model=model,
                       alpha=alpha, trials=trials, seed=seed).power
        if got >= target_power:
            return n
    return None


def analytic_deterministic_items(mdd: float, alpha: float = DEFAULT_ALPHA) -> int:
    """Items needed under the deterministic model, in closed form.

    Every flipped item is discordant and all move the same way, so significance needs
    `min_discordant_items` of them, and the simulator flips `round(mdd * n)` items. The
    answer is therefore the smallest n whose rounded flip count reaches that floor.

    At alpha=0.05 and a 5-point shift that is 110 items, not the ceil(6/0.05) = 120 the
    continuous version suggests: round(0.05 * 110) is 6 because 5.5 rounds to even. The
    rounding has to match the simulator's or the two disagree by one item at the
    boundary and the difference looks like a bug. Used to cross-check the simulator,
    which must land on the same answer.
    """
    if mdd <= 0:
        raise ValueError("mdd must be positive")
    needed = min_discordant_items(alpha)
    # The simulator flips `round(mdd * n)` items, so the closed form has to use the same
    # rounding or the two disagree by one item at the boundary and look like a bug.
    n = 1
    while round(mdd * n) < needed:
        n += 1
    return n
