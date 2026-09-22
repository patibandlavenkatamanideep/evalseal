"""The receipt must seal how the answer was judged, not only what it scored."""
from __future__ import annotations

from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import LLMJudgeScorer, RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.executor import run_eval
from evalseal.ledger import config_fingerprint, seal_and_append
from evalseal.models import SCHEMA_VERSION, SuiteProvenance
from evalseal.provenance import environment_provenance, file_hash, git_provenance, text_hash

runner = CliRunner()


def _judged(rubric: str, verdicts=("PASS",) * 5):
    target = LocalCallableTarget(lambda p: "an answer")
    judge = LocalCallableTarget(scripted({_judge_prompt(rubric): list(verdicts)}), name="judge")
    scorer = LLMJudgeScorer(judge=judge, rubric=rubric)
    return run_eval(make_dataset("q"), target, scorer, n_repeats=5)


def _judge_prompt(rubric: str) -> str:
    return (f"{rubric}\n\nUSER PROMPT:\nq\n\nRESPONSE TO GRADE:\nan answer\n\n"
            "Answer with exactly one word: PASS or FAIL.")


def test_manifest_seals_environment_and_code():
    rec = run_eval(make_dataset("q"), LocalCallableTarget(lambda p: "yes"),
                   RegexScorer("yes"), n_repeats=5)
    env = rec.manifest.environment
    assert env.evalseal_version and env.python_version and env.platform
    assert rec.manifest.schema_version == SCHEMA_VERSION
    # git fields are present; their values depend on where the tests run
    assert set(rec.manifest.code.model_dump()) == {"commit", "dirty"}


def test_dataset_case_ids_are_sealed():
    rec = run_eval(make_dataset("a", "b"), LocalCallableTarget(lambda p: "yes"),
                   RegexScorer("yes"), n_repeats=5)
    assert rec.manifest.dataset.case_ids == [c.case_id for c in rec.results]
    assert rec.manifest.dataset.n_cases == 2


def test_changing_the_rubric_changes_the_rubric_and_prompt_hashes():
    first = _judged("Be strict.")
    second = _judged("Be lenient.")
    assert first.manifest.scorer.rubric_hash != second.manifest.scorer.rubric_hash
    assert first.manifest.scorer.judge_prompt_hash != second.manifest.scorer.judge_prompt_hash
    assert config_fingerprint(first) != config_fingerprint(second)


def test_same_configuration_produces_the_same_fingerprint():
    assert config_fingerprint(_judged("Be strict.")) == config_fingerprint(_judged("Be strict."))


def test_judge_prompt_template_is_hashed_but_not_stored_by_default():
    """The sealed hash is of the template, with the case left as placeholders."""
    from evalseal.adapters.scorer import build_judge_prompt

    rec = _judged("Be strict.")
    expected = text_hash(build_judge_prompt("Be strict.", "{prompt}", "{response}"))
    assert rec.manifest.scorer.judge_prompt_hash == expected
    assert rec.manifest.scorer.judge_prompt is None


def test_judge_prompt_can_be_stored_explicitly():
    target = LocalCallableTarget(lambda p: "an answer")
    judge = LocalCallableTarget(lambda p: "PASS", name="judge")
    rec = run_eval(make_dataset("q"), target,
                   LLMJudgeScorer(judge=judge, rubric="Be strict."),
                   n_repeats=5, store_judge_prompt=True)
    stored = rec.manifest.scorer.judge_prompt
    assert stored is not None
    assert "RESPONSE TO GRADE" in stored
    # The template, so no case text and no response reaches the receipt through it.
    assert "{prompt}" in stored and "{response}" in stored
    assert "an answer" not in stored


def test_suite_hash_is_sealed_when_supplied(tmp_path):
    suite_file = tmp_path / "suite.json"
    suite_file.write_text('{"n_repeats": 5}')
    rec = run_eval(make_dataset("q"), LocalCallableTarget(lambda p: "yes"),
                   RegexScorer("yes"), n_repeats=5,
                   suite=SuiteProvenance(name="suite", path=str(suite_file),
                                         hash=file_hash(suite_file)))
    assert rec.manifest.suite.hash == file_hash(suite_file)
    suite_file.write_text('{"n_repeats": 7}')
    assert rec.manifest.suite.hash != file_hash(suite_file)   # a change is visible


def test_fingerprint_ignores_things_that_do_not_change_the_measurement():
    """Two runs of the same config compare, even though their timestamps differ."""
    a = run_eval(make_dataset("q"), LocalCallableTarget(lambda p: "yes"),
                 RegexScorer("yes"), n_repeats=5)
    b = run_eval(make_dataset("q"), LocalCallableTarget(lambda p: "yes"),
                 RegexScorer("yes"), n_repeats=5)
    assert a.created_at != b.created_at or a.hash != b.hash
    assert config_fingerprint(a) == config_fingerprint(b)


def test_diff_says_when_configs_are_not_comparable(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_judged("Be strict."), ledger, relink=True)
    seal_and_append(_judged("Be lenient."), ledger, relink=True)

    result = runner.invoke(app, ["diff", "--ledger", str(ledger), "0", "1"])
    assert result.exit_code == 0, result.output
    assert "Not directly comparable" in result.output
    assert "rubric" in result.output


def test_provenance_helpers_do_not_read_the_environment():
    """A helper that read os.environ could leak a key into a receipt."""
    import inspect

    from evalseal import provenance
    source = inspect.getsource(provenance)
    assert "os.environ" not in source and "getenv" not in source
    assert environment_provenance()["python_version"]
    assert set(git_provenance()) == {"commit", "dirty"}
