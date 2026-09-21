"""The CI-facing surface: failure policy, JUnit output, exit codes."""
from __future__ import annotations

from pathlib import Path
from xml.etree.ElementTree import fromstring

from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import EXIT_UNSTABLE, app
from evalseal.executor import run_eval
from evalseal.report import failing_cases, to_junit

runner = CliRunner()


def _record():
    target = LocalCallableTarget(scripted({
        "stable": ["yes"],
        "borderline": ["yes", "yes", "yes", "yes", "no"],
        "unstable": ["yes", "no", "yes", "no", "yes"],
    }))
    ds = make_dataset("stable", "borderline", "unstable")
    return run_eval(ds, target, RegexScorer(r"^yes$"), n_repeats=5)


def test_fail_on_policies_select_different_cases():
    """At N=5 the "borderline" case is UNSTABLE too, so both policies select both.

    Under the Wilson rule a 4/5 case is not borderline: five runs do not establish its
    direction. BORDERLINE needs a larger N to exist at all, which is why the two
    policies no longer differ on this fixture.
    """
    rec = _record()
    assert [r.case_id for r in failing_cases(rec, "none")] == []
    assert [r.stability for r in failing_cases(rec, "unstable")] == ["UNSTABLE", "UNSTABLE"]
    assert sorted(r.stability for r in failing_cases(rec, "borderline")) == [
        "UNSTABLE", "UNSTABLE",
    ]


def test_junit_marks_only_policy_violations_as_failures():
    rec = _record()
    root = fromstring(to_junit(rec, "unstable"))
    suite = root.find("testsuite")
    assert suite.get("tests") == "3"
    # Two cases are UNSTABLE at N=5: the 3/5 case and the 4/5 one.
    assert suite.get("failures") == "2"

    by_name = {c.get("name"): c for c in suite.findall("testcase")}
    failed = [name for name, c in by_name.items() if c.find("failure") is not None]
    assert len(failed) == 2
    failure = by_name[failed[0]].find("failure")
    assert failure.get("type") == "UNSTABLE"
    assert "95% CI" in failure.text
    messages = " ".join(
        by_name[n].find("failure").get("message") for n in failed)
    assert "flip rate 40%" in messages and "flip rate 20%" in messages

    # The stricter policy selects the same set, since BORDERLINE cannot occur at N=5.
    strict = fromstring(to_junit(rec, "borderline")).find("testsuite")
    assert strict.get("failures") == "2"


def test_junit_records_provenance_properties():
    rec = _record()
    suite = fromstring(to_junit(rec)).find("testsuite")
    props = {p.get("name"): p.get("value") for p in suite.find("properties")}
    assert props["n_repeats"] == "5"
    assert props["dataset_hash"] == rec.manifest.dataset.hash
    assert props["model"] == "local"


def _cli_args(tmp_path: Path, *extra: str) -> list[str]:
    (tmp_path / "ds.jsonl").write_text('{"case_id": "only", "prompt": "p"}\n')
    (tmp_path / "t.json").write_text('{"model": "m"}')
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}')
    return ["run", "--dataset", str(tmp_path / "ds.jsonl"),
            "--target-config", str(tmp_path / "t.json"),
            "--scorer-config", str(tmp_path / "s.json"),
            "--cassette", str(tmp_path / "c.json"), "--n", "5", *extra]


def test_fail_on_none_exits_zero_even_when_unstable(tmp_path, monkeypatch):
    import httpx
    answers = iter(["yes", "no"] * 10)
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, json={"model": "m", "choices": [{"message": {"content": next(answers)}}]},
        request=httpx.Request("POST", url)))

    result = runner.invoke(app, _cli_args(tmp_path, "--concurrency", "1",
                                          "--fail-on", "none",
                                          "--junit-xml", str(tmp_path / "junit.xml")))
    assert result.exit_code == 0, result.output

    suite = fromstring((tmp_path / "junit.xml").read_text()).find("testsuite")
    assert suite.get("failures") == "0"          # policy said don't fail
    assert suite.findall("testcase")[0].get("name") == "only"


def test_default_policy_still_fails_on_unstable(tmp_path, monkeypatch):
    import httpx
    answers = iter(["yes", "no"] * 10)
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, json={"model": "m", "choices": [{"message": {"content": next(answers)}}]},
        request=httpx.Request("POST", url)))

    result = runner.invoke(app, _cli_args(tmp_path, "--concurrency", "1"))
    assert result.exit_code == EXIT_UNSTABLE
    assert "fail --fail-on unstable" in result.output
    assert "only" in result.output


def test_public_api_is_importable_and_typed():
    from importlib.resources import files

    import evalseal
    assert evalseal.__version__ == "1.7.0"
    for name in ("run_eval", "analyze_case", "verify_chain", "Cassette", "to_junit"):
        assert name in evalseal.__all__ and hasattr(evalseal, name)
    assert (files("evalseal") / "py.typed").is_file()   # PEP 561 marker ships
