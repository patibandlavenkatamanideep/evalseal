"""Which measurement a recorded response belongs to, and who may write it.

Two defects, both of them about identity rather than durability:

**C2-1.** The entry key was `(request, repeat)`. Two different cases that happen to ask
the same question therefore shared one entry, so one provider draw served both and
their scores agreed by construction. An eval whose whole purpose is to measure
disagreement between repeated draws was quietly measuring one draw twice.

**C2-2.** Two calls in flight for the same unit both called the provider, both wrote,
and the last write won. One of the two scores in the receipt then came from a response
the cassette no longer holds, so replaying that receipt produces a different number
than the receipt records.
"""
from __future__ import annotations

import itertools
import json
import threading
from pathlib import Path

import pytest

from evalseal.adapters.recording import (
    FORMAT_VERSION,
    Cassette,
    CassetteFormatError,
    slot,
    unit,
)

_PAYLOAD = {"url": "u", "body": {"model": "m", "prompt": "What is 2+2?"}}


def _draws(prefix="draw"):
    counter = itertools.count(1)
    return lambda: {"text": f"{prefix} #{next(counter)}"}


# --- C2-1: two cases, one question, two independent draws ---------------------------

def test_two_cases_asking_the_same_question_get_independent_draws(tmp_path):
    """The reproduction. On the baseline both cases were served `draw #1`."""
    cassette = Cassette(tmp_path / "c.json", mode="resume")
    provider = _draws()

    with unit("c0", 0):
        first = cassette.call(_PAYLOAD, provider)
    with unit("c1", 0):
        second = cassette.call(_PAYLOAD, provider)

    assert first["text"] == "draw #1"
    assert second["text"] == "draw #2"


def test_the_entry_key_distinguishes_the_case(tmp_path):
    cassette = Cassette(tmp_path / "c.json", mode="resume")
    assert cassette.entry_key(_PAYLOAD, 0, case_id="c0") != \
           cassette.entry_key(_PAYLOAD, 0, case_id="c1")


def test_the_entry_key_distinguishes_the_role(tmp_path):
    """A target call and a judge call for one unit are two different measurements."""
    cassette = Cassette(tmp_path / "c.json", mode="resume")
    assert cassette.entry_key(_PAYLOAD, 0, case_id="c0", role="target") != \
           cassette.entry_key(_PAYLOAD, 0, case_id="c0", role="judge")


def test_the_same_unit_still_replays_the_same_response(tmp_path):
    """The property that made the repeat part of the key in the first place."""
    path = tmp_path / "c.json"
    recorded = Cassette(path, mode="resume")
    with unit("c0", 3):
        first = recorded.call(_PAYLOAD, _draws())

    with unit("c0", 3):
        assert Cassette(path, mode="replay").call(_PAYLOAD, _draws("other")) == first


def test_repeats_of_one_case_stay_independent(tmp_path):
    cassette = Cassette(tmp_path / "c.json", mode="resume")
    provider = _draws()
    texts = []
    for repeat in range(3):
        with unit("c0", repeat):
            texts.append(cassette.call(_PAYLOAD, provider)["text"])
    assert texts == ["draw #1", "draw #2", "draw #3"]


def test_a_whole_run_of_duplicate_prompts_measures_real_variance(tmp_path):
    """End to end: a suite whose cases are deliberately identical.

    Two cases with the same prompt and a target that alternates. With one shared draw
    per repeat both cases scored identically and the suite reported zero flips; with
    independent draws they flip, which is what the target actually does.
    """
    from conftest import scripted

    from evalseal.adapters.dataset import Case, Dataset
    from evalseal.adapters.scorer import RegexScorer
    from evalseal.adapters.target import LocalCallableTarget
    from evalseal.executor import run_eval

    dataset = Dataset([Case("c0", "same prompt", None), Case("c1", "same prompt", None)],
                      hash="sha256:test")
    target = LocalCallableTarget(scripted({"same prompt": ["yes", "no"]}))
    record = run_eval(dataset, target, RegexScorer("yes"), n_repeats=4)

    # Each case draws its own alternating sequence rather than sharing one.
    assert [c.scores for c in record.results] == [
        [1.0, 0.0, 1.0, 0.0], [1.0, 0.0, 1.0, 0.0]]


# --- C2-2: one defined outcome for a duplicated unit ---------------------------------

def test_a_duplicated_unit_gets_one_response(tmp_path):
    """The reproduction: two in-flight calls for one unit used to produce two
    responses, one of which the cassette then threw away - so one of the two sealed
    scores came from bytes a replay will never return."""
    path = tmp_path / "c.json"
    cassette = Cassette(path, mode="resume")
    inside = threading.Event()        # the first caller has reached the provider
    release = threading.Event()       # ...and may now return
    scored: dict[str, str] = {}
    called: list[str] = []

    def worker(name: str, text: str) -> None:
        def provider() -> dict:
            called.append(name)
            inside.set()
            release.wait(timeout=10)
            return {"text": text}
        with unit("c0", 0):
            scored[name] = cassette.call(_PAYLOAD, provider)["text"]

    first = threading.Thread(target=worker, args=("x", "ANSWER-X"))
    first.start()
    assert inside.wait(timeout=10), "the first caller never reached the provider"
    # Only now start the second, so it is guaranteed to find the draw in flight.
    second = threading.Thread(target=worker, args=("y", "ANSWER-Y"))
    second.start()
    release.set()
    for t in (first, second):
        t.join(timeout=10)
        assert not t.is_alive(), "a caller is still waiting on the draw"

    assert called == ["x"], "only one caller may reach the provider for one unit"
    assert set(scored.values()) == {"ANSWER-X"}, scored
    with unit("c0", 0):
        replayed = Cassette(path, mode="replay").call(_PAYLOAD, _draws("other"))
    assert replayed["text"] == next(iter(scored.values()))


def test_a_failed_call_does_not_block_the_next_one(tmp_path):
    """Single-flight must release its claim when the provider raises, or one transient
    failure would deadlock every later call for that unit."""
    cassette = Cassette(tmp_path / "c.json", mode="resume")

    def boom() -> dict:
        raise RuntimeError("provider is down")

    with unit("c0", 0), pytest.raises(RuntimeError, match="provider is down"):
        cassette.call(_PAYLOAD, boom)
    with unit("c0", 0):
        assert cassette.call(_PAYLOAD, _draws())["text"] == "draw #1"


# --- explicit modes ------------------------------------------------------------------

def test_replay_refuses_to_call_the_provider(tmp_path):
    cassette = Cassette(tmp_path / "c.json", mode="replay")
    with unit("c0", 0), pytest.raises(RuntimeError, match="No cassette entry"):
        cassette.call(_PAYLOAD, _draws())


def test_resume_reuses_what_is_there_and_records_what_is_not(tmp_path):
    path = tmp_path / "c.json"
    with unit("c0", 0):
        Cassette(path, mode="resume").call(_PAYLOAD, _draws("first"))

    resumed = Cassette(path, mode="resume")
    with unit("c0", 0):
        assert resumed.call(_PAYLOAD, _draws("second"))["text"] == "first #1"
    with unit("c0", 1):
        assert resumed.call(_PAYLOAD, _draws("second"))["text"] == "second #1"


def test_fresh_ignores_what_is_on_disk(tmp_path):
    """A new experiment must not silently inherit another run's draws."""
    path = tmp_path / "c.json"
    with unit("c0", 0):
        Cassette(path, mode="resume").call(_PAYLOAD, _draws("old"))

    with unit("c0", 0):
        assert Cassette(path, mode="fresh").call(
            _PAYLOAD, _draws("new"))["text"] == "new #1"


def test_fresh_still_gives_one_answer_per_unit(tmp_path):
    """Fresh means "do not inherit", not "draw again every time it is asked"."""
    cassette = Cassette(tmp_path / "c.json", mode="fresh")
    provider = _draws()
    with unit("c0", 0):
        first = cassette.call(_PAYLOAD, provider)
    with unit("c0", 0):
        again = cassette.call(_PAYLOAD, provider)
    assert first == again


def test_the_mode_is_named_in_the_record_env_var_for_compatibility(tmp_path, monkeypatch):
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    assert Cassette(tmp_path / "c.json").mode == "resume"
    monkeypatch.setenv("EVALSEAL_RECORD", "0")
    assert Cassette(tmp_path / "c.json").mode == "replay"
    monkeypatch.setenv("EVALSEAL_MODE", "fresh")
    assert Cassette(tmp_path / "c.json").mode == "fresh"


def test_the_record_flag_still_works(tmp_path):
    """`record=` is the published argument; it keeps meaning what it meant."""
    assert Cassette(tmp_path / "c.json", record=True).mode == "resume"
    assert Cassette(tmp_path / "c.json", record=False).mode == "replay"
    assert Cassette(tmp_path / "c.json", record=True).record is True


def test_an_unknown_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        Cassette(tmp_path / "c.json", mode="whatever")


# --- the older format stays replayable ------------------------------------------------

def _legacy(path, payload, index, response):
    """A format-2 cassette, keyed the way format 2 keyed things."""
    import hashlib

    blob = json.dumps({"request": payload, "repeat": index},
                      sort_keys=True, separators=(",", ":")).encode()
    path.write_text(json.dumps({
        "version": 2,
        "entries": {hashlib.sha256(blob).hexdigest(): response}}), encoding="utf-8")


def test_a_format_2_cassette_still_replays(tmp_path):
    """Invariant 4's shape, one layer down: an artifact recorded by an older build
    must keep working, read by the rules that wrote it."""
    path = tmp_path / "old.json"
    _legacy(path, _PAYLOAD, 0, {"text": "recorded in 2026"})

    cassette = Cassette(path, mode="replay")
    assert cassette.format == 2
    with unit("c0", 0):
        assert cassette.call(_PAYLOAD, _draws())["text"] == "recorded in 2026"


def test_a_format_2_cassette_cannot_tell_two_cases_apart(tmp_path):
    """Replayable, and honest about what it cannot do: this is exactly C2-1, which is
    why the format was bumped rather than patched."""
    path = tmp_path / "old.json"
    _legacy(path, _PAYLOAD, 0, {"text": "one draw"})
    cassette = Cassette(path, mode="replay")

    with unit("c0", 0):
        first = cassette.call(_PAYLOAD, _draws())
    with unit("c1", 0):
        second = cassette.call(_PAYLOAD, _draws())
    assert first == second


def test_recording_into_a_format_2_cassette_is_refused(tmp_path):
    """Appending format-3 entries to a format-2 file would make a mixed-key cassette
    whose misses and hits depend on which half of the file answers."""
    path = tmp_path / "old.json"
    _legacy(path, _PAYLOAD, 0, {"text": "one draw"})
    with pytest.raises(CassetteFormatError, match="re-record"):
        Cassette(path, mode="resume")


def test_a_run_against_a_format_2_cassette_says_so(tmp_path):
    """A caveat the numbers depend on belongs in the receipt, not in a changelog."""
    from conftest import make_dataset

    from evalseal.adapters.scorer import RegexScorer
    from evalseal.adapters.target import OpenAICompatibleTarget
    from evalseal.executor import run_eval

    path = tmp_path / "old.json"
    body = {"model": "m", "messages": [{"role": "user", "content": "q0"}]}
    reply = {"model": "m", "choices": [{"message": {"content": "yes"},
                                        "finish_reason": "stop"}]}
    _legacy(path, {"url": "https://api.openai.com/v1", "body": body}, 0, reply)

    target = OpenAICompatibleTarget(model="m", cassette=Cassette(path, mode="replay"))
    record = run_eval(make_dataset("q0"), target, RegexScorer("yes"), n_repeats=1)
    assert any("CASSETTE FORMAT 2" in w for w in record.aggregate.warnings), \
        record.aggregate.warnings


def test_a_new_cassette_is_written_in_the_current_format(tmp_path):
    path = tmp_path / "new.json"
    with unit("c0", 0):
        Cassette(path, mode="resume").call(_PAYLOAD, _draws())
    assert json.loads(path.read_text())["version"] == FORMAT_VERSION


def test_an_unsupported_future_format_is_refused(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"version": 99, "entries": {}}), encoding="utf-8")
    with pytest.raises(CassetteFormatError):
        Cassette(path, mode="replay")


# --- the published helper keeps working -----------------------------------------------

def test_slot_still_pins_the_repeat(tmp_path):
    """`slot` is exported from the package root. It keeps working, without case
    identity, which is why the executor uses `unit` instead."""
    cassette = Cassette(tmp_path / "c.json", mode="resume")
    provider = _draws()
    with slot(0):
        first = cassette.call(_PAYLOAD, provider)
    with slot(1):
        second = cassette.call(_PAYLOAD, provider)
    assert first != second
    with slot(0):
        assert cassette.call(_PAYLOAD, provider) == first


# --- the mode is reachable from the command line --------------------------------------

def test_the_run_command_takes_a_mode(tmp_path, monkeypatch):
    """The mode decides whether a run measures anything, so it is a flag, not only an
    environment variable someone has to remember to unset."""
    import shutil

    from typer.testing import CliRunner

    from evalseal.cli import app

    repo = Path(__file__).resolve().parent.parent
    workspace = tmp_path / "ws"
    shutil.copytree(repo / "examples" / "gsm8k", workspace / "examples" / "gsm8k")
    (workspace / "tests" / "cassettes").mkdir(parents=True)
    shutil.copy(repo / "tests" / "cassettes" / "gsm8k_lite.json",
                workspace / "tests" / "cassettes" / "gsm8k_lite.json")
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(app, [
        "run", "--suite", "examples/gsm8k/suite-lite.json", "--quiet",
        "--cassette-mode", "replay", "--ledger", str(tmp_path / "l.jsonl")])
    assert result.exit_code == 0, result.output


def test_an_unknown_mode_is_rejected_by_the_command_line(tmp_path):
    from typer.testing import CliRunner

    from evalseal.cli import app

    suite = Path(__file__).resolve().parent.parent / "examples/gsm8k/suite-lite.json"
    result = CliRunner().invoke(app, [
        "run", "--suite", str(suite), "--cassette-mode", "rewind"])
    assert result.exit_code != 0
    assert "rewind" in result.output
