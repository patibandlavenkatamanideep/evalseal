"""Facts about what produced a run: the code, the environment, the inputs.

Everything here is a hash or a short identifier. File contents are hashed rather than
stored, so a dataset cannot leak through here.

One module reads environment variables: `ci_provenance`, and it reads only the fixed
allowlist below. That list is spelled out as constants rather than matched by prefix,
because a rule like "record every GITHUB_* variable" would sweep up
`GITHUB_TOKEN`. No value read here is secret, and a variable not on the list cannot
reach a sealed record.
"""

from __future__ import annotations

import hashlib
import os
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


# Non-secret identifiers only, named one by one. Nothing here is a token, and no
# pattern match is used, so a new secret in the environment cannot be swept in.
_CI_VARS: dict[str, dict[str, str]] = {
    "github_actions": {
        "marker": "GITHUB_ACTIONS", "run_id": "GITHUB_RUN_ID",
        "repository": "GITHUB_REPOSITORY", "ref": "GITHUB_REF",
        "commit": "GITHUB_SHA", "event": "GITHUB_EVENT_NAME",
    },
    "gitlab_ci": {
        "marker": "GITLAB_CI", "run_id": "CI_PIPELINE_ID",
        "repository": "CI_PROJECT_PATH", "ref": "CI_COMMIT_REF_NAME",
        "commit": "CI_COMMIT_SHA", "event": "CI_PIPELINE_SOURCE",
        "run_url": "CI_PIPELINE_URL",
    },
    "circleci": {
        "marker": "CIRCLECI", "run_id": "CIRCLE_BUILD_NUM",
        "repository": "CIRCLE_PROJECT_REPONAME", "ref": "CIRCLE_BRANCH",
        "commit": "CIRCLE_SHA1", "run_url": "CIRCLE_BUILD_URL",
    },
    "buildkite": {
        "marker": "BUILDKITE", "run_id": "BUILDKITE_BUILD_NUMBER",
        "repository": "BUILDKITE_PIPELINE_SLUG", "ref": "BUILDKITE_BRANCH",
        "commit": "BUILDKITE_COMMIT", "run_url": "BUILDKITE_BUILD_URL",
    },
}


def ci_provenance() -> dict[str, str | bool | None] | None:
    """What the environment claims about the CI job running this, or None locally.

    **This is a claim, not proof.** Every value is an environment variable, and a
    local shell can export the same ones. What turns it into evidence is outside this
    record: a `run_url` a third party can open, and a ledger signed by a key that only
    the CI job holds. A pre-registration that requires CI is a statement about where
    the receipt is *supposed* to come from, and it catches the accident - a receipt
    produced on a laptop and attached to a PR - rather than a determined forger.
    """
    for provider, keys in _CI_VARS.items():
        if os.environ.get(keys["marker"], "").lower() not in {"true", "1", "yes"}:
            continue
        out: dict[str, str | bool | None] = {"provider": provider, "claimed": True}
        for field, var in keys.items():
            if field != "marker":
                out[field] = os.environ.get(var) or None
        if provider == "github_actions":
            server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            repo, run_id = out.get("repository"), out.get("run_id")
            out["run_url"] = f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id \
                else None
        return out
    # Many systems set only CI=true. Record that plainly rather than guessing a vendor.
    if os.environ.get("CI", "").lower() in {"true", "1", "yes"}:
        return {"provider": "unknown", "claimed": True}
    return None
