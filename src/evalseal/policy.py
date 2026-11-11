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

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .diffing import DiffResult, diff_records, load_receipt
from .independence import INDEPENDENT, audit_independence
from .ledger import (
    config_fingerprint,
    evaluator_fingerprint,
    explain_fingerprint_mismatch,
    verify_chain,
)
from .models import FailOn, RunRecord
from .paired import REGRESSION, PairedComparison
from .report import failing_cases
from .validation import InvalidInput, strict_json_loads


class _Strict(BaseModel):
    # A typo in a policy file must not read as "no opinion".
    model_config = ConfigDict(extra="forbid")


class RunRules(_Strict):
    """Thresholds the run has to clear on its own."""
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    max_flip_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    max_unstable_cases: int | None = Field(default=None, ge=0)
    #: Observations the grader could not read. A score floor cannot see these: an
    #: ungraded repeat scores 0.0, so a grader failing on the hard cases looks exactly
    #: like a model getting them wrong, and a *case* whose every repeat went ungraded
    #: contributes a 0.0 with nothing measured behind it. Both forms default to off,
    #: so a policy written before 2.3 behaves as it did.
    max_unscorable: int | None = Field(default=None, ge=0)
    max_unscorable_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Fail when any case has no verdict at all. Sharper than the two above: one such
    #: case is a hole in the measurement whatever the suite-wide fraction says.
    require_every_case_scored: bool = False
    #: Fail when a case's repeats were not separate draws - when one recorded response
    #: was served to several of them, so an interval over N repeats is backed by fewer
    #: than N observations. A receipt that cannot answer the question (anything sealed
    #: before schema 1.8) fails this rule too: a rule that skipped on "unknown" would
    #: report a pass for a check that did not happen. Default off.
    require_independent_repeats: bool = False
    fail_on: FailOn | None = None
    verify_ledger: bool = True
    expect_evaluator: str | None = None
    expect_config: str | None = None


class CaseRules(_Strict):
    """Cases that carry more weight than the average."""
    critical: list[str] = Field(default_factory=list)
    min_score: dict[str, float] = Field(default_factory=dict)


#: What a regression budget is being asked for.
#:
#:   fail_on_evidence_of_regression - fail only when the paired test supports a
#:       regression *and* the drop exceeds the budget. Permissive by construction: an
#:       inconclusive comparison passes, because nothing was established either way.
#:       The default, and what every policy written before 2.3 means.
#:   require_evidence_within_budget - pass only when the interval rules out a drop
#:       larger than the budget. An inconclusive comparison **fails**: the policy asked
#:       for evidence the run is within budget and there is none.
#:
#: The two are not opposites of one another, and the reason there are two is that
#: "no evidence of a regression" and "evidence of no regression" are different claims
#: about the same experiment. A gate that offers only the first reads as the second.
BudgetPolicy = Literal["fail_on_evidence_of_regression", "require_evidence_within_budget"]

#: The three states a regression budget can be in. Reported by name, so a green tick
#: cannot stand for two of them.
EVIDENCE_OF_REGRESSION = "evidence_of_regression"
EVIDENCE_WITHIN_BUDGET = "evidence_within_budget"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DriftRules(_Strict):
    """What has to hold against a baseline run."""
    baseline: str
    require_comparable: bool = True
    max_score_regression: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Which question `max_score_regression` is asking. See `BudgetPolicy`.
    regression_budget_policy: BudgetPolicy = "fail_on_evidence_of_regression"
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
        # Strict: a duplicate member is resolved silently in favour of the last one, so
        # a policy committed as `{"min_score": 0.9, "min_score": 0.0}` is reviewed as
        # 0.9 and enforced as 0.0. Nothing hashes a policy file, so no other layer
        # would notice.
        try:
            data = strict_json_loads(text, what=str(p))
        except InvalidInput as e:
            raise PolicyError(str(e)) from None
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
        # The denominator is named here on purpose. A mean over 40 cases of which 6
        # were never graded is a different quantity from a mean over 40 graded cases,
        # and a reader comparing two gate logs has no other way to see the difference.
        detail = f"mean score {agg.mean_score:.3f} vs floor {rules.min_score:.3f}"
        if agg.n_observations:
            detail += f" over {agg.n_cases} case(s), {agg.n_observations} repeat(s)"
        if agg.n_unscorable:
            detail += (f"; {agg.n_unscorable} repeat(s) were not gradeable and scored "
                       "0.0, so this floor is being applied to a partly ungraded run")
        checks.append(Check("run.min_score", ok, detail))

    if rules.max_unscorable is not None:
        ok = agg.n_unscorable <= rules.max_unscorable
        checks.append(Check(
            "run.max_unscorable", ok,
            f"{agg.n_unscorable} ungraded repeat(s) of {agg.n_observations} "
            f"vs limit {rules.max_unscorable}"))

    if rules.max_unscorable_fraction is not None:
        share = agg.n_unscorable / agg.n_observations if agg.n_observations else 0.0
        ok = share <= rules.max_unscorable_fraction
        checks.append(Check(
            "run.max_unscorable_fraction", ok,
            f"{share:.1%} of {agg.n_observations} repeat(s) ungraded vs limit "
            f"{rules.max_unscorable_fraction:.1%}"))

    if rules.require_every_case_scored:
        blind = sorted(c.case_id for c in record.results
                       if c.scores and c.unscorable_count == len(c.scores))
        checks.append(Check(
            "run.require_every_case_scored", not blind,
            f"{len(blind)} case(s) have no verdict at all"
            + (f": {', '.join(blind[:8])}" if blind else "")
            + ("" if not blind else
               ". Each scores 0.0 with nothing graded behind it, which no score floor "
               "can distinguish from a case that failed.")))

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

    if rules.require_independent_repeats:
        audit = audit_independence(record)
        checks.append(Check(
            "run.require_independent_repeats",
            audit.verdict == INDEPENDENT, audit.summary()))

    if rules.fail_on is not None:
        failed = failing_cases(record, rules.fail_on)
        detail = (f"{len(failed)} case(s) violate fail_on={rules.fail_on}"
                  + (f": {', '.join(c.case_id for c in failed)}" if failed else ""))
        checks.append(Check("run.fail_on", not failed, detail))

    for rule, kind, expected, actual in (
        ("run.expect_evaluator", "evaluator",
         rules.expect_evaluator, evaluator_fingerprint(record)),
        ("run.expect_config", "config", rules.expect_config, config_fingerprint(record)),
    ):
        if expected is not None:
            ok = expected == actual
            checks.append(Check(
                rule, ok,
                f"matches the pinned fingerprint ({actual[:28]}...)" if ok
                else explain_fingerprint_mismatch(kind, expected, actual)))
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


def budget_state(paired: PairedComparison | None, delta: float, budget: float) -> str:
    """Which of the three states a regression budget is in. See `BudgetPolicy`.

    `evidence_of_regression` needs the paired comparison to support a regression *and*
    the observed drop to exceed the budget.

    `evidence_within_budget` needs the interval's lower bound to sit above `-budget`:
    the data rule out a drop that large. This is a non-inferiority question, and it is
    not answered by failing to find a regression - a tiny, uninformative experiment
    fails to find one every time.

    Anything else is `insufficient_evidence`, which is the usual answer at N=5 and the
    one a gate must not print as a green tick without saying so.
    """
    if paired is None:
        return INSUFFICIENT_EVIDENCE
    if paired.verdict == REGRESSION and -delta > budget:
        return EVIDENCE_OF_REGRESSION
    if paired.n_items and paired.ci[0] > -budget:
        return EVIDENCE_WITHIN_BUDGET
    return INSUFFICIENT_EVIDENCE


def _budget_check(rules: DriftRules, result: DiffResult) -> Check:
    """Apply `max_score_regression` under the policy that was asked for.

    Before 2.0 the allowance was the budget plus a per-case half-width, which at N=5 is
    0.217 - larger than most real regressions, so the rule passed almost anything.
    Requiring significance replaced that, and is right for the question "is there
    evidence of a regression" and wrong for "is this run within budget": a 0.600 drop
    against a 0.050 budget over five items passed, because five items cannot reach
    significance at all and the rule needed both conditions to fail a build. Both
    questions are now askable and the detail always names which was asked and what the
    evidence supports.
    """
    budget = rules.max_score_regression
    assert budget is not None       # only called when the rule is set
    paired = result.paired
    state = budget_state(paired, result.score_delta, budget)
    detail = (
        f"[{state}] score moved {result.score_delta:+.4f} against a budget of "
        f"{budget:.3f}"
    )
    if paired is not None:
        detail += (
            f"; paired test says {paired.verdict} over {paired.n_items} item(s) "
            f"(p={paired.p_value:.4g}, {paired.ci_label} "
            f"[{paired.ci[0]:+.4f}, {paired.ci[1]:+.4f}])"
        )
    else:
        detail += "; no paired comparison was possible, so nothing was established"

    if rules.regression_budget_policy == "require_evidence_within_budget":
        passed = state == EVIDENCE_WITHIN_BUDGET
        if not passed and state == INSUFFICIENT_EVIDENCE:
            detail += (
                ". This policy asks for evidence that the run is within budget, and "
                "the interval does not rule out a larger drop. Failing to find a "
                "regression is not the same as finding there is none."
            )
    else:
        passed = state != EVIDENCE_OF_REGRESSION
        if state == INSUFFICIENT_EVIDENCE:
            detail += (
                ". Passing here means no regression was established, not that the run "
                "is within budget - this experiment could not tell. Set "
                "regression_budget_policy: require_evidence_within_budget to make that "
                "the question."
            )
    if paired is not None and paired.mean_moved_without_majorities:
        detail += (
            f" The interval excludes zero while only {paired.n_discordant} item(s) "
            "changed majority verdict, so the shift is within items rather than across "
            "them."
        )
    return Check("drift.max_score_regression", passed, detail)


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
        checks.append(_budget_check(rules, result))

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
