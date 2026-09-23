"""Receipts bind the files they depended on by digest, and say so honestly.

The cassette is the one that matters: it holds the responses the verdicts came from.
Until schema 1.4 nothing bound it to the receipt, so the responses could be swapped
without the receipt noticing.
"""
from __future__ import annotations

import json
import shutil

import pytest
from typer.testing import CliRunner

from evalseal.anchor import check_artifacts
from evalseal.cli import app
from evalseal.ledger import load_all

runner = CliRunner()


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """A real `evalseal run` against a stubbed provider, so a cassette exists."""
    import httpx

    answers = iter(["yes", "no"] * 20)
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, request=httpx.Request("POST", url),
        json={"model": "m", "choices": [{"message": {"content": next(answers)}}]}))

    (tmp_path / "ds.jsonl").write_text('{"case_id": "only", "prompt": "p"}\n')
    (tmp_path / "t.json").write_text('{"model": "m"}')
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}')
    ledger = tmp_path / "l.jsonl"
    cassette = tmp_path / "c.json"
    result = runner.invoke(app, [
        "run", "--dataset", str(tmp_path / "ds.jsonl"),
        "--target-config", str(tmp_path / "t.json"),
        "--scorer-config", str(tmp_path / "s.json"),
        "--cassette", str(cassette), "--n", "5", "--concurrency", "1",
        "--ledger", str(ledger), "--fail-on", "none",
    ])
    assert result.exit_code == 0, result.output
    return tmp_path, ledger, cassette


def _roles(record):
    return {a.role: a for a in record.manifest.artifacts}


def test_the_cassette_dataset_and_scorer_inputs_are_sealed(recorded):
    _, ledger, cassette = recorded
    record = load_all(ledger)[-1]
    roles = _roles(record)

    assert set(roles) >= {"cassette", "dataset"}
    assert roles["cassette"].kind == "hashed"
    assert roles["cassette"].sha256 is not None
    # The digest is of the cassette as it stood when the record was sealed, which is
    # after recording finished.
    import hashlib
    expected = "sha256:" + hashlib.sha256(cassette.read_bytes()).hexdigest()
    assert roles["cassette"].sha256 == expected


def test_an_unchanged_cassette_verifies(recorded):
    _, ledger, _ = recorded
    results = {c.role: c for c in check_artifacts(load_all(ledger)[-1])}
    assert results["cassette"].status == "ok"
    assert results["dataset"].status == "ok"


def test_editing_the_cassette_is_detected(recorded):
    """The acceptance criterion: swapping a response must change the outcome."""
    _, ledger, cassette = recorded
    data = json.loads(cassette.read_text(encoding="utf-8"))
    entry = next(iter(data["entries"]))
    data["entries"][entry]["choices"][0]["message"]["content"] = "tampered"
    cassette.write_text(json.dumps(data), encoding="utf-8")

    results = {c.role: c for c in check_artifacts(load_all(ledger)[-1])}
    assert results["cassette"].status == "changed"
    assert "does not match the digest" in results["cassette"].detail


def test_a_missing_artifact_is_missing_never_passed(recorded):
    _, ledger, cassette = recorded
    cassette.unlink()
    results = {c.role: c for c in check_artifacts(load_all(ledger)[-1])}
    assert results["cassette"].status == "missing"
    assert results["cassette"].status != "ok"


def test_verify_artifacts_exits_nonzero_on_a_changed_cassette(recorded):
    tmp_path, ledger, cassette = recorded
    ok = runner.invoke(app, ["verify", "--ledger", str(ledger), "--artifacts"])
    assert ok.exit_code == 0, ok.output
    assert "ok" in ok.output and "cassette" in ok.output

    shutil.copy(cassette, cassette.with_suffix(".bak"))
    cassette.write_text('{"version": 2, "entries": {}}', encoding="utf-8")
    bad = runner.invoke(app, ["verify", "--ledger", str(ledger), "--artifacts"])
    assert bad.exit_code == 1
    assert "changed" in bad.output


def test_verify_artifacts_says_so_when_a_record_binds_none(recorded):
    """Records sealed before 1.4 have no artifacts; that is reported, not implied."""
    _, ledger, _ = recorded
    record = load_all(ledger)[-1]
    record.manifest.artifacts = []
    assert check_artifacts(record) == []


def test_anchor_verify_catches_a_cassette_edited_after_sealing(recorded):
    """Phase 4's acceptance criterion, through the anchor rather than verify."""
    from evalseal.anchor import anchor_passed, build_anchor, verify_anchor
    from evalseal.diffing import load_receipt

    workspace, ledger, cassette = recorded
    receipt = workspace / "report.json"
    record = load_receipt(receipt)
    anchor = build_anchor(record, receipt, ledger)
    assert any(a["role"] == "cassette" for a in anchor["sealed_artifacts"])

    before = {c.name: c for c in verify_anchor(anchor, record, ledger=ledger,
                                               base_dir=workspace)}
    assert before["sealed:cassette"].passed
    assert anchor_passed(verify_anchor(anchor, record, ledger=ledger, base_dir=workspace))

    data = json.loads(cassette.read_text(encoding="utf-8"))
    entry = next(iter(data["entries"]))
    data["entries"][entry]["choices"][0]["message"]["content"] = "swapped"
    cassette.write_text(json.dumps(data), encoding="utf-8")

    after = {c.name: c for c in verify_anchor(anchor, record, ledger=ledger,
                                              base_dir=workspace)}
    assert not after["sealed:cassette"].passed
    assert "changed since the receipt was sealed" in after["sealed:cassette"].detail
    assert not anchor_passed(verify_anchor(anchor, record, ledger=ledger,
                                           base_dir=workspace))
