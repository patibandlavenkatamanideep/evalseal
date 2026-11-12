# Security

## Reporting a vulnerability

Report privately through
[GitHub security advisories](https://github.com/patibandlavenkatamanideep/evalseal/security/advisories/new).
Please don't open a public issue for a vulnerability. Expect an initial response within a
week.

## How evalseal handles your credentials

- **API keys are read from the environment**, or from a `.env` file that is gitignored.
  A key is used only in the `Authorization` header of the request to the endpoint you
  configured.
- **Keys are never written to a cassette.** Cassettes store request bodies and provider
  responses; auth headers are not part of the recorded request, and there is a test for it.
- **Replay needs no key at all.** That is the point of committing a cassette: CI reproduces
  a run with no credentials present.

## How evalseal handles your signing key

- **A private key is created owner-only, at creation.** `O_CREAT|O_EXCL|O_NOFOLLOW` with
  mode `0600`, so it is never briefly readable by other local users, is never written
  through a symbolic link planted at its path, and cannot be created twice by two racing
  processes. Before 2.3 it was written at the umask's mode and narrowed afterwards.
- **Neither key file is overwritten by accident.** Replacing a private key invalidates
  every signature made with it; replacing a public key makes a pinned verifier report
  every signature as untrusted. `--overwrite-public` is the explicit way to rotate, and
  there is no flag for replacing a private key.
- **Windows has no POSIX modes.** Exclusive creation still works; the file is as private
  as the directory holding it, and `keygen` says so there.
- **Rotation is a procedure, not a command**, because every verifier has to be told. See
  [docs/operations.md](docs/operations.md#key-rotation).

## What a cassette does contain

Prompts, model responses, and provider metadata. If your dataset contains sensitive or
personal data, the cassette will too. Treat a committed cassette as public, and review one
before committing it.

## Trust boundaries

- **The ledger is tamper-evident, not tamper-proof.** It detects edits to past records;
  it does not stop someone who can rewrite the whole file from building a new valid
  chain. Signing the head closes that gap **for anyone holding the public key** — the
  head commits to every record before it — and closes nothing for anyone who does not.
  What a signature establishes, and the four other assurance levels around it, is set out
  in [docs/verification-policy.md](docs/verification-policy.md). What it does **not**
  establish: that the run happened as described, that the inputs were honest, or that no
  other run was made and discarded. A signer can sign a record containing whatever they
  like.
- **A remote peer cannot exhaust this process's memory.** One response is read under a
  byte limit on *decoded* bytes, inflated under a cap so a compression ratio cannot
  decide how much memory is used, and the whole call is bounded by a total deadline. An
  encoding this client cannot meter is refused rather than inflated. Measured limits are
  in [docs/operations.md](docs/operations.md).
- **A grading pattern from an untrusted config cannot hang a job.** Scorer files can
  arrive from a fork in a pull-request workflow, and Python's regex engine has no
  timeout: `^(a+)+$` takes 25 seconds on 31 characters. Nested unbounded quantifiers are
  refused before a run starts. This is a restriction, not a proof of safety — the
  limits of it are stated in `src/evalseal/patterns.py`.
- **A non-canonical endpoint is flagged, not blocked.** Pointing the tool at a proxy is
  allowed and reported as a provenance warning; a proxy can return whatever it likes.
- **Releases are published from a pinned GitHub Actions workflow** via PyPI trusted
  publishing, so no long-lived PyPI token exists to leak.
