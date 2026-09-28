"""Agent receipts: sealing the tool ACL *and* the frozen tool responses.

EvalSeal does not run agents. What it can do is bind the two files that define an
agent's environment into a receipt and notice when either changes. These tests cover
that, plus the honesty of the claim: `test_docs_do_not_claim_agent_replay` fails if
the page starts implying EvalSeal replays agents.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.agent_artifacts import (
    validate_agent_artifact,
    validate_tool_manifest,
    validate_tool_responses,
)
from evalseal.anchor import check_artifacts
from evalseal.cli import _parse_artifacts, app
from evalseal.executor import run_eval

runner = CliRunner()

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "examples" / "agent" / "tool-manifest.json"
RESPONSES = REPO / "examples" / "agent" / "tool-responses.json"


def _canonical(obj) -> str:
    """The digest convention the schema documents, and the receipt already uses."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _agent_record(tmp_path, acl: Path, responses: Path):
    return run_eval(
        make_dataset("a"), LocalCallableTarget(scripted({"a": ["yes"]})),
        RegexScorer(r"^yes$"), n_repeats=2,
        artifact_paths={"tool_acl": str(acl), "tool_responses": str(responses)})


# --- the example files are real, not illustrative ------------------------------------

def test_the_example_manifest_digests_match_its_own_tool_definitions():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    by_name = {t["name"]: t for t in manifest["tools"]}
    for entry in manifest["allowed"]:
        assert _canonical(by_name[entry["name"]]) == entry["definition_sha256"]


def test_the_example_responses_bind_to_the_example_manifest():
    """The pair cannot be mixed and matched: the responses name the ACL they used."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    responses = json.loads(RESPONSES.read_text(encoding="utf-8"))
    assert responses["manifest_sha256"] == _canonical(manifest)


def test_every_recorded_call_is_keyed_and_digested():
    responses = json.loads(RESPONSES.read_text(encoding="utf-8"))
    seen = set()
    for call in responses["calls"]:
        key = (call["case_id"], call["repeat"], call["seq"])
        assert key not in seen, f"duplicate call key {key}"
        seen.add(key)
        assert call["input_sha256"].startswith("sha256:")
        assert call["outcome"] in {"ok", "denied", "error"}
        if call["outcome"] == "ok":
            assert call["response_sha256"].startswith("sha256:")


def test_a_denied_call_is_recorded_rather_than_omitted():
    """An agent that tried a forbidden tool behaved differently from one that did not."""
    responses = json.loads(RESPONSES.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    denied = [c for c in responses["calls"] if c["outcome"] == "denied"]
    assert denied, "the example must show a blocked call"
    for call in denied:
        assert call["tool"] in manifest["denied"]
        assert call["response_sha256"] is None


def test_the_example_declares_a_reproducible_response_mode():
    for path in (MANIFEST, RESPONSES):
        assert json.loads(path.read_text(encoding="utf-8"))["response_mode"] == "recorded"


# --- sealing and re-checking them -----------------------------------------------------

def test_both_artifacts_are_sealed_into_the_receipt(tmp_path):
    record = _agent_record(tmp_path, MANIFEST, RESPONSES)
    roles = {a.role: a for a in record.manifest.artifacts}
    assert {"tool_acl", "tool_responses"} <= set(roles)
    for role in ("tool_acl", "tool_responses"):
        assert roles[role].kind == "hashed"
        assert roles[role].sha256 == _file_digest(
            MANIFEST if role == "tool_acl" else RESPONSES)


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_an_edited_tool_response_is_reported_as_changed(tmp_path):
    """The point of the whole feature: a mock edited after the fact must not pass."""
    acl = tmp_path / "tools.json"
    responses = tmp_path / "responses.json"
    acl.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    responses.write_text(RESPONSES.read_text(encoding="utf-8"), encoding="utf-8")
    record = _agent_record(tmp_path, acl, responses)

    assert {c.status for c in check_artifacts(record)} == {"ok"}

    edited = json.loads(responses.read_text(encoding="utf-8"))
    edited["calls"][0]["response_sha256"] = "sha256:" + "0" * 64
    responses.write_text(json.dumps(edited, indent=2), encoding="utf-8")

    by_role = {c.role: c for c in check_artifacts(record)}
    assert by_role["tool_responses"].status == "changed"
    assert by_role["tool_acl"].status == "ok"      # the ACL alone would have passed


def test_a_changed_acl_is_reported_even_when_responses_are_untouched(tmp_path):
    acl = tmp_path / "tools.json"
    responses = tmp_path / "responses.json"
    acl.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    responses.write_text(RESPONSES.read_text(encoding="utf-8"), encoding="utf-8")
    record = _agent_record(tmp_path, acl, responses)

    widened = json.loads(acl.read_text(encoding="utf-8"))
    widened["denied"].remove("shell.exec")        # quietly allowing a blocked tool
    acl.write_text(json.dumps(widened, indent=2), encoding="utf-8")

    by_role = {c.role: c for c in check_artifacts(record)}
    assert by_role["tool_acl"].status == "changed"


def test_a_missing_artifact_is_missing_not_passing(tmp_path):
    acl = tmp_path / "tools.json"
    acl.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    record = _agent_record(tmp_path, acl, RESPONSES)
    acl.unlink()
    by_role = {c.role: c for c in check_artifacts(record)}
    assert by_role["tool_acl"].status == "missing"


# --- the CLI flag ------------------------------------------------------------------------

def test_run_seals_artifacts_named_on_the_command_line(tmp_path, monkeypatch):
    """The wiring from `--artifact role=path` through to a sealed, checkable digest."""
    import httpx

    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, json={"model": "m", "choices": [{"message": {"content": "yes"}}]},
        request=httpx.Request("POST", url)))

    (tmp_path / "ds.jsonl").write_text('{"case_id": "a01", "prompt": "p"}\n')
    (tmp_path / "t.json").write_text('{"model": "m"}')
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}')
    ledger = tmp_path / "ledger.jsonl"

    result = runner.invoke(app, [
        "run", "--dataset", str(tmp_path / "ds.jsonl"),
        "--target-config", str(tmp_path / "t.json"),
        "--scorer-config", str(tmp_path / "s.json"),
        "--cassette", str(tmp_path / "c.json"), "--n", "5", "--quiet",
        "--ledger", str(ledger),
        "--artifact", f"tool_acl={MANIFEST}",
        "--artifact", f"tool_responses={RESPONSES}"])
    assert result.exit_code == 0, result.output

    checked = runner.invoke(app, ["verify", "--ledger", str(ledger), "--artifacts"])
    assert checked.exit_code == 0, checked.output
    unwrapped = " ".join(checked.output.split())
    assert "tool_acl" in unwrapped and "tool_responses" in unwrapped


def test_artifact_flag_parses_role_and_path():
    assert _parse_artifacts(["tool_acl=tools.json"]) == {"tool_acl": "tools.json"}
    assert _parse_artifacts([]) == {}


@pytest.mark.parametrize("bad", ["tools.json", "=tools.json", "tool_acl=", ""])
def test_a_malformed_artifact_flag_is_refused_with_an_example(bad):
    import typer

    with pytest.raises(typer.BadParameter) as e:
        _parse_artifacts([bad])
    assert "ROLE=PATH" in str(e.value)
    assert "tool_acl=tools.json" in str(e.value)


def test_a_reserved_role_cannot_be_shadowed():
    """`cassette` already means something in a receipt; two meanings would confuse it."""
    import typer

    with pytest.raises(typer.BadParameter) as e:
        _parse_artifacts(["cassette=other.json"])
    assert "already sealed by the run itself" in str(e.value)


def test_the_same_role_twice_is_refused():
    import typer

    with pytest.raises(typer.BadParameter) as e:
        _parse_artifacts(["tool_acl=a.json", "tool_acl=b.json"])
    assert "given twice" in str(e.value)


# --- the claim --------------------------------------------------------------------------

def test_docs_do_not_claim_agent_replay():
    """EvalSeal has no tool loop. The page must keep saying so."""
    text = (REPO / "docs" / "agent-eval-receipts.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "does not" in lowered and "replay" in lowered
    assert "no tool loop" in lowered
    for overclaim in ("evalseal replays agents", "evalseal runs your agent",
                      "proves the agent called"):
        assert overclaim not in lowered


# --- schema validation: shape, not truth ------------------------------------------------

def _manifest(**over):
    tool = {"name": "search_docs", "description": "d",
            "parameters": {"type": "object", "properties": {}}}
    base = {
        "schema": "evalseal.agent-tools/1",
        "response_mode": "recorded",
        "allowed": [{"name": "search_docs", "definition_sha256": _canonical(tool)}],
        "denied": ["shell.exec"],
        "tools": [tool],
    }
    base.update(over)
    return base


def _responses(**over):
    base = {
        "schema": "evalseal.agent-tool-responses/1",
        "response_mode": "recorded",
        "calls": [{"case_id": "a01", "repeat": 0, "seq": 0, "tool": "search_docs",
                   "input_sha256": _canonical({"query": "q"}),
                   "response_sha256": _canonical({"r": 1}), "outcome": "ok"}],
    }
    base.update(over)
    return base


def test_the_committed_examples_validate_clean():
    assert validate_agent_artifact("tool_acl", MANIFEST) == []
    assert validate_agent_artifact("tool_responses", RESPONSES) == []


def test_a_role_this_module_does_not_claim_is_left_alone(tmp_path):
    """`--artifact` stays general; only the documented agent roles get a schema."""
    junk = tmp_path / "whatever.bin"
    junk.write_text("not json at all", encoding="utf-8")
    assert validate_agent_artifact("cassette", junk) == []
    assert validate_agent_artifact("my_custom_thing", junk) == []


def test_invalid_json_under_an_agent_role_is_reported(tmp_path):
    bad = tmp_path / "tools.json"
    bad.write_text("{oops", encoding="utf-8")
    problems = validate_agent_artifact("tool_acl", bad)
    assert len(problems) == 1 and "not valid JSON" in problems[0]


@pytest.mark.parametrize("over,expected", [
    ({"schema": "something-else"}, "expected 'evalseal.agent-tools/1'"),
    ({"response_mode": "replayed"}, "`response_mode` is 'replayed'"),
    ({"response_mode": None}, "missing `response_mode`"),
    ({"tools": None}, "missing `tools`"),
    ({"allowed": None}, "missing `allowed`"),
    ({"denied": "shell.exec"}, "`denied` must be a list"),
])
def test_a_malformed_manifest_names_the_problem(over, expected):
    problems = validate_tool_manifest(_manifest(**over))
    assert any(expected in p for p in problems), problems


def test_an_allowed_tool_with_no_definition_is_caught():
    """An ACL that permits a tool it never defines says nothing about what was allowed."""
    m = _manifest(allowed=[{"name": "ghost", "definition_sha256": "sha256:" + "a" * 64}])
    problems = validate_tool_manifest(m)
    assert any("permits 'ghost', which `tools` does not define" in p for p in problems)


def test_a_tool_both_allowed_and_denied_is_a_contradiction():
    m = _manifest(denied=["search_docs"])
    problems = validate_tool_manifest(m)
    assert any("appear in both `allowed` and `denied`" in p for p in problems)


def test_a_bad_definition_digest_is_caught():
    m = _manifest(allowed=[{"name": "search_docs", "definition_sha256": "nope"}])
    assert any("definition_sha256" in p for p in validate_tool_manifest(m))


def test_duplicate_tool_names_are_caught():
    tool = {"name": "x", "description": "d", "parameters": {}}
    m = _manifest(tools=[tool, tool],
                  allowed=[{"name": "x", "definition_sha256": _canonical(tool)}])
    assert any("repeats the name" in p for p in validate_tool_manifest(m))


@pytest.mark.parametrize("over,expected", [
    ({"schema": "wrong"}, "expected 'evalseal.agent-tool-responses/1'"),
    ({"calls": None}, "missing `calls`"),
    ({"calls": "not a list"}, "`calls` must be a list"),
    ({"manifest_sha256": "nope"}, "`manifest_sha256` is not a sha256"),
])
def test_a_malformed_response_file_names_the_problem(over, expected):
    problems = validate_tool_responses(_responses(**over))
    assert any(expected in p for p in problems), problems


def test_duplicate_call_keys_are_caught():
    """The key is what makes a concurrent run produce the same file as a serial one."""
    call = _responses()["calls"][0]
    problems = validate_tool_responses(_responses(calls=[call, dict(call)]))
    assert any("repeats the key" in p for p in problems)


def test_a_successful_call_needs_a_response_digest():
    call = dict(_responses()["calls"][0])
    del call["response_sha256"]
    problems = validate_tool_responses(_responses(calls=[call]))
    assert any("succeeded but has no valid `response_sha256`" in p for p in problems)


def test_a_denied_call_carrying_a_response_digest_is_caught():
    """Two different events described at once; a reader cannot tell which happened."""
    call = dict(_responses()["calls"][0])
    call["outcome"] = "denied"
    problems = validate_tool_responses(_responses(calls=[call]))
    assert any("was denied but carries a `response_sha256`" in p for p in problems)


def test_a_denied_call_with_a_null_response_is_fine():
    call = dict(_responses()["calls"][0], outcome="denied", response_sha256=None)
    assert validate_tool_responses(_responses(calls=[call])) == []


def test_an_unknown_outcome_is_caught():
    call = dict(_responses()["calls"][0], outcome="maybe")
    assert any("`outcome` 'maybe'" in p for p in validate_tool_responses(
        _responses(calls=[call])))


@pytest.mark.parametrize("field", ["case_id", "tool", "repeat", "seq"])
def test_a_call_missing_a_key_field_is_caught(field):
    call = dict(_responses()["calls"][0])
    del call[field]
    assert any(field in p for p in validate_tool_responses(_responses(calls=[call])))


# --- fail closed at seal time -----------------------------------------------------------

def test_run_refuses_to_seal_a_malformed_agent_artifact(tmp_path):
    """A digest over a broken file verifies perfectly and describes nothing."""
    import typer

    broken = tmp_path / "tools.json"
    broken.write_text(json.dumps(_manifest(response_mode="teleported")), encoding="utf-8")
    with pytest.raises(typer.BadParameter) as e:
        _parse_artifacts([f"tool_acl={broken}"])
    message = str(e.value)
    assert "problem(s) in the agent artifact(s)" in message
    assert "teleported" in message
    assert "docs/agent-eval-receipts.md" in message
    # And it does not overstate what the check did.
    assert "not whether the agent really made these calls" in message


def test_run_still_seals_a_valid_agent_artifact(tmp_path):
    good = tmp_path / "tools.json"
    good.write_text(json.dumps(_manifest()), encoding="utf-8")
    assert _parse_artifacts([f"tool_acl={good}"]) == {"tool_acl": str(good)}


def test_an_unreadable_agent_artifact_is_left_to_the_external_rule(tmp_path):
    """One rule in one place: `run` seals it as `external` and a prereg decides."""
    missing = tmp_path / "gone.json"
    assert _parse_artifacts([f"tool_acl={missing}"]) == {"tool_acl": str(missing)}
