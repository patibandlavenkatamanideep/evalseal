"""A number that cannot be a measurement must not be read as one.

The receipt models carried no constraints: no bounds, no finiteness, no check that the
summary matched the cases it summarises. So invalid data did not fail - it passed,
because the comparisons a gate makes are false for a value that is not a number, and
absent for a summary nobody cross-checks.

What the baseline did, each reproduced below:

| input | baseline |
|---|---|
| `aggregate.mean_score = 42.0` | `gate --min-score 0.9` exits **0** |
| `n_cases = 999` with 2 results | `gate --min-score 0.95` exits **0** |
| two cases with the same `case_id` | accepted, chain verifies |
| no cases at all | accepted |
| a non-finite score | written as `null`, receipt then **unloadable** |
| `{"min_score": 0.9, "min_score": 0.0}` in a policy | enforced as **0.0** |

The last one needs no tampering and no hash break: a policy file is committed and read
by people, and nothing hashes it. A reviewer approving `0.9` is approving a file that
enforces `0.0`, because JSON's last-member-wins is silent.

Policy *thresholds* were already guarded - `ge=0.0, le=1.0` rejects NaN, Infinity and
42.0, and `extra="forbid"` rejects a typo. Those are asserted here too, so the
protection cannot be removed quietly.
"""
from __future__ import annotations

import json

import pytest
from conftest import make_dataset, scripted

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.executor import run_eval
from evalseal.ledger import _content_hash
from evalseal.models import RunRecord


def _fresh():
    return run_eval(
        make_dataset("a", "b"),
        LocalCallableTarget(scripted({"a": ["yes"], "b": ["yes"]})),
        RegexScorer(r"^yes$"), n_repeats=5)


def _seal(record, path):
    record.hash = _content_hash(record)
    path.write_text(json.dumps(json.loads(record.model_dump_json())) + "\n",
                    encoding="utf-8")
    return path


def _run(args):
    from typer.testing import CliRunner

    from evalseal.cli import app

    return CliRunner().invoke(app, args)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _problems(record):
    from evalseal.validation import validate_record

    return {p.name: p for p in validate_record(record)}


# --- what a valid receipt must keep doing ---------------------------------------------

def test_a_valid_receipt_has_no_problems():
    assert _problems(_fresh()) == {}


def test_a_valid_receipt_passes_the_gate(tmp_path):
    from evalseal.ledger import seal_and_append

    ledger = tmp_path / "l.jsonl"
    seal_and_append(_fresh(), ledger, relink=True)
    result = _run(["gate", "--ledger", str(ledger), "--json"])
    payload = json.loads(result.output)
    rules = {c["rule"]: c for c in payload["checks"]}
    assert rules["ledger.data_invariants"]["passed"], rules["ledger.data_invariants"]


# --- A4-2: scores that cannot be scores -----------------------------------------------

@pytest.mark.parametrize("value", [42.0, -5.0, 1.5])
def test_an_out_of_range_mean_is_a_problem(value):
    record = _fresh()
    record.aggregate.mean_score = value
    assert "aggregate.mean_score" in _problems(record)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_mean_is_a_problem(value):
    record = _fresh()
    record.aggregate.mean_score = value
    problem = _problems(record)["aggregate.mean_score"]
    assert "finite" in problem.detail


def test_an_out_of_range_case_score_is_a_problem():
    record = _fresh()
    record.results[0].scores[0] = 9.0
    assert any(p.startswith("results[0].scores") for p in _problems(record))


def test_a_mean_of_42_does_not_pass_a_min_score_gate(tmp_path):
    """The reproduction: `NaN`-style arithmetic is not needed, 42.0 simply clears 0.9."""
    record = _fresh()
    record.aggregate.mean_score = 42.0
    ledger = _seal(record, tmp_path / "big.jsonl")
    result = _run(["gate", "--ledger", str(ledger), "--min-score", "0.9"])
    assert result.exit_code != 0, result.output
    assert "FAIL ledger.data_invariants" in _flat(result.output)


def test_a_non_finite_flip_rate_does_not_pass_a_max_flip_rate_gate(tmp_path):
    """Every comparison against NaN is False, so a NaN flip rate cleared any ceiling."""
    record = _fresh()
    record.results[0].flip_rate = float("nan")
    assert any("flip_rate" in p for p in _problems(record))
    assert not (float("nan") > 0.0), "which is why it has to be caught before the compare"


# --- A4-3: a summary that contradicts the cases ---------------------------------------

def test_a_case_count_that_disagrees_with_the_cases_is_a_problem():
    record = _fresh()
    record.aggregate.n_cases = 999
    assert "aggregate.n_cases" in _problems(record)


def test_a_mean_that_disagrees_with_the_cases_is_a_problem():
    record = _fresh()
    record.aggregate.mean_score = 0.0 if record.aggregate.mean_score > 0.5 else 1.0
    assert "aggregate.mean_score" in _problems(record)


def test_stability_counts_must_add_up():
    record = _fresh()
    record.aggregate.n_stable += 7
    assert "aggregate.stability_counts" in _problems(record)


def test_a_fabricated_summary_does_not_pass_a_min_score_gate(tmp_path):
    record = _fresh()
    record.aggregate.n_cases = 999
    record.aggregate.mean_score = 1.0
    ledger = _seal(record, tmp_path / "incons.jsonl")
    result = _run(["gate", "--ledger", str(ledger), "--min-score", "0.95"])
    assert result.exit_code != 0, result.output
    assert "FAIL ledger.data_invariants" in _flat(result.output)


# --- A4-4 and A4-5: case identity and emptiness ---------------------------------------

def test_duplicate_case_ids_are_a_problem():
    record = _fresh()
    record.results[1].case_id = record.results[0].case_id
    problem = _problems(record)["results.case_id"]
    assert record.results[0].case_id in problem.detail


def test_an_empty_case_set_is_a_problem():
    payload = json.loads(_fresh().model_dump_json())
    payload["results"] = []
    payload["aggregate"]["n_cases"] = 0
    record = RunRecord.model_validate(payload)
    assert "results" in _problems(record)


def test_an_empty_case_set_does_not_pass_the_gate(tmp_path):
    payload = json.loads(_fresh().model_dump_json())
    payload["results"] = []
    payload["aggregate"].update(n_cases=0, mean_score=1.0, n_stable=0,
                                n_borderline=0, n_unstable=0)
    record = RunRecord.model_validate(payload)
    ledger = _seal(record, tmp_path / "empty.jsonl")
    result = _run(["gate", "--ledger", str(ledger), "--min-score", "0.5"])
    assert result.exit_code != 0, result.output


# --- A4-1: duplicate JSON members -----------------------------------------------------

def test_strict_json_rejects_a_duplicate_member():
    from evalseal.validation import InvalidInput, strict_json_loads

    with pytest.raises(InvalidInput, match="min_score"):
        strict_json_loads('{"run": {"min_score": 0.9, "min_score": 0.0}}', what="policy")


def test_strict_json_rejects_a_duplicate_member_at_the_top_level():
    from evalseal.validation import InvalidInput, strict_json_loads

    with pytest.raises(InvalidInput, match="hash"):
        strict_json_loads('{"hash": "a", "hash": "b"}', what="receipt")


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_strict_json_rejects_non_finite_literals(literal):
    from evalseal.validation import InvalidInput, strict_json_loads

    with pytest.raises(InvalidInput, match="finite"):
        strict_json_loads(f'{{"x": {literal}}}', what="receipt")


def test_strict_json_accepts_ordinary_json():
    from evalseal.validation import strict_json_loads

    assert strict_json_loads('{"a": [1, 2.5, null, true], "b": {"c": "d"}}',
                             what="x") == {"a": [1, 2.5, None, True], "b": {"c": "d"}}


def test_a_policy_with_a_duplicate_member_is_rejected(tmp_path):
    """A reviewer reads 0.9 and the gate used to enforce 0.0."""
    from evalseal.policy import PolicyError, load_policy

    path = tmp_path / "policy.json"
    path.write_text('{"run": {"min_score": 0.9, "min_score": 0.0}}', encoding="utf-8")
    with pytest.raises(PolicyError, match="min_score"):
        load_policy(path)


def test_a_preregistration_with_a_duplicate_member_is_rejected(tmp_path):
    from evalseal.prereg import PreregError, load_preregistration

    valid = json.loads(_valid_prereg())
    body = json.dumps(valid)
    doubled = body[:-1] + ', "n_repeats": 1}'
    path = tmp_path / "prereg.json"
    path.write_text(doubled, encoding="utf-8")
    with pytest.raises(PreregError, match="n_repeats"):
        load_preregistration(path)


def _valid_prereg() -> str:
    from evalseal.prereg import build_preregistration

    return build_preregistration(n_repeats=20).to_json()


# --- the protections that already existed, pinned so they cannot be removed -----------

@pytest.mark.parametrize("body", [
    '{"run": {"min_score": NaN}}',
    '{"run": {"max_flip_rate": Infinity}}',
    '{"run": {"min_score": 42.0}}',
    '{"run": {"min_score": -1.0}}',
    '{"run": {"mni_score": 0.9}}',
])
def test_a_policy_threshold_that_cannot_be_a_threshold_is_rejected(tmp_path, body):
    from evalseal.policy import PolicyError, load_policy

    path = tmp_path / "policy.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


# --- A4-6 and A4-7: invalid input is named, not raised at the user --------------------

def test_a_non_finite_score_is_refused_at_seal_time(tmp_path):
    """It used to be written as `null`, which made the sealed receipt unloadable: the
    measurement was lost and every later command raised a validation error."""
    from evalseal.ledger import seal_and_append

    record = _fresh()
    record.results[0].mean = float("nan")
    with pytest.raises(ValueError, match="finite"):
        seal_and_append(record, tmp_path / "l.jsonl", relink=True)


def test_malformed_json_is_reported_without_a_traceback(tmp_path):
    from evalseal.cli import EXIT_BAD_INPUT

    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json at all\n", encoding="utf-8")
    for command in ("gate", "verify"):
        result = _run([command, "--ledger", str(bad)])
        assert result.exit_code == EXIT_BAD_INPUT, (command, result.output)
        assert "Traceback" not in result.output, command
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{command} raised {result.exception!r} at the user")


def test_a_ledger_with_a_duplicate_member_is_reported_cleanly(tmp_path):
    from evalseal.cli import EXIT_BAD_INPUT

    ledger = _seal(_fresh(), tmp_path / "l.jsonl")
    line = ledger.read_text().strip()
    ledger.write_text(line[:-1] + ', "created_at": "2000-01-01T00:00:00+00:00"}\n',
                      encoding="utf-8")
    result = _run(["verify", "--ledger", str(ledger)])
    assert result.exit_code == EXIT_BAD_INPUT, result.output
    assert "created_at" in _flat(result.output)
    assert "Traceback" not in result.output


# --- the remaining invariants ---------------------------------------------------------

def test_an_inverted_confidence_interval_is_a_problem():
    record = _fresh()
    record.results[0].ci95 = (0.9, 0.1)
    problem = _problems(record)["results[0].ci95"]
    assert "inverted" in problem.detail


def test_an_out_of_range_interval_bound_is_a_problem():
    record = _fresh()
    record.results[0].ci95 = (-0.5, 1.0)
    assert "results[0].ci95[0]" in _problems(record)


def test_verdict_counts_must_match_the_verdicts():
    record = _fresh()
    record.results[0].pass_count += 3
    assert "results[0].verdict_counts" in _problems(record)


def test_more_flips_than_runs_is_a_problem():
    record = _fresh()
    record.results[0].flip_count = len(record.results[0].scores) + 1
    assert "results[0].flip_count" in _problems(record)


def test_a_case_mean_that_disagrees_with_its_scores_is_a_problem():
    record = _fresh()
    record.results[0].mean = 0.0 if record.results[0].mean > 0.5 else 1.0
    assert "results[0].mean" in _problems(record)


def test_a_value_that_is_not_a_number_at_all_is_a_problem():
    """`True` is an `int` in Python, so a bool would otherwise pass a numeric check
    and compare as 1.0 against a threshold."""
    from evalseal.validation import _finite_in_range

    assert _finite_in_range(True, "x") is not None
    assert "not a number" in _finite_in_range(True, "x").detail
    assert _finite_in_range("0.5", "x") is not None


def test_a_problem_serialises():
    from evalseal.validation import Problem

    assert Problem("a.b", "why").to_dict() == {"name": "a.b", "detail": "why"}


def test_require_valid_lists_the_first_few_and_counts_the_rest():
    from evalseal.validation import InvalidInput, require_valid

    record = _fresh()
    for case in record.results:
        case.mean = 42.0
        case.flip_rate = 7.0
    record.aggregate.mean_score = 99.0
    with pytest.raises(InvalidInput, match="data invariant"):
        require_valid(record)


def test_require_valid_passes_a_good_record():
    from evalseal.validation import require_valid

    require_valid(_fresh())


def test_a_ledger_line_that_is_not_an_object_is_an_input_error(tmp_path):
    from evalseal.ledger import load_all
    from evalseal.validation import InvalidInput

    path = tmp_path / "l.jsonl"
    path.write_text('["not", "a", "record"]\n', encoding="utf-8")
    with pytest.raises(InvalidInput, match="not a record object"):
        load_all(path)


def test_a_ledger_line_missing_fields_is_an_input_error(tmp_path):
    from evalseal.ledger import load_all
    from evalseal.validation import InvalidInput

    path = tmp_path / "l.jsonl"
    path.write_text('{"manifest": {}}\n', encoding="utf-8")
    with pytest.raises(InvalidInput, match="not a valid record"):
        load_all(path)
