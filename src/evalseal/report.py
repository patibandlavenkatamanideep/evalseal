from __future__ import annotations

from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from .models import CaseResult, RunRecord


def write_json(record: RunRecord, path: str | Path = "report.json") -> None:
    Path(path).write_text(record.model_dump_json(indent=2))


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
    Path(path).write_text(to_markdown(record))


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
    Path(path).write_text(to_junit(record, fail_on))
