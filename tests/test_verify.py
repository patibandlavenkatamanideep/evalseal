from __future__ import annotations

import json
from pathlib import Path

from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.executor import run_eval
from evalseal.ledger import last_hash, seal_and_append

runner = CliRunner()


def _seal(path: Path, outputs: list[str]):
    target = LocalCallableTarget(scripted({"q1": outputs, "q2": outputs}))
    rec = run_eval(make_dataset("q1", "q2"), target, RegexScorer("yes"),
                   n_repeats=len(outputs), prev_hash=last_hash(path))
    seal_and_append(rec, path)


def test_verify_passes_on_intact_ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seal(path, ["yes"] * 5)
    _seal(path, ["yes"] * 5)
    result = runner.invoke(app, ["verify", "--ledger", str(path)])
    assert result.exit_code == 0, result.output
    assert "Chain intact: 2 record(s)" in result.output


def test_verify_fails_on_tampered_score(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seal(path, ["yes", "no", "yes", "no", "yes"])
    d = json.loads(path.read_text())
    d["results"][0]["scores"] = [1.0] * 5
    path.write_text(json.dumps(d) + "\n")

    result = runner.invoke(app, ["verify", "--ledger", str(path)])
    assert result.exit_code == 1
    assert "TAMPER DETECTED" in result.output


def test_verify_on_empty_ledger(tmp_path):
    result = runner.invoke(app, ["verify", "--ledger", str(tmp_path / "none.jsonl")])
    assert result.exit_code == 0
    assert "0 record(s)" in result.output


def test_a_single_item_going_from_all_fail_to_all_pass_is_still_inconclusive(tmp_path):
    """The largest move one item can make, and it is not evidence.

    Exact McNemar on one discordant item gives p = 2 * 0.5 = 1.0. The old rule called
    this "REAL CHANGE" because 1.0 exceeded the per-case half-width, which is the same
    arithmetic mistake in the opposite direction: over-claiming on one item having
    under-claimed on forty.
    """
    path = tmp_path / "ledger.jsonl"
    _seal(path, ["no"] * 5)
    _seal(path, ["yes"] * 5)
    result = runner.invoke(app, ["diff", "0", "1", "--ledger", str(path)])
    assert result.exit_code == 0, result.output
    assert "inconclusive" in result.output
    assert "REAL CHANGE" not in result.output


def test_inconclusive_output_says_it_is_not_a_claim_of_sameness(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seal(path, ["yes", "no", "yes", "no", "yes"])  # mean 0.6
    _seal(path, ["no", "yes", "no", "yes", "no"])   # mean 0.4
    result = runner.invoke(app, ["diff", "--ledger", str(path), "0", "-1"])
    assert result.exit_code == 0, result.output
    assert "inconclusive" in result.output
    assert "not the same as no difference" in result.output


def test_diff_rejects_out_of_range_index(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seal(path, ["yes"] * 5)
    result = runner.invoke(app, ["diff", "0", "5", "--ledger", str(path)])
    assert result.exit_code == 2
