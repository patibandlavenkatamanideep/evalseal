from __future__ import annotations

import itertools

import pytest

from evalseal.adapters.dataset import Case, Dataset

# Every variable `provenance.ci_provenance` looks at. Cleared for the whole suite so a
# record sealed in a test has no CI claim unless a test sets one on purpose. Without
# this, "a receipt produced outside CI" means something different on a laptop and on a
# GitHub runner, and the tests that assert it invert when they finally run in CI - which
# is exactly what happened.
_CI_MARKERS = (
    "CI", "GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_REPOSITORY", "GITHUB_REF",
    "GITHUB_SHA", "GITHUB_EVENT_NAME", "GITHUB_SERVER_URL",
    "GITLAB_CI", "CI_PIPELINE_ID", "CI_PROJECT_PATH", "CI_COMMIT_REF_NAME",
    "CI_COMMIT_SHA", "CI_PIPELINE_SOURCE", "CI_PIPELINE_URL",
    "CIRCLECI", "CIRCLE_BUILD_NUM", "CIRCLE_PROJECT_REPONAME", "CIRCLE_BRANCH",
    "CIRCLE_SHA1", "CIRCLE_BUILD_URL",
    "BUILDKITE", "BUILDKITE_BUILD_NUMBER", "BUILDKITE_PIPELINE_SLUG",
    "BUILDKITE_BRANCH", "BUILDKITE_COMMIT", "BUILDKITE_BUILD_URL",
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """Tests never record, never touch the real ledger, and never inherit a CI claim."""
    monkeypatch.delenv("EVALSEAL_RECORD", raising=False)
    monkeypatch.delenv("EVALSEAL_API_KEY", raising=False)
    for marker in _CI_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.chdir(tmp_path)


def scripted(outputs_by_prompt: dict[str, list[str]]):
    """A fn for LocalCallableTarget that cycles through fixed outputs per prompt."""
    cycles = {p: itertools.cycle(outs) for p, outs in outputs_by_prompt.items()}
    return lambda prompt: next(cycles[prompt])


def make_dataset(*prompts: str) -> Dataset:
    return Dataset([Case(f"c{i}", p) for i, p in enumerate(prompts)], hash="sha256:test")
