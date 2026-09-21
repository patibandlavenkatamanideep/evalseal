"""Per-case verdict distribution, and the gate that reads it. No API keys."""
from __future__ import annotations

import json

from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.analyze import analyze_case
from evalseal.cli import EXIT_UNSTABLE, app
from evalseal.executor import run_eval
from evalseal.ledger import seal_and_append
from evalseal.report import case_rows, to_case_table, verdict_sequence

runner = CliRunner()


def _record(**scripts):
    target = LocalCallableTarget(scripted({k: list(v) for k, v in scripts.items()}))
    ds = make_dataset(*scripts)
    return run_eval(ds, target, RegexScorer(r"^yes$"), n_repeats=5)


def test_verdict_counts_for_all_pass_all_fail_and_alternating():
    rec = _record(
        allpass=["yes"],
        allfail=["no"],
        alternating=["yes", "no", "yes", "no", "yes"],
        one_flip=["yes", "yes", "yes", "yes", "no"],
    )
    by_id = {c.case_id: c for c in rec.results}
    cases = {c.case_id: (c.pass_count, c.fail_count, c.flip_count, c.stability_label)
             for c in rec.results}
    ids = {ds_case: cid for cid, ds_case in
           zip([c.case_id for c in rec.results], ["allpass", "allfail", "alternating", "one_flip"],
               strict=True)}

    assert cases[ids["allpass"]] == (5, 0, 0, "stable_pass")
    assert cases[ids["allfail"]] == (0, 5, 0, "stable_fail")
    assert cases[ids["alternating"]] == (3, 2, 2, "unstable")
    assert cases[ids["one_flip"]] == (4, 1, 1, "unstable")

    # the sequence is preserved in run order, not sorted
    assert verdict_sequence(by_id[ids["one_flip"]]) == "PPPPF"
    assert verdict_sequence(by_id[ids["alternating"]]) == "PFPFP"


def test_flip_rate_is_non_majority_over_runs_not_transitions():
    """PPFP has one non-majority verdict in four runs (0.25), not two transitions."""
    stats = analyze_case([1, 1, 0, 1], binary=True)
    assert stats.flip_count == 1
    assert stats.flip_rate == 0.25


def test_three_unanimous_runs_do_not_establish_stability():
    """Both labels now agree that three runs are not enough.

    3/3 gives Wilson [0.44, 1.00], which still contains 0.5. The old coarse rule called
    this STABLE because the flip rate was 0, which over-claimed: a zero flip rate over
    three runs is what you would expect from an item that passes 70% of the time.
    """
    stats = analyze_case([1, 1, 1], binary=True)
    assert stats.flip_rate == 0.0
    assert stats.stability == "UNSTABLE"
    assert stats.stability_label == "insufficient_runs"


def test_case_table_sorts_unstable_first_and_can_filter():
    rec = _record(stable=["yes"], flaky=["yes", "no", "yes", "no", "yes"])
    ordered = [c.case_id for c in case_rows(rec)]
    flaky_id = next(c.case_id for c in rec.results if c.flip_count)
    assert ordered[0] == flaky_id                 # unstable first

    only = case_rows(rec, unstable_only=True)
    assert [c.case_id for c in only] == [flaky_id]
    table = to_case_table(rec, unstable_only=True)
    assert flaky_id in table and "flip_rate" in table


def test_report_json_exposes_distribution_and_provenance(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(_record(flaky=["yes", "no", "yes", "no", "yes"]), ledger, relink=True)

    result = runner.invoke(app, ["report", "--ledger", str(ledger), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["summary"]["n_cases"] == 1
    case = payload["cases"][0]
    assert case["verdict_distribution"] == {"pass": 3, "fail": 2, "other": 0}
    assert case["verdict_sequence"] == "PFPFP"
    assert case["flip_count"] == 2
    assert payload["provenance"]["config_fingerprint"].startswith("sha256:")
    assert payload["provenance"]["evalseal_version"]


def test_gate_thresholds(tmp_path):
    ledger = tmp_path / "l.jsonl"
    seal_and_append(
        _record(stable=["yes"], flaky=["yes", "no", "yes", "no", "yes"]), ledger, relink=True
    )
    base = ["gate", "--ledger", str(ledger)]

    assert runner.invoke(app, base).exit_code == 0            # no thresholds: passes

    over = runner.invoke(app, [*base, "--max-flip-rate", "0.10"])
    assert over.exit_code == EXIT_UNSTABLE
    assert "max-flip-rate" in over.output

    under = runner.invoke(app, [*base, "--max-flip-rate", "0.50"])
    assert under.exit_code == 0, under.output

    low = runner.invoke(app, [*base, "--min-score", "0.99"])
    assert low.exit_code == EXIT_UNSTABLE and "min-score" in low.output


def test_gate_critical_case_must_not_flip(tmp_path):
    ledger = tmp_path / "l.jsonl"
    rec = _record(stable=["yes"], flaky=["yes", "no", "yes", "no", "yes"])
    seal_and_append(rec, ledger, relink=True)
    flaky_id = next(c.case_id for c in rec.results if c.flip_count)
    stable_id = next(c.case_id for c in rec.results if not c.flip_count)

    bad = runner.invoke(app, ["gate", "--ledger", str(ledger), "--critical", flaky_id])
    assert bad.exit_code == EXIT_UNSTABLE and "flipped" in bad.output

    good = runner.invoke(app, ["gate", "--ledger", str(ledger), "--critical", stable_id])
    assert good.exit_code == 0, good.output

    missing = runner.invoke(app, ["gate", "--ledger", str(ledger), "--critical", "nope"])
    assert missing.exit_code == EXIT_UNSTABLE and "not in this record" in missing.output
