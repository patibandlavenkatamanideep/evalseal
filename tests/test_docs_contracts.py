"""The contract documents quote numbers and names; this keeps them true.

docs/operations.md, docs/statistical-contract.md, docs/compatibility.md and
docs/verification-policy.md state limits, defaults, schema versions and command names.
Prose that has drifted from the code teaches the wrong thing confidently, and these four
pages are the ones a reviewer is told to act on.

Only checkable claims are asserted. The judgement in the prose is not testable and
should not be faked into a test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from evalseal.adapters.recording import FORMAT_VERSION, SUPPORTED_FORMATS
from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import backoff_seconds
from evalseal.analyze import wilson_ci
from evalseal.evidence import case_evidence
from evalseal.ledger import FINGERPRINT_SCHEME, SUPPORTED_SCHEMAS
from evalseal.models import SCHEMA_VERSION, CaseResult
from evalseal.paired import _BOOTSTRAP_ITERS, exact_mcnemar
from evalseal.power import min_discordant_items
from evalseal.signing import PRIVATE_KEY_MODE
from evalseal.transport import (
    ACCEPT_ENCODING,
    MAX_RESPONSE_BYTES,
    MAX_TSA_RESPONSE_BYTES,
    Budget,
)

DOCS = Path(__file__).resolve().parent.parent / "docs"
OPERATIONS = DOCS / "operations.md"
STATISTICAL = DOCS / "statistical-contract.md"
COMPATIBILITY = DOCS / "compatibility.md"
VERIFICATION = DOCS / "verification-policy.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [OPERATIONS, STATISTICAL, COMPATIBILITY, VERIFICATION])
def test_the_document_exists_and_is_not_a_stub(path):
    assert path.is_file(), path
    assert len(_text(path)) > 2000, f"{path.name} is too short to be the page it claims"


# --- docs/operations.md --------------------------------------------------------------

def test_the_documented_response_limits_are_the_code_s():
    text = _text(OPERATIONS)
    assert f"{MAX_RESPONSE_BYTES // 1024 // 1024} MiB" in text
    assert f"{MAX_TSA_RESPONSE_BYTES // 1024 // 1024} MiB" in text
    assert f"`{ACCEPT_ENCODING}`" in text


def test_the_documented_derived_deadline_is_the_derived_deadline():
    derived = Budget().total_deadline(lambda i: backoff_seconds(i))
    assert f"{derived:.0f} s at the defaults" in _text(OPERATIONS)


def test_the_documented_in_flight_bound_matches_the_defaults():
    """concurrency x max_response_bytes, the worst-case transfer memory."""
    bound = 4 * MAX_RESPONSE_BYTES // 1024 // 1024
    assert f"{bound} MiB at the defaults" in _text(OPERATIONS)


def test_the_documented_private_key_mode_is_the_code_s():
    assert f"`{PRIVATE_KEY_MODE:04o}`".replace("0o", "0") in _text(OPERATIONS) or \
        f"`{PRIVATE_KEY_MODE:o}`" in _text(OPERATIONS)


def test_the_largest_committed_cassette_response_is_under_the_quoted_figure():
    """operations.md says "under 40 KB", and calls it 0.5% of the default."""
    import json

    cassettes = Path(__file__).resolve().parent / "cassettes"
    largest = 0
    for path in cassettes.glob("*.json"):
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries", {})
        for entry in entries.values():
            largest = max(largest, len(json.dumps(entry).encode()))
    assert largest < 40 * 1024, largest
    assert "under 40 KB" in _text(OPERATIONS)


def test_every_troubleshooting_symptom_is_a_message_the_code_can_print():
    """Each row quotes a real string, so the table cannot describe a message that moved."""
    text = _text(OPERATIONS)
    from evalseal.ledger import IncompleteFinalRecord
    from evalseal.patterns import UnsafePattern, check_pattern
    from evalseal.transport import ResponseTooLarge, _Clock, read_bounded

    for fragment in (
        "is already being written by another evalseal process",
        "ends with an incomplete record",
        "TAMPER DETECTED at record",
        "carries no hash at all",
        "nests an unbounded quantifier",
        "UNSCORABLE OBSERVATIONS",
        "ran out of time",
    ):
        assert fragment in text, fragment

    # And three of them, produced for real.
    with pytest.raises(UnsafePattern, match="nests an unbounded quantifier"):
        check_pattern("(a+)+")
    with pytest.raises(ResponseTooLarge):
        read_bounded([b"xx"], 1, "x")
    with pytest.raises(Exception, match="ran out of time"):
        _Clock(0.0).check("x")
    assert issubclass(IncompleteFinalRecord, ValueError)


def test_the_recovery_and_rotation_procedures_name_real_flags():
    text = _text(OPERATIONS)
    for flag in ("--overwrite-public", "--max-response-bytes", "--deadline",
                 "--max-retries", "--cassette", "evalseal recover", "--yes"):
        assert flag in text, flag


# --- docs/statistical-contract.md ----------------------------------------------------

def test_the_six_item_floor_is_the_floor():
    text = _text(STATISTICAL)
    assert min_discordant_items(0.05) == 6
    assert exact_mcnemar(6, 0) == pytest.approx(0.03125)
    assert "**six**" in text and "2 × 0.5^n" in text


def test_the_documented_bootstrap_resample_count_is_the_code_s():
    assert f"{_BOOTSTRAP_ITERS:,}".replace(",", " ") in _text(STATISTICAL)


def test_the_quoted_wilson_half_width_at_five_of_five():
    lo, hi = wilson_ci(5, 5)
    assert round((hi - lo) / 2, 3) == 0.217
    assert "0.217" in _text(STATISTICAL)


def test_the_quoted_pass_and_flip_margins_at_three_of_five():
    case = CaseResult(case_id="a", scores=[1.0] * 3 + [0.0] * 2, mean=0.6,
                      ci95=wilson_ci(3, 5), flip_rate=0.4, stability="UNSTABLE",
                      majority_verdict=1, pass_count=3, fail_count=2, flip_count=2)
    evidence = case_evidence(case)
    assert evidence.pass_rate_margin == pytest.approx(0.326, abs=5e-4)
    assert evidence.flip_rate_margin == pytest.approx(0.191, abs=5e-4)
    assert "0.326 against\n0.191" in _text(STATISTICAL) or \
        "0.326 against 0.191" in _text(STATISTICAL)


def test_the_three_budget_states_are_named_by_their_real_values():
    from evalseal.policy import (
        EVIDENCE_OF_REGRESSION,
        EVIDENCE_WITHIN_BUDGET,
        INSUFFICIENT_EVIDENCE,
    )

    text = _text(STATISTICAL)
    for value in (EVIDENCE_OF_REGRESSION, EVIDENCE_WITHIN_BUDGET,
                  INSUFFICIENT_EVIDENCE):
        assert f"`{value}`" in text, value
    assert "require_evidence_within_budget" in text
    assert "fail_on_evidence_of_regression" in text


def test_every_drift_kind_is_in_the_grounds_table():
    from evalseal import drift as drift_module

    text = _text(STATISTICAL)
    kinds = {drift_module.EVALUATOR_DRIFT, drift_module.TARGET_CHANGE,
             drift_module.TARGET_VARIANCE, drift_module.JUDGE_VARIANCE,
             drift_module.CANNOT_ISOLATE, drift_module.STABLE}
    for kind in kinds:
        assert f"`{kind}`" in text, kind


# --- docs/compatibility.md -----------------------------------------------------------

def test_the_documented_versions_are_the_current_ones():
    text = _text(COMPATIBILITY)
    assert f"`{SCHEMA_VERSION}`" in text
    assert f"1.0 … {SUPPORTED_SCHEMAS[-1]}" in text
    assert f"evalseal-fp/{FINGERPRINT_SCHEME}" in text
    assert f"| **cassette format** | `{FORMAT_VERSION}` |" in text
    readable = " and ".join(str(v) for v in SUPPORTED_FORMATS)
    assert readable in text


def test_every_supported_schema_has_a_row_in_the_table():
    text = _text(COMPATIBILITY)
    for version in SUPPORTED_SCHEMAS:
        assert f"| {version} |" in text, version


def test_the_field_counts_in_the_table_match_the_strip_table():
    from evalseal.ledger import _FIELDS_ADDED_IN

    text = _text(COMPATIBILITY)
    for version, paths in _FIELDS_ADDED_IN.items():
        if len(paths) > 1:
            assert f"({len(paths)} paths)" in text, (version, len(paths))


def test_the_runtime_dependencies_quoted_are_the_declared_ones():
    import tomllib

    project = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml")
        .read_text(encoding="utf-8"))["project"]
    text = _text(COMPATIBILITY)
    for spec in project["dependencies"]:
        bare = spec.split("#")[0].strip()
        assert f"`{bare}`" in text, bare


def test_the_migration_notes_name_the_new_commands():
    text = _text(COMPATIBILITY)
    assert "evalseal evidence" in text and "evalseal recover" in text


def test_a_pattern_the_migration_note_promises_still_loads():
    """compatibility.md says fifteen real patterns are tested to still load."""
    for pattern in (r"^yes$", r"(abc)+", r"[+*]+", r"(a+){2}", r"PASS|FAIL"):
        RegexScorer(pattern)


# --- docs/verification-policy.md -----------------------------------------------------

def test_the_assurance_levels_are_all_described():
    text = _text(VERIFICATION)
    for level in ("L0", "L1", "L2", "L3", "L4"):
        assert f"**{level}" in text, level


def test_the_fail_closed_table_names_real_check_rules():
    text = _text(VERIFICATION)
    for rule in ("prereg.require_signature", "prereg.require_anchor",
                 "prereg.require_external_anchor", "drift.max_score_regression",
                 "run.require_independent_repeats"):
        assert rule in text, rule


def test_the_phrases_it_promises_not_to_print_are_not_printed():
    """Each one has a test elsewhere; this asserts the promise names real behaviour."""
    from evalseal.decompose import CaseDecomposition, Decomposition
    from evalseal.decompose import render as render_decomposition
    from evalseal.paired import INCONCLUSIVE, PairedComparison

    comparison = PairedComparison(n_items=5, delta=0.0, ci=(-0.2, 0.2), p_value=1.0,
                                  test="exact_mcnemar", verdict=INCONCLUSIVE)
    assert "no difference" not in comparison.summary().lower()

    decomposition = Decomposition(n_repeats=5, cases=[CaseDecomposition(
        "a", judge_only_verdicts=[1, 0, 0, 1, 1], full_verdicts=[1, 1, 1, 1, 0])])
    rendered = render_decomposition(decomposition)
    # It names apportioning as the thing it does *not* do, and never prints the
    # clipped "100%" that used to stand for a contradiction.
    assert "does not support apportioning variance" in rendered
    assert "no number here is a share of one" in rendered
    assert "100%" not in rendered


def test_the_ci_claim_is_described_as_a_claim():
    text = _text(VERIFICATION)
    assert "GITHUB_ACTIONS=true evalseal run" in text
    assert "CIProvenance.claimed" in text


# --- the documents point at each other ----------------------------------------------

@pytest.mark.parametrize("path", [OPERATIONS, STATISTICAL, COMPATIBILITY, VERIFICATION])
def test_every_relative_link_resolves(path):
    import re

    for target in re.findall(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", _text(path)):
        assert (path.parent / target).resolve().is_file(), f"{path.name} -> {target}"


# --- SECURITY.md ---------------------------------------------------------------------

def test_security_md_does_not_claim_signing_is_unimplemented():
    """It said "There is no signing yet" while `evalseal sign` had shipped."""
    security = (Path(__file__).resolve().parent.parent / "SECURITY.md"
                ).read_text(encoding="utf-8")
    assert "no signing yet" not in security
    assert "Signing the head closes that gap" in security
    # And it says what a signature does not establish, which is the part that matters.
    assert "that the run happened as described" in security


def test_security_md_describes_the_key_handling_the_code_does():
    security = (Path(__file__).resolve().parent.parent / "SECURITY.md"
                ).read_text(encoding="utf-8")
    assert "O_CREAT|O_EXCL|O_NOFOLLOW" in security
    assert f"`{PRIVATE_KEY_MODE:o}`".replace("600", "0600") in security
    assert "--overwrite-public" in security


# --- the delivery documents ----------------------------------------------------------

READINESS = DOCS / "readiness.md"
RELEASE_NOTES = DOCS / "release-notes-2.3.0-draft.md"
REVIEWER = DOCS / "reviewer-checklist.md"


@pytest.mark.parametrize("path", [READINESS, RELEASE_NOTES, REVIEWER])
def test_the_delivery_document_exists_and_links_resolve(path):
    import re

    assert path.is_file(), path
    text = path.read_text(encoding="utf-8")
    assert len(text) > 1500, f"{path.name} is too short to be the page it claims"
    for target in re.findall(r"\]\(([^)#:]+\.md)(?:#[^)]*)?\)", text):
        assert (path.parent / target).resolve().is_file(), f"{path.name} -> {target}"


def test_the_readiness_matrix_covers_every_command():
    """A capability with no row is a capability nobody decided about."""

    from evalseal.cli import app

    text = READINESS.read_text(encoding="utf-8")
    commands = {c.name or c.callback.__name__ for c in app.registered_commands}
    # The ones whose readiness is a judgement rather than a given.
    for name in ("import", "diff", "drift", "power", "evidence", "decompose",
                 "recover", "verify", "anchor", "sign"):
        assert name in commands
        assert name in text, f"readiness.md says nothing about `{name}`"


def test_the_readiness_matrix_does_not_claim_adoption():
    text = READINESS.read_text(encoding="utf-8")
    assert "Nobody outside this repository has used it" in text
    assert "the maintainer's demo is not adoption" in text


def test_the_readiness_matrix_keeps_the_open_items_open():
    """Each is a known gap documented elsewhere; none may quietly disappear."""
    text = READINESS.read_text(encoding="utf-8")
    for gap in ("Overlapping alternation is not", "NFS or SMB",
                "passes on `insufficient_evidence`", "reports `unknown`",
                "TSA signature NOT verified by EvalSeal"):
        assert gap in text, gap


def test_the_release_notes_are_marked_as_a_draft_and_the_version_is_unchanged():
    import tomllib

    text = RELEASE_NOTES.read_text(encoding="utf-8")
    assert "Nothing is tagged, built or published" in text
    project = tomllib.loads(
        (DOCS.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "2.2.0"
    assert "still reports `2.2.0`" in text


def test_the_release_notes_lead_with_the_two_upgrade_consequences():
    text = " ".join(RELEASE_NOTES.read_text(encoding="utf-8").split())
    assert "scores from `answer_match` may change" in text
    assert "evaluator pin on such a suite will fail" in text
    assert "49 are read differently" in text
    assert "No independent adoption" in text


def test_the_reviewer_checklist_is_short_enough_to_finish():
    text = REVIEWER.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 100, "a checklist nobody finishes is unused"
    assert text.count("- [ ]") <= 6
