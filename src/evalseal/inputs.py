"""Every file a run depends on is read once, hashed as it is read, and used from that.

An input used to be *used* at the start of a run and *hashed* at the end. Anything that
rewrote the file in between - a `git checkout`, an editor save, another job regenerating
a dataset - left a receipt describing bytes that produced no score at all:

```
prompt actually used  : what the model was asked
dataset bytes at start: sha256:99a1baea720ec4631
dataset bytes at end  : sha256:15524b82f5e323980
digest SEALED in the receipt: sha256:15524b82f5e323980
  -> sealed the bytes that ran  : False
  -> sealed bytes nothing ran on: True
manifest.dataset.hash : sha256:99a1baea720ec4631 (computed at load)
any warning about it  : []
validate_record says  : nothing wrong
```

The receipt held two different digests for one file and said nothing about either, and
re-hashing the artifact afterwards reported `ok` against a file no score came from. That
is the strongest kind of false green the artifact binding can give: a verification that
passes because it is checking the wrong bytes.

A capture closes it by construction rather than by a cross-check: the bytes are read
once, the digest is of what was read, the parse is of what was read, and the file is
re-read at the end only to *report* that it moved.

Scope: this holds a file's bytes in memory for the life of the run, which suits the
files EvalSeal binds - datasets, suites, tool manifests and frozen tool responses. It is
not a general large-file mechanism.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read(path: str | Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


@dataclass(frozen=True)
class InputSnapshot:
    """One file as a run found it: the bytes it used, and the digest of those bytes."""

    role: str
    path: str
    data: bytes | None          # None when the file could not be read at all
    sha256: str | None          # the digest of `data`, so they cannot disagree
    size: int | None

    @property
    def readable(self) -> bool:
        return self.data is not None

    def text(self) -> str:
        """The captured bytes as text. Raises if there was nothing to read."""
        if self.data is None:
            raise ValueError(f"{self.role} {self.path} was not readable when it was read")
        return self.data.decode("utf-8")

    def drift(self) -> str | None:
        """How the file on disk now differs from the bytes this run used, or None.

        Phrased as a sentence rather than a boolean because the three ways it can
        differ need three different sentences, and "unreadable now" must never collapse
        into "unchanged".
        """
        now = _read(self.path)
        current = _digest(now) if now is not None else None
        if current == self.sha256:
            return None
        if current is None:
            return (f"it is no longer readable; this run read "
                    f"{self.sha256} from it")
        if self.sha256 is None:
            return f"it was unreadable when this run started and is now {current}"
        return f"it is now {current}; this run used {self.sha256}"


def capture(role: str, path: str | Path) -> InputSnapshot:
    """Read one input and hash what was read.

    An unreadable file is captured as unreadable rather than refused: `run_eval` seals
    it as `external` with a note saying so, which is the honest record, and refusing
    would push people towards not binding the file at all.
    """
    data = _read(path)
    return InputSnapshot(
        role=role,
        path=str(path),
        data=data,
        sha256=_digest(data) if data is not None else None,
        size=len(data) if data is not None else None,
    )


def capture_all(paths: Mapping[str, str | Path]) -> dict[str, InputSnapshot]:
    """Capture every named input, in role order."""
    return {role: capture(role, paths[role]) for role in sorted(paths)}


__all__ = ["InputSnapshot", "capture", "capture_all"]
