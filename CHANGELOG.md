# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The sealed record format carries its own `schema_version`; a record written by an
older version still verifies.

## [Unreleased]

### Added

- **`evalseal import`**: reads another tool's export into a sealed receipt. One
  adapter, promptfoo, written against a real export from **0.125.1**
  (`results.version: 3`) rather than an invented shape. The receipt binds the export's
  digest, so `verify --artifacts` can show the retained file still matches what was
  sealed - and nothing more than that. See
  [the quickstart](examples/integrations/promptfoo/README.md) and
  [the field mapping](docs/importers/promptfoo.md), which has a column for what each
  field does **not** support.

  What it refuses to invent: `origin` is `imported` on every call; `served_model` stays
  null because promptfoo does not record it; sampling parameters are `unsupported`
  rather than `inherited`; the export's timestamps are kept as claims. `scorer.impl_hash`
  stays null, so **an imported receipt is never comparable** - the grading code belongs
  to another tool. Two imports missing that same field are not thereby comparable, and
  `diff` refuses explicitly rather than letting two absences hash to one fingerprint.

  Schema 1.9 adds `manifest.imported`, whose `gaps` list names every field the export
  could not fill.

#### Earlier in this unreleased cycle

- **Four contract documents**, each one a page a reviewer is told to act on:
  [docs/statistical-contract.md](docs/statistical-contract.md) (estimands, assumptions,
  coverage, decision rules), [docs/verification-policy.md](docs/verification-policy.md)
  (five assurance levels, trust roots, fail-closed rules),
  [docs/compatibility.md](docs/compatibility.md) (package/schema/fingerprint/cassette
  matrix and the 2.3 upgrade notes) and
  [docs/operations.md](docs/operations.md) (limits, key handling, recovery, retention,
  troubleshooting). Every number in the last of those is measured by
  `scripts/measure_limits.py` on a machine it names, not asserted as an SLA.

- **`evalseal recover`**: moves an interrupted append aside and keeps the complete
  history. The partial bytes go to a sidecar and are fsynced before the ledger is
  truncated, under the ledger lock.

- **Bounded transport** (`src/evalseal/transport.py`): `--max-response-bytes` (8 MiB by
  default) and `--deadline` on `run`, as suite keys too, for provider and
  timestamp-authority traffic alike.

- **A packaging lane and a minimum-dependency lane.** `scripts/wheel_smoke.py` installs
  the built wheel outside the repository and exercises the CLI from a clean environment;
  CI runs it on Linux, macOS and Windows. A second lane pins every declared dependency
  to exactly its lower bound and runs the suite, so the bounds are claims rather than
  guesses.

#### Earlier: scoring, attribution and statistics

- **[docs/metrics.md](docs/metrics.md): what every number means.** One row per
  user-facing metric and verdict, with its unit, denominator, treatment of ties and
  missing observations, the question it answers and whether higher or lower is better -
  binary, continuous and unscorable alike. It was written before any of the arithmetic
  below was changed, and used to review both the implementation and PR #12 rather than
  assuming either was right. Seven rows disagreed with the code.

- **`evalseal evidence`**: how precisely the repeats you already have pin down each
  case's behaviour, with the interval, a label saying whether a magnitude may be quoted
  at all, and how many more repeats a target margin would take. Ported from PR #12 by
  behaviour - see [docs/pr-12-disposition.md](docs/pr-12-disposition.md) - with three of
  its defects fixed first. `docs/uncertainty.md` answers the questions it raises.

- **Ungraded observations are counted and gateable.** `results[].unscorable_count`,
  `aggregate.n_unscorable` and `aggregate.n_observations` (schema 1.8), with
  `run.max_unscorable`, `run.max_unscorable_fraction` and
  `run.require_every_case_scored`. All default to off.

- **How many independent draws are behind a number.** `execution.calls[].source_key`
  (schema 1.8) names the cassette entry that served each call, which is the only
  evidence that separates two independently recorded draws that happened to agree from
  one recorded draw counted twice. `evalseal report --json` carries the audit and
  `run.require_independent_repeats` gates on it; a receipt that cannot answer the
  question reports `unknown` and fails that rule rather than being assumed independent.

- **`drift.regression_budget_policy`**: `require_evidence_within_budget` passes only
  when the interval rules out a drop larger than the budget, so an inconclusive
  comparison fails. The default, `fail_on_evidence_of_regression`, is the old behaviour.

- **`cannot_isolate`**, a first-class `drift` label for when the receipts admit more
  than one explanation, and `drift.grounds`, which states what the label rests on.

### Fixed

- **A case with no graded repeat rendered as `FFF`.** The verdict strip fell back to
  the scores when a case had no verdicts, so three rows the provider errored on read as
  three graded failures - the opposite of what happened. An ungraded repeat is `-` now.

- **An imported receipt claimed independent draws.** The independence audit counted
  distinct rows, and promptfoo caches by default without marking per row which were
  served from cache, so a row count is not a draw count. It reports `unknown` for any
  import, on the same page that warns caching was on.

- **`evalseal diff` printed that two runs were not comparable without printing why.**
  The reason was in `--json` only; the human output showed a table of changed hashes.
  For an import that meant a changed suite digest was shown while the actual answer -
  the grading code is another tool's and cannot be fingerprinted - went unsaid.

#### Earlier in this unreleased cycle

- **An unbounded response body.** Against a loopback server returning 200 MB of JSON,
  and the same body gzipped to about 200 KB on the wire:

  ```
  a 200 MB JSON body  -> accepted 210 MB of content  (peak traced 630 MB)
  a gzip bomb         -> accepted 210 MB of content  (peak traced 629 MB)
  ```

  Neither is a plausible chat completion; both are what a captive portal, a
  misconfigured `--base-url` or a proxy's error page hands you. Bodies are now read in
  chunks under a limit on **decoded** bytes, and inflated under `zlib`'s own
  `max_length`, because httpx decodes a whole raw chunk before yielding any of it and at
  a 1000:1 ratio one 64 KiB read expands to 64 MB — which left the same bomb peaking at
  270 MB even with the limit in place. It now peaks at 17 MB. The TSA backend read
  `response.content` the same way, for a token that is a few kilobytes.

- **A grading pattern could outlast the evaluation.** `RegexScorer('^(a+)+$')` took
  **25.12 s on 31 characters**, doubling with each one after, from a scorer config file
  that in a pull-request workflow can come from a fork. Nested unbounded quantifiers are
  refused before anything runs; `patterns.py` states what that does not cover.

- **A private signing key was created world-readable.** `write_bytes` honours the umask,
  so the file existed at `0644` until the `chmod(0o600)` landed. It is created with
  `O_CREAT|O_EXCL|O_NOFOLLOW` and mode `0600` now — which also refuses a symlink at the
  key's path (a **dangling** one passed the old `exists()` check and the key was created
  at the link's target) and makes creation the existence check, so check-then-write has
  no window.

- **The public key destination was replaced silently.** A verifier pinned to the old key
  would see every signature as untrusted with no record of which key changed.
  `--overwrite-public` is the explicit way to rotate.

- **An interrupted append made the whole ledger unreadable.** One partial final line and
  `load_all` raised *"line 2 is not valid JSON"*, so `verify`, `gate`, `report`, `diff`
  and `sign` all failed and none of the intact history could be read.
  `IncompleteFinalRecord` names the condition, says how many records are fine, and names
  `evalseal recover`.

- **`sign_head` did not hold the ledger lock.** An append between reading the head and
  writing the signature left a valid signature over a head that was no longer the head,
  while the newest record was unsigned and nothing said so. The signature line is also
  fsynced inside the lock.

- **CI went green on a failed anchor verification.** `anchor-verify`'s exit code was
  printed in the step summary as "FAILED" and then ignored. The copied workflow now fails
  for it, and the anchor is created **before** the gate — a pre-registration can require
  one, and `gate --prereg` can only check a file that exists, so `require_anchor: true`
  used to fail for an anchor the job was about to create.

- **A configured-but-missing contract was skipped in silence.** `if [ -f "$PREREG" ]`
  left the gate running without it, so deleting the pre-registration in a pull request
  removed the contract and the check stayed green. `REQUIRE_PREREG`, `REQUIRE_BASELINE`
  and `REQUIRE_ANCHOR` decide what is required, checked before the replay.

- **The release did not depend on lint, audit or packaging.** `build` needed only the
  cross-platform tests, while ruff, mypy and `pip-audit` lived in a workflow a tag push
  does not wait for.

- **Concurrency had no ceiling.** `"concurrency": 1000` in a suite file would have
  started a thousand threads and read a thousand bodies at once. Capped at 64, refused
  with the arithmetic.

#### Earlier: scoring, attribution and statistics

- **A grader that could not read a reply graded it anyway.** `extract_answer` ended with
  "otherwise, the last number anywhere in the reply", so a refusal that restated the
  problem scored a PASS:

  ```
  genuine answer 30      -> extracted '30'  score 1.0
  refusal, no answer     -> extracted '30'  score 1.0
  genuine wrong answer   -> extracted '28'  score 0.0
  ```

  The supported answer format is now declared (`####`, `\boxed{}`, an "answer is"
  phrase, or the answer alone on the final line, whichever appears **last** in the
  reply), and anything outside it is unscorable rather than scored on a guess. A reply
  truncated at the token limit is unscorable too, and `parse_verdict` returns `None`
  instead of mapping an empty reply, an apology and `ERROR: 503` all to a graded FAIL.

  Precedence by position rather than by marker class **changes the frozen Gemma/Flash
  comparison**: 49 of its 1500 recorded replies are read differently, and six of gemma's
  nine "unstable" items were the grader reading a different number out of a reply the
  model had not changed. `examples/gsm8k_compare/RESULTS.md` carries both readings. The
  recordings are byte-identical; the pre-registered verdict is still inconclusive.

- **`exact_mcnemar(1000, 1000)` raised `OverflowError: int too large to convert to
  float`** instead of returning 1.0, taking the gate down with it on any suite with a
  large number of discordant items each way.

- **Every confidence interval was labelled "95% CI"** whatever `alpha` was in force, so
  `alpha=0.20` printed an 80% interval as a 95% one.

- **A 0.600 drop against a 0.050 budget passed the gate.** Five items cannot reach
  significance - the six-item floor - and the rule needed both over-budget *and*
  significance to fail, so the only state it could distinguish was "evidence of a
  regression". The three states are now named in the detail, and the strict policy above
  asks the other question.

- **`judge_variance` was returned whenever the target model *name* matched.** Two runs
  of one model at temperature 0.0 and 1.0 read as "the target model is unchanged, yet
  ... came back with a different verdict". A changed sampling parameter, endpoint, API
  version or system prompt now gives `cannot_isolate`.

- **A continuous scorer's drift was invisible.** `verdict_changed` compared `pass_count`
  with `fail_count`, both 0 when there are no verdicts, so a run whose mean moved from
  0.10 to 0.90 reported `stable` - "No drift".

- **`decompose` called a clipped ratio a share of variance.** A judge_only flip rate of
  0.4 against a full rate of 0.2 was clipped from 2.0 to 1.0 and rendered as "The judge
  alone reproduces 100% of the suite's mean flip rate", so the one case where the arms
  contradict the apportioning model was reported as a perfect fit. With the judge
  flipping on every case and the full arm on none, it printed "Neither arm flipped, so
  there is no variance here to apportion" - false about the data on its own page.

- **`power` reported a figure for a test `diff` would not run.** It simulated the exact
  McNemar test over any receipt, including continuous-scored ones that `diff` tests with
  a paired permutation, and nothing said which metric the power was for.

- **A receipt with no hash was reported as "TAMPER DETECTED".** `RunRecord.hash` is
  empty until the ledger seals it, so that was a false accusation about a file nobody
  altered. There is an `unsealed` status now; it still fails, under its own name.

- **The cross-schema drift note said "sealed before schema 1.4"** when the evaluator
  fingerprint has needed 1.6 since scheme 3, so a pair sealed under 1.5 and 1.5 was told
  one of them predated 1.4.

- **`target seed` was missing from the config field table** while `config_fingerprint`
  hashed it, so a changed target seed showed up in `diff` only as "something changed
  that this summary does not list" and in `drift` not at all - and `drift` decides a
  cause from exactly those names.

- **`evalseal report` printed the per-case table and nothing else**, so a run whose
  grader failed on a third of its repeats looked like any other. It now leads with the
  mean, the counts, the ungraded share, the origin and the draw count, and prints the
  run's warnings.

#### Earlier: sealing the experiment

- **An input was used at the start of a run and hashed at the end.** Anything that
  rewrote the file in between - a `git checkout`, an editor save, another job
  regenerating a dataset - left a receipt describing bytes that produced no score:

  ```
  prompt actually used  : what the model was asked
  dataset bytes at start: sha256:99a1baea720ec4631
  dataset bytes at end  : sha256:15524b82f5e323980
  digest SEALED in the receipt: sha256:15524b82f5e323980
    -> sealed the bytes that ran  : False
    -> sealed bytes nothing ran on: True
  manifest.dataset.hash : sha256:99a1baea720ec4631 (computed at load)
  any warning about it  : []
  validate_record says  : nothing wrong
  ```

  The receipt held two different digests for one file and said nothing about either, and
  `verify --artifacts` afterwards reported `ok` - against the file nothing ran on. A
  check that passes because it is looking at the wrong bytes is worse than no check.

  New `evalseal.inputs` captures each input once: the bytes are read, hashed as read,
  and used from there. `Dataset.from_bytes` parses the captured bytes (hashing them the
  way `read_text` did, so a CRLF dataset keeps the digest it has always had), the suite's
  whole-file and section digests come from one read, and `validate_agent_artifact`
  accepts the captured bytes so the file that is validated is the file whose digest is
  sealed. A file that changes while the run is in flight becomes a named warning -
  `DATASET CHANGED DURING RUN: it is now ...; this run used ...` - plus a note on the
  artifact; the run keeps the bytes it read, and `verify --artifacts` reports `changed`
  afterwards, which is the truth. A file that only *appears* mid-run is reported too,
  and is not adopted: "there was no tool manifest" and "there was one, a moment too late
  to be the one that applied" are different facts.

- **Two writers on one cassette silently deleted each other's responses.** Every
  `Cassette` kept the whole entry table in memory and rewrote the whole file after each
  recorded response, so two writers each erased the other's entries and the last one
  won:

  ```
  after writer A : 1 entries
  after writer B : 1 entries
  A's entry survived: False
  A replays       : NO -> No cassette entry for repeat 0 of request 7f24486a3603
  ```

  Writer A's response was admitted, acknowledged, and then deleted by a process that
  never knew A existed - and the receipt A sealed could no longer be replayed. A
  cassette that may write now takes an exclusive advisory lock on `<cassette>.lock`, and
  a second writer is refused with `CassetteBusy`, the holding pid and what to do
  instead. Readers take no lock, so a CI job that only replays is never blockable by a
  recording run. `Cassette` is closeable and usable as a context manager. The decision,
  and the append-journal and SQLite alternatives it was chosen over, are in
  [docs/adr/0002-cassette-writer-strategy.md](docs/adr/0002-cassette-writer-strategy.md).

- **A receipt was bound to a cassette that later runs were free to rewrite.** Wrong in
  both directions at once:

  ```
  run A sealed cassette : sha256:57106b513ae0af6f6 (3 entries)
  run A mean            : 1.0
  run A verifies        : ['ok']

  after an honest append of 2 more repeats:
  run A verifies        : [('cassette', 'changed')]

  after a `fresh` re-draw on the same path:
  run A verifies        : [('cassette', 'changed')]
  replaying run A now   : mean 0.0 (run A's receipt says 1.0 )
  any immutable copy of run A's responses: ['run.json']
  ```

  An honest later run turned the earlier receipt red - a check that cries wolf on normal
  use is a check people switch off - and a `fresh` re-draw destroyed the responses behind
  it with no copy kept anywhere.

  Each run now **finalizes** the cassette entries it read or wrote into its output
  directory and binds that file into the receipt as the `cassette_snapshot` artifact. A
  snapshot is content-addressed, never overwritten, and is itself a cassette:
  `Cassette(snapshot, mode="replay")` reproduces the run it came from even after the
  live cassette has been re-drawn. The live cassette stays sealed as a new artifact kind,
  `mutable`, and `verify --artifacts` reports a mismatch on one as **`moved`** rather
  than `changed` - but only once the receipt's own snapshot confirms that every response
  it used is still present and byte-identical. A response that was *replaced* is
  `changed`, names how many, and still exits non-zero; a receipt with no snapshot (every
  receipt sealed before this release) reports a mismatch as `changed` exactly as before,
  because unknown belongs on the stronger side of that distinction.

- **`decompose` reported an unusable cassette as a traceback.** Its two cassettes were
  constructed *before* the `try` that caught `CassetteFormatError`, so the handler
  written for that error could never run: an incompatible cassette reached the user as
  an exception instead of the message. Found while wiring the new `CassetteBusy` through
  the same path.

- **Two runs in one working directory overwrote each other's `report.json`.** `run` now
  writes `runs/<timestamp>-<session>/report.json`, `report.md` and the cassette snapshot,
  one directory per run, named after the `session_id` the receipt carries. `--out-dir`
  chooses the directory and `--no-snapshot` skips the copy. `./report.json` and
  `./report.md` are still written as aliases for the latest run, so existing workflows
  are unaffected.

- **A setting a config asked for could be silently dropped.** `_build_target` read the
  keys it knew about with `cfg.get(...)` and ignored everything else:

  - `top_p` and `max_tokens` were **never transmitted** for an OpenAI-compatible
    target. The adapter had no fields for them. A config asking for `top_p: 0.1` ran
    unconstrained, and the receipt recorded `top_p: null` - which reads as an effective
    value rather than as "we never set one".
  - a misspelled key was indistinguishable from a decision. `tempreature: 0.9` beside
    `temperature: 0.0` was accepted, ignored, and the run used the value that happened
    to be spelled correctly, with nothing in the output mentioning the other key.
  - `params_source` was derived from `temperature` alone, so a run that set only
    `top_p` was reported as inheriting every provider default.

  New `evalseal.config` validates the target and scorer files against models that
  forbid unknown keys, and the error names the nearest legal key: an unsupported key
  now reports *"did you mean 'temperature'?"* and lists what is supported. Every field
  in those schemas is one the adapters actually transmit, and ranges are the provider's
  own - Anthropic's `temperature` stops at 1.0 where OpenAI's reaches 2.0, and
  `system` and `seed` belong to the provider that has them rather than to both.

  The judge's `judge_`-prefixed keys are translated into target vocabulary and
  validated by the same schema, so a judge can no longer accept a key a target would
  reject. Tests assert that every target and scorer config in this repository still
  loads, because a schema that rejects the project's own examples is the wrong schema.

- **About half of all timestamp requests encoded a negative nonce.** DER integers are
  signed, so a value whose top byte has the high bit set needs a leading zero byte.
  `_integer()` reserved that byte and the next line stripped it off, so `128` encoded
  as `-128`, `255` as `-1`, and **492 of 1000** random 64-bit nonces as a different,
  negative number. `build_timestamp_request` draws a random 64-bit nonce, so roughly
  half of every request EvalSeal ever sent was malformed: a strict authority rejects
  it, and a lenient one echoes whatever it parsed. Certificate serial numbers are large
  random integers and hit the same path.

- **The message imprint's algorithm was never read.** Any 32-byte `hashedMessage` was
  accepted and labelled `sha256:`, whatever the `AlgorithmIdentifier` said. A token
  declaring SHA-512 beside a 32-byte imprint was accepted and compared against the
  anchor's SHA-256 digest as though the authority had timestamped it; a genuine SHA-384
  or SHA-512 token, whose imprint is 48 or 64 bytes, was rejected as "not a readable
  TSTInfo". The OID now decides the algorithm, the length must be the one that
  algorithm produces, and an unrecognised OID fails closed naming it.

- **The signer certificate was whichever came first.** `certificates[0]` was assumed to
  be the signer. The certificate set is a DER `SET OF`, whose order is its sorted
  encoding rather than signer-first - and the recorded real token embeds **two**
  certificates, so this was live against the one real authority in the repository. The
  certificate is now selected by matching the `SignerIdentifier`, by issuer and serial
  or by subject key identifier.

- **`SignerInfo` was parsed by counting SEQUENCEs.** That works only for the
  `IssuerAndSerialNumber` form of `sid`. In the equally legal `[0] SubjectKeyIdentifier`
  form every index shifts and the *signature* algorithm was read as the digest
  algorithm. It is now parsed positionally per RFC 5652 section 5.3.

- **The `contentType` signed attribute was defined and never compared.** The constant
  existed in the module and nothing read it, so the signature did not commit to what
  kind of content it covered; only the unsigned `eContentType` field was checked.

- **No RFC 5035 signing-certificate binding.** `SignerIdentifier` is outside
  `signedAttrs` and so is not covered by the signature, which means that without this
  attribute nothing signed says which certificate signed the token. It is now required
  and its hash must match the certificate the signature was checked against. This gap
  was found by the new `openssl ts -verify` cross-check, not by any test written here -
  and the recorded freetsa token already carries the attribute, so requiring it is what
  a real authority does.

- **Signed attributes had no cardinality.** `attribute()` returned the first match and
  skipped an attribute carrying several values, so two verifiers could read the same
  token differently. Exactly one `contentType` and one `messageDigest`, each with one
  value, or the token fails.

- **Trailing material after the token was ignored**, letting one reader see a token and
  another see a token plus something else from the same bytes.

- **The request nonce was generated and discarded**, so a response could not be
  distinguished from a replay of an earlier token for the same digest. `submit` now
  keeps the nonce and requires it to be echoed, as RFC 3161 section 2.4.2 says it must
  be. Freshness is claimed only for a submission, never for a token read back from a
  file, and a test pins that no verification wording implies otherwise.

  [ADR 0001](docs/adr/0001-timestamp-verification-trust-model.md) records the trust
  model these rest on: one-hop issuer verification rather than path building, no
  revocation claim because none is checked, verifier-supplied roots only, the supported
  algorithms, and the parser profile. It also records an open decision - whether to
  replace the hand-written CMS parser with `asn1crypto` - which needs a maintainer call
  because it adds a required runtime dependency.

  The demo bundle's synthetic proof and its throwaway CA were regenerated offline,
  since the old token predates the signing-certificate requirement. Nothing outside
  `external_proofs` changed, and the anchor's subject digest excludes that field, so
  the rest of the bundle verifies unchanged.

- **A number that could not be a measurement was read as one.** The receipt models
  carried no constraints - no bounds, no finiteness, and no check that a summary agreed
  with the cases it summarises - so invalid data did not fail a gate, it *passed* one:

  - `aggregate.mean_score = 42.0` cleared `--min-score 0.9`. Nothing asked whether a
    mean score of 42 is a mean score.
  - `aggregate.n_cases = 999` on a two-case run with `mean_score = 1.0` cleared
    `--min-score 0.95`, and the chain verified: sealing attests that nobody changed a
    number afterwards, not that it was ever true.
  - a non-finite score was serialised as `null`, so the sealed receipt could never be
    loaded again - the measurement lost and every later command failing on a file that
    verified as nothing.
  - duplicate case ids and an empty case set were both accepted.

  JSON contributed two more. A duplicate member is resolved silently in favour of the
  last one, so a policy committed as `{"run": {"min_score": 0.9, "min_score": 0.0}}`
  was reviewed as `0.9` and enforced as `0.0` - no tampering and no broken hash
  required, because nothing hashes a policy file. And Python's `json` accepts `NaN`,
  `Infinity` and `-Infinity`, which are not JSON, and against which every comparison is
  false, so such a value clears any threshold it is tested against.

  New `evalseal.validation`:

  - `strict_json_loads()` refuses duplicate members and non-finite literals. Used by
    the policy loader, the pre-registration loader and `load_all`.
  - `validate_record()` returns a named `Problem` per invariant that does not hold:
    score, mean, flip-rate and interval bounds and finiteness, intervals the right way
    round, case means matching their scores, verdict counts matching the verdicts,
    a non-empty case set, distinct case ids, `n_cases` matching the cases,
    stability counts adding up, and the summary mean matching the case means.
  - `require_valid()` is called by `seal_and_append` *before* the lock, so an invalid
    record is never sealed.
  - `gate` reports `ledger.data_invariants`, kept separate from
    `ledger.record_verified` because the hash matching and the numbers being possible
    are different claims.
  - A malformed ledger is now an input error with the documented exit code and no
    traceback. `verify` used to let pydantic's `ValidationError` reach the user.

  Policy thresholds were already guarded - `ge=0.0, le=1.0` rejects `NaN`, `Infinity`
  and `42.0`, and `extra="forbid"` rejects a typo - and tests now pin that so it cannot
  be removed quietly.

- **A field the sealing schema did not have could be injected and read as sealed
  evidence.** A record sealed under an older schema is re-hashed with the fields that
  schema lacked stripped out, which is what keeps an old ledger verifiable. The same
  strip leaves those fields *unsealed*: they are absent from the bytes the hash commits
  to, so any value may be written into them without disturbing it.

  Writing `manifest.suite = {"hash": <forged>}` into a genuine 1.1 receipt left
  `check_record_hash` returning `("legacy", "1.1")`, `verify` printing *"Chain
  intact"*, and a pre-registration pinning that digest reporting **"suite digest
  matches"** - on a digest that was never sealed.

  Two related defects made it worse:

  - `_schema_tuple` returned `(0,)` for any version it could not parse, so a record
    claiming `schema_version: "nonsense"` was treated as predating every field and had
    the **whole** strip table applied before hashing: `manifest.suite`,
    `manifest.code`, `manifest.environment`, `manifest.artifacts`,
    `results[].verdicts`, `results[].pass_count` and `results[].stability_label` all
    injectable at once, with the chain still reported intact.
  - Support for a claimed schema was checked only as a *fallback*, after the hashes
    were compared, so a record claiming a version this build does not understand could
    still come back verified. `9.9` was reported as `("ok", "1.5")` - asserting it was
    sealed under today's schema.

  Fixed by quarantine rather than rejection, so old receipts keep working:

  - `ledger.quarantine_unsealed()` removes the fields the sealing schema did not cover
    and names what it removed. A genuine old receipt is untouched, because those
    fields are already at their defaults; a receipt with a value written into one comes
    back without it. The record still verifies - quarantine is not an accusation.
  - `verification.load_verified()` is the loading path for anything that decides
    something: it verifies the chain, establishes each record's sealing schema,
    quarantines unsealed fields before anything reads them, and reports per record.
    `gate` now loads through it, and adds two named checks, `ledger.record_verified`
    and `ledger.unsealed_fields`. The gate previously read `load_all` and verified the
    chain *after* every check had already used the record.
  - `schema_is_supported()` and `SUPPORTED_SCHEMAS` reject a version that is
    unparseable or newer than this build, before any hash is compared. Unknown in
    either direction is unsupported: a newer schema may have added fields this build
    cannot see, or changed what an existing one means - 1.3 added no field and only
    changed the meaning of `scorer.judge_prompt_hash`, so a format change need not be
    visible in the data at all.
  - `verify` now says when a record that verified carries unsealed fields. "Chain
    intact" was true and not the whole truth.

  Genuine 1.0 through 1.5 ledgers still verify, and a record whose schema label was
  changed without rewriting its content is still reported as such rather than as
  tampering.

- **`require_external_anchor` was satisfied by any non-empty list.** The strongest
  clause a contract can declare was evaluated as `bool(anchor["external_proofs"])`,
  which made it the easiest clause in the contract to satisfy. On the previous release
  all of these passed it:

  ```json
  [{"backend": "local", "proof": null}]   // the backend that attests nothing
  [{}]                                     // an empty object
  [{"backend": "notarius-maximus"}]        // a backend that does not exist
  ["no"]                                   // the string "no"
  ```

  Only a completely empty list failed, so any producer could satisfy a requirement for
  third-party attestation by writing one character into an anchor they wrote themselves.

  Reading the backend's `check()` instead would have produced a quieter false green:
  it returns `True` for an imprint-only token, because it answers "could this be
  re-checked at all?". A token whose signature was never verified - because the verifier
  supplied no trust root - is "ok" to that boolean. `Rfc3161Backend.verify()` now
  returns the state and `check()` wraps it, with its own boolean semantics unchanged.

  External evidence is now an `ExternalEvidence` result with five states, of which one
  satisfies the requirement:

  | state | satisfies it |
  |---|---|
  | `verified` - an approved backend checked the proof against a root the **verifier** supplied | yes |
  | `imprint_only` - the imprint matches; the signature was not checked | no |
  | `unsupported` - an unknown backend, a malformed entry, or `local`, which attests nothing | no |
  | `missing` - no external proof at all | no |
  | `failed` - checked, and it did not hold | no |

  A verified proof must also come from an approved backend: `rfc3161` by default,
  replaceable with `gate --approved-backend NAME`. `local` is never accepted even when
  named, because external evidence rests on someone other than the party being audited.
  A proof that **failed** is not routed around - an anchor carrying one verified and one
  failed proof fails the requirement, since a token that does not verify is evidence of
  tampering or of a proof copied from another anchor. Proofs are checked against the
  subject digest **recomputed** from the anchor, never the `subject_digest` field it
  carries, which differ exactly when the anchor was edited after being stamped.

  `gate` gains `--tsa-ca` so the requirement can actually be met, and
  `--approved-backend`. A requirement the gate could not evaluate - `--require-external-anchor`
  with no `--anchor` - reports `not checked` and fails.

  `AnchorState.has_external_proof` is **removed** rather than deprecated: while the
  field existed, the cheapest way to answer the clause was to read a boolean a producer
  could set. Callers pass `check_preregistration(..., external=...)` instead.

- **A trust root supplied for one verification stayed configured for the whole process.**
  `verify --tsa-ca` applied the root with `register_backend()`, which mutates a
  module-global registry. In a CLI that exits immediately this is invisible; in a test
  suite, or any program verifying more than one anchor, a root supplied for one
  verification silently decided the next one's result, and a proof could appear verified
  against a root its verifier never chose. `anchor.temporary_backends()` scopes a
  registration to a block and restores the registry, and the gate's own path constructs
  a configured backend per call without touching the registry at all.

- **`require_signature` was declared and never read.** A pre-registration could set it,
  `examples/prereg.json` shipped it, and `gate --prereg` passed an **unsigned** receipt
  silently - no signature clause appeared in the output at all. It was reachable only
  by hand-editing JSON, which is how a setting ends up declared and unread. This is the
  failure `policy.py` opens by naming: a threshold that quietly does nothing is worse
  than no threshold, because the build going green is read as evidence.

  Wiring it to `verify_signatures()` and reading the boolean would have replaced one
  false green with a subtler one, so `verification.py` now returns a typed outcome and
  the clause reports four claims separately:

  - **presence** - a sidecar with at least one entry
  - **validity** - the signature verifies **and** the record still matches the hash
    that was signed. Comparing the signature against the record's stored `hash` field
    only compares two copies of the same number: edit a score, leave the field, and the
    signature would still "cover" it. The content is re-hashed.
  - **coverage** - a valid signature over *this* receipt. `sign` signs the ledger head,
    so signing and then appending leaves the new receipt uncovered while
    `verify --signed` still correctly reports "records 0-0 of 2 are signed" and exits 0.
    A gate reading that boolean would pass a receipt nobody signed.
  - **identity** - the signer is one the **verifier** trusts, via `--trusted-key`
    (repeatable, for rotation) or `trusted_public_keys`. The flag wins, because trust
    should not be decided by the file being verified. With none declared the clause says
    *"identity not checked"* rather than implying a trusted signer.

  Unsupported algorithms, malformed signatures, a missing sidecar, an unreadable ledger
  and a rewritten ledger each fail closed with their own message. A requirement the gate
  could not evaluate fails rather than skips.

#### Earlier: signing requirements

- **`evalseal preregister --require-signature` and `--trusted-key`**, and
  `gate --trusted-key`. The requirement had no CLI surface at all.

#### Earlier still

- **RFC 3161 signature validation.** `evalseal anchor-verify --tsa-ca issuer.pem` now
  checks a timestamp token's signature over the re-tagged `signedAttrs`, the
  `messageDigest` attribute against `eContent`, that `eContentType` is
  `id-ct-TSTInfo`, that the signing certificate carries a **critical**
  `id-kp-timeStamping` EKU, that `genTime` falls inside that certificate's validity,
  and that the certificate was issued by a root **you** supplied.

  No new dependency: the CMS navigation is hand-rolled on the TLV walker already in
  `rfc3161.py`, and `cryptography` does every cryptographic operation and all
  certificate parsing. `asn1crypto` was the fallback and `pyasn1-modules` was rejected;
  neither was needed.

  **Three states, three sentences**, each tested to differ. With a root:
  *signature verified against the certificate you supplied; revocation not checked*.
  Without one: *message imprint checked, TSA signature NOT verified by EvalSeal* - the
  claim this backend always made, so a caller who supplies no root gets no silent
  upgrade, and the signature check is reported as **not run** rather than as passed. On
  failure: *verification FAILED*, naming the checks.

  Revocation is never checked, and is named in the verified sentence rather than
  omitted, because a verifier silent about what it skipped is read as having skipped
  nothing. There is no path building and no default authority, and **the trust root is
  never read from the proof** - a root the author chose would defeat the point of an
  external attestation, and a test greps the source to keep it that way.
- **`tests/tsa_fixture.py`**, a timestamp authority built in-process with a knob for
  each thing that can be wrong. A recorded response from a real authority shows the
  parser handles what the world emits; it cannot show what happens when a signature is
  tampered with or a certificate lacks the timeStamping EKU, because a real authority
  cannot be asked to misbehave. Twelve failure modes are now asserted, each failing
  exactly one **named** check - a matrix where every failure said "invalid" would teach
  nothing.

- **A decision table for Rekor and OpenTimestamps**, in
  [docs/external-anchoring.md](docs/external-anchoring.md#choosing-between-them) -
  design only, no implementation. It compares what each establishes, whose word it rests
  on, who sees the digest, time granularity, revocability and offline verifiability, and
  states the posture: **RFC 3161 is the default because it publishes nothing**, and a
  public log must never be selected on a user's behalf. A test fails the day a backend
  named for a public log appears in the registry, so that choice cannot be made by
  accident.
- **A privacy section that makes the argument that matters.** The usual warning is that
  a digest of low-entropy content can be guessed, which is beside the point for a
  `subject_digest`. The real cost of a public log is that it **leaks cadence and
  existence, not content**: it records permanently that some commitment was made at
  some time under some key, so a burst of anchors or a long gap says something whether
  or not the digests ever resolve. And it is irrevocable, by design.

- **`examples/verification-demo/`**, the walkthrough for someone handed a receipt with
  no access to the author's machine. Four passing steps - chain, artifacts, signature,
  anchor - each stating what it establishes and **who must be trusted** for it, then
  five tampering demos: an edited score, an edited artifact, a deleted artifact, an
  edited receipt, and the wrong trust root. A demo where everything is green cannot
  tell a working check from one that always passes, so half the page is failures, and
  a test runs every command including the failures.

  The page closes with what no amount of verifying establishes - that the eval ran, that
  the provider served the model it named, that the inputs were honest, when it happened,
  or that it was the only run.

  The bundle's timestamp authority is a throwaway fixture that attests nothing, labelled
  as such in a warning block, because a reader following a demo is exactly who might
  mistake it for a real timestamp. A test asserts that label stays, and that no private
  key was ever committed.

- **A recorded real TSA response**, from `freetsa.org`, and the fix it immediately
  forced. The verifier assumed SHA-256 with RSA; freetsa signs with **ECDSA and
  SHA-512**, so the first real token failed twice - a 64-byte `messageDigest` attribute
  where 32 were expected, and an unrecognised signature algorithm. The digest and
  signature algorithms are now read from the SignerInfo, RSA and ECDSA are supported
  with SHA-256, SHA-384 and SHA-512, and the algorithm lookup compares the OID alone
  because RSA carries an explicit NULL parameter and ECDSA omits the field. The
  synthetic fixture can now produce every combination, so the assumption cannot creep
  back for an algorithm the recorded token does not happen to use.

  This is the whole argument for recording a real response rather than trusting a
  self-made one, demonstrated on the first attempt.
- **The procedure and the waiting test that made it a one-command step.**
  [tests/fixtures/tsa/RECORDING.md](tests/fixtures/tsa/RECORDING.md) is the one network
  call that would close the last box, and `tests/test_real_tsa_fixture.py` consumes the
  fixture already - skipping while it is absent, passing the moment it lands, which was
  verified by planting a stand-in and watching all ten tests run. The procedure leads
  with the trap: the fixture's certificate **will** expire, so verification is checked
  as of the token's own `genTime` and never the clock now, and the consuming test
  asserts that locally rather than trusting the prose.

### Fixed

- **A roadmap box that claimed its own work was impossible without a new dependency.**
  The v2.2 milestone said validating the RFC 3161 signature "needs an ASN.1 dependency
  (`asn1crypto` or `pyasn1-modules`)". It did not; the CMS navigation is hand-rolled and
  `cryptography` does the rest. The box is ticked and the assumption corrected, so the
  next person does not re-derive a trade-off that was never required.
- **A `.gitattributes`, because a CRLF checkout breaks every sealed digest.** A receipt
  seals SHA-256 digests over its artifacts and `verify --artifacts` re-hashes the bytes
  on disk, so a Windows checkout that rewrites LF to CRLF changes those bytes and the
  check correctly reports the artifact as `changed`. That is how
  `examples/verification-demo/` failed on Windows and nowhere else. Every path whose
  digest is stored anywhere is now marked `-text`, with the rule of thumb written beside
  it, and two tests enforce it: one asserts the attribute, the other reproduces the
  CRLF failure so it cannot silently stop failing.
- **An example receipt that `.gitignore` was swallowing.** `report.json` is ignored to
  stop generated receipts being committed from the worktree root, and it swallowed the
  demo's receipt too - so every test reading it passed locally and failed on every
  platform in CI. An explicit negation for `examples/**/report.json`, and the
  completeness test now reads `git ls-files` rather than the filesystem, which is what
  would have caught it first.
- **An import cycle that shipped broken in 2.2.0.** `from evalseal.rfc3161 import
  Rfc3161Backend` raised `cannot import name 'Rfc3161Backend' from partially
  initialized module`, and so did `import evalseal` followed by the same line, because
  `evalseal/__init__` does not import `anchor`. The cycle is rfc3161 → anchor (for
  `AnchorError`) → rfc3161, closed by a registration at `anchor.py`'s module scope, and
  it resolved only when `anchor` happened to be imported first - which every test and
  the CLI did, which is why it was never seen. Built-in backends now register on first
  use of the registry; four import orders are asserted in subprocesses, because a cycle
  only bites on a cold import.
- **`parse_timestamp_response` navigates the structure** instead of finding TSTInfo by
  shape. A signature covers the actual `eContent` octets, not something that merely
  looks like a TSTInfo - and the old hand-made test double, which satisfied the
  heuristic, turned out not to be navigable CMS at all.
- **The privacy section claimed the cassette's digest was not sealed.** It has been
  since schema 1.4, and `verify --artifacts` re-hashes it. The same stale claim was
  fixed in the README for 2.2.0; this copy survived.
- **A stored proof no longer says "NOT verified by EvalSeal" permanently.** That was
  accurate when nothing could verify it and misleading once something could. The
  `boundary` field now describes the token at rest and points at how to check it.

### Changed

- **Validity is checked against the token's own `genTime`, never the clock now**, with a
  test pinning that. A recorded fixture's certificate expires, and a verifier using
  `now` would start failing on a date nobody chose.
- `anchor --list-backends` describes what verification checks with and without a root,
  rather than carrying a flat `EXPERIMENTAL` marker that is no longer the whole story.


## [2.2.0] - 2026-09-28

A minor release: it adds commands (`preregister`) and flags, and no command is removed.

Everything here answers an objection raised about 2.1.0 rather than an idea about what
to build next. The objections, and the honest answer to each, are in the sections below.

### Migrating from 2.1.0

**Nothing is required.** Old receipts verify, old suite files load, old policies apply
and existing pins are unchanged.

Two things are worth knowing:

- **Schema 1.5 records what the environment claimed about CI** and the section digests
  of a version-2 suite. Records sealed under 1.4 and earlier verify as before, re-hashed
  under their own schema.
- **The evaluator fingerprint did not change.** It is still scheme 2, and a suite moved
  to the new sectioned format produces the same fingerprint as the flat file it
  replaces - asserted by a test that runs both. No re-pinning.

### Added

- **`evalseal preregister`, and `gate --prereg`.** The objection: *"does EvalSeal stop
  someone rerunning until they get a good result and only sealing that one?"* It does
  not, and [docs/pre-registration.md](docs/pre-registration.md) says so in those words -
  no local tool can prove a private run never happened. What a pre-registration does is
  fix the contract **before any number exists**: the suite, dataset, exact case-id set,
  repeat count, a per-case score floor, required artifact roles, optionally an embedded
  policy and a pinned evaluator fingerprint. Dropping the eight items that failed,
  halving the repeats or swapping the judge then fails the gate by name instead of
  being a quiet edit. Every clause is one named check, passing ones reported too.
- **Agent eval receipts.** The objection: *"for agent evals, sealing the tool ACL is not
  enough - mocked and frozen tool responses must be sealed too."* Correct, and for the
  same reason sealing a judge's model name without its rubric is not enough: the ACL can
  be byte-identical while `search_docs` returns something new. `run --artifact
  ROLE=PATH` binds any file into a receipt by digest, so `verify --artifacts` re-checks
  it. [docs/agent-eval-receipts.md](docs/agent-eval-receipts.md) defines the two
  artifacts - a tool manifest with per-tool definition digests, and frozen responses
  keyed by (case, repeat, seq) with per-call input and response digests, an outcome of
  ok/denied/error and a response mode of live/recorded/mocked/external - and maps
  Kitaru-style replay, Langfuse and Braintrust traces onto them. **EvalSeal does not run
  or replay agents**, has no tool loop, and a test fails if the page stops saying so.
- **`evalseal --version`** (`-V`), printing the version, the `harness_version` it seals
  into receipts and the record schema. Two strings because they can disagree, and an
  editable checkout whose metadata lags is exactly when someone needs to see both.
- **Schema validation for agent artifacts.** A file sealed under an agent role -
  `tool_acl`, `tool_manifest`, `tool_responses`, `tool_calls` - is checked against the
  documented schema and the run is **refused** if it does not match, for the same reason
  an `external` artifact fails closed: a digest over a broken file produces a receipt
  that verifies perfectly and describes an environment nobody can reconstruct. It
  catches a bad `response_mode`, an `allowed` tool the manifest never defines, a tool
  both allowed and denied, duplicate `(case_id, repeat, seq)` keys, a missing
  `response_sha256` on a successful call and a present one on a denied call. This is
  **shape validation, not truth**: EvalSeal still does not run or replay agents, and
  cannot tell whether a harness reported calls it never made. A role outside that set
  stays opaque, so `--artifact` remains general.
- **An RFC 3161 anchor backend**, experimental. The objection: *"a local signature proves
  integrity after signing, but not independent time or custody."* `anchor --with rfc3161
  --tsa URL` builds a real DER `TimeStampReq`, submits it to an authority **you** name,
  and stores the token. Verification is offline and partial, and says which half ran: it
  confirms the token's message imprint is this anchor's subject digest and reports the
  asserted time, and does **not** validate the authority's CMS signature. Proofs carry
  `verified_by_evalseal: false` and every message hands back the `openssl ts -verify`
  command that finishes the check. There is no default authority, because a default would
  make one service part of this project's trust root. Only the 32-byte digest is sent; a
  test asserts the request body stays under 128 bytes.
- **Sectioned suite files (version 2).** The objection: *"the suite hash is excluded from
  the evaluator fingerprint because suites bundle target config; eventually suite files
  should split evaluator and target sections."* They now can:
  `evaluator`, `target`, `dataset`, `cases`, `run`, `policy`. The evaluator and target
  section digests are sealed separately, so a receipt shows which half of a changed suite
  moved. `cases.only` filters the run and `policy.file` is applied by `gate --suite` -
  neither is accepted and ignored. The flat format keeps working;
  [docs/suite-format.md](docs/suite-format.md) explains the split, and why the section
  digests are recorded rather than fingerprinted.
- **CI provenance in the receipt (schema 1.5)**, so a pre-registration can require that a
  receipt came from CI. Both the field and the rule are named for what they are -
  `claimed`, and `require_ci_claim` - because a rule called `require_ci` reads as proof,
  and this is a claim: `GITHUB_ACTIONS=true evalseal run` on a laptop passes it. The
  check's output leads with `CLAIM ONLY, not proof` and says that a resolvable run URL
  and a CI-held signing key are what make it evidence. Only a fixed allowlist of
  variables is read, named one at a time rather than by prefix, so `GITHUB_TOKEN` cannot
  be swept in.
- **A much fuller PR workflow**: artifact verification, the pre-registration gate, a judge
  drift report beside the score diff, and an anchor that is re-verified after being
  written. The step summary gained the pre-registration clauses, what moved underneath
  the score with the least stable cases, the served model when it differs, and the
  verification output quoted verbatim. When no pre-registration was applied it says so.

### Fixed

- **`evalseal anchor` tracebacked when handed the output of `report --json`.** Both files
  get called report.json in different places, so passing the wrong one is an ordinary
  mistake - and it produced a pydantic `ValidationError: input_value='}'`, which
  described this project's own JSONL fallback rather than anything the user did. Loading
  a receipt now names the file and what it turned out to be, telling apart invalid JSON
  (with the position), a report summary, an anchor, a JSON list, an object missing
  receipt keys, and a receipt whose fields are wrong - that last naming the fields. Exit
  code 2, already documented as "bad arguments".
- **The README claimed the cassette's hash was not sealed.** It has been since schema
  1.4, and `verify --artifacts` re-checks it. The bullet now states the real limit: a
  digest can only be matched against a file someone kept.
- **A pre-registration pinned the dataset with the wrong digest of the two.** A receipt's
  dataset hash is taken over the decoded text; `file_hash` is over the raw bytes. Reading
  a CRLF file translates newlines, so the two disagree on Windows and the dataset clause
  failed for a reason that had nothing to do with the dataset. The contract now pins the
  digest a receipt carries - one definition, as with the case-set hash. Found by CI on
  Windows, which is the only place it could have been found.

### Changed

- **Schema 1.5** adds `environment.ci` and the suite's `evaluator_hash` and
  `target_hash`. Older records verify, re-hashed under the schema that sealed them.
- **`provenance.py` now reads environment variables**, which it previously never did. Its
  no-secrets invariant moved from "reads nothing" to "reads only this allowlist, named
  one variable at a time", asserted on the module source and, end to end, on the sealed
  bytes of a record produced with secrets in the environment.
- **THREAT_MODEL.md** gained *Five questions people actually ask* - cherry-picking, what
  an agent receipt should seal, what a local anchor proves, what an external one would
  add, and why the suite hash stays out of the evaluator fingerprint - plus the long
  cherry-picking answer, the CI trust boundary and tool-response drift for agent evals.
  README gained *Why each piece exists*. ROADMAP records what the v2.2 anchoring
  milestone actually met and the one gap that keeps it experimental.
  docs/integrations.md replaces the hypothetical replay mapping with the command that
  seals it today.
- **`anchor-verify` reports `created_at` as an unverified local clock**, on its own line
  marked not-verified, rather than leaving it unmentioned beside the checks that passed.
  Every layer is now separately named - receipt hash, ledger chain, ledger head,
  signature, each artifact, the clock, the external proof - and a test asserts it.
- **The PR step summary leads with comparability**, ahead of the gate verdict and every
  number, because a score delta between differently graded runs is not a change.
- **The RFC 3161 backend states its boundary in one phrase, everywhere.** *"message
  imprint checked, TSA signature NOT verified by EvalSeal"* now leads every proof, every
  check result, `--list-backends`, the README, the threat model and
  docs/external-anchoring.md, and proofs carry a `boundary` field repeating it. For a
  tool whose claim is "here is exactly what was established", a partial check that reads
  like a full one is the worst possible output.
- **A required artifact fails closed.** A role sealed as `external` - named but
  unreadable, so no digest exists - fails the gate unless listed in
  `allow_external_artifacts`. A named test records the decision.

## [2.1.0] - 2026-09-23

A minor release: it adds commands (`anchor`, `anchor-verify`, `drift`) and flags, so
semver rules out a patch release.

No command is removed and no exit code changes meaning. Three things do change shape, all
of them covered under *Migrating from 2.0.x* directly below: fingerprint values now carry
a scheme prefix, `diff`'s comparability verdict is the single token `non_comparable`, and
the sealed-record schema moves to 1.4. Receipts sealed by earlier versions keep
verifying.

### Migrating from 2.0.x

Two changes affect existing users, and both were designed to fail loudly rather than
quietly. Nothing else needs doing.

**Old receipts keep verifying.** Records sealed under schema 1.1, 1.2 or 1.3 are
re-hashed under the schema that sealed them, so `verify` still passes and says which:

```console
$ evalseal verify --ledger old.jsonl
Chain intact: 1 record(s). 1 older schema version(s) verified under their own rules (1.1).
$ echo $?
0
```

**Pinned fingerprints must be re-pinned once.** A fingerprint written by 2.0.x has no
scheme tag and cannot match a scheme-2 value. Rather than reporting that as a content
mismatch - which would send someone hunting a change nobody made - the pin fails with
an explanation, whether it lives in a `--expect-evaluator` flag or a policy file's
`run.expect_evaluator`:

```console
$ evalseal gate --expect-evaluator sha256:aaaa...
FAIL The pinned evaluator fingerprint was made under an unversioned scheme; this build
computes scheme 2, which covers fields the older scheme did not. The two cannot be
compared. Re-pin from `evalseal report --json`.
$ echo $?
3
```

To re-pin: `evalseal report --json | jq -r .provenance.evaluator_fingerprint` (or
`.config_fingerprint`) and paste the new value into the flag or the policy file. A pin is
a statement that the instrument has not changed; scheme 2 measures more of the
instrument, so the statement has to be made again.

### Added

- **Evaluator fingerprint scheme 2.** The fingerprint decides whether two runs can be
  compared, and it was missing parts of the instrument it claims to identify. It now
  covers the scorer type **and its own settings**, the judge's provider, endpoint, model
  and sampling parameters including `max_tokens` and `seed`, the rubric and prompt
  template hashes, the dataset hash and the exact set of case ids. The target model, its
  parameters and the suite hash stay out on purpose: swapping the model under test is the
  reason to run a benchmark. Fingerprints carry their scheme
  (`evalseal-fp/2:sha256:…`), so a pin made under an older one fails with "made under an
  unversioned scheme - re-pin" rather than an unexplained mismatch. The same explanation
  is given whether the pin is a `--expect-*` flag or a policy file's `run.expect_*` rule:
  one function answers the question, because two answers would eventually disagree.
- **`evalseal drift A B`**, judge drift as a first-class report. It labels the cause from
  the receipts: `evaluator_drift` when the grading setup changed, `judge_variance` when an
  identical judge disagreed with itself, `target_variance` when a deterministic grader
  means only the target can have moved, `target_change`, or `stable`. It carries both
  fingerprints, per-case verdict strips, pass/fail/other counts, flip rates, stability
  changes, the least stable cases and a plain-English summary, as Markdown or JSON.
  Evaluator drift is never phrased as the model improving or worsening, and a test asserts
  those words cannot appear in that summary.
- **Artifact binding.** Receipts seal digests of the cassette, dataset and suite, with an
  explicit `kind` - `hashed`, `embedded` or `external` - so a reader never has to guess
  what the receipt holds. `evalseal verify --artifacts` re-hashes them and reports ok,
  changed, missing or unverifiable, and `anchor-verify` checks them too. A missing file is
  reported as missing, never as passing.
- **An anchor backend protocol.** `submit` a subject digest, `check` a proof later, and
  say what it establishes. The local backend is kept and is honest: it attests nothing and
  its check says so. Backends register themselves, so an adapter for a real service is
  additive. No fake backend ships - the test double lives in the tests, because a fake
  that looks like an anchor is worse than no anchor. `evalseal anchor --with <backend>`
  and `--list-backends`.
- **`THREAT_MODEL.md`**: what each layer protects, who must be trusted for each claim, and
  the specific questions - a changed judge, a changed dataset, concurrent CI writers, a
  lying clock, a provider that changes behaviour behind a stable model name.
- Integration docs for LangSmith/Langfuse and replay-style workflows, and an explicit
  trust boundary on every integration.

### Fixed

- **Two different regexes fingerprinted identically.** The scorer's own settings were
  sealed nowhere, so `regex "^yes$"` and `regex "^no$"` - opposite graders - produced the
  same evaluator fingerprint and `diff` called two runs comparable that measured opposite
  things. Schema 1.4 seals a scorer config hash.
- **The judge's endpoint was not part of the fingerprint**, so the same model name reached
  through a proxy or a different deployment looked like the same grader.
- **`max_tokens` was dropped from the sealed parameters**, so a receipt could not explain a
  truncated verdict even though the run warned about one.
- **The cassette was not bound to the receipt.** It holds the responses the verdicts came
  from, and nothing in the receipt described it, so responses could be swapped and the
  receipt still verified.
- **`diff` now emits `non_comparable` as a single token** with a `reason`, and names the
  exact evaluator fields that moved instead of reporting "something changed". Two records
  sealed either side of schema 1.4 get a reason that says so, because the older one does
  not carry the fields the newer one is being compared on.

### Changed

- **Schema 1.4** adds the provider and `max_tokens` for target and judge, the scorer
  config hash, and the artifact list. Older records still verify, re-hashed under the
  schema that sealed them.
- `TargetResponse.provider` is defaulted rather than required, so a custom `Target`
  written before this keeps working and reports `unknown`.
- Anchor format version 2: `external_proofs` is a list, and the anchor carries the
  digests the receipt itself sealed.

### Adoption readiness, earlier in this cycle

The rest of this entry covers the first half of the cycle: hygiene, the PR receipt
workflow, the local anchor and the fingerprint/gate corrections that preceded scheme 2.

#### Fixed

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
- **`evalseal run` crashed on Windows whenever stdout was redirected.** The report prints
  "⚠" and "·"; a redirected stdout falls back to the locale encoding, which on Windows is
  cp1252 and cannot carry either, so `evalseal run > out.txt` and any CI step that
  captured the output died with `UnicodeEncodeError` while the same command in a terminal
  worked. `report.py` had already made every *file* write UTF-8 for this reason; the
  console half was missing. The CLI now sets UTF-8 with `errors="replace"` on stdout and
  stderr. The repository's own CI could not have caught this: every CLI step is gated
  `runner.os != 'Windows'`. The regression test forces the condition with
  `PYTHONIOENCODING`, so it runs on every platform.
- **`.coverage 2` was committed.** `.gitignore` matched `.coverage.*` but not the
  `name 2` copies macOS and iCloud create. It now matches `.coverage*` and the receipts
  EvalSeal generates.

#### Added

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

#### Changed

- **Schema 1.3.** No field is added; `scorer.judge_prompt_hash` changes meaning, from an
  instantiated prompt to the template. Records sealed under 1.2 still verify. A 1.2 judge
  record and a 1.3 one disagree on this hash for that reason alone, and `diff` now shows
  the schema version among its other differences so the cause is visible.
- **`--store-judge-prompt` now stores the template**, not an instantiated prompt, so it no
  longer embeds case text or a response. It still carries the rubric verbatim, so it
  stays opt-in.

#### Gaps found mid-cycle and closed in it

The judge's endpoint, `max_tokens` and the cassette digest were documented as known gaps
partway through this cycle and are fixed above, under fingerprint scheme 2 and schema 1.4.
They are listed here because the intermediate commits mention them as open.

#### Recorded evidence

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

[Unreleased]: https://github.com/patibandlavenkatamanideep/evalseal/compare/v2.2.0...HEAD
[2.2.0]: https://github.com/patibandlavenkatamanideep/evalseal/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/patibandlavenkatamanideep/evalseal/compare/v2.0.1...v2.1.0
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
