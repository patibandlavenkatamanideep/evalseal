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


def fake_http(monkeypatch, fake_post):
    """Patch the HTTP layer with a `fake_post(self, url, headers=None, json=None)`.

    The adapters stream responses so the body can be read under a byte limit
    (`transport.py`), so patching `httpx.Client.post` no longer intercepts anything.
    This keeps every test's `fake_post` exactly as it was and adapts it: the response
    the fake builds is rebuilt as a fresh streaming one, because a `httpx.Response`
    constructed with `json=` has already consumed its stream and cannot be iterated.

    A fake that raises still raises, and one that returns a prepared `httpx.Response`
    keeps its status and headers - which is what the retry and error-path tests assert
    on.
    """
    import contextlib

    import httpx

    @contextlib.contextmanager
    def fake_stream(self, method, url, headers=None, content=None, json=None, **kw):
        built = fake_post(self, url, headers=headers, json=json)
        yield httpx.Response(
            built.status_code, headers=built.headers,
            stream=httpx.ByteStream(built.content),
            request=built.request or httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, "stream", fake_stream)
    # Left in place too: a test that calls `post` directly, or an adapter path that has
    # not moved to the bounded transport, must not silently reach the network.
    monkeypatch.setattr(httpx.Client, "post", fake_post)


def fake_tsa_http(monkeypatch, fake_post):
    """Patch the HTTP layer with a module-level `fake_post(url, **kwargs)`.

    The RFC 3161 backend reads its reply under a byte limit now, so it streams through
    `httpx.Client` rather than calling `httpx.post`. Both are patched: the stream so
    the fake is reached, and `httpx.post` so a call that still goes that way cannot
    quietly reach the network - several of these tests exist precisely to assert that
    verification touches no network at all.
    """
    import contextlib

    import httpx

    @contextlib.contextmanager
    def fake_stream(self, method, url, headers=None, content=None, json=None, **kw):
        built = fake_post(url, headers=headers, content=content, json=json)
        yield httpx.Response(
            built.status_code, headers=built.headers,
            stream=httpx.ByteStream(built.content),
            request=built.request or httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, "stream", fake_stream)
    monkeypatch.setattr(httpx, "post", fake_post)
