# Running EvalSeal: limits, keys, recovery, retention

Everything numeric on this page was measured by `scripts/measure_limits.py` on one
machine, named below. **None of it is an SLA.** Run the script yourself and the numbers
will differ; the shapes — linear in observations, flat in concurrency for a replay —
will not.

```
macOS-26.2-arm64-arm-64bit-Mach-O, Python 3.13.2, arm
```

## Limits that are enforced

| limit | default | where | what happens past it |
|---|---|---|---|
| bytes read from one provider response, **after decompression** | 8 MiB | `--max-response-bytes`, suite `max_response_bytes` | `ResponseTooLarge`, nothing parsed |
| bytes read from one timestamp-authority response | 1 MiB | `Rfc3161Backend(max_bytes=…)` | the anchor is refused, nothing recorded |
| content encodings accepted | `gzip, deflate` | `transport.ACCEPT_ENCODING` | `UnsupportedEncoding`: an encoding this client cannot meter is refused rather than inflated |
| wall clock for one provider call, **including retries and backoff** | derived: `timeout × (retries + 1) + Σ backoff` = 391 s at the defaults | `--deadline` | `DeadlineExceeded`, naming what to change |
| retries per call | 5, on 429/408/5xx and connection errors | `--max-retries` | the last response's status is raised |
| grading patterns | nested unbounded quantifiers refused | scorer config and `RegexScorer` | `UnsafePattern` before the run starts |

The response limit is not a guess: the largest response in any cassette committed to
this repository is under 40 KB, which is 0.5% of the default. The limit exists for the
HTML login page a proxy returns and the misconfigured endpoint, not for a long answer.

## Limits that are not enforced, with their measured cost

### Memory is linear in observations

| cases | observations | peak | per observation | receipt |
|---|---|---|---|---|
| 40 | 200 | 0.6 MiB | 2.9 KiB | 2.5 KB/case |
| 200 | 1 000 | 2.5 MiB | 2.6 KiB | 2.4 KB/case |
| 1 000 | 5 000 | 12.4 MiB | 2.5 KiB | 2.4 KB/case |

A run holds every measurement until the record is sealed, because the receipt carries
per-call provenance for all of them — that is the point of the execution manifest, and
streaming it away would mean a receipt that cannot say what each call did. So **budget
about 2.5 KiB per observation**: a 10 000-case suite at five repeats is roughly 125 MiB
and a 120 MB receipt. Nothing refuses a larger run; it will simply use what the table
says it will.

The receipt is the part to watch. At 2.4 KB per case a large suite produces a file that
is awkward to attach to a pull request long before memory is a problem.

### Concurrency changes wall clock, not memory

200 cases × 5 repeats, replayed:

| workers | seconds | peak |
|---|---|---|
| 1 | 0.105 | 2.5 MiB |
| 4 | 0.117 | 2.6 MiB |
| 16 | 0.118 | 2.6 MiB |

A replay is CPU-bound and single-threaded in Python, so concurrency buys nothing and
costs nothing there. Against a live provider it is the only thing that matters, and the
in-flight bound is exactly `--concurrency`: that many requests, each capped at
`--max-response-bytes`. Peak transfer memory is therefore `concurrency ×
max_response_bytes` in the worst case — 32 MiB at the defaults.

### Verifying a ledger is linear and cheap

| records | seconds | per record | file |
|---|---|---|---|
| 10 | 0.001 | 0.12 ms | 0.07 MiB |
| 50 | 0.006 | 0.11 ms | 0.37 MiB |
| 200 | 0.024 | 0.12 ms | 1.46 MiB |

0.12 ms per record, so verification is never the slow part. The file grows at about
7 KB per record for a five-case suite; see **Retention** below.

### Grading cost depends on anchoring, not just on length

One `RegexScorer` match against one response body:

| body | anchored (`^\s*42\s*$`) | scanning (`42`) |
|---|---|---|
| 64 KiB | 0.001 ms | 0.016 ms |
| 1 MiB | 0.000 ms | 0.249 ms |
| 8 MiB | 0.000 ms | 1.981 ms |

An anchored pattern fails at the first character whatever the body's length. An
unanchored one scans all of it, at roughly 0.25 ms per MiB. Both are negligible beside
a provider call; the number is here so that "the grader is free" is not assumed for a
pattern that is not.

## Filesystem guarantees, and where they stop

EvalSeal coordinates processes on **one machine, one local filesystem**.

| it does | it does not |
|---|---|
| serialise ledger appends and signature appends with `fcntl.flock` (POSIX) or `msvcrt.locking` (Windows) on a `.lock` sibling | work across NFS, SMB, or any networked filesystem — advisory locks there are weaker or absent, and EvalSeal attempts no distributed consensus |
| hold a cassette exclusively for the life of a run, refusing a second writer by name | coordinate two machines writing one shared directory |
| write the ledger, signatures and snapshots with one `write` per line and an `fsync` inside the lock | survive a filesystem that reorders or discards an fsynced write |
| rename a temp file over the cassette, so a crash leaves the previous version whole | protect against another process editing a file it does not hold the lock for |

Two runs in one directory: the ledger serialises, and the cassette does not — the second
run is refused with the path and the holding pid. Give each run its own `--cassette`, or
let them queue.

Per-run output goes to `runs/<timestamp>-<session>/`; `./report.json` and `./report.md`
are aliases for the most recent one. Two concurrent runs therefore keep their own
directories and race only over the aliases.

## Permissions

| file | mode | why |
|---|---|---|
| private key | `0600`, set **at creation** with `O_CREAT|O_EXCL|O_NOFOLLOW` | it was created at `0644` and narrowed afterwards, which left a window where any local user could read it |
| public key | the process umask | it is public; it is still never overwritten without `--overwrite-public` |
| ledger, signatures, cassettes, receipts | the process umask | they hold prompts and responses, so a directory with the privacy you need is the control, not a mode on each file |

On Windows there are no POSIX modes and `O_NOFOLLOW` does not exist. `O_EXCL` is still
honoured, so exclusive creation works; the file is as private as its directory, and
`keygen` says so when it runs there.

## Key rotation

A signature says *this key signed a ledger whose head was H*. Rotating a key means every
verifier has to be told, which is the step people skip, so the procedure is deliberately
not one command.

1. **Generate the new key at a new path.** `evalseal keygen --private-key
   evalseal-2026.key --public-key evalseal-2026.pub`. The private key path must not
   exist; replacing one in place invalidates nothing and proves nothing, it just leaves
   two keys with the same name in two places.
2. **Publish the new public key beside the old one.** Both, until step 5. A verifier
   pinned to one key reports the other's signatures as untrusted, which is a correct
   answer to the wrong question.
3. **Sign with the new key.** `evalseal sign --ledger … --key evalseal-2026.key`
   appends; it does not replace. A ledger can carry signatures from several keys, and
   `verify --public-key` checks the one you name.
4. **Tell the verifiers.** Update `trusted_public_keys` in any pre-registration and
   `--trusted-key` in any gate. A pre-registration that requires a signature from a key
   nobody holds any more fails closed, which is the behaviour you want and still a
   failure someone has to fix.
5. **Stop publishing the old public key** once nothing needs to verify a signature made
   with it. Keep the key itself for as long as you keep the ledgers it signed: deleting
   it does not invalidate those signatures, but it does mean nobody can ever make
   another one that matches.

Replacing a public key file on purpose takes `--overwrite-public`. There is no flag for
replacing a private key: delete it yourself, having decided to.

## Recovering an interrupted write

A process killed between one record's newline and the next leaves a partial line at the
end of the ledger. Every record before it is intact, and until 2.3 nothing could read
any of them — the whole file came back as *"line N is not valid JSON"*.

```
$ evalseal verify --ledger .evalseal/pr.jsonl
Cannot read the ledger: .evalseal/pr.jsonl ends with an incomplete record: 12 complete
record(s) followed by 843 byte(s) that are not a whole line. ...
Run `evalseal recover --ledger .evalseal/pr.jsonl` to move the partial tail aside and
keep the complete history. Nothing is deleted: the bytes are written to a sidecar first.

$ evalseal recover --ledger .evalseal/pr.jsonl
843 byte(s) -> .evalseal/pr.jsonl.incomplete-20261111T120000Z
12 complete record(s) kept in .evalseal/pr.jsonl.
ok   Chain intact: 12 record(s).
```

The partial bytes are written to a sidecar and fsynced **before** the ledger is
truncated, under the ledger lock. It asks first unless given `--yes`, because truncating
a ledger changes tamper-evidence and a tool that quietly drops a trailing line is a tool
that can be made to drop a real one.

`recover` only ever removes an incomplete **final** line. Damage anywhere else is not an
interrupted append, is not recoverable this way, and is reported as what it is.

## Retention

Nothing expires anything. What to keep, and why:

| artifact | keep | because |
|---|---|---|
| the ledger | as long as you may need to verify any receipt in it | each record's hash commits to its predecessor; dropping a record breaks the chain for every record after it |
| cassette snapshots (`runs/*/…snapshot-*.json`) | as long as the receipt that binds them | they are the responses the scores came from. Without them a later run that re-draws the live cassette leaves nothing able to reproduce the receipt |
| the live cassette | as long as you want to resume or replay | it grows; snapshots do not |
| `runs/<id>/` | as long as the pull request, at least | about 2.4 KB per case for the receipt, plus the snapshot |
| private keys | as long as the ledgers they signed | see rotation above |
| `*.incomplete-*` sidecars | until you have looked at one | they are the only copy of what `recover` removed |

A ledger grows by roughly 7 KB per record for a five-case suite, and a receipt by about
2.4 KB per case. Splitting by ledger — one per suite, or one per branch — is the
supported way to keep any single file small; there is no compaction, because compaction
would rewrite the chain.

## Troubleshooting

| symptom | cause | what to do |
|---|---|---|
| `cassette … is already being written by another evalseal process (held by pid N)` | one cassette, two runs. Enforced, because both would rewrite the whole file and the last would win | give this run its own `--cassette`, or wait |
| `ends with an incomplete record` | an interrupted append | `evalseal recover` (above) |
| `TAMPER DETECTED at record N` | the content does not hash to its stored hash | the record was edited. `verify` names the record; the ledger is not repairable by design |
| `carries no hash at all` | a receipt that never went through `seal_and_append` | it is unsealed, not altered. Seal it, or stop treating it as a receipt |
| `ResponseTooLarge … declared a … byte response` | not a chat completion. Usually a proxy, a login page or the wrong `--base-url` | check the endpoint before raising the limit |
| `content-encoding 'br'` refused | the peer used an encoding this client cannot bound | nothing to configure; the request asks only for gzip and deflate |
| `ran out of time: … total budget spent` | the total deadline, not the per-attempt timeout | raise `--deadline` or lower `--max-retries` |
| `nests an unbounded quantifier` | a grading pattern that can take exponential time | rewrite it without the nesting, or use `answer_match` |
| `UNSCORABLE OBSERVATIONS: N of M` | the grader could not read N replies. Not model failures | read `unscorable_reason`; often `max_tokens` or a judge returning errors |
| `origin: replay … not a fresh measurement` | the numbers came from a cassette | correct for a regression check, useless as a claim about the model today |
| `Independence unknown` | a receipt sealed before schema 1.8 | nothing to fix; it cannot say whether its repeats were separate draws, and says so |
| `evalseal` not found after install | the console script is not on `PATH` | `python -m evalseal.cli` works identically |

## What none of this establishes

A limit enforced here bounds what EvalSeal will *read*, not what a provider *did*. A
lock serialises this tool's own writes, not another program's. An fsync asks the
filesystem to persist; it does not audit it. And none of it makes a signature evidence
that a run happened as described — see [verification-policy.md](verification-policy.md).
