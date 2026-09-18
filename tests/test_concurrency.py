"""Concurrency must not change results: same scores, same cassette entries, any order."""
from __future__ import annotations

import json
import threading

from conftest import make_dataset

from evalseal.adapters.recording import Cassette, slot
from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.executor import run_eval


class _SlotTarget:
    """Replies with its (prompt, repeat-slot) so a misrouted response is visible."""

    def __init__(self, cassette: Cassette):
        self.cassette = cassette

    def generate(self, prompt: str):
        from evalseal.adapters.target import TargetResponse
        raw = self.cassette.call(
            {"prompt": prompt}, lambda: {"text": f"{prompt}-{threading.get_ident()}"}
        )
        return TargetResponse(raw["text"], "m", "m", None, "local://", {}, "explicit")


def test_cassette_slot_pins_the_entry_regardless_of_call_order(tmp_path):
    path = tmp_path / "c.json"
    rec = Cassette(path, record=True)
    for k in (2, 0, 1):                     # recorded out of order
        with slot(k):
            rec.call({"q": 1}, lambda k=k: {"v": k})

    replay = Cassette(path)
    for k in (1, 2, 0):                     # replayed in yet another order
        with slot(k):
            assert replay.call({"q": 1}, lambda: pytest_fail())["v"] == k


def pytest_fail():
    raise AssertionError("replay must not call the network")


def test_concurrent_run_matches_serial_run(tmp_path):
    ds = make_dataset(*[f"q{i}" for i in range(8)])
    scripted = {f"q{i}": ["yes", "no", "yes", "no", "yes"] for i in range(8)}

    def build(path, record):
        cass = Cassette(path, record=record)

        class T:
            def generate(self, prompt):
                from evalseal.adapters.target import TargetResponse
                raw = cass.call(
                    {"prompt": prompt},
                    lambda: {"text": scripted[prompt][_slot_of(cass)]},
                )
                return TargetResponse(raw["text"], "m", "m", None, "local://", {}, "explicit")

        return T()

    def _slot_of(_cass):
        from evalseal.adapters import recording
        return recording._slot.get() or 0

    path = tmp_path / "c.json"
    serial = run_eval(ds, build(path, True), RegexScorer("^yes$"), n_repeats=5, concurrency=1)
    parallel = run_eval(ds, build(path, False), RegexScorer("^yes$"), n_repeats=5, concurrency=8)

    def key(r):
        return [(c.case_id, c.scores, c.stability, c.ci95) for c in r.results]
    assert key(serial) == key(parallel)
    assert parallel.manifest.run_config.concurrency == 8
    assert serial.aggregate.warnings == parallel.aggregate.warnings


def test_progress_callback_fires_once_per_call():
    seen = []
    lock = threading.Lock()

    def tick():
        with lock:
            seen.append(1)

    ds = make_dataset("a", "b", "c")
    target = LocalCallableTarget(lambda p: "yes")
    run_eval(ds, target, RegexScorer("yes"), n_repeats=4, concurrency=4, on_unit_done=tick)
    assert len(seen) == 12


def test_cassette_is_thread_safe_when_recording(tmp_path):
    path = tmp_path / "c.json"
    cass = Cassette(path, record=True)
    errors = []

    def work(k):
        try:
            with slot(k):
                cass.call({"q": "same"}, lambda: {"v": k})
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    entries = json.loads(path.read_text())["entries"]
    assert len(entries) == 16                      # one per repeat, none lost to a race
    assert sorted(v["v"] for v in entries.values()) == list(range(16))
