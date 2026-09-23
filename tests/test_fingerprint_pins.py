"""The two fingerprints answer two different questions, and the CLI must not blur them.

evaluator fingerprint -> "are these runs comparable?"  (how a run was graded)
config fingerprint    -> "what exactly ran?"            (grading plus the target under test)

Until 2.0.2 a failed `gate --expect-config` printed "Not directly comparable". The gate
holds only a hash, so it cannot tell which side changed, and the claim was false in the
single most common case: same grading, different target model.
"""
from __future__ import annotations

import json

from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import LLMJudgeScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import EXIT_UNSTABLE, app
from evalseal.executor import run_eval
from evalseal.ledger import config_fingerprint, evaluator_fingerprint, seal_and_append

runner = CliRunner()


def _text(result) -> str:
    """CLI output with line wrapping collapsed: rich wraps at the terminal width, and an
    assertion should not depend on where a sentence happened to break."""
    return " ".join(result.output.split())


def _judge_prompt(rubric: str) -> str:
    return (f"{rubric}\n\nUSER PROMPT:\nq\n\nRESPONSE TO GRADE:\nan answer\n\n"
            "Answer with exactly one word: PASS or FAIL.")


def _record(rubric: str = "Be strict.", model: str = "model-a"):
    target = LocalCallableTarget(lambda p: "an answer", name=model)
    judge = LocalCallableTarget(scripted({_judge_prompt(rubric): ["PASS"]}), name="judge")
    return run_eval(make_dataset("q"), target, LLMJudgeScorer(judge=judge, rubric=rubric),
                    n_repeats=5)


def _ledger(tmp_path, record):
    path = tmp_path / "l.jsonl"
    seal_and_append(record, path)
    return path


def test_a_new_target_model_changes_the_config_but_not_the_evaluator():
    a, b = _record(model="model-a"), _record(model="model-b")
    assert evaluator_fingerprint(a) == evaluator_fingerprint(b)
    assert config_fingerprint(a) != config_fingerprint(b)


def test_config_pin_mismatch_does_not_claim_the_runs_are_incomparable(tmp_path):
    """The regression: only the target moved, so the runs are comparable.

    The gate must still fail - the pinned configuration is not what ran - but it must
    not say the scores cannot be compared, because they can.
    """
    pinned = config_fingerprint(_record(model="model-a"))
    ledger = _ledger(tmp_path, _record(model="model-b"))

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--no-verify-ledger",
                                 "--expect-config", pinned])
    assert result.exit_code == EXIT_UNSTABLE
    assert "Configuration differs from the pinned one" in _text(result)
    assert "Not directly comparable" not in _text(result)
    # Actionable in both places the pin can live.
    assert "--expect-evaluator" in _text(result)
    assert "run.expect_evaluator" in _text(result)


def test_evaluator_pin_passes_when_only_the_target_model_changed(tmp_path):
    pinned = evaluator_fingerprint(_record(model="model-a"))
    ledger = _ledger(tmp_path, _record(model="model-b"))

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--no-verify-ledger",
                                 "--expect-evaluator", pinned])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_evaluator_pin_fails_as_incomparable_when_the_grading_changed(tmp_path):
    pinned = evaluator_fingerprint(_record(rubric="Be strict."))
    ledger = _ledger(tmp_path, _record(rubric="Be lenient."))

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--no-verify-ledger",
                                 "--expect-evaluator", pinned])
    assert result.exit_code == EXIT_UNSTABLE
    assert "Not directly comparable" in _text(result)
    assert "grading setup changed" in _text(result)


def test_both_pins_can_be_satisfied_together(tmp_path):
    record = _record()
    ledger = _ledger(tmp_path, record)
    result = runner.invoke(app, [
        "gate", "--ledger", str(ledger),
        "--expect-evaluator", evaluator_fingerprint(record),
        "--expect-config", config_fingerprint(record),
    ])
    assert result.exit_code == 0, result.output


def test_report_json_exposes_both_fingerprints_so_either_can_be_pinned(tmp_path):
    record = _record()
    ledger = _ledger(tmp_path, record)
    result = runner.invoke(app, ["report", "--ledger", str(ledger), "--json"])
    assert result.exit_code == 0, result.output
    prov = json.loads(result.output)["provenance"]
    assert prov["evaluator_fingerprint"] == evaluator_fingerprint(record)
    assert prov["config_fingerprint"] == config_fingerprint(record)


def test_gate_json_reports_both_fingerprints(tmp_path):
    record = _record()
    ledger = _ledger(tmp_path, record)
    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evaluator_fingerprint"] == evaluator_fingerprint(record)
    assert payload["config_fingerprint"] == config_fingerprint(record)


def test_a_pin_from_an_older_scheme_says_so_instead_of_blaming_the_grading(tmp_path):
    """A scheme-1 pin cannot match a scheme-2 fingerprint, and nothing about the run
    changed. Reporting "the grading setup changed" would send someone hunting a change
    that was never made."""
    ledger = _ledger(tmp_path, _record())
    stale = "sha256:" + "0" * 64          # how scheme 1 wrote them: no scheme tag

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--no-verify-ledger",
                                 "--expect-evaluator", stale])
    assert result.exit_code == EXIT_UNSTABLE
    text = _text(result)
    assert "made under an unversioned scheme" in text
    assert "Re-pin from" in text
    assert "grading setup changed" not in text


def test_a_pin_from_a_future_scheme_is_also_named(tmp_path):
    ledger = _ledger(tmp_path, _record())
    future = "evalseal-fp/99:sha256:" + "0" * 64
    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--no-verify-ledger",
                                 "--expect-config", future])
    assert result.exit_code == EXIT_UNSTABLE
    assert "made under scheme 99" in _text(result)


def test_a_current_scheme_mismatch_still_explains_the_difference(tmp_path):
    """Same scheme, genuinely different grading: the old explanation still applies."""
    pinned = evaluator_fingerprint(_record(rubric="Be strict."))
    ledger = _ledger(tmp_path, _record(rubric="Be lenient."))
    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--no-verify-ledger",
                                 "--expect-evaluator", pinned])
    assert result.exit_code == EXIT_UNSTABLE
    assert "grading setup changed" in _text(result)


def test_a_policy_file_pin_from_an_older_scheme_explains_itself_too(tmp_path):
    """The path that matters most: the PR workflow gates through a policy file.

    A policy pin written under scheme 1 must get the same re-pin explanation the flag
    gets, not a raw "expected sha256:aaa..., got evalseal-fp/2:sha256..." mismatch.
    """
    ledger = _ledger(tmp_path, _record())
    policy = tmp_path / "p.json"
    policy.write_text(json.dumps(
        {"run": {"expect_evaluator": "sha256:" + "a" * 64, "verify_ledger": False}}),
        encoding="utf-8")

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--policy", str(policy),
                                 "--json"])
    assert result.exit_code == EXIT_UNSTABLE
    detail = next(c["detail"] for c in json.loads(result.output)["checks"]
                  if c["rule"] == "run.expect_evaluator")
    assert "made under an unversioned scheme" in detail
    assert "Re-pin from" in detail
    assert "grading setup changed" not in detail


def test_a_policy_pin_that_matches_says_so(tmp_path):
    record = _record()
    ledger = _ledger(tmp_path, record)
    policy = tmp_path / "p.json"
    policy.write_text(json.dumps(
        {"run": {"expect_evaluator": evaluator_fingerprint(record), "verify_ledger": False}}),
        encoding="utf-8")

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--policy", str(policy),
                                 "--json"])
    assert result.exit_code == 0, result.output
    check = next(c for c in json.loads(result.output)["checks"]
                 if c["rule"] == "run.expect_evaluator")
    assert check["passed"] and "matches the pinned fingerprint" in check["detail"]
