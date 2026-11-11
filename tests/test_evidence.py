"""How much a run supports, ported from PR #12 with three defects fixed.

PR #12 (`uncertainty-v2.3`, draft, head fb2fac7) added `evalseal evidence`, a policy
`uncertainty` block and `report --json` additions. They were reviewed against
docs/metrics.md rather than taken as correct, and three things were wrong. Each was
reproduced against PR #12's own code before any of it was ported.

**D5-1. `require_powered_flip_rate` crashed on a continuous-scored case.**

    label 'not_binary'  margin None  underpowered False
    raised TypeError: unsupported format string passed to NoneType.__format__

`uncertain()` treated a case with no margin as uncertain, then the detail formatted the
widest such case's margin. A continuous-scored suite took the gate down.

**D5-2. The policy named a flip-rate margin and the check used the pass-rate one.**

    3/5:   pass-rate CI (0.231, 0.882) half-width 0.326
         | flip-rate CI (0.118, 0.500) half-width 0.191
         -> gate compared 0.326 against target_flip_rate_margin

So a case whose flip rate was pinned inside a declared 0.25 margin failed a gate that
declared 0.25.

**D5-3. The rendered table hardcoded 95%.** At `--confidence 0.80`:

    | confidence | 80% |
    | case | runs | flips | flip rate | 95% flip-rate range | margin | ...
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import AnswerMatchScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.analyze import wilson_ci
from evalseal.cli import app
from evalseal.evidence import (
    DIRECTIONAL,
    FLIP_RATE,
    INSUFFICIENT,
    NOT_BINARY,
    PASS_RATE,
    STRONGER,
    analyze_evidence,
    case_evidence,
    flip_margin_at,
    flip_rate_interval,
    margin_at,
    render,
    repeats_for_margin,
    z_for_confidence,
)
from evalseal.executor import run_eval
from evalseal.models import CaseResult
from evalseal.policy import Policy, evaluate
from evalseal.report import write_json

runner = CliRunner()


def _case(case_id: str, passes: int, fails: int, *, unscorable: int = 0,
          binary: bool = True) -> CaseResult:
    """A case result with `passes`/`fails` graded repeats and `unscorable` ungraded."""
    n = passes + fails + unscorable
    scores = [1.0] * passes + [0.0] * (fails + unscorable)
    flips = min(passes, fails)
    return CaseResult(
        case_id=case_id, scores=scores, mean=passes / n if n else 0.0,
        ci95=wilson_ci(passes, passes + fails), flip_rate=flips / max(1, passes + fails),
        stability="UNSTABLE",
        majority_verdict=(1 if passes > fails else 0) if binary and passes != fails
        else (None if not binary else None),
        pass_count=passes, fail_count=fails, flip_count=flips,
        unscorable_count=unscorable,
        unscorable_reason="no answer in the declared format" if unscorable else None,
    )


# --- D5-2: each margin is the margin it is named ------------------------------------

@pytest.mark.parametrize("passes,fails,pass_hw,flip_hw", [
    (3, 2, 0.326, 0.191),       # the interval straddles 0.5: the two differ
    (5, 0, 0.217, 0.217),       # unanimous: they happen to coincide
    (4, 1, 0.294, 0.232),
    (15, 6, 0.181, 0.181),
])
def test_the_two_margins_are_reported_separately(passes, fails, pass_hw, flip_hw):
    ev = case_evidence(_case("a", passes, fails))
    assert ev.pass_rate_margin == pytest.approx(pass_hw, abs=5e-4)
    assert ev.flip_rate_margin == pytest.approx(flip_hw, abs=5e-4)
    # The old name still carries the pass-rate margin it always carried.
    assert ev.margin == ev.pass_rate_margin


def test_the_margins_match_an_independent_computation():
    """An oracle from the definitions: Wilson, then the image under min(p, 1-p)."""
    z = z_for_confidence(0.95)
    for passes in range(0, 8):
        n = 7
        lo, hi = wilson_ci(passes, n, z)
        want_pass = (hi - lo) / 2
        flo, fhi = (min(lo, 1 - lo), min(hi, 1 - hi))
        want_flip = (0.5 if lo <= 0.5 <= hi else max(flo, fhi)) - min(flo, fhi)
        # abs=1e-4 because `flip_rate_interval` rounds its endpoints to four places
        # on purpose, so the derived half-width inherits that rounding.
        assert margin_at(passes, n, z) == pytest.approx(want_pass)
        assert flip_margin_at(passes, n, z) == pytest.approx(want_flip / 2, abs=1e-4)


def test_the_gate_compares_the_margin_the_policy_names():
    """D5-2. 3 of 5 passes: flip rate pinned to +/-0.191, pass rate to +/-0.326."""
    record = _record({"a": (3, 2)})
    lenient = Policy.model_validate({
        "run": {"verify_ledger": False},
        "uncertainty": {"require_powered_flip_rate": True,
                        "target_flip_rate_margin": 0.25}})
    check = _only(evaluate(lenient, record), "uncertainty.powered_flip_rate")
    assert check.passed, check.detail        # 0.191 <= 0.25, and it used to compare 0.326

    strict = Policy.model_validate({
        "run": {"verify_ledger": False},
        "uncertainty": {"require_powered_flip_rate": True,
                        "target_flip_rate_margin": 0.15}})
    check = _only(evaluate(strict, record), "uncertainty.powered_flip_rate")
    assert not check.passed
    assert "Widest flip-rate interval: a at +/-0.191" in check.detail


def test_the_repeat_recommendation_aims_at_the_flip_rate_margin():
    z = z_for_confidence(0.95)
    flip = repeats_for_margin(0.10, z, quantity=FLIP_RATE)
    pass_rate = repeats_for_margin(0.10, z, quantity=PASS_RATE)
    assert flip is not None and pass_rate is not None
    # The flip-rate interval is the narrower of the two at p=0.5, so it needs fewer.
    assert flip < pass_rate
    assert flip_margin_at(round(0.5 * flip), flip, z) <= 0.10
    assert flip_margin_at(round(0.5 * (flip - 1)), flip - 1, z) > 0.10


def test_an_unknown_quantity_is_refused():
    with pytest.raises(ValueError, match="unknown quantity"):
        repeats_for_margin(0.1, z_for_confidence(0.95), quantity="whatever")


# --- D5-1: a case with no interval is excluded, not formatted ------------------------

def _float_record():
    class Float:
        kind = "exact"

        def config(self) -> dict:
            return {}

        def score(self, prompt, response_text, expected):
            from evalseal.adapters.scorer import ScoreResult
            return ScoreResult(0.4, False, None)

    ds = Dataset([Case("f1", "q1"), Case("f2", "q2")], hash="sha256:test")
    return run_eval(ds, LocalCallableTarget(lambda p: "x", name="m"), Float(),
                    n_repeats=5)


def test_a_continuous_case_has_no_margin_and_says_so():
    ev = analyze_evidence(_float_record())
    assert [c.label for c in ev.cases] == [NOT_BINARY, NOT_BINARY]
    assert all(c.flip_rate_margin is None and c.pass_rate_margin is None
               for c in ev.cases)
    assert ev.label == NOT_BINARY
    assert len(ev.not_binary) == 2


def test_the_powered_rule_does_not_crash_on_a_continuous_suite():
    """D5-1. This raised TypeError: unsupported format string passed to NoneType."""
    policy = Policy.model_validate({
        "run": {"verify_ledger": False},
        "uncertainty": {"require_powered_flip_rate": True}})
    check = _only(evaluate(policy, _float_record()), "uncertainty.powered_flip_rate")
    assert check.passed
    assert "carry continuous scores and have no flip-rate interval" in check.detail
    assert "f1, f2" in check.detail
    assert "read their scores directly" in check.detail


def test_a_mixed_suite_names_both_the_thin_cases_and_the_inapplicable_ones():
    binary = _record({"a": (3, 2)})
    floats = _float_record()
    binary.results.extend(floats.results)
    policy = Policy.model_validate({
        "run": {"verify_ledger": False},
        "uncertainty": {"require_powered_flip_rate": True,
                        "target_flip_rate_margin": 0.10}})
    check = _only(evaluate(policy, binary), "uncertainty.powered_flip_rate")
    assert not check.passed
    assert "Widest flip-rate interval: a at +/-0.191" in check.detail
    assert "2 case(s) carry continuous scores" in check.detail


# --- D5-3: the label follows the confidence ------------------------------------------

@pytest.mark.parametrize("confidence,label", [(0.95, "95%"), (0.80, "80%"),
                                              (0.99, "99%")])
def test_the_rendered_range_is_labelled_from_the_confidence(confidence, label):
    """D5-3. The header said "95% flip-rate range" while the summary said 80%."""
    ev = analyze_evidence(_record({"b1": (4, 1)}), confidence=confidence)
    text = render(ev)
    assert f"| {label} flip-rate range" in text
    assert f"| confidence | {label} |" in text
    others = {"95%", "80%", "99%"} - {label}
    assert not any(f"| {o} flip-rate range" in text for o in others)


def test_the_confidence_reaches_the_interval_and_not_only_the_label():
    narrow = case_evidence(_case("a", 4, 1), confidence=0.80)
    wide = case_evidence(_case("a", 4, 1), confidence=0.99)
    assert narrow.pass_rate_margin < wide.pass_rate_margin
    assert narrow.pass_ci[0] > wide.pass_ci[0]


def test_an_impossible_confidence_is_refused():
    for bad in (0.0, 1.0, -0.5, 2.0):
        with pytest.raises(ValueError, match="confidence must be"):
            z_for_confidence(bad)


# --- ungraded repeats are not observations ------------------------------------------

def test_the_interval_is_over_graded_repeats_only():
    """Five repeats, three ungraded: two observations, not five."""
    ev = case_evidence(_case("a", 2, 0, unscorable=3))
    assert (ev.n_runs, ev.n_graded) == (5, 2)
    assert ev.pass_ci == pytest.approx(tuple(round(v, 4) for v in wilson_ci(2, 2)),
                                       abs=1e-4)
    assert ev.label == INSUFFICIENT          # two graded repeats is below the floor
    assert ev.pass_rate == 1.0               # of the two that were graded


def test_a_case_with_no_graded_repeat_is_not_binary_rather_than_perfect():
    ev = case_evidence(_case("a", 0, 0, unscorable=5))
    assert ev.n_graded == 0
    assert ev.label == NOT_BINARY
    assert ev.pass_ci is None and ev.flip_rate_margin is None


def test_the_repeat_floor_rule_counts_graded_repeats():
    record = _record({"a": (2, 0)}, unscorable={"a": 3})
    policy = Policy.model_validate({
        "run": {"verify_ledger": False},
        "uncertainty": {"min_repeats_per_case": 5}})
    check = _only(evaluate(policy, record), "uncertainty.min_repeats_per_case")
    assert not check.passed
    assert "case a has 2 graded repeat(s), policy requires 5" in check.detail


def test_an_inconsistent_receipt_is_named_rather_than_raising_a_math_error():
    """PR #12's own fix, kept: this raised `math domain error` three frames down."""
    bad = _case("a", 3, 0)
    bad.pass_count = 9
    with pytest.raises(ValueError, match="which cannot happen"):
        case_evidence(bad)


# --- compatibility -------------------------------------------------------------------

def test_a_policy_without_an_uncertainty_block_behaves_as_it_did():
    record = _record({"a": (3, 2)})
    policy = Policy.model_validate({"run": {"min_score": 0.0, "verify_ledger": False}})
    result = evaluate(policy, record)
    assert result.passed
    assert not any(c.rule.startswith("uncertainty.") for c in result.checks)


def test_an_unknown_uncertainty_key_is_refused_rather_than_ignored():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Policy.model_validate({"uncertainty": {"require_powered_flipp_rate": True}})


def test_the_2_2_0_report_json_keys_are_all_still_there():
    """The contract PR #12 pinned: keys may appear beside the old ones, never replace."""
    record = _record({"a": (3, 2)})
    write_json(record, "report.json")
    payload = json.loads(runner.invoke(app, ["report", "report.json", "--json"]).stdout)
    for key in ("summary", "cases", "provenance"):
        assert key in payload
    for key in ("mean_score", "flip_rate", "n_cases", "n_stable", "n_unstable",
                "sealed_hash"):
        assert key in payload["summary"], key
    for key in ("case_id", "runs", "verdict_distribution", "verdict_sequence",
                "flip_count", "flip_rate", "majority_verdict", "stability",
                "stability_label", "ci95"):
        assert key in payload["cases"][0], key


def test_report_json_carries_the_uncertainty_block():
    record = _record({"a": (3, 2), "b": (5, 0)})
    write_json(record, "report.json")
    payload = json.loads(runner.invoke(app, ["report", "report.json", "--json"]).stdout)
    block = payload["uncertainty"]
    assert block["label"] == DIRECTIONAL
    assert block["n_cases"] == 2
    assert block["worst_flip_rate_margin"] == pytest.approx(0.217, abs=5e-4)
    assert block["worst_pass_rate_margin"] == pytest.approx(0.326, abs=5e-4)
    assert "pass_rate_margin" in block["caveat"]
    case = next(c for c in payload["cases"] if c["case_id"] == "a")
    assert case["evidence"]["flip_rate_margin"] == pytest.approx(0.191, abs=5e-4)
    assert case["evidence"]["n_graded"] == 5


# --- the command ---------------------------------------------------------------------

def test_the_evidence_command_reports_a_suite():
    record = _record({"a": (3, 2), "b": (5, 0)})
    write_json(record, "report.json")
    result = runner.invoke(app, ["evidence", "report.json"])
    assert result.exit_code == 0
    assert "Evidence" in result.stdout
    assert "directional" in result.stdout


def test_the_evidence_command_emits_json():
    record = _record({"a": (3, 2)})
    write_json(record, "report.json")
    result = runner.invoke(app, ["evidence", "report.json", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["cases"][0]["flip_rate_margin"] == pytest.approx(0.191, abs=5e-4)
    assert payload["target_margin"] == 0.10


def test_the_evidence_command_can_report_one_case():
    record = _record({"a": (3, 2), "b": (5, 0)})
    write_json(record, "report.json")
    result = runner.invoke(app, ["evidence", "report.json", "--case", "b", "--json"])
    assert result.exit_code == 0
    assert [c["case_id"] for c in json.loads(result.stdout)["cases"]] == ["b"]


def test_an_unknown_case_is_an_actionable_message_and_exit_2():
    record = _record({"a": (3, 2)})
    write_json(record, "report.json")
    result = runner.invoke(app, ["evidence", "report.json", "--case", "nope"])
    assert result.exit_code == 2
    assert "no case 'nope'" in result.stdout
    assert "it has: a" in result.stdout


def test_the_evidence_command_writes_markdown():
    record = _record({"a": (3, 2)})
    write_json(record, "report.json")
    result = runner.invoke(app, ["evidence", "report.json", "--out", "ev.md"])
    assert result.exit_code == 0
    from pathlib import Path
    assert "Evidence" in Path("ev.md").read_text(encoding="utf-8")


def test_power_and_evidence_stay_separate_commands():
    """They answer different questions: prospective-comparative and retrospective.

    PR #12 argued the case and this pins it, because merging them would put `--mdd`
    and `--target-margin` on one command where each is meaningless to the other.
    """
    power = runner.invoke(app, ["power", "--help"]).stdout
    evidence = runner.invoke(app, ["evidence", "--help"]).stdout
    assert "--target-margin" not in power and "--mdd" not in evidence
    assert "different question" in evidence


# --- helpers --------------------------------------------------------------------------

def _record(spec: dict[str, tuple[int, int]], unscorable: dict[str, int] | None = None):
    """A real sealed record whose cases have the requested pass/fail/ungraded counts."""
    unscorable = unscorable or {}
    answers: dict[str, list[str]] = {}
    for cid, (passes, fails) in spec.items():
        answers[cid] = (["yes"] * passes + ["no"] * fails
                        + ["refused"] * unscorable.get(cid, 0))
    n_repeats = max(len(v) for v in answers.values())
    ds = Dataset([Case(cid, cid, "yes") for cid in answers], hash="sha256:test")
    state: dict[str, int] = {}

    def respond(prompt: str) -> str:
        i = state.get(prompt, 0)
        state[prompt] = i + 1
        reply = answers[prompt][i % len(answers[prompt])]
        # "refused" is prose with no answer in the declared format, so it is ungraded.
        return reply if reply != "refused" else "I cannot answer that question."

    return run_eval(ds, LocalCallableTarget(respond, name="m"), AnswerMatchScorer(),
                    n_repeats=n_repeats)


def _only(result, rule: str):
    return next(c for c in result.checks if c.rule == rule)


def test_the_helper_builds_the_counts_it_claims():
    """The fixture is checked against itself before anything is asserted through it."""
    record = _record({"a": (3, 2), "b": (2, 0)}, unscorable={"b": 3})
    by_id = {c.case_id: c for c in record.results}
    assert (by_id["a"].pass_count, by_id["a"].fail_count) == (3, 2)
    assert by_id["a"].unscorable_count == 0
    assert (by_id["b"].pass_count, by_id["b"].fail_count) == (2, 0)
    assert by_id["b"].unscorable_count == 3


def test_a_strong_case_is_labelled_as_such():
    ev = case_evidence(_case("a", 40, 0))
    assert ev.label == STRONGER
    assert ev.pass_rate_margin < 0.075


def test_flip_rate_interval_folds_at_a_half():
    assert flip_rate_interval(0.2, 0.8) == (0.2, 0.5)
    assert flip_rate_interval(0.6, 0.9) == (0.1, 0.4)
    assert flip_rate_interval(0.0, 0.3) == (0.0, 0.3)


def test_the_numbers_docs_uncertainty_quotes():
    """The prose in docs/uncertainty.md cannot rot without this failing."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "docs" / "uncertainty.md"
           ).read_text(encoding="utf-8")
    z = z_for_confidence(0.95)
    assert repeats_for_margin(0.10, z, quantity=FLIP_RATE) == 22
    assert repeats_for_margin(0.10, z, pass_rate=1.0, quantity=FLIP_RATE) == 16
    assert "| 22 repeats |" in doc
    assert "| 16 repeats, unanimous case |" in doc
    for passes, fails, pass_hw, flip_hw in ((5, 0, "0.217", "0.217"),
                                            (4, 1, "0.294", "0.232"),
                                            (3, 2, "0.326", "0.191"),
                                            (15, 6, "0.181", "0.181")):
        ev = case_evidence(_case("a", passes, fails))
        assert f"{ev.pass_rate_margin:.3f}" == pass_hw
        assert f"{ev.flip_rate_margin:.3f}" == flip_hw
        assert f"| {pass_hw} | {flip_hw} |" in doc, (passes, fails)
