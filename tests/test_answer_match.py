"""The scorer a real benchmark needs: compare an extracted answer to ground truth."""
from __future__ import annotations

import pytest

from evalseal.adapters.scorer import AnswerMatchScorer, extract_answer


@pytest.mark.parametrize("text,want", [
    ("The answer is 18.", "18"),
    ("Janet makes $18 every day.", "18"),
    ("#### 72", "72"),
    ("reasoning...\n#### 1,000", "1,000"),
    (r"so \boxed{42} is the result", "42"),
    ("Final answer: -5", "-5"),
    ("She has 3 apples, then buys 4, so 7 total", "7"),
    ("no digits here", "no digits here"),
])
def test_extraction(text, want):
    assert extract_answer(text) == want


@pytest.mark.parametrize("response,expected,ok", [
    ("The answer is 18.", "18", True),
    ("#### 1,000", "1000", True),          # commas are formatting, not value
    ("$18.00", "18", True),                # currency and trailing zeros
    ("The answer is 19.", "18", False),
    ("yes", "Yes", True),                  # non-numeric compares case-folded
    ("maybe", "yes", False),
])
def test_scoring(response, expected, ok):
    result = AnswerMatchScorer().score("q", response, expected)
    assert result.score == float(ok)
    assert result.binary and result.verdict == int(ok)


def test_expected_is_required():
    with pytest.raises(ValueError, match="needs `expected`"):
        AnswerMatchScorer().score("q", "18", None)


def test_last_marker_wins_over_earlier_reasoning():
    # A model that revises itself should be graded on its final answer.
    assert extract_answer("First I thought 10. #### 12") == "12"


def test_prose_containing_the_word_answer_does_not_hijack_extraction():
    """Regression: a real Gemini reply whose reasoning mentions "answer" mid-sentence.
    The loose pattern captured that clause instead of the number it ended on."""
    reply = (
        'Since the question asks for "8 of the stalls" at the end, it implies that all '
        "stalls mentioned have a uniform number of cows.\n"
        "Cows in 8 stalls = 22 cows/stall * 8 stalls = 176 cows.\n\n176"
    )
    assert extract_answer(reply) == "176"


def test_explicit_answer_phrase_beats_a_later_working_number():
    assert extract_answer("The answer is 42. Check: 42 * 2 = 84.") == "42"


def test_non_numeric_answer_phrase_still_works():
    assert extract_answer("The answer is yes.") == "yes"
    assert AnswerMatchScorer().score("q", "The answer is yes.", "yes").score == 1.0
