"""Importing a real promptfoo export, at the boundary a user meets.

The fixtures under `tests/fixtures/importers/promptfoo/` are **genuine promptfoo
0.125.1 artifacts** with synthetic evaluation data inside them; `PROVENANCE.md` there
records the version, the command and the digest. Everything under `malformed/` is
hand-written and is kept in its own directory so the two can never be confused.

Four demonstrations, in the order someone would do them:

a. import one export offline and inspect a specific case
b. verify against the original export, edit one result, watch verification fail
c. compare two genuine exports with a known grading change
d. import an export with an errored case and see what it does to the report and the gate
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalseal.cli import app
from evalseal.diffing import diff_records, load_receipt
from evalseal.importers.base import ImportRefused
from evalseal.importers.promptfoo import SUPPORTED_EXPORT_SCHEMAS, import_export
from evalseal.ledger import check_record_hash, seal_and_append
from evalseal.policy import Policy, evaluate

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "importers" / "promptfoo"
EXPORT = FIXTURES / "arithmetic-export.json"
STRICT = FIXTURES / "arithmetic-export-strict.json"
MALFORMED = FIXTURES / "malformed"

#: Recorded in PROVENANCE.md. If the fixture is edited, this fails - which is the point.
EXPORT_SHA = "sha256:8fa9e5f632bb008646fe51c4ab1998afbb0eff2e8bff599d6f4cbb1c61a7a776"
STRICT_SHA = "sha256:e59142d9d6dfe41e5414500e233c3c47b1968d67af753cb868eb835ecddbd092"

runner = CliRunner()


def _wide() -> CliRunner:
    """A plain runner. The width is pinned in conftest, before `cli` is imported.

    `CliRunner(env={"COLUMNS": ...})` looks like it would do this and does not: rich
    reads COLUMNS when the console is constructed, which is at import time.
    """
    return CliRunner()


# --- the fixture is what PROVENANCE.md says it is ------------------------------------

def test_the_fixture_is_the_export_its_provenance_describes():
    import hashlib

    raw = EXPORT.read_bytes()
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == EXPORT_SHA
    export = json.loads(raw)
    assert export["metadata"]["promptfooVersion"] == "0.125.1"
    assert export["results"]["version"] in SUPPORTED_EXPORT_SCHEMAS
    assert len(export["results"]["results"]) == 15

    provenance = (FIXTURES / "PROVENANCE.md").read_text(encoding="utf-8")
    assert EXPORT_SHA.split(":", 1)[1] in provenance
    assert "0.125.1" in provenance
    assert str(len(raw)) in provenance
    # It must not claim to be something it is not.
    assert "synthetic" in provenance
    assert "not a customer's export" in provenance


def test_promptfoos_own_totals_corroborate_the_import():
    """An oracle that is not this importer: promptfoo's own `stats`.

    9 passed of 15 rows is 0.6, and the imported receipt's mean over five cases is
    0.6 as well. They are computed differently - rows versus the mean of case means -
    and agree here because every case has the same number of rows.
    """
    export = json.loads(EXPORT.read_text(encoding="utf-8"))
    stats = export["results"]["stats"]
    assert (stats["successes"], stats["failures"], stats["errors"]) == (9, 3, 3)

    record = import_export(EXPORT)
    assert record.aggregate.mean_score == pytest.approx(stats["successes"] / 15)
    assert record.aggregate.n_observations == 15
    assert record.aggregate.n_unscorable == stats["errors"]


# --- (a) import one export and inspect a case ----------------------------------------

def test_repeats_are_grouped_by_the_field_that_groups_them():
    """`testIdx` is a flat row index. Keying on it gives fifteen cases of one repeat."""
    record = import_export(EXPORT)
    assert len(record.results) == 5
    assert all(len(c.scores) == 3 for c in record.results)

    export = json.loads(EXPORT.read_text(encoding="utf-8"))
    rows = export["results"]["results"]
    assert [r["testIdx"] for r in rows[:3]] == [0, 1, 2]       # one case, three repeats
    assert len({r["repeatGroupId"] for r in rows}) == 5


def test_a_specific_case_reads_the_way_the_export_does():
    """The failing case: the provider answers 7 and the assertion wants 6."""
    record = import_export(EXPORT)
    case = next(c for c in record.results
                if c.case_id == "a question the provider answers wrongly")
    assert case.scores == [0.0, 0.0, 0.0]
    assert case.fail_count == 3 and case.unscorable_count == 0
    assert case.majority_verdict == 0

    calls = [c for c in record.manifest.execution.calls if c.case_id == case.case_id]
    assert len(calls) == 3
    assert {c.repeat for c in calls} == {0, 1, 2}
    assert all(c.origin == "imported" for c in calls)
    assert all(c.requested_model == "deterministic-local:fixture-v1" for c in calls)


def test_a_row_with_two_assertions_is_one_observation():
    record = import_export(EXPORT)
    case = next(c for c in record.results if c.case_id == "two assertions on one case")
    assert len(case.scores) == 3           # three repeats, not six assertions
    export = json.loads(EXPORT.read_text(encoding="utf-8"))
    row = next(r for r in export["results"]["results"]
               if r["testCase"]["description"] == "two assertions on one case")
    assert len(row["gradingResult"]["componentResults"]) == 2


def test_nothing_claims_a_live_call_or_a_served_model():
    record = import_export(EXPORT)
    execution = record.manifest.execution
    assert execution.origin == "imported"
    assert (execution.n_live, execution.n_replayed, execution.attempts) == (0, 0, 0)
    assert record.manifest.target.served_model is None
    assert record.manifest.target.system_fingerprint is None
    assert record.manifest.scorer.impl_hash is None
    # And the export's own timestamps are kept as claims, not used as measurement time.
    claimed = record.manifest.imported.claimed_times
    assert set(claimed) >= {"exportedAt", "evaluationCreatedAt"}
    assert execution.started_at not in claimed.values()


def test_the_gaps_are_named_rather_than_left_as_nulls():
    gaps = import_export(EXPORT).manifest.imported.gaps
    joined = " ".join(gaps)
    assert any(g.startswith("scorer.impl_hash") for g in gaps)
    assert any(g.startswith("target.served_model") for g in gaps)
    assert any(g.startswith("repeat independence") for g in gaps)
    assert "cached" in joined        # promptfoo caches by default; the export says so


def test_the_import_command_writes_a_sealed_receipt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _wide().invoke(app, ["import", str(EXPORT), "--ledger", "l.jsonl"])
    assert result.exit_code == 0, result.output
    assert "Imported promptfoo 0.125.1" in result.output
    assert "5 case(s), 15 observation(s), mean 0.600" in result.output
    assert "3 field(s) this export could not fill" in result.output

    record = load_receipt(tmp_path / "report.json")
    assert check_record_hash(record) == ("ok", record.manifest.schema_version)
    assert record.manifest.imported.source_sha256 == EXPORT_SHA
    assert (tmp_path / "report.md").is_file()


def test_the_report_leads_with_the_fact_that_it_was_imported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _wide().invoke(app, ["import", str(EXPORT), "--ledger", "l.jsonl", "--quiet"])
    text = _wide().invoke(app, ["report", "report.json"]).output
    assert "imported from promptfoo 0.125.1" in text
    assert "not measured here" in text
    assert "gap - scorer.impl_hash" in text

    payload = json.loads(
        _wide().invoke(app, ["report", "report.json", "--json"]).output)
    assert payload["imported"]["tool"] == "promptfoo"
    assert payload["imported"]["source_sha256"] == EXPORT_SHA
    assert len(payload["imported"]["gaps"]) == 3
    assert payload["summary"]["n_unscorable"] == 3


# --- (b) verify against the export, then edit it --------------------------------------

def test_the_export_is_bound_to_the_receipt_and_verifies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    copy = tmp_path / "export.json"
    copy.write_bytes(EXPORT.read_bytes())
    assert _wide().invoke(
        app, ["import", str(copy), "--ledger", "l.jsonl", "--quiet"]).exit_code == 0

    result = _wide().invoke(app, ["verify", "--ledger", "l.jsonl", "--artifacts"])
    assert result.exit_code == 0, result.output
    assert "import_source" in result.output
    assert "matches the sealed digest" in result.output


def test_editing_one_result_in_the_export_fails_verification(tmp_path, monkeypatch):
    """The whole claim an imported receipt makes, demonstrated end to end."""
    monkeypatch.chdir(tmp_path)
    copy = tmp_path / "export.json"
    copy.write_bytes(EXPORT.read_bytes())
    _wide().invoke(app, ["import", str(copy), "--ledger", "l.jsonl", "--quiet"])

    export = json.loads(copy.read_text(encoding="utf-8"))
    failing = next(r for r in export["results"]["results"]
                   if r["failureReason"] == 1)
    failing["success"] = True                       # a failure turned into a pass
    failing["failureReason"] = 0
    copy.write_text(json.dumps(export), encoding="utf-8")

    result = _wide().invoke(app, ["verify", "--ledger", "l.jsonl", "--artifacts"])
    assert result.exit_code == 1, result.output
    assert "changed" in result.output
    assert "import_source" in result.output


def test_the_receipt_itself_still_verifies_after_the_export_is_edited(tmp_path,
                                                                     monkeypatch):
    """The chain covers the receipt; the artifact check covers the file beside it.

    A reader has to be able to tell "someone edited the export" from "someone edited
    the receipt", and these are two different checks with two different answers.
    """
    monkeypatch.chdir(tmp_path)
    copy = tmp_path / "export.json"
    copy.write_bytes(EXPORT.read_bytes())
    _wide().invoke(app, ["import", str(copy), "--ledger", "l.jsonl", "--quiet"])
    copy.write_text("{}", encoding="utf-8")

    assert _wide().invoke(app, ["verify", "--ledger", "l.jsonl"]).exit_code == 0
    assert _wide().invoke(
        app, ["verify", "--ledger", "l.jsonl", "--artifacts"]).exit_code == 1


# --- (c) compare two genuine exports --------------------------------------------------

def test_the_second_export_differs_only_in_one_assertion():
    """Both are genuine promptfoo output; the config changed one expected value."""
    import hashlib

    raw = STRICT.read_bytes()
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == STRICT_SHA
    strict = json.loads(raw)
    assert strict["results"]["stats"]["successes"] == 6      # was 9
    assert strict["metadata"]["promptfooVersion"] == "0.125.1"


def test_comparing_two_imports_reports_what_the_evidence_supports():
    before, after = import_export(EXPORT), import_export(STRICT)
    result = diff_records(before, after)

    # The grading changed and the receipts cannot prove it either way, so the only
    # honest verdict is that they are not comparable.
    assert not result.comparable
    assert result.score_verdict == "non_comparable"
    assert "imported from promptfoo 0.125.1" in result.reason
    assert "scorer.impl_hash" in result.reason

    # What *is* visible: the case that changed, and by how much.
    assert result.score_before == pytest.approx(0.6)
    assert result.score_after == pytest.approx(0.4)
    assert result.paired is not None and result.paired.n_items == 5
    assert result.paired.discordant_regressed == 1
    # No case split its repeats evenly, so none was excluded from the paired test.
    assert result.paired.ambiguous_items == []


def test_two_imports_missing_the_same_field_are_not_thereby_comparable():
    """The trap: a fingerprint over a manifest of nulls is still a value, and two
    such values are equal."""
    from evalseal.ledger import evaluator_fingerprint

    before, after = import_export(EXPORT), import_export(EXPORT)
    # The fingerprints *do* match, which is exactly why comparability cannot rest on
    # them for an import.
    assert evaluator_fingerprint(before) == evaluator_fingerprint(after)
    result = diff_records(before, after)
    assert not result.comparable
    assert "Both sides are missing it" in result.reason
    assert "agree about nothing" in result.reason


def test_an_import_against_a_native_run_is_also_not_comparable():
    from conftest import make_dataset

    from evalseal.adapters.scorer import RegexScorer
    from evalseal.adapters.target import LocalCallableTarget
    from evalseal.executor import run_eval

    native = run_eval(make_dataset("q"),
                      LocalCallableTarget(lambda p: "yes", name="m"),
                      RegexScorer("yes"), n_repeats=3)
    result = diff_records(native, import_export(EXPORT))
    assert not result.comparable
    assert "the later run was imported" in result.reason


def test_the_cli_diff_of_two_imports_exits_zero_and_says_why(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _wide().invoke(app, ["import", str(EXPORT), "--ledger", "l.jsonl", "--quiet",
                         "--out", "a.json"])
    _wide().invoke(app, ["import", str(STRICT), "--ledger", "l.jsonl", "--quiet",
                         "--out", "b.json"])
    result = _wide().invoke(app, ["diff", "a.json", "b.json", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["comparable"] is False
    assert payload["verdict"] == "non_comparable"
    assert "scorer.impl_hash" in payload["reason"]


# --- (d) an errored case, through the report and the gate -----------------------------

def test_an_errored_case_is_unscorable_and_not_a_failure():
    record = import_export(EXPORT)
    errored = next(c for c in record.results
                   if c.case_id == "a case the provider errors on")
    assert errored.unscorable_count == 3
    assert errored.fail_count == 0 and errored.pass_count == 0
    assert errored.majority_verdict is None
    assert "no answer configured" in errored.unscorable_reason

    failed = next(c for c in record.results
                  if c.case_id == "a question the provider answers wrongly")
    # Both contribute 0.0 to the mean, and they are not the same result.
    assert errored.mean == failed.mean == 0.0
    assert failed.fail_count == 3 and failed.unscorable_count == 0


def test_a_strict_gate_fails_on_the_errored_case_and_names_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _wide().invoke(app, ["import", str(EXPORT), "--ledger", "l.jsonl", "--quiet"])
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "version": 1,
        "run": {"min_score": 0.5, "require_every_case_scored": True,
                "max_unscorable_fraction": 0.1}}), encoding="utf-8")

    result = _wide().invoke(app, ["gate", "--ledger", "l.jsonl",
                                  "--policy", str(policy)])
    assert result.exit_code == 3, result.output
    payload = json.loads(_wide().invoke(app, [
        "gate", "--ledger", "l.jsonl", "--policy", str(policy), "--json"]).output)
    failed = {c["rule"]: c["detail"] for c in payload["violations"]}
    assert "run.require_every_case_scored" in failed
    assert "a case the provider errors on" in failed["run.require_every_case_scored"]
    assert "run.max_unscorable_fraction" in failed
    # The score floor passed, which is the point: it cannot see this.
    floor = next(c for c in payload["checks"] if c["rule"] == "run.min_score")
    assert floor["passed"] is True
    assert "partly ungraded run" in floor["detail"]


def test_a_policy_that_does_not_ask_about_gradeability_still_passes(tmp_path,
                                                                   monkeypatch):
    monkeypatch.chdir(tmp_path)
    _wide().invoke(app, ["import", str(EXPORT), "--ledger", "l.jsonl", "--quiet"])
    policy = tmp_path / "old.json"
    policy.write_text(json.dumps({"version": 1, "run": {"min_score": 0.5}}),
                      encoding="utf-8")
    assert _wide().invoke(app, ["gate", "--ledger", "l.jsonl",
                                "--policy", str(policy)]).exit_code == 0


def test_independence_is_unknown_for_an_imported_receipt():
    """Not `independent`. The report said "15 repeat(s) came from 15 separate draw(s)"
    on the same page as a warning that caching was on.

    The rows are distinct; that is not the question. promptfoo caches by default and
    does not mark per row which were served from cache, so a row count is not a draw
    count - and no call in an imported receipt carries a `source_key`, because that
    field means "the cassette entry that served this call".
    """
    from evalseal.independence import audit_independence

    record = import_export(EXPORT)
    audit = audit_independence(record)
    assert audit.verdict == "unknown"
    assert audit.known is False
    assert "does not record whether a repeat was a fresh draw" in audit.reason
    assert "No effective sample size is derived" in audit.summary()
    assert all(c.source_key is None for c in record.manifest.execution.calls)
    assert any("CACHING WAS ENABLED" in w for w in record.aggregate.warnings)


# --- refusals --------------------------------------------------------------------------

@pytest.mark.parametrize("name,fragment", [
    ("not-json.json", "not readable as JSON"),
    ("json-array.json", "not an object"),
    ("no-results-key.json", "has no `results` object"),
    ("unknown-export-schema.json", "has only been read against"),
    ("no-rows.json", "has no rows"),
    ("contradictory-row.json", "will not choose between them"),
    ("duplicate-repeat-index.json", "same `repeatIndex`"),
    ("assertion-failed-but-success.json", "an assertion failed"),
])
def test_a_malformed_export_is_refused_with_the_field_named(name, fragment):
    with pytest.raises(ImportRefused) as caught:
        import_export(MALFORMED / name)
    assert fragment in str(caught.value), str(caught.value)


def test_the_malformed_fixtures_are_labelled_as_hand_written():
    readme = (MALFORMED / "README.md").read_text(encoding="utf-8")
    assert "Not promptfoo artifacts" in readme
    assert "written by hand" in readme


def test_a_missing_file_is_refused_rather_than_tracebacking(tmp_path):
    with pytest.raises(ImportRefused, match="does not exist"):
        import_export(tmp_path / "nope.json")


def test_the_cli_exits_2_on_a_refusal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _wide().invoke(app, ["import", str(MALFORMED / "no-rows.json"),
                                  "--ledger", "l.jsonl"])
    assert result.exit_code == 2
    assert "has no rows" in result.output
    assert not (tmp_path / "report.json").exists()


def test_an_unknown_tool_is_refused_without_guessing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _wide().invoke(app, ["import", str(EXPORT), "--tool", "braintrust",
                                  "--ledger", "l.jsonl"])
    assert result.exit_code == 2
    assert "No importer for 'braintrust'" in result.output
    assert "deliberately not guessed at" in result.output


def test_an_empty_observation_set_is_refused():
    from evalseal.importers.base import Gaps, build_record
    from evalseal.models import ImportProvenance, ScorerProvenance, TargetProvenance

    with pytest.raises(ImportRefused, match="no observations"):
        build_record(
            observations=[], gaps=Gaps(), dataset_hash="sha256:x", suite=None,
            scorer=ScorerProvenance(type="exact"),
            target=TargetProvenance(requested_model="m", base_url="imported://x",
                                    effective_params={}),
            n_repeats=1, warnings=[],
            imported=ImportProvenance(tool="t", source_sha256="sha256:x",
                                      source_bytes=1, imported_at="now"))


# --- the receipt is a receipt ----------------------------------------------------------

def test_an_imported_receipt_seals_and_rehashes_like_any_other(tmp_path):
    ledger = tmp_path / "l.jsonl"
    record = seal_and_append(import_export(EXPORT), ledger)
    assert check_record_hash(record) == ("ok", record.manifest.schema_version)
    assert record.manifest.schema_version == "1.9"


def test_the_policy_surface_is_unchanged_for_an_import():
    """An imported receipt is gated by the same rules as any other."""
    record = import_export(EXPORT)
    policy = Policy.model_validate(
        {"run": {"min_score": 0.5, "verify_ledger": False}})
    assert evaluate(policy, record).passed


def test_a_case_with_no_graded_repeat_does_not_render_as_three_failures():
    """It showed `FFF`, which reads as three graded failures. Nothing was graded."""
    from evalseal.report import verdict_sequence

    record = import_export(EXPORT)
    errored = next(c for c in record.results
                   if c.case_id == "a case the provider errors on")
    failed = next(c for c in record.results
                  if c.case_id == "a question the provider answers wrongly")
    assert verdict_sequence(errored) == "---"
    assert verdict_sequence(failed) == "FFF"


def test_a_partly_graded_case_marks_the_ungraded_repeats_without_placing_them():
    from evalseal.models import CaseResult
    from evalseal.report import verdict_sequence

    case = CaseResult(
        case_id="x", scores=[1.0, 1.0, 0.0], mean=2 / 3, ci95=(0.0, 1.0),
        flip_rate=0.0, stability="UNSTABLE", majority_verdict=1,
        verdicts=[1, 1], pass_count=2, fail_count=0, unscorable_count=1)
    # Two passes and one ungraded repeat. The receipt does not record which position
    # the ungraded one was in, so it is appended and the docstring says so.
    assert verdict_sequence(case) == "PP-"


def test_the_human_diff_says_why_not_only_that(tmp_path, monkeypatch):
    """It printed "evaluator configuration changed" and a table of hashes. For an
    import the real answer - the grading code is another tool's - was only in --json."""
    from evalseal.report import render_diff

    text = render_diff(diff_records(import_export(EXPORT), import_export(STRICT)))
    assert "Not directly comparable" in text
    assert "imported from promptfoo 0.125.1" in text
    assert "scorer.impl_hash" in text
    assert "docs/importers/promptfoo.md" in text


# --- the quickstart's numbers --------------------------------------------------------

QUICKSTART = (Path(__file__).resolve().parent.parent / "examples" / "integrations"
              / "promptfoo" / "README.md")


def _prose(path: Path) -> str:
    """The text with its line wrapping and blockquote markers collapsed.

    A claim that spans a line break is still the claim; asserting on the raw file
    would make these tests fail when someone rewraps a paragraph. The leading `>` of a
    continued blockquote line goes too, or it lands in the middle of a sentence.
    """
    lines = [line.lstrip().removeprefix(">").strip()
             for line in path.read_text(encoding="utf-8").splitlines()]
    return " ".join(" ".join(lines).split())


def test_the_quickstart_quotes_the_numbers_the_commands_produce():
    """Prose that has drifted from the tool teaches the wrong thing confidently."""
    text = QUICKSTART.read_text(encoding="utf-8")
    record = import_export(EXPORT)

    assert f"{record.aggregate.n_cases} case(s), " \
           f"{record.aggregate.n_observations} observation(s), mean " \
           f"{record.aggregate.mean_score:.3f}" in text
    assert record.manifest.imported.source_sha256[:27] in text
    assert str(record.manifest.imported.source_bytes) in text
    assert f"{len(record.manifest.imported.gaps)} field(s) this export could not " \
           "fill" in text
    strict = import_export(STRICT)
    assert f"{strict.aggregate.mean_score:.3f}" in text


def test_the_quickstart_states_the_trust_boundary_in_those_words():
    text = _prose(QUICKSTART)
    assert "Verification can show that the retained export matches the sealed bytes" \
        in text
    assert "cannot prove that promptfoo executed the run as reported, or that " \
           "unpublished runs do not exist" in text


def test_the_quickstart_does_not_count_the_demo_as_adoption():
    text = _prose(QUICKSTART)
    assert "The maintainer's demo is not adoption" in text
    assert "No one outside this repository has used it" in text


def test_the_quickstart_has_a_pilot_checklist_someone_can_follow():
    text = QUICKSTART.read_text(encoding="utf-8")
    assert "A pilot checklist" in text
    assert text.count("- [ ]") >= 8
    assert "your own sanitized export" in text
    # The checklist has to tell someone what counts as a useful outcome, including
    # the outcome where nothing happens.
    assert "What would make this worth continuing" in text


def test_the_quickstart_links_resolve():
    import re

    for target in re.findall(r"\]\(([^)#]+\.(?:md|json))(?:#[^)]*)?\)",
                             QUICKSTART.read_text(encoding="utf-8")):
        assert (QUICKSTART.parent / target).resolve().is_file(), target


def test_the_documented_limits_match_the_adapter():
    text = QUICKSTART.read_text(encoding="utf-8")
    assert f"`results.version: {SUPPORTED_EXPORT_SCHEMAS[0]}`" in text
    assert "0.125.1" in text


def test_every_rendering_of_the_verdict_strip_agrees():
    """The Markdown report and the JUnit XML rebuilt the strip inline.

    Fixing `verdict_sequence` fixed neither - and the Markdown report is the file
    that gets pasted into a pull request, so it was the one still saying `FFF` about
    three repeats nothing had graded.
    """
    from evalseal.report import to_junit, to_markdown, verdict_sequence

    record = import_export(EXPORT)
    errored = next(c for c in record.results
                   if c.case_id == "a case the provider errors on")
    assert verdict_sequence(errored) == "---"

    markdown = to_markdown(record)
    row = next(line for line in markdown.splitlines() if errored.case_id in line)
    assert "`---`" in row and "FFF" not in row

    junit = to_junit(record, fail_on="none")
    errored_case = junit[junit.index(errored.case_id):]
    assert "verdicts ---" in errored_case
