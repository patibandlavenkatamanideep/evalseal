"""Check that an agent artifact is the shape it claims to be, before sealing it.

## What this does, and the line it does not cross

EvalSeal **binds** agent artifacts; it does not produce them, and it does not run or
replay agents. It has no tool loop, no planner and no sandbox. This module adds exactly
one thing to that: when a run seals a file under an agent role, the file is checked
against the schema in `docs/agent-eval-receipts.md` first.

That is shape validation, not truth validation. It catches a malformed manifest, a
duplicated call key, a `denied` call carrying a response digest, an `allowed` tool the
manifest never defines. It cannot catch a harness that reports tool calls it never made,
and nothing here should be read as saying otherwise.

## Why validate at all, rather than just hashing

Because a digest over a broken file is a digest over a broken file. Sealing it produces
a receipt that verifies perfectly and describes an environment nobody can reconstruct -
the receipt would be sound and useless at the same time, which is the worst outcome for
a tool whose value is that its receipts mean something.

So this fails closed, like the `external` artifact rule: if a file is sealed under an
agent role and does not match the schema, the run stops and names every problem, rather
than binding it and leaving the reader to discover the breakage later.
"""

from __future__ import annotations

import json
from pathlib import Path

TOOL_MANIFEST_SCHEMA = "evalseal.agent-tools/1"
TOOL_RESPONSES_SCHEMA = "evalseal.agent-tool-responses/1"

RESPONSE_MODES = ("live", "recorded", "mocked", "external")
OUTCOMES = ("ok", "denied", "error")

# Roles that mean "this is an agent artifact, hold it to the schema". A role outside
# this set is an opaque file: its digest is sealed and its content is not this module's
# business, which is what keeps `--artifact` general.
MANIFEST_ROLES = ("tool_acl", "tool_manifest")
RESPONSES_ROLES = ("tool_responses", "tool_calls")
AGENT_ROLES = (*MANIFEST_ROLES, *RESPONSES_ROLES)


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    hex_part = value.removeprefix("sha256:")
    return len(hex_part) == 64 and all(c in "0123456789abcdef" for c in hex_part.lower())


def _check_mode(data: dict, problems: list[str]) -> None:
    mode = data.get("response_mode")
    if mode is None:
        problems.append("missing `response_mode`; one of " + ", ".join(RESPONSE_MODES))
    elif mode not in RESPONSE_MODES:
        problems.append(
            f"`response_mode` is {mode!r}; expected one of {', '.join(RESPONSE_MODES)}")


def validate_tool_manifest(data: object) -> list[str]:
    """Problems with a tool manifest, in the order a reader would fix them."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return [f"expected a JSON object, got {type(data).__name__}"]

    if data.get("schema") != TOOL_MANIFEST_SCHEMA:
        problems.append(
            f"`schema` is {data.get('schema')!r}; expected {TOOL_MANIFEST_SCHEMA!r}")
    _check_mode(data, problems)

    tools = data.get("tools")
    defined: set[str] = set()
    if tools is None:
        problems.append("missing `tools`")
    elif not isinstance(tools, list):
        problems.append("`tools` must be a list")
    else:
        for i, tool in enumerate(tools):
            if not isinstance(tool, dict):
                problems.append(f"tools[{i}] must be an object")
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                problems.append(f"tools[{i}] has no `name`")
                continue
            if name in defined:
                problems.append(f"tools[{i}] repeats the name {name!r}")
            defined.add(name)

    allowed = data.get("allowed")
    if allowed is None:
        problems.append("missing `allowed`")
    elif not isinstance(allowed, list):
        problems.append("`allowed` must be a list")
    else:
        for i, entry in enumerate(allowed):
            if not isinstance(entry, dict):
                problems.append(f"allowed[{i}] must be an object")
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                problems.append(f"allowed[{i}] has no `name`")
            elif defined and name not in defined:
                # An allowed tool with no definition is the gap that makes an ACL
                # unreadable: nothing says what the agent was actually permitted to do.
                problems.append(
                    f"allowed[{i}] permits {name!r}, which `tools` does not define")
            if not _is_digest(entry.get("definition_sha256")):
                problems.append(
                    f"allowed[{i}] has no valid `definition_sha256` (sha256:<64 hex>)")

    denied = data.get("denied", [])
    if not isinstance(denied, list) or not all(isinstance(d, str) for d in denied):
        problems.append("`denied` must be a list of tool names")
    else:
        both = sorted(set(denied) & {e.get("name") for e in allowed or []
                                    if isinstance(e, dict)})
        if both:
            problems.append(
                f"{', '.join(both)} appear in both `allowed` and `denied`; "
                "the ACL contradicts itself")
    return problems


def validate_tool_responses(data: object) -> list[str]:
    """Problems with a frozen-response file."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return [f"expected a JSON object, got {type(data).__name__}"]

    if data.get("schema") != TOOL_RESPONSES_SCHEMA:
        problems.append(
            f"`schema` is {data.get('schema')!r}; expected {TOOL_RESPONSES_SCHEMA!r}")
    _check_mode(data, problems)

    if "manifest_sha256" in data and not _is_digest(data["manifest_sha256"]):
        problems.append("`manifest_sha256` is not a sha256:<64 hex> digest")

    calls = data.get("calls")
    if calls is None:
        problems.append("missing `calls`")
        return problems
    if not isinstance(calls, list):
        return [*problems, "`calls` must be a list"]

    seen: set[tuple] = set()
    for i, call in enumerate(calls):
        if not isinstance(call, dict):
            problems.append(f"calls[{i}] must be an object")
            continue
        where = f"calls[{i}]"
        for field in ("case_id", "tool"):
            if not isinstance(call.get(field), str) or not call[field]:
                problems.append(f"{where} has no `{field}`")
        for field in ("repeat", "seq"):
            if not isinstance(call.get(field), int) or isinstance(call.get(field), bool):
                problems.append(f"{where} has no integer `{field}`")

        key = (call.get("case_id"), call.get("repeat"), call.get("seq"))
        if all(k is not None for k in key):
            if key in seen:
                # The key is what makes a concurrent run produce the same file as a
                # serial one. Two calls sharing it means one of them is unreachable.
                problems.append(
                    f"{where} repeats the key (case_id={key[0]!r}, repeat={key[1]}, "
                    f"seq={key[2]}); keys must be unique")
            seen.add(key)

        if not _is_digest(call.get("input_sha256")):
            problems.append(f"{where} has no valid `input_sha256`")

        outcome = call.get("outcome")
        if outcome not in OUTCOMES:
            problems.append(
                f"{where} has `outcome` {outcome!r}; expected one of "
                f"{', '.join(OUTCOMES)}")
            continue
        response = call.get("response_sha256", "__missing__")
        if outcome == "ok":
            if not _is_digest(response):
                problems.append(
                    f"{where} succeeded but has no valid `response_sha256`")
        elif response not in (None, "__missing__") and not _is_digest(response):
            problems.append(f"{where} has an invalid `response_sha256`")
        elif outcome == "denied" and _is_digest(response):
            # A blocked call that carries a response digest is describing two
            # different events, and a reader cannot tell which one happened.
            problems.append(
                f"{where} was denied but carries a `response_sha256`; a blocked call "
                "returned nothing")
    return problems


def validate_agent_artifact(role: str, path: str | Path) -> list[str]:
    """Validate one file against the schema its role implies.

    Returns an empty list for a role this module does not claim - `--artifact` stays
    general, and only the documented agent roles are held to a schema.
    """
    if role not in AGENT_ROLES:
        return []
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        # Unreadable is not this module's failure to report: `run` seals it as
        # `external` with a note, and a pre-registration decides whether that is
        # acceptable. Saying nothing here keeps one rule in one place.
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [f"not valid JSON ({e.msg} at line {e.lineno} column {e.colno})"]

    if role in MANIFEST_ROLES:
        return validate_tool_manifest(data)
    return validate_tool_responses(data)


__all__ = [
    "AGENT_ROLES", "OUTCOMES", "RESPONSE_MODES", "TOOL_MANIFEST_SCHEMA",
    "TOOL_RESPONSES_SCHEMA", "validate_agent_artifact", "validate_tool_manifest",
    "validate_tool_responses",
]
