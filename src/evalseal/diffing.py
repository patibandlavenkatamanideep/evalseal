"""Compare two sealed runs and say whether the comparison means anything.

The question this answers is not "did the number move" but "can I compare these two
numbers without fooling myself". A changed rubric or judge prompt moves a score while the
target model sits still, so comparability is reported first and the score second.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .ledger import (
    _case_set_hash,
    evaluator_fingerprint,
    records_share_fingerprint_inputs,
)
from .models import RunRecord
from .paired import INCONCLUSIVE, PairedComparison, compare_runs, per_case_halfwidth

# The verdict when the two runs were not graded the same way. It is not a score result,
# and it is reported before any score so it cannot be read as one.
NON_COMPARABLE = "non_comparable"


@dataclass
class FieldChange:
    """One provenance field that differs between the two runs."""
    name: str
    before: str | None
    after: str | None

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def to_dict(self) -> dict[str, object]:
        return {"field": self.name, "before": self.before, "after": self.after,
                "changed": self.changed}


@dataclass
class DiffResult:
    comparable: bool
    config_changes: list[FieldChange] = field(default_factory=list)
    provenance: list[FieldChange] = field(default_factory=list)

    score_before: float = 0.0
    score_after: float = 0.0
    # The paired item-by-item comparison. This is the evidence; the two means above are
    # just the headline numbers it explains.
    paired: PairedComparison | None = None

    flip_rate_before: float = 0.0
    flip_rate_after: float = 0.0

    unstable_before: list[str] = field(default_factory=list)
    unstable_after: list[str] = field(default_factory=list)
    newly_unstable: list[str] = field(default_factory=list)
    now_stable: list[str] = field(default_factory=list)
    still_unstable: list[str] = field(default_factory=list)

    cases_added: list[str] = field(default_factory=list)
    cases_removed: list[str] = field(default_factory=list)
    # Why the runs are not comparable, in one sentence. Empty when they are.
    reason: str = ""

    @property
    def evaluator_changes(self) -> list[FieldChange]:
        """The subset of config changes that actually affect comparability."""
        return [c for c in self.config_changes if c.name in EVALUATOR_FIELDS]

    @property
    def score_delta(self) -> float:
        return round(self.score_after - self.score_before, 6)

    @property
    def flip_rate_delta(self) -> float:
        return round(self.flip_rate_after - self.flip_rate_before, 6)

    @property
    def per_case_halfwidth(self) -> float:
        """The pre-2.0 "noise floor", under a name that says what it measures.

        It is the widest per-case CI half-width across the two runs: a statement about
        how little N repeats pin down a single item. It was used as a threshold on the
        suite mean until 2.0, which is a different quantity entirely.
        """
        return self.paired.per_case_halfwidth if self.paired else 0.0

    @property
    def score_verdict(self) -> str:
        """What the paired test supports: regression, improvement, or inconclusive.

        Never "no difference". Failing to reach significance says the experiment could
        not tell, which at N=5 is the usual outcome.
        """
        if not self.comparable:
            return NON_COMPARABLE
        return self.paired.verdict if self.paired else INCONCLUSIVE

    def to_dict(self) -> dict[str, object]:
        return {
            "comparable": self.comparable,
            "verdict": self.score_verdict,
            "reason": self.reason,
            "config_changes": [c.to_dict() for c in self.config_changes],
            # The subset that decides comparability. A reader explaining why two runs are
            # not comparable should list these, not every config change: a different
            # target model is a config change that leaves the runs comparable.
            "evaluator_changes": [c.to_dict() for c in self.evaluator_changes],
            "provenance": [c.to_dict() for c in self.provenance],
            "score": {
                "before": self.score_before, "after": self.score_after,
                "delta": self.score_delta,
                "verdict": self.score_verdict,
                "per_case_halfwidth": self.per_case_halfwidth,
            },
            "paired": self.paired.to_dict() if self.paired else None,
            "flip_rate": {
                "before": self.flip_rate_before, "after": self.flip_rate_after,
                "delta": self.flip_rate_delta,
            },
            "unstable_cases": {
                "before": self.unstable_before, "after": self.unstable_after,
                "newly_unstable": self.newly_unstable, "now_stable": self.now_stable,
                "still_unstable": self.still_unstable,
            },
            "cases": {"added": self.cases_added, "removed": self.cases_removed},
        }


class ReceiptError(ValueError):
    """A file was handed to EvalSeal where a sealed receipt was expected.

    Its message has to name the file, say what kind of file it turned out to be, and
    say what to pass instead. A pydantic traceback tells a user that
    `input_value='}'` failed to parse, which describes this module's fallback path
    rather than their mistake.
    """


_RECEIPT_KEYS = {"manifest", "results", "aggregate"}
# `evalseal report --json` writes a summary *of* a receipt, not a receipt. The two are
# easy to confuse because both are called report.json in different docs, so name it.
_REPORT_KEYS = {"summary", "provenance", "cases"}

_WHAT_IS_EXPECTED = (
    "Expected a sealed receipt: the report.json that `evalseal run` writes, or a "
    "ledger (.jsonl) whose head is the receipt."
)


def _describe_shape(data: object, source: str | Path) -> str:
    """Say what the file actually is, in the user's terms rather than pydantic's."""
    if isinstance(data, list):
        return (f"{source} holds a JSON list. {_WHAT_IS_EXPECTED} A list is what a "
                "dataset file looks like.")
    if not isinstance(data, dict):
        return f"{source} holds a bare JSON {type(data).__name__}. {_WHAT_IS_EXPECTED}"
    keys = set(data)
    if keys >= _REPORT_KEYS and "manifest" not in keys:
        return (f"{source}: this looks like report JSON, not a sealed receipt. Use the "
                "receipt JSON produced by `evalseal run` - it is written as report.json "
                f"beside report.md when the run is sealed. {_WHAT_IS_EXPECTED}")
    if "anchor_version" in keys or data.get("kind") == "evalseal.local-anchor":
        return (f"{source} is an anchor, not the receipt it anchors. {_WHAT_IS_EXPECTED} "
                "`anchor-verify` takes both, anchor first.")
    missing = sorted(_RECEIPT_KEYS - keys)
    if missing:
        return (f"{source} is a JSON object but not a sealed receipt: it has no "
                f"{', '.join(missing)}. {_WHAT_IS_EXPECTED}")
    return ""          # shaped like a receipt; the field-level error below is the story


def _parse_one(text: str, source: str | Path) -> RunRecord:
    """Validate one JSON document, translating both failure modes into plain English."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ReceiptError(
            f"{source} is not valid JSON ({e.msg} at line {e.lineno} column {e.colno}). "
            + _WHAT_IS_EXPECTED
        ) from None
    shape = _describe_shape(data, source)
    if shape:
        raise ReceiptError(shape)
    try:
        return RunRecord.model_validate(data)
    except ValidationError as e:
        # Shaped like a receipt and still invalid: the field names are the useful part,
        # so keep them and drop pydantic's urls and input dumps.
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})
        raise ReceiptError(
            f"{source} looks like a receipt but {len(fields)} field(s) are wrong or "
            f"missing: {', '.join(fields[:8])}"
            + (" ..." if len(fields) > 8 else "")
            + ". It may have been written by a newer EvalSeal, or edited by hand."
        ) from None


def load_receipt(source: str | Path) -> RunRecord:
    """Read a sealed record from either a receipt file or a ledger.

    `report.json` is one pretty-printed record; a ledger is one compact record per line.
    Try the whole file first, and only then treat it as JSONL and take the head, so a
    multi-line receipt is not mistaken for a ledger whose last line is a lone brace.

    Every failure raises `ReceiptError` naming the file and what it turned out to be.
    Passing the wrong file is an ordinary mistake - `report --json` output and an anchor
    are both one tab-completion away - and an ordinary mistake must not print a stack.
    """
    path = Path(source)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ReceiptError(f"{source} could not be read: {e.strerror}") from None
    if not text:
        raise ReceiptError(f"{source} is empty. " + _WHAT_IS_EXPECTED)

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        # A ledger is one record per line; a receipt is one pretty-printed record. Try
        # the whole file first so a multi-line receipt is not read as a ledger whose
        # last line is a lone brace, and report the whole-file failure if both fail:
        # for a receipt that is the real error, and for a ledger the last line's own
        # error surfaces instead.
        try:
            return _parse_one(text, source)
        except ReceiptError as whole_file:
            try:
                return _parse_one(lines[-1], source)
            except ReceiptError as last_line:
                raise (last_line if _looks_like_jsonl(lines) else whole_file) from None
    return _parse_one(text, source)


def _looks_like_jsonl(lines: list[str]) -> bool:
    """Whether the file reads as one JSON document per line, as a ledger does."""
    return all(line.lstrip().startswith("{") and line.rstrip().endswith("}")
               for line in lines)


def mean_flip_rate(record: RunRecord) -> float:
    """Mean per-case flip rate. One definition, so every surface prints one number."""
    if not record.results:
        return 0.0
    return round(sum(c.flip_rate for c in record.results) / len(record.results), 4)


def _unstable(record: RunRecord) -> list[str]:
    """Cases whose verdict was not unanimous.

    Keyed on `flip_rate`, not `flip_count`: the count was added in schema 1.2 and
    defaults to 0 on older receipts, which would silently report an old run as having
    nothing unstable in it.
    """
    return sorted(c.case_id for c in record.results if c.flip_rate > 0)


# `per_case_halfwidth` is defined in paired.py, beside the test that took over its old
# role as a threshold. Re-exported here because this is where callers looked for it.
__all__ = ["DiffResult", "FieldChange", "ReceiptError", "diff_records",
           "load_receipt", "mean_flip_rate", "per_case_halfwidth"]


# Which of the fields below decide comparability. Kept beside `_config_fields` so the
# two cannot drift apart: `evaluator_fingerprint` is the authority, this names the same
# ground in a form a reader can be shown.
# Every field the evaluator fingerprint hashes, by the name a reader sees. Kept beside
# `_config_fields` so the two cannot drift: if the fingerprint covers something this set
# does not name, a diff can only say "something changed", which is not an explanation.
EVALUATOR_FIELDS = frozenset({
    "scorer type", "scorer settings", "judge provider", "judge endpoint", "judge model",
    "judge temperature", "judge top_p", "judge max_tokens", "judge seed",
    "judge prompt", "rubric", "dataset", "case set",
})


def _config_fields(record: RunRecord) -> dict[str, str | None]:
    """Every field either fingerprint covers, flattened for display."""
    m = record.manifest
    judge = m.scorer.judge
    jp = judge.effective_params if judge else None
    tp = m.target.effective_params
    return {
        # The instrument.
        "scorer type": m.scorer.type,
        "scorer settings": m.scorer.config_hash,
        "judge provider": judge.provider if judge else None,
        "judge endpoint": judge.base_url if judge else None,
        "judge model": judge.requested_model if judge else None,
        "judge temperature": str(jp.temperature) if jp else None,
        "judge top_p": str(jp.top_p) if jp else None,
        "judge max_tokens": str(jp.max_tokens) if jp else None,
        "judge seed": str(jp.seed) if jp else None,
        "judge prompt": m.scorer.judge_prompt_hash,
        "rubric": m.scorer.rubric_hash,
        "dataset": m.dataset.hash,
        "case set": _case_set_hash(record),
        # The subject under test, and the suite that names it: provenance, not
        # comparability. Listed so a reader sees them, excluded from EVALUATOR_FIELDS.
        "target provider": m.target.provider,
        "target endpoint": m.target.base_url,
        "target model": m.target.requested_model,
        "target temperature": str(tp.temperature),
        "target top_p": str(tp.top_p),
        "target max_tokens": str(tp.max_tokens),
        "suite": m.suite.hash if m.suite else None,
    }


def _provenance_fields(record: RunRecord) -> dict[str, str | None]:
    m = record.manifest
    return {
        "served model": m.target.served_model,
        "n_repeats": str(m.run_config.n_repeats),
        "evalseal version": m.environment.evalseal_version,
        # A judge prompt hash sealed under 1.2 and one sealed under 1.3 measure different
        # things, so a reader seeing them differ needs to see this too.
        "schema version": m.schema_version,
        "git commit": (m.code.commit or "")[:12] or None,
        "sealed hash": (record.hash or "")[:20] or None,
    }


def _incomparability_reason(
    before: RunRecord, after: RunRecord, config: list[FieldChange]
) -> str:
    """One sentence saying why the evaluator fingerprints differ.

    A schema difference gets named first. A record sealed before schema 1.4 has no
    judge endpoint, no `max_tokens` and no scorer config hash, so a fingerprint
    difference against a newer record may be an artefact of the older schema rather than
    a change anyone made, and saying "the rubric changed" would be wrong.
    """
    if not records_share_fingerprint_inputs(before, after):
        versions = sorted({before.manifest.schema_version, after.manifest.schema_version})
        return (
            f"the two runs were sealed under different schema versions "
            f"({', '.join(versions)}). Fields the evaluator fingerprint covers from 1.4 - "
            "the judge endpoint, max_tokens, the scorer's own settings - are absent from "
            "the older record, so they cannot be compared. Re-record the older run to "
            "compare these directly."
        )
    changed = [c.name for c in config if c.changed and c.name in EVALUATOR_FIELDS]
    if changed:
        return f"the grading setup changed: {', '.join(changed)}."
    return (
        "the evaluator fingerprints differ although no named field did, which means a "
        "judge parameter or scorer setting changed that this summary does not list."
    )


def diff_records(before: RunRecord, after: RunRecord) -> DiffResult:
    """Compare two sealed runs across score, stability and evaluator configuration."""
    config = [
        FieldChange(name, b, a)
        for (name, b), a in zip(
            _config_fields(before).items(), _config_fields(after).values(), strict=True
        )
    ]
    # Comparability is about the grader, not the subject: a different target model is
    # exactly what a benchmark compares.
    comparable = evaluator_fingerprint(before) == evaluator_fingerprint(after)
    reason = _incomparability_reason(before, after, config) if not comparable else ""

    paired = compare_runs(before, after)
    ids_b = {c.case_id for c in before.results}
    ids_a = {c.case_id for c in after.results}
    # Stability movement is only meaningful for items present in both runs. A case that
    # was dropped from the dataset would otherwise show up as "now stable", which reads
    # as an improvement when what happened is that the item is gone.
    shared = ids_b & ids_a
    unstable_b = [c for c in _unstable(before) if c in shared]
    unstable_a = [c for c in _unstable(after) if c in shared]

    return DiffResult(
        comparable=comparable,
        config_changes=[c for c in config if c.changed],
        provenance=[
            FieldChange(name, b, a)
            for (name, b), a in zip(
                _provenance_fields(before).items(), _provenance_fields(after).values(),
                strict=True,
            )
        ],
        score_before=before.aggregate.mean_score,
        score_after=after.aggregate.mean_score,
        paired=paired,
        flip_rate_before=mean_flip_rate(before),
        flip_rate_after=mean_flip_rate(after),
        unstable_before=_unstable(before),
        unstable_after=_unstable(after),
        newly_unstable=sorted(set(unstable_a) - set(unstable_b)),
        now_stable=sorted(set(unstable_b) - set(unstable_a)),
        still_unstable=sorted(set(unstable_a) & set(unstable_b)),
        cases_added=sorted(ids_a - ids_b),
        cases_removed=sorted(ids_b - ids_a),
        reason=reason,
    )
