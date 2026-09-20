from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element, SubElement, tostring

from .models import CaseResult, RunRecord

if TYPE_CHECKING:
    from .diffing import DiffResult


# Every artifact is written UTF-8 explicitly. Path.write_text defaults to the locale
# encoding, which on a Windows console is cp1252 and cannot encode the "·" and "⚠" this
# report uses — that crashed `evalseal run` on Windows while passing everywhere else.
def write_json(record: RunRecord, path: str | Path = "report.json") -> None:
    Path(path).write_text(record.model_dump_json(indent=2), encoding="utf-8")


def verdict_sequence(case: CaseResult) -> str:
    """`PPFP` — the observed verdicts in run order, so a reader sees when it flipped."""
    if case.verdicts:
        return "".join("P" if v else "F" for v in case.verdicts)
    return "".join("P" if s >= 0.5 else "F" for s in case.scores)


def case_rows(record: RunRecord, unstable_only: bool = False) -> list[CaseResult]:
    """Cases for display: unstable first, then by case id."""
    rows = [c for c in record.results if not unstable_only or c.flip_count > 0]
    return sorted(rows, key=lambda c: (-c.flip_rate, c.case_id))


def to_case_table(record: RunRecord, unstable_only: bool = False) -> str:
    """The per-case view: which cases are unstable, and under which judge."""
    rows = case_rows(record, unstable_only)
    if not rows:
        return "No unstable cases." if unstable_only else "No cases."
    prompt_hash = (record.manifest.scorer.judge_prompt_hash or "-")[:14]
    header = ("| case_id | runs | pass | fail | flips | flip_rate | majority | "
              "stability | judge_prompt_hash |")
    lines = [header, "|---|---|---|---|---|---|---|---|---|"]
    for c in rows:
        majority = "-" if c.majority_verdict is None else ("PASS" if c.majority_verdict else "FAIL")
        lines.append(
            f"| {c.case_id} | {len(c.scores)} | {c.pass_count} | {c.fail_count} | "
            f"{c.flip_count} | {c.flip_rate:.0%} | {majority} | {c.stability_label} | "
            f"`{prompt_hash}` |"
        )
    return "\n".join(lines)


def to_markdown(record: RunRecord) -> str:
    a = record.aggregate
    t = record.manifest.target
    judge = record.manifest.scorer.judge
    lines = [
        "# EvalSeal run\n",
        f"- Model requested: `{t.requested_model}`",
        f"- Served: `{t.served_model}`  |  fingerprint: `{t.system_fingerprint}`",
        f"- Temperature: `{t.effective_params.temperature}` ({t.params_source})",
        f"- Scorer: `{record.manifest.scorer.type}`"
        + (f"  |  judge: `{judge.served_model or judge.requested_model}`"
           f", temperature `{judge.effective_params.temperature}` ({judge.params_source})"
           if judge else ""),
        f"- N repeats: {record.manifest.run_config.n_repeats}",
        f"- Dataset: `{record.manifest.dataset.hash[:20]}...` "
        f"({record.manifest.dataset.n_cases} cases)",
        f"- Sealed hash: `{record.hash[:20]}...`\n",
        f"**Aggregate:** {a.n_cases} cases · mean {a.mean_score:.2f} · "
        f"{a.n_stable} stable / {a.n_borderline} borderline / {a.n_unstable} unstable\n",
    ]
    if a.warnings:
        lines.append("## ⚠ Provenance warnings")
        lines += [f"- {w}" for w in a.warnings]
        lines.append("")
    lines.append("## Per-case reproducibility\n")
    lines.append("| case | verdicts | mean | 95% CI | flip rate | stability |")
    lines.append("|---|---|---|---|---|---|")
    for r in record.results:
        verdicts = "".join("P" if s >= 0.5 else "F" for s in r.scores)
        lines.append(
            f"| {r.case_id} | `{verdicts}` | {r.mean:.2f} | [{r.ci95[0]:.2f}, {r.ci95[1]:.2f}] "
            f"| {r.flip_rate:.0%} | {r.stability} |"
        )
    return "\n".join(lines) + "\n"


def write_markdown(record: RunRecord, path: str | Path = "report.md") -> None:
    Path(path).write_text(to_markdown(record), encoding="utf-8")


# Stability classes that count as a failure under each policy.
_FAILING: dict[str, set[str]] = {
    "none": set(),
    "unstable": {"UNSTABLE"},
    "borderline": {"UNSTABLE", "BORDERLINE"},
}


def failing_cases(record: RunRecord, fail_on: str) -> list[CaseResult]:
    """Cases that violate the policy. `none` never fails, so a run can report only."""
    return [r for r in record.results if r.stability in _FAILING[fail_on]]


def to_junit(record: RunRecord, fail_on: str = "unstable") -> str:
    """JUnit XML so CI systems show per-case reproducibility next to ordinary tests."""
    failing = {r.case_id for r in failing_cases(record, fail_on)}
    total_seconds = sum(r.seconds for r in record.results)
    suite = Element("testsuite", {
        "name": "evalseal",
        "tests": str(len(record.results)),
        "failures": str(len(failing)),
        "errors": "0",
        "skipped": "0",
        "time": f"{total_seconds:.3f}",
        "timestamp": record.created_at,
    })
    props = SubElement(suite, "properties")
    for key, value in {
        "model": record.manifest.target.requested_model,
        "served_model": record.manifest.target.served_model or "",
        "n_repeats": str(record.manifest.run_config.n_repeats),
        "dataset_hash": record.manifest.dataset.hash,
        "sealed_hash": record.hash,
    }.items():
        SubElement(props, "property", {"name": key, "value": value})

    for r in record.results:
        verdicts = "".join("P" if s >= 0.5 else "F" for s in r.scores)
        case = SubElement(suite, "testcase", {
            "classname": "evalseal.reproducibility",
            "name": r.case_id,
            "time": f"{r.seconds:.3f}",
        })
        detail = (
            f"verdicts {verdicts} · mean {r.mean:.2f} · "
            f"95% CI [{r.ci95[0]:.2f}, {r.ci95[1]:.2f}] · flip rate {r.flip_rate:.0%}"
        )
        if r.case_id in failing:
            SubElement(case, "failure", {
                "message": f"{r.stability}: flip rate {r.flip_rate:.0%}",
                "type": r.stability,
            }).text = detail
        else:
            SubElement(case, "system-out").text = f"{r.stability} · {detail}"

    if record.aggregate.warnings:
        SubElement(suite, "system-out").text = "\n".join(record.aggregate.warnings)

    suites = Element("testsuites", {
        "name": "evalseal",
        "tests": str(len(record.results)),
        "failures": str(len(failing)),
        "time": f"{total_seconds:.3f}",
    })
    suites.append(suite)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(suites, encoding="unicode")


def write_junit(record: RunRecord, path: str | Path, fail_on: str = "unstable") -> None:
    Path(path).write_text(to_junit(record, fail_on), encoding="utf-8")


def render_diff(result: DiffResult) -> str:
    """Human-readable drift report. Comparability comes first, deliberately:
    a score delta is meaningless until you know the two runs measured the same thing."""
    lines: list[str] = []

    if result.comparable:
        lines.append("**Comparable:** yes. Same grading setup, so the scores mean the "
                     "same thing.")
    else:
        lines.append("**Not directly comparable: evaluator configuration changed.**")
        lines.append("")
        lines.append("| what changed | before | after |")
        lines.append("|---|---|---|")
        for change in result.config_changes:
            lines.append(f"| {change.name} | `{change.before}` | `{change.after}` |")
    lines.append("")

    arrow = "no change" if result.score_delta == 0 else f"{result.score_delta:+.3f}"
    lines.append("| metric | before | after | change |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| score | {result.score_before:.3f} | {result.score_after:.3f} | "
        f"{arrow} ({result.score_verdict}, noise floor ±{result.noise_floor:.3f}) |"
    )
    # One decimal: at whole percents a 1.3% -> 0.7% move renders as "1% 1% -1%".
    lines.append(
        f"| mean flip rate | {result.flip_rate_before:.1%} | {result.flip_rate_after:.1%} | "
        f"{result.flip_rate_delta:+.1%} |"
    )
    lines.append(
        f"| cases that flipped | {len(result.unstable_before)} | "
        f"{len(result.unstable_after)} | "
        f"{len(result.unstable_after) - len(result.unstable_before):+d} |"
    )
    lines.append("")

    for label, cases in (
        ("Newly unstable", result.newly_unstable),
        ("Now stable", result.now_stable),
        ("Still unstable", result.still_unstable),
        ("Cases added", result.cases_added),
        ("Cases removed", result.cases_removed),
    ):
        if cases:
            lines.append(f"- **{label}:** {', '.join(cases)}")

    changed_provenance = [c for c in result.provenance if c.changed]
    if changed_provenance:
        lines.append("")
        lines.append("Other differences: " + ", ".join(
            f"{c.name} `{c.before}` to `{c.after}`" for c in changed_provenance
        ))
    return "\n".join(lines)
