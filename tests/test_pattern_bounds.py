"""A grading pattern must not take longer than the evaluation it grades.

**Baseline reproduction (2.2.0).** `RegexScorer`'s pattern comes from a scorer config
file, which in a pull-request workflow can come from a fork, and Python's `re` is a
backtracking engine with no timeout:

    RegexScorer('^(a+)+$') on 31 characters of input: 25.12s

Each extra character roughly doubles it, so forty characters is hours - inside a CI job
holding a cassette lock, with nothing to see but a hung step. It needs no hostility:
`([a-z]+)+` is the sort of thing someone writes by accident.

CPython's `sre` does not release the GIL while matching, so a watchdog thread cannot
interrupt it and `signal.alarm` is POSIX-only and main-thread-only. The pattern is
therefore refused before anything runs. patterns.py states exactly what that catches
and what it does not.
"""
from __future__ import annotations

import time

import pytest
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.cli import app
from evalseal.config import ConfigError, load_scorer_config
from evalseal.patterns import UnsafePattern, check_pattern, find_nested_quantifier

runner = CliRunner()

# The construct family that blows up on a few dozen characters.
UNSAFE = [
    r"^(a+)+$", r"(a*)*", r"(\d+)*", r"([a-z]+){2,}", r"(?:a+)+", r"x(y+)+z",
    r"((a)+)+", r"(a+)+b", r"(?P<x>a+)+",
]
# Patterns a real scorer uses, plus the near-misses a sloppy check would reject.
SAFE = [
    r"^yes$", r"a+", r"(abc)+", r"[+*]+", r"\(a+\)+", r"(a+){2}", r"(a|b)+",
    r"^(?:yes|no)$", r"(\d{3})+", r"a*b*", r"(a+)", r"[a-z]+@[a-z]+",
    r"PASS|FAIL", r"^\s*42\s*$", r"(?i)yes",
]


@pytest.mark.parametrize("pattern", UNSAFE)
def test_a_nested_unbounded_quantifier_is_found(pattern):
    found = find_nested_quantifier(pattern)
    assert found is not None, pattern
    construct, offset = found
    assert pattern[offset:offset + len(construct)] == construct


@pytest.mark.parametrize("pattern", SAFE)
def test_a_pattern_a_real_scorer_uses_is_left_alone(pattern):
    """A check with false positives is a check people route around."""
    assert find_nested_quantifier(pattern) is None, pattern
    check_pattern(pattern)


def test_a_bounded_repetition_of_a_quantified_group_is_allowed():
    """`{2}` and `{2,5}` repeat a fixed number of times, so nesting cannot be
    exponential in the length of the input."""
    for pattern in (r"(a+){2}", r"(a+){2,5}", r"(a*){0,3}"):
        check_pattern(pattern)
    with pytest.raises(UnsafePattern):
        check_pattern(r"(a+){2,}")        # no upper bound: the same ambiguity


def test_a_character_class_is_not_a_quantifier():
    """`[+*]` is two literals. A naive scan would read them as nested quantifiers."""
    check_pattern(r"([+*]+)")
    check_pattern(r"(a[*+]b)+")


def test_an_escaped_parenthesis_is_not_a_group():
    check_pattern(r"\(a+\)+")


# --- what the refusal says -----------------------------------------------------------

def test_the_refusal_names_the_construct_its_offset_and_a_rewrite():
    with pytest.raises(UnsafePattern) as caught:
        check_pattern(r"^(a+)+$")
    message = str(caught.value)
    assert "'(a+)+'" in message
    assert "offset 1" in message
    assert "exponential in the length of the response" in message
    assert "25 seconds on 31 characters" in message
    assert "`a+` alone usually matches the same strings" in message
    assert "answer_match" in message
    assert "docs/operations.md" in message


def test_a_malformed_pattern_is_reported_where_it_is_configured():
    with pytest.raises(UnsafePattern, match="is not a valid regular expression"):
        check_pattern(r"(unclosed")


# --- the three places a pattern arrives ----------------------------------------------

def test_the_scorer_refuses_it_at_construction():
    """Not on the first case scored: by then a cassette is open and a run is in flight."""
    with pytest.raises(UnsafePattern):
        RegexScorer(r"^(a+)+$")
    assert RegexScorer(r"^yes$").pattern == r"^yes$"


def test_the_scorer_config_refuses_it_while_the_file_is_being_read():
    with pytest.raises(ConfigError) as caught:
        load_scorer_config({"type": "regex", "pattern": r"([a-z]+)+"})
    assert "nests an unbounded quantifier" in str(caught.value)
    assert "pattern" in str(caught.value)


def test_a_safe_scorer_config_still_loads():
    loaded = load_scorer_config({"type": "regex", "pattern": r"^(yes|no)$"})
    assert loaded.pattern == r"^(yes|no)$"


def test_the_cli_refuses_the_suite_before_it_asks_for_a_key(tmp_path, monkeypatch):
    """Exit 2 - bad input - and no key required, because nothing was going to run."""
    monkeypatch.delenv("EVALSEAL_API_KEY", raising=False)
    (tmp_path / "ds.jsonl").write_text('{"case_id": "a", "prompt": "p"}\n')
    (tmp_path / "t.json").write_text('{"model": "m"}')
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "^(a+)+$"}')
    # A wide terminal, because the assertion is on the message and rich wraps it.
    wide = CliRunner(env={"COLUMNS": "200"})
    result = wide.invoke(app, [
        "run", "--dataset", str(tmp_path / "ds.jsonl"),
        "--target-config", str(tmp_path / "t.json"),
        "--scorer-config", str(tmp_path / "s.json"),
        "--cassette", str(tmp_path / "c.json"), "--n", "2",
        "--ledger", str(tmp_path / "l.jsonl"),
    ])
    assert result.exit_code == 2, result.output
    assert "unbounded quantifier" in result.output
    assert "API key" not in result.output


# --- the cost, measured --------------------------------------------------------------

def test_a_safe_pattern_is_fast_on_a_body_at_the_transport_limit():
    """The bound that remains: a non-nested pattern over the largest readable response.

    This is the number docs/operations.md quotes. It is a floor on what a grader costs,
    not an SLA: a slower machine will be slower.
    """
    scorer = RegexScorer(r"^\s*yes\s*$")
    text = "x" * (1024 * 1024)
    started = time.perf_counter()
    for _ in range(5):
        scorer.score("q", text, None)
    elapsed = (time.perf_counter() - started) / 5
    assert elapsed < 0.5, f"{elapsed:.3f}s per match on 1 MiB"


def test_the_pattern_that_was_refused_would_have_been_slow():
    """The reproduction, shrunk so the test is quick: the growth is the point.

    Twenty characters is already measurable and 31 took 25 seconds on the build this
    replaced. Run through `re` directly, because the scorer now refuses it.
    """
    import re

    pattern = re.compile(r"^(a+)+$")
    timings = []
    for n in (16, 20):
        text = "a" * n + "b"
        started = time.perf_counter()
        pattern.search(text)
        timings.append(time.perf_counter() - started)
    # Four more characters, at least four times the work. The exact ratio depends on
    # the machine; the shape does not.
    assert timings[1] > timings[0] * 4, timings
    assert find_nested_quantifier(r"^(a+)+$") is not None
