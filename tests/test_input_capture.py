"""An input is read once, hashed as read, and used from what was read.

The baseline: an input was *used* at the start of a run and *hashed* at the end, so
anything that rewrote the file in between left a receipt describing bytes that produced
no score.

```
prompt actually used  : what the model was asked
dataset bytes at start: sha256:99a1baea720ec4631
dataset bytes at end  : sha256:15524b82f5e323980
digest SEALED in the receipt: sha256:15524b82f5e323980
  -> sealed the bytes that ran  : False
  -> sealed bytes nothing ran on: True
manifest.dataset.hash : sha256:99a1baea720ec4631 (computed at load)
any warning about it  : []
validate_record says  : nothing wrong
```

The receipt carried two different digests for one file and said nothing about either,
and `verify --artifacts` afterwards reported `ok` - against the file nothing ran on.
A check that passes because it is looking at the wrong bytes is worse than no check.
"""
from __future__ import annotations

import hashlib
import json
import threading

import pytest
from typer.testing import CliRunner

from evalseal.adapters.dataset import Dataset
from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.anchor import check_artifacts
from evalseal.cli import app
from evalseal.executor import run_eval
from evalseal.inputs import capture, capture_all
from evalseal.ledger import load_all

runner = CliRunner()

_ORIGINAL = '{"case_id": "c0", "prompt": "what the model was asked", "expected": "yes"}\n'
_REWRITTEN = '{"case_id": "c0", "prompt": "something else entirely", "expected": "no"}\n'


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def dataset_file(tmp_path):
    path = tmp_path / "ds.jsonl"
    path.write_text(_ORIGINAL, encoding="utf-8")
    return path


def _run_while_editing(dataset_file, *, edit: str | None = _REWRITTEN, roles=("dataset",)):
    """Run three repeats, rewriting (or deleting) the dataset during the first call."""
    captured = capture_all({role: str(dataset_file) for role in roles})
    data = captured["dataset"].data
    ds = Dataset.from_bytes(data, what=str(dataset_file)) if data is not None else None
    done = threading.Event()

    def answer(_prompt):
        if not done.is_set():
            if edit is None:
                dataset_file.unlink()
            else:
                dataset_file.write_text(edit, encoding="utf-8")
            done.set()
        return "yes"

    if ds is None:                      # the unreadable case needs a dataset from somewhere
        ds = Dataset.from_bytes(_ORIGINAL.encode(), what="<memory>")
    return run_eval(ds, LocalCallableTarget(answer), RegexScorer("yes"), n_repeats=3,
                    dataset_path=str(dataset_file),
                    artifact_paths={role: str(dataset_file) for role in roles},
                    captured=captured)


# --- the reproduction, inverted -------------------------------------------------------

def test_the_sealed_digest_is_of_the_bytes_that_ran(dataset_file):
    before = dataset_file.read_bytes()
    record = _run_while_editing(dataset_file)

    art = next(a for a in record.manifest.artifacts if a.role == "dataset")
    assert art.sha256 == _sha256(before)
    assert art.sha256 != _sha256(dataset_file.read_bytes())


def test_the_two_digests_of_one_file_in_a_receipt_agree(dataset_file):
    """An independent oracle on the same property, from the other side of the receipt.

    `manifest.dataset.hash` is computed when the cases are parsed and the `dataset`
    artifact digest when the record is sealed. They were two reads of one file and could
    disagree without anything noticing; now there is one read behind both. (For a CRLF
    dataset they still differ by definition - the case hash is over normalized text -
    which is why this is a test and not a validation rule that would reject old
    receipts.)
    """
    record = _run_while_editing(dataset_file)
    art = next(a for a in record.manifest.artifacts if a.role == "dataset")
    assert record.manifest.dataset.hash == art.sha256


def test_the_cases_that_ran_are_the_ones_that_were_read(dataset_file):
    record = _run_while_editing(dataset_file)
    assert [c.case_id for c in record.results] == ["c0"]
    assert record.aggregate.mean_score == 1.0, "scored against the expected it was read with"


def test_a_mid_run_edit_is_named_in_the_receipt(dataset_file):
    record = _run_while_editing(dataset_file)
    warnings = [w for w in record.aggregate.warnings if "CHANGED DURING RUN" in w]
    assert len(warnings) == 1
    assert warnings[0].startswith("DATASET CHANGED DURING RUN:")
    assert "cannot reproduce them" in warnings[0]

    art = next(a for a in record.manifest.artifacts if a.role == "dataset")
    assert "changed while the run was in flight" in art.note


def test_a_file_deleted_during_the_run_is_not_reported_as_unchanged(dataset_file):
    record = _run_while_editing(dataset_file, edit=None)
    warnings = [w for w in record.aggregate.warnings if "CHANGED DURING RUN" in w]
    assert "no longer readable" in warnings[0]
    art = next(a for a in record.manifest.artifacts if a.role == "dataset")
    assert art.kind == "hashed", "the bytes were read, so there is still something to check"


def test_an_untouched_input_says_which_bytes_its_digest_is_of(dataset_file):
    captured = capture_all({"dataset": str(dataset_file)})
    record = run_eval(Dataset.from_bytes(captured["dataset"].data),
                      LocalCallableTarget(lambda p: "yes"), RegexScorer("yes"),
                      n_repeats=2, artifact_paths={"dataset": str(dataset_file)},
                      captured=captured)
    art = next(a for a in record.manifest.artifacts if a.role == "dataset")
    assert art.note == "digest of the bytes this run read, taken before use"
    assert not [w for w in record.aggregate.warnings if "CHANGED DURING RUN" in w]


def test_an_input_that_was_never_readable_binds_nothing(tmp_path):
    captured = capture_all({"tool_acl": str(tmp_path / "absent.json")})
    record = run_eval(Dataset.from_bytes(_ORIGINAL.encode()),
                      LocalCallableTarget(lambda p: "yes"), RegexScorer("yes"),
                      n_repeats=2, artifact_paths={"tool_acl": str(tmp_path / "absent.json")},
                      captured=captured)
    art = next(a for a in record.manifest.artifacts if a.role == "tool_acl")
    assert art.kind == "external" and art.sha256 is None
    assert "nothing to verify against" in art.note


def test_an_input_that_appears_mid_run_is_not_silently_adopted(tmp_path):
    """"There was no tool manifest" and "there was one, a moment too late" differ."""
    late = tmp_path / "late.json"
    captured = capture_all({"tool_acl": str(late)})
    done = threading.Event()

    def answer(_prompt):
        if not done.is_set():
            late.write_text('{"version": 1}', encoding="utf-8")
            done.set()
        return "yes"

    record = run_eval(Dataset.from_bytes(_ORIGINAL.encode()),
                      LocalCallableTarget(answer), RegexScorer("yes"), n_repeats=2,
                      artifact_paths={"tool_acl": str(late)}, captured=captured)
    warnings = [w for w in record.aggregate.warnings if "CHANGED DURING RUN" in w]
    assert "was unreadable when this run started" in warnings[0]
    assert "Nothing in this receipt is bound to it" in warnings[0]
    art = next(a for a in record.manifest.artifacts if a.role == "tool_acl")
    assert art.kind == "external", "a file that arrived late is not the file that applied"


# --- the capture itself ----------------------------------------------------------------

def test_a_capture_hashes_what_it_read(dataset_file):
    snap = capture("dataset", dataset_file)
    assert snap.readable
    assert snap.sha256 == _sha256(snap.data)
    assert snap.size == len(dataset_file.read_bytes())
    assert snap.text() == _ORIGINAL
    assert snap.drift() is None


def test_a_capture_of_a_missing_file_is_captured_as_missing(tmp_path):
    snap = capture("dataset", tmp_path / "nope.jsonl")
    assert not snap.readable
    assert snap.sha256 is None and snap.size is None
    assert snap.drift() is None, "still missing is not a change"
    with pytest.raises(ValueError, match="not readable"):
        snap.text()


def test_capture_all_keeps_roles_in_order(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    captured = capture_all({"zebra": str(tmp_path / "a"), "aardvark": str(tmp_path / "b")})
    assert list(captured) == ["aardvark", "zebra"]
    assert captured["aardvark"].role == "aardvark"


def test_from_bytes_and_from_jsonl_agree(dataset_file):
    assert Dataset.from_bytes(dataset_file.read_bytes()).hash == \
           Dataset.from_jsonl(dataset_file).hash


def test_a_crlf_dataset_keeps_the_hash_it_has_always_had(tmp_path):
    """The hash is over normalized text, which is what `read_text` returned before."""
    lf, crlf = tmp_path / "lf.jsonl", tmp_path / "crlf.jsonl"
    lf.write_bytes(_ORIGINAL.encode())
    crlf.write_bytes(_ORIGINAL.replace("\n", "\r\n").encode())
    assert Dataset.from_bytes(lf.read_bytes()).hash == Dataset.from_bytes(crlf.read_bytes()).hash


def test_blank_lines_in_a_dataset_are_skipped(tmp_path):
    body = _ORIGINAL + "\n   \n" + _ORIGINAL.replace("c0", "c1")
    ds = Dataset.from_bytes(body.encode())
    assert [c.case_id for c in ds.cases] == ["c0", "c1"]


def test_a_dataset_error_still_names_its_source(tmp_path):
    path = tmp_path / "dupes.jsonl"
    body = ('{"case_id": "c0", "prompt": "a"}\n' * 2)
    path.write_bytes(body.encode())
    with pytest.raises(ValueError, match=rf"{path}:2: duplicate case_id 'c0'"):
        Dataset.from_bytes(path.read_bytes(), what=str(path))


# --- through the command line -----------------------------------------------------------

@pytest.fixture
def cli_workspace(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    (tmp_path / "ds.jsonl").write_text(_ORIGINAL, encoding="utf-8")
    (tmp_path / "t.json").write_text('{"model": "m"}', encoding="utf-8")
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}', encoding="utf-8")

    edited = threading.Event()

    def post(self, url, **kwargs):
        if not edited.is_set():
            (tmp_path / "ds.jsonl").write_text(_REWRITTEN, encoding="utf-8")
            edited.set()
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"model": "m",
                                    "choices": [{"message": {"content": "yes"}}]})

    monkeypatch.setattr(httpx.Client, "post", post)
    return tmp_path, edited


def _invoke(workspace, *extra):
    return runner.invoke(app, [
        "run", "--dataset", str(workspace / "ds.jsonl"),
        "--target-config", str(workspace / "t.json"),
        "--scorer-config", str(workspace / "s.json"),
        "--cassette", str(workspace / "c.json"), "--n", "3", "--concurrency", "1",
        "--ledger", str(workspace / "l.jsonl"), "--fail-on", "none",
        "--cassette-mode", "resume", *extra])


def test_a_run_whose_dataset_is_rewritten_under_it_says_so(cli_workspace):
    workspace, edited = cli_workspace
    before = (workspace / "ds.jsonl").read_bytes()
    result = _invoke(workspace)
    assert result.exit_code == 0, result.output
    assert edited.is_set(), "the fixture must actually have edited the file"

    record = load_all(workspace / "l.jsonl")[-1]
    art = next(a for a in record.manifest.artifacts if a.role == "dataset")
    assert art.sha256 == _sha256(before)
    assert any("DATASET CHANGED DURING RUN" in w for w in record.aggregate.warnings)
    assert "Provenance warnings" in " ".join(result.output.split())

    # And re-hashing it afterwards fails, because the file really is not the one that
    # ran. Before C2c this reported `ok`, against bytes no score came from.
    results = {c.role: c for c in check_artifacts(record, base_dir=workspace)}
    assert results["dataset"].status == "changed"


def test_an_unedited_run_binds_its_dataset_cleanly(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    (tmp_path / "ds.jsonl").write_text(_ORIGINAL, encoding="utf-8")
    (tmp_path / "t.json").write_text('{"model": "m"}', encoding="utf-8")
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}', encoding="utf-8")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, request=httpx.Request("POST", url),
        json={"model": "m", "choices": [{"message": {"content": "yes"}}]}))
    result = _invoke(tmp_path)
    assert result.exit_code == 0, result.output
    record = load_all(tmp_path / "l.jsonl")[-1]
    assert not [w for w in record.aggregate.warnings if "CHANGED DURING RUN" in w]
    results = {c.role: c for c in check_artifacts(record, base_dir=tmp_path)}
    assert results["dataset"].status == "ok"


def test_a_missing_dataset_is_an_input_error_not_a_crash(tmp_path, monkeypatch):
    (tmp_path / "ds.jsonl").write_text(_ORIGINAL, encoding="utf-8")
    (tmp_path / "t.json").write_text('{"model": "m"}', encoding="utf-8")
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}', encoding="utf-8")
    real = capture_all

    def vanished(paths):
        captured = real(paths)
        return {role: (snap if role != "dataset" else capture(role, tmp_path / "gone"))
                for role, snap in captured.items()}

    monkeypatch.setattr("evalseal.cli.capture_all", vanished)
    result = _invoke(tmp_path)
    assert result.exit_code == 2
    assert "cannot read the dataset" in " ".join(result.output.split())


def test_the_suite_hash_is_of_the_bytes_the_run_read(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    (tmp_path / "ds.jsonl").write_text(_ORIGINAL, encoding="utf-8")
    (tmp_path / "t.json").write_text('{"model": "m"}', encoding="utf-8")
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}', encoding="utf-8")
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "version": 2,
        "dataset": {"path": str(tmp_path / "ds.jsonl")},
        "evaluator": {"scorer": str(tmp_path / "s.json")},
        "target": {"config": str(tmp_path / "t.json")},
        "run": {"n_repeats": 3, "cassette": str(tmp_path / "c.json"),
                "ledger": str(tmp_path / "l.jsonl"), "fail_on": "none"},
    }), encoding="utf-8")
    before = suite.read_bytes()

    edited = threading.Event()

    def post(self, url, **kwargs):
        if not edited.is_set():
            suite.write_text("{}", encoding="utf-8")
            edited.set()
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"model": "m",
                                    "choices": [{"message": {"content": "yes"}}]})

    monkeypatch.setattr(httpx.Client, "post", post)
    # --ledger explicitly: the flag has a default, so a suite's `run.ledger` never
    # wins over it, and the record would land in the default .evalseal/ ledger.
    result = runner.invoke(app, ["run", "--suite", str(suite), "--cassette-mode", "resume",
                                 "--ledger", str(tmp_path / "l.jsonl")])
    assert result.exit_code == 0, result.output

    record = load_all(tmp_path / "l.jsonl")[-1]
    assert record.manifest.suite.hash == _sha256(before)
    assert record.manifest.suite.evaluator_hash is not None, "read from the captured bytes"
    assert any("SUITE CHANGED DURING RUN" in w for w in record.aggregate.warnings)


def test_an_agent_artifact_is_judged_by_the_bytes_that_get_sealed(tmp_path):
    """Validating by path and hashing by path are two reads; this is one."""
    import typer

    from evalseal.cli import _check_agent_artifacts

    tool = {"name": "search_docs", "description": "d",
            "parameters": {"type": "object", "properties": {}}}
    definition = "sha256:" + hashlib.sha256(json.dumps(
        tool, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {
        "schema": "evalseal.agent-tools/1",
        "response_mode": "recorded",
        "allowed": [{"name": "search_docs", "definition_sha256": definition}],
        "denied": ["shell.exec"],
        "tools": [tool],
    }
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({**manifest, "response_mode": "teleported"}),
                    encoding="utf-8")
    captured = capture_all({"tool_acl": str(path)})
    # The file is corrected after the capture. The run used what it read, so that is
    # what is judged - and what would have been sealed.
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(typer.BadParameter, match="teleported"):
        _check_agent_artifacts(captured)
    # And the corrected file passes, so the check is not simply always failing.
    assert _check_agent_artifacts(capture_all({"tool_acl": str(path)})) is None
