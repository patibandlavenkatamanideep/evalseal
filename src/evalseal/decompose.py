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

The arms are not a variance decomposition, and this module no longer uses the word as
if they were. Two flip rates measured under two designs are two flip rates. Their ratio
is not a share of variance, not a lower bound on one, and not a quantity with a
confidence interval - saying any of those would require a model of how the judge's
disagreement varies across the target's output distribution, and this experiment
supplies none.

What the arms do support is a comparison: *how much of the flipping that a full run
shows can be reproduced with the target's response held fixed?* That is reported as
`judge_only_flip_rate` beside `full_flip_rate`, with the ratio named
`judge_only_over_full` and left unclipped, because a value above 1 is information and
rounding it to "100%" destroys it.

Judging one fixed response N times measures the judge's disagreement *at that response*,
and a response the target happened to produce once may be easier or harder to grade
consistently than the ones it would produce on other draws.

**Two findings this wording replaces, both reproduced on 2.2.0:**

*D2-3.* With `judge_only` flipping on both cases and `full` flipping on neither, the
report said *"Neither arm flipped, so there is no variance here to apportion"* - a
plainly false statement about the data in front of it, produced because the ratio's
denominator was zero and `None` was rendered as "nothing happened".

*D2-4.* With a judge_only flip rate of 0.4 against a full rate of 0.2, the ratio 2.0
was clipped to 1.0 and rendered as *"The judge alone reproduces 100% of the suite's mean
flip rate"*. The one case where the arms contradict the apportioning model was the one
case the model reported as a perfect fit.

Each arm records to its own cassette. Sharing one would let the arms collide whenever
the target's first response happens to equal a later one, which would couple two
measurements that the experiment needs to keep separate.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .adapters.dataset import Dataset
from .adapters.recording import unit as recording_unit
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
    def judge_only_over_full(self) -> float | None:
        """`judge_only_flip_rate / full_flip_rate`, **unclipped**, or None at 0/0.

        A ratio of two measured flip rates, not a share of anything. Above 1 means the
        fixed-response arm disagreed with itself *more* than the full arm did, which is
        possible and worth seeing: it says the apportioning intuition does not hold for
        this case. Clipping it to 1.0 and calling it "100% of the variance" reported a
        contradiction as a perfect fit.

        None only when neither arm flipped, where there is no ratio to take. A case
        where the judge flipped and the full arm did not has an infinite ratio, which
        is reported as `None` here and named explicitly in `comparison`.
        """
        full = self.full_flip_rate
        if full == 0:
            return None
        return round(self.judge_only_flip_rate / full, 4)

    @property
    def comparison(self) -> str:
        """What the two arms show for this case, in words that claim only that."""
        judge, full = self.judge_only_flip_rate, self.full_flip_rate
        if judge == 0 and full == 0:
            return "neither arm flipped"
        if full == 0:
            return ("only the fixed-response arm flipped: the judge disagreed with "
                    "itself on one unchanging response while the full run did not")
        if judge == 0:
            return ("only the full arm flipped: the judge was self-consistent on the "
                    "fixed response, so the flipping did not come from re-judging")
        if judge > full:
            return ("the fixed-response arm flipped more than the full run, so the "
                    "flipping cannot be apportioned between them")
        return "both arms flipped, the fixed-response arm less"

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "judge_only_verdicts": self.judge_only_verdicts,
            "full_verdicts": self.full_verdicts,
            "judge_only_flip_rate": round(self.judge_only_flip_rate, 4),
            "full_flip_rate": round(self.full_flip_rate, 4),
            "judge_only_over_full": self.judge_only_over_full,
            "comparison": self.comparison,
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
    def overall_judge_only_over_full(self) -> float | None:
        """The suite's two mean flip rates as a ratio, unclipped, or None at 0/0."""
        full = self.mean_full_flip_rate
        if full == 0:
            return None
        return round(self.mean_judge_only_flip_rate / full, 4)

    @property
    def n_judge_only_above_full(self) -> int:
        """Cases where the fixed-response arm flipped more than the full run.

        The count that says how often the apportioning intuition fails on this suite.
        """
        return sum(1 for c in self.cases
                   if c.judge_only_flip_rate > c.full_flip_rate)

    def to_dict(self) -> dict[str, object]:
        return {
            "n_repeats": self.n_repeats,
            "n_cases": len(self.cases),
            "mean_judge_only_flip_rate": self.mean_judge_only_flip_rate,
            "mean_full_flip_rate": self.mean_full_flip_rate,
            "n_flipped_judge_only": self.n_flipped_judge_only,
            "n_flipped_full": self.n_flipped_full,
            "overall_judge_only_over_full": self.overall_judge_only_over_full,
            "n_judge_only_above_full": self.n_judge_only_above_full,
            "cases": [c.to_dict() for c in self.cases],
        }


def _judge_only_case(case, target: Target, scorer: Scorer, n_repeats: int) -> list[int]:
    """One case of the judge-only arm: sample once, then judge that response N times.

    The target call and the judgings of it cannot overlap - there is nothing to judge
    until the response exists - so the parallelism in this arm is across cases, never
    within one. Each unit is entered inside the worker, because a contextvar does not
    cross into a thread.
    """
    # The single target call is repeat 0; each judging gets its own repeat, so the N
    # judge calls are distinct cassette entries even though the request is
    # byte-identical every time. That is the whole point of the arm. Both are named
    # with this case's id, so a case that shares a prompt with another still gets its
    # own draws rather than inheriting that one's.
    with recording_unit(case.case_id, 0, "target"):
        response = target.generate(case.prompt)
    verdicts: list[int] = []
    for k in range(n_repeats):
        with recording_unit(case.case_id, k, "judge"):
            result = scorer.score(case.prompt, response.text, case.expected)
        verdicts.append(int(result.score >= 0.5))
    return verdicts


def run_judge_only_arm(
    dataset: Dataset,
    target: Target,
    scorer: Scorer,
    n_repeats: int,
    concurrency: int = 1,
) -> dict[str, list[int]]:
    """Sample the target once per case, then judge that one response N times."""
    cases = list(dataset.cases)
    if concurrency == 1:
        per_case = [_judge_only_case(c, target, scorer, n_repeats) for c in cases]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # list() re-raises the first failure and preserves input order, so results
            # are assembled by position rather than by which worker finished first.
            per_case = list(pool.map(
                lambda c: _judge_only_case(c, target, scorer, n_repeats), cases))
    return {c.case_id: v for c, v in zip(cases, per_case, strict=True)}


def _full_unit(case, repeat: int, target: Target, scorer: Scorer) -> int:
    """One (case, repeat) of the full arm: generate once, judge that response once."""
    with recording_unit(case.case_id, repeat, "target"):
        response = target.generate(case.prompt)
    with recording_unit(case.case_id, repeat, "judge"):
        result = scorer.score(case.prompt, response.text, case.expected)
    return int(result.score >= 0.5)


def run_full_arm(
    dataset: Dataset,
    target: Target,
    scorer: Scorer,
    n_repeats: int,
    concurrency: int = 1,
) -> dict[str, list[int]]:
    """Sample the target N times and judge each response once: an ordinary run."""
    cases = list(dataset.cases)
    units = [(c, k) for c in cases for k in range(n_repeats)]
    if concurrency == 1:
        flat = [_full_unit(c, k, target, scorer) for c, k in units]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            flat = list(pool.map(lambda u: _full_unit(u[0], u[1], target, scorer), units))
    return {
        c.case_id: flat[i * n_repeats:(i + 1) * n_repeats]
        for i, c in enumerate(cases)
    }


def decompose(
    dataset: Dataset,
    judge_only_target: Target,
    judge_only_scorer: Scorer,
    full_target: Target,
    full_scorer: Scorer,
    n_repeats: int = 5,
    concurrency: int = 1,
) -> Decomposition:
    """Run both arms and pair them by case.

    The two arms take separate target/scorer objects because each carries its own
    cassette. One cassette shared between the arms would let them collide whenever the
    target's first response equals a later one, coupling two measurements the
    experiment needs to keep apart.

    `concurrency` changes how long this takes and nothing else. Every cassette entry is
    keyed by (request, repeat) rather than by arrival order, and results are assembled by
    position, so the verdicts are identical at any width. The arms still run one after
    the other: they are separate experiments, and overlapping them would only compete for
    the same rate limit.
    """
    if n_repeats < 2:
        raise ValueError("decompose needs n_repeats >= 2 to see a flip at all")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    started = time.perf_counter()
    judge_only = run_judge_only_arm(
        dataset, judge_only_target, judge_only_scorer, n_repeats, concurrency)
    full = run_full_arm(dataset, full_target, full_scorer, n_repeats, concurrency)

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
    """A Markdown summary that states the confound this experiment removes.

    It reports the two arms and compares them. It does not apportion variance between
    them, because the design does not support apportioning: see the module docstring.
    """
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
    judge, full = result.mean_judge_only_flip_rate, result.mean_full_flip_rate
    ratio = result.overall_judge_only_over_full
    if judge == 0 and full == 0:
        lines.append(
            "Neither arm flipped, so there is nothing here to compare. That is a "
            "statement about this suite at this N, not about judges in general."
        )
    elif full == 0:
        # D2-3: this said "neither arm flipped", which was false about its own data.
        lines.append(
            f"**Only the fixed-response arm flipped.** The judge disagreed with itself "
            f"on {result.n_flipped_judge_only} case(s) whose target response never "
            f"changed (mean flip rate {judge:.1%}), while the full run returned the "
            f"same majority verdict everywhere. The full run's stability is therefore "
            "not evidence that the judge is stable - at this N it did not happen to "
            "land on the disagreement, and a re-run may."
        )
    elif judge > full:
        # D2-4: this was clipped to 1.0 and read as "100% of the variance".
        lines.append(
            f"**The fixed-response arm flipped more than the full run** "
            f"({judge:.1%} against {full:.1%}, a ratio of {ratio:.2f}). The flipping "
            "cannot be apportioned between the judge and the target here: holding the "
            "response fixed produced *more* disagreement, not less, so the two arms are "
            "measuring the judge at different responses rather than nesting one inside "
            "the other."
        )
    else:
        lines.append(
            f"With the target's response held fixed the judge still flipped at "
            f"{judge:.1%}, against {full:.1%} when the target varied too "
            f"(a ratio of {ratio:.2f}). Read that as a comparison of two measured flip "
            "rates, not as a share of the variance: the fixed response is one draw out "
            "of the target's distribution, and the judge may be more or less "
            "self-consistent on the others."
        )
    if result.n_judge_only_above_full:
        lines.append("")
        lines.append(
            f"{result.n_judge_only_above_full} case(s) flipped more with the response "
            "held fixed than with it varying, which is where the apportioning "
            "intuition fails."
        )
    lines.append("")
    lines.append("| case | judge_only | target_and_judge | ratio | what the arms show |")
    lines.append("|---|---|---|---|---|")
    for c in sorted(result.cases,
                    key=lambda c: (-max(c.full_flip_rate, c.judge_only_flip_rate),
                                   c.case_id)):
        if c.judge_only_flip_rate == 0 and c.full_flip_rate == 0:
            continue
        ratio = c.judge_only_over_full
        lines.append(
            f"| {c.case_id} | {c.judge_only_flip_rate:.0%} | "
            f"{c.full_flip_rate:.0%} | "
            + ("-" if ratio is None else f"{ratio:.2f}")
            + f" | {c.comparison} |"
        )
    lines.append("")
    lines.append(
        "`judge_only` measures the judge's disagreement at one particular response, "
        "which may be easier or harder to grade consistently than the responses the "
        "target would give on other draws. The ratio is two measured flip rates "
        "divided, and nothing more: this design does not support apportioning variance "
        "between the judge and the target, so no number here is a share of one."
    )
    return "\n".join(lines)
