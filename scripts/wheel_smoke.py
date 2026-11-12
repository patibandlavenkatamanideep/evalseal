#!/usr/bin/env python3
"""Install the built wheel somewhere else entirely and use it like a user would.

## Why a separate lane

Every test in `tests/` runs with `pythonpath = ["src"]` (pyproject) from the repository
root, against an editable install. That is the right way to test behaviour and the wrong
way to test *packaging*: a module missing from the wheel, `py.typed` not shipped, a
console script that does not resolve, a data file referenced from `examples/` but never
included - all of them pass that lane and ship.

So this one builds the wheel, installs it into a virtual environment **outside the
repository**, changes into a scratch directory with no `src/` anywhere above it, and runs
the commands a reader of the README would run. It asserts that `evalseal` resolves to
site-packages, which is the one thing that makes the rest of it mean anything.

## What it does not do

It is a smoke check, not a second test suite. It answers "is the artifact usable", not
"is the behaviour right" - that is what `tests/` is for, and duplicating assertions here
would mean two places to update and one of them going stale.

Usage:

    python scripts/wheel_smoke.py                 # build, install, exercise
    python scripts/wheel_smoke.py --wheel dist/evalseal-2.2.0-py3-none-any.whl
    python scripts/wheel_smoke.py --keep          # leave the scratch tree for poking

Exit 0 when the wheel is usable, 1 with the failing command's output otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
#: Copied into the scratch tree, because the examples reference them by relative path.
NEEDED_CASSETTES = ("gsm8k_lite.json", "gsm8k.json")


class SmokeFailure(RuntimeError):
    pass


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None,
        expect: tuple[int, ...] = (0,), what: str = "") -> subprocess.CompletedProcess:
    # `text=True` alone decodes with the *locale* codec, which on a Windows runner is
    # cp1252: the first `·` in EvalSeal's own output raised UnicodeDecodeError and the
    # smoke check failed for the encoding of its own transcript.
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    if proc.returncode not in expect:
        raise SmokeFailure(
            f"{what or ' '.join(cmd)} exited {proc.returncode}, expected "
            f"{' or '.join(str(c) for c in expect)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def build_wheel(into: Path) -> Path:
    print("building the wheel")
    run([sys.executable, "-m", "build", "--wheel", "--outdir", str(into)],
        cwd=REPO, what="python -m build")
    wheels = sorted(into.glob("*.whl"))
    if len(wheels) != 1:
        raise SmokeFailure(f"expected one wheel in {into}, found {wheels}")
    return wheels[0]


def make_env(root: Path, wheel: Path) -> Path:
    """A virtual environment outside the repository, with the wheel installed in it."""
    print(f"installing {wheel.name} into a fresh environment")
    env_dir = root / "venv"
    venv.create(env_dir, with_pip=True, clear=True)
    scripts = "Scripts" if os.name == "nt" else "bin"
    python = env_dir / scripts / ("python.exe" if os.name == "nt" else "python")
    run([str(python), "-m", "pip", "install", "--quiet", f"{wheel}[yaml]"],
        cwd=root, what="pip install the wheel")
    return python


def scratch_tree(root: Path) -> Path:
    """The files a user would have: the examples and the cassettes they replay."""
    work = root / "work"
    (work / "tests" / "cassettes").mkdir(parents=True)
    shutil.copytree(REPO / "examples", work / "examples")
    for name in NEEDED_CASSETTES:
        source = REPO / "tests" / "cassettes" / name
        if source.exists():
            shutil.copy(source, work / "tests" / "cassettes" / name)
    return work


def clean_env(python: Path) -> dict[str, str]:
    """An environment with nothing of the repository in it.

    `PYTHONPATH` is cleared rather than trusted: a shell that happened to export
    `src/` would make this whole script test the source tree again.
    """
    scripts = python.parent
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "EVALSEAL_RECORD", "EVALSEAL_API_KEY",
                        "EVALSEAL_MODE")}
    env["PATH"] = f"{scripts}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = ""
    return env


def check_it_is_the_installed_package(python: Path, work: Path,
                                      env: dict[str, str]) -> None:
    """The one assertion the rest depends on: this is the wheel, not the repo."""
    proc = run([str(python), "-c",
                "import evalseal, json, pathlib, sys;"
                "print(json.dumps({'file': evalseal.__file__,"
                " 'version': evalseal.__version__,"
                " 'typed': (pathlib.Path(evalseal.__file__).parent / 'py.typed')"
                ".is_file()}))"],
               cwd=work, env=env, what="locate the installed package")
    where = json.loads(proc.stdout.strip().splitlines()[-1])
    package = Path(where["file"]).resolve()
    if REPO in package.parents:
        raise SmokeFailure(
            f"`import evalseal` resolved to {package}, inside the repository. This lane "
            "has to exercise the installed wheel or it tests nothing new.")
    site = Path(sysconfig.get_path("purelib"))
    print(f"  evalseal {where['version']} from {package.parent}")
    if not where["typed"]:
        raise SmokeFailure(
            "py.typed is not in the wheel, so a downstream project gets no types from "
            "this package however many annotations it carries.")
    if site in package.parents:                       # pragma: no cover - diagnostic
        print("  (note: resolved inside this interpreter's site-packages)")


def exercise(python: Path, work: Path, env: dict[str, str]) -> None:
    """Run what a README reader runs, in a directory with no source tree above it."""
    # The resolved absolute path, not the bare name. On Windows `subprocess` looks an
    # executable name up in the *parent* process's PATH - the `env` handed to the
    # child has no say in it - so `["evalseal", ...]` raised FileNotFoundError on a
    # runner where the venv was only ever on the child's PATH.
    resolved = shutil.which("evalseal", path=env["PATH"])
    evalseal = [resolved] if resolved else [str(python), "-m", "evalseal"]
    print(f"  invoking {evalseal[0]}")

    print("  --version and every --help")
    run([*evalseal, "--version"], cwd=work, env=env, what="evalseal --version")
    run([*evalseal, "--help"], cwd=work, env=env, what="evalseal --help")
    # The command list comes from the installed app rather than from parsing rich's
    # output, which wraps and colours and is not a machine-readable surface.
    names = json.loads(run(
        [str(python), "-c",
         "import json, typer.main;"
         "from evalseal.cli import app;"
         "print(json.dumps(sorted(c.name or c.callback.__name__ "
         "for c in app.registered_commands)))"],
        cwd=work, env=env, what="list the commands").stdout.strip().splitlines()[-1])
    broken = []
    for name in names:
        proc = subprocess.run([*evalseal, name, "--help"], cwd=work, env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
        if proc.returncode != 0:
            broken.append((name, proc.stdout + proc.stderr))
    if broken:
        detail = "\n".join(f"  {n}: {out.strip()[:200]}" for n, out in broken)
        raise SmokeFailure(f"{len(broken)} subcommand help screen(s) failed:\n{detail}")
    if len(names) < 10:
        raise SmokeFailure(f"only {len(names)} commands registered; expected 10+")
    print(f"    {len(names)} subcommand help screens exited 0")

    suite = Path("examples") / "gsm8k" / "suite-lite.json"
    if (work / "tests" / "cassettes" / "gsm8k_lite.json").exists():
        print("  replay a suite from its committed cassette")
        run([*evalseal, "run", "--suite", str(suite), "--ledger", "s.jsonl",
             "--quiet", "--fail-on", "none"], cwd=work, env=env,
            what="evalseal run")
        for step in (
            ["verify", "--ledger", "s.jsonl", "--artifacts"],
            ["report", "report.json"],
            ["report", "report.json", "--json"],
            ["evidence", "report.json", "--json"],
            ["gate", "--ledger", "s.jsonl", "--min-score", "0.0"],
            ["anchor", "report.json", "--ledger", "s.jsonl", "--out", "a.json"],
            ["anchor-verify", "a.json", "report.json", "--ledger", "s.jsonl"],
            ["keygen", "--private-key", "k.key", "--public-key", "k.pub"],
            ["sign", "--ledger", "s.jsonl", "--key", "k.key"],
            ["verify", "--ledger", "s.jsonl", "--public-key", "k.pub"],
            ["recover", "--ledger", "s.jsonl", "--yes"],
        ):
            print(f"  {' '.join(step[:2])}")
            run([*evalseal, *step], cwd=work, env=env,
                what=f"evalseal {' '.join(step)}")
        print("  diff and drift against a second arm")
        run([*evalseal, "run", "--suite", str(suite), "--ledger", "s.jsonl",
             "--quiet", "--fail-on", "none", "--out-dir", "run2"], cwd=work, env=env,
            what="second run")
        run([*evalseal, "diff", "0", "1", "--ledger", "s.jsonl", "--json"],
            cwd=work, env=env, what="evalseal diff")
        run([*evalseal, "drift", "0", "1", "--ledger", "s.jsonl", "--json"],
            cwd=work, env=env, what="evalseal drift")
    else:
        print("  (no gsm8k_lite cassette in this checkout; replay steps skipped)")

    print("  the offline examples that ship with the package")
    drift = Path("examples") / "not-comparable-drift"
    if (work / drift).exists():
        run([*evalseal, "diff", str(drift / "baseline.json"),
             str(drift / "candidate.json"), "--json"], cwd=work, env=env,
            what="diff the not-comparable example")
        run([*evalseal, "drift", str(drift / "baseline.json"),
             str(drift / "candidate.json"), "--json"], cwd=work, env=env,
            what="drift the not-comparable example")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, help="Use this wheel instead of building.")
    parser.add_argument("--keep", action="store_true", help="Keep the scratch tree.")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="evalseal-wheel-smoke-"))
    print(f"scratch tree: {root}")
    try:
        wheel = args.wheel.resolve() if args.wheel else build_wheel(root / "dist")
        python = make_env(root, wheel)
        work = scratch_tree(root)
        env = clean_env(python)
        check_it_is_the_installed_package(python, work, env)
        exercise(python, work, env)
    except SmokeFailure as e:
        # Kept on failure: the point of a packaging check is that you can go and look
        # at the tree it failed in.
        print(f"\nFAILED: {e}", file=sys.stderr)
        print(f"(scratch tree kept at {root} for inspection)", file=sys.stderr)
        return 1
    print("\nthe wheel is usable from a clean environment")
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
