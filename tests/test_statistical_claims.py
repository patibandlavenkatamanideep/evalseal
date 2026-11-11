"""The claim, the interval, the test and the gate have to be about the same thing.

Four findings, each reproduced on the 2.2.0 build first.

**D3-1. The exact test crashed at scale.**

    exact_mcnemar(5, 0)       = 0.0625
    exact_mcnemar(60, 60)     = 1.0
    exact_mcnemar(1000, 1000) raised OverflowError: int too large to convert to float

The binomial tail was summed as exact integers and then multiplied by `0.5 ** n`, which
is 0.0 above n=1074; the int was far outside the float range, so the multiplication
raised. A paired test over a large suite took the gate down with it.

**D3-2. Every interval was labelled 95%.**

    alpha=0.20 (an 80% interval) summary:
      "regression: delta -0.3000, 95% CI [-0.4000, -0.2000], p=0.001"

**D3-3. A regression budget that could not be failed.** Five items, mean 1.000 -> 0.400:

    paired: verdict=inconclusive p=0.25 ci=(-1.0, -0.2) discordant=3
    PASS  drift.max_score_regression: score moved -0.6000 against a budget of 0.050

A 60-point drop, twelve times over budget, green. Five items cannot reach significance
at all - the six-item floor - and the rule needed both over-budget *and* significance to
fail. There was no field that asked for evidence the run was *within* budget.

**D3-4. Power for one metric standing in for the other.** `power --from-receipt` ran a
McNemar simulation over any receipt, including a continuous-scored one that `diff` tests
with a paired permutation instead.
"""
from __future__ import annotations

import math

import pytest

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import RegexScorer, ScoreResult
from evalseal.adapters.target import LocalCallableTarget
from evalseal.diffing import diff_records
from evalseal.executor import run_eval
from evalseal.paired import (
    DEFAULT_ALPHA,
    INCONCLUSIVE,
    REGRESSION,
    PairedComparison,
    compare_runs,
    exact_mcnemar,
)
from evalseal.policy import (
    EVIDENCE_OF_REGRESSION,
    EVIDENCE_WITHIN_BUDGET,
    INSUFFICIENT_EVIDENCE,
    DriftRules,
    Policy,
    _drift_checks,
    budget_state,
)
from evalseal.power import NotBinary, estimate_from_record

# --- the exact test ----------------------------------------------------------------

@pytest.mark.parametrize("regressed,improved,want", [
    (0, 0, 1.0),            # nothing disagreed: not evidence of sameness
    (1, 0, 1.0),            # one item, either way: the most p can be
    (5, 0, 0.0625),         # 2 * 0.5**5
    (6, 0, 0.03125),        # the six-item floor: the first count that can be significant
    (3, 3, 1.0),            # a tie
    (60, 60, 1.0),
    (1000, 1000, 1.0),      # D3-1: this raised OverflowError
    (1000, 999, 1.0),
])
def test_exact_mcnemar_at_every_scale(regressed, improved, want):
    assert exact_mcnemar(regressed, improved) == pytest.approx(want)


def test_exact_mcnemar_matches_a_direct_binomial_computation():
    """An independent oracle: `statistics` binomial, summed as floats, for small n.

    Derived from the definition rather than from the implementation - twice the smaller
    tail of Binomial(n, 1/2), capped at 1 - so it checks the formula and not the code.
    """
    def oracle(r: int, i: int) -> float:
        n = r + i
        if n == 0:
            return 1.0
        k = min(r, i)
        tail = math.fsum(math.comb(n, j) * 0.5 ** n for j in range(k + 1))
        return min(1.0, 2.0 * tail)

    for r in range(0, 14):
        for i in range(0, 14):
            assert exact_mcnemar(r, i) == pytest.approx(oracle(r, i), abs=1e-12), (r, i)


def test_a_huge_one_sided_count_underflows_rather_than_crashing():
    """2000 items all one way: the true p is below double precision, reported as 0.0."""
    assert exact_mcnemar(2000, 0) == 0.0
    # And the decision it drives is the one it should drive.
    assert exact_mcnemar(2000, 0) < DEFAULT_ALPHA


def test_a_tie_carries_no_direction_and_is_excluded_rather_than_broken():
    """An even number of repeats can split; such an item is named, not assigned."""
    before = _arm({"a": "yes", "b": "yes"}, n_repeats=4)
    after = _arm({"a": "yes", "b": "yes"}, n_repeats=4, split="b")
    assert [c.scores for c in after.results if c.case_id == "b"] == [[1.0, 1.0, 0.0, 0.0]]
    result = compare_runs(before, after)
    assert result.ambiguous_items == ["b"]
    assert result.n_discordant == 0
    assert result.p_value == 1.0


# --- the interval's label ----------------------------------------------------------

@pytest.mark.parametrize("alpha,label", [(0.05, "95% CI"), (0.10, "90% CI"),
                                         (0.20, "80% CI"), (0.01, "99% CI")])
def test_the_interval_is_labelled_from_the_alpha_in_force(alpha, label):
    """D3-2. An 80% interval printed as "95% CI" on the baseline."""
    p = PairedComparison(n_items=20, delta=-0.3, ci=(-0.4, -0.2), p_value=0.001,
                         test="exact_mcnemar", verdict=REGRESSION, alpha=alpha)
    assert p.ci_label == label
    assert label in p.summary()
    assert p.to_dict()["ci_label"] == label
    assert p.to_dict()["confidence"] == pytest.approx(1 - alpha)


def test_the_inconclusive_summary_is_labelled_too():
    p = PairedComparison(n_items=5, delta=-0.1, ci=(-0.3, 0.1), p_value=0.5,
                         test="exact_mcnemar", verdict=INCONCLUSIVE, alpha=0.10)
    assert "90% CI" in p.summary() and "95%" not in p.summary()


def test_the_old_ci95_key_and_attribute_still_work():
    """A 2.2.0 consumer is unaffected; it just cannot tell which interval it has."""
    p = PairedComparison(n_items=5, delta=0.0, ci=(-0.1, 0.1), p_value=1.0,
                         test="exact_mcnemar", verdict=INCONCLUSIVE, alpha=0.20)
    assert p.ci95 == p.ci == (-0.1, 0.1)
    payload = p.to_dict()
    assert payload["ci95"] == payload["ci"] == [-0.1, 0.1]


def test_a_configured_confidence_level_reaches_the_interval_itself():
    """Not only the label: the bootstrap percentiles move with alpha."""
    before = _arm({f"c{i}": "yes" for i in range(12)})
    after = _arm({f"c{i}": ("yes" if i < 6 else "no") for i in range(12)},
                 flip=[f"c{i}" for i in range(6, 12)])
    wide = compare_runs(before, after, alpha=0.01)
    narrow = compare_runs(before, after, alpha=0.20)
    assert wide.ci[0] <= narrow.ci[0] and wide.ci[1] >= narrow.ci[1]
    assert wide.alpha == 0.01 and narrow.alpha == 0.20


# --- the two metrics stay apart ----------------------------------------------------

def _arm(answers: dict[str, str], *, n_repeats: int = 5, flip=None, split=None):
    """One case per key.

    `flip` names cases that answer 'no' on their last repeat - one dissent, so the
    majority still stands. `split` names cases that answer 'no' on the whole second
    half, which at an even repeat count is a tie with no majority at all.
    """
    flip = {flip} if isinstance(flip, str) else set(flip or ())
    split = {split} if isinstance(split, str) else set(split or ())
    ds = Dataset([Case(cid, cid) for cid in answers], hash="sha256:test")

    state: dict[str, int] = {}

    def respond(prompt: str) -> str:
        n = state.get(prompt, 0)
        state[prompt] = n + 1
        if prompt in flip and n == n_repeats - 1:
            return "no"
        if prompt in split and n >= n_repeats // 2:
            return "no"
        return answers[prompt]

    return run_eval(ds, LocalCallableTarget(respond, name="model-a"),
                    RegexScorer("^yes$"), n_repeats=n_repeats)


def test_a_shift_that_moves_the_mean_without_moving_majorities_names_both():
    """docs/metrics.md §6 and §7. The two results disagree and both are reported."""
    ids = [f"c{i}" for i in range(10)]
    before = _arm(dict.fromkeys(ids, "yes"))
    after = _arm(dict.fromkeys(ids, "yes"), flip=ids)       # 4/5 instead of 5/5
    result = compare_runs(before, after)
    assert result.delta == pytest.approx(-0.2)
    assert result.n_discordant == 0                          # no majority moved
    assert result.p_value == 1.0                             # the item test saw nothing
    assert result.ci_excludes_zero                           # the mean plainly moved
    assert result.mean_moved_without_majorities
    assert result.verdict == INCONCLUSIVE                    # never out-claims either
    note = result.summary()
    assert "only 0 item(s) changed majority verdict" in note
    assert "More repeats would make this harder to detect" in note


def test_the_rendered_diff_says_which_quantity_the_p_value_is_about():
    ids = [f"c{i}" for i in range(8)]
    from evalseal.report import render_diff
    text = render_diff(diff_records(_arm(dict.fromkeys(ids, "yes")),
                                    _arm(dict.fromkeys(ids, "yes"), flip=ids)))
    assert "how many items changed which way they mostly go" in text
    assert "how far the mean score moved" in text
    assert "items whose majority verdict moved" in text


def test_a_continuous_scorer_says_the_item_counting_test_does_not_apply():
    from evalseal.report import render_diff
    text = render_diff(diff_records(_float_run(0.4), _float_run(0.6)))
    assert "has no majority verdict to change" in text
    assert "paired permutation" in text


class _Float:
    kind = "exact"

    def __init__(self, value: float):
        self.value = value

    def config(self) -> dict:
        return {}

    def score(self, prompt, response_text, expected):
        return ScoreResult(self.value, False, None)


def _float_run(value: float):
    ds = Dataset([Case(f"c{i}", f"q{i}") for i in range(8)], hash="sha256:test")
    return run_eval(ds, LocalCallableTarget(lambda p: "x", name="model-a"),
                    _Float(value), n_repeats=5)


def test_power_refuses_a_continuous_receipt_rather_than_answering_for_another_test():
    """D3-4. The simulated test is not the test `diff` would run on this receipt."""
    with pytest.raises(NotBinary, match="continuous scores"):
        estimate_from_record(_float_run(0.4), mdd=0.05)


def test_power_names_the_metric_it_is_the_power_for():
    binary = _arm({f"c{i}": "yes" for i in range(10)})
    payload = estimate_from_record(binary, mdd=0.05, trials=200).to_dict()
    assert payload["metric"] == "paired majority-verdict change (exact McNemar)"
    assert "mean score" in payload["not_the_power_for"]


# --- the regression budget ---------------------------------------------------------

@pytest.mark.parametrize("verdict,delta,ci,want", [
    (REGRESSION, -0.60, (-0.80, -0.40), EVIDENCE_OF_REGRESSION),
    (REGRESSION, -0.01, (-0.02, -0.005), EVIDENCE_WITHIN_BUDGET),   # significant, tiny
    (INCONCLUSIVE, -0.60, (-1.00, -0.20), INSUFFICIENT_EVIDENCE),
    (INCONCLUSIVE, 0.00, (-0.01, 0.01), EVIDENCE_WITHIN_BUDGET),
    (INCONCLUSIVE, -0.02, (-0.30, 0.10), INSUFFICIENT_EVIDENCE),
])
def test_the_three_budget_states(verdict, delta, ci, want):
    paired = PairedComparison(n_items=20, delta=delta, ci=ci, p_value=0.01,
                              test="exact_mcnemar", verdict=verdict)
    assert budget_state(paired, delta, budget=0.05) == want


def test_no_paired_comparison_is_insufficient_evidence_rather_than_within_budget():
    assert budget_state(None, 0.0, budget=0.05) == INSUFFICIENT_EVIDENCE


def _budget_check(before, after, **rules):
    checks = _drift_checks(
        DriftRules(baseline="x", max_score_regression=0.05, **rules), after, before)
    return next(c for c in checks if c.rule == "drift.max_score_regression")


def test_a_strict_policy_does_not_pass_on_an_inconclusive_result():
    """D3-3, the point of the whole rule. Five items, 1.000 -> 0.400, budget 0.050."""
    ids = [f"c{i}" for i in range(5)]
    before = _arm(dict.fromkeys(ids, "yes"))
    after = _arm({cid: ("yes" if i < 2 else "no") for i, cid in enumerate(ids)})
    assert diff_records(before, after).paired.verdict == INCONCLUSIVE

    lenient = _budget_check(before, after)
    assert lenient.passed                      # unchanged: nothing was established
    assert f"[{INSUFFICIENT_EVIDENCE}]" in lenient.detail
    assert "not that the run is within budget" in lenient.detail
    assert "require_evidence_within_budget" in lenient.detail

    strict = _budget_check(before, after,
                           regression_budget_policy="require_evidence_within_budget")
    assert not strict.passed
    assert "Failing to find a regression is not the same as finding there is none" \
        in strict.detail


def test_a_strict_policy_passes_when_the_interval_rules_out_a_larger_drop():
    ids = [f"c{i}" for i in range(20)]
    before = _arm(dict.fromkeys(ids, "yes"))
    after = _arm(dict.fromkeys(ids, "yes"))
    check = _budget_check(before, after,
                          regression_budget_policy="require_evidence_within_budget")
    assert check.passed and f"[{EVIDENCE_WITHIN_BUDGET}]" in check.detail


def test_evidence_of_a_regression_fails_under_either_policy():
    ids = [f"c{i}" for i in range(20)]
    before = _arm(dict.fromkeys(ids, "yes"))
    after = _arm({cid: ("yes" if i < 4 else "no") for i, cid in enumerate(ids)})
    assert diff_records(before, after).paired.verdict == REGRESSION
    for mode in ("fail_on_evidence_of_regression", "require_evidence_within_budget"):
        check = _budget_check(before, after, regression_budget_policy=mode)
        assert not check.passed, mode
        assert f"[{EVIDENCE_OF_REGRESSION}]" in check.detail


def test_the_budget_detail_names_the_interval_it_quotes():
    ids = [f"c{i}" for i in range(6)]
    before = _arm(dict.fromkeys(ids, "yes"))
    after = _arm(dict.fromkeys(ids, "yes"), flip=ids)
    detail = _budget_check(before, after).detail
    assert "95% CI" in detail
    assert "item(s) changed majority verdict" in detail


def test_a_policy_written_before_the_three_way_rule_keeps_its_meaning():
    """`regression_budget_policy` defaults to the 2.2.0 behaviour."""
    policy = Policy.model_validate(
        {"drift": {"baseline": "b.json", "max_score_regression": 0.02}})
    assert policy.drift.regression_budget_policy == "fail_on_evidence_of_regression"


def test_an_unknown_budget_policy_is_refused_rather_than_ignored():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Policy.model_validate({"drift": {"baseline": "b.json",
                                         "regression_budget_policy": "whatever"}})
