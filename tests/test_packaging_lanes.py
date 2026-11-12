"""Packaging and release structure, asserted so it cannot drift back.

Three gaps this pins shut.

**Nothing tested the artifact.** Every test in `tests/` runs with
`pythonpath = ["src"]` from the repository root against an editable install, which is
the right way to test behaviour and the wrong way to test packaging. A module missing
from the wheel, `py.typed` not shipped, or a console script that does not resolve passes
that lane and ships. `scripts/wheel_smoke.py` installs the built wheel into an
environment outside the repository and asserts that `import evalseal` resolves there.

**Release did not depend on lint, audit or packaging.** `build` needed only
`cross-platform-tests`; ruff, mypy and `pip-audit` lived in a separate workflow that a
tag push does not wait for. A release could publish code that failed any of them.

**The lower bounds were guesses.** `pydantic>=2.6` and the rest are a promise to anyone
installing EvalSeal alongside other packages, and resolving the newest of everything
never tests them.

The slow lane here - actually building and installing the wheel - is opt-in, because it
needs the network and about a minute: set `EVALSEAL_WHEEL_SMOKE=1`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
RELEASE = REPO / ".github" / "workflows" / "release.yml"
SMOKE = REPO / "scripts" / "wheel_smoke.py"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _needs(job: dict) -> set[str]:
    needs = job.get("needs") or []
    return {needs} if isinstance(needs, str) else set(needs)


# --- the release waits for everything ------------------------------------------------

def test_the_release_build_waits_for_lint_audit_and_tests():
    jobs = _workflow(RELEASE)["jobs"]
    assert _needs(jobs["build"]) == {"lint", "audit", "cross-platform-tests"}
    for name in ("lint", "audit"):
        assert name in jobs, f"release.yml has no {name} job to depend on"


def test_the_release_lint_job_runs_the_same_checks_as_ci():
    release = _workflow(RELEASE)["jobs"]["lint"]
    ci = _workflow(CI)["jobs"]["lint"]
    commands = {s["run"] for s in release["steps"] if "run" in s}
    for step in (s["run"] for s in ci["steps"] if "run" in s):
        assert step in commands, f"release lint does not run {step!r}"


def test_the_release_audits_dependencies_before_publishing():
    audit = _workflow(RELEASE)["jobs"]["audit"]
    assert any("pip-audit" in s.get("run", "") for s in audit["steps"])
    assert any("--strict" in s.get("run", "") for s in audit["steps"])


def test_publishing_waits_for_the_installed_wheel_check():
    jobs = _workflow(RELEASE)["jobs"]
    assert "installed-wheel" in jobs
    assert _needs(jobs["publish-pypi"]) == {"build", "installed-wheel"}
    assert _needs(jobs["installed-wheel"]) == {"build"}


def test_the_installed_wheel_check_covers_the_other_two_platforms():
    job = _workflow(RELEASE)["jobs"]["installed-wheel"]
    assert set(job["strategy"]["matrix"]["os"]) == {"macos-latest", "windows-latest"}
    # Linux is covered inside `build`, where the wheel is made.
    build = _workflow(RELEASE)["jobs"]["build"]
    assert any("wheel_smoke.py" in s.get("run", "") for s in build["steps"])


def test_what_is_published_is_what_was_validated():
    """`publish-pypi` downloads the artifact `build` uploaded; it does not rebuild."""
    jobs = _workflow(RELEASE)["jobs"]
    uploaded = [s for s in jobs["build"]["steps"]
                if "upload-artifact" in str(s.get("uses", ""))]
    assert len(uploaded) == 1
    name = uploaded[0]["with"]["name"]

    publish = jobs["publish-pypi"]["steps"]
    downloaded = [s for s in publish if "download-artifact" in str(s.get("uses", ""))]
    assert len(downloaded) == 1
    assert downloaded[0]["with"]["name"] == name
    assert not any("python -m build" in s.get("run", "") for s in publish), (
        "publish-pypi must not build again: it would publish a second artifact that "
        "nothing validated")


def test_every_release_action_is_pinned_to_a_sha():
    for path in (RELEASE, CI):
        for job in _workflow(path)["jobs"].values():
            for step in job["steps"]:
                uses = step.get("uses")
                if uses is None:
                    continue
                _, _, ref = uses.partition("@")
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (
                    f"{path.name}: {uses} is not pinned to a commit sha")


# --- the CI lanes ---------------------------------------------------------------------

def test_ci_has_a_packaging_lane_on_all_three_platforms():
    job = _workflow(CI)["jobs"]["wheel"]
    assert set(job["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest", "macos-latest", "windows-latest"}
    assert any("wheel_smoke.py" in s.get("run", "") for s in job["steps"])


def test_the_packaging_lane_does_not_install_the_source_tree():
    """An editable install would put `src/` on the path and defeat the lane."""
    job = _workflow(CI)["jobs"]["wheel"]
    for step in job["steps"]:
        run = step.get("run", "")
        assert "pip install -e" not in run, run


def test_ci_has_a_minimum_dependency_lane():
    job = _workflow(CI)["jobs"]["minimum-deps"]
    runs = " ".join(s.get("run", "") for s in job["steps"])
    assert "constraints.txt" in runs
    assert "-c constraints.txt" in runs
    assert "pytest" in runs


def test_the_minimum_lane_pins_every_declared_lower_bound():
    """The generator in the workflow, run here against the real pyproject."""
    project = tomllib.loads(
        (REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = project["dependencies"] + project["optional-dependencies"]["dev"]
    pinned = {}
    for spec in declared:
        name, _, bound = spec.partition(">=")
        bound = bound.split(";")[0].split("#")[0].strip()
        if bound:
            pinned[name.strip()] = bound
    # Every runtime dependency has to carry a lower bound, or the lane cannot pin it.
    for spec in project["dependencies"]:
        name = spec.partition(">=")[0].strip()
        assert name in pinned, f"{spec!r} declares no lower bound to test"
    assert pinned["pydantic"] == "2.6"
    assert pinned["httpx"] == "0.27"


def test_no_transitive_dependency_is_pinned_for_downstream_users():
    """A library that pins its whole tree cannot be installed beside anything else."""
    project = tomllib.loads(
        (REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for spec in project["dependencies"]:
        assert "==" not in spec, f"{spec!r} pins exactly; use a lower bound"


# --- the smoke script's own claims ---------------------------------------------------

def test_the_wheel_includes_py_typed():
    """Annotations are invisible downstream without it, however many there are."""
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    force = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force["src/evalseal/py.typed"] == "evalseal/py.typed"
    assert (REPO / "src" / "evalseal" / "py.typed").is_file()


def test_the_smoke_script_refuses_to_pass_on_the_source_tree():
    """Its central assertion, read from the source: without it the lane tests nothing."""
    text = SMOKE.read_text(encoding="utf-8")
    assert "inside the repository" in text
    assert "REPO in package.parents" in text
    assert 'env["PYTHONPATH"] = ""' in text


def test_the_smoke_script_is_syntactically_valid_and_self_describing():
    proc = subprocess.run([sys.executable, str(SMOKE), "--help"],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "--wheel" in proc.stdout and "--keep" in proc.stdout


@pytest.mark.skipif(
    os.environ.get("EVALSEAL_WHEEL_SMOKE") != "1",
    reason="builds a wheel and creates a venv; set EVALSEAL_WHEEL_SMOKE=1 to run")
def test_the_wheel_really_works_from_a_clean_environment():
    """The slow lane, run in CI on every platform and opt-in locally."""
    proc = subprocess.run([sys.executable, str(SMOKE)], cwd=REPO,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=1800)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "the wheel is usable from a clean environment" in proc.stdout


# --- the documented escape hatch has to work -----------------------------------------

@pytest.mark.parametrize("module", ["evalseal", "evalseal.cli"])
def test_running_the_package_as_a_module_works(module):
    """docs/operations.md offers this when the console script is not on PATH.

    It promised `python -m evalseal.cli`, which defined `app` and never called it:
    the command printed nothing and exited 0. A documented escape hatch that silently
    does nothing is worse than none, because the person using it believes they ran
    something.
    """
    proc = subprocess.run([sys.executable, "-m", module, "--version"],
                          cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "evalseal" in proc.stdout
    assert "record schema" in proc.stdout


def test_the_troubleshooting_row_names_a_spelling_that_works():
    import tomllib

    operations = (REPO / "docs" / "operations.md").read_text(encoding="utf-8")
    row = next(line for line in operations.splitlines()
               if "not found after install" in line)
    assert "python -m evalseal" in row
    # And the console script it is a fallback for is actually declared.
    project = tomllib.loads(
        (REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"]["evalseal"] == "evalseal.cli:app"


def test_the_smoke_script_invokes_a_resolved_path():
    """Windows resolves an executable name against the parent's PATH, not the child's."""
    text = SMOKE.read_text(encoding="utf-8")
    assert "resolved = shutil.which" in text
    assert 'evalseal = [resolved] if resolved else' in text
    assert '["evalseal"] if shutil.which' not in text


def test_the_typer_bound_is_the_version_that_actually_works():
    """`typer>=0.12` was declared and never worked.

    Typer before 0.19 raises `RuntimeError: Type not yet supported` on this CLI's
    `Literal` option types, so the command tree could not be built at all. Found by
    the minimum-dependency lane on its first run.
    """
    import tomllib

    project = tomllib.loads(
        (REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    typer_spec = next(s for s in project["dependencies"] if s.startswith("typer"))
    major, minor = (int(part) for part in typer_spec.partition(">=")[2].split(".")[:2])
    assert (major, minor) >= (0, 19), typer_spec


def test_the_cli_command_tree_builds():
    """What the old bound broke: not a runtime edge case, the whole command tree."""
    import typer.main

    from evalseal.cli import app

    command = typer.main.get_command(app)
    assert len(command.commands) >= 10
