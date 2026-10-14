# Recording a real RFC 3161 response

Every RFC 3161 test in this repository runs against `tests/tsa_fixture.py`, an authority
built in-process. That is the right default: it is offline, deterministic, and it can be
asked to misbehave, which is how the twelve failure modes are tested at all.

What it cannot tell us is whether the parser handles what the *world* emits. Real
authorities differ in field order, in whether they embed the full chain, in signature
algorithm, and in how much of the optional TSTInfo they populate. **No live timestamp
response has been recorded here yet.** This page is the procedure; it has not been run.

`tests/test_real_tsa_fixture.py` already consumes these files and **skips while they are
absent**, so the moment they land the test runs. Nothing else needs changing.

## What gets recorded

Three files in this directory:

| file | what it is |
|---|---|
| `response.tsr` | the DER `TimeStampResp` exactly as the authority returned it |
| `ca.pem` | the authority's root or issuing certificate, however they publish it |
| `recorded.json` | the digest submitted, the authority URL, and the date recorded |

Nothing here is secret. A timestamp response contains a signed statement about a digest
and the authority's own certificate. The request carries only a 32-byte digest, so
nothing about any eval leaves the machine — a test asserts the request body stays under
128 bytes for exactly this reason.

## Choosing an authority

Any public RFC 3161 service that needs no account. At the time of writing, `freetsa.org`
and the TSAs operated by several CAs qualify; check current terms before relying on one.

Two things to prefer:

- **Publishes its certificate**, so `ca.pem` can be fetched rather than extracted.
- **Returns the signing certificate in the token** (`certReq` is set by default in
  EvalSeal's request), so verification needs no out-of-band lookup.

EvalSeal ships no default authority, and this fixture does not create one. It is a test
input, not a recommendation.

## Record it

```bash
# 1. Any sealed receipt will do; the demo bundle is already here.
D=examples/verification-demo

# 2. Submit the anchor's subject digest. This is the one network call.
evalseal anchor $D/report.json --ledger $D/ledger.jsonl \
  --with rfc3161 --tsa https://<authority>/tsr \
  --out /tmp/real-anchor.json

# 3. Pull the token out of the proof, as DER.
python3 -c "
import base64, json, pathlib
proof = json.load(open('/tmp/real-anchor.json'))['external_proofs'][0]
pathlib.Path('tests/fixtures/tsa/response.tsr').write_bytes(
    base64.b64decode(proof['proof']))
print('authority :', proof['authority'])
print('asserted  :', proof['attested_time'])
print('digest    :', proof['subject_digest'])
"

# 4. Fetch the authority's certificate, however they publish it.
curl -so tests/fixtures/tsa/ca.pem https://<authority>/files/cacert.pem
```

Then write `recorded.json` with the three values step 3 printed, plus today's date.

## ⚠️ The pinned verification time — read this before committing

**A signing certificate expires.** If the test verified "as of now", it would pass today
and start failing on a date nobody chose, with a message about a certificate validity
window that tells a future reader nothing about why their build broke.

So verification is checked **as of the token's own `genTime`**, which `verify_token`
already does — see `_validity_check`, which says so in its docstring, and
`test_validity_is_checked_against_gen_time_not_the_clock_now`, which pins it.

The consuming test asserts the same thing again, locally, so the guarantee is visible
where the fixture is used and not only where the code is written.

One consequence to accept knowingly: this fixture will eventually be a token signed by a
long-expired certificate, and that is **fine and intended**. It is testing that the
parser and the signature check work, not that the certificate is currently valid.

## Before committing

```bash
# The response must be DER, not an HTML error page.
python3 -c "
import pathlib
b = pathlib.Path('tests/fixtures/tsa/response.tsr').read_bytes()
assert b[:1] == b'\\x30', 'not a DER SEQUENCE - did the authority return an error page?'
print('DER, %d bytes' % len(b))
"

# It must verify against the certificate, as of its own genTime.
python3 -c "
import json, pathlib
from evalseal.rfc3161 import verify_token
meta = json.load(open('tests/fixtures/tsa/recorded.json'))
result = verify_token(pathlib.Path('tests/fixtures/tsa/response.tsr').read_bytes(),
                      meta['subject_digest'],
                      ca_pem=pathlib.Path('tests/fixtures/tsa/ca.pem').read_bytes())
print(result.state, '|', result.boundary)
for c in result.checks:
    print(' ', 'ok  ' if c.passed else ('skip' if not c.ran else 'FAIL'), c.name)
"
```

`state` must be `verified`. If the chain check fails, `ca.pem` is probably the root while
the token was signed by an intermediate — EvalSeal does **no path building**, by design,
so supply the issuing certificate directly.

These files are binary-ish fixtures whose bytes are load-bearing. `.gitattributes`
already marks `tests/fixtures/**` as `-text`, so a Windows checkout will not rewrite
them; without that, every digest in them would break.

## After recording

1. Commit `response.tsr`, `ca.pem` and `recorded.json`.
2. `pytest tests/test_real_tsa_fixture.py -v` — the skips become passes.
3. Note it in `CHANGELOG.md`, with the authority and the date.
4. Tick the second box of **#14** in `ROADMAP.md`.
