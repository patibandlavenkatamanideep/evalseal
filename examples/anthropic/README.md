# Anthropic, natively

`provider: "anthropic"` speaks the Messages API directly rather than through an
OpenAI-compatible shim. The difference matters to a receipt: `max_tokens` is required
rather than optional, the system prompt is a top-level field rather than a message, the
reply is a list of content blocks rather than a string, there is no `system_fingerprint`
to pin a backend with, and an overload is HTTP 529 rather than 503.

**Status: no live run recorded yet.** The adapter is tested against the documented wire
format; [RECORDING.md](RECORDING.md) is the procedure for recording the small example
suite here (`suite.json`, 10 GSM8K problems, 50 calls):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
EVALSEAL_RECORD=1 evalseal run --suite examples/anthropic/suite.json
```

Replay afterwards needs no key, like every other cassette in this repo.

A judge can use it too, through `judge_provider` in the scorer config:

```json
{
  "type": "llm_judge",
  "judge_provider": "anthropic",
  "judge_model": "claude-haiku-4-5-20251001",
  "rubric": "Answer PASS if the response is correct, FAIL otherwise."
}
```

## Two things this adapter reports that a shim cannot

**`system_fingerprint` is `None`, and stays `None`.** Anthropic publishes no backend
identifier. Reporting nothing is honest; deriving one from the model name would fake a
pin that does not exist, and `diff` would then claim two runs came from the same backend
on no evidence.

**A reply cut off at `max_tokens` is reported as truncated.** Scored naively an
incomplete answer is indistinguishable from a wrong one, which turns a configuration
mistake into a finding about the model. The run warns instead:

```
TARGET RESPONSE TRUNCATED: 3 of 100 call(s) hit the token limit. Those answers are
incomplete, not incorrect; raise max_tokens before reading anything into the score.
```

The same warning now covers the OpenAI shape, where `finish_reason: "length"` means the
same thing.
