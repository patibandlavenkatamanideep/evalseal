"""Judge drift: naming what moved between two runs, and refusing to name what cannot be.

The labels carry the weight here. Calling evaluator drift a model change would be the
worst thing this tool could do, so each label has a test that pins what it means.
"""
from __future__ import annotations

import json

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import LLMJudgeScorer, RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.drift import (
    EVALUATOR_DRIFT,
    JUDGE_VARIANCE,
    STABLE,
    TARGET_CHANGE,
    TARGET_VARIANCE,
    analyze_drift,
    render,
)
from evalseal.executor import run_eval
from evalseal.ledger import seal_and_append

runner = CliRunner()


def _judged(*, rubric="Be strict.", verdicts=("PASS",), target="model-a", answer="an answer"):
    judge = LocalCallableTarget(scripted({_prompt(rubric, answer): list(verdicts)}),
                                name="judge-a")
    return run_eval(make_dataset("q"), LocalCallableTarget(lambda p: answer, name=target),
                    LLMJudgeScorer(judge=judge, rubric=rubric), n_repeats=5)


def _prompt(rubric: str, answer: str) -> str:
    return (f"{rubric}\n\nUSER PROMPT:\nq\n\nRESPONSE TO GRADE:\n{answer}\n\n"
            "Answer with exactly one word: PASS or FAIL.")


def _regex(answers, target="model-a"):
    return run_eval(make_dataset("q"), LocalCallableTarget(scripted({"q": list(answers)}),
                                                           name=target),
                    RegexScorer(r"^yes$"), n_repeats=5)


# --- the labels --------------------------------------------------------------------

def test_a_changed_rubric_is_evaluator_drift_not_a_model_change():
    report = analyze_drift(_judged(rubric="Be strict."), _judged(rubric="Be lenient."))
    assert report.kind == EVALUATOR_DRIFT
    assert not report.comparable
    assert {c.name for c in report.evaluator_changes} >= {"rubric", "judge prompt"}

    summary = report.summary()
    assert "did not measure the same thing" in summary
    assert "none of that can be attributed to the model" in summary
    for forbidden in ("improved", "worse", "better", "regression"):
        assert forbidden not in summary.lower()


def test_same_evaluator_same_target_with_moved_verdicts_is_judge_variance():
    """An LLM judge is a sampler: identical instrument, different verdict."""
    before = _judged(verdicts=("PASS",) * 5)
    after = _judged(verdicts=("FAIL",) * 5)
    report = analyze_drift(before, after)

    assert report.comparable
    assert report.kind == JUDGE_VARIANCE
    assert len(report.verdict_changes) == 1
    assert "cannot be separated from the target" in report.summary()
    assert "decompose" in report.summary()


def test_a_deterministic_grader_makes_it_target_variance():
    """A regex cannot disagree with itself, so the target is the only thing left."""
    report = analyze_drift(_regex(["yes"]), _regex(["no"]))
    assert report.comparable
    assert report.kind == TARGET_VARIANCE
    assert "can only come from the target" in report.summary()


def test_a_changed_target_model_is_a_comparison_not_drift():
    report = analyze_drift(_regex(["yes"], target="model-a"),
                           _regex(["no"], target="model-b"))
    assert report.comparable
    assert report.kind == TARGET_CHANGE
    assert "not drift" in report.summary()
    assert "evalseal diff" in report.summary()


def test_identical_runs_report_no_drift():
    report = analyze_drift(_regex(["yes"]), _regex(["yes"]))
    assert report.kind == STABLE
    assert report.verdict_changes == []
    assert "No drift" in report.summary()


# --- the content the report has to carry -------------------------------------------

def test_the_report_carries_strips_counts_flip_rates_and_stability():
    before = _regex(["yes"])
    after = _regex(["yes", "no", "yes", "no", "yes"])
    case = analyze_drift(before, after).cases[0]

    assert case.before_sequence == "PPPPP"
    assert case.after_sequence == "PFPFP"
    assert case.before_counts == {"pass": 5, "fail": 0, "other": 0}
    assert case.after_counts == {"pass": 3, "fail": 2, "other": 0}
    assert case.before_flip_rate == 0.0 and case.after_flip_rate == pytest.approx(0.4)
    assert case.before_stability == "STABLE" and case.after_stability == "UNSTABLE"
    assert case.stability_changed
    assert not case.verdict_changed          # the majority is still PASS


def test_top_unstable_orders_by_the_later_run():
    """Case ids are the prompts here, so the ordering assertion names real cases."""
    ds = Dataset([Case(p, p) for p in ("i0", "i1", "i2", "i3")], hash="sha256:t")
    before = run_eval(ds, LocalCallableTarget(lambda p: "yes"),
                      RegexScorer(r"^yes$"), n_repeats=5)
    after = run_eval(
        ds,
        # Explicit five-run sequences, so the flip rates differ: i2 at 40%, i1 at 20%.
        LocalCallableTarget(scripted({
            "i0": ["yes"] * 5,
            "i1": ["yes", "yes", "yes", "yes", "no"],
            "i2": ["yes", "no", "yes", "no", "yes"],
            "i3": ["yes"] * 5,
        })),
        RegexScorer(r"^yes$"), n_repeats=5)
    report = analyze_drift(before, after)
    top = report.top_unstable(2)
    assert [c.case_id for c in top] == ["i2", "i1"]
    assert top[0].after_flip_rate > top[1].after_flip_rate


def test_cases_present_in_only_one_run_are_listed_not_compared():
    """Only ids in both runs are compared; the rest are named, not silently dropped."""
    target = LocalCallableTarget(lambda p: "yes")
    before = run_eval(Dataset([Case("a", "a"), Case("b", "b")], hash="sha256:1"),
                      target, RegexScorer("yes"), n_repeats=5)
    after = run_eval(Dataset([Case("b", "b"), Case("c", "c")], hash="sha256:2"),
                     target, RegexScorer("yes"), n_repeats=5)

    report = analyze_drift(before, after)
    assert [c.case_id for c in report.cases] == ["b"]
    assert report.only_in_before == ["a"]
    assert report.only_in_after == ["c"]


def test_a_cross_schema_comparison_is_flagged():
    before, after = _regex(["yes"]), _regex(["yes"])
    before.manifest.schema_version = "1.3"
    assert "sealed before schema 1.4" in analyze_drift(before, after).schema_note


def test_render_leads_with_the_cause_not_with_a_number():
    text = render(analyze_drift(_judged(rubric="A"), _judged(rubric="B")))
    assert text.startswith("# Judge drift: evaluator drift")
    assert "| evaluator fingerprint |" in text
    # The cause is named before any per-case number.
    assert text.index("did not measure the same thing") < text.index("mean flip rate")


# --- the CLI ------------------------------------------------------------------------

def test_cli_drift_reports_and_never_gates(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_judged(rubric="Be strict."), ledger, relink=True)
    seal_and_append(_judged(rubric="Be lenient."), ledger, relink=True)

    result = runner.invoke(app, ["drift", "0", "-1", "--ledger", str(ledger)])
    assert result.exit_code == 0, result.output
    assert "evaluator drift" in " ".join(result.output.split())


def test_cli_drift_json_is_machine_readable(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_regex(["yes"]), ledger, relink=True)
    seal_and_append(_regex(["no"]), ledger, relink=True)

    result = runner.invoke(app, ["drift", "0", "-1", "--ledger", str(ledger), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == TARGET_VARIANCE
    assert payload["comparable"] is True
    assert payload["n_verdict_changes"] == 1
    assert payload["cases"][0]["before"]["sequence"] == "PPPPP"
    assert payload["summary"]


def test_cli_drift_writes_a_markdown_report(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_regex(["yes"]), ledger, relink=True)
    seal_and_append(_regex(["yes"]), ledger, relink=True)
    out = tmp_path / "drift.md"

    result = runner.invoke(app, ["drift", "0", "-1", "--ledger", str(ledger),
                                 "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "No drift" in out.read_text(encoding="utf-8")
