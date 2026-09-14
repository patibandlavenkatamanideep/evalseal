from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Callable


def request_key(payload: dict) -> str:
    """Stable hash of the *effective* request, so identical requests hit the cassette."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class Cassette:
    """JSON-backed record/replay store keyed by request hash.

    Each key maps to an ordered *list* of responses. Repeating an identical request is
    the whole point of an N-run eval, so the k-th call with a given key is served the
    k-th recorded response. (A single response per key would replay repeat #1 N times
    and hide every flip.)

    Mode is controlled by env var EVALSEAL_RECORD (or the `record` argument):
      unset/0 -> replay only (raises if a request is missing) — this is CI mode
      1       -> record: already-recorded responses are reused, new calls are captured
    """

    def __init__(self, path: str | Path, record: bool | None = None):
        self.path = Path(path)
        if record is None:
            record = os.environ.get("EVALSEAL_RECORD", "0") == "1"
        self.record = record
        self._data: dict[str, list[dict]] = {}
        self._cursor: Counter[str] = Counter()
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def call(self, payload: dict, do_real_call: Callable[[], dict]) -> dict:
        key = request_key(payload)
        index = self._cursor[key]
        self._cursor[key] += 1
        recorded = self._data.get(key, [])
        if index < len(recorded):
            return recorded[index]
        if not self.record:
            raise RuntimeError(
                f"No cassette entry #{index} for request {key[:12]} in {self.path} and "
                f"recording is off. Run with EVALSEAL_RECORD=1 and an API key to capture it."
            )
        resp = do_real_call()          # real network call, record mode only
        self._data.setdefault(key, []).append(resp)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        return resp
