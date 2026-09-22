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


def test_an_interrupted_save_leaves_the_previous_cassette_intact(tmp_path, monkeypatch):
    """A crash mid-write must not cost the responses already recorded.

    The cassette is rewritten in full after every response. It used to truncate the
    file and then fill it, so an interrupt in between left partial JSON and lost every
    entry. The write now goes to a temp file that is renamed over the original, so the
    file on disk is always a complete version.
    """
    import json
    import os

    import pytest

    from evalseal.adapters.recording import Cassette, slot

    path = tmp_path / "c.json"
    cas = Cassette(path, record=True)
    with slot(0):
        cas.call({"q": "first"}, lambda: {"answer": 1})
    before = path.read_text(encoding="utf-8")

    real_replace = os.replace

    def interrupted(src, dst):
        raise KeyboardInterrupt("simulated Ctrl+C between write and rename")

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt), slot(0):
        cas.call({"q": "second"}, lambda: {"answer": 2})
    monkeypatch.setattr(os, "replace", real_replace)

    # The file still holds the complete earlier version, and it parses.
    assert path.read_text(encoding="utf-8") == before
    assert len(json.loads(before)["entries"]) == 1
    # And no temp file is left lying around to confuse the next run.
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


def test_a_completed_save_replaces_the_file_with_the_new_version(tmp_path):
    import json

    from evalseal.adapters.recording import Cassette, slot

    path = tmp_path / "c.json"
    cas = Cassette(path, record=True)
    for k in range(3):
        with slot(k):
            cas.call({"q": "same"}, lambda k=k: {"answer": k})
    assert len(json.loads(path.read_text(encoding="utf-8"))["entries"]) == 3
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]
