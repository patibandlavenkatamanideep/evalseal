# Threat model

What a receipt proves, what it does not, and who has to be trusted for each claim.

The short version: **EvalSeal proves integrity, provenance and comparability. It does not
prove truth.** A sealed receipt of a run that never happened seals just as cleanly as one
of a run that did. Everything below is an attempt to say exactly where that line falls.

## What EvalSeal proves

| claim | mechanism | who you must trust |
|---|---|---|
| this receipt has not been edited since it was sealed | SHA-256 over the record's content | nobody |
| this record is the one that was in the ledger, in this position | hash chain, each record committing to the previous | nobody |
| these files are the ones the run used | artifact digests sealed in the receipt | nobody |
| this receipt came from the holder of this key | Ed25519 signature over the ledger head | that the private key was not shared or stolen |
| these two runs were graded the same way | evaluator fingerprint scheme 2 | that the fingerprint covers what matters, which this file lists |
| the score moved by more than noise | paired test over the same items | the statistics, which are in `analyze.py` and `paired.py` |
| this anchor existed before time T | an external proof, when one is attached | the attesting party |

## What EvalSeal does not prove

- **That the eval ran at all.** Every number comes from a cassette or a live call that
  EvalSeal itself made, but a receipt is a record of bytes, not of events. Someone able
  to write a cassette can seal a receipt for an eval that never happened.
- **That the provider served the model it named.** `served_model` is whatever the
  response said. A provider that silently routes to a different model produces a receipt
  that faithfully records the lie.
- **That the inputs were honest.** A rubric that grades nonsense, a dataset with wrong
  answers, a judge prompt designed to pass everything: all seal cleanly.
- **When anything happened.** See "if the machine lies about time" below.
- **That the model is good.** A receipt measures reproducibility and provenance. It says
  nothing about whether the task matters or the scoring is sensible.
- **That this was the only time the suite ran.** See "Can EvalSeal stop cherry-picking?"
  below: it cannot, and nothing that runs on the author's machine can.

## What each layer protects

### Ledger hashes

Each record's SHA-256 covers its whole content plus the previous record's hash. Editing a
past score breaks that record's hash; re-hashing it breaks the next record's link.
`evalseal verify` recomputes all of it.

**Protects against:** a single edited record, a reordered ledger, a silently dropped
record.
**Does not protect against:** someone rewriting the whole file. A complete rebuild
produces a valid chain. That is what signing is for.

### Signing

`evalseal sign` signs the ledger head with Ed25519. The head commits to every earlier
record through the chain, so one signature covers the ledger up to that point.

**Protects against:** anyone without the private key altering the ledger undetectably.
**Does not protect against:** the key holder. They can rebuild the ledger and re-sign it.
A verifier who only ever sees the new signature cannot tell. It also says nothing about
*when* the signature was made - `signed_at` is the signer's own clock.

### Anchoring

An anchor binds the receipt, the ledger head, the signature and the artifact digests into
one file a third party can re-check. Today's anchors are **local**: `created_at` is the
anchoring machine's clock and `external_proofs` is empty.

**A local anchor protects against:** a receipt being swapped for a different one, or an
artifact changing, between you and whoever you hand it to. `anchor-verify` names each
layer separately - receipt hash, ledger chain, ledger head, signature, each artifact -
and prints `created_at` on its own line marked *not verified*, so a clean verdict cannot
be read as including the timestamp.
**It does not protect against:** backdating, or the key holder re-anchoring a rebuilt
ledger. Both need an external attestation.

**External anchoring, as it stands.** `evalseal anchor --with rfc3161 --tsa URL`
submits the subject digest to a timestamp authority you name and stores the token.
`anchor-verify` re-reads it offline and confirms the token is about this anchor's
subject digest, and reports the time the authority asserts. It **does not verify the
authority's signature** - that needs CMS validation EvalSeal does not do - so the proof
carries `verified_by_evalseal: false` and the check hands back the `openssl ts -verify`
command that finishes the job. Until that check moves inside EvalSeal, treat an RFC 3161
proof as evidence a third party can confirm, not as something EvalSeal confirmed. There
is no default authority, by design:
[docs/external-anchoring.md](docs/external-anchoring.md).

### Artifact digests

The receipt seals digests of the cassette, dataset and suite. `evalseal verify
--artifacts` re-hashes them.

**Protects against:** responses being swapped after the fact, a dataset changing under a
saved score.
**Does not protect against:** an artifact that no longer exists. A digest can only be
matched against a file someone kept, which is why anyone who may need to prove what a
model said must retain the raw artifacts themselves, securely, outside EvalSeal.

## Five questions people actually ask

Short answers, each linking to the long one. These are the questions raised about 2.1.0,
in the words they were asked in.

**1. Can EvalSeal stop cherry-picking?** No. No local tool can prove hidden private
reruns never happened. EvalSeal reduces the risk by making the declared suite, run
count, evaluator, artifacts, ledger, CI receipt and anchor verifiable. The long answer
is the next section.

**2. What should an agent eval receipt seal?** Target model, evaluator fingerprint, tool
ACL, tool manifest, the frozen replay or mocked responses, the per-call tool inputs and
outputs, the dataset and case set, and the policy. The ACL alone is not enough: it says
what the agent *could* call, and the frozen responses are the world it was evaluated
inside. See [docs/agent-eval-receipts.md](docs/agent-eval-receipts.md).

**3. What does local anchoring prove?** Integrity of the receipt, the ledger head, the
signature state and the bound artifacts, from the moment the anchor was created. **It
does not prove independent time.** `created_at` is the anchoring machine's own clock, and
`anchor-verify` prints it on a line marked *not verified* rather than beside the checks
that passed.

**4. What would external anchoring add?** An independent timestamp, or inclusion in a
transparency log, letting a third party confirm the receipt existed by a given time -
which is what stops backdating and stops a key holder silently replacing a signed state.
The `rfc3161` backend stores a real token today; it is experimental because EvalSeal does
not yet validate the authority's signature. See
[docs/external-anchoring.md](docs/external-anchoring.md).

**5. Why is the suite hash not in the evaluator fingerprint?** Because a flat suite file
bundles the target model config. Hashing the whole suite would make "same evaluator,
different target model" non-comparable - and that comparison is the reason to run a
benchmark. The version-2 suite split separates the two, and
[docs/suite-format.md](docs/suite-format.md) explains why the section digests are
recorded rather than folded into the fingerprint.

## Can EvalSeal stop cherry-picking?

**No.** No local tool can prove that hidden runs never happened.

The ledger is a file on the author's machine. It records what was appended to it.
Someone who runs a suite five times can delete the ledger four times and seal the fifth
run into a fresh one; the result verifies perfectly, because nothing outside that
machine saw the other four. A hash chain shows that a sequence was not edited *after*
it was written. It says nothing about sequences that were never written down.

What EvalSeal does instead is reduce the risk by making the evaluation contract, the
run count, the ledger, the artifacts, the CI receipt and the anchors verifiable:

| move | what makes it visible |
|---|---|
| drop the items that failed | `case_set_hash` in a pre-registration, checked by name |
| shorten the runs and report the best | declared `n_repeats` and a per-case score floor |
| swap the judge or edit the rubric after seeing the score | pinned evaluator fingerprint |
| loosen the threshold after a failure | a policy committed before the run; the change is a commit |
| publish a score with no responses behind it | required artifact roles, re-hashed by `verify --artifacts` |
| produce the receipt on the laptop of the person being measured | `require_ci`, and a signing key only CI holds |
| backdate it | an external anchor - and only an external one |

None of that stops a determined author from pre-registering, running privately until
the result is good, and publishing the run that matched. What it costs them is
optionality: every parameter they might have tuned in response to the result was fixed
by digest beforehand. The remaining move - re-rolling the same declared evaluation
until noise favours them - is what `evalseal power` and per-case flip rates exist to
make expensive.

The honest summary: **a pre-registration makes the contract checkable, not the author
honest.** [docs/pre-registration.md](docs/pre-registration.md) says the same thing at
length.

## Specific questions

### What happens if the judge changes?

The evaluator fingerprint changes, and every comparison against an older run is reported
`non_comparable` before any score is shown. `evalseal drift` labels it `evaluator_drift`
and states in words that none of the movement can be attributed to the model under test.

Scheme 2 covers the scorer type and its settings, the judge's provider, endpoint, model,
temperature, top_p, max_tokens and seed, the rubric hash, the prompt template hash, the
dataset hash and the exact set of case ids. A change to any of those makes runs
non-comparable.

**What it still misses:** anything the provider changes behind a stable model name. A
judge model silently updated server-side has the same fingerprint and different
behaviour. That is the case `evalseal drift` exists to make visible: identical
fingerprint, moved verdicts.

### What happens if the dataset changes?

The dataset hash covers the file's bytes and the case-set hash covers the ids actually
scored, so both a changed file and a changed subset make runs non-comparable. Cases
present in only one run are listed and excluded from the paired test rather than silently
compared.

### What happens if two CI jobs write concurrently?

The ledger head is read and the record appended inside one file lock, so two runs cannot
create sibling records claiming the same predecessor. `verify` reports siblings
explicitly if a ledger acquires them another way.

This is `fcntl` on POSIX and `msvcrt` on Windows: **one machine**. On NFS or SMB the
guarantee weakens or disappears, and two runners on different machines sharing a ledger
over a network filesystem are outside what this protects.

### What happens if the machine lies about time?

Every timestamp in a receipt, a signature and a local anchor comes from the machine that
wrote it. A backdated clock produces a backdated receipt, and nothing in the current
implementation detects it. An external anchor is the only fix, and it is future work.

Treat `created_at`, `started_at` and `signed_at` as claims by the author, not as evidence.

### What happens if the target model provider silently changes behaviour?

Nothing in the fingerprint changes, because nothing the receipt can see changed. The
provenance records `served_model` and the provider's `system_fingerprint` when one is
returned, and a run warns if either moves mid-run, but a provider that changes behaviour
without changing those is invisible to a single receipt.

What does show it: running the suite again and comparing. Verdicts that move with an
identical evaluator fingerprint are exactly the signal, and `evalseal drift` reports
them. Whether that movement is the provider or ordinary sampling variance cannot be
separated from two receipts alone.

### What happens if a mocked tool response changes? (agent evals)

A tool ACL says which tools an agent could call. It does not say what they returned,
and the returns are half the environment: `search_docs` surfacing a different top
result, or a mock edited to be friendlier after a bad run, changes behaviour while the
ACL stays byte-identical. A receipt that sealed only the ACL would report two
incomparable runs as comparable - the same failure as sealing a judge's model name but
not its rubric.

Seal both, by digest, and `verify --artifacts` re-checks both:

```bash
evalseal run --suite agent-suite.json \
  --artifact tool_acl=tools.json --artifact tool_responses=responses.json
```

**Protects against:** a mock, a recorded transcript or an ACL changing under a saved
score.
**Does not protect against:** a harness that reports tool calls it did not make, a
`live` tool mode (which is not reproducible by definition and labels itself so), or a
recorded response that was wrong when it was recorded. EvalSeal binds the file; it did
not observe the agent, and it does not replay agents.
[docs/agent-eval-receipts.md](docs/agent-eval-receipts.md) has the schema and the
limits.

### What does a receipt produced in CI establish?

Schema 1.5 seals what the environment *claimed* about the CI job - provider, run id,
run URL, repository, ref, commit, event - and a pre-registration can require it with
`require_ci`. The field is named `claimed` because that is exactly what it is.

**The trust boundary:** every value is an environment variable. `GITHUB_ACTIONS=true
evalseal run` on a laptop produces a receipt that claims CI. The check's own output
says so rather than implying more.

What turns the claim into evidence lives outside the receipt, and both parts are yours
to arrange:

1. **The run URL resolves** to a job a reviewer can open and see running this suite.
2. **The ledger is signed by a key only CI holds.** If the signing key is in CI secrets
   and not on developer machines, a locally produced receipt cannot carry a valid
   signature. This is the part that actually moves receipt production off the machine
   of the person being measured, and it is a property of your key management.

A CI runner is also not a neutral party: whoever controls the workflow file controls
what runs. A receipt from CI raises the cost of producing a dishonest one; it does not
make one impossible.

Only a fixed allowlist of variables is read, named one at a time rather than by prefix,
so a secret in the environment cannot reach a sealed record. A test asserts both the
allowlist and, end to end, that no secret appears in the sealed bytes.

### What requires retaining the original private artifacts?

Any dispute about *content* rather than identity. A hash proves that a disclosed file is
the one that was sealed; it cannot reconstruct a file nobody kept. If you may need to
show what a model actually said, keep the cassette.

## Privacy posture

- **Hashes by default.** Receipts store digests of the dataset, rubric and judge prompt
  template, plus case ids, scores and verdicts. They do not store prompts or responses.
- **Raw content only behind an explicit flag.** `--store-judge-prompt` seals the judge
  prompt template verbatim. Since schema 1.3 that template carries no case text, but it
  does carry the rubric, so it stays opt-in.
- **Cassettes are the exception, and they are not receipts.** A cassette holds full
  request bodies and response envelopes in plain text. It never holds an API key, which
  is sent in a header and never recorded. Committing a cassette publishes the prompts and
  responses in it: fine for public benchmark data, not fine for a private dataset.
- **HTML receipts and CI artifacts carry hashes and scores only**, so a receipt can be
  attached to a pull request without leaking the dataset.

## Reporting a weakness

If you find a way to make EvalSeal vouch for something it should not, please report it
through [SECURITY.md](SECURITY.md). A receipt that overclaims is a bug of the most
serious kind this project has.
