"""Check a sealed run against a policy file that lives in the repo.

A gate spelled out in CI flags is a gate nobody reads. A policy file is reviewable,
diffable, and blames someone in `git log` when it loosens.

Two rules shape this module, and both exist because the usual failure of a quality gate
is not that it fails wrongly - it is that it silently checks nothing:

*Unknown keys are errors.* A policy with `min_scor: 0.9` is refused, not ignored. A
threshold that quietly does nothing is worse than no threshold, because the build going
green is read as evidence.

*Unrunnable rules are failures, not skips.* If a drift rule names a baseline that is not
there, the gate fails. Skipping it would report a pass for a comparison that never
happened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .diffing import diff_records, load_receipt
from .ledger import config_fingerprint, evaluator_fingerprint, verify_chain
from .models import FailOn, RunRecord
from .report import failing_cases


class _Strict(BaseModel):
    # A typo in a policy file must not read as "no opinion".
    model_config = ConfigDict(extra="forbid")


class RunRules(_Strict):
    """Thresholds the run has to clear on its own."""
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    max_flip_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    max_unstable_cases: int | None = Field(default=None, ge=0)
    fail_on: FailOn | None = None
    verify_ledger: bool = True
    expect_evaluator: str | None = None
    expect_config: str | None = None


class CaseRules(_Strict):
    """Cases that carry more weight than the average."""
    critical: list[str] = Field(default_factory=list)
    min_score: dict[str, float] = Field(default_factory=dict)


class DriftRules(_Strict):
    """What has to hold against a baseline run."""
    baseline: str
    require_comparable: bool = True
    max_score_regression: float | None = Field(default=None, ge=0.0, le=1.0)
    allow_new_unstable: bool = True
    allow_removed_cases: bool = True


class Policy(_Strict):
    version: Literal[1] = 1
    run: RunRules = RunRules()
    cases: CaseRules = CaseRules()
    drift: DriftRules | None = None


@dataclass
class Check:
    """One rule that ran, and whether the record satisfied it."""
    rule: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"rule": self.rule, "passed": self.passed, "detail": self.detail}


@dataclass
class PolicyResult:
    checks: list[Check]

    @property
    def violations(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "violations": [c.to_dict() for c in self.violations],
        }


class PolicyError(ValueError):
    """The policy file itself is wrong, as opposed to the run failing it."""


def load_policy(path: str | Path) -> Policy:
    """Read a policy from JSON, or YAML when PyYAML is available.

    The extension decides the parser. Guessing from content would make a malformed JSON
    file silently fall through to a YAML parser that accepts almost anything.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    if not text.strip():
        raise PolicyError(f"{p} is empty")

    if suffix in {".yml", ".yaml"}:
        try:
            import yaml
        except ModuleNotFoundError as e:
            raise PolicyError(
                f"{p} is YAML, which needs PyYAML: pip install 'evalseal[yaml]' "
                "(or write the policy as JSON)"
            ) from e
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise PolicyError(f"{p} is not valid YAML: {e}") from e
    elif suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise PolicyError(f"{p} is not valid JSON: {e}") from e
    else:
        raise PolicyError(f"{p} must be .json, .yml or .yaml")

    if data is None:
        raise PolicyError(f"{p} is empty")
    if not isinstance(data, dict):
        raise PolicyError(f"{p} must contain a mapping at the top level")

    try:
        return Policy.model_validate(data)
    except ValidationError as e:
        raise PolicyError(f"{p} is not a valid policy:\n{e}") from e


def _resolve_baseline(spec: str, policy_dir: Path) -> RunRecord:
    """A baseline path is relative to the policy file, which is where a reader looks."""
    candidate = Path(spec)
    if not candidate.is_absolute():
        candidate = policy_dir / candidate
    if not candidate.exists():
        raise PolicyError(
            f"drift baseline {spec!r} not found (looked in {candidate}). A drift rule "
            "that cannot run is a failure, not a skip."
        )
    return load_receipt(candidate)


def _run_checks(rules: RunRules, record: RunRecord, ledger: Path | None) -> list[Check]:
    checks: list[Check] = []
    agg = record.aggregate

    if rules.verify_ledger and ledger is not None:
        ok, msg = verify_chain(ledger)
        checks.append(Check("run.verify_ledger", ok, msg))

    if rules.min_score is not None:
        ok = agg.mean_score >= rules.min_score
        checks.append(Check(
            "run.min_score", ok,
            f"mean score {agg.mean_score:.3f} vs floor {rules.min_score:.3f}"))

    if rules.max_flip_rate is not None:
        over = [c for c in record.results if c.flip_rate > rules.max_flip_rate]
        detail = f"{len(over)} case(s) over {rules.max_flip_rate:.0%}"
        if over:
            worst = max(over, key=lambda c: c.flip_rate)
            detail += f" (worst: {worst.case_id} at {worst.flip_rate:.0%})"
        checks.append(Check("run.max_flip_rate", not over, detail))

    if rules.max_unstable_cases is not None:
        ok = agg.n_unstable <= rules.max_unstable_cases
        checks.append(Check(
            "run.max_unstable_cases", ok,
            f"{agg.n_unstable} unstable vs limit {rules.max_unstable_cases}"))

    if rules.fail_on is not None:
        failed = failing_cases(record, rules.fail_on)
        detail = (f"{len(failed)} case(s) violate fail_on={rules.fail_on}"
                  + (f": {', '.join(c.case_id for c in failed)}" if failed else ""))
        checks.append(Check("run.fail_on", not failed, detail))

    for rule, expected, actual in (
        ("run.expect_evaluator", rules.expect_evaluator, evaluator_fingerprint(record)),
        ("run.expect_config", rules.expect_config, config_fingerprint(record)),
    ):
        if expected is not None:
            ok = expected == actual
            checks.append(Check(
                rule, ok, f"expected {expected[:20]}..., got {actual[:20]}..."))
    return checks


def _case_checks(rules: CaseRules, record: RunRecord) -> list[Check]:
    checks: list[Check] = []
    by_id = {c.case_id: c for c in record.results}

    if rules.critical:
        # One check per missing id, so a rule name identifies a single thing in --json
        # exactly as `cases.min_score[...]` does. Naming a case the record does not have
        # almost always means the dataset moved out from under the policy.
        for case_id in sorted(set(rules.critical) - set(by_id)):
            checks.append(Check(
                f"cases.critical[{case_id}]", False, "case not in this record"))
        flipped = sorted(cid for cid in rules.critical
                         if cid in by_id and by_id[cid].flip_count)
        checks.append(Check(
            "cases.critical", not flipped,
            f"{len(flipped)} critical case(s) flipped"
            + (f": {', '.join(flipped)}" if flipped else "")))

    for case_id, floor in sorted(rules.min_score.items()):
        if case_id not in by_id:
            checks.append(Check(
                f"cases.min_score[{case_id}]", False, "case not in this record"))
            continue
        actual = by_id[case_id].mean
        checks.append(Check(
            f"cases.min_score[{case_id}]", actual >= floor,
            f"{actual:.3f} vs floor {floor:.3f}"))
    return checks


def _drift_checks(rules: DriftRules, record: RunRecord, baseline: RunRecord) -> list[Check]:
    result = diff_records(baseline, record)
    checks: list[Check] = []

    if rules.require_comparable:
        changed = ", ".join(c.name for c in result.evaluator_changes)
        checks.append(Check(
            "drift.require_comparable", result.comparable,
            f"evaluator changed: {changed}" if changed
            else "same grading setup on both sides"))

    if rules.max_score_regression is not None:
        # The noise floor is added to the allowance rather than replacing it: a drop the
        # runs cannot distinguish from noise is not a regression to fail a build over.
        allowed = rules.max_score_regression + result.noise_floor
        drop = -result.score_delta
        checks.append(Check(
            "drift.max_score_regression", drop <= allowed,
            f"score moved {result.score_delta:+.3f}; allowance "
            f"{rules.max_score_regression:.3f} + noise floor {result.noise_floor:.3f}"))

    if not rules.allow_new_unstable:
        new = result.newly_unstable
        checks.append(Check(
            "drift.allow_new_unstable", not new,
            f"{len(new)} case(s) started flipping"
            + (f": {', '.join(new)}" if new else "")))

    if not rules.allow_removed_cases:
        gone = result.cases_removed
        checks.append(Check(
            "drift.allow_removed_cases", not gone,
            f"{len(gone)} case(s) left the dataset"
            + (f": {', '.join(gone)}" if gone else "")))
    return checks


def evaluate(
    policy: Policy,
    record: RunRecord,
    *,
    ledger: Path | None = None,
    policy_dir: Path | None = None,
) -> PolicyResult:
    """Apply every rule in the policy and report each one, passed or failed.

    Passing checks are returned too. A gate that prints only failures leaves a reader
    unable to tell a clean run from a policy that checked nothing.
    """
    checks = _run_checks(policy.run, record, ledger)
    checks += _case_checks(policy.cases, record)
    if policy.drift is not None:
        baseline = _resolve_baseline(policy.drift.baseline, policy_dir or Path("."))
        checks += _drift_checks(policy.drift, record, baseline)
    return PolicyResult(checks)
