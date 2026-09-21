# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The sealed record format carries its own `schema_version`; a record written by an
older version still verifies.

## [1.5.0] - 2026-09-21

### Added
- **A shareable HTML receipt.** `evalseal run --html receipt.html`,
  `evalseal report <receipt|index> --html`, and `evalseal diff A B --html` write a
  single self-contained page. No stylesheet, script, font or image is fetched, so a
  report archived as a CI artifact renders the same way a year later on a machine with
  no network.
- **The HTML is deterministic.** Nothing in it reads the clock or the environment; the
  only timestamp shown is the one sealed into the record. The same record produces
  byte-identical bytes, so the page can itself be hashed and attached to the chain.
- **Per-case verdict strips.** Each case gets one cell per repeat, in run order, marked
  where the verdict disagreed with the majority. `FPPFP` says a case flipped; the strip
  says when, which is the part that points at a cause.
- **`evalseal report` takes a receipt path or a ledger index** as a positional argument,
  the same tokens `diff` accepts. Without one it still uses the latest sealed record, so
  existing invocations are unchanged.
- **`to_html`, `diff_to_html`, `write_html` and `write_diff_html`** are exported from the
  package, along with `mean_flip_rate()` and `noise_floor()` - the two summary statistics
  every surface now shares, so a dashboard built on the library prints the same numbers
  the CLI does.

### Fixed
- **`evalseal diff 0 -1` did not work**, although the command's own `--help` and the
  README both gave it as an example. Click read the bare `-1` as an unknown option and
  refused the command; the documented workaround was a `--` separator. `diff` and
  `report` now accept negative ledger indices directly. A genuine unknown option is
  still an error.

### Changed
- Prompts, responses and the judge prompt are never written into an HTML page, only their
  hashes - including when the record was sealed with `--store-judge-prompt`. A receipt is
  meant to be forwarded, and a forwarded file that carries the dataset is a leak.
- `mean flip rate` and the noise floor had three separate implementations across `diff`,
  `report --json` and the reporting code. They now have one each.

## [1.4.0] - 2026-09-20

### Added
- **First-class drift comparison.** `evalseal diff` no longer just prints a mean delta. It
  reports, in this order: whether the two runs are comparable at all, the score change
  against the noise floor, the mean flip-rate change, and which individual cases started
  or stopped flipping. The case-level part is the actionable one - it names a question to
  go read.
- **Comparability is decided by an evaluator fingerprint**, not the whole config.
  `evaluator_fingerprint()` covers the grading side only: scorer type, rubric hash, judge
  prompt hash, judge model and parameters, and the dataset. It deliberately excludes the
  target model and its parameters, because comparing two target models is the point of a
  benchmark. Previously `diff` used `config_fingerprint()`, which includes the target, so
  the single most common comparison - model A versus model B on one suite - was reported
  as "not directly comparable". That was wrong.
- **`diff` accepts receipt files as well as ledger indices**, in any mix:
  `evalseal diff before.json after.json`, `evalseal diff --ledger runs.jsonl -- 0 -1`, or
  two different ledgers. `load_receipt()` reads either shape.
- **`evalseal diff --json`** emits the whole comparison as one object for a pipeline to
  assert on: `comparable`, `score`, `flip_rate`, `unstable_cases`, `cases`,
  `config_changes`, `provenance`.
- **`diff_records`, `load_receipt`, `render_diff` and `DiffResult`** are exported from the
  package, so the comparison is available as a library with the same rules the CLI uses.

### Fixed
- **A pre-1.2 receipt reported nothing unstable.** The diff keyed instability on
  `flip_count`, a field added in schema 1.2 that deserializes to 0 on older records - so
  comparing an old run against a new one silently showed zero flips on the old side. It
  now keys on `flip_rate`, which every schema has.
- **Flip-rate rounding made a real move look like none.** At whole percents a 1.3% to 0.7%
  change rendered as "1% 1% -1%". Flip rates now print to one decimal.
- **`load_receipt` mis-read pretty-printed receipts.** Taking the last line of the file
  works for a ledger and yields a lone `}` for a `report.json`. It now parses the whole
  file first and only falls back to JSONL.

### Changed
- `diff` still always exits 0. It reports; it does not gate. Gating stays `gate`'s job, so
  a comparison in CI cannot fail a build by accident.

## [1.3.2] - 2026-09-20

### Fixed
- **`evalseal run` crashed on Windows.** Artifacts were written with `Path.write_text`,
  which uses the *locale* encoding — cp1252 on a Windows console, which cannot encode the
  `·` and `⚠` the report contains. The run exited 1 with `UnicodeEncodeError: 'charmap'`.
  Every text artifact EvalSeal reads or writes is now explicitly UTF-8. Found by the
  Windows CI job added in 1.3.1, on its first run.
- **Key permissions on Windows.** `chmod(0o600)` is a no-op there; the code now skips it
  and says why (NTFS inherits the parent directory's ACL), and the test skips rather than
  asserting something untrue about the platform.

### Changed
- **Releases are gated on Windows and macOS, not Linux alone.** 1.3.1 published the
  Unicode bug above precisely because the release workflow only ran `pytest` on Ubuntu.
  Publishing now waits for the test suite to pass on all three platforms.

## [1.3.1] - 2026-09-20

### Added
- **Ledgers sealed by older versions verify again.** A record written under schema 1.0 or
  1.1 is re-hashed the way its own schema would have hashed it, so upgrading no longer
  strands an existing ledger. `verify` says which older schemas it accepted. Tampering
  with a legacy record is still caught — there is a test that edits one and expects
  `TAMPER DETECTED`. The fixture was produced by running 1.2.0 for real, not hand-written.
- **Windows and macOS in CI.** The ledger lock uses `msvcrt` on Windows and `fcntl`
  elsewhere, so the concurrency guarantee was previously only exercised on Linux.
- **Tamper test suite** covering a changed score, a changed judge prompt hash, a broken
  `prev_hash`, duplicated `prev_hash` (siblings), a deleted record, and a re-hashed forgery
  that still breaks its successor.
- **`docs/github-action-example.yml`**, a copy-pasteable workflow that replays a cassette,
  verifies the ledger, gates on thresholds and uploads the per-case report.
- A console quickstart in the README showing the per-case table, verify, and a failing gate.

## [1.3.0] - 2026-09-20

### Fixed
- **Ledger appends are now linear under concurrent writers.** Two `evalseal run`
  processes could read the same head hash, both validate it, and both append a record
  claiming the same predecessor — leaving sibling records that weaken the chain. The head
  read, validation, seal, append and `fsync` now happen inside one advisory file lock
  (`fcntl` on Unix, `msvcrt` on Windows) held on `<ledger>.lock`. `verify` reports siblings
  explicitly rather than as a generic broken link. **Local filesystems only** — this is not
  distributed consensus, and the guarantee weakens on NFS or SMB.

### Added
- **Per-case verdict distribution.** Each case now records its verdict sequence, pass /
  fail / other counts, flip count, and a finer `stability_label` — `stable_pass`,
  `stable_fail`, `unstable`, or `insufficient_runs` when N < 5. `run --show-cases`
  (with `--unstable-only`) prints the table; `report --json` emits it machine-readably
  with a provenance block. The existing `stability` field is unchanged.
- **The receipt seals the evaluator configuration**, not only the score: judge prompt hash,
  rubric hash, scorer type, judge model and parameters, dataset path and case ids, suite
  name and hash, EvalSeal version, git commit and dirty flag, Python version and platform.
  `--store-judge-prompt` additionally seals the prompt verbatim; it is off by default
  because the prompt embeds case text.
- **`evalseal gate`** applies CI thresholds to a sealed record and exits 3 on violation:
  `--min-score`, `--max-flip-rate`, `--critical` (case ids that must not flip),
  `--expect-config` (evaluator fingerprint), and ledger verification by default.
- **`evalseal report`** shows per-case detail for any sealed record, `--json` included.
- `diff` now says **"Not directly comparable: evaluator configuration changed"** and names
  what differed, because a changed rubric moves the score without the model changing.

### Changed
- Records are sealed under schema **1.2**. Ledgers sealed by 1.2.x verify with the version
  that wrote them; `verify` names the older schema rather than alleging tampering.
- Flip rate is documented explicitly as `non-majority verdicts / total runs`, not
  transitions, so it does not depend on the order concurrent runs completed in.

## [1.2.0] - 2026-09-18

### Added
- `run --only a,b` runs just the named cases, for re-examining a flip at higher N.

### Fixed
- `answer_match` now strips surrounding backticks and quotes before comparing. A model
  answering `` `RetrievalResult` `` was being scored wrong for markdown decoration, which
  manufactured variance that did not exist: re-running the two affected cases at N=20
  showed 20-for-20 correct answers. The codeqa suite goes from 0.993 with 2 borderline
  cases to 1.000 with all 60 stable, and the README claim that the model was unreliable on
  project-specific class names was wrong and has been corrected. Normalization touches
  presentation only — a different identifier still scores zero, and the judge-graded suite
  still flips on 5 of 20 cases.

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

[1.5.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.5.0
[1.4.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.4.0
[1.3.2]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.3.2
[1.3.1]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.3.1
[1.3.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.3.0
[1.2.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.2.0
[1.1.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.1.0
[1.0.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.0.0
[0.3.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v0.3.0
[0.2.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v0.2.0
[0.1.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v0.1.0
