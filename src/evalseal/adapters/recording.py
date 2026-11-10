from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from ..locking import LockBusy, drop_lock, take_lock

#: The format this build writes. Version 3 keys an entry by the *measurement* it
#: belongs to - case, repeat and role - as well as by the request. Version 2 keyed it
#: by `(request, repeat)` alone, so two different cases that happened to ask the same
#: question shared one entry: one provider draw served both, and their scores agreed by
#: construction. An eval that exists to measure disagreement between repeated draws was
#: measuring one draw twice.
FORMAT_VERSION = 3
#: Formats this build can read. A version-2 cassette stays replayable, under the rules
#: that wrote it, because a recording someone kept is evidence and breaking it would
#: throw that evidence away. It cannot be *appended to*: see `_load`.
SUPPORTED_FORMATS = (2, 3)

#: What a cassette is allowed to do.
#:   replay - no provider calls at all; a miss is an error. This is CI mode.
#:   resume - reuse what is recorded, call the provider for what is not.
#:   fresh  - ignore what is on disk and draw again. A new experiment must not inherit
#:            another run's draws just because the question matches.
Mode = Literal["replay", "resume", "fresh"]
MODES: tuple[Mode, ...] = ("replay", "resume", "fresh")


def canonical(payload: object) -> bytes:
    """The one serialization this module hashes and compares.

    Everything that needs a digest of a request or a response goes through this, so a
    cassette key and the digest sealed in a receipt cannot be computed two ways and
    then disagree.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def request_key(payload: dict) -> str:
    """Stable hash of the *effective* request, so identical requests hit the cassette."""
    return hashlib.sha256(canonical(payload)).hexdigest()


def digest(payload: object) -> str:
    """`sha256:...` of the canonical form of `payload`."""
    return "sha256:" + hashlib.sha256(canonical(payload)).hexdigest()


#: A call was served by a real provider call, or out of the cassette. Typed as literals
#: so a receipt's `origin` field cannot be widened to an arbitrary string on the way in.
LIVE: Literal["live"] = "live"
REPLAY: Literal["replay"] = "replay"


@dataclass(frozen=True)
class CallOutcome:
    """One cassette call: what came back, and where it came from.

    `origin` is the field a receipt could not previously report. Without it a run that
    replayed a stale cassette and a run that called the model produce receipts no
    reader can tell apart, which is the strongest false green a CI gate can give.
    """
    response: dict
    origin: Literal["live", "replay"]
    request_digest: str
    response_digest: str
    repeat: int
    entry_key: str


@dataclass(frozen=True)
class Unit:
    """The measurement a call belongs to: a case, one of its repeats, and a role.

    `case_id` is None only for a caller that did not say - `slot()` keeps that door
    open for compatibility. A None case is written into the key explicitly rather than
    left out, so "no case was given" and "case 'None'" cannot collide.
    """
    case_id: str | None = None
    repeat: int = 0
    role: str = "target"


_unit: contextvars.ContextVar[Unit | None] = contextvars.ContextVar(
    "evalseal_unit", default=None)


@contextmanager
def unit(case_id: str | None, repeat: int, role: str = "target") -> Iterator[None]:
    """Pin every cassette call inside this block to one measurement.

    The executor opens one of these per (case, repeat) and names the role around each
    call, so a replay returns the same response for the same measurement no matter how
    many workers run or what order they finish in - and so two cases asking the same
    question do not share a draw.
    """
    token = _unit.set(Unit(case_id, repeat, role))
    try:
        yield
    finally:
        _unit.reset(token)


def current_unit() -> Unit | None:
    """The measurement in progress, or None outside one.

    Public because a custom `Target` genuinely needs it - a scripted test double has to
    know which repeat it is answering. The alternative was reaching into the module's
    contextvar, which is what one of this project's own tests did until the variable
    was renamed under it.
    """
    return _unit.get()


@contextmanager
def slot(index: int) -> Iterator[None]:
    """Pin every cassette call inside this block to repeat `index`, with no case.

    Exported from the package root since 0.x, so it keeps working. It cannot tell two
    cases apart, which is why the executor uses `unit()`: prefer that.
    """
    with unit(None, index):
        yield


class CassetteFormatError(RuntimeError):
    """The cassette on disk was written by an incompatible version."""


class CassetteBusy(LockBusy):
    """Another writer owns this cassette.

    A cassette has one writer at a time, enforced rather than assumed: every writer
    keeps the whole entry table in memory and rewrites the whole file, so two of them
    each erase the other's responses and the last one wins. Writer A's response was
    admitted, acknowledged and then silently deleted by a process that never knew A
    existed - and the receipt A sealed could no longer be replayed. See
    docs/adr/0002-cassette-writer-strategy.md.
    """


#: Where `finalize` puts a snapshot when the caller names no directory.
SNAPSHOT_DIR = "snapshots"


@dataclass(frozen=True)
class CassetteSnapshot:
    """The responses behind one receipt, written somewhere nothing will append to.

    A receipt binds its cassette by digest, but the cassette is a file later runs are
    free to rewrite. An honest later run that only added repeats turned an earlier
    receipt's artifact check from `ok` to `changed`; a `fresh` re-draw wrote the same
    keys with new responses and destroyed the earlier run's evidence outright. A
    snapshot is the entries that run actually used, in the format they were keyed under,
    written to a file whose name contains its own digest.
    """
    path: Path
    sha256: str
    n_entries: int
    format: int


def _resolve_mode(mode: str | None, record: bool | None) -> Mode:
    """One mode from three ways of asking for one, most explicit first."""
    if mode is not None:
        if mode not in MODES:
            raise ValueError(
                f"unknown cassette mode {mode!r}; expected one of {', '.join(MODES)}")
        return mode
    if record is not None:
        return "resume" if record else "replay"
    from_env = os.environ.get("EVALSEAL_MODE")
    if from_env:
        if from_env not in MODES:
            raise ValueError(
                f"unknown cassette mode {from_env!r} in $EVALSEAL_MODE; expected one "
                f"of {', '.join(MODES)}")
        return from_env
    return "resume" if os.environ.get("EVALSEAL_RECORD", "0") == "1" else "replay"


def _serialize(version: int, entries: dict[str, dict]) -> str:
    """The one on-disk form of a cassette, for the live file and for a snapshot alike."""
    return json.dumps({"version": version, "entries": entries}, indent=2, sort_keys=True)


def _write_atomically(path: Path, text: str) -> None:
    """Replace `path` with `text`: the file is always the old version or the new one.

    A plain write truncates first and fills second, so an interrupt landing in between
    left a partial file - and a partial JSON file loses every response in it, not just
    the latest. Writing a sibling temp file and renaming it over the original closes
    that window: os.replace is atomic on POSIX and Windows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same directory, so the rename never crosses a filesystem boundary.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leave no stray temp file behind, then let the error surface.
        tmp.unlink(missing_ok=True)
        raise


class Cassette:
    """JSON-backed record/replay store keyed by (request, repeat).

    Repeating an identical request is the whole point of an N-run eval, so the repeat
    index is part of the key: repeat 3 of a case always replays the response recorded
    for repeat 3. (Keying on the request alone would replay repeat #1 N times and hide
    every flip; keying on arrival order would break as soon as runs are concurrent.)

    The mode is explicit: `mode="replay" | "resume" | "fresh"`. `EVALSEAL_MODE` sets it
    from the environment; `EVALSEAL_RECORD=1`/`record=True` still mean `resume` and
    `0`/`False` still mean `replay`, because both are published interfaces.

    A cassette that may write **owns its file**: it takes an exclusive advisory lock and
    a second writer raises `CassetteBusy` rather than erasing the first one's responses.
    Readers take no lock, because replay never writes and a CI job that only replays
    must not be blockable by a recording run. Close it - or use it as a context manager -
    to release; dropping it on the floor also works, since the lock lives in a file
    descriptor the interpreter closes.
    """

    def __init__(
        self,
        path: str | Path,
        record: bool | None = None,
        *,
        mode: str | None = None,
    ):
        self.path = Path(path)
        self.mode = _resolve_mode(mode, record)
        self._entries: dict[str, dict] = {}
        self._cursor: Counter[str] = Counter()
        self._lock = threading.Lock()
        #: One in-flight marker per key. Two callers scheduled for the same measurement
        #: used to call the provider twice and race to write; the loser's response was
        #: dropped while its score stayed in the receipt, so replaying that receipt
        #: produced a different number. Now the second caller waits for the first.
        self._inflight: dict[str, threading.Event] = {}
        #: Keys re-drawn in this session. `fresh` ignores the file, not itself: asking
        #: twice for one measurement must still give one answer.
        self._drawn: set[str] = set()
        #: Keys this session read or wrote - the entries a receipt from it depends on,
        #: and so exactly what `finalize` puts in the snapshot.
        self._used: set[str] = set()
        self.format = FORMAT_VERSION
        #: The handle holding this cassette's writer lock, or None for a reader.
        self._owner: IO[bytes] | None = None
        try:
            if self.record:
                # Taken before the file is read, so what this cassette loads is what it
                # owns, and released again if the load then rejects the format.
                try:
                    self._owner = take_lock(self.path, "cassette")
                except LockBusy as e:
                    raise CassetteBusy(
                        f"{e}. A cassette has one writer at a time: both would rewrite "
                        f"the whole file and the last one would win, silently deleting "
                        f"responses the other had already recorded. Give this run its "
                        f"own --cassette path, or wait for the other to finish."
                    ) from e
            if self.path.exists():
                self._entries = self._load()
        except BaseException:
            self.close()
            raise

    # --- ownership ----------------------------------------------------------------

    def close(self) -> None:
        """Release the writer lock. Idempotent, and a no-op for a reader.

        `getattr` rather than `self._owner`, because this also runs on an object whose
        `__init__` raised before it got that far - an unknown mode name, for instance -
        and a half-built cassette must not turn a clear error into an AttributeError
        from a finalizer.
        """
        owner = getattr(self, "_owner", None)
        self._owner = None
        if owner is not None:
            drop_lock(owner)

    def __enter__(self) -> Cassette:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Closing the descriptor is what releases an flock, so this happens with or
        # without this method. Doing it explicitly keeps a ResourceWarning away from
        # callers who never close the cassette.
        self.close()

    def _load(self) -> dict[str, dict]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        version = raw.get("version") if isinstance(raw, dict) else None
        if version not in SUPPORTED_FORMATS:
            raise CassetteFormatError(
                f"{self.path} was written by an incompatible evalseal version "
                f"(format {version!r}; this build reads "
                f"{', '.join(str(v) for v in SUPPORTED_FORMATS)}). "
                "Re-record it with EVALSEAL_RECORD=1."
            )
        self.format = version
        if version != FORMAT_VERSION and self.mode != "replay":
            # Appending format-3 entries to a format-2 file would leave a cassette whose
            # keys mean two different things, and whether a call hit or missed would
            # depend on which half of the file answered.
            raise CassetteFormatError(
                f"{self.path} is cassette format {version}, which keys entries by "
                f"(request, repeat) and cannot tell two cases apart that ask the same "
                f"question. This build writes format {FORMAT_VERSION}. It can still be "
                f"replayed as it is; to record into it, re-record it from scratch "
                f"(delete it and run with EVALSEAL_RECORD=1)."
            )
        return raw["entries"]

    @property
    def record(self) -> bool:
        """Whether this cassette may call the provider. The published flag."""
        return self.mode != "replay"

    def entry_key(self, payload: dict, index: int, *, case_id: str | None = None,
                  role: str = "target") -> str:
        """The key this cassette's format uses for one measurement's request.

        Format 2 is keyed without the case or the role, and is read that way, because
        that is how its entries were written. The defect is in the format, so it is
        fixed by a new format rather than by reading the old one differently.
        """
        if self.format != FORMAT_VERSION:
            return request_key({"request": payload, "repeat": index})
        return request_key({"request": payload, "case": case_id,
                            "repeat": index, "role": role})

    def call(
        self,
        payload: dict,
        do_real_call: Callable[[], dict],
        index: int | None = None,
    ) -> dict:
        """The response alone. Kept because it is the public shape callers already use."""
        return self.call_detail(payload, do_real_call, index).response

    def call_detail(
        self,
        payload: dict,
        do_real_call: Callable[[], dict],
        index: int | None = None,
    ) -> CallOutcome:
        """The response, plus where it came from and digests of both sides.

        The digests are computed here rather than by each adapter so that the value a
        receipt seals is the same value the cassette keyed on. Two implementations of
        "the digest of this request" would eventually disagree, and the disagreement
        would read as a tampered cassette.
        """
        here = _unit.get()
        if index is None:
            index = here.repeat if here is not None else None
        case_id = here.case_id if here is not None else None
        role = here.role if here is not None else "target"

        while True:
            with self._lock:
                if index is None:
                    # No unit (a direct call, not an eval measurement): fall back to
                    # arrival order.
                    counter_key = request_key(payload)
                    index = self._cursor[counter_key]
                    self._cursor[counter_key] += 1
                key = self.entry_key(payload, index, case_id=case_id, role=role)
                # `fresh` ignores what the file already held, but not what this session
                # has already drawn: one measurement gets one answer either way.
                usable = self.mode != "fresh" or key in self._drawn
                hit = self._entries.get(key) if usable else None
                if hit is not None:
                    return self._outcome(payload, hit, REPLAY, index, key)
                if not self.record:
                    raise RuntimeError(
                        f"No cassette entry for repeat {index} of request {key[:12]} in "
                        f"{self.path} and recording is off. Run with EVALSEAL_RECORD=1 "
                        "and a key to capture it."
                    )
                waiter = self._inflight.get(key)
                if waiter is None:
                    # This caller owns the draw for this measurement.
                    self._inflight[key] = threading.Event()
                    break
            # Someone else is already drawing it. Wait for their answer rather than
            # asking the provider a second time: two answers for one measurement means
            # one of them is not in the cassette the receipt is bound to.
            waiter.wait()

        try:
            resp = do_real_call()      # real network call, outside the lock
        finally:
            with self._lock:
                released = self._inflight.pop(key, None)
            if released is not None:
                # Released even when the call raised, or one transient failure would
                # leave every later caller for this measurement waiting forever.
                released.set()
        with self._lock:
            self._entries[key] = resp
            self._drawn.add(key)
            self._write()
        return self._outcome(payload, resp, LIVE, index, key)

    def _outcome(self, payload: dict, response: dict,
                 origin: Literal["live", "replay"], index: int, key: str) -> CallOutcome:
        # Recorded for every call, replayed or live: the snapshot is the entries this
        # run's scores came from, which is not the same set as the whole file.
        self._used.add(key)
        return CallOutcome(
            response=response,
            origin=origin,
            # The request as it was keyed, and the response bytes this call used -
            # on a replay, the bytes in the cassette rather than anything new.
            request_digest=digest(payload),
            response_digest=digest(response),
            repeat=index,
            entry_key=key,
        )

    def _write(self) -> None:
        """Persist the whole entry table, atomically."""
        # A format-2 file is replay-only, so `_write` is only ever reached for
        # format 3. Asserted rather than assumed: writing format 3 into a file whose
        # keys mean something else is the one outcome `_load` exists to prevent.
        assert self.format == FORMAT_VERSION, self.format
        _write_atomically(self.path, _serialize(FORMAT_VERSION, self._entries))

    # --- finalization -------------------------------------------------------------

    def used_entries(self) -> dict[str, dict]:
        """The entries this session read or wrote, in key order."""
        with self._lock:
            return {k: self._entries[k] for k in sorted(self._used) if k in self._entries}

    def finalize(self, directory: str | Path | None = None,
                 *, name: str | None = None) -> CassetteSnapshot:
        """Write the responses behind this run somewhere nothing will append to.

        The snapshot holds the entries this session actually used, serialized exactly
        as a cassette is, so it *is* a cassette: `Cassette(snapshot, mode="replay")`
        reproduces the run it was finalized from even after the live file has been
        re-drawn. Its name carries its own digest, so a later run with different
        entries writes a different file and no snapshot is ever overwritten.

        Writing one that already exists is a no-op when the bytes match. When they do
        not, that is an edited or corrupted snapshot, and it raises rather than
        replacing a file an earlier receipt is bound to.
        """
        entries = self.used_entries()
        text = _serialize(self.format, entries)
        raw = text.encode()
        sha = hashlib.sha256(raw).hexdigest()
        where = Path(directory) if directory is not None else self.path.parent / SNAPSHOT_DIR
        # Named so a run directory reads at a glance, and content-addressed so a later
        # run with different entries cannot land on the same name.
        target = where / (name or f"{self.path.stem}.snapshot-{sha[:16]}.json")
        if target.exists():
            if target.read_bytes() != raw:
                raise RuntimeError(
                    f"{target} already exists and holds different bytes "
                    f"(sha256:{hashlib.sha256(target.read_bytes()).hexdigest()[:16]} "
                    f"on disk, sha256:{sha[:16]} here). A snapshot is immutable: an "
                    f"earlier receipt may be bound to the file that is there. Move it "
                    f"aside, or finalize into another directory."
                )
        else:
            _write_atomically(target, text)
        return CassetteSnapshot(path=target, sha256=f"sha256:{sha}",
                                n_entries=len(entries), format=self.format)
