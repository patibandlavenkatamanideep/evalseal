"""examples/not-comparable-drift: a 40-point jump caused entirely by the grader.

Ported from PR #12 (see docs/pr-12-disposition.md, item 11). An example whose output
has drifted from its README teaches the wrong thing confidently, so every number the
prose promises is asserted here.

Both receipts were produced by a local function with no network, and neither has been
regenerated: they are the bytes PR #12 committed, and the schema note in the drift
report names the version they were sealed under.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from evalseal.diffing import diff_records, load_receipt
from evalseal.drift import EVALUATOR_DRIFT, analyze_drift
from evalseal.policy import Policy, evaluate

DRIFT = Path(__file__).resolve().parent.parent / "examples" / "not-comparable-drift"


def _pair():
    return tuple(load_receipt(DRIFT / n) for n in ("baseline.json", "candidate.json"))


def test_the_two_runs_share_a_target_and_differ_only_in_the_grader():
    before, after = _pair()
    assert before.manifest.target.requested_model == after.manifest.target.requested_model
    assert before.manifest.scorer.config_hash != after.manifest.scorer.config_hash
    assert before.manifest.dataset.hash == after.manifest.dataset.hash


def test_the_forty_point_jump_the_readme_opens_with():
    before, after = _pair()
    assert before.aggregate.mean_score == 0.6
    assert after.aggregate.mean_score == 1.0
    assert diff_records(before, after).score_delta == pytest.approx(0.4)


def test_diff_refuses_the_comparison_and_names_the_grader():
    result = diff_records(*_pair())
    assert not result.comparable
    assert result.score_verdict == "non_comparable"
    assert [c.name for c in result.evaluator_changes] == ["scorer settings"]
    assert "target model" not in [c.name for c in result.evaluator_changes]


def test_drift_calls_it_evaluator_drift_and_refuses_to_credit_the_model():
    report = analyze_drift(*_pair())
    assert report.kind == EVALUATOR_DRIFT
    assert report.grounds == ["the evaluator fingerprint differs"]
    summary = report.summary()
    assert "did not measure the same thing" in summary
    assert "none of that can be attributed to the model under test" in summary
    for credit in ("improved", "better", "worse", "regression"):
        assert credit not in summary.lower()


def test_the_schema_note_names_the_versions_it_is_talking_about():
    """It used to say "before schema 1.4" for a pair sealed under 1.5 and 1.5."""
    note = analyze_drift(*_pair()).schema_note
    assert "before schema 1.6" in note
    assert "(Sealed under 1.5 and 1.5.)" in note


def test_the_pre_1_7_receipts_are_reported_as_not_saying_where_the_numbers_came_from():
    notes = analyze_drift(*_pair()).notes
    assert any("sealed before schema 1.7" in n for n in notes)


def test_independence_is_unknown_for_these_receipts_rather_than_assumed():
    from evalseal.independence import UNKNOWN, audit_independence

    for record in _pair():
        audit = audit_independence(record)
        assert audit.verdict == UNKNOWN
        assert "No effective sample size is derived" in audit.summary()


def test_the_policy_the_readme_suggests_fails_the_build():
    _, after = _pair()
    policy = Policy.model_validate({
        "run": {"verify_ledger": False},
        "drift": {"baseline": "baseline.json", "require_comparable": True}})
    result = evaluate(policy, after, policy_dir=DRIFT)
    assert not result.passed
    violation = next(c for c in result.violations
                     if c.rule == "drift.require_comparable")
    assert "evaluator changed: scorer settings" in violation.detail


def test_the_readme_explains_why_the_target_is_excluded():
    text = (DRIFT / "README.md").read_text(encoding="utf-8")
    assert "excludes the target model" in text
    assert "target_change" in text
    for value in ("0.600", "1.000", "+0.400", "2 of 5"):
        assert value in text, f"the README no longer quotes {value!r}"


def test_both_receipts_verify_under_the_schema_that_sealed_them():
    """Sealed under 1.5, re-hashed by a 1.8 build. Invariant 4, concretely.

    They arrived from PR #12 with `hash: ""` - never sealed - which `check_record_hash`
    reported as `tampered`, i.e. "content does not match its stored hash", about files
    nobody had altered. They are sealed now, and a record with no hash reports
    `unsealed` under its own name.
    """
    from evalseal.ledger import check_record_hash

    for record in _pair():
        assert record.manifest.schema_version == "1.5"
        assert check_record_hash(record) == ("legacy", "1.5")


def test_a_record_with_no_hash_is_unsealed_rather_than_tampered():
    from evalseal.ledger import check_record_hash, verify_chain

    record = _pair()[0].model_copy(deep=True)
    record.hash = ""
    assert check_record_hash(record) == ("unsealed", "1.5")

    import pathlib
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "l.jsonl"
        path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
        ok, message = verify_chain(path)
    assert not ok                        # it still fails: nothing can check it
    assert "never sealed" in message
    assert "TAMPER" not in message
    assert "not tampering" in message
