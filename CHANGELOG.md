# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The sealed record format carries its own `schema_version`; a record written by an
older version still verifies.

## [Unreleased]

Targeted at **2.1.0**. It was planned as a 2.0.2 hygiene release, but it adds commands
(`anchor`, `anchor-verify`) and a flag (`gate --expect-evaluator`), and semver reserves a
patch release for fixes. Nothing here removes or changes the meaning of an existing
command, exit code or JSON field, apart from the sealed-field fix under *Fixed* below.

### Fixed

- **LLM-judge suites were reported "not comparable" across target models, and their
  receipts were not reproducible.** The sealed `judge_prompt_hash` was the hash of the
  last judge prompt sent, which embeds the last case's prompt and the target's response.
  It therefore changed whenever the target answered differently, and under concurrency
  with whichever case happened to finish last: three concurrency-8 replays of one
  cassette sealed three different values. Through the evaluator fingerprint, `diff` called
  "same grading, different model" incomparable for every judge suite - the comparison a
  benchmark exists to make - and could call two replays of one cassette incomparable. The
  receipt now seals the judge prompt **template**: the rubric and instruction wrapper, with
  the case as `{prompt}` / `{response}` placeholders. One function builds both the prompt
  sent and the sealed template, and the bytes sent to the judge are unchanged, so every
  committed judge cassette still replays.
- **`gate --expect-config` claimed non-comparability it could not know.** A mismatch
  printed "Not directly comparable: evaluator configuration changed". The gate holds a
  hash, not the pinned record, and the config fingerprint includes the target model, so
  the claim was false whenever only the target changed. It now reports a changed
  configuration and points at `--expect-evaluator`.
- **The docs contradicted each other and the code on fingerprints.** The README described
  `--expect-config` as comparing "the evaluator fingerprint" while listing the target
  model in it; DESIGN.md said `diff` compares `config_fingerprint`, false since 1.4; the
  CLI help called the config pin an "evaluator config fingerprint". All now give one
  account: the evaluator fingerprint answers *are these runs comparable?*, the config
  fingerprint *what exactly ran?*
- **Cassette saves were not atomic.** Each save truncated the file and then refilled it,
  so an interrupt in between lost every recorded response, not just the latest. Saves now
  write a temp file, fsync it, and rename it over the original.
- **`evalseal power` took minutes at realistic suite sizes**, scanning every odd repeat
  count to 201. The scan stops once power stops improving; 150 items went from minutes to
  under two seconds.
- **`.coverage 2` was committed.** `.gitignore` matched `.coverage.*` but not the
  `name 2` copies macOS and iCloud create. It now matches `.coverage*` and the receipts
  EvalSeal generates.

### Added

- **`evalseal anchor` and `evalseal anchor-verify`.** A local anchor binds a receipt to
  the ledger it was sealed into, the ledger's head and length, the signature (bytes,
  public key and key fingerprint) and any extra artifacts, by hash, in one JSON file. A
  third party can re-check it without the author's machine; a signed anchor verifies its
  signature from the anchor alone. Verification fails if the receipt, anchor, ledger
  history or a bound artifact changed, and reports anything it could not check as not
  checked rather than passed. Everything except `created_at` is deterministic. It is
  **not** an external timestamp: `created_at` is the local clock and `external_proof` is
  null. [docs/external-anchoring.md](docs/external-anchoring.md) is the design for
  independent anchoring.
- **`gate --expect-evaluator`**, pinning the grading setup from the command line, as
  policy files could already with `run.expect_evaluator`.
- **Both fingerprints in `report --json` and `gate --json`**, so a pin can be copied from
  a real record.
- **`evaluator_changes` in `diff --json`**: the config changes that actually decide
  comparability. `config_changes` also lists a new target model, which is not a reason two
  runs are incomparable.
- **The PR receipt workflow**, [docs/pr-receipt-workflow.md](docs/pr-receipt-workflow.md):
  replay with no key, verify, gate against a policy, compare with a baseline, anchor,
  upload, and a step summary that puts comparability before any score delta. A test
  executes the workflow's own steps in four scenarios.
- **ROADMAP.md**, **docs/external-anchoring.md**, **docs/integrations.md**.
- **`examples/anthropic/RECORDING.md`** and a 10-problem suite, so the missing live
  Anthropic recording can be made without guessing. No cassette was recorded: no key was
  available, and the repo does not claim one.
- `ledger.check_record_hash`, the one definition of "does this record's hash hold" that
  `verify` and `anchor` now share; `signing.public_key_fingerprint`.

### Changed

- **Schema 1.3.** No field is added; `scorer.judge_prompt_hash` changes meaning, from an
  instantiated prompt to the template. Records sealed under 1.2 still verify. A 1.2 judge
  record and a 1.3 one disagree on this hash for that reason alone, and `diff` now shows
  the schema version among its other differences so the cause is visible.
- **`--store-judge-prompt` now stores the template**, not an instantiated prompt, so it no
  longer embeds case text or a response. It still carries the rubric verbatim, so it
  stays opt-in.

### Known gaps, now documented

- Neither fingerprint covers the judge's endpoint, which is sealed as provenance only.
- `max_tokens`, sent by the Anthropic adapter, is dropped from the sealed parameters.
- A receipt does not seal its cassette's hash, so responses are bound only through
  `anchor --artifact`.

All three change content hashes or pinned fingerprints to fix, and are scheduled in
ROADMAP.md with a fingerprint-scheme version rather than folded in here.

### Recorded evidence

- **A pre-registered comparison, reported inconclusive.** `gemini-2.5-flash` against
  `gemma-4-26b-a4b-it` on 150 GSM8K problems, five repeats each, designed and its power
  published before any response was recorded. Accuracy 0.9760 against 0.9613: a 1.5-point
  gap where the design was powered for 4, with two discordant items that cancel, so
  p = 1 and the verdict is `inconclusive`. The same receipts show gemma flipping 9 items
  to flash's 1, a mean flip rate of 2.00% against 0.27% - a difference no test was needed
  to see and that a single score from each would have hidden. Both cassettes are
  committed and `tests/test_gsm8k_compare.py` asserts every published number.
  See `examples/gsm8k_compare/RESULTS.md`.

## [2.0.1] - 2026-09-21

### Added
- **`evalseal decompose --concurrency`**, which `run` has always had. Recording the
  decomposition behind the README's variance claim took about fifty minutes serially for
  320 calls. Width changes the wall clock and nothing else: cassette entries are keyed by
  (request, repeat) rather than arrival order and results are assembled by position, which
  the tests assert by replaying the committed cassettes at concurrency 1, 4 and 16 and
  requiring identical verdicts.

### Note
- The judge-only arm parallelises across cases only. Within a case the target call must
  finish before any judging of it starts, or the judge would grade an empty string; a test
  pins that ordering at concurrency 8.

## [2.0.0] - 2026-09-21

Breaking. The statistics changed, and with them the vocabulary `diff` uses, the
stability classes, and two exported names. Sealed records written by earlier versions
still verify: `verify` re-hashes stored content and does not re-derive stability.

### Fixed

- **`diff` used a per-item statistic as a suite-level threshold.** It compared the
  difference in mean score against the widest per-case Wilson half-width and called that
  a "noise floor". At N=5 a perfect item spans [0.57, 1.00], so the bar sat at 0.217,
  while a mean over 40 items is far better determined than any single item in it. Almost
  every real shift came back "within noise", which reads as reassurance. A recorded
  example: 8 of 40 items regressing is a delta of 0.20 with p = 0.0078, and the old rule
  called it noise.
- **The comparison ignored pairing.** Both runs cover the same items, so the two runs are
  not independent samples.
- **Fixed flip-rate thresholds could not mean what they looked like.** `BORDERLINE_MAX_FLIP
  = 0.20` reads as a 20% tolerance, but at N=5 the only reachable flip rates are 0, 0.2
  and 0.4, so the cutoff encoded the single outcome "one of five runs disagreed".
- **Three unanimous runs were reported STABLE.** 3/3 gives a Wilson interval of
  [0.44, 1.00], which is also what you would see from an item that passes 70% of the
  time. The coarse label now agrees with the `insufficient_runs` label the finer
  taxonomy already gave it.

### Added

- **A paired comparison in `diff`.** Binary scorers get an exact McNemar test on the
  items that changed majority verdict; float scorers get a paired permutation test. Both
  get a 95% CI for the difference in mean score from a cluster bootstrap that resamples
  whole items and carries all of an item's repeats together. Output is delta, CI,
  p-value, discordant-item counts, and a verdict.
- **`evalseal power`.** Simulates how many items or repeats it would take to detect a
  difference. It reports the hard floor first: at alpha 0.05 at least six items must
  change verdict in the same direction before any paired result can be significant,
  because 2 x 0.5^6 = 0.031 and 2 x 0.5^5 = 0.063. Item models are explicit
  (`deterministic`, `bernoulli`, or `--from-receipt` using a real run's per-item rates),
  and the closed form for the deterministic case cross-checks the simulator.
- **`evalseal decompose`.** Two arms over one suite, holding the task fixed: judge a
  single fixed target response N times, versus sample the target N times and judge each
  once. This separates judge variance from target variance, which comparing two
  different suites cannot do. Each arm records to its own cassette.
- **A reported disagreement between the two criteria.** When the bootstrap interval
  excludes zero but McNemar does not agree, `diff` says so: the shift is within items
  rather than across them, and more repeats would make it harder to see, not easier.

### Changed

- **The verdict vocabulary.** `REAL CHANGE` and `within noise` are gone. A comparison is
  `regression`, `improvement`, or `inconclusive`, and inconclusive states in words that
  it is not a claim the two runs are the same.
- **Stability classes follow the Wilson interval of the pass proportion.** UNSTABLE when
  it contains 0.5, STABLE when it excludes 0.5 and every run agreed, BORDERLINE in
  between. A consequence worth stating: **BORDERLINE cannot occur at N=5**, because 4/5
  gives [0.38, 0.96] and straddles 0.5. It becomes reachable at larger N, where 18/20
  gives [0.70, 0.97].
- **`drift.max_score_regression` in a policy now requires the paired test to support a
  regression** as well as the drop exceeding the budget. The old allowance added a
  per-case half-width of 0.217 to the budget, which passed almost anything.
- **Renamed:** `noise_floor()` is now `per_case_halfwidth()`, and the `noise_floor` field
  in `diff --json` is now `score.per_case_halfwidth`. The number is unchanged; the name
  now says what it measures.
- **`classify_stability(rate)` is now `classify_stability(successes, n_runs, flip_count)`.**
- `diff --json` gains a `paired` object with every statistic above.

### Recorded evidence

- **The judge-versus-model claim was confounded and is replaced.** The README argued from
  a judge-graded suite flipping on 5 of 20 cases and an arithmetic-graded suite flipping
  on none of 40 that the variance came from the judge. Those suites differ in grader *and*
  in task, so the comparison cannot separate the two. `evalseal decompose`, recorded live
  on 2026-09-21 against `gemini-2.5-flash` over `borderline_judge` and committed as
  `tests/cassettes/decompose_borderline.{judge_only,full}.json`, runs the experiment that
  can: judging one frozen target response five times gives a mean flip rate of **6.0%**
  across 3 of 20 cases, against **5.0%** across 3 of 20 for the full pipeline. The judge
  grading an unchanging string disagrees with itself about as often as the whole pipeline
  does. The README now also states what that does not show - the two arms are independent
  samples so their 6.0/5.0 gap is noise, judge_only measures the judge at one particular
  response, and three flipping cases is below the six-item significance floor.
- **`borderline_judge` reclassifies from 15 stable / 2 borderline / 3 unstable to
  15 / 0 / 5.** The two cases that dissented once in five are UNSTABLE under the Wilson
  rule. Mean score, verdict sequences and per-case intervals are unchanged.
- `gsm8k` (0.975, 40 STABLE) and `codeqa` (1.000, 60 STABLE) are unchanged.
- The README's incomparable-`diff` example was regenerated. The old one came from two
  pre-correction `codeqa` runs whose cassettes no longer exist, so it could not be
  reproduced; it is replaced by a diff between two committed suites.

### Compatibility

- 0.1.x cassettes remain unconvertible, for the reason already documented: they record
  arrival order, not which repeat produced a response.
- Older-schema ledgers still verify and are still reported as an older schema rather than
  as tampering.
- Stability strings are sealed at write time, so a record sealed before 2.0 keeps the
  classification it was sealed with. Re-running a suite reclassifies it.

## [1.7.0] - 2026-09-21

### Added
- **A native Anthropic target**, not an OpenAI shim. `provider: "anthropic"` in a target
  config speaks the Messages API directly: `max_tokens` required rather than optional,
  the system prompt as a top-level field rather than a message, a reply that is a list
  of content blocks rather than a string, no `system_fingerprint`, and HTTP 529 for
  overload. A shim papers over each of those, and a receipt that records what a shim
  assumed is a receipt about the shim. Built on httpx, so the cassette records the raw
  response envelope and the base install gains no dependency.
- **`judge_provider`** does the same for an LLM judge, which is now configured through
  the same code path as a target rather than a second, smaller vocabulary.
- **Truncation is reported.** A reply cut off at the token limit is an absent answer,
  not a wrong one, and scoring it naively turns a configuration mistake into a finding
  about the model. `TargetResponse.truncated` carries it and the run warns:
  `TARGET RESPONSE TRUNCATED: 3 of 100 call(s) hit the token limit`. This covers the
  OpenAI shape too, where `finish_reason: "length"` means the same thing.
- **HTTP 529 is retried.** Anthropic signals overload with 529, which is in no standard
  retry set, so an overloaded provider would have failed the run outright.
- `AnthropicTarget` and `extract_text` are exported, and `api.anthropic.com` counts as a
  canonical endpoint rather than raising a proxy warning.
- An example config and guide in `examples/anthropic/`.

### Changed
- **One retry loop instead of two.** `post_with_retries` is shared by both adapters. A
  second copy would drift, and the copy that drifted would be the provider with the
  fewest tests pointed at it.
- `system_fingerprint` is reported as `None` for Anthropic and stays that way. Deriving
  one from the model name would fake a pin that does not exist, and `diff` would then
  claim two runs shared a backend on no evidence.

### Note
- The adapter is covered by 23 tests against the documented wire format but is **not yet
  backed by a recorded live run**, unlike every other suite in this repo. Recording one
  needs `ANTHROPIC_API_KEY`; see `examples/anthropic/`.

## [1.6.0] - 2026-09-21

### Added
- **Policy files.** `evalseal gate --policy evalseal.yml` moves thresholds out of CI
  shell lines and into a file that lives in the repo, so loosening one shows up in review
  and `git log` says who did it. Rules cover the run (`min_score`, `max_flip_rate`,
  `max_unstable_cases`, `fail_on`, `verify_ledger`, `expect_evaluator`, `expect_config`),
  individual cases (`critical`, per-case `min_score`), and drift against a baseline.
- **Drift rules in the gate.** `drift.baseline` points at a receipt or ledger and the gate
  can then require the two runs be comparable, cap a score regression, forbid a case from
  newly starting to flip, or forbid a case from quietly leaving the dataset. The
  regression allowance is the stated budget *plus* the noise floor of the two runs, so a
  drop neither run can distinguish from noise never fails a build.
- **Every rule that ran is printed, passed or failed.** A gate that speaks up only on
  failure cannot be told apart from a gate that checked nothing, which is what a policy
  becomes after a year in a repo. `--json` emits the same checks for a pipeline.
- **`Policy`, `PolicyResult`, `Check`, `load_policy` and `evaluate`** are exported, so a
  harness can apply the same rules the CLI applies.
- **`DiffResult.evaluator_changes`**, the subset of config changes that actually decide
  comparability, beside the `EVALUATOR_FIELDS` the fingerprint is built from.
- An example policy in `examples/evalseal.yml`, and a `[yaml]` extra. YAML policies need
  `pip install 'evalseal[yaml]'`; JSON policies need nothing extra.

### Fixed
- **Case ids were eaten by the console.** Rich reads `cases.min_score[gsm001]` as markup
  and printed `cases.min_score`, dropping exactly the identifier a reader needs. Gate
  output is escaped now.
- **A misleading comparability message.** The drift check listed every changed config
  field, so a run that changed only the target model reported "evaluator changes: target
  model" while passing. It now names only fields that decide comparability, or says the
  grading setup matched.

### Changed
- **A policy that cannot be understood fails the build.** An unknown key such as
  `min_scor: 0.9` is refused with exit 2 rather than ignored, and a rule that cannot run -
  a missing drift baseline, a critical case the dataset no longer has - is a failure
  rather than a skip. The usual way a quality gate fails is not that it fails wrongly, it
  is that it silently checks nothing and the green build gets read as evidence.
- Policy rules and command-line flags are additive: a flag can tighten a checked-in
  policy, never silently loosen it.

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

[Unreleased]: https://github.com/patibandlavenkatamanideep/evalseal/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v2.0.1
[2.0.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v2.0.0
[1.7.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.7.0
[1.6.0]: https://github.com/patibandlavenkatamanideep/evalseal/releases/tag/v1.6.0
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
