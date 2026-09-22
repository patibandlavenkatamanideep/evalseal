# Roadmap

EvalSeal produces reproducibility receipts for LLM evaluations: it runs each case several
times, measures how often the verdict disagrees with itself, seals the result with its
provenance into a hash-linked ledger, and gives CI and reviewers something to check. This
page says where it is, where it is going next, and what it will not become.

Dates are deliberately absent. Each milestone ships when its acceptance criteria hold,
and each one names them.

## Current status

### What EvalSeal does today

- **Repeated-run measurement.** Every case runs N times; each gets a verdict sequence, a
  flip rate and a Wilson interval, and a stability class derived from that interval.
- **A paired comparison between two runs.** `diff` tests two sealed runs item by item
  (exact McNemar on the items that changed verdict, cluster bootstrap over items for the
  interval) and reports `regression`, `improvement` or `inconclusive`, never "no
  difference". It checks comparability before it reports a delta.
- **Sealed provenance.** Requested and served model, endpoint, parameters and whether they
  were set, rubric and judge prompt hashes, dataset and suite hashes, EvalSeal version, git
  commit and dirty flag, Python and platform.
- **Two fingerprints.** An evaluator fingerprint ("are these runs comparable?") and a
  config fingerprint ("what exactly ran?").
- **Tamper evidence.** A hash-linked append-only ledger, `verify`, and Ed25519 signatures
  over the ledger head that a third party can check with the public key.
- **Record and replay.** Cassettes keyed by request and repeat, so CI replays a recorded
  run with no API key and no network, at any concurrency, with identical results.
- **CI surface.** `gate` with flags or a checked-in policy file, JUnit output, JSON output
  for every command that reports, and self-contained HTML receipts.
- **Experiment sizing and attribution.** `power` says how many items a difference would
  need; `decompose` separates judge variance from target variance on one suite.

### What it does not prove

- **That the provider behaved truthfully.** A receipt records what the endpoint returned,
  not whether the model named in the response is the model that ran.
- **That a live model would answer the same way today.** A replay reproduces the recorded
  responses and the analysis over them. Reproducing the model needs a new recording.
- **When something happened.** Every timestamp in a receipt, including a signature's
  `signed_at`, comes from the machine that wrote it. See
  [docs/external-anchoring.md](docs/external-anchoring.md).
- **That a sealed rubric is a good rubric.** Hashes detect change, not incorrectness.
- **That an `inconclusive` comparison means two runs are equal.** It means the experiment
  lacked the power to tell. `evalseal power` says what it would take.

### Out of scope, on purpose

Hosting, storing traces, dashboards, human review queues, and running evals at scale. Other
tools do those well; EvalSeal is the receipt layer around them. See
[docs/integrations.md](docs/integrations.md).

## Milestones

### v2.0.2 - hygiene and docs precision (folds into 2.1.0)

Planned as a patch. It is shipping inside **2.1.0** instead, because the same branch adds
a CLI flag and two commands, and semver reserves a patch release for fixes. The hygiene
items themselves:

- [x] Remove the accidentally committed `.coverage 2`; widen `.gitignore` to `.coverage*`
      and to the receipts EvalSeal generates.
- [x] One consistent account of the two fingerprints across the README, DESIGN.md and the
      CLI help, matching the code.
- [x] `gate --expect-config` no longer reports "not directly comparable" on a mismatch. It
      cannot know which side changed, and the claim was false whenever only the target
      model moved.
- [x] Cassette saves are atomic, so an interrupted recording keeps what it recorded.
- [x] `power` no longer scans every repeat count up to 201 at realistic suite sizes.

### v2.1 - the PR receipt workflow

Make a pull request the natural place to read a receipt.

- [x] A copy-pasteable workflow and guide: [docs/pr-receipt-workflow.md](docs/pr-receipt-workflow.md).
      Replays a cassette with no key, verifies, gates, optionally diffs against a
      baseline, uploads the receipts, and writes a step summary that shows comparability
      before any score delta.
- [x] `gate --expect-evaluator`, so a PR can pin the grading setup while still changing
      the model under test.
- [x] A local anchor file (`evalseal anchor`, `evalseal anchor-verify`) that binds a
      receipt, its ledger head and its signature state into one checkable object.
- [ ] A reusable composite action (`uses: patibandlavenkatamanideep/evalseal@v2`) so the
      workflow is a few lines rather than a file to copy.
- [ ] Baseline retrieval that does not depend on committing receipts: fetch the latest
      receipt from the default branch's artifacts.
- [ ] **Provenance completeness.** Seal `max_tokens`, which the Anthropic adapter sends but
      the sealed parameters drop, and bring the judge's endpoint into the evaluator
      fingerprint. Both change content hashes and every pinned fingerprint value, so they
      ship with a fingerprint scheme version that makes an old pin fail with "fingerprint
      scheme changed" rather than an unexplained mismatch, plus fingerprint-stability
      tests.

Accepted when: a repository with no API key can adopt the workflow by copying one file,
and a reviewer can tell from the step summary alone why the check passed or failed.

### v2.2 - external anchoring

Give a third party a timestamp and custody claim that does not rest on the signer's clock.

- [ ] An adapter interface over the local anchor object, filling its `external_proof`.
- [ ] RFC 3161 timestamp authority adapter.
- [ ] Sigstore / Rekor transparency log adapter.
- [ ] OpenTimestamps-style adapter.
- [ ] `anchor-verify` checks the external proof offline where the proof format allows it.

The design, including what each proof does and does not establish, is in
[docs/external-anchoring.md](docs/external-anchoring.md). EvalSeal will not run a timestamp
server or a verifier of its own: the trust root has to be someone other than the party
being audited.

Accepted when: a receipt anchored by one person verifies for another with no access to
the first person's machine, and the verifier output says exactly what was proven.

### v2.3 - integrations with promptfoo and Braintrust

Seal results produced by the eval tools teams already use, instead of asking them to
rerun everything through EvalSeal.

- [ ] An importer that turns exported per-case results into a sealed record, with the
      exported file's hash as provenance.
- [ ] promptfoo: seal the output of a repeated `promptfoo eval`.
- [ ] Braintrust: seal an exported experiment.
- [ ] Generic JSONL importer for any harness that can write `{case_id, repeat, score}`.

The shape of each wrapper, and what each tool keeps owning, is in
[docs/integrations.md](docs/integrations.md). Importers will be built against those tools'
documented export formats, verified against real exports, not guessed.

Accepted when: a team using one of those tools gets a sealed, diffable receipt without
changing how they run evals.

### v2.4 - provider-backed examples

Every provider the code supports should have a recorded run behind it.

- [ ] A recorded Anthropic cassette. The native adapter is covered by tests against the
      documented wire format but has no live recording yet; the README says so.
      [examples/anthropic/RECORDING.md](examples/anthropic/RECORDING.md) is the procedure.
- [ ] A recorded OpenAI cassette through the OpenAI-compatible adapter's default endpoint.
- [ ] A recorded comparison large enough for `diff` to return a significant result on real
      data. One is pre-registered in [examples/gsm8k_compare](examples/gsm8k_compare/).

Accepted when: no provider claim in the README lacks a committed cassette.

### v3.0 - only if a contract has to break

There is no v3.0 plan. It exists only for a change that cannot be made compatibly: a
sealed-record schema change that older ledgers cannot be verified under, a CLI exit code
that has to change meaning, or a JSON output field that has to change type. Additive
changes, new commands and new optional fields stay in 2.x.

## Non-goals

- **Not a benchmark leaderboard.** EvalSeal says whether two runs can be compared and
  whether they differ. It does not rank models, and it will not publish rankings.
- **Not a hosted trust authority.** There is no EvalSeal server, and anchoring will use
  independent services rather than one run by this project. A receipt that can only be
  verified by asking its author is not a receipt.
- **Not a replacement for eval platforms.** Running evals at scale, storing traces,
  dashboards and human review belong to the tools built for them.
- **Not a guarantee that the provider or model behaved truthfully.** A receipt records
  what came back, faithfully. It cannot see behind the endpoint.
- **Not a substitute for retaining private artifacts in a dispute.** EvalSeal stores
  hashes of prompts and responses by default, not the text. A hash lets a later-disclosed
  artifact be matched against what was sealed; it cannot reconstruct one that was thrown
  away. Anyone who may need to prove what a model said must keep the raw artifacts,
  securely, outside EvalSeal.

## Contributing to a milestone

Open an issue naming the milestone before starting on it. Every change should improve at
least one of: trust, reproducibility, CI usability, third-party verification, or
integration readiness. A change that improves none of those is out of scope even if it is
useful.
