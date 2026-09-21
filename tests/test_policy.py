"""Policy files: what they check, and what they refuse to check silently."""
from __future__ import annotations

import json

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import EXIT_UNSTABLE, app
from evalseal.executor import run_eval
from evalseal.ledger import evaluator_fingerprint, seal_and_append
from evalseal.policy import Policy, PolicyError, evaluate, load_policy
from evalseal.report import write_json

runner = CliRunner()


def _record(**scripts):
    scripts = scripts or {
        "stable": ["yes"],
        "flaky": ["yes", "no", "yes", "no", "yes"],
    }
    target = LocalCallableTarget(scripted(scripts))
    return run_eval(make_dataset(*scripts), target, RegexScorer(r"^yes$"), n_repeats=5)


def _write(tmp_path, data, name="policy.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- the policy file itself -------------------------------------------------------

def test_an_unknown_key_is_refused_not_ignored(tmp_path):
    """`min_scor: 0.9` must not read as "no opinion about the score"."""
    path = _write(tmp_path, {"run": {"min_scor": 0.9}})
    with pytest.raises(PolicyError) as e:
        load_policy(path)
    assert "not a valid policy" in str(e.value)


def test_an_out_of_range_threshold_is_refused(tmp_path):
    path = _write(tmp_path, {"run": {"min_score": 1.5}})
    with pytest.raises(PolicyError):
        load_policy(path)


@pytest.mark.parametrize("body,fragment", [
    ("{not json", "not valid JSON"),
    ("", "empty"),
    ("[1, 2]", "mapping at the top level"),
])
def test_malformed_policies_say_what_is_wrong(tmp_path, body, fragment):
    path = tmp_path / "policy.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(PolicyError) as e:
        load_policy(path)
    assert fragment in str(e.value)


def test_an_unknown_extension_is_refused(tmp_path):
    path = tmp_path / "policy.txt"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(PolicyError) as e:
        load_policy(path)
    assert ".json, .yml or .yaml" in str(e.value)


def test_yaml_is_read_when_pyyaml_is_available(tmp_path):
    yaml = pytest.importorskip("yaml")
    path = tmp_path / "evalseal.yml"
    path.write_text("run:\n  min_score: 0.9\n  max_flip_rate: 0.25\n", encoding="utf-8")
    policy = load_policy(path)
    assert policy.run.min_score == 0.9 and policy.run.max_flip_rate == 0.25
    assert yaml is not None


def test_yaml_without_pyyaml_names_the_extra(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_yaml(name, *a, **k):
        if name == "yaml":
            raise ModuleNotFoundError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    path = tmp_path / "evalseal.yml"
    path.write_text("run: {}\n", encoding="utf-8")
    with pytest.raises(PolicyError) as e:
        load_policy(path)
    assert "evalseal[yaml]" in str(e.value)


# --- run rules --------------------------------------------------------------------

def test_passing_checks_are_reported_too():
    """A gate that speaks only on failure cannot be told from one that checks nothing."""
    result = evaluate(Policy.model_validate({"run": {"min_score": 0.5}}), _record())
    assert result.passed
    assert [c.rule for c in result.checks] == ["run.min_score"]
    assert "mean score" in result.checks[0].detail


def test_run_thresholds_fail_with_the_offending_case_named():
    record = _record()
    result = evaluate(
        Policy.model_validate({"run": {"max_flip_rate": 0.1, "min_score": 0.99}}), record)
    assert not result.passed
    rules = {c.rule for c in result.violations}
    assert rules == {"run.max_flip_rate", "run.min_score"}
    worst = next(c for c in result.violations if c.rule == "run.max_flip_rate")
    assert "worst: c1" in worst.detail


def test_max_unstable_cases_counts_the_aggregate():
    record = _record()
    assert evaluate(Policy.model_validate(
        {"run": {"max_unstable_cases": 1}}), record).passed
    assert not evaluate(Policy.model_validate(
        {"run": {"max_unstable_cases": 0}}), record).passed


def test_expect_evaluator_pins_the_grading_side():
    record = _record()
    good = {"run": {"expect_evaluator": evaluator_fingerprint(record)}}
    assert evaluate(Policy.model_validate(good), record).passed
    bad = {"run": {"expect_evaluator": "sha256:nope"}}
    assert not evaluate(Policy.model_validate(bad), record).passed


# --- case rules -------------------------------------------------------------------

def test_a_critical_case_that_flips_fails():
    record = _record()
    assert not evaluate(Policy.model_validate({"cases": {"critical": ["c1"]}}), record).passed
    assert evaluate(Policy.model_validate({"cases": {"critical": ["c0"]}}), record).passed


def test_a_critical_case_missing_from_the_record_fails():
    """Usually the dataset moved out from under the policy, which is worth failing on."""
    result = evaluate(Policy.model_validate({"cases": {"critical": ["gone"]}}), _record())
    assert not result.passed
    assert result.violations[0].rule == "cases.critical[gone]"
    assert result.violations[0].detail == "case not in this record"


def test_per_case_score_floors():
    record = _record()
    assert evaluate(Policy.model_validate(
        {"cases": {"min_score": {"c0": 1.0}}}), record).passed
    result = evaluate(Policy.model_validate(
        {"cases": {"min_score": {"c1": 1.0}}}), record)
    assert not result.passed and "0.600 vs floor 1.000" in result.violations[0].detail


# --- drift rules ------------------------------------------------------------------

def test_a_missing_baseline_fails_rather_than_skipping(tmp_path):
    """A drift rule that cannot run must not report a pass."""
    policy = Policy.model_validate({"drift": {"baseline": "nope.json"}})
    with pytest.raises(PolicyError) as e:
        evaluate(policy, _record(), policy_dir=tmp_path)
    assert "not a skip" in str(e.value)


def test_baseline_is_resolved_relative_to_the_policy_file(tmp_path):
    nested = tmp_path / "ci"
    nested.mkdir()
    write_json(_record(), nested / "baseline.json")
    policy = Policy.model_validate({"drift": {"baseline": "baseline.json"}})
    assert evaluate(policy, _record(), policy_dir=nested).passed


def test_new_instability_can_be_forbidden(tmp_path):
    before = _record(a=["yes"], b=["yes"])
    after = _record(a=["yes"], b=["yes", "no", "yes", "no", "yes"])
    write_json(before, tmp_path / "baseline.json")

    policy = Policy.model_validate(
        {"drift": {"baseline": "baseline.json", "allow_new_unstable": False}})
    result = evaluate(policy, after, policy_dir=tmp_path)
    assert not result.passed
    assert "started flipping" in result.violations[0].detail

    allowed = Policy.model_validate({"drift": {"baseline": "baseline.json"}})
    assert evaluate(allowed, after, policy_dir=tmp_path).passed


def test_a_regression_inside_the_noise_floor_does_not_fail(tmp_path):
    """The allowance is the stated budget plus the noise the runs themselves show."""
    before = _record(a=["yes"], b=["yes"])
    after = _record(a=["yes"], b=["yes", "yes", "yes", "yes", "no"])
    write_json(before, tmp_path / "baseline.json")

    policy = Policy.model_validate(
        {"drift": {"baseline": "baseline.json", "max_score_regression": 0.0}})
    result = evaluate(policy, after, policy_dir=tmp_path)
    check = next(c for c in result.checks if c.rule == "drift.max_score_regression")
    assert check.passed, check.detail
    assert "noise floor" in check.detail


def test_removed_cases_can_be_forbidden(tmp_path):
    before = _record(a=["yes"], b=["yes"])
    after = _record(a=["yes"])
    write_json(before, tmp_path / "baseline.json")

    policy = Policy.model_validate(
        {"drift": {"baseline": "baseline.json", "allow_removed_cases": False,
                   "require_comparable": False}})
    result = evaluate(policy, after, policy_dir=tmp_path)
    assert not result.passed
    assert "left the dataset" in result.violations[-1].detail


# --- the CLI ----------------------------------------------------------------------

def test_cli_gate_with_a_policy(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_record(), ledger, relink=True)
    path = _write(tmp_path, {"run": {"max_flip_rate": 0.1}})

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--policy", str(path)])
    assert result.exit_code == EXIT_UNSTABLE
    assert "run.max_flip_rate" in result.output


def test_cli_gate_prints_the_checks_that_passed(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_record(), ledger, relink=True)
    path = _write(tmp_path, {"run": {"min_score": 0.5, "max_flip_rate": 0.9}})

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--policy", str(path)])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output and "run.min_score" in result.output


def test_cli_rejects_a_broken_policy_instead_of_passing(tmp_path):
    """A typo in the policy must not turn into a green build."""
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_record(), ledger, relink=True)
    path = _write(tmp_path, {"run": {"min_scor": 0.9}})

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--policy", str(path)])
    assert result.exit_code == 2
    assert "Policy error" in result.output


def test_flags_and_policy_are_additive(tmp_path):
    """A flag can tighten a checked-in policy; it must not silently loosen it."""
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_record(), ledger, relink=True)
    path = _write(tmp_path, {"run": {"min_score": 0.5}})

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--policy", str(path),
                                 "--max-flip-rate", "0.1"])
    assert result.exit_code == EXIT_UNSTABLE
    assert "run.min_score" in result.output      # the policy check still ran and passed
    assert "max-flip-rate" in result.output      # and the flag added a failure


def test_cli_gate_json_reports_every_check(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_record(), ledger, relink=True)
    path = _write(tmp_path, {"run": {"min_score": 0.5, "max_flip_rate": 0.1}})

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--policy", str(path),
                                 "--json"])
    assert result.exit_code == EXIT_UNSTABLE
    payload = json.loads(result.output)
    assert payload["passed"] is False
    # verify_ledger defaults on, so the chain is checked even when the policy is silent.
    assert {c["rule"] for c in payload["checks"]} == {
        "run.verify_ledger", "run.min_score", "run.max_flip_rate"}
    assert payload["sealed_hash"].startswith("sha256:")


def test_case_ids_are_not_eaten_as_console_markup(tmp_path):
    """`cases.min_score[c0]` reaches rich as markup; the id must survive printing."""
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_record(), ledger, relink=True)
    path = _write(tmp_path, {"cases": {"min_score": {"c0": 1.0}}})

    result = runner.invoke(app, ["gate", "--ledger", str(ledger), "--policy", str(path)])
    assert result.exit_code == 0, result.output
    assert "cases.min_score[c0]" in result.output


def test_comparability_detail_names_only_evaluator_changes(tmp_path):
    """A changed target model is not an evaluator change, and must not be reported as one."""
    before = _record()
    after = _record()
    after.manifest.target.requested_model = "some-other-model"
    write_json(before, tmp_path / "baseline.json")

    policy = Policy.model_validate({"drift": {"baseline": "baseline.json"}})
    check = evaluate(policy, after, policy_dir=tmp_path).checks[0]
    assert check.passed and check.detail == "same grading setup on both sides"

    after.manifest.scorer.rubric_hash = "sha256:different"
    check = evaluate(policy, after, policy_dir=tmp_path).checks[0]
    assert not check.passed and check.detail == "evaluator changed: rubric"


def test_invalid_yaml_says_so(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "p.yml"
    path.write_text("run:\n  min_score: [unclosed\n", encoding="utf-8")
    with pytest.raises(PolicyError) as e:
        load_policy(path)
    assert "not valid YAML" in str(e.value)


def test_a_yaml_document_with_no_content_is_empty(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "p.yml"
    path.write_text("# only a comment\n---\n", encoding="utf-8")
    with pytest.raises(PolicyError) as e:
        load_policy(path)
    assert "empty" in str(e.value)


def test_fail_on_policy_selects_by_stability_class():
    record = _record()
    assert evaluate(Policy.model_validate({"run": {"fail_on": "none"}}), record).passed
    result = evaluate(Policy.model_validate({"run": {"fail_on": "unstable"}}), record)
    assert not result.passed
    assert "violate fail_on=unstable" in result.violations[0].detail


def test_a_per_case_floor_for_a_case_that_is_gone_fails():
    result = evaluate(
        Policy.model_validate({"cases": {"min_score": {"vanished": 0.5}}}), _record())
    assert not result.passed
    assert result.violations[0].detail == "case not in this record"


def test_policy_result_serializes_checks_and_violations():
    result = evaluate(
        Policy.model_validate({"run": {"min_score": 0.99, "max_flip_rate": 0.9}}), _record())
    payload = result.to_dict()
    assert payload["passed"] is False
    assert len(payload["checks"]) == 2 and len(payload["violations"]) == 1
    assert payload["violations"][0]["rule"] == "run.min_score"
