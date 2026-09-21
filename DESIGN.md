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
| `analyze.py` | Pure functions: Wilson / bootstrap CI, flip rate, stability class. No I/O. |
| `paired.py` | Exact McNemar, paired permutation, cluster bootstrap. Pure functions. |
| `power.py` | Simulated power: how many items or repeats a difference would need. |
| `decompose.py` | Two-arm experiment separating judge variance from target variance. |
| `adapters/recording.py` | Cassette keyed by SHA-256 of the effective request + repeat index. |
| `adapters/target.py` | `Target` protocol; `LocalCallableTarget` (tests), `OpenAICompatibleTarget`. |
| `adapters/scorer.py` | Exact, regex, and LLM-judge scorers. The judge is a `Target`. |
| `executor.py` | N-run loop (optionally concurrent), provenance capture and gap warnings. |
| `ledger.py` | Hash-linked append-only JSONL; `verify_chain`. |
| `signing.py` | Ed25519 keypairs, signing the ledger head, verifying signatures. |
| `policy.py` | Policy files: thresholds, critical cases, drift rules against a baseline. |
| `report.py`, `htmlreport.py`, `cli.py` | Markdown / JSON / JUnit / HTML; the commands. |

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

**Two runs of one suite are paired, and the old threshold was not a threshold.** Until
2.0 `diff` compared the suite-level mean difference against the widest *per-case* Wilson
half-width and called it a noise floor. Those describe different things: at N=5 a perfect
item spans [0.57, 1.00], so the bar sat at 0.217, while a mean over 40 items is far
better determined than any item in it. The rule therefore reported almost every real
shift as "within noise", which reads as reassurance. Both runs cover the same items, so
the comparison is paired: an exact McNemar test on the items that changed majority
verdict, and a cluster bootstrap that resamples whole items with all their repeats
attached. Resampling individual repeats would treat one item's repeats as independent
observations of the suite and give an interval that is too narrow. The old quantity
survives as `per_case_halfwidth`, which is what it measures.

**The verdict is never "no difference".** A test that does not reach significance has
not shown two runs are the same; it has failed to show they differ, which at N=5 is the
usual outcome. `diff` says "inconclusive" and `evalseal power` answers the question that
follows: at least six items must change verdict in the same direction before any paired
comparison can be significant at alpha 0.05, because 2 x 0.5^6 = 0.031 and
2 x 0.5^5 = 0.063. A suite where fewer than six items can move cannot produce a
significant result at any number of repeats.

**Stability is decided by an interval, not by a flip-rate cutoff.** The old rule called a
flip rate of 0 STABLE, up to 0.20 BORDERLINE, and more UNSTABLE. At N=5 the only
reachable flip rates are 0, 0.2 and 0.4, so "at most 20%" was never a tolerance band - it
was the single outcome "one of five runs disagreed", dressed as a percentage that invites
reading it as a 20% error budget. Classes now follow the Wilson interval of the pass
proportion: UNSTABLE when it contains 0.5, so the direction is not established; STABLE
when it excludes 0.5 and every run agreed; BORDERLINE in between. A visible consequence
is that three unanimous runs no longer count as STABLE - 3/3 gives [0.44, 1.00], which is
what you would also expect from an item that passes 70% of the time.

**Comparing two suites cannot attribute variance.** The README once argued from a
judge-graded suite flipping on 5 of 20 cases and an arithmetic-graded suite flipping on
none of 40 that the variance came from the judge. The suites differ in grader *and* in
task, so the comparison is confounded. `evalseal decompose` runs the experiment that
separates them on one suite: one arm samples the target once and judges that fixed
response N times, the other samples the target N times and judges each once. Each arm
records to its own cassette, because one shared file would let them collide whenever the
target's first response equals a later one.

**Binary verdicts get a Wilson interval, not a bootstrap.** Resampling five identical
verdicts only ever produces the same value, so the bootstrap reports a width-zero interval
and every interval collapses to certainty the data cannot support. The Wilson
score interval stays well-defined at the boundary (5/5 gives roughly [0.57, 1.00], in line
with the rule of three, which bounds an unseen failure rate near 3/n). Float scores keep
the seeded bootstrap, since no closed form fits an arbitrary score distribution. Both are
deterministic, so re-running the analysis reproduces the interval exactly.

**The ledger lock covers the whole read-modify-append, not just the write.** Locking only
the append leaves the race intact: two processes read the same head, both validate it, and
both append. The head is therefore read inside the lock, and the record is re-pointed at
the live head when the caller asks for it (`relink=True`, which `evalseal run` uses) so
concurrent runs serialise instead of failing. The lock is an open file descriptor rather
than the existence of a lock file, so a process that dies mid-write releases it when the OS
closes its descriptors — no stale lock to reap, no timeout to tune. It coordinates
processes on **one machine**; networked filesystems are out of scope.

**The receipt seals how the answer was judged.** A tamper-evident score is not worth much
if the rubric changed quietly, so the manifest carries the judge prompt hash as well as the
rubric hash — the rubric is what a user edits, the prompt is what the model saw — plus the
scorer type, judge model and parameters, dataset and suite hashes, code commit and
environment. `config_fingerprint` hashes exactly the parts that decide whether two runs
measure the same thing, which is what `diff` and `gate --expect-config` compare. The prompt
itself is stored only behind `--store-judge-prompt`, because it embeds case text that may
be private.

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

**Normalization covers presentation, never content.** `answer_match` strips surrounding
backticks and quotes before comparing, because a model writing `` `ValueError` `` gave the
same answer in code formatting — scoring it wrong turns a formatting habit into fake model
variance, which is exactly what happened in the codeqa suite. The line is that decoration
around a value is removed and the value itself is never rewritten: a near-miss identifier
still scores zero.

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

- **Small-N intervals are wide, and should be.** At N=5 even a perfectly stable case spans
  [0.57, 1.00]. That is the honest width for five observations, not a defect — but it means
  small differences cannot be called at small N. Raise N when you need that power.
- **Wilson assumes independent runs.** Repeats of one case are treated as independent
  Bernoulli trials. Provider-side caching, a sticky backend, or a rate-limit retry serving
  a repeated response would break that assumption and narrow the interval unfairly.
- **Flip rate needs N ≥ 5 to mean much.** At N=3 a single dissent already reads as 33%.
  Use a larger N on the cases you care about.
- **BORDERLINE is unreachable at N=5.** Stability now follows the Wilson interval of the
  pass proportion rather than a flip-rate cutoff, and five runs can only produce
  "unanimous" (5/5 or 0/5, whose intervals clear 0.5) or "not established". The middle
  class needs a larger N to exist. That is what five observations support, not a gap in
  the rule.
- **A paired test cannot see a sub-majority shift.** McNemar works on items that change
  majority verdict. A change that lowers every item's pass rate from 0.90 to 0.85 moves
  the suite mean by 0.05 and flips almost no majorities, so the test reports nothing.
  Worse, raising N makes this *harder* to see, because extra repeats sharpen each item
  toward its own majority and remove the discordance the test feeds on. `diff` reports
  the disagreement when the bootstrap interval excludes zero and McNemar does not agree,
  rather than resolving it silently.
- **Power is estimated on the test, not on the full rule.** `evalseal power` simulates
  the McNemar criterion. `diff` additionally requires the bootstrap interval to exclude
  zero, which is slightly stricter, so reported power is a mild upper bound.
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
