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
