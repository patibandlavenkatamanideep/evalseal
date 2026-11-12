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


#: The byte every lock contends for. `fcntl.flock` locks the whole file and ignores
#: offsets; `msvcrt.locking` locks `nbytes` **from the current file position**, so two
#: processes only contend if they lock the same place. They did not: the handle was
#: opened in append mode, so the position was end-of-file, and the second writer locked
#: a byte past the first writer's pid and was granted it. A second `evalseal run` on one
#: cassette was served instead of refused, on Windows only.
_LOCK_BYTE = 0
#: The holder's pid lives after the locked byte, fixed width so it can be overwritten
#: without truncating. Truncating a file whose byte 0 another process holds raises
#: PermissionError on Windows, where the lock is mandatory rather than advisory.
_PID_OFFSET = 1
_PID_WIDTH = 16


def lock_path_for(ledger: str | Path) -> Path:
    """`<ledger>.lock`, alongside the ledger it guards."""
    ledger = Path(ledger)
    return ledger.with_name(ledger.name + ".lock")


def _open_lock_file(path: Path) -> IO[bytes]:
    """Open `<target>.lock` for reading and writing, creating it if absent.

    Not `path.open("ab+")`: in append mode every write goes to end-of-file whatever
    the position is, so the pid could only be written by truncating first - which is
    the operation Windows refuses while another process holds the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    return os.fdopen(fd, "rb+")


def _acquire(handle: IO[bytes]) -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows path
        handle.seek(_LOCK_BYTE)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release(handle: IO[bytes]) -> None:
    if sys.platform == "win32":  # pragma: no cover - Windows path
        handle.seek(_LOCK_BYTE)
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
    with _open_lock_file(path) as handle:
        _acquire(handle)
        try:
            yield path
        finally:
            _release(handle)


class LockBusy(RuntimeError):
    """Another process (or another object in this one) already owns the lock."""


def _acquire_nowait(handle: IO[bytes]) -> None:
    """Take the lock or raise OSError at once. Never waits.

    The seek is what makes this work on Windows: `msvcrt.locking` locks from the
    current position, so both contenders have to be at the same byte.
    """
    if sys.platform == "win32":  # pragma: no cover - Windows path
        handle.seek(_LOCK_BYTE)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _holder(path: Path) -> str:
    """A hint about who holds the lock, from the pid the holder wrote into the file.

    Advisory and best-effort: the file is read without the lock, so the answer can be
    stale. It goes into an error message as a hint, never into a decision.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(_PID_OFFSET)
            text = handle.read(_PID_WIDTH).decode("utf-8", "replace").strip()
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
    handle = _open_lock_file(path)
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
    # Written after the locked byte, at a fixed width, so nothing has to truncate.
    try:
        handle.seek(_PID_OFFSET)
        handle.write(str(os.getpid()).encode().ljust(_PID_WIDTH)[:_PID_WIDTH])
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
