"""The sealed judge prompt hash identifies how the judge was asked - nothing else.

Until schema 1.3 the receipt hashed the last judge prompt sent. That prompt embeds the
last case's prompt and the target's response, so the hash changed whenever the target
answered differently, and under concurrency with whichever case finished last. Through
the evaluator fingerprint, that made every LLM-judge suite report "not comparable"
across target models, and made two replays of one cassette disagree.
"""
from __future__ import annotations

from conftest import make_dataset

from evalseal.adapters.scorer import LLMJudgeScorer, build_judge_prompt
from evalseal.adapters.target import LocalCallableTarget
from evalseal.diffing import diff_records
from evalseal.executor import run_eval
from evalseal.ledger import evaluator_fingerprint


def _judged(model: str = "model-a", answer: str = "an answer", *, concurrency: int = 1,
            rubric: str = "Be strict."):
    target = LocalCallableTarget(lambda p: f"{answer} to {p}", name=model)
    judge = LocalCallableTarget(lambda p: "PASS", name="judge")
    return run_eval(make_dataset(*(f"q{i}" for i in range(12))), target,
                    LLMJudgeScorer(judge=judge, rubric=rubric),
                    n_repeats=5, concurrency=concurrency)


def test_same_grading_different_target_model_is_comparable_for_a_judge_suite():
    """The regression. This held for regex scorers and silently failed for judges."""
    a = _judged(model="model-a", answer="A says")
    b = _judged(model="model-b", answer="B says")
    assert a.manifest.scorer.judge_prompt_hash == b.manifest.scorer.judge_prompt_hash
    assert evaluator_fingerprint(a) == evaluator_fingerprint(b)
    result = diff_records(a, b)
    assert result.comparable
    assert result.evaluator_changes == []


def test_the_hash_does_not_depend_on_scheduling():
    """Twelve cases at concurrency 8 finish in a different order each time."""
    hashes = {_judged(concurrency=8).manifest.scorer.judge_prompt_hash for _ in range(5)}
    hashes.add(_judged(concurrency=1).manifest.scorer.judge_prompt_hash)
    assert len(hashes) == 1


def test_a_changed_rubric_still_changes_the_hash():
    assert (_judged(rubric="Be strict.").manifest.scorer.judge_prompt_hash
            != _judged(rubric="Be lenient.").manifest.scorer.judge_prompt_hash)


def test_the_template_is_exactly_the_wrapper_that_is_sent():
    """Cassette keys depend on the judge request bytes, so the refactor must not move
    one. This is the prompt the scorer sent before the template existed."""
    rubric, prompt, response = "Be strict.", "What is 2+2?", "4"
    before = (
        f"{rubric}\n\n"
        f"USER PROMPT:\n{prompt}\n\n"
        f"RESPONSE TO GRADE:\n{response}\n\n"
        f"Answer with exactly one word: PASS or FAIL."
    )
    assert build_judge_prompt(rubric, prompt, response) == before


def test_braces_in_a_rubric_do_not_break_the_template():
    """The template is built by the same function, not by str.format over the rubric."""
    scorer = LLMJudgeScorer(judge=LocalCallableTarget(lambda p: "PASS"),
                            rubric="Return {\"ok\": true} only when correct.")
    assert scorer.judge_prompt_template.startswith('Return {"ok": true} only')
    assert "{prompt}" in scorer.judge_prompt_template
