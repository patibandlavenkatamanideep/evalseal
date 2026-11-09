"""What actually happened when the numbers were produced.

A receipt described the *setup* of a run in detail and the *execution* of it barely at
all. `manifest.target` was the provenance of call number one, standing in for every
call; retries, provider request ids and per-case identity were nowhere; and - the one
that matters most for a CI gate - nothing said whether the run had called a provider or
replayed a cassette. A replayed run and a live one produced receipts that no field
could tell apart.
"""
from __future__ import annotations

import json

import httpx
import pytest
from conftest import make_dataset

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.recording import Cassette
from evalseal.adapters.scorer import LLMJudgeScorer, RegexScorer
from evalseal.adapters.target import AnthropicTarget, LocalCallableTarget
from evalseal.executor import run_eval
from evalseal.validation import InvalidInput, require_valid, validate_record


def _reply(text="yes", *, model="m", request_id="req_abc123", stop_reason="end_turn"):
    return {"id": request_id, "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "usage": {"input_tokens": 9, "output_tokens": 2}}


def _serve(monkeypatch, replies, *, count=None):
    """Answer Messages requests offline. `replies` is cycled if it runs short."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "disposable-test-value")
    sent: list[dict] = []
    queue = list(replies)

    def fake_post(self, url, headers=None, json=None):
        sent.append(json)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    if count is not None:
        assert count  # documentation only
    return sent


def _run(tmp_path, monkeypatch, replies=None, *, record=True, cassette=None, n_repeats=2):
    _serve(monkeypatch, replies or [_reply()])
    cass = Cassette(cassette or (tmp_path / "c.json"), record=record)
    target = AnthropicTarget(model="m", cassette=cass, sleep=lambda _: None)
    return run_eval(make_dataset("q1", "q2"), target, RegexScorer("yes"),
                    n_repeats=n_repeats)


# --- C1-8: a replayed run and a live one were indistinguishable ---------------------

def test_a_live_run_says_it_called_the_provider(tmp_path, monkeypatch):
    live = _run(tmp_path, monkeypatch)
    assert live.manifest.execution.origin == "live"
    assert live.manifest.execution.n_live == 4
    assert live.manifest.execution.n_replayed == 0


def test_a_replayed_run_says_it_replayed(tmp_path, monkeypatch):
    """The gate's blind spot: a stale cassette passing as a run against the model."""
    cassette = tmp_path / "c.json"
    _run(tmp_path, monkeypatch, cassette=cassette)          # records
    replayed = _run(tmp_path, monkeypatch, cassette=cassette, record=False)

    assert replayed.manifest.execution.origin == "replay"
    assert replayed.manifest.execution.n_live == 0
    assert replayed.manifest.execution.n_replayed == 4


def test_the_two_receipts_differ_in_a_field_a_reader_can_find(tmp_path, monkeypatch):
    """The reproduction. On the baseline these two manifests were identical."""
    cassette = tmp_path / "c.json"
    live = _run(tmp_path, monkeypatch, cassette=cassette)
    replayed = _run(tmp_path, monkeypatch, cassette=cassette, record=False)

    assert live.manifest.execution.origin != replayed.manifest.execution.origin


def test_a_partly_replayed_run_is_not_called_live(tmp_path, monkeypatch):
    """Two of four calls from a cassette is not a live run, and not a replayed one."""
    cassette = tmp_path / "c.json"
    one_case = Dataset([Case("c0", "q1", None)], hash="sha256:test")
    _serve(monkeypatch, [_reply()])
    run_eval(one_case, AnthropicTarget(model="m", sleep=lambda _: None,
                                       cassette=Cassette(cassette, record=True)),
             RegexScorer("yes"), n_repeats=2)

    mixed = _run(tmp_path, monkeypatch, cassette=cassette, record=True)
    assert mixed.manifest.execution.origin == "mixed"
    assert mixed.manifest.execution.n_replayed == 2
    assert mixed.manifest.execution.n_live == 2


def test_a_local_callable_is_neither_live_nor_replayed():
    """An in-process function never reached a provider. Calling that "live" would be
    the strongest claim in the receipt and the easiest one to make falsely."""
    record = run_eval(make_dataset("q1"), LocalCallableTarget(lambda p: "yes"),
                      RegexScorer("yes"), n_repeats=2)
    assert record.manifest.execution.origin == "local"


# --- C1-9: one call stood in for every call ------------------------------------------

def test_every_call_is_bound_to_its_case_and_repeat(tmp_path, monkeypatch):
    """The receipt could not say which case or repeat any recorded call belonged to."""
    record = _run(tmp_path, monkeypatch)
    calls = record.manifest.execution.calls

    assert len(calls) == 4
    assert {(c.case_id, c.repeat, c.role) for c in calls} == {
        ("c0", 0, "target"), ("c0", 1, "target"),
        ("c1", 0, "target"), ("c1", 1, "target")}


def test_a_served_model_change_is_attributed_to_the_call_that_saw_it(
        tmp_path, monkeypatch):
    """`manifest.target` is call one. A drift warning said the backend changed; nothing
    said *where*, so nobody could re-run the affected cases."""
    record = _run(tmp_path, monkeypatch, [
        _reply(model="m-old"), _reply(model="m-new"),
        _reply(model="m-new"), _reply(model="m-new")])
    by_unit = {(c.case_id, c.repeat): c.served_model
               for c in record.manifest.execution.calls}
    assert by_unit[("c0", 0)] == "m-old"
    assert by_unit[("c0", 1)] == "m-new"


def test_the_judge_is_recorded_as_its_own_role(tmp_path, monkeypatch):
    """Two calls per unit, two roles. A judged run makes twice as many claims."""
    _serve(monkeypatch, [_reply(text="PASS")])
    judge = AnthropicTarget(model="judge-1", sleep=lambda _: None,
                            cassette=Cassette(tmp_path / "c.json", record=True))
    record = run_eval(make_dataset("q1"), LocalCallableTarget(lambda p: "an answer"),
                      LLMJudgeScorer(judge=judge, rubric="Be strict."), n_repeats=2)

    roles = sorted(c.role for c in record.manifest.execution.calls)
    assert roles == ["judge", "judge", "target", "target"]
    assert record.manifest.execution.origin == "mixed"   # local target, live judge


def test_each_call_records_the_request_and_response_it_used(tmp_path, monkeypatch):
    """Invariant 7: the digest is of the bytes this call actually used."""
    record = _run(tmp_path, monkeypatch, [_reply(text="yes"), _reply(text="no")])
    calls = record.manifest.execution.calls

    assert all(c.request_digest.startswith("sha256:") for c in calls)
    assert all(c.response_digest.startswith("sha256:") for c in calls)
    # Two different responses to the same request are two different digests.
    assert len({c.response_digest for c in calls}) > 1
    # The same prompt at two repeats is the same request.
    same_case = [c for c in calls if c.case_id == "c0"]
    assert same_case[0].request_digest == same_case[1].request_digest


def test_the_provider_request_id_is_kept(tmp_path, monkeypatch):
    """The only handle for checking a call against the provider's own logs."""
    record = _run(tmp_path, monkeypatch, [_reply(request_id="req_one")])
    assert record.manifest.execution.calls[0].provider_request_id == "req_one"


# --- C1-10: retries were invisible ---------------------------------------------------

def test_a_retried_call_says_how_many_attempts_it_took(tmp_path, monkeypatch):
    """A call that failed twice and then succeeded looked like a clean first try."""
    overloaded = httpx.Response(529, json={"error": "overloaded"},
                                request=httpx.Request("POST", "https://x/v1/messages"))
    record = _run(tmp_path, monkeypatch, [overloaded, overloaded, _reply()])
    attempts = [c.attempts for c in record.manifest.execution.calls]
    assert max(attempts) == 3
    assert record.manifest.execution.attempts > len(record.manifest.execution.calls)


def test_a_truncated_call_is_marked_on_the_call(tmp_path, monkeypatch):
    """An answer cut off at the token limit is absent, not wrong - per call, not per run."""
    record = _run(tmp_path, monkeypatch, [_reply(stop_reason="max_tokens")])
    assert all(c.status == "truncated" for c in record.manifest.execution.calls)
    assert record.manifest.execution.n_truncated == 4


# --- what the provider says is a claim ------------------------------------------------

def test_a_reported_fingerprint_is_named_as_the_provider_s_claim(tmp_path, monkeypatch):
    """`reported_fingerprint`, not `backend` - it is what the provider said it was.

    It is not proof of which weights served the request, that the request was served at
    all, or that no other run happened. The field name and its documentation have to
    say so, because a hash in a receipt reads as evidence.
    """
    from evalseal.models import CallProvenance

    doc = (CallProvenance.__doc__ or "") + json.dumps(
        CallProvenance.model_json_schema())
    assert "claim" in doc.lower()


# --- the acceptance criterion ---------------------------------------------------------

def test_a_call_cannot_be_reassigned_to_another_repeat(tmp_path, monkeypatch):
    """C1 acceptance. Two calls claiming one (case, repeat, role) is undecidable: a
    reader asking "what did repeat 1 of c0 return" would get two answers."""
    record = _run(tmp_path, monkeypatch)
    record.manifest.execution.calls[1].repeat = 0        # now two calls claim (c0, 0)

    problems = validate_record(record)
    assert any("repeat" in p.name or "repeat" in p.detail for p in problems), problems
    with pytest.raises(InvalidInput):
        require_valid(record)


def test_a_call_cannot_name_a_case_the_receipt_does_not_have(tmp_path, monkeypatch):
    record = _run(tmp_path, monkeypatch)
    record.manifest.execution.calls[0].case_id = "not-in-this-run"
    with pytest.raises(InvalidInput):
        require_valid(record)


def test_a_call_cannot_claim_a_repeat_outside_the_run(tmp_path, monkeypatch):
    record = _run(tmp_path, monkeypatch)
    record.manifest.execution.calls[0].repeat = 99
    with pytest.raises(InvalidInput):
        require_valid(record)


def test_the_counts_must_match_the_calls_they_summarize(tmp_path, monkeypatch):
    """A summary that disagrees with its own detail is the whole bug class in one line."""
    record = _run(tmp_path, monkeypatch)
    record.manifest.execution.n_live += 7
    with pytest.raises(InvalidInput):
        require_valid(record)


def test_a_clean_run_has_no_problems(tmp_path, monkeypatch):
    assert validate_record(_run(tmp_path, monkeypatch)) == []


# --- where a reader actually looks ----------------------------------------------------

def test_the_markdown_receipt_says_where_the_numbers_came_from(tmp_path, monkeypatch):
    """A field nobody reads is not a fix. Origin goes in the receipt's header."""
    from evalseal.report import to_markdown

    cassette = tmp_path / "c.json"
    live = to_markdown(_run(tmp_path, monkeypatch, cassette=cassette))
    replayed = to_markdown(_run(tmp_path, monkeypatch, cassette=cassette, record=False))

    assert "live provider calls" in live
    assert "no provider was called" in replayed


def test_a_pre_1_7_receipt_gets_no_origin_line():
    """Silence, not a guess. An old receipt does not know where its numbers came from."""
    from evalseal.report import origin_line, to_markdown

    record = run_eval(make_dataset("q1"), LocalCallableTarget(lambda p: "yes"),
                      RegexScorer("yes"), n_repeats=2)
    record.manifest.execution = None
    assert origin_line(record) is None
    assert "Origin:" not in to_markdown(record)


def test_the_gate_says_when_it_is_grading_replayed_numbers(tmp_path, monkeypatch):
    """Replay is a legitimate way to run a gate; passing as a fresh measurement is not.

    Not a check, because a check that failed would break every cassette-based CI run.
    A line a reviewer sees is the honest middle.
    """
    from typer.testing import CliRunner

    from evalseal.cli import app
    from evalseal.ledger import seal_and_append

    cassette = tmp_path / "c.json"
    _run(tmp_path, monkeypatch, cassette=cassette)
    replayed = _run(tmp_path, monkeypatch, cassette=cassette, record=False)
    ledger = tmp_path / "l.jsonl"
    seal_and_append(replayed, ledger)

    result = CliRunner().invoke(app, ["gate", "--ledger", str(ledger)])
    assert result.exit_code == 0, result.output
    # Whitespace-normalized: rich wraps to the console width, so asserting on a raw
    # substring makes the test depend on how wide the terminal happened to be.
    flat = " ".join(result.output.split())
    assert "origin: replay" in flat
    assert "not a fresh measurement of the model" in flat


def test_the_gate_json_carries_the_origin(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from evalseal.cli import app
    from evalseal.ledger import seal_and_append

    ledger = tmp_path / "l.jsonl"
    seal_and_append(_run(tmp_path, monkeypatch), ledger)
    result = CliRunner().invoke(app, ["gate", "--ledger", str(ledger), "--json"])
    assert json.loads(result.output)["origin"] == "live"


def test_a_mixed_run_shows_both_counts_and_its_retries(tmp_path, monkeypatch):
    """The detail in the origin line, for the case where one number is not enough."""
    from evalseal.report import origin_line

    cassette = tmp_path / "c.json"
    one_case = Dataset([Case("c0", "q1", None)], hash="sha256:test")
    _serve(monkeypatch, [_reply()])
    run_eval(one_case, AnthropicTarget(model="m", sleep=lambda _: None,
                                       cassette=Cassette(cassette, record=True)),
             RegexScorer("yes"), n_repeats=2)

    overloaded = httpx.Response(529, json={"error": "overloaded"},
                                request=httpx.Request("POST", "https://x/v1/messages"))
    mixed = _run(tmp_path, monkeypatch, [overloaded, _reply()], cassette=cassette)
    line = origin_line(mixed)
    assert "partly replayed" in line
    assert "2 live, 2 replayed" in line
    assert "HTTP attempt(s)" in line


def test_negative_counts_are_not_counts(tmp_path, monkeypatch):
    """`sequence` and `attempts` count occurrences; a negative one is not a count."""
    record = _run(tmp_path, monkeypatch)
    record.manifest.execution.calls[0].attempts = -1
    with pytest.raises(InvalidInput, match="negative"):
        require_valid(record)


def test_an_origin_claim_cannot_contradict_the_calls(tmp_path, monkeypatch):
    """The strongest claim in the receipt, checked against its own evidence."""
    record = _run(tmp_path, monkeypatch)
    record.manifest.execution.origin = "replay"
    with pytest.raises(InvalidInput, match="origin"):
        require_valid(record)


def test_an_execution_manifest_that_lists_no_calls_is_not_consistent():
    """Emptying the call list used to leave a receipt that validated cleanly.

    Found by writing this test: the coverage check was guarded on there being calls at
    all, so "a run happened and here is nothing" passed. An *absent* manifest is a
    different claim - a receipt sealed before 1.7 keeps none - and still passes.
    """
    record = run_eval(make_dataset("q1"), LocalCallableTarget(lambda p: "yes"),
                      RegexScorer("yes"), n_repeats=1)
    record.manifest.execution.calls = []
    record.manifest.execution.n_calls = 0
    record.manifest.execution.attempts = 0
    problems = [p.name for p in validate_record(record)]
    # Nothing contradicts the stated origin, so it is not second-guessed...
    assert "manifest.execution.origin" not in problems
    # ...but a score with no call behind it cannot be traced to anything.
    assert "manifest.execution.calls" in problems
    with pytest.raises(InvalidInput):
        require_valid(record)
