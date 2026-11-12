"""Input validation, so a number that cannot be a measurement is never read as one.

## Why this module exists

The receipt models carried no constraints: no bounds, no finiteness, and no check that
a summary agreed with the cases it summarises. Invalid data therefore did not fail a
gate - it *passed* it, because the comparison a gate makes is false for a value that is
not a number, and absent for a summary nobody cross-checks:

- `aggregate.mean_score = 42.0` cleared `--min-score 0.9`, because 42.0 really is
  greater than 0.9. Nothing asked whether a mean score of 42 is a mean score.
- `aggregate.n_cases = 999` on a two-case run with `mean_score = 1.0` cleared
  `--min-score 0.95`. The chain verified, because the fabricated numbers were sealed
  along with everything else; sealing attests that nobody changed them afterwards, not
  that they were ever true.
- a non-finite score was serialised as `null` by pydantic and the sealed receipt then
  failed to load at all, so an invalid measurement became an unreadable file and a
  raw validation error rather than a named failure.

JSON adds two of its own. A duplicate member is resolved silently in favour of the
last one, so `{"min_score": 0.9, "min_score": 0.0}` in a committed policy is read as
`0.9` by the reviewer who approves it and enforced as `0.0` by the gate - no tampering
and no broken hash required, because nothing hashes a policy file. And Python's `json`
accepts `NaN`, `Infinity` and `-Infinity`, which are not JSON at all, which is how a
non-finite value gets in from outside in the first place.

## The rule

A measurement is a finite number in its declared range, a summary agrees with what it
summarises, and a case set is non-empty with distinct ids. Anything else is an *input
error*: reported by name, with the documented exit code, and never as a score.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import RunRecord

#: How far a recomputed summary may differ from the stored one before it is a problem.
#: Loose enough for float accumulation over a large case set, far tighter than any
#: difference that would change a verdict.
TOLERANCE = 1e-6


class InvalidInput(ValueError):
    """The input is not a well-formed EvalSeal file, as opposed to failing a check.

    A `ValueError` so that callers already catching that keep working, and a distinct
    type so a command can report it as an input error with the documented exit code
    instead of letting a traceback reach the user.
    """


@dataclass(frozen=True)
class Problem:
    """One data invariant that does not hold, named by the field it is about."""

    name: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "detail": self.detail}


# --------------------------------------------------------------------------------------
# Strict JSON
# --------------------------------------------------------------------------------------


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise InvalidInput(
                f"duplicate member {key!r}: the file sets it twice, and JSON resolves "
                "that silently in favour of the last one. A reader reviewing the first "
                "value and a tool enforcing the second would disagree without either "
                "being wrong, so this is refused rather than guessed at")
        seen[key] = value
    return seen


def _reject_constant(name: str) -> Any:
    raise InvalidInput(
        f"{name} is not a finite number, and not JSON either - the specification has no "
        f"{name} literal. A value that is not a number cannot be compared against a "
        "threshold: every comparison against it is false, so it would clear any limit "
        "it was tested against")


def strict_json_loads(text: str, *, what: str) -> Any:
    """Parse JSON, refusing duplicate members and non-finite literals.

    `what` names the kind of file, for the message. Raises `InvalidInput` for anything
    malformed, so one caller can catch one type.
    """
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates,
                          parse_constant=_reject_constant)
    except InvalidInput as e:
        raise InvalidInput(f"{what}: {e}") from None
    except json.JSONDecodeError as e:
        raise InvalidInput(f"{what} is not valid JSON: {e}") from None


# --------------------------------------------------------------------------------------
# Record invariants
# --------------------------------------------------------------------------------------


def _finite_in_range(
    value: float, name: str, *, low: float = 0.0, high: float = 1.0,
) -> Problem | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return Problem(name, f"{value!r} is not a number")
    if not math.isfinite(value):
        return Problem(name, f"{value!r} is not finite, so it is not a measurement")
    if not low <= value <= high:
        return Problem(name, f"{value!r} is outside the valid range [{low}, {high}]")
    return None


def validate_record(record: RunRecord) -> list[Problem]:
    """Every data invariant that does not hold for this record.

    An empty list means the numbers are internally consistent and in range. It does not
    mean they are true: that is what the hash, the signature and the anchor are for, and
    none of them would notice a fabricated summary, because a fabricated summary gets
    sealed along with everything else.
    """
    problems: list[Problem] = []
    results = record.results

    if not results:
        problems.append(Problem(
            "results", "the receipt carries no cases, so it measured nothing. A gate "
                       "cannot be satisfied by a run that did not happen"))

    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for index, case in enumerate(results):
        if case.case_id in seen:
            duplicates.append(case.case_id)
        else:
            seen[case.case_id] = index
    if duplicates:
        problems.append(Problem(
            "results.case_id",
            f"{len(duplicates)} duplicate case id(s): {', '.join(sorted(set(duplicates)))}. "
            "A per-case threshold and a per-case comparison both address a case by id, "
            "so two cases sharing one makes which was checked undecidable"))

    for index, case in enumerate(results):
        for position, score in enumerate(case.scores):
            problem = _finite_in_range(score, f"results[{index}].scores[{position}]")
            if problem is not None:
                problems.append(problem)
        for field in ("mean", "flip_rate"):
            problem = _finite_in_range(getattr(case, field), f"results[{index}].{field}")
            if problem is not None:
                problems.append(problem)
        problems.extend(_case_summary_problems(index, case))

    problems.extend(_aggregate_problems(record))
    problems.extend(_execution_problems(record))
    return problems


def _execution_problems(record: Any) -> list[Problem]:
    """Whether the execution manifest agrees with the measurements it describes.

    A per-call manifest is only worth sealing if a reader can trust the binding. Two
    calls claiming one (case, repeat, role) makes "what did repeat 1 of c0 return"
    undecidable; a call naming a case the receipt does not have, or a repeat the run
    never had, describes a measurement that is not in this receipt. Either way the
    detail would read as evidence about a unit it does not belong to.

    The summary counts are checked against the calls for the same reason the aggregate
    is checked against the cases: a summary nobody cross-checks is a summary anybody
    can write.
    """
    execution = getattr(record.manifest, "execution", None)
    if execution is None:
        # A receipt sealed before schema 1.7, or one built without execution detail.
        # Absent is not inconsistent; what it cannot be is read as "nothing happened".
        return []

    problems: list[Problem] = []
    calls = execution.calls
    repeats_by_case = {case.case_id: len(case.scores) for case in record.results}

    seen: set[tuple[str, int, str, int]] = set()
    duplicates: list[str] = []
    for index, call in enumerate(calls):
        key = (call.case_id, call.repeat, call.role, call.sequence)
        if key in seen:
            duplicates.append(f"{call.case_id} repeat {call.repeat} {call.role}")
        seen.add(key)

        if call.case_id not in repeats_by_case:
            problems.append(Problem(
                f"manifest.execution.calls[{index}].case_id",
                f"the call is attributed to case {call.case_id!r}, which this receipt "
                "does not contain, so it describes a measurement that is not here"))
        elif not 0 <= call.repeat < repeats_by_case[call.case_id]:
            problems.append(Problem(
                f"manifest.execution.calls[{index}].repeat",
                f"repeat {call.repeat} is outside the {repeats_by_case[call.case_id]} "
                f"repeat(s) recorded for case {call.case_id!r}"))
        if call.sequence < 0 or call.attempts < 0:
            problems.append(Problem(
                f"manifest.execution.calls[{index}]",
                f"sequence {call.sequence} and attempts {call.attempts} count "
                "occurrences; neither can be negative"))

    if duplicates:
        problems.append(Problem(
            "manifest.execution.calls",
            f"{len(duplicates)} call(s) claim a repeat another call already claims "
            f"({', '.join(sorted(set(duplicates)))}). Which one produced that score is "
            "then undecidable"))

    # Every score needs the call that produced it, and every call a score to belong to.
    # Checked even when the call list is empty: a manifest that says a run happened and
    # lists nothing is the same false claim as one that lists the wrong thing. An
    # *absent* manifest is a different matter and returns early above.
    expected = {(case.case_id, repeat)
                for case in record.results for repeat in range(len(case.scores))}
    measured = {(c.case_id, c.repeat) for c in calls if c.role == "target"}
    if measured != expected:
        missing = len(expected - measured)
        extra = len(measured - expected)
        problems.append(Problem(
            "manifest.execution.calls",
            f"the target calls do not cover the scores: {missing} scored "
            f"(case, repeat) pair(s) have no call and {extra} call(s) have no score. "
            "A score without the call behind it cannot be traced to anything"))

    for field, actual in (
        ("n_calls", len(calls)),
        ("n_live", sum(1 for c in calls if c.origin == "live")),
        ("n_replayed", sum(1 for c in calls if c.origin == "replay")),
        ("attempts", sum(c.attempts for c in calls)),
        ("n_truncated", sum(1 for c in calls if c.status == "truncated")),
    ):
        stated = getattr(execution, field)
        if stated != actual:
            problems.append(Problem(
                f"manifest.execution.{field}",
                f"the summary says {stated} but the sealed calls say {actual}"))

    origins = {c.origin for c in calls}
    if not calls:
        expected_origin = execution.origin
    else:
        expected_origin = origins.pop() if len(origins) == 1 else "mixed"
    if execution.origin != expected_origin:
        problems.append(Problem(
            "manifest.execution.origin",
            f"the run claims origin {execution.origin!r} but its calls say "
            f"{expected_origin!r}. Whether a provider was called is the strongest "
            "claim in a receipt and cannot disagree with the calls behind it"))
    return problems


def _case_summary_problems(index: int, case: Any) -> list[Problem]:
    problems: list[Problem] = []
    low, high = case.ci95
    for name, bound in (("ci95[0]", low), ("ci95[1]", high)):
        problem = _finite_in_range(bound, f"results[{index}].{name}")
        if problem is not None:
            problems.append(problem)
    if (math.isfinite(low) and math.isfinite(high) and low > high):
        problems.append(Problem(
            f"results[{index}].ci95",
            f"the interval is inverted: [{low}, {high}]"))

    if case.scores and all(
            isinstance(s, (int, float)) and math.isfinite(s) for s in case.scores):
        expected = sum(case.scores) / len(case.scores)
        if math.isfinite(case.mean) and abs(case.mean - expected) > TOLERANCE:
            problems.append(Problem(
                f"results[{index}].mean",
                f"{case.mean} is stored, but the {len(case.scores)} score(s) in this "
                f"case average {expected}"))

    counted = case.pass_count + case.fail_count + case.other_count
    if case.verdicts and counted != len(case.verdicts):
        problems.append(Problem(
            f"results[{index}].verdict_counts",
            f"pass+fail+other is {counted}, but {len(case.verdicts)} verdict(s) were "
            "recorded"))
    if case.flip_count > len(case.scores):
        problems.append(Problem(
            f"results[{index}].flip_count",
            f"{case.flip_count} flips in {len(case.scores)} run(s)"))
    return problems


def _aggregate_problems(record: RunRecord) -> list[Problem]:
    problems: list[Problem] = []
    aggregate, results = record.aggregate, record.results

    problem = _finite_in_range(aggregate.mean_score, "aggregate.mean_score")
    if problem is not None:
        problems.append(problem)

    if aggregate.n_cases != len(results):
        problems.append(Problem(
            "aggregate.n_cases",
            f"the summary claims {aggregate.n_cases} case(s) and the receipt carries "
            f"{len(results)}. A gate reading the summary would be answering about a run "
            "this receipt does not describe"))

    counts = aggregate.n_stable + aggregate.n_borderline + aggregate.n_unstable
    if counts != len(results):
        problems.append(Problem(
            "aggregate.stability_counts",
            f"stable+borderline+unstable is {counts}, and the receipt carries "
            f"{len(results)} case(s)"))

    # Only when the stored mean is usable and every case mean is, or the comparison
    # would report a disagreement caused by a value already reported above.
    if (results and problem is None
            and all(math.isfinite(c.mean) for c in results)):
        expected = sum(c.mean for c in results) / len(results)
        if abs(aggregate.mean_score - expected) > TOLERANCE:
            problems.append(Problem(
                "aggregate.mean_score",
                f"{aggregate.mean_score} is stored, but the {len(results)} case mean(s) "
                f"average {expected}. Sealing a summary attests that nobody changed it "
                "afterwards, not that it was ever true"))
    return problems


def require_valid(record: RunRecord, *, what: str = "receipt") -> None:
    """Raise `InvalidInput` if any data invariant fails. Used where writing is refused.

    Sealing an invalid record would put an authoritative-looking hash on a number that
    cannot be a measurement, and a non-finite score additionally serialises as `null`,
    making the sealed receipt unloadable - the measurement lost and every later command
    failing on it.
    """
    problems = validate_record(record)
    if problems:
        listed = "; ".join(f"{p.name}: {p.detail}" for p in problems[:5])
        more = "" if len(problems) <= 5 else f" (and {len(problems) - 5} more)"
        raise InvalidInput(f"{what} fails {len(problems)} data invariant(s): {listed}{more}")


__all__ = [
    "InvalidInput",
    "Problem",
    "TOLERANCE",
    "require_valid",
    "strict_json_loads",
    "validate_record",
]
