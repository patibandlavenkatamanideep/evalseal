# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html);
while the version is 0.x, a minor bump may break formats.

## [1.1.0] - 2026-09-18

### Changed
- **Binary verdicts now use a Wilson score interval instead of the bootstrap.** Resampling
  identical verdicts produced a width-zero interval, so a perfectly stable case reported
  `[1.00, 1.00]` and `diff` inherited a noise floor of ±0.000 — meaning a one-case
  difference was announced as a REAL CHANGE. A 5-for-5 case now reports `[0.57, 1.00]`, in
  line with the rule of three, and `diff`'s floor at N=5 is ±0.217. Reported CI values
  change for every binary case; float scores keep the seeded bootstrap. `wilson_ci` is
  exported.

### Added
- `examples/codeqa/`: a code-comprehension suite generated from real repositories, with
  ground truth taken from the Python AST, plus its recorded cassette. Accuracy 0.993 at
  N=5 with 2 borderline cases, both asking for project-specific class names.
- A second GSM8K run against `gemini-3.1-flash-lite` and a worked `diff` between models.
- CI replays the codeqa suite alongside the other two.

## [1.0.0] - 2026-09-18

First stable release: the cassette format, the sealed-record schema and the CLI surface
are now covered by semantic versioning, and a breaking change to any of them means 2.0.

### Added
- **Ledger signing.** `evalseal keygen`, `evalseal sign`, and `evalseal verify --public-key`
  (or `--signed`). Ed25519 over the chain head, which commits to every earlier record, so a
  third party holding the public key can tell a receipt came from you and has not been
  altered. `run --sign-key` signs as part of the run. Signing a ledger that does not verify
  is refused.
- **Suite files.** `evalseal run --suite suite.json` supplies dataset, target, scorer and run
  settings in one place; paths resolve relative to the suite file, explicit flags override
  it, and unknown keys are an error.
- **`answer_match` scorer** for benchmarks with ground truth: extracts the final answer
  (`####`, `\boxed{}`, "the answer is <number>", or the last number) and compares it
  numerically when both sides are numbers.
- **A second worked example, `examples/gsm8k/`**, built from the GSM8K test split by a
  seeded script, with its own recorded cassette. Result: accuracy 0.975 with all 40 cases
  STABLE, against 5 of 20 flipping in the judge-graded suite.

### Fixed
- Answer extraction no longer captures prose containing the word "answer"; the phrase form
  now requires a number.

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

[1.1.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.1.0
[1.0.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.0.0
[0.3.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v0.3.0
[0.2.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v0.2.0
[0.1.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v0.1.0
