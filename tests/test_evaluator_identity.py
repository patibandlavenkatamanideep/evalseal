"""Evaluator identity, scheme 3: the grading *code* is part of the instrument.

Scheme 2 sealed the scorer's *configuration* - a regex, a tolerance, the judge's
parameters - and nothing about the algorithm that turns a response into a verdict. So
a fork that changed answer extraction, a judge given a different system prompt, or a
judge reached through a different API version all produced a byte-identical evaluator
fingerprint. `--expect-evaluator` passed and `diff` called the runs comparable.

Each test here names one input that decides a verdict and shows the fingerprint move
with it. The two "stays put" tests at the end are the other half of the contract: a
change to the model under test must *not* make a benchmark incomparable.
"""
from __future__ import annotations

import importlib.util
import re

import httpx
import pytest
from conftest import fake_http, make_dataset

import evalseal.adapters.scorer as scorer_mod
from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.recording import Cassette
from evalseal.adapters.scorer import (
    AnswerMatchScorer,
    ExactMatchScorer,
    LLMJudgeScorer,
    RegexScorer,
)
from evalseal.adapters.target import AnthropicTarget, LocalCallableTarget
from evalseal.config import judge_config, load_scorer_config
from evalseal.diffing import EVALUATOR_FIELDS, diff_records
from evalseal.executor import run_eval
from evalseal.identity import code_digest
from evalseal.ledger import config_fingerprint, evaluator_fingerprint
from evalseal.models import SuiteProvenance

# "41" is a false lead and "42" is the answer. Every scorer in the baseline reads the
# last number, so this case passes; a fork that reads the first number fails it. One
# dataset, two algorithms, opposite verdicts - and in scheme 2, one fingerprint.
_MISLEADING = "I first thought 41, but the answer is 42"


def _answer_run(model: str = "model-a"):
    ds = Dataset([Case("c0", "what is six times seven?", "42")], hash="sha256:test")
    return run_eval(ds, LocalCallableTarget(lambda p: _MISLEADING, name=model),
                    AnswerMatchScorer(), n_repeats=3)


def _first_number(text: str) -> str:
    """A fork of `extract_answer` that keeps the model's first number, not its last.

    A real and tempting change: it is what you write if you believe a model states its
    answer up front. It is a different instrument, and the receipt has to say so.
    """
    numbers = re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)
    return numbers[0] if numbers else text.strip()


def test_a_forked_extraction_algorithm_changes_the_evaluator_fingerprint(monkeypatch):
    """C1-1. The whole defect in one test: same config, opposite scores, same pin."""
    before = _answer_run()
    monkeypatch.setattr(scorer_mod, "extract_answer", _first_number)
    after = _answer_run()

    # The change is real and it is large: every case flips.
    assert before.aggregate.mean_score == 1.0
    assert after.aggregate.mean_score == 0.0
    # So the instrument is not the same instrument.
    assert evaluator_fingerprint(before) != evaluator_fingerprint(after)
    assert before.manifest.scorer.impl_hash != after.manifest.scorer.impl_hash


def test_a_forked_extraction_algorithm_blocks_the_comparison(monkeypatch):
    """Invariant 5: an evaluator change blocks comparison rather than explaining a jump."""
    before = _answer_run()
    monkeypatch.setattr(scorer_mod, "extract_answer", _first_number)
    after = _answer_run()

    result = diff_records(before, after)
    assert not result.comparable
    # And the reason names the grading code, rather than falling through to "something
    # changed that this summary does not list".
    assert "implementation" in result.reason
    assert "does not list" not in result.reason


def test_normalization_is_part_of_the_instrument(monkeypatch):
    """`strip_decoration` decides whether `` `42` `` is the right answer."""
    before = _answer_run()
    monkeypatch.setattr(scorer_mod, "strip_decoration", lambda value: value)
    assert evaluator_fingerprint(_answer_run()) != evaluator_fingerprint(before)


def test_the_verdict_parser_is_part_of_the_instrument(monkeypatch):
    """A judge's PASS/FAIL parser is the last step between a reply and a score."""
    before = _judged_locally()
    monkeypatch.setattr(scorer_mod, "parse_verdict", lambda text: 1)
    assert evaluator_fingerprint(_judged_locally()) != evaluator_fingerprint(before)


def test_rewriting_the_scoring_method_itself_changes_the_fingerprint(monkeypatch):
    """Not only the helpers: the comparison in `score` is the instrument too."""
    before = _answer_run()

    def always_pass(self, prompt, response_text, expected):
        return scorer_mod.ScoreResult(1.0, True, 1)

    monkeypatch.setattr(AnswerMatchScorer, "score", always_pass)
    assert evaluator_fingerprint(_answer_run()) != evaluator_fingerprint(before)


# `_BARE`, `_COMMENTED` and `_CHANGED` are three spellings of one algorithm: the same
# code with different comments, docstring and line wrapping, plus one that really is a
# different instrument. Written to real files because that is the only honest way to
# test this - `exec`-ing source under an existing filename makes `inspect.getsource`
# read unrelated lines out of that file, a trap worth naming: it silently returns code
# the function does not contain.
_BARE = '''
import re
_NUMBER_RE = re.compile(r"-?\\d[\\d,]*(?:\\.\\d+)?")

def extract_answer(text):
    numbers = _NUMBER_RE.findall(text)
    if numbers:
        return numbers[-1].strip()
    return text.strip()
'''

_COMMENTED = '''
import re
_NUMBER_RE = re.compile(r"-?\\d[\\d,]*(?:\\.\\d+)?")

def extract_answer(text):
    """The model\'s final answer: the last number it wrote.

    A docstring the other spelling does not have, describing the same code.
    """
    # A comment, too.
    numbers = _NUMBER_RE.findall(
        text,
    )
    if numbers:
        return numbers[-1].strip()

    return text.strip()
'''

_CHANGED = '''
import re
_NUMBER_RE = re.compile(r"-?\\d[\\d,]*(?:\\.\\d+)?")

def extract_answer(text):
    numbers = _NUMBER_RE.findall(text)
    if numbers:
        return numbers[0].strip()        # the FIRST number: a different instrument
    return text.strip()
'''


def _load(tmp_path, name, source):
    """Import `source` as a module from a real file, so its source is readable."""
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_comment_or_docstring_edit_does_not_change_code_identity(tmp_path):
    """The other direction. An over-sensitive digest re-pins every receipt for nothing.

    Identity comes from the normalized syntax tree, so prose and layout are not part of
    it - only what the code does. Without this, writing a docstring would declare every
    past benchmark incomparable, and a check that fires for nothing gets turned off.
    """
    bare = _load(tmp_path, "bare", _BARE)
    commented = _load(tmp_path, "commented", _COMMENTED)
    changed = _load(tmp_path, "changed", _CHANGED)

    assert code_digest(bare.extract_answer) == code_digest(commented.extract_answer)
    assert code_digest(bare.extract_answer) != code_digest(changed.extract_answer)


def test_a_comment_edit_does_not_change_the_evaluator_fingerprint(tmp_path, monkeypatch):
    """The same property through the whole pipeline, not only the digest function."""
    before = _answer_run()
    bare = _load(tmp_path, "bare2", _BARE)
    monkeypatch.setattr(scorer_mod, "extract_answer", bare.extract_answer)
    plain = evaluator_fingerprint(_answer_run())

    commented = _load(tmp_path, "commented2", _COMMENTED)
    monkeypatch.setattr(scorer_mod, "extract_answer", commented.extract_answer)
    documented = evaluator_fingerprint(_answer_run())

    assert plain == documented
    # And this really is a fork of the original, not a no-op patch.
    assert plain != evaluator_fingerprint(before)


def test_a_fork_in_another_module_is_identified_by_what_it_does(tmp_path, monkeypatch):
    """Two forks that share a name are not the same instrument.

    Identified by qualified name alone, `vendored.extract_answer` would collide with
    any other fork installed under that name, and the collision reads as "unchanged".
    """
    bare = _load(tmp_path, "vendored", _BARE)
    changed = _load(tmp_path, "vendored_too", _CHANGED)
    monkeypatch.setattr(scorer_mod, "extract_answer", bare.extract_answer)
    one = evaluator_fingerprint(_answer_run())
    monkeypatch.setattr(scorer_mod, "extract_answer", changed.extract_answer)
    assert evaluator_fingerprint(_answer_run()) != one


# --- the judge's own verdict-affecting settings -------------------------------------

def _reply(text="PASS"):
    return {"id": "msg_01", "type": "message", "role": "assistant", "model": "judge-1",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def _answer_every_call(monkeypatch):
    """Answer any Messages request offline, recording what went out."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "disposable-test-value")
    sent: list[dict] = []

    def fake_post(self, url, headers=None, json=None):
        sent.append({"url": url, "headers": headers or {}, "body": json})
        return httpx.Response(200, json=_reply(), request=httpx.Request("POST", url))

    fake_http(monkeypatch, fake_post)
    return sent


def _judged_by_anthropic(tmp_path, monkeypatch, **judge_kw):
    _answer_every_call(monkeypatch)
    judge = AnthropicTarget(
        model="judge-1", cassette=Cassette(tmp_path / "c.json", record=True),
        sleep=lambda _: None, **judge_kw)
    return run_eval(make_dataset("q1"),
                    LocalCallableTarget(lambda p: "an answer", name="model-a"),
                    LLMJudgeScorer(judge=judge, rubric="Be strict."), n_repeats=1)


def _judged_locally(rubric="Be strict."):
    return run_eval(make_dataset("q1"),
                    LocalCallableTarget(lambda p: "an answer", name="model-a"),
                    LLMJudgeScorer(judge=LocalCallableTarget(lambda p: "PASS",
                                                             name="judge-1"),
                                   rubric=rubric), n_repeats=1)


def test_two_judge_system_prompts_do_not_share_a_fingerprint(tmp_path, monkeypatch):
    """C1-3. The system prompt steers the judge; nothing recorded it.

    "You are a lenient grader" and "You are a harsh grader" are two instruments, and
    the receipt showed one fingerprint for both.
    """
    lenient = _judged_by_anthropic(tmp_path / "a", monkeypatch, system="Be lenient.")
    harsh = _judged_by_anthropic(tmp_path / "b", monkeypatch, system="Be harsh.")
    assert evaluator_fingerprint(lenient) != evaluator_fingerprint(harsh)


def test_the_judge_system_prompt_is_sealed_by_hash_not_verbatim(tmp_path, monkeypatch):
    """A system prompt can be as private as the rubric it carries."""
    record = _judged_by_anthropic(tmp_path, monkeypatch, system="Be lenient.")
    judge = record.manifest.scorer.judge
    assert judge is not None
    assert judge.system_prompt_hash is not None
    assert judge.system_prompt_hash.startswith("sha256:")
    assert "lenient" not in record.model_dump_json()


def test_no_system_prompt_is_recorded_as_absent_not_as_an_empty_one(tmp_path, monkeypatch):
    """Invariant 2: "we sent none" is a different claim from "we sent an empty string"."""
    none = _judged_by_anthropic(tmp_path / "a", monkeypatch)
    empty = _judged_by_anthropic(tmp_path / "b", monkeypatch, system="")
    assert none.manifest.scorer.judge.system_prompt_hash is None
    assert empty.manifest.scorer.judge.system_prompt_hash is not None
    assert evaluator_fingerprint(none) != evaluator_fingerprint(empty)


def test_the_judge_api_version_changes_the_fingerprint(tmp_path, monkeypatch):
    """C1-4. The API version decides how a request is interpreted, and was not sealed."""
    old = _judged_by_anthropic(tmp_path / "a", monkeypatch,
                               anthropic_version="2023-06-01")
    new = _judged_by_anthropic(tmp_path / "b", monkeypatch,
                               anthropic_version="2099-01-01")
    assert old.manifest.scorer.judge.api_version == "2023-06-01"
    assert evaluator_fingerprint(old) != evaluator_fingerprint(new)


def test_an_openai_judge_does_not_silently_drop_its_system_prompt():
    """C1-5. `judge_config` popped `system` for an OpenAI judge. Same class as C1-2."""
    scorer = load_scorer_config({
        "type": "llm_judge", "rubric": "Be strict.", "judge_provider": "openai",
        "judge_model": "judge-1", "system": "You are a harsh grader.",
    })
    assert judge_config(scorer)["system"] == "You are a harsh grader."


def test_an_openai_target_transmits_its_system_prompt(tmp_path, monkeypatch):
    """Transmitted and recorded, which is the other half of "never silently ignored"."""
    monkeypatch.setenv("EVALSEAL_API_KEY", "disposable-test-value")
    sent: list[dict] = []

    def fake_post(self, url, headers=None, json=None):
        sent.append(json)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "model": "m", "choices": [{"message": {"content": "yes"},
                                       "finish_reason": "stop"}]})

    fake_http(monkeypatch, fake_post)
    from evalseal.adapters.target import OpenAICompatibleTarget

    target = OpenAICompatibleTarget(
        model="m", cassette=Cassette(tmp_path / "c.json", record=True),
        system="You are terse.", sleep=lambda _: None)
    response = target.generate("hello")

    assert sent[0]["messages"][0] == {"role": "system", "content": "You are terse."}
    assert sent[0]["messages"][1] == {"role": "user", "content": "hello"}
    assert response.system_prompt_hash is not None


# --- an absent parameter is not an effective value ----------------------------------

def test_an_absent_parameter_says_whether_it_was_inherited_or_unsupported(
        tmp_path, monkeypatch):
    """C1-6. `temperature: null` and `seed: null` meant two different things.

    One is "we did not set it, so the provider used a default this receipt does not
    know". The other is "this API has no such parameter". Reported as the same `null`,
    a reader cannot tell which, and both read as an effective value of nothing.
    """
    record = _judged_by_anthropic(tmp_path, monkeypatch)
    params = record.manifest.scorer.judge.effective_params

    assert params.seed is None
    assert params.unset["seed"] == "unsupported"       # the Messages API has no seed
    assert params.unset["temperature"] == "inherited"  # we did not send one
    # max_tokens is always sent by this adapter, so it is neither.
    assert params.max_tokens == 1024
    assert "max_tokens" not in params.unset


def test_a_value_we_sent_is_never_marked_absent(tmp_path, monkeypatch):
    record = _judged_by_anthropic(tmp_path, monkeypatch, temperature=0.0)
    params = record.manifest.scorer.judge.effective_params
    assert params.temperature == 0.0
    assert "temperature" not in params.unset


def test_inherited_and_unsupported_are_not_the_same_instrument(tmp_path, monkeypatch):
    """A judge that inherits a default is not a judge whose API cannot take one."""
    record = _judged_by_anthropic(tmp_path, monkeypatch)
    judge = record.manifest.scorer.judge
    before = evaluator_fingerprint(record)
    judge.effective_params.unset = {**judge.effective_params.unset,
                                    "seed": "inherited"}
    assert evaluator_fingerprint(record) != before


# --- the half that must not move -----------------------------------------------------

def test_changing_only_the_target_keeps_the_runs_comparable():
    """Scheme 3 must still let a benchmark do the one thing it exists to do."""
    a, b = _answer_run(model="model-a"), _answer_run(model="model-b")
    assert evaluator_fingerprint(a) == evaluator_fingerprint(b)
    assert config_fingerprint(a) != config_fingerprint(b)
    assert diff_records(a, b).comparable


def test_two_runs_of_the_same_code_fingerprint_identically():
    """Identity, not novelty: nothing here may vary between two runs of one build."""
    assert evaluator_fingerprint(_answer_run()) == evaluator_fingerprint(_answer_run())


def test_every_fingerprinted_field_has_a_name_a_reader_is_shown():
    """`diff` must be able to explain any incomparability it reports.

    The diff names fields; the fingerprint hashes them. If the fingerprint covers
    ground no name describes, the only available reason is "something changed", which
    sends a reader looking for a change nobody can point at.
    """
    for name in ("scorer implementation", "judge api version", "judge system prompt"):
        assert name in EVALUATOR_FIELDS


@pytest.mark.parametrize("build", [
    lambda: ExactMatchScorer(),
    lambda: RegexScorer(r"^yes$"),
    lambda: AnswerMatchScorer(),
    lambda: LLMJudgeScorer(judge=LocalCallableTarget(lambda p: "PASS", name="j"),
                           rubric="Be strict."),
], ids=["exact", "regex", "answer_match", "llm_judge"])
def test_every_scorer_kind_reports_an_implementation(build):
    """No scorer may be left without an identity: a missing one reads as "unchanged"."""
    ds = Dataset([Case("c0", "q", "yes")], hash="sha256:test")
    record = run_eval(ds, LocalCallableTarget(lambda p: "yes"), build(), n_repeats=1)
    assert record.manifest.scorer.impl_hash is not None
    assert record.manifest.scorer.impl_hash.startswith("sha256:")


# --- the acceptance criterion, exhaustively ------------------------------------------

def _set(path: str, value):
    """A mutation of one dotted manifest field, for the table below."""
    def apply(record):
        node = record.manifest
        parts = path.split(".")
        for part in parts[:-1]:
            node = getattr(node, part)
        setattr(node, parts[-1], value)
    return apply


_OTHER_DIGEST = "sha256:" + "a" * 64

#: Every field `diffing.EVALUATOR_FIELDS` names, and a way to change it. The table is
#: checked against that set below, so a field named to a reader but absent from the
#: fingerprint - or hashed but never named - fails a test instead of shipping.
_EVALUATOR_INPUTS = [
    ("scorer type", _set("scorer.type", "regex")),
    ("scorer settings", _set("scorer.config_hash", _OTHER_DIGEST)),
    ("scorer implementation", _set("scorer.impl_hash", _OTHER_DIGEST)),
    ("judge provider", _set("scorer.judge.provider", "anthropic")),
    ("judge endpoint", _set("scorer.judge.base_url", "https://proxy.internal/v1")),
    ("judge model", _set("scorer.judge.requested_model", "judge-b")),
    ("judge api version", _set("scorer.judge.api_version", "2099-01-01")),
    ("judge system prompt", _set("scorer.judge.system_prompt_hash", _OTHER_DIGEST)),
    ("judge temperature", _set("scorer.judge.effective_params.temperature", 0.7)),
    ("judge top_p", _set("scorer.judge.effective_params.top_p", 0.5)),
    ("judge max_tokens", _set("scorer.judge.effective_params.max_tokens", 64)),
    ("judge seed", _set("scorer.judge.effective_params.seed", 7)),
    ("judge unset params",
     _set("scorer.judge.effective_params.unset", {"seed": "inherited"})),
    ("judge prompt", _set("scorer.judge_prompt_hash", _OTHER_DIGEST)),
    ("rubric", _set("scorer.rubric_hash", _OTHER_DIGEST)),
    ("dataset", _set("dataset.hash", _OTHER_DIGEST)),
    ("case set", lambda record: setattr(record.results[0], "case_id", "renamed")),
]


@pytest.mark.parametrize("name, mutate", _EVALUATOR_INPUTS,
                         ids=[name for name, _ in _EVALUATOR_INPUTS])
def test_every_supported_evaluator_input_changes_the_fingerprint(name, mutate):
    """C1 acceptance: an input that decides a verdict cannot leave the pin unmoved."""
    record = _judged_locally()
    before = evaluator_fingerprint(record)
    mutate(record)
    assert evaluator_fingerprint(record) != before, name


def test_the_table_above_covers_exactly_the_fields_a_reader_is_shown():
    """The oracle for the table: it is checked against the diff's own field list.

    Add a field to `EVALUATOR_FIELDS` without hashing it and this fails. Hash one
    without naming it and the diff can only say "something changed", which the module
    itself calls out as no explanation at all - so that fails here too.
    """
    assert {name for name, _ in _EVALUATOR_INPUTS} == set(EVALUATOR_FIELDS)


_TARGET_ONLY = [
    ("target provider", _set("target.provider", "anthropic")),
    ("target endpoint", _set("target.base_url", "https://proxy.internal/v1")),
    ("target model", _set("target.requested_model", "model-b")),
    ("target temperature", _set("target.effective_params.temperature", 0.9)),
    ("target top_p", _set("target.effective_params.top_p", 0.3)),
    ("target max_tokens", _set("target.effective_params.max_tokens", 32)),
    ("target api version", _set("target.api_version", "2099-01-01")),
    ("target system prompt", _set("target.system_prompt_hash", _OTHER_DIGEST)),
    ("suite", _set("suite", SuiteProvenance(hash=_OTHER_DIGEST))),
]


@pytest.mark.parametrize("name, mutate", _TARGET_ONLY,
                         ids=[name for name, _ in _TARGET_ONLY])
def test_no_field_of_the_thing_under_test_changes_the_fingerprint(name, mutate):
    """The other half of the acceptance criterion, field by field.

    Swapping the model under test - including its own system prompt, which is part of
    the subject rather than the instrument - is what a benchmark is for. If any of
    these moved the evaluator fingerprint, every A-vs-B comparison would report itself
    as incomparable.
    """
    record = _judged_locally()
    before = evaluator_fingerprint(record)
    mutate(record)
    assert evaluator_fingerprint(record) == before, name
    assert config_fingerprint(record) != config_fingerprint(_judged_locally()), name
