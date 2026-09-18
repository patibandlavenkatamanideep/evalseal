## What changed

<!-- One or two sentences. Link the issue if there is one. -->

## Checks

- [ ] `pytest -q --cov=evalseal --cov-fail-under=90` passes with **no API key set**
- [ ] `ruff check src tests` and `mypy` pass
- [ ] Behaviour change is covered by a test
- [ ] If a sealed record's fields changed, `SCHEMA_VERSION` is bumped
- [ ] If the cassette format changed, `FORMAT_VERSION` is bumped and DESIGN.md says why
- [ ] No API keys, tokens, or personal data in cassettes or fixtures
