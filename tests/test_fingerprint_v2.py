"""Evaluator fingerprint scheme 2: the instrument, and only the instrument.

One test per acceptance criterion. The fingerprint must move when anything that decides
how a response scores moves, and must not move when the model under test changes.
"""
from __future__ import annotations

from conftest import make_dataset

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.recording import Cassette
from evalseal.adapters.scorer import AnswerMatchScorer, LLMJudgeScorer, RegexScorer
from evalseal.adapters.target import AnthropicTarget, LocalCallableTarget
from evalseal.diffing import diff_records
from evalseal.executor import run_eval
from evalseal.ledger import (
    FINGERPRINT_PREFIX,
    config_fingerprint,
    evaluator_fingerprint,
    fingerprint_scheme,
    records_share_fingerprint_inputs,
)


def _judged(*, rubric="Be strict.", judge_model="judge-a", target_model="model-a",
            judge_params=None, base_url="https://api.example/v1"):
    """A judged run whose judge provenance can be varied one field at a time."""
    judge = LocalCallableTarget(lambda p: "PASS", name=judge_model)
    record = run_eval(make_dataset("q1", "q2"),
                      LocalCallableTarget(lambda p: "an answer", name=target_model),
                      LLMJudgeScorer(judge=judge, rubric=rubric), n_repeats=5)
    jp = record.manifest.scorer.judge
    assert jp is not None
    jp.base_url = base_url
    for field, value in (judge_params or {}).items():
        setattr(jp.effective_params, field, value)
    return record


def _fp(record):
    return evaluator_fingerprint(record)


# --- the acceptance criteria -------------------------------------------------------

def test_changing_judge_temperature_changes_the_fingerprint():
    assert _fp(_judged(judge_params={"temperature": 0.0})) != \
           _fp(_judged(judge_params={"temperature": 1.0}))


def test_changing_judge_top_p_changes_the_fingerprint():
    assert _fp(_judged(judge_params={"top_p": 0.9})) != \
           _fp(_judged(judge_params={"top_p": 0.5}))


def test_changing_judge_max_tokens_changes_the_fingerprint():
    """Scheme 1 could not see this: max_tokens was dropped before it was sealed."""
    assert _fp(_judged(judge_params={"max_tokens": 64})) != \
           _fp(_judged(judge_params={"max_tokens": 1024}))


def test_changing_the_judge_endpoint_changes_the_fingerprint():
    assert _fp(_judged(base_url="https://api.example/v1")) != \
           _fp(_judged(base_url="https://proxy.internal/v1"))


def test_changing_the_judge_model_changes_the_fingerprint():
    assert _fp(_judged(judge_model="judge-a")) != _fp(_judged(judge_model="judge-b"))


def test_changing_the_prompt_template_changes_the_fingerprint(monkeypatch):
    import evalseal.adapters.scorer as scorer_mod

    before = _fp(_judged())
    original = scorer_mod.build_judge_prompt
    monkeypatch.setattr(scorer_mod, "build_judge_prompt",
                        lambda rubric, prompt, response: original(rubric, prompt, response)
                        + "\n\nBe extra careful.")
    assert _fp(_judged()) != before


def test_changing_the_rubric_changes_the_fingerprint():
    assert _fp(_judged(rubric="Be strict.")) != _fp(_judged(rubric="Be lenient."))


def test_changing_only_the_target_model_keeps_the_runs_comparable():
    """The comparison a benchmark exists to make must survive scheme 2."""
    a, b = _judged(target_model="model-a"), _judged(target_model="model-b")
    assert evaluator_fingerprint(a) == evaluator_fingerprint(b)
    assert config_fingerprint(a) != config_fingerprint(b)
    assert diff_records(a, b).comparable


# --- the scorer is part of the instrument too --------------------------------------

def _scored(scorer, model="m"):
    """A dataset carrying `expected`, which answer_match requires."""
    ds = Dataset([Case("c0", "q", "yes")], hash="sha256:test")
    return run_eval(ds, LocalCallableTarget(lambda p: "yes", name=model),
                    scorer, n_repeats=5)


def test_changing_a_regex_pattern_changes_the_fingerprint():
    """Two different regexes are two different graders. Scheme 1 hashed them the same."""
    assert _fp(_scored(RegexScorer(r"^yes$"))) != _fp(_scored(RegexScorer(r"^no$")))


def test_changing_a_numeric_tolerance_changes_the_fingerprint():
    assert _fp(_scored(AnswerMatchScorer(tolerance=1e-6))) != \
           _fp(_scored(AnswerMatchScorer(tolerance=0.5)))


def test_changing_the_scorer_type_changes_the_fingerprint():
    assert _fp(_scored(RegexScorer("yes"))) != _fp(_scored(AnswerMatchScorer()))


# --- inputs ------------------------------------------------------------------------

def test_changing_the_case_set_changes_the_fingerprint():
    """Same dataset hash, a different set of cases actually scored."""
    one = Dataset([Case("c0", "q", "yes")], hash="sha256:same")
    two = Dataset([Case("c0", "q", "yes"), Case("c1", "q2", "yes")], hash="sha256:same")
    target = LocalCallableTarget(lambda p: "yes")
    a = run_eval(one, target, RegexScorer("yes"), n_repeats=5)
    b = run_eval(two, target, RegexScorer("yes"), n_repeats=5)
    assert a.manifest.dataset.hash == b.manifest.dataset.hash
    assert _fp(a) != _fp(b)


def test_max_tokens_reaches_the_receipt_from_the_anthropic_adapter(tmp_path, monkeypatch):
    """The gap that motivated scheme 2: the adapter sent it, nothing recorded it."""
    import httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, request=httpx.Request("POST", url),
        json={"model": "m", "content": [{"type": "text", "text": "yes"}],
              "stop_reason": "end_turn"}))
    target = AnthropicTarget(model="m", max_tokens=77,
                             cassette=Cassette(tmp_path / "c.json", record=True))
    record = run_eval(make_dataset("q"), target, RegexScorer("yes"), n_repeats=5)

    assert record.manifest.target.effective_params.max_tokens == 77
    assert record.manifest.target.provider == "anthropic"


# --- scheme identity ---------------------------------------------------------------

def test_the_fingerprint_carries_its_scheme():
    value = _fp(_scored(RegexScorer("yes")))
    assert value.startswith(f"{FINGERPRINT_PREFIX}:sha256:")
    assert fingerprint_scheme(value) == 2
    assert fingerprint_scheme("sha256:" + "0" * 64) is None


def test_records_sealed_before_1_4_are_flagged_as_missing_fingerprint_inputs():
    new = _scored(RegexScorer("yes"))
    old = _scored(RegexScorer("yes"))
    old.manifest.schema_version = "1.3"
    assert records_share_fingerprint_inputs(new, new)
    assert not records_share_fingerprint_inputs(old, new)
