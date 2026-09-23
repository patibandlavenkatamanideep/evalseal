"""Execute docs/pr-receipt-workflow.yml, step by step, against a copy of the repo.

A workflow in the docs that nobody runs rots quietly. This parses the real file, runs
each `run:` step in order with the environment GitHub Actions would provide, evaluates
each step's `if:` the way Actions does, and asserts on the step summary it writes.

Two things differ from a real runner, both deliberately: the install step is skipped in
favour of the EvalSeal under test, and `uses:` steps (checkout, setup-python, artifact
upload) are skipped because they have no local equivalent.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / "docs" / "pr-receipt-workflow.yml"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the workflow's run steps are bash; runners are Linux")


def _workspace(tmp_path: Path) -> Path:
    """The files the workflow reads, laid out as in a checkout."""
    ws = tmp_path / "ws"
    shutil.copytree(REPO / "examples", ws / "examples")
    (ws / "tests" / "cassettes").mkdir(parents=True)
    for name in ("gsm8k.json", "gsm8k_lite.json", "run.json"):
        shutil.copy(REPO / "tests" / "cassettes" / name, ws / "tests" / "cassettes" / name)
    return ws


def _env_with_evalseal() -> dict[str, str]:
    """The environment with the EvalSeal under test first on PATH."""
    return {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}


def _condition(expr: str | None, ws: Path, env: dict[str, str], outputs: dict) -> bool:
    """The three `if:` forms the workflow uses, evaluated as Actions would."""
    if expr is None:
        return not outputs["_failed"]
    expr = expr.strip()
    if expr == "always()":
        return True
    if expr.startswith("hashFiles(env.BASELINE_RECEIPT)"):
        return not outputs["_failed"] and (ws / env["BASELINE_RECEIPT"]).is_file()
    if expr.startswith("steps.gate.outputs.code"):
        return not outputs["_failed"] and outputs.get("gate", {}).get("code") != "0"
    raise AssertionError(f"the workflow uses an `if:` this harness does not model: {expr}")


def run_workflow(ws: Path, **env_overrides: str) -> dict:
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = {**os.environ, **{k: str(v) for k, v in wf["env"].items()}, **env_overrides}
    # The EvalSeal under test, not whatever is on PyPI.
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"
    env.pop("EVALSEAL_RECORD", None)
    env.pop("EVALSEAL_API_KEY", None)
    summary = ws / "summary.md"
    summary.write_text("", encoding="utf-8")
    env["GITHUB_STEP_SUMMARY"] = str(summary)

    outputs: dict = {"_failed": False}
    ran: list[str] = []
    for step in wf["jobs"]["receipt"]["steps"]:
        name = step.get("name") or step.get("uses", "")
        if "uses" in step or name == "Install EvalSeal":
            continue
        if not _condition(step.get("if"), ws, env, outputs):
            continue
        step_env = {**env, **{k: str(v) for k, v in step.get("env", {}).items()}}
        if "GATE_CODE" in step_env and "${{" in step_env["GATE_CODE"]:
            step_env["GATE_CODE"] = outputs.get("gate", {}).get("code", "")
        out_file = ws / f".out-{len(ran)}"
        out_file.write_text("", encoding="utf-8")
        step_env["GITHUB_OUTPUT"] = str(out_file)

        if step.get("shell") == "python":
            cmd = [sys.executable, "-c", step["run"]]
        else:
            cmd = ["bash", "-eo", "pipefail", "-c", step["run"]]
        proc = subprocess.run(cmd, cwd=ws, env=step_env, capture_output=True, text=True,
                              timeout=180)
        ran.append(name)
        if "id" in step:
            outputs[step["id"]] = dict(
                line.split("=", 1) for line in out_file.read_text().splitlines() if "=" in line)
        if proc.returncode != 0:
            outputs["_failed"] = True
            outputs["_failed_step"] = name
            outputs["_stderr"] = proc.stderr + proc.stdout

    return {"summary": summary.read_text(encoding="utf-8"), "ran": ran,
            "failed": outputs["_failed"], "failed_step": outputs.get("_failed_step"),
            "log": outputs.get("_stderr", ""), "ws": ws}


def _baseline(ws: Path, suite: str) -> None:
    """A receipt from a different run, placed where the workflow looks for a baseline."""
    ledger = ws / ".evalseal" / "baseline.jsonl"
    subprocess.run(
        ["evalseal", "run", "--suite", suite, "--ledger", str(ledger),
         "--fail-on", "none", "--quiet"],
        cwd=ws, check=True, capture_output=True,
        env=_env_with_evalseal(),
    )
    (ws / "evals").mkdir(exist_ok=True)
    shutil.move(ws / "report.json", ws / "evals" / "baseline-receipt.json")


def test_the_default_workflow_passes_with_no_api_key(tmp_path):
    result = run_workflow(_workspace(tmp_path))
    assert not result["failed"], (result["failed_step"], result["log"])

    s = result["summary"]
    assert "**Gate PASSED**" in s
    assert "No baseline receipt, so no drift comparison ran." in s
    assert "| `run.min_score` | ok |" in s                 # passing checks are listed
    assert "mean score | 0.975 over 40 cases" in s
    assert "EvalSeal version (sealed)" in s
    assert "unstable cases | 0" in s
    for artifact in ("receipt.html", "report.json", "gate.json", "anchor.json"):
        assert artifact in s
        assert (result["ws"] / artifact).exists()
    assert "Compare with the baseline receipt" not in result["ran"]


def test_the_written_anchor_verifies(tmp_path):
    result = run_workflow(_workspace(tmp_path))
    ws = result["ws"]
    proc = subprocess.run(
        ["evalseal", "anchor-verify", "anchor.json", "report.json",
         "--ledger", ".evalseal/pr.jsonl"],
        cwd=ws, capture_output=True, text=True,
        env=_env_with_evalseal(),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_comparable_baseline_is_reported_before_the_numbers(tmp_path):
    """Same grading, different target model: comparable, and the delta is paired."""
    ws = _workspace(tmp_path)
    _baseline(ws, "examples/gsm8k/suite-lite.json")
    result = run_workflow(ws)
    assert not result["failed"], (result["failed_step"], result["log"])

    s = result["summary"]
    assert "**Comparable with the baseline:** yes" in s
    assert "| 0.975 | 0.975 | +0.0000 |" in s
    assert "**inconclusive**" in s
    assert "not that the runs are the same" in s
    assert "drift.html" in s
    assert s.index("Comparable with the baseline") < s.index("mean score")


def test_a_non_comparable_baseline_is_not_hidden_behind_its_delta(tmp_path):
    """The judge-graded suite as baseline: different grader, different dataset."""
    ws = _workspace(tmp_path)
    _baseline(ws, "examples/borderline_judge/suite.json")
    result = run_workflow(ws)

    s = result["summary"]
    assert "**NOT COMPARABLE with the baseline.**" in s
    assert "must not be read as one" in s
    assert "(not comparable)" in s
    reasons = s.split("grading setup changed (", 1)[1].split(")", 1)[0]
    assert "scorer type" in reasons and "dataset" in reasons
    assert "target model" not in reasons       # not a grading change
    assert s.index("NOT COMPARABLE") < s.index("| check | result |")


def test_a_failing_gate_fails_the_job_but_still_writes_the_summary(tmp_path):
    ws = _workspace(tmp_path)
    strict = ws / "strict.yml"
    strict.write_text("version: 1\nrun:\n  min_score: 0.999\n", encoding="utf-8")
    result = run_workflow(ws, POLICY="strict.yml")

    assert result["failed"]
    assert result["failed_step"] == "Fail the check if the gate failed"
    s = result["summary"]
    assert "**Gate FAILED**" in s
    assert "| `run.min_score` | **FAIL** |" in s
    assert "What to inspect" in s


def test_the_workflow_is_read_only_and_pinned():
    """Supply-chain hygiene a copied workflow should keep."""
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = wf.get("on", wf.get(True))
    assert "pull_request_target" not in triggers
    assert wf["permissions"] == {"contents": "read"}
    for step in wf["jobs"]["receipt"]["steps"]:
        if "uses" in step:
            ref = step["uses"].split("@", 1)[1]
            assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), step["uses"]
