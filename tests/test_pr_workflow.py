"""Execute docs/pr-receipt-workflow.yml, step by step, against a copy of the repo.

A workflow in the docs that nobody runs rots quietly. This parses the real file, runs
each `run:` step in order with the environment GitHub Actions would provide, evaluates
each step's `if:` the way Actions does, and asserts on the step summary it writes.

Two things differ from a real runner, both deliberately: the install step is skipped in
favour of the EvalSeal under test, and `uses:` steps (checkout, setup-python, artifact
upload) are skipped because they have no local equivalent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / "docs" / "pr-receipt-workflow.yml"
#: `${{ steps.gate.outputs.code }}` and friends.
_STEP_OUTPUT = re.compile(
    r"\$\{\{\s*steps\.(?P<step>[\w-]+)\.outputs\.(?P<name>[\w-]+)\s*\}\}")

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


def run_workflow(ws: Path, _workflow: Path | None = None, **env_overrides: str) -> dict:
    """Execute the workflow against `ws`. `_workflow` runs a patched copy of it.

    A patched copy is how a fault is injected into one step without pretending the
    rest of the file is different: everything else is the committed workflow.
    """
    wf = yaml.safe_load((_workflow or WORKFLOW).read_text(encoding="utf-8"))
    env = {**os.environ, **{k: str(v) for k, v in wf["env"].items()}, **env_overrides}
    # A GitHub runner is not an 80-column terminal, and rich wraps an error message to
    # whatever it is told. Narrow output made a test assert on half a sentence.
    env["COLUMNS"] = "200"
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
        # Resolve every `${{ steps.<id>.outputs.<name> }}` the way a runner does, not
        # just GATE_CODE by name. One step's expression being substituted while
        # another's was passed through literally is a harness that tests the step it
        # happens to know about.
        for key, value in list(step_env.items()):
            match = _STEP_OUTPUT.fullmatch(value.strip())
            if match:
                step_env[key] = outputs.get(match["step"], {}).get(match["name"], "")
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
            # The *first* failure is the cause; a runner marks the job with that one and
            # only `if: always()` steps run afterwards. Overwriting it meant a test
            # asking which check failed was told about the final catch-all step.
            outputs.setdefault("_failed_step", name)
            outputs["_stderr"] = outputs.get("_stderr", "") + proc.stderr + proc.stdout

    return {"summary": summary.read_text(encoding="utf-8"), "ran": ran,
            "failed": outputs["_failed"], "failed_step": outputs.get("_failed_step"),
            "log": outputs.get("_stderr", ""), "ws": ws}


def _baseline(ws: Path, suite: str) -> None:
    """A receipt from a different run, placed where the workflow looks for a baseline."""
    ledger = ws / ".evalseal" / "baseline.jsonl"
    proc = subprocess.run(
        ["evalseal", "run", "--suite", suite, "--ledger", str(ledger),
         "--fail-on", "none", "--quiet"],
        cwd=ws, check=False, capture_output=True, text=True,
        env=_env_with_evalseal(),
    )
    # Not check=True: that raises CalledProcessError, whose repr says only "returned
    # non-zero exit status 1" and throws away everything the command printed. This
    # failed once during a full run and left nothing to diagnose it with.
    assert proc.returncode == 0, (
        f"building the baseline with `evalseal run` failed ({proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
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
    assert "# NOT COMPARABLE with the baseline" in s
    assert "must not be read as one" in s
    assert "(not comparable)" in s
    reasons = s.split("# NOT COMPARABLE with the baseline (", 1)[1].split(")", 1)[0]
    assert "scorer type" in reasons and "dataset" in reasons
    assert "target model" not in reasons       # not a grading change
    assert s.index("NOT COMPARABLE") < s.index("| check | result |")
    # Comparability leads: it comes before the gate verdict and before any number.
    assert s.index("NOT COMPARABLE") < s.index("Gate ")
    assert s.index("NOT COMPARABLE") < s.index("mean score")


def test_comparability_is_the_headline_above_the_gate_result(tmp_path):
    """Phase F's ordering: a reviewer who reads one line reads comparability."""
    ws = _workspace(tmp_path)
    _baseline(ws, "examples/gsm8k/suite.json")
    result = run_workflow(ws)
    assert not result["failed"], (result["failed_step"], result["log"])

    s = result["summary"]
    assert "**Comparable with the baseline:** yes" in s
    assert s.index("Comparable with the baseline") < s.index("Gate ")
    assert s.index("Comparable with the baseline") < s.index("mean score")


def test_without_a_baseline_the_headline_says_nothing_was_compared(tmp_path):
    result = run_workflow(_workspace(tmp_path))
    s = result["summary"]
    assert "no baseline receipt, so nothing was compared" in s
    assert s.index("Comparability:") < s.index("Gate ")


def test_a_failing_gate_fails_the_job_but_still_writes_the_summary(tmp_path):
    ws = _workspace(tmp_path)
    strict = ws / "strict.yml"
    strict.write_text("version: 1\nrun:\n  min_score: 0.999\n", encoding="utf-8")
    result = run_workflow(ws, POLICY="strict.yml")

    assert result["failed"]
    assert result["failed_step"] == "Fail the check if any required check failed"
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


def test_the_pre_registration_is_applied_and_named(tmp_path):
    """A prereg file in the workspace must actually gate, not sit there unread."""
    result = run_workflow(_workspace(tmp_path))
    assert not result["failed"], (result["failed_step"], result["log"])

    s = result["summary"]
    assert "the pre-registration" in s
    for rule in ("prereg.case_set", "prereg.n_repeats", "prereg.evaluator_fingerprint",
                 "prereg.artifact[cassette]"):
        assert f"| `{rule}` | ok |" in s, rule
    # And the honest caveat is not printed when a prereg *was* applied.
    assert "nothing checked that this is the evaluation that was declared" not in s


def test_without_a_pre_registration_the_summary_says_so(tmp_path):
    """Opting out deliberately, which is the only way to run without one now."""
    ws = _workspace(tmp_path)
    (ws / "examples" / "prereg.json").unlink()
    result = run_workflow(ws, REQUIRE_PREREG="false")
    assert not result["failed"], (result["failed_step"], result["log"])

    s = result["summary"]
    assert "prereg." not in s
    assert "No pre-registration was applied" in s


def test_a_broken_pre_registration_fails_the_job(tmp_path):
    """A contract the receipt does not honour must fail, not be skipped."""
    ws = _workspace(tmp_path)
    prereg = ws / "examples" / "prereg.json"
    contract = json.loads(prereg.read_text(encoding="utf-8"))
    contract["runs"]["n_repeats"] = 9            # nobody ran nine repeats
    prereg.write_text(json.dumps(contract), encoding="utf-8")

    result = run_workflow(ws)
    assert result["failed"]
    assert "**Gate FAILED**" in result["summary"]
    assert "prereg.n_repeats" in result["summary"]


def test_artifact_and_anchor_verification_reach_the_summary(tmp_path):
    result = run_workflow(_workspace(tmp_path))
    s = result["summary"]
    assert "Artifact and anchor verification" in s
    assert "matches the sealed digest" in s          # from verify --artifacts
    assert "Anchor verified" in s
    # The anchor's limits are restated where someone reads the result.
    assert "establishes nothing about when" in s
    assert (result["ws"] / "verify.txt").exists()
    assert (result["ws"] / "anchor-verify.txt").exists()


def test_judge_drift_is_reported_when_a_baseline_exists(tmp_path):
    ws = _workspace(tmp_path)
    _baseline(ws, "examples/gsm8k/suite.json")
    result = run_workflow(ws)
    assert not result["failed"], (result["failed_step"], result["log"])

    s = result["summary"]
    assert "Judge drift:" in s
    assert (ws / "judge-drift.json").exists()
    assert (ws / "judge-drift.md").exists()


# --- fault injection: every required check has to fail the job ----------------------
#
# The acceptance criterion for this phase. Each of these makes one required check fail
# and asserts three things: the job fails, the step that failed is named, and the
# diagnostics are still uploaded - a red check with nothing attached is a red check
# nobody can act on.

def _uploaded(ws: Path) -> set[str]:
    """The files the upload step would collect, of those it names."""
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    upload = next(s for s in wf["jobs"]["receipt"]["steps"]
                  if "upload-artifact" in str(s.get("uses", "")))
    named = [line.strip() for line in upload["with"]["path"].splitlines() if line.strip()]
    env = {k: str(v) for k, v in wf["env"].items()}
    present = set()
    for entry in named:
        for key, value in env.items():
            entry = entry.replace(f"${{{{ env.{key} }}}}", value)
        if (ws / entry).exists():
            present.add(entry)
    return present


def test_a_missing_pre_registration_fails_before_anything_runs(tmp_path):
    """Deleting the contract in a pull request used to remove it silently."""
    ws = _workspace(tmp_path)
    (ws / "examples" / "prereg.json").unlink()
    result = run_workflow(ws)
    assert result["failed"]
    assert result["failed_step"] == "Required contracts are present"
    assert "examples/prereg.json is required by REQUIRE_PREREG=true" in result["log"]
    assert "set REQUIRE_PREREG=false" in result["log"]
    # It failed in one second rather than after a replay.
    assert "Replay the suite from its cassette" not in result["ran"]


def test_a_missing_policy_fails_before_anything_runs(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "examples" / "evalseal.yml").unlink()
    result = run_workflow(ws)
    assert result["failed"]
    assert result["failed_step"] == "Required contracts are present"
    assert "is required by POLICY=true" in result["log"]


def test_a_missing_baseline_fails_only_when_it_is_required(tmp_path):
    """False by default: the first pull request on a repository has no history."""
    ws = _workspace(tmp_path)
    assert not (ws / "evals" / "baseline-receipt.json").exists()
    assert not run_workflow(ws)["failed"]

    required = run_workflow(_workspace(tmp_path / "again"), REQUIRE_BASELINE="true")
    assert required["failed"]
    assert required["failed_step"] == "Required contracts are present"
    assert "is required by REQUIRE_BASELINE=true" in required["log"]


def test_a_failed_anchor_verification_fails_the_job(tmp_path):
    """It printed FAILED in the summary and left the check green."""
    ws = _workspace(tmp_path)
    # Corrupt the anchor between its creation and its verification by making the
    # receipt it binds change underneath it.
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in wf["jobs"]["receipt"]["steps"]:
        if step.get("id") == "anchor":
            step["run"] = step["run"].replace(
                "evalseal anchor-verify",
                "python -c \"import json,pathlib;"
                "p=pathlib.Path('report.json');d=json.loads(p.read_text());"
                "d['aggregate']['mean_score']=0.123;"
                "p.write_text(json.dumps(d))\"\n          evalseal anchor-verify")
    patched = tmp_path / "patched.yml"
    patched.write_text(yaml.safe_dump(wf, sort_keys=False), encoding="utf-8")

    result = run_workflow(ws, _workflow=patched)
    assert result["failed"]
    assert result["failed_step"] == "Fail the check if any required check failed"
    assert "anchor-verify failed with exit code" in result["log"]
    assert "REQUIRE_ANCHOR=false" in result["log"]
    # The diagnostics are still there.
    assert "anchor-verify.txt" in _uploaded(result["ws"])
    assert result["summary"]


def test_a_failed_anchor_verification_can_be_downgraded_to_a_report(tmp_path):
    """So a repository that does not want to gate on it still gets the finding."""
    ws = _workspace(tmp_path)
    result = run_workflow(ws, REQUIRE_ANCHOR="false")
    assert not result["failed"], (result["failed_step"], result["log"])


def test_a_failing_gate_still_uploads_its_diagnostics(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "strict.yml").write_text("version: 1\nrun:\n  min_score: 0.999\n",
                                   encoding="utf-8")
    result = run_workflow(ws, POLICY="strict.yml")
    assert result["failed"]
    collected = _uploaded(result["ws"])
    for name in ("gate.json", "report.json", "verify.txt", "anchor.json"):
        assert name in collected, (name, collected)


def test_the_anchor_is_created_before_the_gate_that_may_require_it():
    """Order, not behaviour: `gate --prereg` can only check an anchor that exists."""
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    names = [s.get("name", "") for s in wf["jobs"]["receipt"]["steps"]]
    assert names.index("Anchor the receipt and re-check the anchor") < \
        names.index("Gate against the policy and the pre-registration")
    gate = next(s for s in wf["jobs"]["receipt"]["steps"] if s.get("id") == "gate")
    assert "--anchor anchor.json" in gate["run"]


# --- the fork trust boundary ---------------------------------------------------------
#
# `on: pull_request` checks out the fork's code, so the fork controls the suite, the
# dataset, the cassette and the scorer config. These assert where that stops, and
# document in executable form the one place it does not.

def _fork_scorer(ws: Path, config: str) -> None:
    """What a fork can put in the repository, since the job reads its checkout."""
    (ws / "examples" / "gsm8k" / "scorer.json").write_text(config, encoding="utf-8")


def test_a_fork_supplied_nested_quantifier_is_refused_before_the_run(tmp_path):
    """The half of the boundary that *is* closed, through the real workflow."""
    ws = _workspace(tmp_path)
    _fork_scorer(ws, '{"type": "regex", "pattern": "^(a+)+$"}')
    result = run_workflow(ws)

    assert result["failed"]
    assert result["failed_step"] == "Replay the suite from its cassette"
    assert "unbounded quantifier" in result["log"]
    # It failed on reading the config, so nothing was scored with it.
    assert not (ws / "report.json").exists()


def test_a_fork_supplied_overlapping_alternation_is_not_refused(tmp_path):
    """The half that is **not** closed, pinned so the gap cannot be forgotten.

    `^(a|a)*$` is exponential for the same reason `(a+)+` is, and deciding that in
    general is deciding regular-expression ambiguity. If a future check starts
    refusing it, this test should be inverted and the blocker note removed from
    docs/pr-receipt-workflow.{md,yml} in the same change.
    """
    from evalseal.patterns import UnsafePattern, check_pattern

    check_pattern(r"^(a|a)*$")          # accepted: no exception
    with pytest.raises(UnsafePattern):
        check_pattern(r"^(a+)+$")       # refused, for contrast


def test_the_measured_blow_up_the_guide_quotes_is_real():
    """The guide quotes timings; a doubling-every-few-characters shape is the claim."""
    import re
    import time

    pattern = re.compile(r"^(a|a)*$")
    timings = []
    for n in (12, 16, 20):
        text = "a" * n + "!"
        started = time.perf_counter()
        pattern.search(text)
        timings.append(time.perf_counter() - started)
    # Four more characters, at least eight times the work. The machine decides the
    # constant; it does not decide the exponent.
    assert timings[2] > timings[1] * 8 > timings[0] * 8, timings


def test_the_blocker_is_documented_where_someone_enabling_this_will_see_it():
    guide = (REPO / "docs" / "pr-receipt-workflow.md").read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "The fork trust boundary" in guide
    assert "^(a|a)*$" in guide and "^(a|a)*$" in workflow
    # The mitigation is named as a bound, never as a fix.
    assert "It is not a fix" in guide or "is not a fix" in workflow
    assert "Require approval" in guide and "outside collaborators" in guide
    assert "head.repo.full_name == github.repository" in guide
    # And the thing that would turn it from annoying into dangerous is still forbidden.
    assert "Never switch this to `pull_request_target`" in guide
    assert "pull_request_target" in workflow


def test_the_job_is_time_bounded():
    job = _workflow(WORKFLOW)["jobs"]["receipt"]
    assert job["timeout-minutes"] == 10


def test_the_fork_cannot_reach_a_secret_or_write_anything():
    wf = _workflow(WORKFLOW)
    assert wf["permissions"] == {"contents": "read"}
    # PyYAML reads a bare `on:` as the boolean True - YAML 1.1, where `on` is a
    # boolean literal. The trigger lives under that key.
    triggers = wf.get("on", wf.get(True))
    assert set(triggers) == {"pull_request"}, triggers
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in body, "this workflow must not read a secret"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
