from __future__ import annotations

from conftest import make_dataset, scripted

from evalseal.adapters.scorer import LLMJudgeScorer, RegexScorer, parse_verdict
from evalseal.adapters.target import LocalCallableTarget
from evalseal.executor import run_eval


def test_scripted_variance_produces_known_flip_rates():
    target = LocalCallableTarget(scripted({
        "stable": ["yes"],
        "borderline": ["yes", "yes", "yes", "yes", "no"],
        "unstable": ["yes", "no", "yes", "no", "yes"],
    }))
    ds = make_dataset("stable", "borderline", "unstable")
    rec = run_eval(ds, target, RegexScorer(pattern=r"^yes$"), n_repeats=5)

    by_prompt = {c.prompt: r for c, r in zip(ds.cases, rec.results)}
    assert by_prompt["stable"].stability == "STABLE"
    assert abs(by_prompt["borderline"].flip_rate - 0.2) < 1e-9
    assert by_prompt["borderline"].stability == "BORDERLINE"
    assert by_prompt["unstable"].flip_rate == 0.4
    assert by_prompt["unstable"].stability == "UNSTABLE"

    agg = rec.aggregate
    assert (agg.n_stable, agg.n_borderline, agg.n_unstable) == (1, 1, 1)
    assert rec.manifest.run_config.n_repeats == 5
    assert rec.manifest.dataset.n_cases == 3
    assert agg.warnings == []  # local targets are explicit and canonical


def test_judge_nondeterminism_is_tracked():
    # The target is perfectly stable; only the judge disagrees with itself.
    target = LocalCallableTarget(lambda p: "a fine answer")
    judge_verdicts = iter(["PASS", "FAIL", "PASS", "PASS", "FAIL"])
    judge = LocalCallableTarget(lambda p: next(judge_verdicts), name="judge")
    scorer = LLMJudgeScorer(judge=judge, rubric="be strict")

    rec = run_eval(make_dataset("q"), target, scorer, n_repeats=5)
    assert rec.results[0].scores == [1.0, 0.0, 1.0, 1.0, 0.0]
    assert rec.results[0].stability == "UNSTABLE"
    assert rec.manifest.scorer.type == "llm_judge"
    assert rec.manifest.scorer.judge.requested_model == "judge"
    assert rec.manifest.scorer.rubric_hash.startswith("sha256:")


def test_parse_verdict():
    assert parse_verdict("PASS") == 1
    assert parse_verdict("pass.") == 1
    assert parse_verdict("FAIL — this does not PASS") == 0
    assert parse_verdict("PASSABLE") == 0
    assert parse_verdict("") == 0
