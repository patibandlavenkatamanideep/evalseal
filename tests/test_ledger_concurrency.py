"""Concurrent writers must produce one linear chain, not siblings.

Deterministic and offline: the work each writer does is a local scripted target, so the
only thing under test is the ledger's locking.
"""
from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from conftest import make_dataset, scripted

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.executor import run_eval
from evalseal.ledger import GENESIS, last_hash, load_all, seal_and_append, verify_chain
from evalseal.locking import lock_path_for


def _append_one(args: tuple[str, int]) -> str:
    """Build and append one record. Runs in a separate process."""
    ledger_str, marker = args
    ledger = Path(ledger_str)
    target = LocalCallableTarget(scripted({f"q{marker}": ["yes"] * 5}))
    record = run_eval(
        make_dataset(f"q{marker}"), target, RegexScorer("yes"),
        n_repeats=5, prev_hash=last_hash(ledger),
    )
    return seal_and_append(record, ledger, relink=True).hash


def test_concurrent_writers_produce_one_linear_chain(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    writers = 8

    with ProcessPoolExecutor(max_workers=writers) as pool:
        hashes = list(pool.map(_append_one, [(str(ledger), i) for i in range(writers)]))

    records = load_all(ledger)
    assert len(records) == writers, "every writer's record must be present"
    assert len(set(hashes)) == writers, "each record must be distinct"

    # Exactly one genesis, and no two records claiming the same predecessor.
    parents = [r.prev_hash for r in records]
    assert parents.count(GENESIS) == 1
    assert len(set(parents)) == len(parents), "sibling records: a prev_hash was reused"

    # Every record except genesis has exactly one predecessor, in file order.
    for i, record in enumerate(records):
        expected = GENESIS if i == 0 else records[i - 1].hash
        assert record.prev_hash == expected

    ok, message = verify_chain(ledger)
    assert ok, message
    assert f"{writers} record(s)" in message


def test_lock_file_lives_beside_the_ledger(tmp_path):
    ledger = tmp_path / "nested" / "ledger.jsonl"
    target = LocalCallableTarget(scripted({"q": ["yes"] * 5}))
    record = run_eval(make_dataset("q"), target, RegexScorer("yes"), n_repeats=5)
    seal_and_append(record, ledger, relink=True)

    assert lock_path_for(ledger) == ledger.with_name("ledger.jsonl.lock")
    assert lock_path_for(ledger).exists()


def test_relink_false_still_refuses_a_stale_predecessor(tmp_path):
    """Default behaviour is unchanged: an explicit prev_hash must match the head."""
    ledger = tmp_path / "ledger.jsonl"
    target = LocalCallableTarget(scripted({"q": ["yes"] * 5}))
    seal_and_append(run_eval(make_dataset("q"), target, RegexScorer("yes"), n_repeats=5), ledger)

    stale = run_eval(make_dataset("q"), target, RegexScorer("yes"), n_repeats=5,
                     prev_hash=GENESIS)
    with pytest.raises(ValueError, match="broken chain"):
        seal_and_append(stale, ledger)


def test_verify_detects_hand_made_siblings(tmp_path):
    """Two records claiming the same parent is reported as siblings, not a bad link."""
    ledger = tmp_path / "ledger.jsonl"
    target = LocalCallableTarget(scripted({"q": ["yes"] * 5}))
    first = seal_and_append(
        run_eval(make_dataset("q"), target, RegexScorer("yes"), n_repeats=5), ledger
    )
    second = seal_and_append(
        run_eval(make_dataset("q"), target, RegexScorer("yes"), n_repeats=5,
                 prev_hash=first.hash), ledger
    )
    # Forge a third record claiming the same parent as the second.
    forged = json.loads(second.model_dump_json())
    forged["created_at"] = "2020-01-01T00:00:00+00:00"
    with ledger.open("a") as f:
        f.write(json.dumps(forged) + "\n")

    ok, message = verify_chain(ledger)
    assert not ok
    assert "Sibling records detected" in message
