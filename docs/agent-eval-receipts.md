# Agent eval receipts

> **What EvalSeal does here today:** it seals the files that define an agent's
> environment into the receipt, by digest, and re-checks them on verification.
>
> It also **checks the shape** of those files before sealing them, against the schema
> below, and refuses the run if they do not match.
>
> **What it does not do:** run or replay agents. EvalSeal calls a target and scores the
> reply. It has no tool loop, no planner and no sandbox, and this page does not pretend
> otherwise. If your harness produces the two artifacts described below, EvalSeal can
> make them part of a receipt that a third party can check. Producing them is your
> harness's job.
>
> Validation is **shape, not truth**. It catches a malformed manifest, a duplicated call
> key, a `denied` call carrying a response digest, an `allowed` tool the manifest never
> defines. It cannot catch a harness that reports tool calls it never made.

## Why the ACL is not enough

The objection that produced this page:

> *"For agent evals, sealing tool ACL is not enough. Mocked/frozen tool responses must
> be sealed too."*

That is correct, and the reason is worth stating precisely.

A tool ACL says *which tools the agent was allowed to call*. For a deterministic replay
that is only half the environment. The other half is *what those tools returned*. Two
runs with a byte-identical ACL can diverge completely:

- `search_docs` returns a different top result because the index was rebuilt.
- `get_order` returns `in_transit` on Monday and `delivered` on Thursday.
- A mock was edited to return a friendlier value after a failing run.

In every case the ACL is unchanged, the agent's behaviour is not, and a receipt that
seals only the ACL reports two incomparable runs as comparable. It is the same class of
bug as sealing the judge model's name but not the rubric: the identifier stayed the
same while the instrument moved.

So an agent receipt needs both:

| artifact | answers | role |
|---|---|---|
| tool manifest + ACL | which tools existed, with what signatures, and which were denied | `tool_acl` |
| frozen tool responses | what each call returned, per case and per repeat | `tool_responses` |

## Sealing them

Any file can be bound into a receipt by digest:

```bash
evalseal run --suite agent-suite.json \
  --artifact tool_acl=examples/agent/tool-manifest.json \
  --artifact tool_responses=examples/agent/tool-responses.json
```

The receipt then carries them in `manifest.artifacts`, with the `kind` field saying
what it holds - `hashed` for a digest, `external` when the file was named but not
readable at seal time. `evalseal verify --artifacts` re-hashes both and reports `ok`,
`changed`, `missing` or `unverifiable`:

```console
$ evalseal verify --ledger .evalseal/ledger.jsonl --artifacts
Chain intact: 1 record(s).
ok           tool_acl: examples/agent/tool-manifest.json matches the sealed digest
changed      tool_responses: examples/agent/tool-responses.json does not match the
             sealed digest
```

A changed mock is now a failed check rather than a silent difference in the score.

## The artifact schema

Both files are plain JSON, defined here rather than by EvalSeal code, because EvalSeal
binds them rather than producing them. Digests are SHA-256 over the canonical JSON
form: sorted keys, no whitespace (`json.dumps(obj, sort_keys=True,
separators=(",", ":"))`), which is the same convention the receipt itself uses.

### `tool-manifest.json` — the ACL

```json
{
  "schema": "evalseal.agent-tools/1",
  "response_mode": "recorded",
  "runtime": {
    "framework": "example-agent-runner",
    "version": "0.3.1",
    "note": "Not an EvalSeal component."
  },
  "allowed": [
    {"name": "search_docs", "definition_sha256": "sha256:…"},
    {"name": "get_order", "definition_sha256": "sha256:…"}
  ],
  "denied": ["shell.exec", "http.post", "fs.write"],
  "tools": [
    {
      "name": "search_docs",
      "description": "Full-text search over the product documentation.",
      "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                     "required": ["query"]}
    }
  ]
}
```

`definition_sha256` covers the whole tool definition, so an edited description or a
changed parameter schema is visible even though the tool's name did not move. A
renamed-but-identical tool and a same-named-but-rewritten tool are different events and
should not look alike.

### `tool-responses.json` — the frozen environment

```json
{
  "schema": "evalseal.agent-tool-responses/1",
  "response_mode": "recorded",
  "recorded_at": "2026-09-23T00:00:00+00:00",
  "manifest_sha256": "sha256:…",
  "calls": [
    {
      "case_id": "a01", "repeat": 0, "seq": 0,
      "tool": "search_docs",
      "input_sha256": "sha256:…",
      "response_sha256": "sha256:…",
      "outcome": "ok"
    },
    {
      "case_id": "a01", "repeat": 0, "seq": 2,
      "tool": "shell.exec",
      "input_sha256": "sha256:…",
      "response_sha256": null,
      "outcome": "denied",
      "denied_by": "tool_manifest.denied"
    }
  ]
}
```

Field by field:

- **`case_id` + `repeat` + `seq`** key a call the way EvalSeal's own cassettes key a
  request: by *(request, repeat)* rather than arrival order, so a concurrent run
  produces the same file as a serial one.
- **`input_sha256`** is the digest of the arguments. It is what makes "the agent asked
  a different question" visible when the response was the same.
- **`response_sha256`** is the digest of what came back, `null` when nothing did.
- **`outcome`** is `ok`, `denied` or `error`. A blocked call is data, not an absence:
  an agent that tried `shell.exec` and was stopped behaved differently from one that
  never tried.
- **`manifest_sha256`** binds the responses to the ACL they were recorded under, so the
  pair cannot be mixed and matched.
- **`response_mode`** is one of:

| mode | meaning | reproducible |
|---|---|---|
| `live` | tools hit real systems during the run | no |
| `recorded` | responses captured from a live run and replayed since | yes |
| `mocked` | responses written by hand | yes |
| `external` | responses came from a system the receipt cannot reach | no |

Digests do not carry the response bodies. Including a `response` field verbatim is
allowed, but it is the same decision as committing a cassette: fine for public
fixtures, not fine for a private dataset. The [threat model](../THREAT_MODEL.md) and
[docs/pre-registration.md](pre-registration.md) both apply here unchanged - a receipt
carries hashes, and matching a hash later needs someone to have kept the file.

A worked pair of files is in [examples/agent/](../examples/agent/), and
`tests/test_agent_artifacts.py` checks that every digest in them is internally
consistent.

### Validation, and what it refuses

Sealing a file under one of the agent roles - `tool_acl`, `tool_manifest`,
`tool_responses`, `tool_calls` - holds it to the schema. A file under any other role is
opaque: its digest is sealed and its content is nobody's business, which is what keeps
`--artifact` general.

Validation **fails closed**, for the same reason an `external` artifact does: a digest
over a broken file produces a receipt that verifies perfectly and describes an
environment nobody can reconstruct. That receipt would be sound and useless at once.

```console
$ evalseal run --suite agent-suite.json --artifact tool_acl=tools.json
Error: 2 problem(s) in the agent artifact(s) you are sealing:
  tool_acl: `response_mode` is 'teleported'; expected one of live, recorded, mocked, external
  tool_acl: allowed[0] permits 'ghost', which `tools` does not define
The schema is in docs/agent-eval-receipts.md. EvalSeal checks the shape of these files,
not whether the agent really made these calls.
```

What it checks: the `schema` string, a `response_mode` from the four above, every tool
having a name and a valid `definition_sha256`, no tool both allowed and denied, no
duplicate tool names, unique `(case_id, repeat, seq)` call keys, a valid `input_sha256`
on every call, a `response_sha256` on every `ok` call and none on a `denied` one, and a
well-formed `manifest_sha256` when present.

The API is available to a harness that wants to check its own output before writing it:

```python
from evalseal.agent_artifacts import validate_tool_manifest, validate_tool_responses

problems = validate_tool_manifest(manifest)   # [] when it is well formed
```

## Mapping an existing harness onto this

None of these are integrations EvalSeal ships. They are the shape of the adapter you
would write, roughly fifty lines each.

**Kitaru-style record/replay.** A replay harness already holds the pair this schema
wants: a tool registry and a recorded transcript. Emit the registry as `allowed` +
`tools`, walk the transcript emitting one `calls` entry per tool invocation with
`response_mode: "recorded"`, and pass both with `--artifact`. The per-call digests come
from the same canonical-JSON helper.

**Langfuse / Braintrust traces.** Both store spans with inputs and outputs. Filter the
trace to tool spans, map span name to `tool`, span input to `input_sha256`, span output
to `response_sha256`, and the trace's parent id to `case_id`. What their UIs cannot do
is prove the trace was not edited afterwards; sealing its digest into a receipt is
precisely the gap this closes. Keep the trace itself - a digest without the artifact
proves nothing later.

**OpenAI / Anthropic tool-use loops written by hand.** The assistant message's
`tool_calls` give you `tool` and arguments; the following tool message gives the
response. One `calls` entry per pair.

## What this still does not establish

- **That the agent actually called those tools.** The file says what a harness reported.
  EvalSeal binds it; it did not observe it.
- **That a `live` run is reproducible.** It is not, by definition, and the mode field
  says so rather than hiding it.
- **That a tool behaved correctly.** A recorded response that was wrong when it was
  recorded seals exactly as cleanly as a right one.
- **That the ACL was enforced.** `denied` is a claim by the harness that enforces it.

What it does establish is the thing that was missing: two agent runs whose ACL *and*
frozen responses hash identically had the same environment, and a receipt whose
`tool_responses` digest no longer matches has had that environment changed underneath
it. Plus, now, that the files were the shape they claimed when they were sealed.

**EvalSeal stays a binder.** Validating a schema is not executing an agent, and it is
not evidence about one. If EvalSeal ever does replay agents, that will be a separate
decision announced as one, not something inferred from this page.
