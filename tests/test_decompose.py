"""Separating judge variance from target variance on one suite.

The arms are driven by scripted local targets, so each test fixes exactly which
component is allowed to vary and asserts that the decomposition finds it there.
"""
from __future__ import annotations

import itertools
import json

import pytest
from typer.testing import CliRunner

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import LLMJudgeScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.decompose import decompose, render

runner = CliRunner()
RUBRIC = "PASS if good."


def _dataset(*ids):
    return Dataset([Case(i, f"prompt for {i}") for i in ids], hash="sha256:test")


def _cycling(values):
    """A target whose replies cycle, ignoring the prompt: variance on demand."""
    it = itertools.cycle(values)
    return LocalCallableTarget(lambda _prompt: next(it), name="scripted")


def _arms(target_replies, judge_replies):
    """Build both arms with independent cycles, as separate cassettes would give."""
    def build():
        return (
            _cycling(list(target_replies)),
            LLMJudgeScorer(judge=_cycling(list(judge_replies)), rubric=RUBRIC),
        )
    return build(), build()


def test_a_flaky_judge_on_a_fixed_response_shows_up_in_the_judge_only_arm():
    """The target says the same thing every time; only the judge wavers."""
    (jt, js), (ft, fs) = _arms(["same answer"], ["PASS", "PASS", "FAIL", "PASS", "PASS"])
    result = decompose(_dataset("a"), jt, js, ft, fs, n_repeats=5)

    case = result.cases[0]
    assert case.judge_only_verdicts == [1, 1, 0, 1, 1]
    assert case.judge_only_flip_rate == pytest.approx(0.2)
    # The full arm sees the same judge cycle over an unchanging response, so it matches.
    assert case.full_flip_rate == pytest.approx(0.2)
    assert case.judge_share == pytest.approx(1.0)


def test_a_stable_judge_leaves_the_judge_only_arm_flat():
    """A judge that always says PASS cannot produce a flip, whatever the target does."""
    (jt, js), (ft, fs) = _arms(["a", "b", "c", "d", "e"], ["PASS"])
    result = decompose(_dataset("a"), jt, js, ft, fs, n_repeats=5)

    assert result.cases[0].judge_only_flip_rate == 0.0
    assert result.cases[0].full_flip_rate == 0.0
    assert result.overall_judge_share is None      # nothing to apportion


def test_variance_that_only_appears_when_the_target_varies_is_attributed_to_the_target():
    """The judge is a pure function of the response, so it cannot flip on a fixed one.

    In the judge_only arm the response is pinned, so the judge returns the same verdict
    five times. In the full arm the target alternates between a good and a bad answer,
    and the same deterministic judge flips with it. That is target variance, and the
    decomposition has to place it there rather than blaming the grader.
    """
    # The marker must not occur in the rubric or the case prompt, both of which are
    # embedded in what the judge sees.
    def judge_of_text():
        return LLMJudgeScorer(
            judge=LocalCallableTarget(
                lambda prompt: "PASS" if "ZEBRA" in prompt else "FAIL", name="judge"),
            rubric=RUBRIC,
        )

    jt = _cycling(["ZEBRA answer"])
    ft = _cycling(["ZEBRA answer", "walrus answer"])
    result = decompose(_dataset("a"), jt, judge_of_text(), ft, judge_of_text(), n_repeats=5)

    case = result.cases[0]
    assert case.judge_only_flip_rate == 0.0        # judge is stable on a fixed response
    assert case.full_flip_rate == pytest.approx(0.4)   # PFPFP -> 2 of 5 disagree
    assert case.judge_share == 0.0                 # none of it is the judge's


def test_judge_share_is_capped_at_one():
    """The arms are separate experiments, so the judge arm can exceed the full arm."""
    (jt, js), (ft, fs) = _arms(["x"], ["PASS", "FAIL"])
    ft = _cycling(["x"])
    fs = LLMJudgeScorer(judge=_cycling(["PASS"]), rubric=RUBRIC)
    result = decompose(_dataset("a"), jt, js, ft, fs, n_repeats=5)
    assert result.cases[0].full_flip_rate == 0.0
    assert result.cases[0].judge_share is None     # no denominator, so no share


def test_aggregates_average_over_cases():
    (jt, js), (ft, fs) = _arms(["same"], ["PASS", "PASS", "FAIL", "PASS", "PASS"])
    result = decompose(_dataset("a", "b", "c", "d"), jt, js, ft, fs, n_repeats=5)
    assert len(result.cases) == 4
    assert 0 < result.mean_judge_only_flip_rate <= 1
    assert result.n_flipped_judge_only >= 1
    payload = result.to_dict()
    assert payload["n_cases"] == 4
    assert len(payload["cases"]) == 4


def test_one_repeat_cannot_show_a_flip_and_is_rejected():
    (jt, js), (ft, fs) = _arms(["x"], ["PASS"])
    with pytest.raises(ValueError, match="n_repeats >= 2"):
        decompose(_dataset("a"), jt, js, ft, fs, n_repeats=1)


def test_render_states_the_lower_bound_caveat():
    (jt, js), (ft, fs) = _arms(["same"], ["PASS", "PASS", "FAIL", "PASS", "PASS"])
    text = render(decompose(_dataset("a", "b"), jt, js, ft, fs, n_repeats=5))
    assert "judge_only" in text and "target_and_judge" in text
    assert "lower bound on the judge's contribution" in text


def test_render_says_so_when_there_is_nothing_to_apportion():
    (jt, js), (ft, fs) = _arms(["x"], ["PASS"])
    text = render(decompose(_dataset("a"), jt, js, ft, fs, n_repeats=5))
    assert "no variance here to apportion" in text


# --- the CLI ----------------------------------------------------------------------

def _configs(tmp_path, scorer_type="llm_judge"):
    (tmp_path / "ds.jsonl").write_text('{"case_id": "only", "prompt": "p"}\n')
    (tmp_path / "t.json").write_text('{"model": "m"}')
    scorer = (
        '{"type": "llm_judge", "judge_model": "j", "rubric": "r"}'
        if scorer_type == "llm_judge" else '{"type": "regex", "pattern": "yes"}'
    )
    (tmp_path / "s.json").write_text(scorer)
    return ["decompose", "--dataset", str(tmp_path / "ds.jsonl"),
            "--target-config", str(tmp_path / "t.json"),
            "--scorer-config", str(tmp_path / "s.json"),
            "--cassette", str(tmp_path / "c.json"), "--n", "5"]


def test_cli_refuses_a_deterministic_scorer(tmp_path):
    """With arithmetic grading there is no judge variance to separate out."""
    result = runner.invoke(app, _configs(tmp_path, scorer_type="regex"))
    assert result.exit_code == 2
    assert "only applies to an llm_judge scorer" in result.output


def test_cli_gives_each_arm_its_own_cassette(tmp_path, monkeypatch):
    """One shared cassette would couple the arms whenever two responses coincide."""
    import httpx
    answers = itertools.cycle(["yes", "PASS", "no", "FAIL"])
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, request=httpx.Request("POST", url),
        json={"model": "m", "choices": [{"message": {"content": next(answers)}}]}))

    result = runner.invoke(app, [*_configs(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "c.judge_only.json").exists()
    assert (tmp_path / "c.full.json").exists()

    payload = json.loads(result.output)
    assert payload["n_cases"] == 1
    assert len(payload["cases"][0]["judge_only_verdicts"]) == 5
    assert len(payload["cases"][0]["full_verdicts"]) == 5


# --- the recorded experiment behind the README claim ------------------------------

def test_the_recorded_decomposition_replays_to_the_numbers_in_the_readme(tmp_path):
    """Guards the README's "where the variance comes from" section.

    Recorded live against gemini-2.5-flash on 2026-09-21 and committed, so this replays
    with no key and no network. If the numbers here move, the README is wrong.
    """
    import shutil
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    base = tmp_path / "decompose_borderline.json"
    for arm in ("judge_only", "full"):
        src = repo / "tests" / "cassettes" / f"decompose_borderline.{arm}.json"
        shutil.copy(src, tmp_path / f"decompose_borderline.{arm}.json")

    result = runner.invoke(app, [
        "decompose",
        "--dataset", str(repo / "examples/borderline_judge/dataset.jsonl"),
        "--target-config", str(repo / "examples/borderline_judge/target.json"),
        "--scorer-config", str(repo / "examples/borderline_judge/scorer.json"),
        "--cassette", str(base), "--n", "5", "--json",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["n_cases"] == 20
    assert payload["mean_judge_only_flip_rate"] == pytest.approx(0.06)
    assert payload["mean_full_flip_rate"] == pytest.approx(0.05)
    assert payload["n_flipped_judge_only"] == 3
    assert payload["n_flipped_full"] == 3

    by_id = {c["case_id"]: c for c in payload["cases"]}
    # The judge disagrees with itself on a frozen response for these two cases.
    assert by_id["b16"]["judge_only_flip_rate"] == pytest.approx(0.4)
    assert by_id["b19"]["judge_only_flip_rate"] == pytest.approx(0.4)
    assert by_id["b16"]["full_flip_rate"] == pytest.approx(0.4)
    assert by_id["b19"]["full_flip_rate"] == pytest.approx(0.4)
    # And the two arms disagree on these, which is sampling noise, not a finding.
    assert by_id["b01"]["judge_only_flip_rate"] == pytest.approx(0.4)
    assert by_id["b01"]["full_flip_rate"] == 0.0
    assert by_id["b10"]["judge_only_flip_rate"] == 0.0
    assert by_id["b10"]["full_flip_rate"] == pytest.approx(0.2)


def test_the_recorded_decomposition_is_below_the_significance_floor():
    """Three flipping cases cannot reach significance; the README says so."""
    from evalseal.power import min_discordant_items
    assert min_discordant_items(0.05) > 3
