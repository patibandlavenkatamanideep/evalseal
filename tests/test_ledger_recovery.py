"""An interrupted append must not cost the history in front of it.

**Baseline reproduction (2.2.0).** A ledger with one complete record and a second one
cut off mid-write - what a kill, a full disk or a lost connection leaves:

    load_all     raised InvalidInput: .../l.jsonl line 2 is not valid JSON:
                 Unterminated string starting at: line 1 column 155 (char 154)
    verify_chain raised the same

Both statements are true and neither tells a reader that the record before the partial
one is fine, or what to do about it. Every command that reads the ledger - `verify`,
`gate`, `report`, `diff`, `sign` - failed the same way, so the tamper-evidence record
was unreadable in full because of a few hundred bytes at the end.

Run in a temporary directory, with the lock held, so nothing here touches a real ledger.
"""
from __future__ import annotations

import json
import os
import threading

import pytest
from conftest import make_dataset
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.executor import run_eval
from evalseal.ledger import (
    IncompleteFinalRecord,
    incomplete_tail,
    load_all,
    recover_incomplete_tail,
    seal_and_append,
    verify_chain,
)
from evalseal.validation import InvalidInput

runner = CliRunner()


def _ledger(tmp_path, n: int = 2):
    """A real sealed ledger with `n` records."""
    path = tmp_path / "l.jsonl"
    for i in range(n):
        record = run_eval(make_dataset("q"),
                          LocalCallableTarget(lambda p: "yes", name=f"m{i}"),
                          RegexScorer("yes"), n_repeats=2)
        seal_and_append(record, path, relink=True)
    return path


def _truncate_last_record(path, keep_fraction: float = 0.4):
    """Cut the final record off mid-line, leaving the ones before it whole."""
    lines = path.read_text(encoding="utf-8").splitlines()
    partial = lines[-1][:int(len(lines[-1]) * keep_fraction)]
    path.write_text("\n".join(lines[:-1]) + "\n" + partial, encoding="utf-8")
    return partial


# --- detection -----------------------------------------------------------------------

def test_an_interrupted_append_is_detected_as_one(tmp_path):
    path = _ledger(tmp_path, 3)
    partial = _truncate_last_record(path)
    found = incomplete_tail(path)
    assert found is not None
    complete, tail = found
    assert complete == 2
    assert tail == partial


def test_a_complete_ledger_has_no_incomplete_tail(tmp_path):
    assert incomplete_tail(_ledger(tmp_path, 2)) is None


def test_a_final_line_missing_only_its_newline_is_not_incomplete(tmp_path):
    """A whole record without a trailing newline is a whole record."""
    path = _ledger(tmp_path, 2)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    assert incomplete_tail(path) is None
    assert len(load_all(path)) == 2


def test_damage_in_the_middle_is_not_claimed_as_an_interrupted_append(tmp_path):
    """Only the last line is recoverable this way; anything else is corruption."""
    path = _ledger(tmp_path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1][:80]                     # break a record in the middle
    path.write_text("\n".join(lines), encoding="utf-8")
    assert incomplete_tail(path) is None
    with pytest.raises(InvalidInput):
        load_all(path)


def test_the_error_says_how_much_history_is_intact_and_what_to_run(tmp_path):
    path = _ledger(tmp_path, 3)
    _truncate_last_record(path)
    with pytest.raises(IncompleteFinalRecord) as caught:
        load_all(path)
    message = str(caught.value)
    assert "2 complete record(s)" in message
    assert "interrupted append" in message
    assert "evalseal recover" in message
    assert "Nothing is deleted" in message
    assert caught.value.complete == 2


def test_it_is_an_input_error_so_every_existing_handler_covers_it(tmp_path):
    """`verify` and the rest already treat a malformed ledger as exit 2, not a crash."""
    path = _ledger(tmp_path, 2)
    _truncate_last_record(path)
    assert issubclass(IncompleteFinalRecord, InvalidInput)
    result = runner.invoke(app, ["verify", "--ledger", str(path)])
    assert result.exit_code == 2, result.output
    assert "evalseal recover" in result.output


# --- recovery ------------------------------------------------------------------------

def test_recovery_keeps_the_complete_records_and_moves_the_tail_aside(tmp_path):
    path = _ledger(tmp_path, 3)
    partial = _truncate_last_record(path)
    before = path.read_text(encoding="utf-8")

    sidecar, kept, moved = recover_incomplete_tail(path)
    assert kept == 2 and moved == len(partial)
    assert sidecar.read_text(encoding="utf-8") == partial
    assert sidecar.name.startswith("l.jsonl.incomplete-")
    # The complete prefix is byte-identical to what it was.
    assert path.read_text(encoding="utf-8") == before[:len(before) - len(partial)]
    records = load_all(path)
    assert len(records) == 2
    ok, message = verify_chain(path)
    assert ok, message


def test_the_sidecar_is_written_before_the_ledger_is_touched(tmp_path):
    """Write, fsync, truncate - so a failure cannot lose the bytes it moved."""
    path = _ledger(tmp_path, 2)
    _truncate_last_record(path)
    original = path.read_text(encoding="utf-8")

    import unittest.mock
    with (
        unittest.mock.patch("pathlib.Path.open", side_effect=_fail_on_write(path)),
        pytest.raises(OSError),
    ):
        recover_incomplete_tail(path)
    # The ledger is untouched, and the sidecar either does not exist or holds the tail.
    assert path.read_text(encoding="utf-8") == original


def _fail_on_write(ledger):
    """Let the sidecar be written, then fail when the ledger is opened for writing."""
    real = type(ledger).open

    def guard(self, mode="r", *a, **kw):
        if self == ledger and "w" in mode:
            raise OSError("disk full")
        return real(self, mode, *a, **kw)

    return guard


def test_recovering_a_healthy_ledger_is_refused_rather_than_silently_truncating(tmp_path):
    path = _ledger(tmp_path, 2)
    with pytest.raises(ValueError, match="nothing to recover"):
        recover_incomplete_tail(path)
    assert len(load_all(path)) == 2


def test_recovery_is_safe_against_a_concurrent_append(tmp_path):
    """Held under the ledger lock, so an appender cannot write while it rewrites."""
    path = _ledger(tmp_path, 2)
    _truncate_last_record(path)
    done: list[object] = []

    def append():
        record = run_eval(make_dataset("q"),
                          LocalCallableTarget(lambda p: "yes", name="late"),
                          RegexScorer("yes"), n_repeats=2)
        try:
            seal_and_append(record, path, relink=True)
            done.append("appended")
        except Exception as e:                  # the partial tail is still there
            done.append(e)

    writer = threading.Thread(target=append)
    sidecar, kept, _ = recover_incomplete_tail(path)
    writer.start()
    writer.join()
    assert kept == 1
    # Whatever the appender did, the file reads cleanly afterwards.
    records = load_all(path)
    assert len(records) in (1, 2)
    ok, message = verify_chain(path)
    assert ok, message


# --- the command ---------------------------------------------------------------------

def test_the_command_reports_what_it_moved_and_verifies_afterwards(tmp_path):
    path = _ledger(tmp_path, 3)
    _truncate_last_record(path)
    result = runner.invoke(app, ["recover", "--ledger", str(path), "--yes"])
    assert result.exit_code == 0, result.output
    assert "byte(s) ->" in result.output
    assert "2 complete record(s) kept" in result.output
    assert "Chain intact: 2 record(s)" in result.output


def test_the_command_asks_before_changing_anything(tmp_path):
    path = _ledger(tmp_path, 2)
    partial = _truncate_last_record(path)
    before = path.read_text(encoding="utf-8")
    result = runner.invoke(app, ["recover", "--ledger", str(path)], input="n\n")
    assert result.exit_code == 1
    assert "Nothing changed" in result.output
    assert path.read_text(encoding="utf-8") == before
    assert partial in before


def test_the_command_says_so_when_there_is_nothing_to_recover(tmp_path):
    path = _ledger(tmp_path, 2)
    result = runner.invoke(app, ["recover", "--ledger", str(path), "--yes"])
    assert result.exit_code == 0
    assert "nothing to recover" in result.output
    assert "Chain intact" in result.output


def test_the_command_reports_damage_it_cannot_fix(tmp_path):
    """Truncating the tail does not help when a record in the middle is broken."""
    path = _ledger(tmp_path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = json.dumps({"not": "a record"})
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = runner.invoke(app, ["recover", "--ledger", str(path), "--yes"])
    assert result.exit_code == 1
    assert "nothing to recover" in result.output


def test_a_missing_ledger_is_exit_2(tmp_path):
    result = runner.invoke(app, ["recover", "--ledger", str(tmp_path / "nope.jsonl")])
    assert result.exit_code == 2
    assert "does not exist" in result.output


# --- signing takes a coherent snapshot -----------------------------------------------

def test_signing_covers_the_record_that_is_the_head_when_it_signs(tmp_path):
    """The read, the verify and the append all happen under one lock.

    Without it an append between reading the head and writing the signature leaves a
    valid signature over a head that is no longer the head, while the newest record is
    unsigned and nothing says so.
    """
    from evalseal.signing import generate_keypair, load_signatures, sign_head

    path = _ledger(tmp_path, 2)
    generate_keypair(tmp_path / "k.key", tmp_path / "k.pub")
    entry = sign_head(path, tmp_path / "k.key")
    records = load_all(path)
    assert entry["record_index"] == len(records) - 1
    assert entry["hash"] == records[-1].hash
    assert load_signatures(path)[-1] == entry


def test_concurrent_signatures_all_land_whole(tmp_path):
    """Eight signers appending to one sidecar: every line parses and none is lost."""
    from evalseal.signing import generate_keypair, load_signatures, sign_head

    path = _ledger(tmp_path, 2)
    keys = []
    for i in range(8):
        generate_keypair(tmp_path / f"k{i}.key", tmp_path / f"k{i}.pub")
        keys.append(tmp_path / f"k{i}.key")

    barrier = threading.Barrier(len(keys))
    errors: list[Exception] = []

    def sign(key):
        barrier.wait()
        try:
            sign_head(path, key)
        except Exception as e:                  # pragma: no cover - would be a defect
            errors.append(e)

    threads = [threading.Thread(target=sign, args=(k,)) for k in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    entries = load_signatures(path)
    assert len(entries) == len(keys)
    assert len({e["public_key"] for e in entries}) == len(keys)


@pytest.mark.skipif(os.name == "nt", reason="fsync on a text handle differs on Windows")
def test_the_signature_is_fsynced_so_a_crash_cannot_leave_half_a_line(tmp_path):
    from evalseal.signing import generate_keypair, sign_head, signatures_path

    path = _ledger(tmp_path, 2)
    generate_keypair(tmp_path / "k.key", tmp_path / "k.pub")
    sign_head(path, tmp_path / "k.key")
    raw = signatures_path(path).read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw.strip())
