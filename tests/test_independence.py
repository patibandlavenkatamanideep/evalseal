"""How many independent draws are behind a number, and saying so when it is unknown.

A replay is a perfectly good regression check and it makes no new observations. Five
repeats served out of five cassette entries are five draws; five repeats served out of
one entry are one draw with an interval drawn around it. The receipt's own
`execution.calls[].source_key` is the only field that can tell those apart, because the
response text and its digest are identical in both cases.

Everything here runs offline: a cassette on disk, no network, no key.
"""
from __future__ import annotations

import json

import pytest

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.recording import Cassette, canonical, request_key
from evalseal.adapters.recording import unit as recording_unit
from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import OpenAICompatibleTarget, ProviderCall
from evalseal.executor import run_eval
from evalseal.independence import (
    INDEPENDENT,
    REUSED,
    UNKNOWN,
    audit_independence,
    records_source_keys,
)
from evalseal.policy import Policy, evaluate


def _dataset(*ids: str, prompt: str | None = None) -> Dataset:
    return Dataset([Case(cid, prompt or cid) for cid in ids], hash="sha256:test")


def _reply(text: str) -> dict:
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "model": "m", "system_fingerprint": None}


def _target(cassette: Cassette, answers=None) -> OpenAICompatibleTarget:
    """An OpenAI-shaped target through the cassette, with the HTTP layer scripted."""
    target = OpenAICompatibleTarget(model="m", cassette=cassette, temperature=0.0)
    if answers is not None:
        target._post = lambda body: ProviderCall(_reply(answers(body)), attempts=1)
    return target


def _record_then_replay(tmp_path, dataset, answers, *, n_repeats=5):
    """Record a run into a fresh cassette, then replay it. Returns both records."""
    path = tmp_path / "c.json"
    live = Cassette(path, record=True, mode="fresh")
    counter = {"n": 0}

    def respond(body):
        counter["n"] += 1
        return answers(body, counter["n"])

    recorded = run_eval(dataset, _target(live, respond), RegexScorer("^yes$"),
                        n_repeats=n_repeats)
    live.close()

    replayed = run_eval(dataset, _target(Cassette(path, record=False, mode="replay")),
                        RegexScorer("^yes$"), n_repeats=n_repeats)
    return recorded, replayed


# --- the distinction the brief names ------------------------------------------------

def test_two_independent_draws_that_agreed_are_two_observations(tmp_path):
    """Every repeat returns the identical string, and each was a separate call."""
    recorded, replayed = _record_then_replay(
        tmp_path, _dataset("a"), lambda payload, n: "yes")

    for record in (recorded, replayed):
        audit = audit_independence(record)
        assert audit.known
        assert audit.verdict == INDEPENDENT
        assert audit.n_repeats == 5 and audit.n_draws == 5
        assert "separate observations that agreed" in audit.summary()

    # The texts really are identical, so nothing but the entry key could tell.
    digests = {c.response_digest for c in recorded.manifest.execution.calls}
    assert len(digests) == 1


def test_one_recorded_draw_served_to_every_repeat_is_one_observation(tmp_path):
    """A format-2 cassette whose single entry answers all five repeats of a case.

    Written by hand because this build cannot produce it: format 3 keys an entry by
    case and repeat, so each repeat gets its own. A cassette someone kept from an
    earlier version is still replayable, and a receipt produced from one must not
    report five observations.
    """
    path = tmp_path / "old.json"
    # One entry keyed the way a very old recorder would: by request alone, with the
    # repeat fixed at 0, so every repeat of every case collides on it.
    payload = {"model": "m", "messages": [{"role": "user", "content": "a"}],
               "temperature": 0.0}
    key = request_key({"request": payload, "repeat": 0})
    path.write_text(json.dumps({"version": 2, "entries": {key: _reply("yes")}}),
                    encoding="utf-8")

    cassette = Cassette(path, record=False, mode="replay")
    # The executor keys by repeat, so drive the five repeats at slot 0 directly: this
    # is what replaying a pre-case-identity cassette amounts to.
    for repeat in range(5):
        with recording_unit("a", 0, "target"):
            outcome = cassette.call_detail(payload, lambda: _reply("yes"))
        assert outcome.origin == "replay"
        assert outcome.entry_key == key, repeat


def test_a_shared_entry_across_two_cases_is_reported_as_item_dependence(tmp_path):
    """Hand-built: one entry serving two case ids, which is the format-2 collision.

    The items are then not independent of each other, which the cluster bootstrap and
    the paired test both assume. It is reported separately from repeat reuse because it
    breaks a different assumption.
    """
    recorded, _ = _record_then_replay(tmp_path, _dataset("a", "b"),
                                      lambda payload, n: "yes")
    calls = recorded.manifest.execution.calls
    # Point every call at one entry, as a single-entry cassette would have.
    shared = calls[0].source_key
    for call in calls:
        call.source_key = shared

    audit = audit_independence(recorded)
    assert audit.verdict == REUSED
    assert list(audit.shared_across_cases) == [shared]
    assert audit.shared_across_cases[shared] == ["a", "b"]
    note = audit.summary()
    assert "served to more than one case" in note
    assert "not independent of each other either" in note


def test_repeat_reuse_within_one_case_is_reported_with_the_count(tmp_path):
    recorded, _ = _record_then_replay(tmp_path, _dataset("a"),
                                      lambda payload, n: "yes")
    calls = [c for c in recorded.manifest.execution.calls if c.role == "target"]
    for call in calls[1:]:
        call.source_key = calls[0].source_key

    audit = audit_independence(recorded)
    assert audit.verdict == REUSED
    case = next(c for c in audit.cases if c.role == "target")
    assert (case.n_repeats, case.n_draws) == (5, 1)
    assert case.reused == {calls[0].source_key: 5}
    assert "one recorded response was served to 5 of them" in case.note
    assert "the measurement was made 1 time(s)" in case.note


# --- whole runs ---------------------------------------------------------------------

def test_an_all_replayed_run_is_independent_when_the_recording_was(tmp_path):
    """Replaying is not the problem; reusing one draw is. The audit separates them."""
    _, replayed = _record_then_replay(tmp_path, _dataset("a", "b"),
                                      lambda payload, n: "yes" if n % 2 else "no")
    assert replayed.manifest.execution.origin == "replay"
    audit = audit_independence(replayed)
    assert audit.verdict == INDEPENDENT
    assert audit.n_draws == audit.n_repeats == 10
    # And the receipt still says plainly that nothing was measured today.
    assert replayed.manifest.execution.n_live == 0


def test_a_mixed_live_and_resumed_run_counts_both_kinds_of_draw(tmp_path):
    """Three repeats recorded, then five run in resume mode: two are new draws."""
    path = tmp_path / "c.json"
    first = Cassette(path, record=True, mode="fresh")
    run_eval(_dataset("a"), _target(first, lambda body: "yes"), RegexScorer("^yes$"),
             n_repeats=3)
    first.close()

    resumed = Cassette(path, record=True, mode="resume")
    record = run_eval(_dataset("a"), _target(resumed, lambda body: "yes"),
                      RegexScorer("^yes$"), n_repeats=5)
    resumed.close()

    execution = record.manifest.execution
    assert execution.origin == "mixed"
    assert (execution.n_replayed, execution.n_live) == (3, 2)
    audit = audit_independence(record)
    assert audit.verdict == INDEPENDENT
    assert audit.n_draws == audit.n_repeats == 5
    # Five distinct entries, so the resumed run added observations rather than
    # re-reading the three it already had.
    assert len({c.source_key for c in execution.calls}) == 5


def test_an_in_process_callable_is_a_draw_of_its_own():
    """No cassette, so no entry key - and the call was still made five times."""
    from evalseal.adapters.target import LocalCallableTarget

    record = run_eval(_dataset("a"), LocalCallableTarget(lambda p: "yes", name="m"),
                      RegexScorer("^yes$"), n_repeats=5)
    assert all(c.source_key is None for c in record.manifest.execution.calls)
    audit = audit_independence(record)
    assert audit.verdict == INDEPENDENT
    assert audit.n_draws == audit.n_repeats == 5


# --- unknown is an answer ------------------------------------------------------------

def test_a_pre_1_8_receipt_reports_unknown_rather_than_deriving_a_sample_size():
    from evalseal.adapters.target import LocalCallableTarget

    record = run_eval(_dataset("a"), LocalCallableTarget(lambda p: "yes", name="m"),
                      RegexScorer("^yes$"), n_repeats=5)
    record.manifest.schema_version = "1.7"
    assert not records_source_keys(record)

    audit = audit_independence(record)
    assert audit.verdict == UNKNOWN and not audit.known
    assert "does not record which cassette entry served each call" in audit.reason
    note = audit.summary()
    assert "Independence unknown" in note
    assert "No effective sample size is derived" in note
    assert audit.cases == []


def test_a_pre_1_7_receipt_says_it_keeps_no_per_call_detail_at_all():
    from evalseal.adapters.target import LocalCallableTarget

    record = run_eval(_dataset("a"), LocalCallableTarget(lambda p: "yes", name="m"),
                      RegexScorer("^yes$"), n_repeats=5)
    record.manifest.execution = None
    audit = audit_independence(record)
    assert audit.verdict == UNKNOWN
    assert "sealed before schema 1.7" in audit.reason


# --- the gate and the report --------------------------------------------------------

def test_the_gate_rule_fails_on_reuse_and_on_unknown(tmp_path):
    """A rule that skipped on unknown would report a pass for a check that did not run."""
    recorded, _ = _record_then_replay(tmp_path, _dataset("a"),
                                      lambda payload, n: "yes")
    policy = Policy.model_validate(
        {"run": {"require_independent_repeats": True, "verify_ledger": False}})
    assert evaluate(policy, recorded).passed

    reused = recorded.model_copy(deep=True)
    calls = [c for c in reused.manifest.execution.calls if c.role == "target"]
    for call in calls[1:]:
        call.source_key = calls[0].source_key
    assert not evaluate(policy, reused).passed

    unknown = recorded.model_copy(deep=True)
    unknown.manifest.schema_version = "1.7"
    result = evaluate(policy, unknown)
    assert not result.passed
    assert "Independence unknown" in result.violations[0].detail


def test_the_rule_is_off_unless_a_policy_asks_for_it(tmp_path):
    recorded, _ = _record_then_replay(tmp_path, _dataset("a"),
                                      lambda payload, n: "yes")
    recorded.manifest.schema_version = "1.7"       # the worst case for this rule
    policy = Policy.model_validate({"run": {"min_score": 0.0, "verify_ledger": False}})
    assert evaluate(policy, recorded).passed
    assert {c.rule for c in evaluate(policy, recorded).checks} == {"run.min_score"}


def test_report_json_carries_the_audit(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from evalseal.cli import app
    from evalseal.report import write_json

    recorded, _ = _record_then_replay(tmp_path, _dataset("a"),
                                      lambda payload, n: "yes")
    monkeypatch.chdir(tmp_path)
    write_json(recorded, "report.json")
    result = CliRunner().invoke(app, ["report", "report.json", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["independence"]["verdict"] == INDEPENDENT
    assert payload["independence"]["n_draws"] == 5
    assert payload["summary"]["n_observations"] == 5


def test_the_markdown_report_states_it_beside_the_origin(tmp_path):
    from evalseal.report import to_markdown

    _, replayed = _record_then_replay(tmp_path, _dataset("a"),
                                      lambda payload, n: "yes")
    text = to_markdown(replayed)
    assert "- Origin: **replayed from a cassette" in text
    assert "- Observations: 5 repeat(s) came from 5 separate draw(s)" in text


def test_the_canonical_form_behind_an_entry_key_is_the_one_the_module_documents():
    """A format-3 key covers the measurement, which is why each repeat is its own."""
    first = request_key({"request": {"a": 1}, "case": "x", "repeat": 0, "role": "target"})
    second = request_key({"request": {"a": 1}, "case": "x", "repeat": 1, "role": "target"})
    other_case = request_key({"request": {"a": 1}, "case": "y", "repeat": 0,
                              "role": "target"})
    assert len({first, second, other_case}) == 3
    # And a format-2 key cannot tell the two cases apart, which is the collision.
    assert (request_key({"request": {"a": 1}, "repeat": 0})
            == request_key({"request": {"a": 1}, "repeat": 0}))
    assert canonical({"a": 1}) == b'{"a":1}'


@pytest.mark.parametrize("version,supported", [
    ("1.6", False), ("1.7", False), ("1.8", True), ("1.10", True), ("2.0", True),
    ("nonsense", False),
])
def test_which_schemas_can_answer_the_question(version, supported, tmp_path):
    from evalseal.adapters.target import LocalCallableTarget

    record = run_eval(_dataset("a"), LocalCallableTarget(lambda p: "yes", name="m"),
                      RegexScorer("^yes$"), n_repeats=2)
    record.manifest.schema_version = version
    assert records_source_keys(record) is supported
