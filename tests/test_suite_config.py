"""A suite file collects the run's settings; explicit flags still win."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from typer.testing import CliRunner

from evalseal.cli import app
from evalseal.ledger import load_all

runner = CliRunner()


def plain(output: str) -> str:
    """Strip ANSI styling and wrapping. Rich colours the `--` of an option separately
    from its name, so a literal "--dataset" never appears in the raw output."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    return re.sub(r"\s+", " ", text)


def _suite(tmp_path: Path, **overrides) -> Path:
    (tmp_path / "ds.jsonl").write_text(
        '{"case_id": "a", "prompt": "2+2?", "expected": "4"}\n'
        '{"case_id": "b", "prompt": "3+3?", "expected": "6"}\n'
    )
    (tmp_path / "t.json").write_text(json.dumps({"model": "m"}))
    (tmp_path / "s.json").write_text(json.dumps({"type": "answer_match"}))
    suite = {
        "dataset": "ds.jsonl", "target": "t.json", "scorer": "s.json",
        "n_repeats": 3, "concurrency": 1, "cassette": "cass.json", "fail_on": "none",
        **overrides,
    }
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(suite))
    return path


def _provider(monkeypatch, answers=("4", "6")):
    def fake_post(self, url, headers=None, json=None):
        q = json["messages"][0]["content"]
        text = answers[0] if "2+2" in q else answers[1]
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": text}}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")


def test_suite_supplies_everything(tmp_path, monkeypatch):
    _provider(monkeypatch)
    suite = _suite(tmp_path)
    result = runner.invoke(
        app, ["run", "--suite", str(suite), "--ledger", str(tmp_path / "l.jsonl")]
    )
    assert result.exit_code == 0, result.output

    rec = load_all(tmp_path / "l.jsonl")[0]
    assert rec.manifest.run_config.n_repeats == 3        # from the suite
    assert rec.manifest.run_config.concurrency == 1
    assert rec.aggregate.mean_score == 1.0               # answer_match graded both right
    assert (tmp_path / "cass.json").exists()             # path resolved next to the suite


def test_explicit_flags_override_the_suite(tmp_path, monkeypatch):
    _provider(monkeypatch)
    suite = _suite(tmp_path)
    result = runner.invoke(app, ["run", "--suite", str(suite), "--n", "5",
                                 "--ledger", str(tmp_path / "l.jsonl")])
    assert result.exit_code == 0, result.output
    assert load_all(tmp_path / "l.jsonl")[0].manifest.run_config.n_repeats == 5


def test_unknown_suite_key_is_rejected(tmp_path):
    suite = _suite(tmp_path, tempreture=0.2)             # a typo must not pass silently
    result = runner.invoke(app, ["run", "--suite", str(suite)])
    assert result.exit_code == 2
    assert "unknown key(s)" in plain(result.output)
    assert "tempreture" in plain(result.output)


def test_missing_inputs_are_named(tmp_path):
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    message = plain(result.output)
    assert "--dataset" in message and "--suite" in message


def test_suite_can_request_signing(tmp_path, monkeypatch):
    _provider(monkeypatch)
    from evalseal.signing import generate_keypair, verify_signatures
    pub = generate_keypair(tmp_path / "k.key", tmp_path / "k.pub")
    suite = _suite(tmp_path, sign_key="k.key")
    ledger = tmp_path / "l.jsonl"

    result = runner.invoke(app, ["run", "--suite", str(suite), "--ledger", str(ledger)])
    assert result.exit_code == 0, result.output
    assert "Signed record 0" in result.output
    ok, msg = verify_signatures(ledger, pub)
    assert ok, msg
