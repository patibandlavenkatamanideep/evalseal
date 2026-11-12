# The verification contract

What a receipt establishes, who has to be trusted for each claim, and what every check
does when it cannot run. One page, because the questions a reviewer actually has are
*can I act on this* and *who am I trusting*, and the answers were spread across six
documents and a dozen error messages.

## Assurance levels

A receipt is not verified or unverified. It sits at one of five levels, and each one buys
strictly more than the one above it. **The level is a property of what was checked, never
of how convincing the output looks.**

| level | established by | who must be trusted | what it rules out |
|---|---|---|---|
| **L0 — parsed** | the file loads as a `RunRecord` under a schema this build knows | the author, entirely | nothing. A hand-written file reaches L0 |
| **L1 — self-consistent** | `evalseal verify`: each record's content hashes to its stored hash, each `prev_hash` matches its predecessor, exactly one record claims GENESIS | the author | an edit made *after* sealing, by anyone who did not rewrite the whole chain |
| **L2 — artifact-bound** | `verify --artifacts`: the dataset, suite and cassette snapshot still hash to what the record sealed | the author | the responses or the questions being swapped after the fact |
| **L3 — attributed** | `verify --public-key K`: a signature over the head, by a key you hold independently | the holder of K, and that K was not shared or stolen | a ledger rebuilt by someone without K |
| **L4 — externally timed** | an anchor carrying a third-party attestation over the receipt digest | the attesting authority, and its trust root | the whole ledger having been produced later than it claims |

Two things are true at every level:

- **No level establishes that the run happened as described.** A signer can sign a record
  containing whatever they like. The chain commits to bytes, not to events.
- **No level establishes that nothing else happened.** A ledger is a record of what was
  appended, not of what was run and discarded. A pre-registration narrows the window;
  it cannot close it.

## Trust roots

| claim | root | how it fails |
|---|---|---|
| this record has not been edited | none — arithmetic | `TAMPER DETECTED at record N` |
| these are the files it was sealed against | none — arithmetic | `changed`, naming the artifact |
| this came from the holder of this key | the key you supply, out of band | `untrusted`: a valid signature by a key that is not the one named |
| this was timestamped | the authority's certificate chain, and the CA you supply with `--tsa-ca` | `not verified`, naming which half was checked |
| this ran in CI | **nothing.** `GITHUB_ACTIONS=true evalseal run` produces a receipt claiming CI | it does not fail; it is a claim, and `CIProvenance.claimed` is its honest name |
| the repeats were independent draws | the cassette entry keys in the receipt | `unknown` for anything sealed before schema 1.8 |

**A trust root is never read from the thing being verified.** A timestamp root carried
inside an anchor would be a root the author chose, so `--tsa-ca` is supplied by the
verifier or the signature is reported as unchecked. The same rule is why
`--public-key` is an argument and not a field.

## Fail-closed rules

Every check obeys one rule: **a check that cannot run is a failure, not a skip.** A
skipped check leaves a green tick standing for work nobody did, which is the failure mode
this whole project exists to remove.

| situation | outcome | why not a skip |
|---|---|---|
| a policy names a drift baseline that is not there | `drift.*` fails | a comparison that did not happen reported as a pass |
| a pre-registration requires a signature and none is present | `prereg.require_signature` fails | the contract asked for attribution |
| a pre-registration requires an anchor and none is passed | `prereg.require_anchor` fails | "no anchor to check" is not "the anchor checked out" |
| a pre-registration requires external evidence and none is supplied | `prereg.require_external_anchor` fails | the clause exists to demand a third party |
| a receipt claims a schema this build does not know | `unsupported_schema`, verification fails | a newer version may have added fields this build cannot see, or changed what one means |
| a receipt carries no hash | `unsealed`, verification fails | nothing can be checked against it. It is *not* reported as tampering: nobody altered it |
| a required contract file is configured and missing | the CI job fails before the replay | deleting the contract in a pull request used to remove it silently |
| `anchor-verify` fails in CI | the job fails, unless `REQUIRE_ANCHOR=false` | it used to print FAILED in the summary and leave the check green |
| an unknown key appears in a policy | the policy is refused | `min_scor: 0.9` would be a threshold that silently checks nothing |
| a grading pattern can take exponential time | the run is refused before it starts | a hung job is a check that never reports |
| the grader could not read a reply | the repeat is `unscorable`, scored 0.0, counted | it used to arrive as a graded FAIL, so a rate-limited judge looked like a failing model |
| a receipt cannot say whether its repeats were separate draws | `run.require_independent_repeats` fails on `unknown`, not only on reuse | a rule that skipped would report a pass for a check that did not happen |

The one deliberate exception is **`drift.max_score_regression` in its default mode**,
which passes on `insufficient_evidence`. That is not a skip — the check ran and says in
words that nothing was established — and `regression_budget_policy:
require_evidence_within_budget` asks the other question. See
[statistical-contract.md](statistical-contract.md) §4.

## What a reviewer should read, in order

1. **Comparability.** A score delta between runs graded differently is not a change.
   `diff` reports it before any number.
2. **Origin.** `replay` means no provider was called. A perfectly good regression check
   and a useless claim about the model's behaviour today.
3. **Independence.** How many separate draws are behind the intervals. `unknown` is an
   answer.
4. **Ungraded observations.** A mean of 0.333 over three cases means something different
   when one of them was never graded.
5. **The gate's checks, passed and failed.** A gate that prints only failures leaves a
   reader unable to tell a clean run from a policy that checked nothing.
6. **The numbers.** Last, because the five above decide what they mean.

## Phrases this project will not print

Each of these was either removed or never allowed, and each has a test:

- "verified" for a timestamp whose signature was not checked — it says *message imprint
  checked, TSA signature NOT verified by EvalSeal*.
- "no difference" for a test that failed to reach significance — it says *inconclusive*,
  and that this is a statement about the experiment.
- "the judge's share of the variance" for a ratio of two flip rates — the design does not
  apportion variance, so the ratio is named as a ratio.
- "tampered" for a file that was never sealed.
- "judge variance" when anything about the target changed — that is `cannot_isolate`.
- "within budget" for a comparison that established nothing.
- Any claim that a signature proves execution, complete history, truthful inputs, or the
  absence of a run that was discarded.
