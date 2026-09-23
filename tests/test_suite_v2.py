"""The sectioned suite format, and the property that makes it safe to adopt.

A file format change is only worth making if the old files keep working and the new
ones mean the same thing. Both halves are asserted here: the flat format still loads,
and a v2 suite describing the same evaluation produces the *same evaluator
fingerprint*, so moving a suite to v2 does not silently make it incomparable with
every receipt sealed before the move.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

from evalseal.cli import _load_suite, _suite_section_hashes

REPO = Path(__file__).resolve().parent.parent
FLAT = REPO / "examples" / "gsm8k" / "suite.json"
V2 = REPO / "examples" / "gsm8k" / "suite-v2.json"


def _write(tmp_path: Path, data: dict, name: str = "suite.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _v2(**sections) -> dict:
    base = {
        "version": 2,
        "evaluator": {"scorer": "scorer.json"},
        "target": {"config": "target.json"},
        "dataset": {"path": "dataset.jsonl"},
        "run": {"n_repeats": 5},
    }
    base.update(sections)
    return base


# --- both formats load ----------------------------------------------------------------

def test_the_flat_format_still_loads():
    cfg = _load_suite(FLAT)
    assert cfg["n_repeats"] == 5
    assert cfg["dataset"].endswith("dataset.jsonl")
    assert cfg["scorer"].endswith("scorer.json")


def test_the_two_example_suites_describe_the_same_evaluation():
    """The v2 example must not quietly differ from the flat one it replaces."""
    flat, v2 = _load_suite(FLAT), _load_suite(V2)
    for key in ("dataset", "target", "scorer", "cassette", "n_repeats",
                "concurrency", "fail_on"):
        assert flat[key] == v2[key], key


def test_sections_are_flattened_onto_the_names_the_commands_use(tmp_path):
    cfg = _load_suite(_write(tmp_path, _v2()))
    assert cfg["scorer"].endswith("scorer.json")
    assert cfg["target"].endswith("target.json")
    assert cfg["dataset"].endswith("dataset.jsonl")
    assert cfg["n_repeats"] == 5


def test_paths_resolve_against_the_suite_file_in_both_formats(tmp_path):
    nested = tmp_path / "suites"
    nested.mkdir()
    cfg = _load_suite(_write(nested, _v2()))
    assert cfg["dataset"] == str((nested / "dataset.jsonl").resolve())


# --- a typo is refused in either format --------------------------------------------------

def test_an_unknown_section_is_refused(tmp_path):
    with pytest.raises(typer.BadParameter) as e:
        _load_suite(_write(tmp_path, _v2(evalutor={"scorer": "s.json"})))
    assert "unknown section(s)" in str(e.value)
    assert "evalutor" in str(e.value)


def test_an_unknown_key_inside_a_section_is_refused(tmp_path):
    with pytest.raises(typer.BadParameter) as e:
        _load_suite(_write(tmp_path, _v2(run={"n_repeats": 5, "n_repeat": 3})))
    assert "section 'run'" in str(e.value)
    assert "n_repeat" in str(e.value)


def test_a_section_that_is_not_a_mapping_is_refused(tmp_path):
    with pytest.raises(typer.BadParameter) as e:
        _load_suite(_write(tmp_path, _v2(evaluator=["scorer.json"])))
    assert "must be a mapping" in str(e.value)


def test_an_unknown_version_is_refused_rather_than_guessed(tmp_path):
    with pytest.raises(typer.BadParameter) as e:
        _load_suite(_write(tmp_path, {"version": 99, "run": {"n_repeats": 5}}))
    assert "unknown suite version" in str(e.value)


def test_the_flat_format_still_refuses_unknown_keys(tmp_path):
    with pytest.raises(typer.BadParameter) as e:
        _load_suite(_write(tmp_path, {"dataset": "d.jsonl", "n_repeat": 5}))
    assert "unknown key(s)" in str(e.value)


def test_a_suite_that_is_not_a_mapping_is_refused(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(typer.BadParameter) as e:
        _load_suite(path)
    assert "mapping at the top level" in str(e.value)


# --- the section digests ------------------------------------------------------------------

def test_section_digests_are_recorded_for_a_v2_suite(tmp_path):
    digests = _suite_section_hashes(_write(tmp_path, _v2()))
    assert digests["evaluator_hash"].startswith("sha256:")
    assert digests["target_hash"].startswith("sha256:")
    assert digests["evaluator_hash"] != digests["target_hash"]


def test_a_flat_suite_has_no_section_digests():
    """There are no sections to hash apart, and a made-up digest would be worse."""
    assert _suite_section_hashes(FLAT) == {"evaluator_hash": None, "target_hash": None}


def test_changing_the_target_leaves_the_evaluator_digest_alone(tmp_path):
    before = _suite_section_hashes(_write(tmp_path, _v2(), "a.json"))
    after = _suite_section_hashes(
        _write(tmp_path, _v2(target={"config": "other-target.json"}), "b.json"))
    assert after["evaluator_hash"] == before["evaluator_hash"]
    assert after["target_hash"] != before["target_hash"]


def test_changing_the_evaluator_leaves_the_target_digest_alone(tmp_path):
    before = _suite_section_hashes(_write(tmp_path, _v2(), "a.json"))
    after = _suite_section_hashes(
        _write(tmp_path, _v2(evaluator={"scorer": "strict.json"}), "b.json"))
    assert after["evaluator_hash"] != before["evaluator_hash"]
    assert after["target_hash"] == before["target_hash"]


def test_section_digests_ignore_key_order(tmp_path):
    """Canonical JSON, so reformatting a suite is not a change to it."""
    a = _write(tmp_path, {"version": 2, "evaluator": {"scorer": "s.json"},
                          "target": {"config": "t.json"}}, "a.json")
    b = _write(tmp_path, {"target": {"config": "t.json"}, "version": 2,
                          "evaluator": {"scorer": "s.json"}}, "b.json")
    assert _suite_section_hashes(a) == _suite_section_hashes(b)


# --- what the split is for: telling a grading change from a model change ------------------

def _project(tmp_path: Path, *, scorer: dict, model: str, name: str) -> Path:
    """A self-contained v2 suite with its own scorer and target files."""
    room = tmp_path / name
    room.mkdir()
    (room / "dataset.jsonl").write_text(
        '{"case_id": "c1", "prompt": "p", "expected": "yes"}\n', encoding="utf-8")
    (room / "scorer.json").write_text(json.dumps(scorer), encoding="utf-8")
    (room / "target.json").write_text(json.dumps({"model": model}), encoding="utf-8")
    return _write(room, {
        "version": 2,
        "evaluator": {"scorer": "scorer.json"},
        "target": {"config": "target.json"},
        "dataset": {"path": "dataset.jsonl"},
        "run": {"n_repeats": 5, "cassette": "cassette.json", "fail_on": "none"},
    })


def _run(suite: Path, ledger: Path, monkeypatch):
    import httpx
    from typer.testing import CliRunner

    from evalseal.cli import app

    model = json.loads((suite.parent / "target.json").read_text())["model"]
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, json={"model": model, "choices": [{"message": {"content": "yes"}}]},
        request=httpx.Request("POST", url)))
    result = CliRunner().invoke(
        app, ["run", "--suite", str(suite), "--ledger", str(ledger), "--quiet"])
    assert result.exit_code == 0, result.output


def test_same_evaluator_different_target_stays_comparable(tmp_path, monkeypatch):
    """Swapping the model under test is the reason to run a benchmark at all."""
    from evalseal.diffing import diff_records
    from evalseal.ledger import load_all

    ledger = tmp_path / "ledger.jsonl"
    scorer = {"type": "regex", "pattern": "^yes$"}
    _run(_project(tmp_path, scorer=scorer, model="model-a", name="a"), ledger, monkeypatch)
    _run(_project(tmp_path, scorer=scorer, model="model-b", name="b"), ledger, monkeypatch)

    before, after = load_all(ledger)
    result = diff_records(before, after)
    assert result.comparable
    assert [c.name for c in result.evaluator_changes] == []


def test_a_changed_target_is_reported_as_a_target_change_not_evaluator_drift(
        tmp_path, monkeypatch):
    from evalseal.drift import TARGET_CHANGE, analyze_drift
    from evalseal.ledger import load_all

    ledger = tmp_path / "ledger.jsonl"
    scorer = {"type": "regex", "pattern": "^yes$"}
    _run(_project(tmp_path, scorer=scorer, model="model-a", name="a"), ledger, monkeypatch)
    _run(_project(tmp_path, scorer=scorer, model="model-b", name="b"), ledger, monkeypatch)

    before, after = load_all(ledger)
    report = analyze_drift(before, after)
    assert report.kind == TARGET_CHANGE
    assert report.comparable
    assert "not drift" in report.summary()


def test_a_changed_evaluator_becomes_non_comparable(tmp_path, monkeypatch):
    from evalseal.diffing import diff_records
    from evalseal.ledger import load_all

    ledger = tmp_path / "ledger.jsonl"
    _run(_project(tmp_path, scorer={"type": "regex", "pattern": "^yes$"},
                  model="model-a", name="a"), ledger, monkeypatch)
    _run(_project(tmp_path, scorer={"type": "regex", "pattern": "^no$"},
                  model="model-a", name="b"), ledger, monkeypatch)

    before, after = load_all(ledger)
    result = diff_records(before, after)
    assert not result.comparable
    assert result.evaluator_changes, "the changed grader must be named"


def test_the_evaluator_fingerprint_does_not_depend_on_the_suite_format(
        tmp_path, monkeypatch):
    """Moving a suite to v2 must not make it incomparable with older receipts.

    This is the property that makes the format change adoptable: the section digests
    are recorded, not fingerprinted, so the fingerprint is unchanged by the move.
    """
    from evalseal.ledger import evaluator_fingerprint, load_all

    ledger = tmp_path / "ledger.jsonl"
    v2_suite = _project(tmp_path, scorer={"type": "regex", "pattern": "^yes$"},
                        model="model-a", name="v2")
    _run(v2_suite, ledger, monkeypatch)

    flat_room = v2_suite.parent
    flat = flat_room / "flat.json"
    flat.write_text(json.dumps({
        "dataset": "dataset.jsonl", "scorer": "scorer.json", "target": "target.json",
        "n_repeats": 5, "cassette": "cassette.json", "fail_on": "none",
    }), encoding="utf-8")
    _run(flat, ledger, monkeypatch)

    from_v2, from_flat = load_all(ledger)
    assert evaluator_fingerprint(from_v2) == evaluator_fingerprint(from_flat)
    assert from_v2.manifest.suite.evaluator_hash is not None
    assert from_flat.manifest.suite.evaluator_hash is None
