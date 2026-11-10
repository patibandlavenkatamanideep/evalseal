# ADR 0002 — One writer owns a cassette; each receipt owns a snapshot

- **Status:** accepted
- **Date:** 2026-11-10
- **Context:** a cassette holds the provider responses a receipt's scores were computed
  from. Two questions had no stated answer, and the code's unstated answer to both was
  wrong:
  1. What happens when **two writers** target one cassette file?
  2. What happens to an **earlier receipt** when a later run writes to the cassette it
     was sealed against?

## 1. What went wrong before this decision

Both are reproduced in `tests/test_cassette_durability.py`; both reproductions are
written against the behaviour this ADR replaces.

**Two writers.** Every `Cassette` kept the whole entry table in memory and rewrote the
whole file after each recorded response. Two writers on one path therefore each held a
*different* idea of the file, and whichever wrote last erased the other's entries:

```
after writer A : 1 entries
after writer B : 1 entries
A's entry survived: False
A replays       : NO -> No cassette entry for repeat 0 of request 7f24486a3603
```

Writer A's response was admitted, acknowledged, and then silently deleted by a process
that never knew A existed. A receipt sealed by A could no longer be replayed.

**A later run.** A receipt binds the cassette by digest (schema 1.4). The digest is of
a file later runs are free to rewrite, so:

- an honest later run that only *adds* repeats turned the earlier receipt's artifact
  check from `ok` to `changed`: a red verification failure with nothing tampered. A
  check that cries wolf on normal use is a check people turn off.
- a `fresh` re-draw writes the *same keys* with new responses. It destroyed the earlier
  run's responses outright. Replaying that receipt's configuration afterwards produced
  mean 0.0 against a receipt that says 1.0, and no copy of the original bytes existed
  anywhere.

## 2. Decision: exclusive ownership, enforced

**A cassette has one writer at a time.** A `Cassette` in `resume` or `fresh` mode takes
a non-blocking exclusive advisory lock on `<cassette>.lock` and holds it until it is
closed. A second writer **fails immediately** with `CassetteBusy`, naming the path, the
holding pid when it can be read, and what to do instead.

Readers (`replay` mode) take no lock. Replay never writes, `os.replace` makes every
visible version of the file complete, and a CI job that only replays must not be
blockable by a recording run.

### Why not the alternatives

**Append journal** (each writer appends its own entries; a reader merges them). It
satisfies "retain all admitted entries", but it retains them *unattributed*: two
processes recording into one file means two experiments' draws interleaved, and nothing
afterwards can say which run admitted which entry, or which of two divergent responses
for one measurement belongs to which receipt. C2a established that one measurement has
one answer; a journal reintroduces the ambiguity one layer down. It also needs
compaction, and compaction rewrites the file — which is the operation this ADR exists to
constrain.

**Transactional local storage** (SQLite). Correct, and more machinery than the problem
needs. It would also replace a file a human can read and `git diff` with a binary one,
and the cassette's reviewability is a large part of why it is a file at all.

**Exclusive ownership** is what the project already does for the ledger
(`locking.py`, `ledger_lock`), so there is one locking story rather than two.

### Why non-blocking, not a queue

Waiting would be defensible for the ledger's short append, which is why `ledger_lock`
blocks. A cassette is held for a whole run — minutes to hours. A second run that waits
silently looks hung, and when it finally got the lock it would be recording into a
cassette whose contents changed underneath its own earlier decisions. Failing in the
first second with an actionable message is the smaller harm.

### What the lock does not do

It is a **local advisory** lock, with the same scope as `locking.py`: `fcntl.flock` on
POSIX, `msvcrt.locking` on Windows. On NFS, SMB or any networked filesystem the
guarantee is weaker or absent. Two runs on two machines sharing a cassette over a
network mount are **not** protected, and EvalSeal does not attempt distributed
consensus. It also does not stop a process that does not use EvalSeal from editing the
file; that is what the digest in the receipt is for.

## 3. Decision: each receipt binds an immutable snapshot

At the end of a run, the cassette is **finalized**: the exact entry table the run used
is serialized and written to a **content-addressed** file,
`<cassette dir>/snapshots/<stem>-<digest>.json`, and that file is bound into the receipt
as the artifact role `cassette_snapshot`.

Three properties follow:

- **Future appends cannot invalidate it.** The name contains the digest of the content,
  so a later run with different entries writes a *different* file. Nothing overwrites a
  snapshot; writing one that already exists with the same bytes is a no-op, and writing
  different bytes to an existing snapshot path is an error rather than a replacement.
- **It is replayable on its own.** A snapshot is a cassette file:
  `Cassette(snapshot, mode="replay")` reproduces the run it was finalized from, even
  after the live cassette has been re-drawn. That is the property the earlier
  reproduction lost.
- **Runs that used identical entries share one file.** The content address deduplicates,
  so replaying a suite N times costs one snapshot, not N.

A snapshot keeps the format it came from. A format-2 cassette finalizes to a format-2
snapshot, which stays replayable under the rules that wrote it, and keeps carrying the
format-2 warning into the receipt.

### The live cassette stays bound, as a mutable artifact

The `cassette` role is still sealed, because "which file did this come from" is worth
recording. It is now marked `kind="mutable"`, and `check_artifacts` reports a mismatch
on a mutable artifact as **`moved`**, not `changed`:

> this file is appended to by later runs, so a mismatch here is expected and is not
> evidence of tampering; the immutable copy is the `cassette_snapshot` artifact

`verify --artifacts` exits non-zero on `changed` and not on `moved`. The distinction is
the one invariant 2 asks for: *the file has moved on* and *the bytes behind this receipt
were altered* are different claims, and only the second is a tamper signal. A `changed`
on the snapshot is a tamper signal, and still fails.

## 4. What this does **not** establish

A snapshot is the set of responses this receipt's scores were computed from, as
recorded. It is not evidence that those responses came from the model named, that they
were obtained when the run says, or that no other run happened. A recorded response is
a recording; the signature over the receipt covers the recording, not its truthfulness.

## 5. Consequences

- `evalseal run` writes a `snapshots/` directory beside the cassette. `--no-snapshot`
  turns it off, for a caller who does not want the copy; the receipt then says so by
  having no `cassette_snapshot` artifact, rather than by binding something weaker.
- Two concurrent `evalseal run` invocations that share a cassette path: the second
  exits 1 with `CassetteBusy`. This is a behaviour change — it used to lose data
  instead.
- `Cassette` is now closeable and usable as a context manager. Code that drops one on
  the floor still works: the lock is released when the object is collected and the
  descriptor closed.
- `ArtifactKind` gains `mutable` and `ArtifactCheck.status` gains `moved`. Both live in
  fields that exist since schema 1.4, so no schema version change is needed and no
  older receipt changes meaning. Receipts sealed before this build have no
  `cassette_snapshot` and a `hashed` cassette; they verify exactly as they did.
