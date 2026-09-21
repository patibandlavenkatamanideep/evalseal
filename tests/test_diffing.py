"""Drift comparison: what moved, and whether the two runs can be compared at all."""
from __future__ import annotations

import json

import pytest
from conftest import scripted
from typer.testing import CliRunner

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import LLMJudgeScorer, RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.diffing import diff_records, load_receipt
from evalseal.executor import run_eval
from evalseal.ledger import config_fingerprint, evaluator_fingerprint, seal_and_append
from evalseal.report import render_diff, write_json

runner = CliRunner()


def _run(scripts: dict[str, list[str]], model_name: str = "local", rubric: str | None = None):
    """Case ids are the prompts, so a changed case set is visible as a changed id set."""
    target = LocalCallableTarget(scripted(scripts), name=model_name)
    ds = Dataset([Case(p, p) for p in scripts], hash="sha256:" + "-".join(scripts))
    if rubric is None:
        return run_eval(ds, target, RegexScorer(r"^yes$"), n_repeats=5)
    judge = LocalCallableTarget(lambda p: "PASS", name="judge")
    return run_eval(ds, target, LLMJudgeScorer(judge=judge, rubric=rubric), n_repeats=5)


def test_same_grading_different_model_is_comparable():
    """The comparison a benchmark exists to make must not be flagged incomparable."""
    before = _run({"q": ["yes"]}, model_name="model-a")
    after = _run({"q": ["yes"]}, model_name="model-b")

    assert evaluator_fingerprint(before) == evaluator_fingerprint(after)
    assert config_fingerprint(before) != config_fingerprint(after)   # the target did change
    assert diff_records(before, after).comparable


def test_changed_rubric_is_not_comparable():
    before = _run({"q": ["yes"]}, rubric="Be strict.")
    after = _run({"q": ["yes"]}, rubric="Be lenient.")

    result = diff_records(before, after)
    assert not result.comparable
    assert {c.name for c in result.config_changes} >= {"rubric", "judge prompt"}


def test_score_and_flip_rate_deltas():
    before = _run({"a": ["yes"], "b": ["yes"]})
    after = _run({"a": ["yes"], "b": ["yes", "no", "yes", "no", "yes"]})

    result = diff_records(before, after)
    assert result.score_before == 1.0
    assert result.score_after < 1.0
    assert result.score_delta < 0
    assert result.flip_rate_after > result.flip_rate_before


def test_unstable_case_sets_are_partitioned():
    before = _run({"a": ["yes", "no", "yes", "no", "yes"], "b": ["yes"]})
    after = _run({"a": ["yes"], "b": ["yes", "no", "yes", "no", "yes"]})

    result = diff_records(before, after)
    assert result.now_stable == ["a"]
    assert result.newly_unstable == ["b"]
    assert result.still_unstable == []


def test_instability_is_read_from_older_receipts_too():
    """`flip_count` arrived in schema 1.2 and defaults to 0, so a pre-1.2 receipt would
    report nothing unstable if the diff keyed on it. `flip_rate` has always been there."""
    before = _run({"a": ["yes"]})
    after = _run({"a": ["yes", "no", "yes", "no", "yes"]})
    for case in after.results:
        case.flip_count = 0                 # what an old record deserializes to

    result = diff_records(before, after)
    assert result.newly_unstable == ["a"]
    assert "cases that flipped | 0 | 1 | +1" in render_diff(result)


def test_added_and_removed_cases_are_listed():
    before = _run({"a": ["yes"], "b": ["yes"]})
    after = _run({"b": ["yes"], "c": ["yes"]})

    result = diff_records(before, after)
    assert result.cases_added == ["c"]
    assert result.cases_removed == ["a"]


def test_score_verdict_is_inconclusive_rather_than_a_claim_of_sameness():
    before = _run({"a": ["yes"]})
    after = _run({"a": ["yes"]})
    assert diff_records(before, after).score_verdict == "inconclusive"

    incomparable = diff_records(_run({"a": ["yes"]}, rubric="A"), _run({"a": ["yes"]}, rubric="B"))
    assert incomparable.score_verdict == "not comparable"


def test_a_suite_wide_shift_is_now_detected_where_the_old_floor_hid_it():
    """Eight of forty items regress: delta -0.2, under the 0.217 per-case half-width
    that used to be the threshold. p = 2 * 0.5**8 = 0.0078."""
    ids = [f"i{k:02d}" for k in range(40)]
    before = _run({i: ["yes"] for i in ids})
    after = _run({**{i: ["yes"] for i in ids}, **{i: ["no"] for i in ids[:8]}})

    result = diff_records(before, after)
    assert result.score_verdict == "regression"
    assert result.paired.p_value < 0.01
    assert abs(result.score_delta) < result.per_case_halfwidth


def test_per_case_halfwidth_is_still_reported_under_its_own_name():
    result = diff_records(_run({"a": ["yes"]}), _run({"a": ["yes"]}))
    assert result.per_case_halfwidth == pytest.approx(0.2172, abs=5e-4)
    assert "per_case_halfwidth" in result.to_dict()["score"]
    assert "noise_floor" not in result.to_dict()["score"]


def test_render_names_what_changed():
    text = render_diff(diff_records(_run({"q": ["yes"]}, rubric="A"),
                                    _run({"q": ["yes"]}, rubric="B")))
    assert "Not directly comparable" in text and "rubric" in text

    same = render_diff(diff_records(_run({"q": ["yes"]}), _run({"q": ["yes"]})))
    assert "Comparable:** yes" in same


def test_cli_diffs_two_receipt_files(tmp_path):
    a, b = tmp_path / "before.json", tmp_path / "after.json"
    write_json(_run({"q": ["yes"]}), a)
    write_json(_run({"q": ["yes", "no", "yes", "no", "yes"]}), b)

    result = runner.invoke(app, ["diff", str(a), str(b), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["comparable"] is True
    assert payload["score"]["delta"] < 0
    assert payload["unstable_cases"]["newly_unstable"]


def test_cli_still_accepts_ledger_indices(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_run({"q": ["yes"]}), ledger, relink=True)
    seal_and_append(_run({"q": ["yes", "no", "yes", "no", "yes"]}), ledger, relink=True)

    result = runner.invoke(app, ["diff", "--ledger", str(ledger), "--", "0", "-1"])
    assert result.exit_code == 0, result.output
    assert "score" in result.output


def test_cli_rejects_a_token_that_is_neither(tmp_path):
    result = runner.invoke(app, ["diff", "nope.json", "0", "--ledger", str(tmp_path / "l.jsonl")])
    assert result.exit_code == 2
    assert "neither an existing file nor a ledger index" in result.output


def test_load_receipt_reads_both_a_receipt_and_a_ledger(tmp_path):
    record = _run({"q": ["yes"]})
    receipt, ledger = tmp_path / "r.json", tmp_path / "l.jsonl"
    write_json(record, receipt)                 # pretty-printed, multi-line
    seal_and_append(record, ledger, relink=True)  # one compact record per line

    assert load_receipt(receipt).aggregate.mean_score == record.aggregate.mean_score
    assert load_receipt(ledger).hash               # the ledger head


def test_load_receipt_takes_the_head_of_a_multi_entry_ledger(tmp_path):
    ledger = tmp_path / "l.jsonl"
    first = _run({"q": ["yes"]})
    seal_and_append(first, ledger, relink=True)
    second = _run({"q": ["yes", "no", "yes", "no", "yes"]})
    seal_and_append(second, ledger, relink=True)

    head = load_receipt(ledger)
    assert head.hash == second.hash != first.hash
    assert head.prev_hash == first.hash


def test_load_receipt_rejects_an_empty_file(tmp_path):
    empty = tmp_path / "nothing.json"
    empty.write_text("")
    with pytest.raises(ValueError, match="empty"):
        load_receipt(empty)


def test_a_removed_case_is_not_reported_as_having_become_stable():
    """Dropping a flaky case from the dataset is not the same as fixing it.

    `now_stable` is a set difference over the unstable ids, so a case that left the
    dataset entirely used to appear there, which reads as an improvement.
    """
    before = _run({"keep": ["yes"], "dropped": ["yes", "no", "yes", "no", "yes"]})
    after = _run({"keep": ["yes"]})

    result = diff_records(before, after)
    assert result.cases_removed == ["dropped"]
    assert result.now_stable == []
    assert "dropped" in result.unstable_before      # still reported as it was
