"""Split an LLM-judged suite's variance into the judge's share and the target's.

## Why this exists

The README used to argue, from two suites side by side, that "the irreproducibility came
from the judge, not the model": a judge-graded suite flipped on 5 of 20 cases while an
arithmetic-graded suite flipped on none of 40. That comparison cannot support the claim.
The two suites differ in the grader *and* in the task - arguable open prompts versus
GSM8K arithmetic - so a difference in flip rate is consistent with the judge being
unreliable, with open-ended tasks drawing more variable answers out of the model, or
with both. The design confounds the two explanations.

This runs the experiment that separates them, on one suite, holding the task fixed.

## The two arms

**judge_only.** Sample the target once, then judge that one fixed response N times. The
response cannot vary, so every flip is the judge disagreeing with itself.

**target_and_judge.** Sample the target N times and judge each response once. This is an
ordinary `evalseal run`, and its flips contain both sources.

Comparing the per-case flip rates gives the decomposition. If judge_only accounts for
most of target_and_judge, the grader is the unstable component. If target_and_judge is
much higher, the target is contributing variance of its own.

## What it still cannot tell you

The arms are not a clean variance decomposition into additive components. Judging one
fixed response N times measures the judge's variance *at that response*, and a response
the target happened to produce once may be easier or harder to grade consistently than
the ones it would produce on other draws. So judge_only estimates the judge's
contribution conditional on a single draw, not averaged over the target's output
distribution. Read it as a lower bound on how much of the flipping the judge can
account for, not as an exact share.

Each arm records to its own cassette. Sharing one would let the arms collide whenever
the target's first response happens to equal a later one, which would couple two
measurements that the experiment needs to keep separate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .adapters.dataset import Dataset
from .adapters.recording import slot
from .adapters.scorer import Scorer
from .adapters.target import Target
from .analyze import flip_rate

JUDGE_ONLY = "judge_only"
TARGET_AND_JUDGE = "target_and_judge"


@dataclass
class CaseDecomposition:
    case_id: str
    judge_only_verdicts: list[int] = field(default_factory=list)
    full_verdicts: list[int] = field(default_factory=list)

    @property
    def judge_only_flip_rate(self) -> float:
        return flip_rate(self.judge_only_verdicts)[0]

    @property
    def full_flip_rate(self) -> float:
        return flip_rate(self.full_verdicts)[0]

    @property
    def judge_share(self) -> float | None:
        """Fraction of the full flip rate the judge alone accounts for.

        None when the full arm showed no flips at all: there is nothing to apportion,
        and reporting 0 would suggest the judge was measured and found stable.
        """
        full = self.full_flip_rate
        if full == 0:
            return None
        return min(1.0, self.judge_only_flip_rate / full)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "judge_only_verdicts": self.judge_only_verdicts,
            "full_verdicts": self.full_verdicts,
            "judge_only_flip_rate": round(self.judge_only_flip_rate, 4),
            "full_flip_rate": round(self.full_flip_rate, 4),
            "judge_share": self.judge_share,
        }


@dataclass
class Decomposition:
    n_repeats: int
    cases: list[CaseDecomposition] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def mean_judge_only_flip_rate(self) -> float:
        if not self.cases:
            return 0.0
        return round(sum(c.judge_only_flip_rate for c in self.cases) / len(self.cases), 4)

    @property
    def mean_full_flip_rate(self) -> float:
        if not self.cases:
            return 0.0
        return round(sum(c.full_flip_rate for c in self.cases) / len(self.cases), 4)

    @property
    def n_flipped_judge_only(self) -> int:
        return sum(1 for c in self.cases if c.judge_only_flip_rate > 0)

    @property
    def n_flipped_full(self) -> int:
        return sum(1 for c in self.cases if c.full_flip_rate > 0)

    @property
    def overall_judge_share(self) -> float | None:
        """Share of the suite's mean flip rate the judge alone reproduces."""
        full = self.mean_full_flip_rate
        if full == 0:
            return None
        return round(min(1.0, self.mean_judge_only_flip_rate / full), 4)

    def to_dict(self) -> dict[str, object]:
        return {
            "n_repeats": self.n_repeats,
            "n_cases": len(self.cases),
            "mean_judge_only_flip_rate": self.mean_judge_only_flip_rate,
            "mean_full_flip_rate": self.mean_full_flip_rate,
            "n_flipped_judge_only": self.n_flipped_judge_only,
            "n_flipped_full": self.n_flipped_full,
            "overall_judge_share": self.overall_judge_share,
            "cases": [c.to_dict() for c in self.cases],
        }


def run_judge_only_arm(
    dataset: Dataset,
    target: Target,
    scorer: Scorer,
    n_repeats: int,
) -> dict[str, list[int]]:
    """Sample the target once per case, then judge that one response N times."""
    verdicts: dict[str, list[int]] = {}
    for case in dataset.cases:
        # The single target call lives in slot 0; each judging gets its own slot, so the
        # N judge calls are distinct cassette entries even though the request is
        # byte-identical every time. That is the whole point of the arm.
        with slot(0):
            response = target.generate(case.prompt)
        per_case: list[int] = []
        for k in range(n_repeats):
            with slot(k):
                result = scorer.score(case.prompt, response.text, case.expected)
            per_case.append(int(result.score >= 0.5))
        verdicts[case.case_id] = per_case
    return verdicts


def run_full_arm(
    dataset: Dataset,
    target: Target,
    scorer: Scorer,
    n_repeats: int,
) -> dict[str, list[int]]:
    """Sample the target N times and judge each response once: an ordinary run."""
    verdicts: dict[str, list[int]] = {}
    for case in dataset.cases:
        per_case: list[int] = []
        for k in range(n_repeats):
            with slot(k):
                response = target.generate(case.prompt)
                result = scorer.score(case.prompt, response.text, case.expected)
            per_case.append(int(result.score >= 0.5))
        verdicts[case.case_id] = per_case
    return verdicts


def decompose(
    dataset: Dataset,
    judge_only_target: Target,
    judge_only_scorer: Scorer,
    full_target: Target,
    full_scorer: Scorer,
    n_repeats: int = 5,
) -> Decomposition:
    """Run both arms and pair them by case.

    The two arms take separate target/scorer objects because each carries its own
    cassette. One cassette shared between the arms would let them collide whenever the
    target's first response equals a later one, coupling two measurements the
    experiment needs to keep apart.
    """
    if n_repeats < 2:
        raise ValueError("decompose needs n_repeats >= 2 to see a flip at all")
    started = time.perf_counter()
    judge_only = run_judge_only_arm(dataset, judge_only_target, judge_only_scorer, n_repeats)
    full = run_full_arm(dataset, full_target, full_scorer, n_repeats)

    cases = [
        CaseDecomposition(
            case_id=case.case_id,
            judge_only_verdicts=judge_only[case.case_id],
            full_verdicts=full[case.case_id],
        )
        for case in dataset.cases
    ]
    return Decomposition(
        n_repeats=n_repeats, cases=cases, seconds=time.perf_counter() - started)


def render(result: Decomposition) -> str:
    """A Markdown summary that states the confound this experiment removes."""
    lines = [
        "# Variance decomposition\n",
        f"{len(result.cases)} case(s), N={result.n_repeats} per arm.\n",
        "| arm | what varies | mean flip rate | cases that flipped |",
        "|---|---|---|---|",
        f"| judge_only | the judge only (one fixed target response) | "
        f"{result.mean_judge_only_flip_rate:.1%} | {result.n_flipped_judge_only} |",
        f"| target_and_judge | the target and the judge | "
        f"{result.mean_full_flip_rate:.1%} | {result.n_flipped_full} |",
        "",
    ]
    share = result.overall_judge_share
    if share is None:
        lines.append(
            "Neither arm flipped, so there is no variance here to apportion. That is a "
            "statement about this suite at this N, not about judges in general."
        )
    else:
        lines.append(
            f"The judge alone reproduces **{share:.0%}** of the suite's mean flip rate "
            "while the target's response is held fixed."
        )
    lines.append("")
    lines.append("| case | judge_only | target_and_judge | judge share |")
    lines.append("|---|---|---|---|")
    for c in sorted(result.cases, key=lambda c: (-c.full_flip_rate, c.case_id)):
        if c.judge_only_flip_rate == 0 and c.full_flip_rate == 0:
            continue
        share_txt = "-" if c.judge_share is None else f"{c.judge_share:.0%}"
        lines.append(
            f"| {c.case_id} | {c.judge_only_flip_rate:.0%} | "
            f"{c.full_flip_rate:.0%} | {share_txt} |"
        )
    lines.append("")
    lines.append(
        "Read `judge_only` as a lower bound on the judge's contribution: it measures "
        "the judge's variance at one particular response, which may be easier or harder "
        "to grade consistently than the responses the target would give on other draws."
    )
    return "\n".join(lines)
