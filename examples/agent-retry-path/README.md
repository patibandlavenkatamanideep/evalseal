# Same final answer, different path

One case, two recorded attempts. Both end with the note written and the order
delivered, so both score identically. They did not do the same thing.

- **`attempt-a`** called `get_order`, tried `write_note`, got an **error**, retried, and
  succeeded. Three calls, one retry.
- **`attempt-b`** called `get_order` and `write_note` once each. Two calls, no retry.

```bash
python -c "
import json
from evalseal.agent_artifacts import describe_paths
for r in describe_paths(json.load(open('examples/agent-retry-path/tool-responses.json'))):
    print(r['attempt_id'], r['n_calls'], 'calls', r['n_retries'], 'retries',
          r['n_errors'], 'errors', r['path_digest'][:24])
"
```

```
attempt-a  3 calls  1 retries  1 errors  sha256:ec8d392dd7072291b...
attempt-b  2 calls  0 retries  0 errors  sha256:ef522467ba82bd8cb...
```

Different `path_digest`, same outcome. The digest hashes the ordered
`(tool, input_sha256, outcome)` triples, so it differs exactly when the route differed -
including when a call was retried, denied, or reordered.

## Why the ACL is not enough

A tool ACL says what the agent was *allowed* to call. These two attempts ran under a
byte-identical ACL. What differed is the world they met: one hit a failing write and one
did not.

That is why a receipt binds both artifacts:

```bash
evalseal run --suite agent-suite.json \
  --artifact tool_acl=examples/agent-retry-path/tool-manifest.json \
  --artifact tool_responses=examples/agent-retry-path/tool-responses.json
```

`evalseal verify --artifacts` then re-hashes both. Edit a mocked response afterwards and
the check reports `changed` rather than quietly scoring differently.

## The fields that make this visible

Beyond `case_id`, `repeat`, `seq`, `tool`, `input_sha256`, `response_sha256` and
`outcome`, a frozen-response file may carry:

| field | what it distinguishes |
|---|---|
| `attempt_id` | two trajectories through the same case and repeat |
| `retry_index` | a retry from a first try of the same call, `0` for the first |
| `tool_call_id` | the harness's own id, so a call matches the provider's record |

All three are optional, so files written before they existed still validate. When
present they are checked: ids must be unique, a `retry_index` above 0 must have the
attempt it retries recorded, and `attempt_id` joins the uniqueness key because two
attempts each legitimately start at `seq` 0.

What that buys you, concretely:

- **same final state, different path** → different `path_digest`
- **failed write then retry** → `outcome: error` then `retry_index: 1`
- **changed tool response** → different `response_sha256`
- **changed tool call input** → different `input_sha256`
- **changed call order** → different `path_digest`, same set of tools

## The boundary

**EvalSeal validates and binds what the harness records. It does not know whether the
harness omitted calls or lied about calls.**

A harness that forgot to log a call produces a clean digest of an incomplete route, and
nothing here would notice. `path_digest` is a reading aid computed from a file EvalSeal
was handed - it is derived, never sealed, and it is not evidence about the agent.
EvalSeal has no tool loop and does not replay agents.

See [docs/agent-eval-receipts.md](../../docs/agent-eval-receipts.md) for the full schema.
No API key; both files are static fixtures.
