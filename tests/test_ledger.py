from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_dataset, scripted

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.ledger import _content_hash, last_hash, load_all, seal_and_append, verify_chain
from evalseal.executor import run_eval


def _record(prev_hash: str):
    target = LocalCallableTarget(scripted({"q": ["yes", "no", "yes", "yes", "yes"]}))
    return run_eval(make_dataset("q"), target, RegexScorer("yes"), n_repeats=5, prev_hash=prev_hash)


def _seal_two(path: Path):
    seal_and_append(_record(last_hash(path)), path)
    seal_and_append(_record(last_hash(path)), path)


def _rewrite_line(path: Path, index: int, mutate):
    lines = path.read_text().splitlines()
    d = json.loads(lines[index])
    mutate(d)
    lines[index] = json.dumps(d)
    path.write_text("\n".join(lines) + "\n")


def test_two_sealed_records_verify(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seal_two(path)
    recs = load_all(path)
    assert len(recs) == 2
    assert recs[0].prev_hash == "GENESIS"
    assert recs[1].prev_hash == recs[0].hash
    assert verify_chain(path) == (True, "Chain intact: 2 record(s).")


def test_hash_survives_json_roundtrip(tmp_path):
    path = tmp_path / "ledger.jsonl"
    sealed = seal_and_append(_record("GENESIS"), path)
    assert _content_hash(load_all(path)[0]) == sealed.hash


def test_tampered_score_is_detected(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seal_two(path)

    def flip_a_fail_to_pass(d):
        d["results"][0]["scores"][1] = 1.0
        d["aggregate"]["mean_score"] = 1.0

    _rewrite_line(path, 0, flip_a_fail_to_pass)
    ok, msg = verify_chain(path)
    assert not ok
    assert "TAMPER DETECTED at record 0" in msg


def test_rehashed_tamper_breaks_the_next_link(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seal_two(path)

    # A smarter attacker edits record 0 AND recomputes its hash.
    recs = load_all(path)
    forged = recs[0].model_copy(deep=True)
    forged.results[0].scores[1] = 1.0
    forged.hash = _content_hash(forged)
    _rewrite_line(path, 0, lambda d: d.update(json.loads(forged.model_dump_json())))

    ok, msg = verify_chain(path)
    assert not ok
    assert "Broken chain at record 1" in msg


def test_deleted_record_breaks_chain(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seal_two(path)
    path.write_text(path.read_text().splitlines()[1] + "\n")
    ok, msg = verify_chain(path)
    assert not ok
    assert "Broken chain at record 0" in msg


def test_append_refuses_unlinked_record(tmp_path):
    path = tmp_path / "ledger.jsonl"
    seal_and_append(_record("GENESIS"), path)
    with pytest.raises(ValueError, match="broken chain"):
        seal_and_append(_record("GENESIS"), path)
