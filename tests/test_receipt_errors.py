"""Passing the wrong file must produce a sentence, not a stack trace.

Every command that reads a receipt goes through `load_receipt`, and the files a user
is most likely to pass by mistake all live in the same directory with similar names:
`report.json` (the receipt), the output of `report --json` (a summary of one),
`anchor.json`, `receipt.html`, the ledger. Telling them apart is the whole job here.

Each test asserts on the *content* of the message, not merely that one exists: an
error that says "invalid" without saying what was expected is the failure this file
exists to prevent.
"""
from __future__ import annotations

import json

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import EXIT_BAD_INPUT, app
from evalseal.diffing import ReceiptError, load_receipt
from evalseal.executor import run_eval
from evalseal.ledger import seal_and_append
from evalseal.report import write_json

runner = CliRunner()


@pytest.fixture
def sealed(tmp_path):
    target = LocalCallableTarget(scripted({"a": ["yes"], "b": ["yes"]}))
    record = run_eval(make_dataset("a", "b"), target, RegexScorer(r"^yes$"), n_repeats=5)
    ledger = tmp_path / "ledger.jsonl"
    record = seal_and_append(record, ledger, relink=True)
    receipt = tmp_path / "report.json"
    write_json(record, receipt)
    return record, receipt, ledger


# --- the four shapes a user actually passes by mistake ------------------------------

def test_report_json_output_is_named_as_such(tmp_path, sealed):
    """The mistake that produced this feature: `report --json` output into `anchor`."""
    _, receipt, ledger = sealed
    summary = tmp_path / "receipt-summary.json"
    result = runner.invoke(app, ["report", str(receipt), "--json"])
    assert result.exit_code == 0
    summary.write_text(result.stdout, encoding="utf-8")

    with pytest.raises(ReceiptError) as e:
        load_receipt(summary)
    message = str(e.value)
    assert "report --json" in message          # names the command that wrote it
    assert "summarises a receipt rather than being one" in message
    assert "evalseal run" in message           # and what to pass instead


def test_invalid_json_says_where_it_broke(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text('{"manifest": ', encoding="utf-8")
    with pytest.raises(ReceiptError) as e:
        load_receipt(bad)
    message = str(e.value)
    assert "not valid JSON" in message
    assert "line 1" in message                 # the position, not a pydantic dump
    assert "sealed receipt" in message


def test_valid_json_that_is_not_a_receipt_lists_what_is_missing(tmp_path):
    other = tmp_path / "target.json"
    other.write_text(json.dumps({"model": "gpt-4o-mini", "temperature": 0}), "utf-8")
    with pytest.raises(ReceiptError) as e:
        load_receipt(other)
    message = str(e.value)
    assert "not a sealed receipt" in message
    assert "aggregate" in message and "manifest" in message and "results" in message


def test_an_anchor_file_is_told_apart_from_the_receipt_it_anchors(tmp_path, sealed):
    _, receipt, ledger = sealed
    anchor_file = tmp_path / "anchor.json"
    assert runner.invoke(app, [
        "anchor", str(receipt), "--ledger", str(ledger), "--out", str(anchor_file),
    ]).exit_code == 0

    with pytest.raises(ReceiptError) as e:
        load_receipt(anchor_file)
    assert "is an anchor, not the receipt it anchors" in str(e.value)


def test_a_receipt_with_broken_fields_names_the_fields(tmp_path, sealed):
    _, receipt, _ = sealed
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["aggregate"]["mean_score"] = "not a number"
    del data["results"][0]["case_id"]
    broken = tmp_path / "broken-receipt.json"
    broken.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ReceiptError) as e:
        load_receipt(broken)
    message = str(e.value)
    assert "looks like a receipt" in message
    assert "aggregate.mean_score" in message
    assert "results.0.case_id" in message


def test_a_json_list_is_named_as_a_list(tmp_path):
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps([{"id": "a", "prompt": "hi"}]), encoding="utf-8")
    with pytest.raises(ReceiptError) as e:
        load_receipt(dataset)
    assert "JSON list" in str(e.value)


def test_html_receipt_is_not_valid_json(tmp_path):
    page = tmp_path / "receipt.html"
    page.write_text("<!doctype html>\n<html><body>score</body></html>", encoding="utf-8")
    with pytest.raises(ReceiptError) as e:
        load_receipt(page)
    assert "not valid JSON" in str(e.value)


def test_an_empty_file_says_so(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ReceiptError) as e:
        load_receipt(empty)
    assert "is empty" in str(e.value)


def test_a_missing_file_is_not_a_traceback(tmp_path):
    with pytest.raises(ReceiptError) as e:
        load_receipt(tmp_path / "nope.json")
    assert "could not be read" in str(e.value)


# --- the good paths still work ------------------------------------------------------

def test_a_real_receipt_and_a_real_ledger_both_still_load(sealed):
    record, receipt, ledger = sealed
    assert load_receipt(receipt).hash == record.hash
    assert load_receipt(ledger).hash == record.hash


def test_a_multi_record_ledger_still_yields_its_head(tmp_path, sealed):
    record, _, ledger = sealed
    target = LocalCallableTarget(scripted({"a": ["no"], "b": ["yes"]}))
    second = run_eval(make_dataset("a", "b"), target, RegexScorer(r"^yes$"), n_repeats=5)
    second = seal_and_append(second, ledger, relink=True)
    assert load_receipt(ledger).hash == second.hash != record.hash


def test_a_truncated_ledger_line_reports_the_line_not_the_whole_file(tmp_path, sealed):
    """A half-written ledger must blame the JSON, not claim the file "is not a receipt"."""
    _, _, ledger = sealed
    with open(ledger, "a", encoding="utf-8") as f:
        f.write('{"manifest": {"target"\n')
    with pytest.raises(ReceiptError) as e:
        load_receipt(ledger)
    assert "not valid JSON" in str(e.value)


# --- the CLI surfaces, which is where a user meets this ------------------------------

@pytest.mark.parametrize("command", ["anchor", "anchor-verify"])
def test_anchor_commands_exit_cleanly_on_report_json_output(tmp_path, sealed, command):
    _, receipt, ledger = sealed
    summary = tmp_path / "summary.json"
    summary.write_text(
        runner.invoke(app, ["report", str(receipt), "--json"]).stdout, encoding="utf-8")

    args = ([command, str(summary), "--ledger", str(ledger)] if command == "anchor"
            else [command, str(summary), str(summary), "--ledger", str(ledger)])
    result = runner.invoke(app, args)

    assert result.exit_code == EXIT_BAD_INPUT
    assert "Traceback" not in result.output
    assert "ValidationError" not in result.output
    assert "Not a receipt" in result.output or "Not an anchor file" in result.output


def test_anchor_exits_cleanly_on_invalid_json(tmp_path):
    bad = tmp_path / "invalid.json"
    bad.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["anchor", str(bad)])
    assert result.exit_code == EXIT_BAD_INPUT
    assert "Traceback" not in result.output
    unwrapped = " ".join(result.output.split())
    assert "Not a receipt" in unwrapped
    assert "not valid JSON" in unwrapped


def test_diff_exits_cleanly_on_a_non_receipt(tmp_path, sealed):
    _, receipt, ledger = sealed
    junk = tmp_path / "junk.json"
    junk.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    result = runner.invoke(app, ["diff", str(junk), str(receipt), "--ledger", str(ledger)])
    assert result.exit_code == EXIT_BAD_INPUT
    assert "Traceback" not in result.output
    # The console wraps to the terminal width, so compare against unwrapped text.
    assert "not a sealed receipt" in " ".join(result.output.split())
