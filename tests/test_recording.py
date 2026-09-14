from __future__ import annotations

import pytest

from evalseal.adapters.recording import Cassette


def test_repeated_identical_requests_replay_in_order(tmp_path):
    path = tmp_path / "c.json"
    live = iter([{"v": "first"}, {"v": "second"}, {"v": "third"}])

    rec = Cassette(path, record=True)
    got = [rec.call({"q": 1}, lambda: next(live)) for _ in range(3)]
    assert [g["v"] for g in got] == ["first", "second", "third"]

    def no_network():
        raise AssertionError("replay must not hit the network")

    replay = Cassette(path)  # EVALSEAL_RECORD unset -> replay mode
    assert [replay.call({"q": 1}, no_network)["v"] for _ in range(3)] == [
        "first", "second", "third",
    ]


def test_replay_fails_loudly_when_entry_missing(tmp_path):
    path = tmp_path / "c.json"
    Cassette(path, record=True).call({"q": 1}, lambda: {"v": "only"})

    replay = Cassette(path)
    replay.call({"q": 1}, lambda: {})
    with pytest.raises(RuntimeError, match="No cassette entry #1"):
        replay.call({"q": 1}, lambda: {})


def test_record_mode_reuses_existing_then_extends(tmp_path):
    path = tmp_path / "c.json"
    Cassette(path, record=True).call({"q": 1}, lambda: {"v": "old"})

    calls = []
    rec = Cassette(path, record=True)
    assert rec.call({"q": 1}, lambda: calls.append(1) or {"v": "new"})["v"] == "old"
    assert rec.call({"q": 1}, lambda: calls.append(1) or {"v": "new"})["v"] == "new"
    assert calls == [1]
