# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## The three checks CI runs

```bash
pytest -q --cov=evalseal --cov-fail-under=90   # no API key needed: cassettes replay
ruff check src tests
mypy
```

Tests must pass with **no network and no API key**. Anything that would call a provider
goes through `Cassette`, which raises in replay mode when an entry is missing. That is
deliberate: a test that silently reaches the network is a broken test.

## Working on the measuring core

`analyze.py` is pure: no I/O, no network, no clock. Keep it that way, and cover new
behavior in `tests/test_analyze.py` first. The rest of the codebase wraps it.

## Recording a cassette

Only needed when changing the demo or adding a provider:

```bash
cp .env.example .env          # add a key; .env is gitignored
EVALSEAL_RECORD=1 evalseal run --dataset ... --target-config ... --scorer-config ... --n 5
```

Recording is resumable: already-recorded responses are reused, so re-running after a
rate limit continues where it stopped. Check a cassette for secrets before committing it;
request bodies are stored, auth headers never are.

## Releasing

1. Bump the version in `pyproject.toml`, `src/evalseal/__init__.py`, and
   `harness_version` in `src/evalseal/models.py`.
2. Commit, then push a matching tag: `git tag -a v0.2.0 -m "..." && git push origin v0.2.0`.
3. `.github/workflows/release.yml` verifies the tag matches the version, runs the tests,
   builds, publishes to PyPI via trusted publishing, and creates the GitHub release.

Changing what a sealed record contains changes its hash. Bump `SCHEMA_VERSION` in
`models.py` when you do, so `verify` can tell an old record apart from a tampered one.
