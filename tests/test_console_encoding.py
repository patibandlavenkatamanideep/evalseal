"""The CLI must survive a stdout that cannot encode what the report prints.

The report contains "⚠" and "·". When stdout is redirected, Python falls back to the
locale encoding, which on Windows is cp1252 and cannot carry either, so `evalseal run >
out.txt` died with UnicodeEncodeError while the same command in a terminal worked. The
repository's own CI could not catch it: every CLI step is gated `runner.os != 'Windows'`.

PYTHONIOENCODING reproduces that condition on any platform, so this runs everywhere.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CASSETTES = ("gsm8k.json", "gsm8k_lite.json")

pytestmark = pytest.mark.skipif(
    not all((REPO / "tests" / "cassettes" / c).exists() for c in CASSETTES),
    reason="gsm8k cassettes not recorded")


@pytest.fixture
def workspace(tmp_path):
    shutil.copytree(REPO / "examples", tmp_path / "examples")
    (tmp_path / "tests" / "cassettes").mkdir(parents=True)
    for name in CASSETTES:
        shutil.copy(REPO / "tests" / "cassettes" / name, tmp_path / "tests" / "cassettes" / name)
    return tmp_path


def _run(workspace: Path, *args: str, encoding: str) -> subprocess.CompletedProcess:
    env = {**os.environ,
           "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
           "PYTHONIOENCODING": encoding}
    env.pop("EVALSEAL_RECORD", None)
    return subprocess.run(
        ["evalseal", *args],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("encoding", ["cp1252", "ascii", "utf-8"])
def test_run_survives_a_narrow_stdout_encoding(workspace, encoding):
    """The report's warning block is what breaks: gsm8k warns TEMPERATURE NOT SET."""
    proc = _run(workspace, "run", "--suite", "examples/gsm8k/suite.json",
                "--ledger", "l.jsonl", "--fail-on", "none", encoding=encoding)
    assert "UnicodeEncodeError" not in proc.stdout + proc.stderr
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Provenance warnings" in proc.stdout


@pytest.mark.parametrize("encoding", ["cp1252", "utf-8"])
def test_report_and_diff_survive_it_too(workspace, encoding):
    """Both print tables full of box-drawing characters."""
    assert _run(workspace, "run", "--suite", "examples/gsm8k/suite.json", "--ledger",
                "l.jsonl", "--fail-on", "none", encoding="utf-8").returncode == 0
    assert _run(workspace, "run", "--suite", "examples/gsm8k/suite-lite.json", "--ledger",
                "l.jsonl", "--fail-on", "none", encoding="utf-8").returncode == 0

    for args in (("report", "--ledger", "l.jsonl", "--unstable-only"),
                 ("diff", "0", "1", "--ledger", "l.jsonl")):
        proc = _run(workspace, *args, encoding=encoding)
        assert "UnicodeEncodeError" not in proc.stdout + proc.stderr
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_json_output_stays_parseable_under_a_narrow_encoding(workspace):
    import json

    assert _run(workspace, "run", "--suite", "examples/gsm8k/suite.json", "--ledger",
                "l.jsonl", "--fail-on", "none", encoding="utf-8").returncode == 0
    proc = _run(workspace, "report", "--ledger", "l.jsonl", "--json", encoding="cp1252")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["summary"]["n_cases"] == 40
