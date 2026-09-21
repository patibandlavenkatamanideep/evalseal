"""Power estimation, cross-checked against closed forms where one exists."""
from __future__ import annotations

import json

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.executor import run_eval
from evalseal.ledger import seal_and_append
from evalseal.power import (
    BERNOULLI,
    DETERMINISTIC,
    analytic_deterministic_items,
    estimate,
    estimate_from_record,
    min_discordant_items,
    required_items,
    required_repeats,
    simulate_power,
)

runner = CliRunner()


def test_six_is_the_smallest_significant_discordant_count():
    """2 * 0.5**6 = 0.03125 < 0.05, and 2 * 0.5**5 = 0.0625 is not."""
    assert min_discordant_items(0.05) == 6
    assert 2 * 0.5 ** 6 < 0.05 <= 2 * 0.5 ** 5


def test_the_floor_moves_with_alpha():
    assert min_discordant_items(0.10) == 5      # 2 * 0.5**5 = 0.0625 < 0.10
    assert min_discordant_items(0.01) == 8      # 2 * 0.5**8 = 0.0078 < 0.01


@pytest.mark.parametrize("mdd", [0.05, 0.10, 0.20, 0.50])
def test_the_simulator_agrees_with_the_closed_form(mdd):
    """Under the deterministic model every flipped item is discordant and they all move
    the same way, so the answer is exactly "enough items that round(mdd*n) reaches the
    discordance floor". The simulation must land on the same number.
    """
    assert required_items(0.9, 5, mdd, 0.8) == analytic_deterministic_items(mdd)


def test_deterministic_power_is_a_step_function_at_the_floor():
    """Six flipped items is significant; five is not, however large the suite."""
    # 0.20 * 30 = 6 flips -> significant in every trial.
    assert estimate(0.9, 30, 5, 0.20, model=DETERMINISTIC).power == 1.0
    # 0.20 * 25 = 5 flips -> never significant.
    assert estimate(0.9, 25, 5, 0.20, model=DETERMINISTIC).power == 0.0


def test_repeats_do_nothing_under_the_deterministic_model():
    """An item that always gives the same verdict gives it N times. That is the point:
    when a suite's items are effectively deterministic, only more items help."""
    powers = {
        n: estimate(0.9, 60, n, 0.10, model=DETERMINISTIC).power for n in (1, 5, 21)
    }
    assert len(set(powers.values())) == 1, powers


def test_repeats_help_only_when_the_shift_crosses_the_majority_boundary():
    """Baseline 0.55 dropping to 0.45 moves items across the 50% line, so sharpening
    each item's majority verdict with more repeats recovers the signal."""
    low = estimate(0.55, 200, 1, 0.10, model=BERNOULLI).power
    high = estimate(0.55, 200, 11, 0.10, model=BERNOULLI).power
    assert low < 0.6 < high


def test_repeats_actively_hurt_when_the_shift_stays_off_the_boundary():
    """0.90 -> 0.85 never changes which way an item mostly goes.

    More repeats push both arms to the same majority and remove the discordance the
    test needs, so power falls. This is the finding that makes "just raise N" bad advice.
    """
    few = estimate(0.9, 40, 1, 0.05, model=BERNOULLI).power
    many = estimate(0.9, 40, 21, 0.05, model=BERNOULLI).power
    assert many < few


def test_power_rises_with_the_size_of_the_difference():
    powers = [estimate(0.9, 60, 5, m, model=DETERMINISTIC).power
              for m in (0.02, 0.10, 0.30)]
    assert powers == sorted(powers)


def test_simulation_is_deterministic():
    a = estimate(0.9, 40, 5, 0.1, model=BERNOULLI, trials=300)
    b = estimate(0.9, 40, 5, 0.1, model=BERNOULLI, trials=300)
    assert a.power == b.power


def test_an_empty_suite_has_no_power():
    assert simulate_power([], [], 5) == 0.0


def test_unknown_model_is_rejected():
    with pytest.raises(ValueError, match="unknown model"):
        estimate(0.9, 40, 5, 0.05, model="wishful")


def test_required_repeats_gives_up_rather_than_searching_forever():
    """A difference no repeat count can reach must return None, not a huge number."""
    assert required_repeats(0.9, 4, 0.01, 0.8, model=BERNOULLI, max_repeats=11) is None


def test_required_items_returns_none_past_the_search_limit():
    assert required_items(0.9, 5, 0.001, 0.8, max_items=200) is None


# --- from a real receipt ----------------------------------------------------------

def _record(**scripts):
    target = LocalCallableTarget(scripted(scripts))
    return run_eval(make_dataset(*scripts), target, RegexScorer(r"^yes$"), n_repeats=5)


def test_estimate_from_a_receipt_uses_the_observed_item_rates():
    record = _record(**{f"i{k}": ["yes"] for k in range(10)})
    est = estimate_from_record(record, 0.5)
    assert est.model == "from_receipt"
    assert est.n_items == 10
    assert est.baseline == 1.0          # every item passed every time


def test_a_receipt_with_no_cases_is_an_error():
    record = _record(a=["yes"])
    record.results = []
    with pytest.raises(ValueError, match="no scored cases"):
        estimate_from_record(record, 0.1)


def test_the_receipt_model_makes_items_flaky_so_repeats_still_hurt():
    """A probability shift is a different failure from a verdict flip.

    Lowering a 5/5 item's rate to 0.7 does not make it fail; it makes it unreliable.
    Repeats then sharpen it back toward "mostly passes", which removes the discordance
    McNemar needs - the same effect as the bernoulli model, reached from a real suite.
    The `deterministic` model covers the other failure, where items become wrong rather
    than unreliable.
    """
    record = _record(**{f"i{k}": ["yes"] for k in range(20)})
    at5 = estimate_from_record(record, 0.3, n_repeats=5).power
    at15 = estimate_from_record(record, 0.3, n_repeats=15).power
    assert at15 < at5


def test_a_shift_large_enough_to_fail_items_outright_is_detected_from_a_receipt():
    """mdd=1.0 drives every item's rate to zero, which is a verdict flip, not flakiness.

    20 items all flipping is far past the six-item floor, so power is total.
    """
    record = _record(**{f"i{k}": ["yes"] for k in range(20)})
    assert estimate_from_record(record, 1.0, n_repeats=5, trials=300).power == 1.0


# --- the CLI ----------------------------------------------------------------------

def test_cli_reports_power_and_what_it_would_take():
    result = runner.invoke(app, ["power", "--baseline", "0.975", "--items", "40",
                                 "--repeats", "5", "--mdd", "0.05"])
    assert result.exit_code == 0, result.output
    assert "Power 0.0%" in result.output
    assert "at least 6 items" in result.output
    assert "110 items" in result.output
    assert "Adding repeats does not help here" in result.output


def test_cli_json_carries_every_number():
    result = runner.invoke(app, ["power", "--items", "40", "--mdd", "0.1",
                                 "--trials", "300", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["min_discordant_items"] == 6
    assert payload["analytic_items_deterministic"] == 55
    assert payload["required_items"] == 55
    assert 0.0 <= payload["power"] <= 1.0


def test_cli_can_read_a_receipt_from_the_ledger(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_record(**{f"i{k}": ["yes"] for k in range(12)}), ledger)
    result = runner.invoke(app, ["power", "--from-receipt", "-1", "--ledger", str(ledger),
                                 "--mdd", "0.5", "--trials", "300"])
    assert result.exit_code == 0, result.output
    assert "from_receipt" in result.output


def test_cli_rejects_an_unknown_model():
    result = runner.invoke(app, ["power", "--model", "wishful"])
    assert result.exit_code == 2
    assert "unknown model" in result.output
