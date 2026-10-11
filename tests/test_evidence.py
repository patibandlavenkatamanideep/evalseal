"""What a run supports, and the discipline of not saying more than that.

The feature's whole value is that it refuses to read a magnitude off five repeats, so
these tests pin the refusal as hard as the arithmetic: `test_no_output_overclaims`
fails if the module starts using the vocabulary of proof, and the label boundaries are
asserted at the exact repeat counts people actually use.
"""
from __future__ import annotations

import json

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.analyze import MIN_RUNS_FOR_STABILITY, wilson_ci
from evalseal.cli import EXIT_BAD_INPUT, app
from evalseal.evidence import (
    CAVEAT,
    DIRECTIONAL,
    EVIDENCE_LABELS,
    INSUFFICIENT,
    MODERATE,
    NOT_BINARY,
    STRONGER,
    analyze_evidence,
    case_evidence,
    classify_evidence,
    flip_rate_interval,
    margin_at,
    recommend,
    render,
    repeats_for_margin,
    z_for_confidence,
)
from evalseal.executor import run_eval
from evalseal.report import write_json

runner = CliRunner()


def _record(*, verdicts: dict[str, list[str]], n_repeats: int):
    """A run whose per-case verdict sequences are exactly what the test wants."""
    cases = list(verdicts)
    target = LocalCallableTarget(scripted(verdicts))
    return run_eval(make_dataset(*cases), target, RegexScorer(r"^yes$"),
                    n_repeats=n_repeats)


def _unanimous(n: int):
    return _record(verdicts={"a": ["yes"]}, n_repeats=n)


# --- the statistics, reused rather than reinvented ------------------------------------

def test_z_matches_the_interval_the_rest_of_the_tool_uses():
    """0.95 must give the same z `analyze` hardcodes, or two intervals would disagree."""
    from evalseal.analyze import _Z95

    assert z_for_confidence(0.95) == pytest.approx(_Z95, abs=1e-12)


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_an_impossible_confidence_is_refused(bad):
    with pytest.raises(ValueError, match="confidence"):
        z_for_confidence(bad)


def test_margin_is_the_half_width_of_the_wilson_interval():
    z = z_for_confidence(0.95)
    lo, hi = wilson_ci(5, 5, z)
    assert margin_at(5, 5, z) == pytest.approx((hi - lo) / 2)


def test_a_wider_interval_means_more_confidence_costs_precision():
    assert margin_at(5, 5, z_for_confidence(0.99)) > margin_at(5, 5, z_for_confidence(0.80))


def test_margin_narrows_as_repeats_grow():
    z = z_for_confidence(0.95)
    margins = [margin_at(n, n, z) for n in (5, 11, 21, 31, 51)]
    assert margins == sorted(margins, reverse=True)


# --- the flip-rate interval is derived, so it cannot contradict the pass interval -----

@pytest.mark.parametrize("pass_ci,expected", [
    ((0.57, 1.00), (0.0, 0.43)),     # unanimous pass: flip could be up to 0.43
    ((0.00, 0.43), (0.0, 0.43)),     # unanimous fail: symmetric
    ((0.38, 0.96), (0.04, 0.50)),    # straddles 0.5, so flip reaches its maximum
])
def test_flip_rate_interval_maps_the_pass_interval(pass_ci, expected):
    assert flip_rate_interval(*pass_ci) == expected


def test_a_flip_rate_interval_never_exceeds_one_half():
    """A binary case cannot flip more than half the time: the minority is the flips."""
    for lo in (0.0, 0.2, 0.5, 0.8):
        for hi in (lo, 0.6, 1.0):
            _, high = flip_rate_interval(lo, hi)
            assert high <= 0.5


def test_the_reported_flip_rate_sits_inside_its_own_interval():
    case = _unanimous(5).results[0]
    ev = case_evidence(case)
    assert ev.flip_rate_ci is not None
    assert ev.flip_rate_ci[0] <= ev.flip_rate <= ev.flip_rate_ci[1]


# --- labels, cut at the repeat counts people actually use ------------------------------

@pytest.mark.parametrize("n,expected", [
    (1, INSUFFICIENT), (3, INSUFFICIENT), (4, INSUFFICIENT),
    (5, DIRECTIONAL), (11, MODERATE), (21, MODERATE), (31, STRONGER),
])
def test_labels_at_real_repeat_counts(n, expected):
    margin = margin_at(n, n, z_for_confidence(0.95))
    assert classify_evidence(n, margin) == expected


def test_below_the_stability_floor_is_always_insufficient():
    """Even a narrow-looking interval cannot rescue four repeats."""
    assert classify_evidence(MIN_RUNS_FOR_STABILITY - 1, 0.01) == INSUFFICIENT


def test_a_float_scorer_is_named_not_binary_rather_than_given_a_number():
    assert classify_evidence(5, None) == NOT_BINARY


def test_a_flaky_case_stays_directional_longer_than_a_unanimous_one():
    """The cases most worth quantifying take the most repeats to quantify."""
    z = z_for_confidence(0.95)
    n = 21
    assert classify_evidence(n, margin_at(n, n, z)) == MODERATE          # unanimous
    assert classify_evidence(n, margin_at(n // 2, n, z)) == DIRECTIONAL  # near 0.5


# --- repeat recommendations: two numbers, both meaningful -------------------------------

def test_the_worst_case_count_exceeds_the_observed_rate_count():
    """Reporting only one of these misleads in a predictable direction."""
    z = z_for_confidence(0.95)
    worst = repeats_for_margin(0.10, z)
    observed = repeats_for_margin(0.10, z, pass_rate=1.0)
    assert worst is not None and observed is not None
    assert worst > observed


def test_a_tighter_margin_never_needs_fewer_repeats():
    z = z_for_confidence(0.95)
    counts = [repeats_for_margin(m, z) for m in (0.30, 0.20, 0.10, 0.05)]
    assert all(c is not None for c in counts)
    assert counts == sorted(counts)


def test_the_recommended_count_actually_reaches_the_margin():
    """The search must return a count that satisfies the thing it was asked for."""
    z = z_for_confidence(0.95)
    for target in (0.30, 0.20, 0.10):
        n = repeats_for_margin(target, z)
        assert n is not None
        assert margin_at(round(0.5 * n), n, z) <= target
        # ...and one fewer must not, or the search overshot.
        if n > 1:
            assert margin_at(round(0.5 * (n - 1)), n - 1, z) > target


def test_an_unreachable_margin_returns_none_rather_than_a_huge_number():
    assert repeats_for_margin(0.0005, z_for_confidence(0.95)) is None


def test_a_non_positive_margin_is_refused():
    with pytest.raises(ValueError, match="target_margin"):
        repeats_for_margin(0.0, z_for_confidence(0.95))


def test_additional_repeats_never_goes_negative():
    """A case already past the target needs zero more, not a negative count."""
    ev = case_evidence(_unanimous(51).results[0], target_margin=0.30)
    assert ev.additional_repeats_at_observed_rate == 0


# --- actions --------------------------------------------------------------------------

@pytest.mark.parametrize("label,flips,expected_fragment", [
    (INSUFFICIENT, 0, "do not gate"),
    (INSUFFICIENT, 1, "inspect"),
    (DIRECTIONAL, 0, "increase repeats"),
    (DIRECTIONAL, 1, "inspect"),
    (STRONGER, 0, "stable enough"),
    (STRONGER, 1, "inspect"),
    (NOT_BINARY, 0, "read the scores directly"),
])
def test_each_label_recommends_one_action(label, flips, expected_fragment):
    assert expected_fragment in recommend(label, flips)


# --- suite level ----------------------------------------------------------------------

def test_the_suite_is_only_as_evidenced_as_its_weakest_case():
    record = _record(verdicts={"a": ["yes"], "b": ["yes"]}, n_repeats=31)
    short = record.results[0]                                  # one interrupted case
    short.scores, short.verdicts = short.scores[:3], short.verdicts[:3]
    short.pass_count = 3
    assert analyze_evidence(record).label == INSUFFICIENT


def test_an_underpowered_suite_says_so_without_quoting_a_magnitude():
    ev = analyze_evidence(_record(verdicts={"a": ["yes", "no"]}, n_repeats=5))
    assert ev.underpowered
    summary = ev.summary()
    assert "direction, not magnitude" in summary
    assert "not worth quoting" in summary


def test_a_well_powered_suite_says_the_rate_can_be_read():
    ev = analyze_evidence(_unanimous(51), target_margin=0.20)
    assert not ev.underpowered
    assert "can be read as rates" in ev.summary()


def test_an_empty_record_does_not_claim_anything():
    record = _unanimous(5)
    record.results = []
    ev = analyze_evidence(record)
    assert ev.label == NOT_BINARY
    assert "nothing to say" in ev.summary()


def test_an_inconsistent_receipt_is_named_rather_than_a_math_error():
    """Found by a test bug: truncating scores without pass_count used to raise
    `math domain error` from inside the Wilson formula, which says nothing."""
    record = _unanimous(5)
    record.results[0].pass_count = 9           # more passes than runs
    with pytest.raises(ValueError) as e:
        analyze_evidence(record)
    message = str(e.value)
    assert "cannot happen" in message
    assert "inconsistent" in message
    assert "9 passes in 5 run(s)" in message


def test_worst_orders_by_widest_interval_first():
    ev = analyze_evidence(_record(
        verdicts={"a": ["yes", "no"], "b": ["yes"]}, n_repeats=5))
    worst = ev.worst()
    assert worst[0].margin is not None and worst[-1].margin is not None
    assert worst[0].margin >= worst[-1].margin


def test_counts_cover_every_label():
    ev = analyze_evidence(_unanimous(5))
    assert set(ev.counts) == set(EVIDENCE_LABELS)
    assert sum(ev.counts.values()) == len(ev.cases)


def test_one_case_can_be_selected():
    record = _record(verdicts={"a": ["yes"], "b": ["yes"]}, n_repeats=5)
    ev = analyze_evidence(record, case_id="c1")
    assert [c.case_id for c in ev.cases] == ["c1"]


def test_an_unknown_case_lists_what_the_record_has():
    with pytest.raises(ValueError) as e:
        analyze_evidence(_unanimous(5), case_id="nope")
    assert "no case 'nope'" in str(e.value)
    assert "c0" in str(e.value)


# --- JSON shape, which CI depends on ---------------------------------------------------

def test_json_shape_is_stable_and_carries_the_caveat():
    payload = analyze_evidence(_unanimous(5)).to_dict()
    assert set(payload) == {
        "confidence", "target_margin", "label", "n_cases", "n_underpowered",
        "min_runs_per_case", "worst_margin", "counts", "summary", "caveat", "cases"}
    assert payload["caveat"] == CAVEAT
    case = payload["cases"][0]
    assert set(case) == {
        "case_id", "n_runs", "flip_count", "flip_rate", "pass_count", "pass_rate",
        "pass_ci", "flip_rate_ci", "margin", "label", "underpowered", "action",
        "repeats_for_target_margin"}
    assert set(case["repeats_for_target_margin"]) == {
        "worst_case", "at_observed_rate", "additional_worst_case",
        "additional_at_observed_rate"}


def test_json_round_trips():
    json.dumps(analyze_evidence(_unanimous(5)).to_dict())


# --- the discipline --------------------------------------------------------------------

def test_no_output_overclaims():
    """The feature exists to avoid overstating five observations."""
    ev = analyze_evidence(_record(verdicts={"a": ["yes", "no"]}, n_repeats=5))
    text = (render(ev) + ev.summary() + CAVEAT + json.dumps(ev.to_dict())).lower()
    for word in ("proves", "proven", "guarantee", "certain", "exact rate",
                 "definitive", "conclusive"):
        assert word not in text, word


def test_the_module_states_the_caveat_wherever_it_reports():
    ev = analyze_evidence(_unanimous(5))
    assert CAVEAT in render(ev)
    assert ev.to_dict()["caveat"] == CAVEAT
    assert "smoke detector" in CAVEAT


def test_rendered_report_leads_with_what_is_not_supported():
    ev = analyze_evidence(_record(verdicts={"a": ["yes", "no"]}, n_repeats=5))
    text = render(ev)
    assert text.index("underpowered") < text.index("Where to spend repeats")


# --- the CLI ---------------------------------------------------------------------------

@pytest.fixture
def receipt(tmp_path):
    record = _record(verdicts={"a": ["yes", "no"], "b": ["yes"]}, n_repeats=5)
    path = tmp_path / "report.json"
    write_json(record, path)
    return path


def test_cli_prints_a_report(receipt):
    result = runner.invoke(app, ["evidence", str(receipt)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "Evidence" in out
    assert "underpowered" in out


def test_cli_json_is_machine_readable(receipt):
    result = runner.invoke(app, ["evidence", str(receipt), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["n_cases"] == 2
    assert payload["confidence"] == 0.95


def test_cli_case_filter(receipt):
    result = runner.invoke(app, ["evidence", str(receipt), "--case", "c0", "--json"])
    assert json.loads(result.stdout)["n_cases"] == 1


def test_cli_unknown_case_exits_cleanly(receipt):
    result = runner.invoke(app, ["evidence", str(receipt), "--case", "nope"])
    assert result.exit_code == EXIT_BAD_INPUT
    assert "Traceback" not in result.output


def test_cli_target_margin_changes_the_recommendation(receipt):
    loose = json.loads(runner.invoke(
        app, ["evidence", str(receipt), "--target-margin", "0.3", "--json"]).stdout)
    tight = json.loads(runner.invoke(
        app, ["evidence", str(receipt), "--target-margin", "0.05", "--json"]).stdout)
    loose_n = loose["cases"][0]["repeats_for_target_margin"]["worst_case"]
    tight_n = tight["cases"][0]["repeats_for_target_margin"]["worst_case"]
    assert tight_n > loose_n


def test_cli_confidence_changes_the_margin(receipt):
    low = json.loads(runner.invoke(
        app, ["evidence", str(receipt), "--confidence", "0.80", "--json"]).stdout)
    high = json.loads(runner.invoke(
        app, ["evidence", str(receipt), "--confidence", "0.99", "--json"]).stdout)
    assert high["worst_margin"] > low["worst_margin"]


def test_cli_writes_a_file(receipt, tmp_path):
    out = tmp_path / "evidence.md"
    result = runner.invoke(app, ["evidence", str(receipt), "--out", str(out)])
    assert result.exit_code == 0
    assert CAVEAT in out.read_text(encoding="utf-8")


def test_cli_reads_a_ledger_as_well_as_a_receipt(tmp_path):
    from evalseal.ledger import seal_and_append

    ledger = tmp_path / "ledger.jsonl"
    seal_and_append(_unanimous(5), ledger, relink=True)
    result = runner.invoke(app, ["evidence", str(ledger), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["n_cases"] == 1


def test_cli_rejects_a_non_receipt_without_a_traceback(tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text('{"hello": "world"}', encoding="utf-8")
    result = runner.invoke(app, ["evidence", str(junk)])
    assert result.exit_code == EXIT_BAD_INPUT
    assert "Traceback" not in result.output
    assert "not a sealed receipt" in " ".join(result.output.split())


def test_help_names_the_other_command_so_the_questions_stay_distinct():
    """`power` and `evidence` answer different questions; the help must say which."""
    result = runner.invoke(app, ["evidence", "--help"])
    out = " ".join(result.output.split())
    assert "evalseal power" in out
    assert "different question" in out


def test_power_is_untouched_and_still_answers_its_own_question():
    result = runner.invoke(app, ["power", "--help"])
    assert result.exit_code == 0
    out = " ".join(result.output.split())
    assert "detect a difference" in out
    assert "--target-margin" not in out      # precision flags belong to `evidence`
