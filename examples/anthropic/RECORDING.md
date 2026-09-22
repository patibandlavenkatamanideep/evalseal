# Recording the Anthropic example

The native Anthropic adapter is covered by tests against the documented Messages API wire
format, but **no live Anthropic run has been recorded in this repository yet**. Every
other provider claim in the README is backed by a committed cassette; this one is not,
and the README says so. This page is the procedure for closing that gap. It has not been
run; when it is, the result replaces this paragraph.

## What gets recorded

`suite.json` in this directory: the first 10 problems of `examples/gsm8k/dataset.jsonl`,
graded by `answer_match`, against `claude-haiku-4-5-20251001` with `max_tokens` 512 and
`temperature` 0.0, five repeats each.

That is **50 API calls**, each capped at 512 output tokens. Check current pricing for the
model before running; the cost is set mostly by how long the replies are.

Ten items is deliberately small. It is enough to show the adapter end to end - served
model, truncation reporting, flip rates - and not enough to compare models: `evalseal
power` puts the floor for any significant paired result at six items changing verdict,
so this suite is an example, not a benchmark.

## Record it

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # never commit this; .env is gitignored
EVALSEAL_RECORD=1 evalseal run --suite examples/anthropic/suite.json \
  --ledger .evalseal/anthropic.jsonl --html anthropic-receipt.html
```

Responses are written to `tests/cassettes/anthropic.json` as they arrive, and each save
replaces the file atomically, so an interrupted run keeps what it recorded. If the API
rate-limits you, run the same command again: recorded responses are reused and recording
continues where it stopped.

## What to expect

- An aggregate line: 10 cases, a mean score, and stable / borderline / unstable counts.
  At N=5 a case can only be STABLE (5/5 or 0/5) or UNSTABLE; BORDERLINE needs more runs.
- **No** `TEMPERATURE NOT SET` warning, because the target sets it explicitly.
- **No** `NON-CANONICAL ENDPOINT` warning: `api.anthropic.com` is a canonical host.
- `system_fingerprint` of `None` in the receipt. Anthropic publishes no backend
  identifier, and EvalSeal reports none rather than inventing one.
- A `RESPONSE TRUNCATED` warning **if** any reply hit 512 tokens. If it appears, those
  answers are incomplete rather than wrong: raise `max_tokens` in `target.json` and record
  again, rather than reading anything into the score.
- A served-model mismatch warning only if the API serves a different model than the one
  requested.

Then confirm the recording replays with no key:

```bash
unset ANTHROPIC_API_KEY
evalseal run --suite examples/anthropic/suite.json --ledger .evalseal/replay.jsonl
evalseal verify --ledger .evalseal/replay.jsonl
```

The replay must reproduce the same verdicts. If it reports a missing cassette entry, the
recording is incomplete; record again with the key.

## Before committing the cassette

A cassette holds the full request bodies and response envelopes, so it contains the
prompts and the model's replies in plain text. It never holds the API key: keys go in a
request header, and headers are not recorded. Check both before committing:

```bash
grep -c "sk-ant-" tests/cassettes/anthropic.json    # must print 0
python -m json.tool tests/cassettes/anthropic.json | head -40   # read what you are publishing
```

These prompts are public GSM8K problems, so the cassette is safe to commit. **For a
private dataset it would not be**: keep that cassette out of a public repository and in
private artifact storage instead. The receipt stays shareable either way, because it
carries only hashes of the dataset, rubric and judge prompt template, never the text.
Keep the cassette somewhere durable all the same: a receipt's hashes can only be matched
against artifacts that still exist.

## After recording

1. Commit `tests/cassettes/anthropic.json`.
2. Add a replay step for this suite to `.github/workflows/ci.yml`, beside the GSM8K and
   code-QA replays, so CI checks it on every push.
3. Replace the "not yet backed by a recorded live run" status in the README's
   *Two providers, two wire formats* section with the recorded result, quoting only
   numbers the replay produces.
4. Note the recording in `CHANGELOG.md`, with the date and model.
