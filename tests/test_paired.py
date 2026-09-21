"""Paired comparison of two runs, against values computed by hand.

Every expected number here is derived on paper in the docstring or comment beside it,
so a regression in the statistics shows up as a disagreement with arithmetic rather
than with whatever the code happened to print the day the test was written.
"""
from __future__ import annotations

import math

import pytest
from conftest import scripted

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.executor import run_eval
from evalseal.paired import (
    DEFAULT_ALPHA,
    IMPROVEMENT,
    INCONCLUSIVE,
    REGRESSION,
    compare_runs,
    exact_mcnemar,
    is_binary,
    paired_bootstrap_ci,
    paired_permutation_p,
    per_case_halfwidth,
)


def _run(scripts: dict[str, list[str]], n_repeats: int = 5):
    """Case ids are the prompts, so a changed item set is visible as a changed id set."""
    target = LocalCallableTarget(scripted(scripts))
    ds = Dataset([Case(p, p) for p in scripts], hash="sha256:" + "-".join(sorted(scripts)))
    return run_eval(ds, target, RegexScorer(r"^yes$"), n_repeats=n_repeats)


def _always(ids, answer="yes"):
    return {i: [answer] for i in ids}


# --- exact McNemar, by hand -------------------------------------------------------

def test_mcnemar_with_no_discordant_items_is_one():
    """Nothing disagreed, so the test has nothing to go on. p=1, not "the same"."""
    assert exact_mcnemar(0, 0) == 1.0


def test_mcnemar_one_discordant_item_cannot_reach_significance():
    """n=1: 2 * P(X <= 0) = 2 * C(1,0) * 0.5**1 = 2 * 0.5 = 1.0.

    One item moving is never evidence, whatever the suite size.
    """
    assert exact_mcnemar(1, 0) == 1.0
    assert exact_mcnemar(0, 1) == 1.0


def test_mcnemar_five_in_one_direction():
    """n=5, k=0: 2 * C(5,0) * 0.5**5 = 2/32 = 0.0625. Still above alpha=0.05."""
    assert exact_mcnemar(5, 0) == pytest.approx(0.0625)
    assert exact_mcnemar(5, 0) > DEFAULT_ALPHA


def test_mcnemar_six_in_one_direction_is_the_smallest_significant_case():
    """n=6, k=0: 2 * 0.5**6 = 2/64 = 0.03125, the first count below 0.05."""
    assert exact_mcnemar(6, 0) == pytest.approx(0.03125)
    assert exact_mcnemar(6, 0) < DEFAULT_ALPHA


def test_mcnemar_fifteen_in_one_direction():
    """n=15, k=0: 2 * 0.5**15 = 2/32768 = 6.103515625e-05."""
    assert exact_mcnemar(15, 0) == pytest.approx(2 / 32768)


def test_mcnemar_is_symmetric_and_balanced_discord_is_never_significant():
    """b=c means the two directions cancel; the two-sided p is capped at 1."""
    assert exact_mcnemar(4, 7) == exact_mcnemar(7, 4)
    for k in range(1, 12):
        assert exact_mcnemar(k, k) == 1.0


def test_mcnemar_mixed_directions_by_hand():
    """b=8, c=2, n=10, k=2: 2 * (C(10,0)+C(10,1)+C(10,2)) * 0.5**10
    = 2 * (1 + 10 + 45) / 1024 = 112/1024 = 0.109375."""
    assert exact_mcnemar(8, 2) == pytest.approx(112 / 1024)


# --- the four scenarios the brief names -------------------------------------------

def test_identical_runs_are_inconclusive_not_identical():
    """Same scripts both sides: delta 0, no discordant items, p=1.

    The verdict must not read as "no difference": identical observed behaviour at N=5
    is consistent with a real difference the experiment is too small to see.
    """
    ids = [f"i{k:02d}" for k in range(10)]
    result = compare_runs(_run(_always(ids)), _run(_always(ids)))

    assert result.n_items == 10
    assert result.delta == 0.0
    assert result.ci95 == (0.0, 0.0)
    assert result.p_value == 1.0
    assert result.n_discordant == 0
    assert result.verdict == INCONCLUSIVE
    assert "inconclusive at this N" in result.summary()
    assert "Not evidence the runs are the same" in result.summary()


def test_one_item_changed_is_inconclusive():
    """10 items, one flips pass -> fail. delta = -1/10 = -0.1 exactly.

    Discordant counts are b=1, c=0, so the exact McNemar p is 1.0: a single item can
    never reach significance, however large the suite.
    """
    ids = [f"i{k:02d}" for k in range(10)]
    before = _run(_always(ids))
    after = _run({**_always(ids), "i00": ["no"]})

    result = compare_runs(before, after)
    assert result.delta == pytest.approx(-0.1)
    assert (result.discordant_regressed, result.discordant_improved) == (1, 0)
    assert result.p_value == 1.0
    assert result.verdict == INCONCLUSIVE


def test_large_systematic_shift_is_a_regression():
    """20 items; 15 of them go from 5/5 pass to 5/5 fail.

    delta = -15/20 = -0.75 exactly.
    Discordant b=15, c=0, so p = 2 * 0.5**15 = 6.103515625e-05.
    The CI must exclude zero, and the verdict must name the direction.
    """
    ids = [f"i{k:02d}" for k in range(20)]
    before = _run(_always(ids))
    after = _run({**_always(ids), **{i: ["no"] for i in ids[:15]}})

    result = compare_runs(before, after)
    assert result.delta == pytest.approx(-0.75)
    assert (result.discordant_regressed, result.discordant_improved) == (15, 0)
    assert result.p_value == pytest.approx(2 / 32768)
    assert result.ci95[1] < 0, result.ci95
    assert result.significant
    assert result.verdict == REGRESSION
    assert "regression" in result.summary()


def test_a_large_shift_the_other_way_is_an_improvement():
    ids = [f"i{k:02d}" for k in range(20)]
    before = _run({**_always(ids), **{i: ["no"] for i in ids[:15]}})
    after = _run(_always(ids))

    result = compare_runs(before, after)
    assert result.delta == pytest.approx(0.75)
    assert (result.discordant_regressed, result.discordant_improved) == (0, 15)
    assert result.verdict == IMPROVEMENT


def test_small_n_must_be_inconclusive_even_when_everything_moved():
    """4 items, every one of them flips pass -> fail. delta = -1.0, the largest shift
    a suite can show, and it is still not significant.

    b=4, c=0, so p = 2 * 0.5**4 = 0.125 > 0.05. Four items cannot reach significance
    under an exact paired test no matter how cleanly they move, and the tool has to say
    so rather than calling a total collapse a result.
    """
    ids = [f"i{k}" for k in range(4)]
    before = _run(_always(ids))
    after = _run(_always(ids, answer="no"))

    result = compare_runs(before, after)
    assert result.delta == pytest.approx(-1.0)
    assert result.n_discordant == 4
    assert result.p_value == pytest.approx(0.125)
    assert result.verdict == INCONCLUSIVE
    assert "inconclusive at this N" in result.summary()


def test_six_cleanly_moved_items_do_reach_significance():
    """The boundary from the other side: 6 items all moving gives p = 0.03125."""
    ids = [f"i{k}" for k in range(6)]
    result = compare_runs(_run(_always(ids)), _run(_always(ids, answer="no")))
    assert result.p_value == pytest.approx(0.03125)
    assert result.verdict == REGRESSION


# --- the bootstrap ----------------------------------------------------------------

def test_bootstrap_of_identical_items_has_zero_width():
    """Every item difference is 0, so every resample averages 0."""
    pairs = [([1.0] * 5, [1.0] * 5) for _ in range(10)]
    assert paired_bootstrap_ci(pairs) == (0.0, 0.0)


def test_bootstrap_of_a_constant_shift_has_zero_width_at_that_shift():
    """Every item moved by exactly -1, so resampling items cannot produce anything else.

    This is the cluster bootstrap behaving correctly: the uncertainty it reports is
    uncertainty about which items are in the suite, and here the items all agree.
    """
    pairs = [([1.0] * 5, [0.0] * 5) for _ in range(10)]
    assert paired_bootstrap_ci(pairs) == (-1.0, -1.0)


def test_bootstrap_brackets_the_observed_delta_when_items_disagree():
    pairs = [([1.0] * 5, [1.0] * 5) for _ in range(8)]
    pairs += [([1.0] * 5, [0.0] * 5) for _ in range(2)]
    lo, hi = paired_bootstrap_ci(pairs)
    assert lo <= -0.2 <= hi          # observed delta is -2/10
    assert lo < hi                   # items disagree, so the interval has width


def test_bootstrap_is_deterministic():
    """A reported interval has to be reproducible from the same record."""
    pairs = [([1.0] * 5, [1.0, 0.0, 1.0, 1.0, 0.0]) for _ in range(12)]
    pairs += [([0.0] * 5, [1.0] * 5) for _ in range(6)]
    assert paired_bootstrap_ci(pairs) == paired_bootstrap_ci(pairs)


def test_bootstrap_keeps_an_items_repeats_together():
    """Item-level means are what get resampled, so within-item order cannot matter."""
    a = [([1.0, 0.0, 1.0, 0.0, 1.0], [0.0] * 5) for _ in range(9)]
    b = [([1.0, 1.0, 1.0, 0.0, 0.0], [0.0] * 5) for _ in range(9)]
    assert paired_bootstrap_ci(a) == paired_bootstrap_ci(b)


# --- the permutation test ---------------------------------------------------------

def test_permutation_of_all_zero_differences_is_one():
    assert paired_permutation_p([0.0] * 8) == 1.0


def test_permutation_is_exact_for_a_small_item_count():
    """4 items all shifted the same way. Only the all-same and all-opposite sign
    patterns reach the observed |mean|, so p = 2/16 = 0.125 - the same value the exact
    McNemar test gives for 4 cleanly moved binary items, as it should be.
    """
    assert paired_permutation_p([-0.5] * 4) == pytest.approx(2 / 16)


def test_permutation_of_six_equal_differences():
    """6 items: 2/64 = 0.03125, matching exact McNemar at n=6."""
    assert paired_permutation_p([-0.25] * 6) == pytest.approx(2 / 64)


def test_permutation_never_returns_zero():
    """The observed assignment is one of the permutations, so p has a floor."""
    p = paired_permutation_p([-0.4] * 12)
    assert p > 0
    assert p == pytest.approx(2 / 2**12)


def test_float_scores_use_the_permutation_test():
    before = _run(_always([f"i{k}" for k in range(6)]))
    after = _run(_always([f"i{k}" for k in range(6)]))
    # Rewrite the sealed scores to non-binary values, as a float scorer would produce.
    for case in after.results:
        case.scores = [0.4] * 5
    for case in before.results:
        case.scores = [0.9] * 5

    assert not is_binary(before)
    result = compare_runs(before, after)
    assert result.test == "paired_permutation"
    assert result.delta == pytest.approx(-0.5)
    assert result.p_value == pytest.approx(2 / 64)
    assert result.verdict == REGRESSION


# --- item sets and edge cases -----------------------------------------------------

def test_items_not_in_both_runs_are_excluded_and_named():
    before = _run(_always(["a", "b", "c"]))
    after = _run(_always(["b", "c", "d"]))
    result = compare_runs(before, after)

    assert result.n_items == 2                     # only b and c are paired
    assert result.items_only_in_before == ["a"]
    assert result.items_only_in_after == ["d"]


def test_no_shared_items_is_inconclusive_rather_than_an_error():
    result = compare_runs(_run(_always(["a"])), _run(_always(["b"])))
    assert result.n_items == 0
    assert result.verdict == INCONCLUSIVE
    assert result.p_value == 1.0


def test_an_evenly_split_item_is_ambiguous_not_silently_assigned():
    """With an even repeat count a 2/2 item has no majority, so it carries no direction.

    Breaking the tie with a rule would invent a verdict the data does not contain.
    """
    ids = ["a", "b", "c", "d"]
    before = _run(_always(ids), n_repeats=4)
    after = _run({**_always(ids), "a": ["yes", "no"]}, n_repeats=4)

    result = compare_runs(before, after)
    assert result.ambiguous_items == ["a"]
    assert result.n_discordant == 0


def test_per_case_halfwidth_is_preserved_under_its_real_name():
    """The pre-2.0 "noise floor": widest per-case Wilson half-width.

    At 5/5 Wilson gives [0.5655, 1.0], a half-width of 0.2172 - a statement about one
    item at N=5, which is exactly why it was the wrong threshold for a suite mean.
    """
    record = _run(_always(["a", "b"]))
    assert per_case_halfwidth(record) == pytest.approx(0.2172, abs=5e-4)
    assert compare_runs(record, record).per_case_halfwidth == pytest.approx(0.2172, abs=5e-4)


def test_the_wide_per_case_halfwidth_no_longer_suppresses_a_real_shift():
    """The regression this whole module exists to fix.

    15 of 20 items collapse - delta -0.75, p = 6.1e-05 - while the per-case half-width
    is 0.217. The old rule compared |delta| against that half-width, and any shift
    smaller than 0.217 was reported as "within noise". The paired test calls this what
    it is.
    """
    ids = [f"i{k:02d}" for k in range(20)]
    result = compare_runs(
        _run(_always(ids)), _run({**_always(ids), **{i: ["no"] for i in ids[:15]}}))
    assert result.per_case_halfwidth == pytest.approx(0.2172, abs=5e-4)
    assert result.verdict == REGRESSION


def test_a_shift_smaller_than_the_old_noise_floor_can_now_be_detected():
    """40 items, 8 of them regress: delta = -0.2, well under the 0.217 half-width that
    used to be the threshold.

    b=8, c=0 gives p = 2 * 0.5**8 = 2/256 = 0.0078125, comfortably significant.
    """
    ids = [f"i{k:02d}" for k in range(40)]
    result = compare_runs(
        _run(_always(ids)), _run({**_always(ids), **{i: ["no"] for i in ids[:8]}}))

    assert result.delta == pytest.approx(-0.2)
    assert abs(result.delta) < result.per_case_halfwidth      # the old rule said "noise"
    assert result.p_value == pytest.approx(2 / 256)
    assert result.verdict == REGRESSION


def test_mcnemar_matches_scipy_style_enumeration():
    """Cross-check the closed form against a direct sum over the binomial pmf."""
    for b, c in [(0, 0), (1, 0), (3, 1), (7, 7), (9, 2), (12, 5)]:
        n = b + c
        if n == 0:
            continue
        k = min(b, c)
        expected = min(1.0, 2.0 * sum(
            math.comb(n, i) * 0.5 ** n for i in range(k + 1)))
        assert exact_mcnemar(b, c) == pytest.approx(expected)
