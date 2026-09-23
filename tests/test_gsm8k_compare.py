"""The pre-registered comparison replays to the numbers published in RESULTS.md.

Both arms are committed cassettes, so this needs no API key and no network. If any
number here moves, examples/gsm8k_compare/RESULTS.md and the README are wrong.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evalseal.diffing import diff_records, mean_flip_rate
from evalseal.ledger import evaluator_fingerprint, load_all

REPO = Path(__file__).resolve().parent.parent
ARMS = ("flash", "gemma")

pytestmark = pytest.mark.skipif(
    not all((REPO / "tests" / "cassettes" / f"gsm8k_compare_{a}.json").exists() for a in ARMS),
    reason="the comparison cassettes are not present",
)


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    """Replay both arms through the CLI, as the RESULTS.md instructions do."""
    ws = tmp_path_factory.mktemp("cmp")
    shutil.copytree(REPO / "examples", ws / "examples")
    (ws / "tests" / "cassettes").mkdir(parents=True)
    for arm in ARMS:
        shutil.copy(REPO / "tests" / "cassettes" / f"gsm8k_compare_{arm}.json",
                    ws / "tests" / "cassettes" / f"gsm8k_compare_{arm}.json")

    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
    env.pop("EVALSEAL_RECORD", None)
    ledger = ws / "cmp.jsonl"
    for arm in ARMS:
        proc = subprocess.run(
            ["evalseal", "run", "--suite", f"examples/gsm8k_compare/suite-{arm}.json",
             "--ledger", str(ledger), "--quiet", "--fail-on", "none"],
            cwd=ws, env=env, capture_output=True, text=True, timeout=600)
        assert proc.returncode == 0, f"{arm} replay failed: {proc.stdout}{proc.stderr}"
    return load_all(ledger)


def test_both_arms_replay_with_no_key(replayed):
    assert len(replayed) == 2
    flash, gemma = replayed
    assert flash.manifest.target.requested_model == "gemini-2.5-flash"
    assert gemma.manifest.target.requested_model == "gemma-4-26b-a4b-it"
    assert flash.aggregate.n_cases == gemma.aggregate.n_cases == 150


def test_the_published_accuracies(replayed):
    flash, gemma = replayed
    assert flash.aggregate.mean_score == pytest.approx(0.9760, abs=5e-5)
    assert gemma.aggregate.mean_score == pytest.approx(0.9613, abs=5e-5)


def test_the_published_stability_difference(replayed):
    """The finding the accuracy test could not reach: gemma flips 9 items to flash's 1."""
    flash, gemma = replayed
    assert flash.aggregate.n_unstable == 1
    assert gemma.aggregate.n_unstable == 9
    assert mean_flip_rate(flash) == pytest.approx(0.0027, abs=5e-5)
    assert mean_flip_rate(gemma) == pytest.approx(0.0200, abs=5e-5)


def test_the_arms_are_comparable(replayed):
    """Same grading, different target: exactly the comparison diff must allow."""
    flash, gemma = replayed
    assert evaluator_fingerprint(flash) == evaluator_fingerprint(gemma)
    assert diff_records(flash, gemma).comparable


def test_the_pre_registered_verdict_is_inconclusive(replayed):
    """Two discordant items, one each way, so the exact McNemar p-value is 1."""
    flash, gemma = replayed
    paired = diff_records(flash, gemma).paired
    assert paired.n_items == 150
    assert paired.delta == pytest.approx(-0.0147, abs=5e-5)
    assert (paired.discordant_regressed, paired.discordant_improved) == (1, 1)
    assert paired.p_value == 1.0
    assert paired.verdict == "inconclusive"


def test_the_analysis_script_runs_and_reports_the_verdict(replayed, tmp_path):
    """RESULTS.md tells a reader to run this script; it has to work."""
    ledger = tmp_path / "cmp.jsonl"
    ledger.write_text(
        "\n".join(r.model_dump_json() for r in replayed) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(REPO / "examples" / "gsm8k_compare" / "analyze.py"), str(ledger)],
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert "verdict:         inconclusive" in proc.stdout
    assert "items whose majority verdict differs (2)" in proc.stdout


def test_results_md_quotes_the_numbers_the_replay_produces(replayed):
    """Guard against the document and the data drifting apart."""
    text = (REPO / "examples" / "gsm8k_compare" / "RESULTS.md").read_text(encoding="utf-8")
    flash, gemma = replayed
    for value in (f"{flash.aggregate.mean_score:.4f}", f"{gemma.aggregate.mean_score:.4f}",
                  "9 of 150", "1 of 150", "exact McNemar p: 1"):
        assert value in text, f"RESULTS.md does not mention {value!r}"
