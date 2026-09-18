# EvalSeal design

## What it claims, and what it doesn't

**EvalSeal measures reproducibility. It does not create determinism.** Setting a
temperature or seed does not make a hosted model deterministic, and EvalSeal never says it
does. It runs every case N times and reports how often the verdict disagrees with itself.

**A green CI replay proves the harness is reproducible, not that the model is.** Replaying
a cassette gives back the recorded responses, so the analysis, scores and report can be
regenerated from them exactly. It says nothing about whether the live model would answer
the same way today. To learn that, record again and `diff` the runs.

**The ledger is tamper-evident, not tamper-proof.** Each record's SHA-256 covers its whole
content plus the previous record's hash. Editing a past score breaks that record's hash,
and re-hashing it breaks the next record's link. Anyone who can rewrite the *whole* file
can still build a new valid chain.

**A signature says who, not what.** `evalseal sign` signs the head hash with Ed25519, and
the head commits to every earlier record, so one signature covers the chain. Holding the
public key, a third party can tell that a receipt came from that key and has not been
altered since. It proves nothing about whether the run happened as described: a signer can
sign a record containing anything. Verification is opt-in (`--public-key` / `--signed`),
because an unsigned ledger is still useful to whoever produced it.

## Components

| module | role |
|---|---|
| `analyze.py` | Pure functions: seeded bootstrap CI, flip rate, stability class. No I/O. |
| `adapters/recording.py` | Cassette keyed by SHA-256 of the effective request + repeat index. |
| `adapters/target.py` | `Target` protocol; `LocalCallableTarget` (tests), `OpenAICompatibleTarget`. |
| `adapters/scorer.py` | Exact, regex, and LLM-judge scorers. The judge is a `Target`. |
| `executor.py` | N-run loop (optionally concurrent), provenance capture and gap warnings. |
| `ledger.py` | Hash-linked append-only JSONL; `verify_chain`. |
| `signing.py` | Ed25519 keypairs, signing the ledger head, verifying signatures. |
| `report.py`, `cli.py` | `report.md` / `report.json` / JUnit; `run`, `verify`, `diff`, `keygen`, `sign`. |

## Decisions

**The cassette keys on (request, repeat).** Repeats of a case send identical requests. If
the cassette kept one response per request hash, repeat #1 would be replayed N times and
every recorded flip would disappear. Keying on arrival order fixes that but only while the
executor is sequential. So the repeat index is part of the key: the executor opens a
`slot(k)` per (case, repeat) unit, and every request inside it — the target call and the
judge call it feeds — is stored and replayed under that k. Replays are therefore identical
at any concurrency, and a cassette file is order-independent. (0.1.x wrote arrival-ordered
lists; those files cannot be converted after the fact, because which repeat produced a
response is exactly what they do not record.)

**Concurrency is bounded and result-preserving.** `--concurrency` runs whole (case, repeat)
units in a thread pool, since the work is I/O-bound. Scores are assembled by index, never
by completion order, so the report, the aggregate and the sealed hash do not depend on
scheduling. `Cassette` is lock-guarded; the lock is released across the network call.

**Retries cover transient failures only.** 408/409/425/429 and 5xx, plus connection errors,
are retried with exponential backoff capped at 30s, preferring the provider's `Retry-After`.
A 400 or 401 is not retried: repeating a malformed or unauthorized request cannot help.
Because every response is cassette-backed as it arrives, an exhausted retry is resumable —
re-run the same command and recording continues where it stopped.

**Only the effective request is hashed.** The cassette key is the URL plus the JSON body
actually sent. Unset parameters are left out of the body entirely, so "temperature not
set" and "temperature=1.0" are different requests. API keys never enter the key or the
cassette.

**Provenance is checked on every response.** A served-model mismatch on call 37 matters
even when call 100 looks clean. The manifest records the first response's metadata, and
drift warnings flag any change in served model or system fingerprint during the run.

**Dated snapshots are not mismatches.** Requesting `gpt-4o-mini` and being served
`gpt-4o-mini-2024-07-18` is the provider resolving an alias. The snapshot is still stored
in `served_model`. Any other difference produces a warning.

**Judge verdict parsing is strict.** The first standalone `PASS`/`FAIL` token decides, and
anything else counts as FAIL. A plain substring check would read "FAIL — does not PASS" as
a pass.

**A schema change is named, not mistaken for tampering.** A record's hash covers its whole
content, so adding a manifest field changes the hash of everything sealed before it.
`SCHEMA_VERSION` is stored in each record, and `verify` reports an older schema as exactly
that instead of raising a false tamper alarm.

**Answer extraction is simple on purpose.** `answer_match` prefers an explicit `####` or
`\boxed{}` marker, then "the answer is <number>", then the last number in the reply. A
cleverer extractor would repair sloppy model output and hide variance behind its own
guesswork — the tool would then be measuring the extractor. The phrase pattern demands a
number precisely because "answer" also occurs in ordinary prose, which an early version
mis-captured.

**A suite file is data, not a language.** It supplies the same options the flags do, with
paths resolved relative to itself, and rejects unknown keys so a typo fails loudly. There
are no expressions, no includes, and no inheritance: a config language would become a
second thing to test.

**Exit codes separate outcomes.** `run` exits 3 when any case is UNSTABLE, which is
different from 1 (error) and 2 (usage). CI can then accept an expected unstable demo while
still failing on a broken replay.

## Known limits

- **Small-N bootstrap CIs are approximate.** With N=5 binary verdicts the bootstrap
  distribution is coarse (steps of 0.2) and tends to under-cover. The CI's seed is fixed,
  so the interval itself is reproducible, but it is still only an approximation.
- **Flip rate needs N ≥ 5 to mean much.** At N=3 a single dissent already reads as 33%.
  Use a larger N on the cases you care about.
- **Stability thresholds are conventions.** `BORDERLINE_MAX_FLIP = 0.20` is a named
  constant chosen for auditability, not a statistically derived cutoff.
- **The `diff` noise floor is conservative.** It uses the widest per-case CI half-width
  across both runs. That rarely claims a false "REAL CHANGE", but it will miss small real
  shifts in the aggregate. A paired test across cases would be more powerful.
- **Float scores use a median-crossing pseudo-flip.** This gives a variance signal
  without a threshold, but it is a heuristic. All built-in scorers are binary.
- **Identical prompts in different cases share cassette entries.** The key is the request
  plus the repeat, not the case id. Two cases with the same prompt therefore replay the
  same response for the same repeat, which understates variance between them. Dataset
  loading rejects duplicate case ids, not duplicate prompts.
- **Concurrency changes rate-limit behaviour, not results.** More workers in flight means
  more 429s on a rate-limited tier, which retries absorb by waiting. If a provider is the
  bottleneck, lower `--concurrency` rather than raising retries.
- **Signing covers the ledger, not the world.** A signature binds a key to a chain head.
  It does not timestamp (a signer can backdate), does not prove the provider's responses
  were genuine, and does not help if the private key leaks. There is no revocation,
  no key rotation record, and no notarization against an external log.
- **`answer_match` grades the final answer only.** Correct reasoning with a mistyped final
  number scores zero, and a lucky guess scores one. That is the usual benchmark convention,
  not a claim about reasoning quality.
- **Canonical hosts are an allowlist.** Any self-hosted or gateway endpoint gets a
  NON-CANONICAL warning by design. The warning means "provenance unverified", not "wrong".
