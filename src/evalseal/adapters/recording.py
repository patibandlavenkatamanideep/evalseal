from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

FORMAT_VERSION = 2


def request_key(payload: dict) -> str:
    """Stable hash of the *effective* request, so identical requests hit the cassette."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


_slot: contextvars.ContextVar[int | None] = contextvars.ContextVar("evalseal_slot", default=None)


@contextmanager
def slot(index: int) -> Iterator[None]:
    """Pin every cassette call inside this block to repeat `index`.

    The executor opens one slot per (case, repeat) unit. The repeat is part of the
    entry key, so a replay returns the same response for the same repeat no matter
    how many workers run or what order they finish in.
    """
    token = _slot.set(index)
    try:
        yield
    finally:
        _slot.reset(token)


class CassetteFormatError(RuntimeError):
    """The cassette on disk was written by an incompatible version."""


class Cassette:
    """JSON-backed record/replay store keyed by (request, repeat).

    Repeating an identical request is the whole point of an N-run eval, so the repeat
    index is part of the key: repeat 3 of a case always replays the response recorded
    for repeat 3. (Keying on the request alone would replay repeat #1 N times and hide
    every flip; keying on arrival order would break as soon as runs are concurrent.)

    Mode is controlled by env var EVALSEAL_RECORD (or the `record` argument):
      unset/0 -> replay only (raises if a request is missing) — this is CI mode
      1       -> record: already-recorded responses are reused, new calls are captured
    """

    def __init__(self, path: str | Path, record: bool | None = None):
        self.path = Path(path)
        if record is None:
            record = os.environ.get("EVALSEAL_RECORD", "0") == "1"
        self.record = record
        self._entries: dict[str, dict] = {}
        self._cursor: Counter[str] = Counter()
        self._lock = threading.Lock()
        if self.path.exists():
            self._entries = self._load()

    def _load(self) -> dict[str, dict]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != FORMAT_VERSION:
            raise CassetteFormatError(
                f"{self.path} was written by an incompatible evalseal version "
                f"(expected format {FORMAT_VERSION}). Re-record it with EVALSEAL_RECORD=1."
            )
        return raw["entries"]

    def entry_key(self, payload: dict, index: int) -> str:
        return request_key({"request": payload, "repeat": index})

    def call(
        self,
        payload: dict,
        do_real_call: Callable[[], dict],
        index: int | None = None,
    ) -> dict:
        if index is None:
            index = _slot.get()
        with self._lock:
            if index is None:
                # No slot (a direct call, not an eval unit): fall back to arrival order.
                counter_key = request_key(payload)
                index = self._cursor[counter_key]
                self._cursor[counter_key] += 1
            key = self.entry_key(payload, index)
            hit = self._entries.get(key)
            if hit is not None:
                return hit
            if not self.record:
                raise RuntimeError(
                    f"No cassette entry for repeat {index} of request {key[:12]} in {self.path} "
                    "and recording is off. Run with EVALSEAL_RECORD=1 and a key to capture it."
                )
        resp = do_real_call()          # real network call, outside the lock
        with self._lock:
            self._entries[key] = resp
            self._write()
        return resp

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": FORMAT_VERSION, "entries": self._entries}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
