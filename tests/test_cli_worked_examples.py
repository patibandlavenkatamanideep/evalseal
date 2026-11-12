"""Two worked examples small enough to check by hand, driven through the CLI.

Every expected number below is derived from the definitions in docs/metrics.md, in the
docstring, before the assertion that checks it - not read off the implementation. If a
number here disagrees with the code, the code is what is wrong.

The point of driving them through the CLI is that the three things a reviewer sees -
the JSON, the human-readable label, and the exit code - have to agree. A gate that
prints a regression and exits 0 is worse than one that prints nothing.

Everything runs offline: an in-process callable target, no cassette, no key.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import AnswerMatchScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.executor import run_eval
from evalseal.ledger import seal_and_append
from evalseal.report import write_json

runner = CliRunner()

_REFUSAL = "I cannot answer that without more information."


def _arm(answers: dict[str, list[str]], *, model: str = "model-a"):
    """One case per key; the list is that case's reply on each repeat, in order."""
    n_repeats = max(len(v) for v in answers.values())
    ds = Dataset([Case(cid, cid, "yes") for cid in answers], hash="sha256:worked")
    seen: dict[str, int] = {}

    def respond(prompt: str) -> str:
        i = seen.get(prompt, 0)
        seen[prompt] = i + 1
        replies = answers[prompt]
        return replies[i % len(replies)]

    return run_eval(ds, LocalCallableTarget(respond, name=model),
                    AnswerMatchScorer(), n_repeats=n_repeats)


def _ledger(tmp_path: Path, *records) -> Path:
    path = tmp_path / "worked.jsonl"
    prev = "GENESIS"
    for record in records:
        record.prev_hash = prev
        prev = seal_and_append(record, path).hash
    return path


# ======================================================================================
# Example 1. A regression the gate has to fail.
#
# Seven cases, five repeats, `answer_match` against the expected answer "yes".
#
#   baseline : every case answers "yes" five times      -> 7 cases at mean 1.0
#   candidate: c1 still answers "yes"; c2..c7 answer "no" five times
#
# By hand, from docs/metrics.md:
#   §2  case mean    = passes / 5.  c1 -> 1.0, c2..c7 -> 0.0
#   §3  suite mean   = mean of case means. baseline 7/7 = 1.0, candidate 1/7 = 0.142857
#   §6  delta        = mean over the 7 paired cases of (after - before)
#                    = (0 + 6 * -1) / 7 = -0.857143
#   §7  majorities   : c1 PASS->PASS (concordant); c2..c7 PASS->FAIL (6 regressed,
#                      0 improved).  p = min(1, 2 * P[Bin(6, 1/2) <= 0])
#                                      = 2 * 1/64 = 0.03125  -> significant at 0.05
#   §4  flip rate    = 0 everywhere: every case is unanimous within itself
# ======================================================================================

@pytest.fixture
def regression(tmp_path):
    before = _arm({f"c{i}": ["yes"] * 5 for i in range(1, 8)})
    after = _arm({"c1": ["yes"] * 5,
                  **{f"c{i}": ["no"] * 5 for i in range(2, 8)}})
    return before, after, _ledger(tmp_path, before, after)


def test_the_hand_computed_means(regression):
    before, after, _ = regression
    assert before.aggregate.mean_score == pytest.approx(1.0)
    assert after.aggregate.mean_score == pytest.approx(1 / 7)
    assert {c.mean for c in after.results} == {0.0, 1.0}
    assert all(c.flip_rate == 0.0 for c in before.results + after.results)


def test_diff_reports_the_hand_computed_delta_and_p_value(regression, tmp_path):
    before, after, _ = regression
    write_json(before, tmp_path / "before.json")
    write_json(after, tmp_path / "after.json")
    result = runner.invoke(app, ["diff", str(tmp_path / "before.json"),
                                 str(tmp_path / "after.json"), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["comparable"] is True
    assert payload["score"]["delta"] == pytest.approx(-0.857143, abs=1e-6)
    paired = payload["paired"]
    assert paired["n_items"] == 7
    assert (paired["discordant_regressed"], paired["discordant_improved"]) == (6, 0)
    assert paired["p_value"] == pytest.approx(0.03125)
    assert paired["test"] == "exact_mcnemar"
    assert paired["verdict"] == "regression"
    assert paired["significant"] is True
    assert paired["ci_label"] == "95% CI"
    assert paired["confidence"] == pytest.approx(0.95)
    # The interval is a bootstrap, so it is not hand-computable - but it must exclude
    # zero and lie inside [-1, 0], which is all the verdict leans on.
    lo, hi = paired["ci"]
    assert -1.0 <= lo <= hi < 0.0


def test_the_human_output_says_the_same_thing_as_the_json(regression, tmp_path):
    before, after, _ = regression
    write_json(before, tmp_path / "before.json")
    write_json(after, tmp_path / "after.json")
    text = runner.invoke(app, ["diff", str(tmp_path / "before.json"),
                               str(tmp_path / "after.json")]).output
    assert "Comparable:" in text and "yes" in text
    assert "-0.857" in text
    assert "0.03125" in text or "0.0312" in text
    assert "regression" in text
    assert "items whose majority verdict moved" in text


@pytest.mark.parametrize("mode", ["fail_on_evidence_of_regression",
                                  "require_evidence_within_budget"])
def test_the_gate_fails_with_exit_3_under_either_budget_policy(regression, tmp_path,
                                                               mode):
    before, after, ledger = regression
    write_json(before, tmp_path / "baseline.json")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "version": 1,
        "run": {"verify_ledger": True},
        "drift": {"baseline": "baseline.json", "max_score_regression": 0.05,
                  "regression_budget_policy": mode}}), encoding="utf-8")

    result = runner.invoke(app, ["gate", "--ledger", str(ledger),
                                 "--policy", str(policy)])
    assert result.exit_code == 3, result.output
    assert "FAIL" in result.output
    assert "evidence_of_regression" in result.output

    as_json = runner.invoke(app, ["gate", "--ledger", str(ledger),
                                  "--policy", str(policy), "--json"])
    assert as_json.exit_code == 3
    payload = json.loads(as_json.output)
    assert payload["passed"] is False
    failed = {c["rule"] for c in payload["violations"]}
    assert "drift.max_score_regression" in failed


def test_drift_calls_it_target_variance_and_not_a_judge_problem(regression, tmp_path):
    before, after, _ = regression
    write_json(before, tmp_path / "before.json")
    write_json(after, tmp_path / "after.json")
    result = runner.invoke(app, ["drift", str(tmp_path / "before.json"),
                                 str(tmp_path / "after.json"), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["kind"] == "target_variance"
    assert payload["n_verdict_changes"] == 6
    assert payload["n_score_changes"] == 6
    assert "the scorer is deterministic, so it cannot disagree with itself" \
        in payload["grounds"]


def test_report_json_and_text_agree_on_the_candidate(regression, tmp_path):
    _, after, _ = regression
    write_json(after, tmp_path / "after.json")
    payload = json.loads(runner.invoke(
        app, ["report", str(tmp_path / "after.json"), "--json"]).output)
    assert payload["summary"]["mean_score"] == pytest.approx(1 / 7)
    assert payload["summary"]["n_cases"] == 7
    assert payload["summary"]["n_unscorable"] == 0
    assert payload["summary"]["n_observations"] == 35
    assert payload["independence"]["verdict"] == "independent"
    assert payload["independence"]["n_draws"] == 35

    text = runner.invoke(app, ["report", str(tmp_path / "after.json")]).output
    assert "mean 0.14" in text          # the same mean, to two places
    assert "7 case(s)" in text and "35 repeat(s)" in text


# ======================================================================================
# Example 2. A shift that moves the mean and no majority.
#
# Ten cases, five repeats. Every case passes 5/5 in the baseline and 4/5 in the
# candidate - one repeat of each now answers "no".
#
# By hand:
#   §2  case mean    : 1.0 -> 0.8
#   §3  suite mean   : 1.0 -> 0.8
#   §6  delta        = (10 * -0.2) / 10 = -0.2
#   §7  majorities   : 4 of 5 still passes, so every majority stays PASS.
#                      0 regressed, 0 improved -> p = 1.0, the largest it can be
#   §4  flip rate    : 1 of 5 repeats disagrees with the majority = 0.2, every case
#
# So the item-counting test sees nothing at all while the mean plainly moved. Both are
# reported; neither is allowed to stand for the other.
# ======================================================================================

@pytest.fixture
def within_cases(tmp_path):
    before = _arm({f"c{i}": ["yes"] * 5 for i in range(10)})
    after = _arm({f"c{i}": ["yes", "yes", "yes", "yes", "no"] for i in range(10)})
    return before, after, _ledger(tmp_path, before, after)


def test_the_hand_computed_shift(within_cases):
    before, after, _ = within_cases
    assert before.aggregate.mean_score == pytest.approx(1.0)
    assert after.aggregate.mean_score == pytest.approx(0.8)
    assert all(c.flip_rate == pytest.approx(0.2) for c in after.results)
    assert all(c.majority_verdict == 1 for c in after.results)


def test_the_two_results_disagree_and_both_are_named(within_cases, tmp_path):
    before, after, _ = within_cases
    write_json(before, tmp_path / "before.json")
    write_json(after, tmp_path / "after.json")
    payload = json.loads(runner.invoke(app, [
        "diff", str(tmp_path / "before.json"), str(tmp_path / "after.json"),
        "--json"]).output)
    paired = payload["paired"]
    assert paired["delta"] == pytest.approx(-0.2)
    assert paired["n_discordant"] == 0
    assert paired["p_value"] == 1.0                 # the item test saw nothing
    lo, hi = paired["ci"]
    assert hi < 0.0                                 # the mean plainly moved
    assert paired["verdict"] == "inconclusive"      # so neither claim wins
    assert paired["mean_moved_without_majorities"] is True
    # The JSON carries the same sentence the human output prints.
    assert "only 0 item(s) changed majority verdict" in paired["summary"]
    assert "More repeats would make this harder to detect" in paired["summary"]


def test_the_rendered_diff_explains_which_quantity_moved(within_cases, tmp_path):
    before, after, _ = within_cases
    write_json(before, tmp_path / "before.json")
    write_json(after, tmp_path / "after.json")
    text = runner.invoke(app, ["diff", str(tmp_path / "before.json"),
                               str(tmp_path / "after.json")]).output
    assert "how many items changed which way they mostly go" in text
    assert "how far the mean score moved" in text
    assert "Inconclusive is not the same as no difference" in text


def test_the_strict_budget_fails_what_the_default_budget_passes(within_cases, tmp_path):
    """The before/after that matters: a 0.200 drop against a 0.050 budget.

    Under the default policy the gate passes, because nothing was *established* - and
    it now says so in the detail instead of printing a bare green tick. Under
    `require_evidence_within_budget` it fails, because the interval does not rule out a
    drop larger than the budget.
    """
    before, after, ledger = within_cases
    write_json(before, tmp_path / "baseline.json")

    def gate(mode: str):
        policy = tmp_path / f"{mode}.json"
        policy.write_text(json.dumps({
            "version": 1, "run": {"verify_ledger": True},
            "drift": {"baseline": "baseline.json", "max_score_regression": 0.05,
                      "regression_budget_policy": mode}}), encoding="utf-8")
        text = runner.invoke(app, ["gate", "--ledger", str(ledger),
                                   "--policy", str(policy)])
        as_json = runner.invoke(app, ["gate", "--ledger", str(ledger), "--policy",
                                      str(policy), "--json"])
        return text, json.loads(as_json.output)

    text, payload = gate("fail_on_evidence_of_regression")
    assert text.exit_code == 0
    assert payload["passed"] is True
    detail = next(c["detail"] for c in payload["checks"]
                  if c["rule"] == "drift.max_score_regression")
    assert "insufficient_evidence" in detail
    assert "not that the run is within budget" in detail
    assert "require_evidence_within_budget" in detail

    text, payload = gate("require_evidence_within_budget")
    assert text.exit_code == 3
    assert payload["passed"] is False
    detail = next(c["detail"] for c in payload["violations"]
                  if c["rule"] == "drift.max_score_regression")
    assert "Failing to find a regression is not the same as finding there is none" \
        in detail


def test_power_and_evidence_answer_the_two_different_questions(within_cases, tmp_path):
    """`power` is about the item test; `evidence` is about this run's precision."""
    _, after, _ = within_cases
    write_json(after, tmp_path / "after.json")

    power = runner.invoke(app, ["power", "--from-receipt", str(tmp_path / "after.json"),
                                "--mdd", "0.05", "--trials", "200", "--json"])
    assert power.exit_code == 0
    assert json.loads(power.output)["metric"].startswith("paired majority-verdict")

    evidence = runner.invoke(app, ["evidence", str(tmp_path / "after.json"), "--json"])
    assert evidence.exit_code == 0
    payload = json.loads(evidence.output)
    assert payload["n_cases"] == 10
    # 4 of 5 passes: pass-rate margin 0.294, flip-rate margin 0.232 (docs/uncertainty.md)
    case = payload["cases"][0]
    assert case["pass_rate_margin"] == pytest.approx(0.294, abs=5e-4)
    assert case["flip_rate_margin"] == pytest.approx(0.232, abs=5e-4)


# ======================================================================================
# Example 3. A run the grader could not read.
#
# Three cases, five repeats. c1 answers correctly; c2 answers wrongly; c3 refuses.
#
# By hand:
#   §1  c3's replies give no answer in the declared format -> unscorable, score 0.0
#   §2  case means: c1 1.0, c2 0.0, c3 0.0
#   §3  suite mean: 1/3 = 0.333333 - the same as if c3 had simply been wrong
#   §8  unscorable: 5 of 15 repeats (33.3%); c3 has no verdict at all
# ======================================================================================

@pytest.fixture
def ungraded(tmp_path):
    record = _arm({"c1": ["yes"] * 5, "c2": ["no"] * 5, "c3": [_REFUSAL] * 5})
    return record, _ledger(tmp_path, record)


def test_the_mean_is_the_same_as_a_wrong_answer_and_the_counts_are_not(ungraded):
    record, _ = ungraded
    assert record.aggregate.mean_score == pytest.approx(1 / 3)
    assert record.aggregate.n_unscorable == 5
    assert record.aggregate.n_observations == 15
    by_id = {c.case_id: c for c in record.results}
    assert (by_id["c2"].fail_count, by_id["c2"].unscorable_count) == (5, 0)
    assert (by_id["c3"].fail_count, by_id["c3"].unscorable_count) == (0, 5)


def test_report_names_the_ungraded_repeats_in_json_and_in_text(ungraded, tmp_path):
    record, _ = ungraded
    write_json(record, tmp_path / "r.json")
    payload = json.loads(runner.invoke(
        app, ["report", str(tmp_path / "r.json"), "--json"]).output)
    assert payload["summary"]["n_unscorable"] == 5
    case = next(c for c in payload["cases"] if c["case_id"] == "c3")
    assert case["unscorable_count"] == 5
    assert "declared format" in case["unscorable_reason"]

    text = runner.invoke(app, ["report", str(tmp_path / "r.json")]).output
    # Rich wraps the header, so match the words rather than the line.
    assert "ungraded" in text and "33.3%" in text
    assert "UNSCORABLE OBSERVATIONS" in text
    assert "CASES WITH NO VERDICT AT ALL" in text


def test_a_strict_gate_fails_with_exit_3_and_names_the_case(ungraded, tmp_path):
    _, ledger = ungraded
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "version": 1,
        "run": {"min_score": 0.3, "max_unscorable": 0,
                "require_every_case_scored": True}}), encoding="utf-8")
    result = runner.invoke(app, ["gate", "--ledger", str(ledger),
                                 "--policy", str(policy)])
    assert result.exit_code == 3, result.output
    payload = json.loads(runner.invoke(app, [
        "gate", "--ledger", str(ledger), "--policy", str(policy), "--json"]).output)
    assert payload["passed"] is False
    failed = {c["rule"]: c["detail"] for c in payload["violations"]}
    assert "run.max_unscorable" in failed
    assert "c3" in failed["run.require_every_case_scored"]
    # The score floor itself passed, which is the point: it cannot see this.
    floor = next(c for c in payload["checks"] if c["rule"] == "run.min_score")
    assert floor["passed"] is True
    assert "partly ungraded run" in floor["detail"]


def test_the_same_run_passes_a_policy_written_before_2_3(ungraded, tmp_path):
    _, ledger = ungraded
    policy = tmp_path / "old.json"
    policy.write_text(json.dumps({"version": 1, "run": {"min_score": 0.3}}),
                      encoding="utf-8")
    result = runner.invoke(app, ["gate", "--ledger", str(ledger),
                                 "--policy", str(policy)])
    assert result.exit_code == 0, result.output


# ======================================================================================
# Old receipts keep verifying, and keep their own interpretation.
# ======================================================================================

def test_a_schema_1_1_ledger_still_verifies_through_the_cli(tmp_path):
    """The oldest fixture in the repo, through `verify` after four schema bumps."""
    fixture = Path(__file__).resolve().parent / "fixtures" / "ledger_schema_1_1.jsonl"
    result = runner.invoke(app, ["verify", "--ledger", str(fixture)])
    assert result.exit_code == 0, result.output
    assert "1.1" in result.output


def test_a_pre_1_8_receipt_verifies_and_reports_unknown_independence(tmp_path):
    """Schema 1.8 added fields; a receipt that predates them must not look tampered.

    `examples/not-comparable-drift` is sealed under 1.5, four schema versions back.
    """
    example = (Path(__file__).resolve().parent.parent / "examples"
               / "not-comparable-drift" / "baseline.json")
    payload = json.loads(runner.invoke(app, ["report", str(example), "--json"]).output)
    assert payload["independence"]["verdict"] == "unknown"
    assert payload["summary"]["n_unscorable"] == 0

    from evalseal.diffing import load_receipt
    from evalseal.ledger import check_record_hash

    record = load_receipt(example)
    assert record.manifest.schema_version == "1.5"
    status, sealed = check_record_hash(record)
    assert (status, sealed) == ("legacy", "1.5")
