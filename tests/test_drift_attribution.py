"""What may be inferred about *why* two runs differ, and what may not.

`drift.kind` is the only field in the product that names a cause. Each label is
admissible only on the grounds docs/metrics.md §10 tabulates, and this file is one test
per row of that table plus one per way of getting it wrong.

Two findings, both reproduced on the 2.2.0 build first.

**D2-1. A changed sampling setting reported as judge variance.** Two runs of one model,
one at temperature 0.0 and one at 1.0, graded by the same judge:

    kind       : judge_variance
    comparable : True
    summary    : "Judge variance: the grading setup is byte-identical and the target
                  model is unchanged, yet 1 of 1 shared case(s) came back with a
                  different verdict..."

The target model *name* was unchanged. What the target did was not, and the report
attributed the difference to the judge.

**D2-2. A continuous scorer's drift invisible.** `verdict_changed` compared `pass_count`
with `fail_count`, both of which are 0 when there are no verdicts:

    mean score moved 0.10 -> 0.90
    kind    : stable
    summary : "No drift: the grading setup is identical and all 2 shared case(s)
               returned the same majority verdict."
"""
from __future__ import annotations

import itertools

import pytest
from conftest import make_dataset

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import LLMJudgeScorer, RegexScorer, ScoreResult
from evalseal.adapters.target import LocalCallableTarget
from evalseal.drift import (
    CANNOT_ISOLATE,
    EVALUATOR_DRIFT,
    JUDGE_VARIANCE,
    STABLE,
    TARGET_CHANGE,
    TARGET_VARIANCE,
    analyze_drift,
    render,
)
from evalseal.executor import run_eval

RUBRIC = "Be strict."


def _judged(verdicts, *, rubric=RUBRIC, answer="an answer", target="model-a"):
    """One case, five repeats, a judge cycling through `verdicts`."""
    cycle = itertools.cycle(list(verdicts))
    judge = LocalCallableTarget(lambda p: next(cycle), name="judge-a")
    return run_eval(make_dataset("q"),
                    LocalCallableTarget(lambda p: answer, name=target),
                    LLMJudgeScorer(judge=judge, rubric=rubric), n_repeats=5)


def _regex(answers, target="model-a", pattern="^yes$"):
    cycle = itertools.cycle(list(answers))
    return run_eval(make_dataset("q"),
                    LocalCallableTarget(lambda p: next(cycle), name=target),
                    RegexScorer(pattern), n_repeats=5)


# --- the four scenarios the brief names ---------------------------------------------

def test_the_same_model_at_a_different_temperature_cannot_be_blamed_on_the_judge():
    """D2-1. Same model name, different sampling, so no cause may be named."""
    before, after = _judged(["PASS"]), _judged(["PASS", "FAIL", "FAIL", "FAIL", "FAIL"])
    before.manifest.target.effective_params.temperature = 0.0
    after.manifest.target.effective_params.temperature = 1.0

    report = analyze_drift(before, after)
    assert report.comparable                      # the evaluator did not change
    assert report.kind == CANNOT_ISOLATE
    assert [c.name for c in report.target_changes] == ["target temperature"]
    assert "Cannot isolate the cause" in report.summary()
    assert "target temperature differs" in report.summary()
    # And it says what would settle it, rather than guessing.
    assert "evalseal decompose" in report.summary()
    assert "judge" not in report.kind


@pytest.mark.parametrize("field,before_value,after_value", [
    ("top_p", 1.0, 0.8),
    ("max_tokens", 1024, 256),
    ("seed", 7, 8),
])
def test_any_changed_sampling_parameter_blocks_attribution(field, before_value,
                                                           after_value):
    before, after = _judged(["PASS"]), _judged(["PASS", "FAIL", "FAIL", "FAIL", "FAIL"])
    setattr(before.manifest.target.effective_params, field, before_value)
    setattr(after.manifest.target.effective_params, field, after_value)
    assert analyze_drift(before, after).kind == CANNOT_ISOLATE


@pytest.mark.parametrize("field,value", [
    ("base_url", "https://proxy.internal/v1"),
    ("api_version", "2026-01-01"),
    ("system_prompt_hash", "sha256:deadbeef"),
    ("provider", "anthropic"),
])
def test_a_changed_endpoint_api_version_or_system_prompt_blocks_attribution(field, value):
    """Each of these changes what the target did without changing its name."""
    before, after = _judged(["PASS"]), _judged(["PASS", "FAIL", "FAIL", "FAIL", "FAIL"])
    setattr(after.manifest.target, field, value)
    report = analyze_drift(before, after)
    assert report.kind == CANNOT_ISOLATE
    assert report.target_changes


def test_a_changed_evaluator_is_evaluator_drift_and_nothing_else():
    before, after = _judged(["PASS"]), _judged(["FAIL"], rubric="Be lenient.")
    report = analyze_drift(before, after)
    assert report.kind == EVALUATOR_DRIFT
    assert not report.comparable
    assert report.grounds == ["the evaluator fingerprint differs"]
    assert "none of that can be attributed to the model under test" in report.summary()


def test_a_fixed_target_response_graded_repeatedly_is_judge_variance():
    """The judge's own scenario: one unchanging response, a judge that wavers."""
    before = _judged(["PASS"])
    after = _judged(["FAIL", "FAIL", "FAIL", "PASS", "PASS"])
    report = analyze_drift(before, after)
    assert report.kind == JUDGE_VARIANCE
    assert not report.target_changes
    assert "every recorded target field is identical" in report.grounds
    assert "the scorer is an LLM judge, which samples" in report.grounds
    # It still refuses to claim the target could not also have varied.
    assert "cannot be separated from the target answering differently" in report.summary()


def test_a_deterministic_grader_leaves_only_the_target():
    before, after = _regex(["yes"]), _regex(["yes", "no", "no", "no", "no"])
    report = analyze_drift(before, after)
    assert report.kind == TARGET_VARIANCE
    assert "the scorer is deterministic, so it cannot disagree with itself" in report.grounds


def test_a_different_model_is_a_comparison_rather_than_drift():
    before, after = _regex(["yes"]), _regex(["no"], target="model-b")
    report = analyze_drift(before, after)
    assert report.kind == TARGET_CHANGE
    assert "the requested target model differs" in report.grounds
    assert "evalseal diff" in report.summary()


def test_a_different_model_on_a_different_endpoint_is_still_a_comparison():
    """Two models are not required to share an endpoint, so this is not ambiguous."""
    before, after = _regex(["yes"]), _regex(["no"], target="model-b")
    after.manifest.target.base_url = "https://other.example/v1"
    assert analyze_drift(before, after).kind == TARGET_CHANGE


# --- continuous scores ---------------------------------------------------------------

class _Float:
    """A scorer that returns a fixed float, so there is no majority verdict at all."""
    kind = "exact"

    def __init__(self, value: float):
        self.value = value

    def config(self) -> dict:
        return {}

    def score(self, prompt, response_text, expected):
        return ScoreResult(self.value, False, None)


def _float_run(value: float, target: str = "model-a"):
    ds = Dataset([Case("a", "q1"), Case("b", "q2")], hash="sha256:test")
    return run_eval(ds, LocalCallableTarget(lambda p: "x", name=target),
                    _Float(value), n_repeats=5)


def test_a_continuous_score_that_moved_is_not_reported_as_no_drift():
    """D2-2. The headline: 0.10 -> 0.90 reported as `stable`, "No drift"."""
    before, after = _float_run(0.1), _float_run(0.9)
    assert (before.aggregate.mean_score, after.aggregate.mean_score) == (0.1, 0.9)

    report = analyze_drift(before, after)
    assert report.kind == TARGET_VARIANCE
    assert report.kind != STABLE
    # No majority verdict moved, because a continuous scorer has none to move.
    assert report.verdict_changes == []
    assert len(report.score_changes) == 2
    assert len(report.moved_cases) == 2
    assert all(c.mean_delta == pytest.approx(0.8) for c in report.score_changes)


def test_a_continuous_score_that_did_not_move_is_still_stable():
    report = analyze_drift(_float_run(0.4), _float_run(0.4))
    assert report.kind == STABLE
    assert report.score_changes == []
    assert "the same mean score" in report.summary()


def test_a_continuous_score_move_with_a_changed_sampling_setting_cannot_be_isolated():
    before, after = _float_run(0.1), _float_run(0.9)
    after.manifest.target.effective_params.top_p = 0.5
    before.manifest.target.effective_params.top_p = 1.0
    assert analyze_drift(before, after).kind == CANNOT_ISOLATE


def test_the_rendered_report_counts_verdict_moves_and_score_moves_separately():
    text = render(analyze_drift(_float_run(0.1), _float_run(0.9)))
    assert "cases with a changed majority verdict | 0 of 2" in text
    assert "cases with a changed mean score | 2 of 2" in text
    assert "Why this label is admissible:" in text


def test_a_binary_shift_that_moves_no_majority_is_still_seen():
    """The §7 blind spot, through drift: every case's pass rate drops without flipping.

    Four of five repeats pass before and three of five after, so both majorities stay
    PASS while every case mean falls by 0.2. A report built on majorities alone calls
    that no drift; the mean comparison is what sees it.
    """
    before = _regex(["yes", "yes", "yes", "yes", "no"])
    after = _regex(["yes", "yes", "yes", "no", "no"])
    report = analyze_drift(before, after)
    assert report.verdict_changes == []
    assert len(report.score_changes) == 1
    assert report.kind == TARGET_VARIANCE
    assert report.score_changes[0].mean_delta == pytest.approx(-0.2)


# --- observations are kept apart from inferences -------------------------------------

def test_the_grounds_are_reported_so_the_label_can_be_checked():
    report = analyze_drift(_judged(["PASS"]), _judged(["PASS", "FAIL", "PASS", "PASS",
                                                       "PASS"]))
    payload = report.to_dict()
    assert payload["kind"] == JUDGE_VARIANCE
    assert payload["grounds"] == report.grounds
    # The counts are observations and carry no attribution.
    assert payload["n_verdict_changes"] == 0
    assert payload["n_score_changes"] == 1
    assert payload["n_moved_cases"] == 1


def test_a_mixed_origin_comparison_says_the_two_are_not_the_same_evidence():
    before, after = _regex(["yes"]), _regex(["yes"])
    after.manifest.execution.origin = "replay"
    report = analyze_drift(before, after)
    assert any("different execution origins" in n for n in report.notes)
    assert any("not a fresh measurement" in n for n in report.notes)
    assert "> These runs have different execution origins" in render(report)


def test_a_pre_1_7_receipt_is_reported_as_not_saying_where_its_numbers_came_from():
    before, after = _regex(["yes"]), _regex(["yes"])
    before.manifest.execution = None
    assert any("sealed before schema 1.7" in n for n in report_notes(before, after))


def report_notes(before, after):
    return analyze_drift(before, after).notes


def test_ungraded_observations_are_named_in_the_drift_notes():
    """A case mean that moved may be gradeability rather than behaviour."""
    before = _regex(["yes"])
    after = _regex(["yes"])
    after.aggregate.n_unscorable = 3
    note = next(n for n in analyze_drift(before, after).notes if "Ungraded" in n)
    assert "0 in the earlier run and 3 in the later one" in note
