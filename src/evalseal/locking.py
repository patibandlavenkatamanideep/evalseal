"""Cross-platform advisory locks for the files a run writes.

Two uses, with deliberately different waiting behaviour:

* `ledger_lock` guards the ledger's read-modify-append section and **waits**. Two
  `evalseal run` processes can otherwise read the same head hash, both find it valid,
  and both append a record claiming the same predecessor. The chain then has siblings
  and the tamper-evidence weakens: neither branch is distinguishable from a rewrite.
  The critical section is one append, so waiting for it costs nothing.
* `take_lock` gives one writer ownership of a file for a whole run and **fails at
  once** when someone else owns it. See docs/adr/0002-cassette-writer-strategy.md.

Scope, stated plainly: this is a **local filesystem** lock. `fcntl.flock` and
`msvcrt.locking` coordinate processes on one machine. On NFS, SMB or any networked
filesystem the guarantees are weaker or absent, and EvalSeal does not attempt
distributed consensus.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt
else:
    import fcntl


def lock_path_for(ledger: str | Path) -> Path:
    """`<ledger>.lock`, alongside the ledger it guards."""
    ledger = Path(ledger)
    return ledger.with_name(ledger.name + ".lock")


def _acquire(handle: IO[bytes]) -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows path
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release(handle: IO[bytes]) -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows path
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def ledger_lock(ledger: str | Path) -> Iterator[Path]:
    """Hold an exclusive lock on `<ledger>.lock` for the whole block.

    The lock is held by an open file descriptor, not by the file's existence, so a
    process that dies mid-write releases it when the OS closes its descriptors. There is
    no stale lock file to clean up and no timeout to tune — which is the main reason to
    prefer flock over a hand-rolled lock-file protocol.
    """
    path = lock_path_for(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab+") as handle:
        _acquire(handle)
        try:
            yield path
        finally:
            _release(handle)


class LockBusy(RuntimeError):
    """Another process (or another object in this one) already owns the lock."""


def _acquire_nowait(handle: IO[bytes]) -> None:
    """Take the lock or raise OSError at once. Never waits."""
    if sys.platform == "win32":  # pragma: no cover - Windows path
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _holder(path: Path) -> str:
    """A hint about who holds the lock, from the pid the holder wrote into the file.

    Advisory and best-effort: the file is read without the lock, so the answer can be
    stale. It goes into an error message as a hint, never into a decision.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return f" (held by pid {text})" if text.isdigit() else ""


def take_lock(target: str | Path, what: str = "file") -> IO[bytes]:
    """Own `<target>.lock` exclusively, or raise `LockBusy` immediately.

    The caller keeps the returned handle for as long as it holds the lock, and passes it
    to `drop_lock` to release. Unlike `ledger_lock` this does not wait: see
    docs/adr/0002-cassette-writer-strategy.md for why a whole-run lock must fail fast
    rather than queue behind a run that may take hours.
    """
    path = lock_path_for(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("ab+")
    try:
        _acquire_nowait(handle)
    except OSError as e:
        hint = _holder(path)
        handle.close()
        raise LockBusy(
            f"{what} {target} is already being written by another evalseal "
            f"process{hint}"
        ) from e
    # A pid for the next caller's error message. Not a lock - the flock is the lock.
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
    except OSError:  # pragma: no cover - the lock is held either way
        pass
    return handle


def drop_lock(handle: IO[bytes]) -> None:
    """Release a lock taken by `take_lock` and close its handle. Safe to call twice."""
    if handle.closed:
        return
    try:
        _release(handle)
    finally:
        handle.close()
