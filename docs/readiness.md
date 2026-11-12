# Readiness: what to depend on, and what not to

One row per capability. **Rising coverage is not readiness** — a thing is ready when the
claim it makes is one you could defend to someone who disagreed with you, and the row
says what would have to happen for it to move up.

The levels:

| | meaning |
|---|---|
| **Depend on it** | tested, used in this repository's own CI, and the failure modes are documented. If it breaks, that is a bug and not a surprise |
| **Use with the caveat** | works, and there is something about it you have to know. The caveat is in the row |
| **Try it** | implemented and exercised, by one person, against fixtures they made. Nobody else has used it |
| **Not ready** | present but incomplete, or complete and unproven in a way that matters |
| **Deliberately absent** | asked for, decided against, with the reason |

Last reviewed at `826ce0d`, against 1446 tests on Linux, macOS and Windows, Python
3.11–3.13.

## Sealing and verification

| capability | level | what the level rests on |
|---|---|---|
| Hash chain over records | **Depend on it** | Every record re-hashed under the schema that sealed it; nine schema versions verify. Tamper, break, sibling and unsealed-record cases are each tested |
| Schema compatibility (1.0 … 1.9) | **Depend on it** | The strip table is asserted field by field; a receipt is never rewritten, and a version this build does not know is refused rather than guessed at |
| Artifact binding (`verify --artifacts`) | **Depend on it** | Dataset, suite, cassette snapshot and imported export. Edit any of them and verification exits 1, with the chain still verifying separately |
| Ed25519 signing (`keygen`, `sign`) | **Depend on it** | Round trip tested; key creation is owner-only, exclusive and symlink-refusing, with a descriptor-leak test on the failure path |
| Recovery from an interrupted append (`recover`) | **Depend on it** | The condition is defined narrowly, the sidecar is written before the truncation, and the whole thing is under the ledger lock |
| Local anchors (`anchor`, `anchor-verify`) | **Use with the caveat** | `created_at` is the machine's own clock. It binds a receipt to its ledger head and signature state; it establishes nothing about *when* |
| RFC 3161 timestamps | **Not ready** | The message imprint is checked offline and the authority's signature is verified only when you supply `--tsa-ca`. Without it the output says *TSA signature NOT verified by EvalSeal*, and that is the honest reading. No recorded real-TSA chain-validation fixture, because its certificate expires on a date nobody chose |
| CI provenance | **Use with the caveat** | `CIProvenance.claimed` is an environment variable. `GITHUB_ACTIONS=true evalseal run` produces a receipt claiming CI. It catches accidents, not intent |

## Measurement

| capability | level | what the level rests on |
|---|---|---|
| Cassette replay (format 3) | **Depend on it** | Concurrent and serial replay agree per measurement; exclusive writer ownership is enforced and tested on Windows as well as POSIX |
| Cassette format 2 | **Use with the caveat** | Readable, not appendable. Two cases asking one question share a draw; every run touching one gets a receipt warning |
| `answer_match` scoring | **Use with the caveat** | The answer format is declared and anything outside it is unscorable rather than guessed. The declared format is narrower than what models emit — a multi-word answer needs a marker |
| LLM-judge scoring | **Use with the caveat** | A judge reply with no verdict token is unscorable, not a FAIL. The judge's own grading code is EvalSeal's, so it *is* fingerprinted — unlike an import |
| Ungraded observations | **Depend on it** | Counted, kept in the score denominator, gateable, and distinct from failures in every rendering |
| Independence of repeats | **Use with the caveat** | Established from cassette entry keys for receipts sealed at 1.8+. Everything older, and every import, reports `unknown` — which is the answer, not a gap to fill |
| Execution provenance | **Depend on it** | Per-call origin, digests, attempts and truncation, with the summary derived from the calls so it cannot disagree with them |

## Statistics

| capability | level | what the level rests on |
|---|---|---|
| Paired comparison (`diff`) | **Depend on it** | Exact McNemar with integer arithmetic at any count, cluster bootstrap over cases, verdict never "no difference". An independent oracle checks the p-value for every count up to 13 each way |
| Interval labelling | **Depend on it** | Derived from `alpha`; `ci95` survives as a compatibility alias carrying the same numbers |
| Regression budget | **Use with the caveat** | Three states are named. **The default mode passes on `insufficient_evidence`** — it says so in the detail, and `require_evidence_within_budget` asks the other question. A repository that never sets it keeps the permissive reading |
| Drift attribution (`drift`) | **Use with the caveat** | A cause is named only on the grounds in [metrics.md §10](metrics.md), and `cannot_isolate` is a first-class answer. It can only see what the receipt records: a proxy that rewrites responses without changing a recorded field still reads as `judge_variance` |
| `power` | **Use with the caveat** | The power for the majority-verdict test and nothing else. A continuous-scored receipt is refused rather than answered. There is no sensitivity figure at all for a permutation test |
| `evidence` | **Use with the caveat** | Retrospective precision per case. The labels are cut on the pass-rate margin while policies gate on the flip-rate margin; both are named everywhere, and a reader skimming one and setting the other is comparing two widths |
| `decompose` | **Use with the caveat** | Two measured flip rates and their ratio. It apportions nothing, because the design cannot — which is less useful than a decomposition and is what the evidence supports |

## Operations

| capability | level | what the level rests on |
|---|---|---|
| Response and deadline bounds | **Depend on it** | Enforced on decoded bytes, with metered decompression; driven against a loopback server rather than a mock |
| Pattern refusal | **Use with the caveat** | The nested-quantifier family is refused. **Overlapping alternation is not**, is exponential, and no static check decides it in general — so a scorer config from an untrusted source can still spin a job |
| File locking | **Use with the caveat** | One machine, one local filesystem. On NFS or SMB the guarantees weaken or vanish, and nothing detects that you are on one |
| Memory | **Use with the caveat** | Linear at ~2.5 KiB per observation, measured and documented. Nothing bounds it; a 100k-observation run will use what the table says |
| Key rotation | **Use with the caveat** | A documented five-step procedure, not a command, because every verifier has to be told |
| The PR workflow | **Use with the caveat** | Executed step by step in CI with fault injection for every required check. **For fork pull requests, read [the trust boundary](pr-receipt-workflow.md#the-fork-trust-boundary) first** |
| Packaging | **Depend on it** | The built wheel is installed outside the repository and exercised on Linux, macOS and Windows; the release waits for lint, audit, tests and that check |
| Declared dependency bounds | **Depend on it** | Each installed at exactly its lower bound with the suite run against it. `typer>=0.12` was wrong and never worked; this lane is why that is known |

## Integration

| capability | level | what the level rests on |
|---|---|---|
| promptfoo import | **Try it** | One adapter, read against a real 0.125.1 export. Every field it cannot fill is named in the receipt. **Nobody outside this repository has used it**, and the maintainer's demo is not adoption — [the pilot checklist](../examples/integrations/promptfoo/README.md#a-pilot-checklist) exists to change that |
| Comparing two imports | **Depend on it** — to refuse | They are never comparable, two imports missing the same field are not thereby comparable, and the reason names the field |
| Any other tool | **Deliberately absent** | One adapter exists. A second should decide what is shared; a framework written against one example fits one example |
| A universal results format | **Deliberately absent** | Same reason, with more surface area |

## Deliberately absent

| | why |
|---|---|
| A hosted service, dashboard, or trace database | Not what this is. A receipt that needs a server to be read is not a receipt |
| More model providers | Two wire formats cover what has been needed. A third should follow a use, not precede one |
| An agent framework | Out of scope, stated in ROADMAP |
| Automatic re-pinning of a fingerprint | A tool that updates a pin when it stops matching has removed the pin |
| Compaction of a ledger | It would rewrite the chain. Split by ledger instead |

## What this project does not claim, at any level

A sealed receipt binds bytes to a record. It does **not** establish that the run
happened as described, that the inputs were honest, that no other run was made and
discarded, or that a provider served what it said it served. Every level above is a
level of *verification*, and verification is not execution. See
[verification-policy.md](verification-policy.md).
