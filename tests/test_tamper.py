"""What a tamper-evident ledger must actually catch, and what it must not cry wolf over.

The legacy fixture was produced by running evalseal 1.2.0 (schema 1.1) for real, not
hand-written, so the compatibility path is tested against a ledger today's code could
not have created.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import make_dataset, scripted

from evalseal.adapters.scorer import LLMJudgeScorer, RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.executor import run_eval
from evalseal.ledger import _content_hash, load_all, seal_and_append, verify_chain

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_schema_1_1.jsonl"


def _two_records(path: Path):
    target = LocalCallableTarget(scripted({"q": ["yes", "no", "yes", "yes", "yes"]}))
    for _ in range(2):
        rec = run_eval(make_dataset("q"), target, RegexScorer("yes"), n_repeats=5)
        seal_and_append(rec, path, relink=True)


def _rewrite(path: Path, index: int, mutate) -> None:
    lines = path.read_text().splitlines()
    record = json.loads(lines[index])
    mutate(record)
    lines[index] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")


def test_tampered_score_is_detected(tmp_path):
    ledger = tmp_path / "l.jsonl"
    _two_records(ledger)
    _rewrite(ledger, 0, lambda r: r["results"][0].update(scores=[1.0] * 5))

    ok, message = verify_chain(ledger)
    assert not ok and "TAMPER DETECTED at record 0" in message


def test_tampered_judge_prompt_hash_is_detected(tmp_path):
    """Swapping the sealed evaluator config must break the seal, not pass quietly."""
    ledger = tmp_path / "l.jsonl"
    target = LocalCallableTarget(lambda p: "an answer")
    judge = LocalCallableTarget(lambda p: "PASS", name="judge")
    seal_and_append(
        run_eval(make_dataset("q"), target, LLMJudgeScorer(judge=judge, rubric="Be strict."),
                 n_repeats=5),
        ledger, relink=True,
    )
    _rewrite(ledger, 0, lambda r: r["manifest"]["scorer"].update(
        judge_prompt_hash="sha256:" + "0" * 64))

    ok, message = verify_chain(ledger)
    assert not ok and "TAMPER DETECTED" in message


def test_broken_prev_hash_is_detected(tmp_path):
    ledger = tmp_path / "l.jsonl"
    _two_records(ledger)
    _rewrite(ledger, 1, lambda r: r.update(prev_hash="sha256:" + "f" * 64))

    ok, message = verify_chain(ledger)
    assert not ok and "Broken chain at record 1" in message


def test_duplicated_prev_hash_is_reported_as_siblings(tmp_path):
    ledger = tmp_path / "l.jsonl"
    _two_records(ledger)
    records = load_all(ledger)
    forged = json.loads(records[1].model_dump_json())
    forged["created_at"] = "2020-01-01T00:00:00+00:00"
    with ledger.open("a") as f:
        f.write(json.dumps(forged) + "\n")

    ok, message = verify_chain(ledger)
    assert not ok and "Sibling records detected" in message


def test_deleted_record_is_detected(tmp_path):
    ledger = tmp_path / "l.jsonl"
    _two_records(ledger)
    ledger.write_text(ledger.read_text().splitlines()[1] + "\n")

    ok, message = verify_chain(ledger)
    assert not ok and "Broken chain at record 0" in message


def test_rehashed_tamper_breaks_the_next_link(tmp_path):
    """A forger who edits a record AND re-hashes it still breaks its successor."""
    ledger = tmp_path / "l.jsonl"
    _two_records(ledger)
    records = load_all(ledger)
    forged = records[0].model_copy(deep=True)
    forged.results[0].scores = [1.0] * 5
    forged.hash = _content_hash(forged)
    _rewrite(ledger, 0, lambda r: r.update(json.loads(forged.model_dump_json())))

    ok, message = verify_chain(ledger)
    assert not ok and "Broken chain at record 1" in message


@pytest.mark.skipif(not FIXTURE.exists(), reason="legacy fixture not checked out")
def test_ledger_sealed_by_1_2_0_still_verifies(tmp_path):
    """Schema 1.1 records lack the fields 1.3 seals; they must still verify."""
    ledger = tmp_path / "legacy.jsonl"
    shutil.copy(FIXTURE, ledger)

    record = load_all(ledger)[0]
    assert record.manifest.schema_version == "1.1"
    assert record.manifest.suite is None            # a field 1.1 did not have

    ok, message = verify_chain(ledger)
    assert ok, message
    assert "older schema version(s) verified under their own rules (1.1)" in message


@pytest.mark.skipif(not FIXTURE.exists(), reason="legacy fixture not checked out")
def test_tampering_with_a_legacy_record_is_still_caught(tmp_path):
    """Backward compatibility must not become a way to smuggle edits past verify."""
    ledger = tmp_path / "legacy.jsonl"
    shutil.copy(FIXTURE, ledger)
    before = load_all(ledger)[0].results[0].scores

    # Flip the verdicts to their opposite, so the edit is real whatever the fixture holds.
    _rewrite(ledger, 0, lambda r: r["results"][0].update(
        scores=[0.0 if s else 1.0 for s in r["results"][0]["scores"]]))
    assert load_all(ledger)[0].results[0].scores != before, "the test must actually edit it"

    ok, message = verify_chain(ledger)
    assert not ok and "TAMPER DETECTED" in message
