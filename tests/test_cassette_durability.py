"""One writer owns a cassette; each receipt owns an immutable copy of what it used.

Two defects, reproduced on the build before C2b and guarded here.

**C2-3, two writers.** Every `Cassette` held the whole entry table in memory and
rewrote the whole file after each recorded response, so two writers each erased the
other's entries and the last one won:

```
after writer A : 1 entries
after writer B : 1 entries
A's entry survived: False
A replays       : NO -> No cassette entry for repeat 0 of request 7f24486a3603
```

Writer A's response was admitted, acknowledged, and then silently deleted by a process
that never knew A existed.

**C2-4, a receipt bound to a file later runs rewrite.**

```
run A sealed cassette : sha256:57106b513ae0af6f6 (3 entries)
run A mean            : 1.0
run A verifies        : ['ok']

after an honest append of 2 more repeats:
run A verifies        : [('cassette', 'changed')]

after a `fresh` re-draw on the same path:
run A verifies        : [('cassette', 'changed')]
replaying run A now   : mean 0.0 (run A's receipt says 1.0 )
any immutable copy of run A's responses: ['run.json']
```

Both directions were wrong at once: an honest later run turned the earlier receipt red,
and a re-draw destroyed the evidence behind it with nothing kept anywhere.

See docs/adr/0002-cassette-writer-strategy.md.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from conftest import fake_http
from typer.testing import CliRunner

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.recording import Cassette, CassetteBusy, unit
from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import OpenAICompatibleTarget, ProviderCall
from evalseal.anchor import check_artifacts
from evalseal.cli import app
from evalseal.executor import run_eval
from evalseal.ledger import load_all

runner = CliRunner()

_DS = Dataset([Case("c0", "q", "yes")], hash="sha256:ds")


def _target(cassette, answers):
    """An OpenAI-shaped target whose draws are scripted, through the cassette."""
    target = OpenAICompatibleTarget(model="m", cassette=cassette)
    draws = iter(answers)
    target._post = lambda body: ProviderCall(
        {"choices": [{"message": {"content": next(draws)}, "finish_reason": "stop"}],
         "model": "m"}, attempts=1)
    return target


def _run(cassette, answers, *, n=3, snapshot_dir=None):
    return run_eval(_DS, _target(cassette, answers), RegexScorer("yes"), n_repeats=n,
                    artifact_paths={"cassette": str(cassette.path)},
                    snapshot_dir=snapshot_dir)


# --- C2-3: one writer, and the other is told ----------------------------------------

def test_a_second_writer_is_refused_rather_than_served(tmp_path):
    path = tmp_path / "c.json"
    with Cassette(path, record=True), pytest.raises(CassetteBusy) as caught:
        Cassette(path, record=True)
    message = str(caught.value)
    assert str(path) in message
    assert "one writer at a time" in message
    assert "--cassette path" in message, "the error must say what to do instead"


def test_the_refusal_names_the_pid_holding_the_cassette(tmp_path):
    import os

    path = tmp_path / "c.json"
    with Cassette(path, record=True), pytest.raises(CassetteBusy) as caught:
        Cassette(path, record=True)
    assert f"held by pid {os.getpid()}" in str(caught.value)


def test_the_first_writers_responses_survive(tmp_path):
    """The reproduction, inverted: B is refused, so A's entry is still there."""
    path = tmp_path / "c.json"
    with Cassette(path, record=True) as a:
        a.call({"model": "m", "prompt": "A"}, lambda: {"text": "answer-A"}, index=0)
        with pytest.raises(CassetteBusy):
            b = Cassette(path, record=True)
            b.call({"model": "m", "prompt": "B"}, lambda: {"text": "answer-B"}, index=0)
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert [v["text"] for v in entries.values()] == ["answer-A"]


def test_a_reader_is_never_blocked_by_a_writer(tmp_path):
    """Replay never writes, and a CI job that only replays must not be blockable."""
    path = tmp_path / "c.json"
    with Cassette(path, record=True) as writer:
        writer.call({"q": 1}, lambda: {"v": "recorded"}, index=0)
        reader = Cassette(path, mode="replay")
        assert reader.call({"q": 1}, lambda: {"v": "never"}, index=0) == {"v": "recorded"}


def test_two_readers_coexist(tmp_path):
    path = tmp_path / "c.json"
    with Cassette(path, record=True) as writer:
        writer.call({"q": 1}, lambda: {"v": "recorded"}, index=0)
    assert Cassette(path).record is False
    assert Cassette(path).record is False


def test_closing_hands_the_cassette_to_the_next_writer(tmp_path):
    path = tmp_path / "c.json"
    first = Cassette(path, record=True)
    first.close()
    first.close()                      # idempotent
    second = Cassette(path, record=True)
    assert second.record
    second.close()


def test_a_rejected_format_does_not_leave_the_cassette_locked(tmp_path):
    """A load that raises must release what it took, or the path is dead for the run."""
    from evalseal.adapters.recording import CassetteFormatError

    path = tmp_path / "c.json"
    path.write_text('{"version": 99, "entries": {}}', encoding="utf-8")
    with pytest.raises(CassetteFormatError):
        Cassette(path, record=True)
    path.write_text('{"version": 3, "entries": {}}', encoding="utf-8")
    with Cassette(path, record=True) as later:
        assert later.record


# --- the acceptance criterion: an interrupted run keeps what it recorded -------------

def test_an_interrupted_run_keeps_every_completed_response(tmp_path, monkeypatch):
    import os

    path = tmp_path / "c.json"
    with Cassette(path, record=True) as cas:
        for repeat in range(2):
            with unit("c0", repeat):
                cas.call({"q": "same"}, lambda r=repeat: {"answer": r})

        def interrupted(src, dst):
            raise KeyboardInterrupt("Ctrl+C between write and rename")

        monkeypatch.setattr(os, "replace", interrupted)
        with pytest.raises(KeyboardInterrupt), unit("c0", 2):
            cas.call({"q": "same"}, lambda: {"answer": 2})

    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 2, "the two completed responses are still on disk"
    assert sorted(v["answer"] for v in entries.values()) == [0, 1]


# --- C2-4: the snapshot is the receipt's own copy ------------------------------------

def test_a_snapshot_holds_the_entries_the_run_used(tmp_path):
    """Not the whole file: the claim is "the responses these scores came from"."""
    path = tmp_path / "c.json"
    # An entry from some earlier run, in the file before this one starts.
    path.write_text(json.dumps({"version": 3, "entries": {"older": {"v": "stale"}}}),
                    encoding="utf-8")
    with Cassette(path, record=True) as cas:
        with unit("c0", 0):
            cas.call({"q": "used"}, lambda: {"v": "A"})
        snap = cas.finalize(tmp_path / "out")
    held = json.loads(snap.path.read_text(encoding="utf-8"))
    assert snap.n_entries == 1
    assert "older" not in held["entries"], "an entry this run never read is not its evidence"
    assert [v["v"] for v in held["entries"].values()] == ["A"]
    assert snap.path.name.endswith(".json") and snap.sha256[7:23] in snap.path.name


def test_a_snapshot_replays_the_run_it_was_taken_from(tmp_path):
    """The headline acceptance: a later re-draw cannot invalidate an earlier receipt."""
    path = tmp_path / "c.json"
    with Cassette(path, mode="resume") as live:
        first = _run(live, itertools.repeat("yes"), snapshot_dir=tmp_path / "run-1")
    assert first.aggregate.mean_score == 1.0
    snapshot = next(a for a in first.manifest.artifacts if a.role == "cassette_snapshot")

    # A `fresh` re-draw writes the same keys with different answers: before C2b this
    # destroyed the responses behind `first` and left no copy anywhere.
    with Cassette(path, mode="fresh") as redraw:
        again = _run(redraw, itertools.repeat("no"), snapshot_dir=tmp_path / "run-2")
    assert again.aggregate.mean_score == 0.0

    replayed = _run(Cassette(snapshot.path, mode="replay"), iter(()))
    assert replayed.aggregate.mean_score == first.aggregate.mean_score
    assert [c.scores for c in replayed.results] == [c.scores for c in first.results]


def test_every_response_in_the_receipt_is_in_its_snapshot(tmp_path):
    """An independent oracle: each sealed call's response digest is in the snapshot.

    The receipt's digests are computed by the cassette; the snapshot is serialized by
    it too. Checking one against the other from outside catches a snapshot that holds
    a *different* set of responses from the ones the scores came from.
    """
    from evalseal.adapters.recording import digest

    path = tmp_path / "c.json"
    with Cassette(path, mode="resume") as live:
        record = _run(live, itertools.repeat("yes"), n=4, snapshot_dir=tmp_path / "out")
    snapshot = next(a for a in record.manifest.artifacts if a.role == "cassette_snapshot")
    held = json.loads(Path(snapshot.path).read_text(encoding="utf-8"))
    in_snapshot = {digest(v) for v in held["entries"].values()}

    calls = record.manifest.execution.calls
    assert len(calls) == 4
    for call in calls:
        assert call.response_digest in in_snapshot, f"{call.case_id}/{call.repeat} is not in it"


def test_one_cassette_shared_by_target_and_judge_is_snapshotted_once(tmp_path):
    """The snapshot is of the file, not of the role that happened to reach it first."""
    from evalseal.adapters.scorer import LLMJudgeScorer

    path = tmp_path / "c.json"
    with Cassette(path, mode="resume") as cas:
        target = _target(cas, itertools.repeat("an answer"))
        judge = _target(cas, itertools.repeat("PASS"))
        judge.model = "judge"
        record = run_eval(_DS, target, LLMJudgeScorer(judge=judge, rubric="r"),
                          n_repeats=2, snapshot_dir=tmp_path / "out")
    roles = [a.role for a in record.manifest.artifacts if a.role.startswith("cassette_snap")]
    assert roles == ["cassette_snapshot"]

    other = tmp_path / "judge.json"
    with Cassette(path, mode="replay") as t_cas, Cassette(other, mode="resume") as j_cas:
        target = _target(t_cas, iter(()))
        judge = _target(j_cas, itertools.repeat("PASS"))
        record = run_eval(_DS, target, LLMJudgeScorer(judge=judge, rubric="r"),
                          n_repeats=2, snapshot_dir=tmp_path / "out2")
    roles = [a.role for a in record.manifest.artifacts if a.role.startswith("cassette_snap")]
    assert roles == ["cassette_snapshot", "cassette_snapshot_2"]


def test_a_target_without_a_cassette_binds_no_snapshot(tmp_path):
    """There is nothing to copy, and nothing is claimed."""
    from evalseal.adapters.target import LocalCallableTarget

    record = run_eval(_DS, LocalCallableTarget(lambda p: "yes"), RegexScorer("yes"),
                      n_repeats=2, snapshot_dir=tmp_path / "out")
    assert [a.role for a in record.manifest.artifacts] == []
    assert not (tmp_path / "out").exists()


def test_without_a_snapshot_directory_nothing_is_written(tmp_path):
    """A caller who does not want the copy gets no snapshot artifact, not a weaker one."""
    path = tmp_path / "c.json"
    with Cassette(path, mode="resume") as live:
        record = _run(live, itertools.repeat("yes"))
    assert not any(a.role.startswith("cassette_snap") for a in record.manifest.artifacts)
    assert not (tmp_path / "snapshots").exists()


# --- a snapshot is immutable ---------------------------------------------------------

def test_finalizing_the_same_entries_twice_writes_one_file(tmp_path):
    path = tmp_path / "c.json"
    with Cassette(path, record=True) as cas:
        with unit("c0", 0):
            cas.call({"q": 1}, lambda: {"v": "A"})
        first = cas.finalize(tmp_path / "out")
        second = cas.finalize(tmp_path / "out")
    assert first == second
    assert [p.name for p in (tmp_path / "out").iterdir()] == [first.path.name]


def test_an_existing_snapshot_is_never_overwritten(tmp_path):
    """If the file there holds other bytes, an earlier receipt may be bound to it."""
    path = tmp_path / "c.json"
    with Cassette(path, record=True) as cas:
        with unit("c0", 0):
            cas.call({"q": 1}, lambda: {"v": "A"})
        snap = cas.finalize(tmp_path / "out")
        snap.path.write_text('{"version": 3, "entries": {"x": {"v": "edited"}}}',
                             encoding="utf-8")
        with pytest.raises(RuntimeError, match="snapshot is immutable"):
            cas.finalize(tmp_path / "out")


def test_a_format_2_cassette_finalizes_to_a_format_2_snapshot(tmp_path):
    """A recording someone kept stays replayable under the rules that wrote it."""
    from evalseal.adapters.recording import request_key

    payload = {"q": "old"}
    path = tmp_path / "old.json"
    # Keyed the way format 2 keyed things: by (request, repeat), with no case or role.
    key = request_key({"request": payload, "repeat": 0})
    path.write_text(json.dumps({"version": 2, "entries": {key: {"v": "kept"}}}),
                    encoding="utf-8")

    cas = Cassette(path, mode="replay")
    with unit("c0", 0):
        assert cas.call(payload, lambda: {"v": "never"}) == {"v": "kept"}
    snap = cas.finalize(tmp_path / "out")

    assert snap.format == 2 and snap.n_entries == 1
    held = json.loads(snap.path.read_text(encoding="utf-8"))
    assert held["version"] == 2 and held["entries"] == {key: {"v": "kept"}}
    # And the snapshot replays as what it is: a format-2 cassette.
    replayed = Cassette(snap.path, mode="replay")
    assert replayed.format == 2
    with unit("c0", 0):
        assert replayed.call(payload, lambda: {"v": "never"}) == {"v": "kept"}


# --- verification tells "moved on" apart from "replaced" -----------------------------

@pytest.fixture
def sealed(tmp_path, monkeypatch):
    """A receipt in a ledger, its live cassette, and its snapshot."""
    import httpx

    answers = itertools.cycle(["yes"])
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    fake_http(monkeypatch, lambda self, url, **k: httpx.Response(
        200, request=httpx.Request("POST", url),
        json={"model": "m", "choices": [{"message": {"content": next(answers)}}]}))
    (tmp_path / "ds.jsonl").write_text('{"case_id": "only", "prompt": "p"}\n')
    (tmp_path / "t.json").write_text('{"model": "m"}')
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}')

    def invoke(*extra, n="3"):
        result = runner.invoke(app, [
            "run", "--dataset", str(tmp_path / "ds.jsonl"),
            "--target-config", str(tmp_path / "t.json"),
            "--scorer-config", str(tmp_path / "s.json"),
            "--cassette", str(tmp_path / "c.json"), "--n", n, "--concurrency", "1",
            "--ledger", str(tmp_path / "l.jsonl"), "--fail-on", "none",
            "--cassette-mode", "resume", *extra])
        assert result.exit_code == 0, result.output
        return result

    invoke()
    return tmp_path, tmp_path / "l.jsonl", tmp_path / "c.json", invoke


def test_the_run_binds_both_the_live_cassette_and_its_snapshot(sealed):
    _, ledger, _, _ = sealed
    roles = {a.role: a for a in load_all(ledger)[-1].manifest.artifacts}
    assert roles["cassette"].kind == "mutable"
    assert roles["cassette_snapshot"].kind == "hashed"
    assert "replayable on its own" in roles["cassette_snapshot"].note


def test_a_later_run_leaves_the_earlier_receipt_verifiable(sealed):
    """The false red: an honest append used to report the earlier receipt as changed."""
    workspace, ledger, _, invoke = sealed
    first = load_all(ledger)[0]
    invoke(n="5")                                  # two more repeats, same cassette

    results = {c.role: c for c in check_artifacts(first, base_dir=workspace)}
    assert results["cassette"].status == "moved"
    assert "not evidence of tampering" in results["cassette"].detail
    assert results["cassette_snapshot"].status == "ok"

    checked = runner.invoke(app, ["verify", "--ledger", str(ledger), "--artifacts",
                                  "--index", "0"])
    assert checked.exit_code == 0, checked.output
    assert "moved" in checked.output


def test_replacing_a_response_the_receipt_used_is_still_changed(sealed):
    """And `moved` must not become a way to hide that."""
    workspace, ledger, cassette, _ = sealed
    data = json.loads(cassette.read_text(encoding="utf-8"))
    first = next(iter(data["entries"]))
    data["entries"][first]["choices"][0]["message"]["content"] = "tampered"
    cassette.write_text(json.dumps(data), encoding="utf-8")

    results = {c.role: c for c in check_artifacts(load_all(ledger)[0], base_dir=workspace)}
    assert results["cassette"].status == "changed"
    assert "replaced, not merely added to" in results["cassette"].detail
    assert results["cassette_snapshot"].status == "ok", "the snapshot is untouched"

    checked = runner.invoke(app, ["verify", "--ledger", str(ledger), "--artifacts",
                                  "--index", "0"])
    assert checked.exit_code == 1
    assert "changed" in checked.output


def test_an_edited_snapshot_is_changed(sealed):
    """The snapshot is the immutable claim, so a mismatch there is a tamper signal."""
    workspace, ledger, _, _ = sealed
    record = load_all(ledger)[0]
    snapshot = next(a for a in record.manifest.artifacts if a.role == "cassette_snapshot")
    (workspace / snapshot.path).write_text('{"version": 3, "entries": {}}',
                                           encoding="utf-8")
    results = {c.role: c for c in check_artifacts(record, base_dir=workspace)}
    assert results["cassette_snapshot"].status == "changed"
    checked = runner.invoke(app, ["verify", "--ledger", str(ledger), "--artifacts",
                                  "--index", "0"])
    assert checked.exit_code == 1


def test_a_receipt_with_no_snapshot_reports_a_mismatch_as_changed(sealed):
    """Every receipt sealed before C2b. Unknown is reported as the stronger claim."""
    workspace, ledger, cassette, invoke = sealed
    record = load_all(ledger)[0]
    record.manifest.artifacts = [a for a in record.manifest.artifacts
                                 if a.role != "cassette_snapshot"]
    invoke(n="5")
    results = {c.role: c for c in check_artifacts(record, base_dir=workspace)}
    assert results["cassette"].status == "changed"
    assert "binds no cassette_snapshot" in results["cassette"].detail


def test_a_snapshot_that_is_gone_is_not_treated_as_agreement(sealed):
    workspace, ledger, _, invoke = sealed
    record = load_all(ledger)[0]
    snapshot = next(a for a in record.manifest.artifacts if a.role == "cassette_snapshot")
    invoke(n="5")
    (workspace / snapshot.path).unlink()
    results = {c.role: c for c in check_artifacts(record, base_dir=workspace)}
    assert results["cassette"].status == "changed"
    assert results["cassette_snapshot"].status == "missing"


# --- per-run output directories ------------------------------------------------------

def test_each_run_writes_its_own_directory(sealed):
    workspace, ledger, _, invoke = sealed
    invoke(n="4")
    runs = sorted((workspace / "runs").iterdir())
    assert len(runs) == 2, [p.name for p in runs]
    for directory in runs:
        assert (directory / "report.json").exists()
        assert (directory / "report.md").exists()
    means = [json.loads((d / "report.json").read_text())["manifest"]["run_config"]["n_repeats"]
             for d in runs]
    assert sorted(means) == [3, 4], "each run's own report survived the other"


def test_the_run_directory_is_named_after_the_session_in_the_receipt(sealed):
    workspace, ledger, _, _ = sealed
    record = load_all(ledger)[0]
    session = record.manifest.execution.session_id
    directory = next(p for p in (workspace / "runs").iterdir())
    assert directory.name.endswith(session[:8])


def test_the_working_directory_report_is_the_latest_run(sealed):
    """Kept as an alias, because every existing workflow reads ./report.json."""
    workspace, _, _, invoke = sealed
    invoke(n="4")
    latest = json.loads((workspace / "report.json").read_text(encoding="utf-8"))
    assert latest["manifest"]["run_config"]["n_repeats"] == 4


def test_out_dir_overrides_the_per_run_default(sealed):
    workspace, _, _, invoke = sealed
    invoke("--out-dir", str(workspace / "chosen"))
    assert (workspace / "chosen" / "report.json").exists()


def test_no_snapshot_leaves_the_receipt_saying_so(sealed):
    workspace, ledger, _, invoke = sealed
    invoke("--no-snapshot", "--out-dir", str(workspace / "bare"))
    roles = [a.role for a in load_all(ledger)[-1].manifest.artifacts]
    assert "cassette" in roles
    assert "cassette_snapshot" not in roles


def test_a_second_run_on_a_busy_cassette_exits_with_the_reason(sealed):
    workspace, ledger, cassette, _ = sealed
    with Cassette(cassette, mode="resume"):
        result = runner.invoke(app, [
            "run", "--dataset", str(workspace / "ds.jsonl"),
            "--target-config", str(workspace / "t.json"),
            "--scorer-config", str(workspace / "s.json"),
            "--cassette", str(cassette), "--n", "3", "--concurrency", "1",
            "--ledger", str(workspace / "l.jsonl"), "--cassette-mode", "resume"])
    assert result.exit_code == 1
    assert "one writer at a time" in " ".join(result.output.split())


# --- the edges of the comparison ------------------------------------------------------

def test_a_live_cassette_that_is_no_longer_json_is_changed(sealed):
    """Not "moved": a file that cannot be parsed cannot be shown to still hold anything."""
    workspace, ledger, cassette, _ = sealed
    cassette.write_text("not json at all", encoding="utf-8")
    results = {c.role: c for c in check_artifacts(load_all(ledger)[0], base_dir=workspace)}
    assert results["cassette"].status == "changed"
    assert "no longer a readable cassette" in results["cassette"].detail


def test_a_snapshot_whose_entries_are_not_a_mapping_is_not_trusted(sealed):
    workspace, ledger, _, invoke = sealed
    record = load_all(ledger)[0]
    snapshot = next(a for a in record.manifest.artifacts if a.role == "cassette_snapshot")
    invoke(n="5")
    (workspace / snapshot.path).write_text('{"version": 3, "entries": []}',
                                           encoding="utf-8")
    results = {c.role: c for c in check_artifacts(record, base_dir=workspace)}
    assert results["cassette"].status == "changed"
    assert "could not be read to narrow that down" in results["cassette"].detail


def test_an_unknown_mode_in_the_environment_is_rejected(tmp_path, monkeypatch):
    """A typo in $EVALSEAL_MODE must not fall through to a silent default."""
    monkeypatch.setenv("EVALSEAL_MODE", "resumee")
    with pytest.raises(ValueError, match=r"unknown cassette mode 'resumee' in \$EVALSEAL_MODE"):
        Cassette(tmp_path / "c.json")


def test_releasing_a_lock_twice_is_harmless(tmp_path):
    """`close()` never calls it twice, but the contract says it is safe, so it is tested."""
    from evalseal.locking import drop_lock, take_lock

    handle = take_lock(tmp_path / "x.json", "cassette")
    drop_lock(handle)
    drop_lock(handle)
    assert handle.closed


def test_an_unreadable_lock_file_leaves_the_error_without_a_pid(tmp_path):
    """The pid is a hint read without the lock, so it has to be optional."""
    from evalseal.locking import _holder

    assert _holder(tmp_path) == "", "a directory is not a pid"
    (tmp_path / "x.lock").write_text("not-a-pid", encoding="utf-8")
    assert _holder(tmp_path / "x.lock") == ""


# --- the same errors, through `decompose` ---------------------------------------------

@pytest.fixture
def decompose_workspace(tmp_path):
    (tmp_path / "ds.jsonl").write_text('{"case_id": "c0", "prompt": "p"}\n', encoding="utf-8")
    (tmp_path / "t.json").write_text('{"model": "m"}', encoding="utf-8")
    (tmp_path / "s.json").write_text(
        '{"type": "llm_judge", "rubric": "r", "judge_model": "j"}', encoding="utf-8")
    return tmp_path, ["decompose", "--dataset", str(tmp_path / "ds.jsonl"),
                      "--target-config", str(tmp_path / "t.json"),
                      "--scorer-config", str(tmp_path / "s.json"),
                      "--cassette", str(tmp_path / "c.json"), "--n", "2"]


def test_run_reports_an_incompatible_cassette_instead_of_raising(sealed):
    """`run` always had the handler; `decompose` had it around the wrong block."""
    workspace, ledger, cassette, _ = sealed
    cassette.write_text('{"version": 99, "entries": {}}', encoding="utf-8")
    result = runner.invoke(app, [
        "run", "--dataset", str(workspace / "ds.jsonl"),
        "--target-config", str(workspace / "t.json"),
        "--scorer-config", str(workspace / "s.json"),
        "--cassette", str(cassette), "--n", "3",
        "--ledger", str(ledger), "--cassette-mode", "resume"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "incompatible evalseal version" in " ".join(result.output.split())


def test_decompose_reports_an_incompatible_cassette_instead_of_raising(decompose_workspace):
    """Found while wiring CassetteBusy in: the handler sat around the wrong block.

    `decompose` built its two cassettes *before* the try that caught
    `CassetteFormatError`, so an incompatible cassette reached the user as a traceback
    rather than as the message written for it.
    """
    workspace, argv = decompose_workspace
    (workspace / "c.judge_only.json").write_text('{"version": 99, "entries": {}}',
                                                 encoding="utf-8")
    result = runner.invoke(app, argv)
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "incompatible evalseal version" in " ".join(result.output.split())


def test_decompose_reports_a_busy_cassette(decompose_workspace, monkeypatch):
    """Only a recording arm can collide: a replaying one takes no lock at all."""
    workspace, argv = decompose_workspace
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    with Cassette(workspace / "c.judge_only.json", mode="resume"):
        result = runner.invoke(app, argv)
    assert result.exit_code == 1
    assert "one writer at a time" in " ".join(result.output.split())
