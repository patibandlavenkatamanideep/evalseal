"""A cross-platform advisory lock for the ledger's read-modify-append section.

Two `evalseal run` processes can otherwise read the same head hash, both find it valid,
and both append a record claiming the same predecessor. The chain then has siblings and
the tamper-evidence weakens: neither branch is distinguishable from a rewrite.

Scope, stated plainly: this is a **local filesystem** lock. `fcntl.flock` and
`msvcrt.locking` coordinate processes on one machine. On NFS, SMB or any networked
filesystem the guarantees are weaker or absent, and EvalSeal does not attempt
distributed consensus.
"""

from __future__ import annotations

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
