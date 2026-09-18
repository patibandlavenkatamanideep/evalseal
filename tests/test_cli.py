"""End-to-end: record through a fake HTTP layer, then replay with no key and no network."""
from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from evalseal.cli import EXIT_UNSTABLE, app, load_dotenv
from evalseal.ledger import load_all, verify_chain

runner = CliRunner()


@pytest.fixture
def example(tmp_path):
    (tmp_path / "ds.jsonl").write_text(
        '{"case_id": "a", "prompt": "Is a hot dog a sandwich?"}\n'
        '{"case_id": "b", "prompt": "Capital of France?"}\n'
    )
    (tmp_path / "target.json").write_text(json.dumps({"model": "gpt-4o-mini"}))
    (tmp_path / "scorer.json").write_text(json.dumps(
        {"type": "llm_judge", "judge_model": "gpt-4o-mini", "rubric": "Be strict."}
    ))
    return tmp_path


def _args(d: Path, *extra: str) -> list[str]:
    # Concurrency 1 by default: the fake provider's scripted cycle is order-sensitive,
    # which is exactly what the cassette slots make irrelevant on replay.
    return ["run", "--dataset", str(d / "ds.jsonl"), "--target-config", str(d / "target.json"),
            "--scorer-config", str(d / "scorer.json"), "--n", "5",
            "--cassette", str(d / "cassette.json"), "--concurrency", "1", *extra]


def _fake_provider(monkeypatch):
    # The judge flips on the hot dog question and is consistent on Paris.
    hotdog = itertools.cycle(["PASS", "FAIL", "PASS", "FAIL", "PASS"])
    calls = []

    def fake_post(self, url, headers=None, json=None):
        calls.append({"headers": headers, "body": json})
        content = json["messages"][0]["content"]
        if "RESPONSE TO GRADE" in content:
            text = next(hotdog) if "hot dog" in content else "PASS"
        else:
            text = "Arguably yes." if "hot dog" in content else "Paris."
        return httpx.Response(
            200,
            json={"model": "gpt-4o-mini-2024-07-18", "system_fingerprint": "fp_test",
                  "choices": [{"message": {"content": text}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    return calls


def test_record_then_keyless_replay_reproduces_the_flip(example, monkeypatch):
    calls = _fake_provider(monkeypatch)
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "sk-test")
    recorded = runner.invoke(app, _args(example))
    assert recorded.exit_code == EXIT_UNSTABLE, recorded.output
    assert len(calls) == 20  # 2 cases x 5 repeats x (target + judge)
    assert "sk-test" not in (example / "cassette.json").read_text()

    # Replay: no key, no recording, and any network call would blow up.
    monkeypatch.delenv("EVALSEAL_RECORD")
    monkeypatch.delenv("EVALSEAL_API_KEY")
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: pytest.fail("network in replay"))
    # Replayed with 4 workers: slots make the result independent of completion order.
    replayed = runner.invoke(app, _args(example, "--concurrency", "4"))
    assert replayed.exit_code == EXIT_UNSTABLE, replayed.output

    first, second = load_all()
    for rec in (first, second):
        by_id = {r.case_id: r for r in rec.results}
        assert by_id["a"].scores == [1.0, 0.0, 1.0, 0.0, 1.0]
        assert by_id["a"].stability == "UNSTABLE"
        assert by_id["b"].stability == "STABLE"
        assert rec.manifest.target.served_model == "gpt-4o-mini-2024-07-18"
        assert any("TEMPERATURE NOT SET" in w for w in rec.aggregate.warnings)
    assert second.prev_hash == first.hash
    assert verify_chain() == (True, "Chain intact: 2 record(s).")
    assert Path("report.json").exists() and Path("report.md").exists()


def test_dotenv_supplies_api_key_for_recording(example, monkeypatch):
    calls = _fake_provider(monkeypatch)
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    Path(".env").write_text('# comment\nexport EVALSEAL_API_KEY="sk-from-dotenv"\n')
    try:
        result = runner.invoke(app, _args(example))
        assert result.exit_code == EXIT_UNSTABLE, result.output
        assert calls[0]["headers"]["Authorization"] == "Bearer sk-from-dotenv"
    finally:
        os.environ.pop("EVALSEAL_API_KEY", None)


def test_real_environment_wins_over_dotenv(monkeypatch):
    monkeypatch.setenv("EVALSEAL_API_KEY", "from-shell")
    Path(".env").write_text("EVALSEAL_API_KEY=from-file\n")
    load_dotenv()
    assert os.environ["EVALSEAL_API_KEY"] == "from-shell"


def test_rate_limit_is_reported_and_progress_kept(example, monkeypatch):
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        429, text="quota exceeded", request=httpx.Request("POST", url)))
    result = runner.invoke(app, _args(example, "--max-retries", "0"))
    assert result.exit_code == 1
    assert "HTTP 429" in result.output and "re-run" in result.output
    assert load_all() == []


def test_replay_without_cassette_fails_loudly(example):
    result = runner.invoke(app, _args(example))
    assert result.exit_code == 1
    assert "No cassette entry" in result.output
    assert load_all() == []
