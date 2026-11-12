"""What the grader may conclude from a reply, and what it must refuse to conclude.

Three findings, each reproduced on the 2.2.0 build before any of this was written.

**D1-1. A non-answer scored as an answer.** The extractor's last rule was "otherwise,
the last number appearing anywhere in the reply". Against an expected answer of 30:

    genuine answer 30      -> extracted '30'     score 1.0
    refusal, no answer     -> extracted '30'     score 1.0
    genuine wrong answer   -> extracted '28'     score 0.0

The refusal was *"I can't determine that from the information given ... It says he eats
4 on each of 5 weekdays, which is 20, and then 30"*. It scored identically to a reply
that worked the problem out, and nothing in the receipt could tell the two apart.

**D1-2. A truncated reply graded as a wrong answer.** A reply cut off at the token limit
mid-calculation was scored on whichever digit the cut landed on, so a `max_tokens`
mistake arrived as a finding about the model.

**D1-3. An unparseable judge reply graded as a FAIL.**

    judge said 'PASS'                            -> verdict 1
    judge said 'FAIL'                            -> verdict 0
    judge said "I'm sorry, I cannot grade this."  -> verdict 0
    judge said ''                                -> verdict 0
    judge said 'ERROR: 503'                      -> verdict 0

Four different situations collapsed into the single value 0, so a rate-limited judge was
indistinguishable from a model that failed every case.

The contract these tests pin is docs/metrics.md §1, §2, §4, §8 and §11.
"""
from __future__ import annotations

import pytest
from conftest import scripted

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import (
    AnswerMatchScorer,
    LLMJudgeScorer,
    RegexScorer,
    ScoreResult,
    build_judge_prompt,
    extract_answer,
    find_answer,
    parse_verdict,
)
from evalseal.adapters.target import LocalCallableTarget, TargetResponse
from evalseal.analyze import analyze_case
from evalseal.executor import run_eval
from evalseal.policy import Policy, evaluate

_EXPECTED = "30"
_GENUINE = ("Mason eats 4 carrots on each of the 5 weekdays and 5 on each of the 2\n"
            "weekend days, so 20 + 10 = 30.\n30")
_REFUSAL = ("I can't determine that from the information given - the problem does not\n"
            "say how many carrots Mason eats at the weekend. Please restate it.\n"
            "(It says he eats 4 on each of 5 weekdays, which is 20, and then 30")
_WRONG = "Mason eats 4 carrots a day, 7 days a week, so 4 x 7 = 28.\n28"


# --- the answer format -------------------------------------------------------------

def test_the_refusal_and_the_genuine_answer_no_longer_score_the_same():
    """D1-1, the headline. Same expected answer, three replies, three outcomes."""
    s = AnswerMatchScorer()
    genuine, refusal, wrong = (s.score("q", t, _EXPECTED)
                               for t in (_GENUINE, _REFUSAL, _WRONG))

    assert (genuine.score, genuine.verdict, genuine.unscorable) == (1.0, 1, False)
    assert (wrong.score, wrong.verdict, wrong.unscorable) == (0.0, 0, False)
    # The refusal is neither: it is a measurement that did not happen.
    assert (refusal.score, refusal.verdict, refusal.unscorable) == (0.0, None, True)
    assert "declared format" in refusal.reason


@pytest.mark.parametrize("reply,want", [
    # Every form the format declares, each with a legitimate final answer.
    ("reasoning, then\n#### 30", "30"),
    (r"so \boxed{30} it is", "30"),
    ("Final answer: 30", "30"),
    ("The answer is 30.", "30"),
    ("20 + 10 = 30 carrots in all.\n\n30\n", "30"),
    ("$30.00", "30.00"),
    ("30%", "30"),
])
def test_a_legitimate_final_answer_is_found_in_every_declared_form(reply, want):
    assert extract_answer(reply) == want
    assert AnswerMatchScorer().score("q", reply, "30").score == 1.0


@pytest.mark.parametrize("reply", [
    "I cannot answer this without knowing the weekend rate.",
    "Could you clarify whether weekends count as days?",
    "The problem gives 4 carrots on each of 5 weekdays, which is 20 carrots in total.",
    "Step 1: 5 x 4 = 20.\nStep 2: 2 x 5 = 10.\nStep 3: 20 + 1",   # truncated mid-sum
    "",
    "   \n  \n",
])
def test_ambiguous_or_absent_answers_are_unscorable_not_wrong(reply):
    """A reply that does not answer is not a wrong answer, however many digits it has."""
    result = AnswerMatchScorer().score("q", reply, "30")
    assert result.unscorable and result.verdict is None and result.score == 0.0


def test_a_contradicted_answer_is_graded_on_where_the_reply_ended_up():
    """Contradictory text: the reply revises itself, and the last answer stands."""
    revised = "The answer is 28. No - recounting the weekend, the answer is 30."
    assert extract_answer(revised) == "30"
    # And the other direction, so this is not an artefact of 30 coming second.
    assert extract_answer("The answer is 30. Actually, the answer is 28.") == "28"


def test_a_marker_mid_reasoning_does_not_outrank_the_reply_s_own_final_line():
    """The gsm8k_compare finding: precedence is by position, not by form.

    37 of gemma's 750 recorded replies rehearsed an answer inside a <thought> block and
    then stated a different one on the final line. Preferring the phrase form over the
    final line graded six of its nine 'unstable' items on a number the model had moved
    past. examples/gsm8k_compare/RESULTS.md carries both readings.
    """
    reply = ("<thought>At this point the answer is 4, but wait - re-reading the\n"
             "question, it asks about all eight stalls.</thought>\n"
             "Cows in 8 stalls = 22 * 8 = 176.\n176")
    assert extract_answer(reply) == "176"
    assert find_answer(reply).rule == "final_line"


def test_a_prose_final_line_is_not_mistaken_for_an_answer():
    """A bare token is an answer; a sentence is not, however short."""
    assert extract_answer("float") == "float"              # the codeqa shape
    assert extract_answer("So the total is 30 carrots.") is None


def test_an_independent_oracle_agrees_with_the_extractor_on_the_declared_forms():
    """A second implementation, deliberately dumb, on inputs where both must agree.

    Written from the specification rather than from the code: each declared form is
    located by a string search, and the latest wins. It does not implement the format;
    it checks that the format's own examples come out where docs/metrics.md says.
    """
    def oracle(reply: str) -> str | None:
        spots: list[tuple[int, str]] = []
        if "####" in reply:
            spots.append((reply.rindex("####"), reply[reply.rindex("####") + 4:].strip()))
        if "\\boxed{" in reply:
            at = reply.rindex("\\boxed{")
            spots.append((at, reply[at + 7:reply.index("}", at)]))
        lowered = reply.lower()
        for phrase in ("the answer is ", "final answer: "):
            if phrase in lowered:
                at = lowered.rindex(phrase)
                spots.append((at, reply[at + len(phrase):].split("\n")[0].rstrip(".")))
        tail = [ln.strip() for ln in reply.splitlines() if ln.strip()]
        if tail and " " not in tail[-1]:
            spots.append((len(reply), tail[-1].rstrip(".")))
        return max(spots, key=lambda s: s[0])[1] if spots else None

    for reply in (_GENUINE, _WRONG, "reasoning\n#### 72", r"text \boxed{42} text",
                  "Final answer: -5", "The answer is 18.", "20 + 10 = 30\n30", "float"):
        assert oracle(reply) == extract_answer(reply), reply


# --- the judge ---------------------------------------------------------------------

@pytest.mark.parametrize("reply,verdict", [
    ("PASS", 1),
    ("pass.", 1),
    ("FAIL", 0),
    ("FAIL - this does not PASS", 0),
])
def test_a_graded_verdict_is_still_a_graded_verdict(reply, verdict):
    assert parse_verdict(reply) == verdict


@pytest.mark.parametrize("reply", [
    "I'm sorry, I cannot grade this.",
    "",
    "ERROR: 503 upstream unavailable",
    "PASSABLE",
])
def test_an_ungraded_judge_reply_is_not_a_fail(reply):
    """D1-3. These four were all the integer 0, which is what a failed case looks like."""
    assert parse_verdict(reply) is None
    judge = LocalCallableTarget(lambda p: reply, name="judge")
    result = LLMJudgeScorer(judge=judge, rubric="Be strict.").score("q", "a", None)
    assert result.unscorable and result.verdict is None and result.score == 0.0
    assert "no PASS or FAIL token" in result.reason


def test_an_empty_judge_reply_says_so_specifically():
    judge = LocalCallableTarget(lambda p: "", name="judge")
    result = LLMJudgeScorer(judge=judge, rubric="Be strict.").score("q", "a", None)
    assert "the reply was empty" in result.reason


# --- the denominator ---------------------------------------------------------------

def test_an_unscorable_repeat_stays_in_the_mean_and_out_of_the_flip_rate():
    """docs/metrics.md §2 and §4: different denominators, on purpose."""
    stats = analyze_case([1.0, 1.0, 0.0, 0.0, 0.0],
                         binary=True, unscorable=[False, False, False, True, True])
    # Mean over all five: the two ungraded repeats still cost the case its score.
    assert stats.mean == pytest.approx(0.4)
    # Flip rate over the three that were graded - two passes and one fail - so one of
    # three disagreed with the majority. The ungraded pair takes no part in it.
    assert stats.flip_rate == pytest.approx(1 / 3)
    assert stats.majority_verdict == 1


def test_a_case_with_no_graded_repeat_has_no_interval_and_no_majority():
    stats = analyze_case([0.0, 0.0], binary=True, unscorable=[True, True])
    assert stats.majority_verdict is None
    assert (stats.ci95_low, stats.ci95_high) == (0.0, 0.0)
    assert stats.stability == "UNSTABLE"
    assert stats.stability_label == "insufficient_runs"


def test_analyze_case_refuses_a_mismatched_unscorable_list():
    with pytest.raises(ValueError, match="one flag per score"):
        analyze_case([1.0, 0.0], binary=True, unscorable=[True])


def _run(answers: dict[str, str], *, expected: str = "30", n_repeats: int = 5):
    cases = [Case(cid, cid, expected) for cid in answers]
    ds = Dataset(cases, hash="sha256:test")
    target = LocalCallableTarget(lambda p: answers[p], name="model-a")
    return run_eval(ds, target, AnswerMatchScorer(), n_repeats=n_repeats)


def test_the_receipt_counts_how_many_observations_were_unscorable():
    record = _run({"a": _GENUINE, "b": _REFUSAL, "c": _WRONG})
    assert record.aggregate.n_observations == 15
    assert record.aggregate.n_unscorable == 5
    by_id = {c.case_id: c for c in record.results}
    assert by_id["b"].unscorable_count == 5
    assert by_id["a"].unscorable_count == by_id["c"].unscorable_count == 0
    assert "declared format" in by_id["b"].unscorable_reason


def test_the_run_warns_about_unscorable_observations_with_a_denominator():
    record = _run({"a": _GENUINE, "b": _REFUSAL})
    warning = next(w for w in record.aggregate.warnings if w.startswith("UNSCORABLE"))
    assert "5 of 10 repeat(s) (50.0%)" in warning
    blind = next(w for w in record.aggregate.warnings if w.startswith("CASES WITH NO"))
    assert blind.split(": ")[1].startswith("b.")


def test_a_refused_case_does_not_look_like_a_failed_case_in_the_receipt():
    """The two score the same 0.0 and must not read the same."""
    refused = _run({"x": _REFUSAL})
    failed = _run({"x": _WRONG})
    assert refused.aggregate.mean_score == failed.aggregate.mean_score == 0.0
    assert refused.results[0].unscorable_count == 5
    assert failed.results[0].unscorable_count == 0
    assert failed.results[0].fail_count == 5
    assert refused.results[0].fail_count == 0


def test_a_truncated_reply_is_unscorable_rather_than_wrong():
    """D1-2, through the executor, where truncation is visible."""
    class Truncating:
        def generate(self, prompt):
            return TargetResponse(
                text="Step 1: 5 x 4 = 20.\nStep 2: 20 + 1", requested_model="m",
                served_model="m", system_fingerprint=None, base_url="local://",
                effective_params={}, params_source="explicit", truncated=True,
                provider="local", origin="local")

    ds = Dataset([Case("a", "q", "30")], hash="sha256:test")
    record = run_eval(ds, Truncating(), AnswerMatchScorer(), n_repeats=3)
    case = record.results[0]
    assert case.unscorable_count == 3 and case.fail_count == 0
    assert "token limit" in case.unscorable_reason
    assert any("RESPONSE TRUNCATED" in w for w in record.aggregate.warnings)


def test_a_truncated_reply_that_matched_by_luck_cannot_lift_the_mean():
    """A regex scorer will happily match a truncated reply. It still must not count."""
    class Truncating:
        def generate(self, prompt):
            return TargetResponse(
                text="yes, and then the calculation continu", requested_model="m",
                served_model="m", system_fingerprint=None, base_url="local://",
                effective_params={}, params_source="explicit", truncated=True,
                provider="local", origin="local")

    ds = Dataset([Case("a", "q")], hash="sha256:test")
    record = run_eval(ds, Truncating(), RegexScorer("yes"), n_repeats=3)
    assert record.aggregate.mean_score == 0.0
    assert record.results[0].unscorable_count == 3


def test_a_run_cannot_pass_a_strict_gate_by_failing_to_grade_the_hard_cases():
    """The denominator invariant, at the gate.

    Both runs answer two cases correctly. In the first the two hard cases are graded
    and wrong; in the second the grader cannot read them at all. The mean score is
    identical - that is what keeps `min_score` honest - and the rules that ask about
    gradeability are what separate the two.
    """
    graded = _run({"a": _GENUINE, "b": _GENUINE, "c": _WRONG, "d": _WRONG})
    ungraded = _run({"a": _GENUINE, "b": _GENUINE, "c": _REFUSAL, "d": _REFUSAL})
    assert graded.aggregate.mean_score == ungraded.aggregate.mean_score == 0.5

    policy = Policy.model_validate({
        "run": {"min_score": 0.4, "max_unscorable": 0, "require_every_case_scored": True,
                "verify_ledger": False}})
    assert evaluate(policy, graded).passed
    strict = evaluate(policy, ungraded)
    assert not strict.passed
    failed = {c.rule: c.detail for c in strict.violations}
    assert set(failed) == {"run.max_unscorable", "run.require_every_case_scored"}
    assert "10 ungraded repeat(s) of 20" in failed["run.max_unscorable"]
    assert "c, d" in failed["run.require_every_case_scored"]


def test_the_score_floor_names_its_own_denominator():
    """A reviewer comparing two gate logs has no other way to see the difference."""
    record = _run({"a": _GENUINE, "b": _REFUSAL})
    policy = Policy.model_validate({"run": {"min_score": 0.4, "verify_ledger": False}})
    detail = next(c.detail for c in evaluate(policy, record).checks
                  if c.rule == "run.min_score")
    assert "over 2 case(s), 10 repeat(s)" in detail
    assert "partly ungraded run" in detail


def test_the_unscorable_fraction_rule_reads_as_a_share():
    record = _run({"a": _GENUINE, "b": _REFUSAL})
    policy = Policy.model_validate({
        "run": {"max_unscorable_fraction": 0.1, "verify_ledger": False}})
    check = next(c for c in evaluate(policy, record).checks
                 if c.rule == "run.max_unscorable_fraction")
    assert not check.passed and "50.0% of 10 repeat(s) ungraded" in check.detail


def test_a_policy_written_before_2_3_behaves_exactly_as_it_did():
    """Every new rule defaults to off."""
    record = _run({"a": _GENUINE, "b": _REFUSAL})
    policy = Policy.model_validate({"run": {"min_score": 0.4, "verify_ledger": False}})
    assert evaluate(policy, record).passed
    assert {c.rule for c in evaluate(policy, record).checks} == {"run.min_score"}


def test_a_judge_run_reports_its_ungraded_observations_end_to_end():
    """The whole path: a judge that answers one case and errors on the other."""
    rubric = "Be strict."
    replies = {
        build_judge_prompt(rubric, "q1", "a1"): ["PASS"],
        build_judge_prompt(rubric, "q2", "a2"): ["ERROR: 503 upstream unavailable"],
    }
    judge = LocalCallableTarget(scripted(replies), name="judge-a")
    answers = {"q1": "a1", "q2": "a2"}
    ds = Dataset([Case("c1", "q1"), Case("c2", "q2")], hash="sha256:test")
    record = run_eval(ds, LocalCallableTarget(lambda p: answers[p], name="model-a"),
                      LLMJudgeScorer(judge=judge, rubric=rubric), n_repeats=5)
    by_id = {c.case_id: c for c in record.results}
    assert by_id["c1"].pass_count == 5 and by_id["c1"].unscorable_count == 0
    assert by_id["c2"].unscorable_count == 5 and by_id["c2"].fail_count == 0
    assert record.aggregate.n_unscorable == 5
    assert "no PASS or FAIL token" in by_id["c2"].unscorable_reason


def test_a_custom_scorer_that_says_nothing_about_gradeability_still_works():
    """`unscorable` defaults to False, so a scorer written before 2.3 is unaffected."""
    class Old:
        kind = "exact"
        def config(self):
            return {}
        def score(self, prompt, response_text, expected):
            return ScoreResult(1.0, True, 1)

    ds = Dataset([Case("a", "q")], hash="sha256:test")
    record = run_eval(ds, LocalCallableTarget(lambda p: "x", name="m"), Old(), n_repeats=3)
    assert record.aggregate.n_unscorable == 0
    assert record.aggregate.n_observations == 3
    assert record.results[0].unscorable_reason is None
