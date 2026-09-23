"""A pre-registration is a contract; these are the ways a receipt can break it.

The claim under test is narrow and has to stay narrow: a passing gate says *this
receipt matches the evaluation that was declared*, not *nobody ran this privately
first*. `test_docs_do_not_overclaim` guards the wording, because the value of the
feature is entirely in not being oversold.

Every clause gets a test for the failing side as well as the passing one. A contract
clause that cannot fail is decoration.
"""
from __future__ import annotations

import json

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.dataset import Dataset
from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import EXIT_BAD_INPUT, EXIT_UNSTABLE, app
from evalseal.executor import run_eval
from evalseal.ledger import _hash_ids, evaluator_fingerprint, seal_and_append
from evalseal.models import CIProvenance
from evalseal.prereg import (
    AnchorState,
    PreregError,
    Preregistration,
    build_preregistration,
    check_preregistration,
    load_preregistration,
)
from evalseal.report import write_json

runner = CliRunner()


def _record(n_repeats: int = 5, *, answer: str = "yes", cases=("a", "b")):
    target = LocalCallableTarget(scripted({c: [answer] for c in cases}))
    return run_eval(make_dataset(*cases), target, RegexScorer(r"^yes$"),
                    n_repeats=n_repeats)


def _contract(**kwargs) -> Preregistration:
    """A contract declaring the two-case suite that `_record` produces."""
    defaults = {"n_repeats": 5, "case_ids": ["c0", "c1"]}
    return build_preregistration(**{**defaults, **kwargs})


def _by_rule(checks):
    return {c.rule: c for c in checks}


def _failed(checks):
    return sorted(c.rule for c in checks if not c.passed)


# --- the passing case ---------------------------------------------------------------

def test_a_receipt_that_honours_the_contract_passes_every_clause():
    checks = check_preregistration(_contract(), _record())
    assert checks, "a contract with clauses must produce checks"
    assert _failed(checks) == []


def test_every_declared_clause_produces_a_check_even_when_it_passes():
    """A gate that reports only failures cannot be told from one that checked nothing."""
    record = _record()
    checks = _by_rule(check_preregistration(
        _contract(evaluator_fingerprint_pin=evaluator_fingerprint(record)), record))
    assert "prereg.case_set" in checks
    assert "prereg.n_repeats" in checks
    assert "prereg.evaluator_fingerprint" in checks
    assert all(c.passed for c in checks.values())


# --- run count ----------------------------------------------------------------------

def test_a_receipt_with_fewer_runs_than_declared_fails():
    checks = _by_rule(check_preregistration(_contract(n_repeats=5), _record(n_repeats=3)))
    assert not checks["prereg.n_repeats"].passed
    assert "ran 3 repeat(s), not the declared 5" in checks["prereg.n_repeats"].detail


def test_a_case_short_of_scores_fails_even_when_the_config_says_otherwise():
    """The config is what was asked for; the per-case counts are what was delivered."""
    record = _record(n_repeats=5)
    record.results[0].scores = record.results[0].scores[:2]      # an interrupted case
    checks = _by_rule(check_preregistration(_contract(n_repeats=5), record))
    assert checks["prereg.n_repeats"].passed                     # config still says 5
    assert not checks["prereg.min_repeats_per_case"].passed
    assert "c0 (2)" in checks["prereg.min_repeats_per_case"].detail


def test_min_repeats_per_case_can_be_declared_lower_than_n_repeats():
    record = _record(n_repeats=5)
    record.results[0].scores = record.results[0].scores[:3]
    checks = _by_rule(check_preregistration(
        _contract(n_repeats=5, min_repeats_per_case=3), record))
    assert checks["prereg.min_repeats_per_case"].passed


# --- the case set, which is the anti-cherry-picking clause ---------------------------

def test_dropping_a_case_after_declaring_it_fails():
    """The clause that matters: quietly removing the items that failed."""
    checks = _by_rule(check_preregistration(
        _contract(case_ids=["c0", "c1", "c2"]), _record(cases=("a", "b"))))
    assert not checks["prereg.case_set"].passed
    assert "not the declared one" in checks["prereg.case_set"].detail
    assert "changes this hash" in checks["prereg.case_set"].detail


def test_adding_a_case_after_declaring_the_set_also_fails():
    checks = _by_rule(check_preregistration(
        _contract(case_ids=["c0"]), _record(cases=("a", "b"))))
    assert not checks["prereg.case_set"].passed


def test_the_declared_case_hash_matches_the_one_the_receipt_computes():
    """Two definitions of this hash would disagree, and read as a dropped case."""
    record = _record()
    declared = _hash_ids([c.case_id for c in record.results])
    assert _contract(case_ids=["c1", "c0"]).dataset.case_set_hash == declared


# --- the grading setup ---------------------------------------------------------------

def test_a_changed_evaluator_fingerprint_fails():
    pinned = evaluator_fingerprint(_record())
    other = run_eval(make_dataset("a", "b"),
                     LocalCallableTarget(scripted({"a": ["yes"], "b": ["yes"]})),
                     RegexScorer(r"^no$"),            # the opposite grader
                     n_repeats=5)
    checks = _by_rule(check_preregistration(
        _contract(evaluator_fingerprint_pin=pinned), other))
    assert not checks["prereg.evaluator_fingerprint"].passed


def test_a_pin_from_an_older_scheme_explains_itself_instead_of_reporting_a_mismatch():
    checks = _by_rule(check_preregistration(
        _contract(evaluator_fingerprint_pin="sha256:deadbeef"), _record()))
    detail = checks["prereg.evaluator_fingerprint"].detail
    assert "unversioned scheme" in detail and "Re-pin" in detail


def test_the_target_model_may_change_without_breaking_the_contract():
    """The point of the evaluator fingerprint: swapping the model under test is the
    reason to run a benchmark, so it must not break a pre-registration."""
    record = _record()
    pinned = evaluator_fingerprint(record)
    record.manifest.target.requested_model = "some-other-model"
    checks = _by_rule(check_preregistration(
        _contract(evaluator_fingerprint_pin=pinned), record))
    assert checks["prereg.evaluator_fingerprint"].passed


# --- required artifacts ---------------------------------------------------------------

def test_a_missing_required_artifact_fails():
    checks = _by_rule(check_preregistration(
        _contract(required_artifacts=["cassette"]), _record()))
    assert not checks["prereg.artifact[cassette]"].passed
    assert "not sealed into this receipt at all" in checks["prereg.artifact[cassette]"].detail


def test_an_artifact_sealed_as_external_fails_unless_allowed(tmp_path):
    """`external` means the file was named and unreadable: a digest-shaped hole."""
    record = run_eval(
        make_dataset("a", "b"),
        LocalCallableTarget(scripted({"a": ["yes"], "b": ["yes"]})),
        RegexScorer(r"^yes$"), n_repeats=5,
        artifact_paths={"cassette": str(tmp_path / "gone.json")})
    assert record.manifest.artifacts[0].kind == "external"

    strict = _by_rule(check_preregistration(
        _contract(required_artifacts=["cassette"]), record))
    assert not strict["prereg.artifact[cassette]"].passed
    assert "allow_external_artifacts" in strict["prereg.artifact[cassette]"].detail

    contract = _contract(required_artifacts=["cassette"])
    contract.allow_external_artifacts = ["cassette"]
    allowed = _by_rule(check_preregistration(contract, record))
    assert allowed["prereg.artifact[cassette]"].passed
    assert "nothing here can be re-checked" in allowed["prereg.artifact[cassette]"].detail


def test_a_sealed_artifact_passes_and_reports_its_digest(tmp_path):
    cassette = tmp_path / "cassette.json"
    cassette.write_text("{}", encoding="utf-8")
    record = run_eval(
        make_dataset("a", "b"),
        LocalCallableTarget(scripted({"a": ["yes"], "b": ["yes"]})),
        RegexScorer(r"^yes$"), n_repeats=5,
        artifact_paths={"cassette": str(cassette)})
    checks = _by_rule(check_preregistration(
        _contract(required_artifacts=["cassette"]), record))
    assert checks["prereg.artifact[cassette]"].passed
    assert "sha256:" in checks["prereg.artifact[cassette]"].detail


# --- CI, and what it is honestly worth -------------------------------------------------

def test_a_receipt_produced_outside_ci_fails_only_when_ci_is_required():
    record = _record()
    assert record.manifest.environment.ci is None

    assert _failed(check_preregistration(_contract(require_ci=False), record)) == []

    checks = _by_rule(check_preregistration(_contract(require_ci=True), record))
    assert not checks["prereg.require_ci"].passed
    assert "outside CI" in checks["prereg.require_ci"].detail


def test_a_ci_receipt_passes_and_the_check_says_it_is_only_a_claim():
    record = _record()
    record.manifest.environment.ci = CIProvenance(
        claimed=True, provider="github_actions",
        run_url="https://github.com/o/r/actions/runs/42")
    checks = _by_rule(check_preregistration(_contract(require_ci=True), record))
    assert checks["prereg.require_ci"].passed
    detail = checks["prereg.require_ci"].detail
    assert "environment's own claim" in detail        # never "proves it ran in CI"
    assert "run URL" in detail


def test_ci_provenance_reads_only_the_allowlist(monkeypatch):
    """A token in the environment must not be able to reach a sealed record."""
    from evalseal.provenance import ci_provenance

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "7")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")

    ci = ci_provenance()
    assert ci is not None
    blob = json.dumps(ci)
    assert "ghp_secret_value" not in blob and "sk-ant-secret" not in blob
    assert ci["repository"] == "owner/repo"
    assert ci["run_url"] == "https://github.com/owner/repo/actions/runs/7"


def test_no_ci_markers_means_none_rather_than_a_guess(monkeypatch):
    from evalseal.provenance import ci_provenance

    for var in ("GITHUB_ACTIONS", "GITLAB_CI", "CIRCLECI", "BUILDKITE", "CI"):
        monkeypatch.delenv(var, raising=False)
    assert ci_provenance() is None


# --- anchors ---------------------------------------------------------------------------

def test_requiring_an_anchor_fails_when_none_was_supplied():
    checks = _by_rule(check_preregistration(_contract(require_anchor=True), _record()))
    assert not checks["prereg.require_anchor"].passed
    assert "--anchor" in checks["prereg.require_anchor"].detail


def test_requiring_an_external_proof_fails_on_a_local_anchor():
    checks = _by_rule(check_preregistration(
        _contract(require_anchor=True, require_external_anchor=True), _record(),
        anchor=AnchorState(present=True, verified=True, has_external_proof=False)))
    assert checks["prereg.require_anchor"].passed
    assert not checks["prereg.require_external_anchor"].passed
    assert "this machine's clock" in checks["prereg.require_external_anchor"].detail


# --- the file itself ---------------------------------------------------------------------

def test_a_pre_registration_round_trips(tmp_path):
    contract = _contract(required_artifacts=["cassette"], require_ci=True)
    path = tmp_path / "prereg.json"
    path.write_text(contract.to_json(), encoding="utf-8")
    assert load_preregistration(path) == contract


def test_an_unknown_key_is_refused_rather_than_ignored(tmp_path):
    """Same rule as policy.py: a typo must not read as "no opinion"."""
    path = tmp_path / "prereg.json"
    data = json.loads(_contract().to_json())
    data["require_c1"] = True                      # a plausible typo for require_ci
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PreregError) as e:
        load_preregistration(path)
    assert "not a valid pre-registration" in str(e.value)


def test_a_broken_pre_registration_file_is_named(tmp_path):
    path = tmp_path / "prereg.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(PreregError) as e:
        load_preregistration(path)
    assert "not valid JSON" in str(e.value)


# --- the CLI ------------------------------------------------------------------------------

@pytest.fixture
def declared(tmp_path):
    """A dataset, a sealed run of it, and a contract declaring that run."""
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        "\n".join(json.dumps({"case_id": f"c{i}", "prompt": p, "expected": "yes"})
                  for i, p in enumerate(("a", "b"))), encoding="utf-8")
    # Run against the real file, so the receipt's dataset hash is the one the
    # contract pins. A fixture that mixed a stub hash with a real file would fail
    # the dataset clause for a reason unrelated to what each test is checking.
    loaded = Dataset.from_jsonl(dataset)
    record = run_eval(loaded, LocalCallableTarget(scripted({"a": ["yes"], "b": ["yes"]})),
                      RegexScorer(r"^yes$"), n_repeats=5, dataset_path=str(dataset))
    ledger = tmp_path / "ledger.jsonl"
    record = seal_and_append(record, ledger, relink=True)
    receipt = tmp_path / "report.json"
    write_json(record, receipt)
    return dataset, ledger, receipt, record


def test_preregister_writes_a_contract_and_gate_checks_it(tmp_path, declared):
    dataset, ledger, receipt, _ = declared
    prereg = tmp_path / "prereg.json"

    created = runner.invoke(app, [
        "preregister", "--dataset", str(dataset), "--n-repeats", "5",
        "--pin-from", str(receipt), "--out", str(prereg)])
    assert created.exit_code == 0
    assert "cannot prove no private run happened" in " ".join(created.output.split())

    gated = runner.invoke(app, ["gate", "--ledger", str(ledger), "--prereg", str(prereg)])
    assert gated.exit_code == 0, gated.output
    assert "prereg.case_set" in gated.output


def test_gate_fails_when_the_contract_is_broken(tmp_path, declared):
    dataset, ledger, receipt, _ = declared
    prereg = tmp_path / "prereg.json"
    runner.invoke(app, ["preregister", "--dataset", str(dataset),
                        "--n-repeats", "9", "--out", str(prereg)])

    gated = runner.invoke(app, ["gate", "--ledger", str(ledger), "--prereg", str(prereg)])
    assert gated.exit_code == EXIT_UNSTABLE
    assert "prereg.n_repeats" in gated.output


def test_gate_refuses_a_broken_contract_rather_than_ignoring_it(tmp_path, declared):
    _, ledger, _, _ = declared
    prereg = tmp_path / "prereg.json"
    prereg.write_text("{not json", encoding="utf-8")
    gated = runner.invoke(app, ["gate", "--ledger", str(ledger), "--prereg", str(prereg)])
    assert gated.exit_code == EXIT_BAD_INPUT
    assert "Pre-registration error" in gated.output


def test_gate_json_carries_the_prereg_checks(tmp_path, declared):
    dataset, ledger, _, _ = declared
    prereg = tmp_path / "prereg.json"
    runner.invoke(app, ["preregister", "--dataset", str(dataset), "--out", str(prereg)])

    gated = runner.invoke(app, ["gate", "--ledger", str(ledger),
                                "--prereg", str(prereg), "--json"])
    payload = json.loads(gated.stdout)
    rules = {c["rule"] for c in payload["checks"]}
    assert {"prereg.case_set", "prereg.n_repeats"} <= rules


def test_an_embedded_policy_is_applied_too(tmp_path, declared):
    """Thresholds fixed in the same file, before the number exists."""
    dataset, ledger, _, _ = declared
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"version": 1, "run": {"min_score": 0.99}}), "utf-8")
    prereg = tmp_path / "prereg.json"
    runner.invoke(app, ["preregister", "--dataset", str(dataset),
                        "--policy", str(policy), "--out", str(prereg)])

    assert load_preregistration(prereg).policy is not None
    gated = runner.invoke(app, ["gate", "--ledger", str(ledger), "--prereg", str(prereg)])
    assert gated.exit_code == 0        # the scripted run scores 1.0


# --- the claim itself ---------------------------------------------------------------------

def test_docs_do_not_overclaim():
    """The feature is only worth having if it is not oversold."""
    from pathlib import Path as P

    import evalseal.prereg as module

    # conftest chdirs into a tmp dir, so resolve the doc from this file.
    doc = P(__file__).resolve().parent.parent / "docs" / "pre-registration.md"
    text = (module.__doc__ or "") + doc.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "cannot prove" in lowered
    for forbidden in ("proves no cherry-picking", "prevents cherry-picking",
                      "guarantees no hidden runs", "proves nobody"):
        assert forbidden not in lowered
