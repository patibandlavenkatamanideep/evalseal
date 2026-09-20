"""Facts about what produced a run: the code, the environment, the inputs.

Everything here is a hash or a short identifier. Nothing captured by this module is
secret: file contents are hashed rather than stored, and no environment variable is
read, so an API key cannot reach a sealed record through here.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from pathlib import Path
from typing import TypedDict


def _evalseal_version() -> str:
    """Read the version from installed metadata.

    Importing it from the package would be circular: `evalseal/__init__` imports the
    executor, which imports this module while the package is still initializing.
    """
    # By call time the package is fully imported, so its own __version__ is safe to
    # read and is the truth. Installed metadata can lag behind an editable checkout,
    # and sealing a stale version would misdescribe the run.
    try:
        from evalseal import __version__

        return __version__
    except ImportError:  # pragma: no cover
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("evalseal")
        except PackageNotFoundError:
            return "unknown"


def text_hash(text: str) -> str:
    """sha256 of a string, the form used for prompts and rubrics."""
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def file_hash(path: str | Path) -> str | None:
    """sha256 of a file's bytes, or None when it is unreadable."""
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


class GitProvenance(TypedDict):
    commit: str | None
    dirty: bool | None


def git_provenance() -> GitProvenance:
    """Commit and worktree cleanliness, when run inside a git checkout.

    A dirty worktree means the sealed commit does not fully describe the code that ran,
    which is worth recording rather than hiding.
    """
    commit = _git("rev-parse", "HEAD")
    if commit is None:
        return {"commit": None, "dirty": None}
    status = _git("status", "--porcelain")
    return {"commit": commit, "dirty": bool(status)}


def environment_provenance() -> dict[str, str]:
    """Interpreter and platform. Coarse on purpose — enough to explain a difference."""
    return {
        "evalseal_version": _evalseal_version(),
        "python_version": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "implementation": sys.implementation.name,
    }
