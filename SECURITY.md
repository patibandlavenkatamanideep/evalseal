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

## What a cassette does contain

Prompts, model responses, and provider metadata. If your dataset contains sensitive or
personal data, the cassette will too. Treat a committed cassette as public, and review one
before committing it.

## Trust boundaries

- **The ledger is tamper-evident, not tamper-proof.** It detects edits to past records;
  it does not stop someone who can rewrite the whole file from building a new valid chain.
  There is no signing yet, so a ledger proves integrity only to someone who trusts its
  source. See DESIGN.md.
- **A non-canonical endpoint is flagged, not blocked.** Pointing the tool at a proxy is
  allowed and reported as a provenance warning; a proxy can return whatever it likes.
- **Releases are published from a pinned GitHub Actions workflow** via PyPI trusted
  publishing, so no long-lived PyPI token exists to leak.
