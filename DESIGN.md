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
can build a new valid chain, though. Anchoring the head hash somewhere external
(signing, notarization) is out of scope for v0.

## Components

| module | role |
|---|---|
| `analyze.py` | Pure functions: seeded bootstrap CI, flip rate, stability class. No I/O. |
| `adapters/recording.py` | Cassette keyed by the SHA-256 of the effective request body. |
| `adapters/target.py` | `Target` protocol; `LocalCallableTarget` (tests), `OpenAICompatibleTarget`. |
| `adapters/scorer.py` | Exact, regex, and LLM-judge scorers. The judge is a `Target`. |
| `executor.py` | N-run loop, provenance capture, provenance-gap warnings. |
| `ledger.py` | Hash-linked append-only JSONL; `verify_chain`. |
| `report.py`, `cli.py` | `report.md` / `report.json`; `run`, `verify`, `diff`. |

## Decisions

**The cassette stores responses as ordered lists.** Repeats of a case send identical
requests. If the cassette kept one response per request hash, repeat #1 would be replayed
N times and every recorded flip would disappear. Instead, the k-th identical call gets the
k-th recorded response. This depends on calls happening in the same order in record and
replay mode, which holds because the executor is sequential.

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
- **Canonical hosts are an allowlist.** Any self-hosted or gateway endpoint gets a
  NON-CANONICAL warning by design. The warning means "provenance unverified", not "wrong".
