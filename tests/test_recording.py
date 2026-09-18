from __future__ import annotations

import json

import pytest

from evalseal.adapters.recording import Cassette, CassetteFormatError, slot


def _no_network():
    raise AssertionError("replay must not hit the network")


def test_each_repeat_records_and_replays_its_own_response(tmp_path):
    path = tmp_path / "c.json"
    rec = Cassette(path, record=True)
    for k in range(3):
        with slot(k):
            rec.call({"q": 1}, lambda k=k: {"v": f"resp{k}"})

    replay = Cassette(path)
    for k in (2, 0, 1):                      # order on replay is irrelevant
        with slot(k):
            assert replay.call({"q": 1}, _no_network)["v"] == f"resp{k}"


def test_repeats_are_distinct_entries_not_one_reused_response(tmp_path):
    """The bug this format exists to prevent: replaying repeat #1 for every repeat."""
    path = tmp_path / "c.json"
    rec = Cassette(path, record=True)
    for k, verdict in enumerate(["PASS", "FAIL", "PASS"]):
        with slot(k):
            rec.call({"q": 1}, lambda v=verdict: {"v": v})

    replay = Cassette(path)
    got = []
    for k in range(3):
        with slot(k):
            got.append(replay.call({"q": 1}, _no_network)["v"])
    assert got == ["PASS", "FAIL", "PASS"]   # the flip survives the round trip


def test_replay_fails_loudly_when_entry_missing(tmp_path):
    path = tmp_path / "c.json"
    with slot(0):
        Cassette(path, record=True).call({"q": 1}, lambda: {"v": "only"})

    replay = Cassette(path)
    with slot(1), pytest.raises(RuntimeError, match="No cassette entry for repeat 1"):
        replay.call({"q": 1}, _no_network)


def test_without_a_slot_calls_fall_back_to_arrival_order(tmp_path):
    path = tmp_path / "c.json"
    rec = Cassette(path, record=True)
    assert rec.call({"q": 1}, lambda: {"v": "first"})["v"] == "first"
    assert rec.call({"q": 1}, lambda: {"v": "second"})["v"] == "second"

    replay = Cassette(path)
    assert [replay.call({"q": 1}, _no_network)["v"] for _ in range(2)] == ["first", "second"]


def test_record_mode_reuses_existing_then_extends(tmp_path):
    path = tmp_path / "c.json"
    with slot(0):
        Cassette(path, record=True).call({"q": 1}, lambda: {"v": "old"})

    calls = []
    rec = Cassette(path, record=True)
    with slot(0):
        assert rec.call({"q": 1}, lambda: calls.append(1) or {"v": "new"})["v"] == "old"
    with slot(1):
        assert rec.call({"q": 1}, lambda: calls.append(1) or {"v": "new"})["v"] == "new"
    assert calls == [1]


def test_incompatible_cassette_format_is_rejected(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"somekey": [{"v": 1}]}))    # pre-0.2 layout
    with pytest.raises(CassetteFormatError, match="incompatible evalseal version"):
        Cassette(path)
