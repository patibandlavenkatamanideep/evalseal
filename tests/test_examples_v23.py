"""The v2.3 examples, with every number their READMEs quote asserted.

An example whose output has drifted from its README teaches the wrong thing
confidently. These tests read the committed fixtures and check the figures the prose
promises, so the prose cannot rot quietly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalseal.agent_artifacts import (
    describe_paths,
    tool_path_digest,
    validate_agent_artifact,
)
from evalseal.diffing import diff_records, load_receipt
from evalseal.drift import EVALUATOR_DRIFT, analyze_drift
from evalseal.evidence import DIRECTIONAL, MODERATE, STRONGER, analyze_evidence

REPO = Path(__file__).resolve().parent.parent
EVIDENCE = REPO / "examples" / "evidence"
DRIFT = REPO / "examples" / "not-comparable-drift"
AGENT = REPO / "examples" / "agent-retry-path"


def _readme(d: Path) -> str:
    return (d / "README.md").read_text(encoding="utf-8")


# --- examples/evidence ----------------------------------------------------------------

def test_the_two_receipts_differ_only_in_repeat_count():
    five, thirty_one = (load_receipt(EVIDENCE / n)
                        for n in ("receipt-n5.json", "receipt-n31.json"))
    assert five.manifest.run_config.n_repeats == 5
    assert thirty_one.manifest.run_config.n_repeats == 31
    # Same grading on both sides, or the example would be teaching drift instead.
    assert diff_records(five, thirty_one).comparable


def test_the_flaky_case_really_does_flip_one_run_in_five():
    """The README's whole argument rests on the true rate being 0.20."""
    record = load_receipt(EVIDENCE / "receipt-n31.json")
    flaky = next(c for c in record.results if c.flip_count)
    assert flaky.n_runs == 31
    assert flaky.pass_count == 25           # 31 runs of a 4-pass-then-1-fail cycle
    assert flaky.flip_count == 6


@pytest.mark.parametrize("name,expected_suite_label,expected_underpowered", [
    ("receipt-n5.json", DIRECTIONAL, 2),
    ("receipt-n31.json", MODERATE, 0),
])
def test_the_suite_labels_the_readme_quotes(name, expected_suite_label,
                                            expected_underpowered):
    ev = analyze_evidence(load_receipt(EVIDENCE / name))
    assert ev.label == expected_suite_label
    assert len(ev.underpowered) == expected_underpowered


def test_the_flip_rate_ranges_the_readme_quotes():
    """These intervals are the point of the example, so they are pinned exactly."""
    at5 = {c.case_id: c for c in analyze_evidence(load_receipt(
        EVIDENCE / "receipt-n5.json")).cases}
    at31 = {c.case_id: c for c in analyze_evidence(load_receipt(
        EVIDENCE / "receipt-n31.json")).cases}

    steady5, flaky5 = at5["c0"], at5["c1"]
    steady31, flaky31 = at31["c0"], at31["c1"]

    def shown(ci):
        """What the README tabulates: the CLI's two-decimal rendering."""
        return (round(ci[0], 2), round(ci[1], 2))

    assert shown(steady5.flip_rate_ci) == (0.0, 0.43)      # five unanimous runs
    assert shown(flaky5.flip_rate_ci) == (0.04, 0.5)
    assert shown(steady31.flip_rate_ci) == (0.0, 0.11)
    assert shown(flaky31.flip_rate_ci) == (0.09, 0.36)

    # The headline claim: more repeats narrow the interval even as the point estimate
    # moves slightly further from the truth.
    assert flaky5.flip_rate == 0.2 and flaky31.flip_rate == pytest.approx(0.1935, abs=1e-3)
    assert (flaky31.flip_rate_ci[1] - flaky31.flip_rate_ci[0]) < (
        flaky5.flip_rate_ci[1] - flaky5.flip_rate_ci[0])


def test_the_labels_per_case_the_readme_tabulates():
    at31 = {c.case_id: c.label for c in analyze_evidence(load_receipt(
        EVIDENCE / "receipt-n31.json")).cases}
    assert at31["c0"] == STRONGER
    assert at31["c1"] == MODERATE


def test_the_repeat_counts_the_readme_quotes():
    ev = analyze_evidence(load_receipt(EVIDENCE / "receipt-n5.json"),
                          target_margin=0.10)
    steady = next(c for c in ev.cases if c.case_id == "c0")
    assert steady.repeats_worst_case == 93
    assert steady.repeats_at_observed_rate == 16


def test_the_evidence_readme_states_what_it_does_not_tell_you():
    text = _readme(EVIDENCE)
    assert "does not tell you" in text
    assert "precisely measured nonsense" in text
    assert "evalseal power" in text          # points at the other question


# --- examples/not-comparable-drift ----------------------------------------------------

def test_the_two_runs_share_a_target_and_differ_only_in_the_grader():
    before, after = (load_receipt(DRIFT / n) for n in ("baseline.json", "candidate.json"))
    assert before.manifest.target.requested_model == after.manifest.target.requested_model
    assert before.manifest.scorer.config_hash != after.manifest.scorer.config_hash
    assert before.manifest.dataset.hash == after.manifest.dataset.hash


def test_the_forty_point_jump_the_readme_opens_with():
    before, after = (load_receipt(DRIFT / n) for n in ("baseline.json", "candidate.json"))
    assert before.aggregate.mean_score == 0.6
    assert after.aggregate.mean_score == 1.0
    result = diff_records(before, after)
    assert result.score_delta == pytest.approx(0.4)


def test_diff_refuses_the_comparison_and_names_the_grader():
    before, after = (load_receipt(DRIFT / n) for n in ("baseline.json", "candidate.json"))
    result = diff_records(before, after)
    assert not result.comparable
    assert result.score_verdict == "non_comparable"
    assert [c.name for c in result.evaluator_changes] == ["scorer settings"]
    # The target model must not be blamed: it did not move.
    assert "target model" not in [c.name for c in result.evaluator_changes]


def test_drift_calls_it_evaluator_drift_and_refuses_to_credit_the_model():
    before, after = (load_receipt(DRIFT / n) for n in ("baseline.json", "candidate.json"))
    report = analyze_drift(before, after)
    assert report.kind == EVALUATOR_DRIFT
    summary = report.summary()
    assert "did not measure the same thing" in summary
    assert "none of that can be attributed to the model under test" in summary
    for credit in ("improved", "better", "worse", "regression"):
        assert credit not in summary.lower()


def test_the_drift_readme_explains_why_the_target_is_excluded():
    text = _readme(DRIFT)
    assert "excludes the target model" in text
    assert "target_change" in text


# --- examples/agent-retry-path --------------------------------------------------------

def test_both_agent_artifacts_validate():
    assert validate_agent_artifact("tool_acl", AGENT / "tool-manifest.json") == []
    assert validate_agent_artifact("tool_responses", AGENT / "tool-responses.json") == []


def test_the_responses_bind_to_the_manifest():
    import hashlib

    manifest = json.loads((AGENT / "tool-manifest.json").read_text(encoding="utf-8"))
    responses = json.loads((AGENT / "tool-responses.json").read_text(encoding="utf-8"))
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    assert responses["manifest_sha256"] == "sha256:" + hashlib.sha256(blob).hexdigest()


def test_the_two_attempts_the_readme_describes():
    responses = json.loads((AGENT / "tool-responses.json").read_text(encoding="utf-8"))
    rows = {r["attempt_id"]: r for r in describe_paths(responses)}
    assert set(rows) == {"attempt-a", "attempt-b"}

    a, b = rows["attempt-a"], rows["attempt-b"]
    assert (a["n_calls"], a["n_retries"], a["n_errors"]) == (3, 1, 1)
    assert (b["n_calls"], b["n_retries"], b["n_errors"]) == (2, 0, 0)


def test_same_final_state_different_path_is_visible():
    """The claim the example exists to demonstrate."""
    responses = json.loads((AGENT / "tool-responses.json").read_text(encoding="utf-8"))
    rows = {r["attempt_id"]: r for r in describe_paths(responses)}
    assert rows["attempt-a"]["path_digest"] != rows["attempt-b"]["path_digest"]
    # Both end on a successful write, which is what makes the scores agree.
    for attempt in ("attempt-a", "attempt-b"):
        calls = [c for c in responses["calls"] if c["attempt_id"] == attempt]
        assert max(calls, key=lambda c: c["seq"])["outcome"] == "ok"


def test_reordering_calls_changes_the_path_digest():
    """`path_digest` must be order-sensitive or it cannot show a changed call order."""
    responses = json.loads((AGENT / "tool-responses.json").read_text(encoding="utf-8"))
    calls = [c for c in responses["calls"] if c["attempt_id"] == "attempt-b"]
    swapped = [dict(c, seq=1 - c["seq"]) for c in calls]
    assert tool_path_digest(calls) != tool_path_digest(swapped)


def test_changing_only_a_response_changes_the_digest_too():
    responses = json.loads((AGENT / "tool-responses.json").read_text(encoding="utf-8"))
    calls = [c for c in responses["calls"] if c["attempt_id"] == "attempt-b"]
    edited = [dict(c) for c in calls]
    edited[0]["input_sha256"] = "sha256:" + "0" * 64
    assert tool_path_digest(calls) != tool_path_digest(edited)


def test_the_agent_readme_keeps_the_boundary_loud():
    # Whitespace-normalised: the boundary sentence wraps across lines in the prose.
    lowered = " ".join(_readme(AGENT).lower().split())
    assert "does not know whether the harness omitted calls or lied about calls" in lowered
    assert "no tool loop" in lowered
    assert "does not replay agents" in lowered
    assert "derived, never sealed" in lowered


# --- every example must be runnable without a key -------------------------------------

@pytest.mark.parametrize("d", [EVIDENCE, DRIFT, AGENT])
def test_no_example_needs_an_api_key(d):
    text = _readme(d)
    assert "No API key" in text or "no API key" in text
    for leak in ("sk-ant-", "sk-", "API_KEY="):
        assert leak not in text
