# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html);
while the version is 0.x, a minor bump may break formats.

## [0.3.0] - 2026-09-17

### Added
- `--fail-on {none,unstable,borderline}` chooses which stability classes fail a run.
- `--junit-xml PATH` writes JUnit XML so CI systems show per-case reproducibility.
- `--timeout` for per-request timeouts.
- Per-case durations in `report.json` and JUnit output.
- A typed public API (`from evalseal import run_eval, analyze_case, ...`) with a PEP 561
  `py.typed` marker, so downstream type checkers use the annotations.
- `pip-audit` job in CI; GitHub Actions pinned to commit SHAs with Dependabot updating them;
  least-privilege `permissions` on both workflows.
- Issue and pull request templates, `SECURITY.md`, and this changelog.

### Changed
- Interrupting a run (Ctrl-C) now exits 130 with a message saying recorded responses are
  kept, instead of a traceback.

## [0.2.0] - 2026-09-17

### Added
- `--concurrency` (default 4) runs cases in parallel; `--max-retries` (default 5) retries
  429/408/5xx and connection errors with backoff that honours `Retry-After`; `--quiet`.
- Progress bar on a TTY.
- `ruff`, `mypy`, and a 90% coverage floor in CI; tests on Python 3.11, 3.12 and 3.13.
- `CONTRIBUTING.md`.

### Changed
- **Breaking:** cassette entries are keyed by (request, repeat) instead of arrival order,
  so replays are identical at any concurrency. Cassettes written by 0.1.x are rejected
  with a clear message; re-record them.
- **Breaking:** records are sealed under schema 1.1 (the manifest now records concurrency).
  `verify` names an older schema instead of reporting tampering.

## [0.1.0] - 2026-09-15

### Added
- `run`, `verify`, `diff`: N-run evals with bootstrap confidence intervals, per-case flip
  rates and stability classes; provenance capture for target and judge; record/replay
  cassettes for keyless CI; hash-linked tamper-evident ledger; Markdown and JSON reports.

[0.3.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v0.3.0
[0.2.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v0.2.0
[0.1.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v0.1.0
