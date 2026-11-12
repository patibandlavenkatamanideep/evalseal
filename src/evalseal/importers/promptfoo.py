"""Read a promptfoo export into a receipt.

Everything that knows promptfoo's JSON is here. Written against a real export from
**promptfoo 0.125.1**, export schema `results.version: 3`; the fixture and the command
that produced it are in `tests/fixtures/importers/promptfoo/`.

`docs/importers/promptfoo.md` is the field mapping, with a column for what each field
does *not* support. The three decisions that matter, because they are the ones a
plausible importer gets wrong:

**`testIdx` is not a case index.** It is a flat row index. With `--repeat 3` the first
case occupies `testIdx` 0, 1 and 2, so keying on it reports fifteen cases of one repeat
each and puts an interval around a sample of one. Repeats are grouped by
`repeatGroupId`.

**`repeatGroupId` is grouping, not identity.** It is positional - `test-0-vars-0` - so
inserting a test at the top renames every group after it. The case id comes from
`testCase.description` where that is present and unique, and from a digest of `vars`
otherwise, and the receipt records which rule was used.

**An errored row is not a failed row.** Both carry `success: false` and `score: 0`.
Only `failureReason` separates them: 1 is an assertion that failed, which is a graded
FAIL, and 2 is the provider erroring, which was never graded at all. The second becomes
an unscorable repeat.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..adapters.recording import digest
from ..models import (
    ImportProvenance,
    ScorerProvenance,
    SuiteProvenance,
    TargetProvenance,
)
from ..provenance import text_hash
from ..validation import InvalidInput, strict_json_loads
from .base import Gaps, ImportRefused, Observation, build_record, empty_params

TOOL = "promptfoo"
#: Export schemas this adapter has been read against. A newer one may have moved a
#: field this reads or changed what one means, and guessing is how an importer
#: silently produces a receipt about the wrong thing.
SUPPORTED_EXPORT_SCHEMAS = (3,)

#: promptfoo's own enum, from the export. 0 is "nothing went wrong".
_ASSERTION_FAILURE = 1
_PROVIDER_ERROR = 2


@dataclass(frozen=True)
class _Row:
    """One row of `results.results`, with the fields this importer reads."""
    raw: dict[str, Any]

    @property
    def group(self) -> str | None:
        value = self.raw.get("repeatGroupId")
        return value if isinstance(value, str) and value else None

    @property
    def repeat_index(self) -> int | None:
        value = self.raw.get("repeatIndex")
        return value if isinstance(value, int) else None

    @property
    def description(self) -> str | None:
        case = self.raw.get("testCase") or {}
        value = case.get("description")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def vars(self) -> dict:
        value = self.raw.get("vars")
        return value if isinstance(value, dict) else {}

    @property
    def failure_reason(self) -> int:
        value = self.raw.get("failureReason")
        return value if isinstance(value, int) else 0

    @property
    def success(self) -> bool:
        return bool(self.raw.get("success"))

    @property
    def provider_id(self) -> str:
        provider = self.raw.get("provider")
        if isinstance(provider, dict) and isinstance(provider.get("id"), str):
            return provider["id"]
        if isinstance(provider, str) and provider:
            return provider
        return "unknown"

    @property
    def output(self) -> str | None:
        response = self.raw.get("response")
        if isinstance(response, dict) and isinstance(response.get("output"), str):
            return response["output"]
        return None

    @property
    def error(self) -> str | None:
        for candidate in (self.raw.get("error"),
                          (self.raw.get("response") or {}).get("error")
                          if isinstance(self.raw.get("response"), dict) else None):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @property
    def assertions(self) -> list[dict]:
        grading = self.raw.get("gradingResult")
        if not isinstance(grading, dict):
            return []
        components = grading.get("componentResults")
        return [c for c in components if isinstance(c, dict)] \
            if isinstance(components, list) else []

    @property
    def grading_pass(self) -> bool | None:
        grading = self.raw.get("gradingResult")
        if isinstance(grading, dict) and isinstance(grading.get("pass"), bool):
            return grading["pass"]
        return None


def _load(path: Path) -> dict:
    try:
        parsed = strict_json_loads(path.read_text(encoding="utf-8"), what=str(path))
    except InvalidInput as e:
        raise ImportRefused(f"{path} is not readable as JSON: {e}") from None
    except UnicodeDecodeError as e:
        raise ImportRefused(f"{path} is not UTF-8 text: {e}") from None
    if not isinstance(parsed, dict):
        raise ImportRefused(
            f"{path} contains a JSON {type(parsed).__name__}, not an object. A "
            "promptfoo export is an object with a `results` key.")
    return parsed


def _section(export: dict, key: str) -> dict:
    """A top-level object, or an empty one. Narrowed here so callers are not full of
    `isinstance` noise and mypy does not see `Any | None`."""
    value = export.get(key)
    return value if isinstance(value, dict) else {}


def _rows(export: dict, path: Path) -> list[_Row]:
    results = export.get("results")
    if not isinstance(results, dict):
        raise ImportRefused(
            f"{path} has no `results` object. Either it is not a promptfoo export, or "
            "it was written by `promptfoo share` rather than `promptfoo eval -o`.")
    version = results.get("version")
    if version not in SUPPORTED_EXPORT_SCHEMAS:
        raise ImportRefused(
            f"{path} declares export schema `results.version: {version!r}`, and this "
            f"importer has only been read against {SUPPORTED_EXPORT_SCHEMAS}. A newer "
            "schema may have moved a field or changed what one means, and guessing "
            "produces a receipt about the wrong thing. Export with promptfoo 0.125.x, "
            "or extend src/evalseal/importers/promptfoo.py against the new shape.")
    rows = results.get("results")
    if not isinstance(rows, list) or not rows:
        raise ImportRefused(f"{path} has no rows in `results.results`.")
    bad = [i for i, row in enumerate(rows) if not isinstance(row, dict)]
    if bad:
        raise ImportRefused(
            f"{path}: `results.results` entries {bad[:5]} are not objects.")
    return [_Row(row) for row in rows]


def _case_ids(rows: list[_Row], gaps: Gaps) -> tuple[dict[int, str], str]:
    """Map each row index to a case id, and say which rule produced them.

    Descriptions where they exist and are unique per group; a digest of `vars`
    otherwise. Never `testIdx`, which is a row index wearing a case index's name.
    """
    groups: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        key = row.group
        if key is None:
            # No grouping field at all: fall back to the vars digest, which at least
            # puts identical inputs together.
            key = f"vars:{digest(row.vars)}"
            gaps.add("repeat grouping",
                     "the export has no `repeatGroupId`, so repeats were grouped by "
                     "the digest of their `vars` instead")
        groups.setdefault(key, []).append(i)

    descriptions = {
        key: {rows[i].description for i in members} for key, members in groups.items()
    }
    usable = all(len(names) == 1 and next(iter(names)) is not None
                 for names in descriptions.values())
    distinct = {next(iter(names)) for names in descriptions.values()} if usable else set()
    if usable and len(distinct) == len(groups):
        rule = "testCase.description"
        names = {key: next(iter(descriptions[key])) for key in groups}
    else:
        rule = "a digest of testCase.vars"
        gaps.add("case id",
                 "`testCase.description` is absent, repeated or inconsistent within a "
                 "repeat group, so case ids are digests of `vars`")
        names = {key: f"case-{digest(rows[members[0]].vars)[7:19]}"
                 for key, members in groups.items()}

    mapping: dict[int, str] = {}
    for key, members in groups.items():
        for i in members:
            mapping[i] = str(names[key])
    return mapping, rule


def _repeat_numbers(rows: list[_Row], case_of: dict[int, str],
                    gaps: Gaps) -> dict[int, int]:
    """`repeatIndex` where the export gives one, else position within the case."""
    seen: dict[str, int] = {}
    numbers: dict[int, int] = {}
    missing = False
    for i, row in enumerate(rows):
        case_id = case_of[i]
        position = seen.get(case_id, 0)
        seen[case_id] = position + 1
        if row.repeat_index is None:
            missing = True
            numbers[i] = position
        else:
            numbers[i] = row.repeat_index
    if missing:
        gaps.add("repeat index",
                 "some rows carry no `repeatIndex`, so their repeat number is their "
                 "position within the case rather than something the export stated")
    # Two rows of one case claiming the same repeat would silently collapse later.
    for case_id in set(case_of.values()):
        indexes = [numbers[i] for i in numbers if case_of[i] == case_id]
        if len(set(indexes)) != len(indexes):
            raise ImportRefused(
                f"case {case_id!r} has two rows with the same `repeatIndex`. Those are "
                "either duplicated rows or a repeat grouping this importer has read "
                "wrongly; either way they must not be counted as separate repeats.")
    return numbers


def _observe(row: _Row, case_id: str, repeat: int) -> Observation:
    """One row as an observation, keeping an error distinct from a failure."""
    output = row.output
    row_id = row.raw.get("id")

    def observed(score: float, *, unscorable: bool = False,
                 reason: str | None = None) -> Observation:
        return Observation(
            case_id=case_id,
            repeat=repeat,
            score=score,
            unscorable=unscorable,
            reason=reason,
            response_digest=digest({"output": output}) if output is not None else None,
            provider_id=row.provider_id,
            source_row_id=row_id if isinstance(row_id, str) else None,
        )

    if row.failure_reason == _PROVIDER_ERROR:
        return observed(
            0.0, unscorable=True,
            reason=row.error or "the provider errored and the row was never graded")
    if not row.assertions and row.grading_pass is None:
        # No grading result and no error flag either: nothing was graded, whatever the
        # row's `success` says. Reported rather than scored.
        return observed(
            0.0, unscorable=True,
            reason=row.error or "the row carries no grading result, so nothing was "
                                "graded; promptfoo did not record why")

    passed = row.success
    if row.grading_pass is not None and row.grading_pass != passed:
        raise ImportRefused(
            f"case {case_id!r} repeat {repeat}: the row says `success: {passed}` and "
            f"its `gradingResult.pass` says {row.grading_pass}. One of them is wrong "
            "and this importer will not choose between them.")
    if row.failure_reason == _ASSERTION_FAILURE and passed:
        raise ImportRefused(
            f"case {case_id!r} repeat {repeat}: `failureReason: 1` means an assertion "
            "failed, but the row also says `success: true`.")
    return observed(1.0 if passed else 0.0)


def _scorer(rows: list[_Row], export: dict, gaps: Gaps) -> ScorerProvenance:
    """What graded these rows, as far as the export says - which is not far."""
    kinds = sorted({
        str(a.get("assertion", {}).get("type"))
        for row in rows for a in row.assertions
        if isinstance(a.get("assertion"), dict) and a["assertion"].get("type")
    })
    gaps.add("scorer.impl_hash",
             "the grading code is promptfoo's, at a version this export names but does "
             "not digest, so no evaluator fingerprint can be computed and two imports "
             "are not comparable")
    if not kinds:
        gaps.add("assertion types",
                 "no row carries a `gradingResult.componentResults` entry naming an "
                 "assertion type")
    return ScorerProvenance(
        type="exact",
        # Deliberately null. See the gap above: this is the field that decides
        # comparability, and an importer that filled it would be inventing the answer.
        impl_hash=None,
        config_hash=text_hash(json.dumps(
            {"promptfoo_assertions": kinds}, sort_keys=True, separators=(",", ":"))),
    )


def _target(rows: list[_Row], gaps: Gaps) -> TargetProvenance:
    providers = sorted({row.provider_id for row in rows})
    if len(providers) > 1:
        gaps.add("target.requested_model",
                 f"the export mixes {len(providers)} providers ({', '.join(providers)}); "
                 "the receipt names the first and each call carries its own")
    gaps.add("target.served_model",
             "promptfoo does not record what the provider said it served, so an alias "
             "resolving to a different snapshot would be invisible")
    return TargetProvenance(
        provider=None,
        requested_model=providers[0] if providers else "unknown",
        served_model=None,
        system_fingerprint=None,
        base_url="imported://promptfoo",
        effective_params=empty_params(),
        # Not `explicit`: nothing here was sent by EvalSeal, so claiming the parameters
        # were set would describe a request that was never made.
        params_source="provider_default",
    )


def import_export(path: str | Path, *, prev_hash: str = "GENESIS"):
    """Read a promptfoo export and return a sealed-ready `RunRecord`.

    Raises `ImportRefused` with a message naming the field at fault, never a traceback
    and never a receipt built on a guess.
    """
    path = Path(path)
    started_at = datetime.now(UTC).isoformat()
    source_sha, raw = _source(path)
    export = _load(path)
    rows = _rows(export, path)
    gaps = Gaps()

    case_of, id_rule = _case_ids(rows, gaps)
    repeats = _repeat_numbers(rows, case_of, gaps)
    observations = [_observe(row, case_of[i], repeats[i]) for i, row in enumerate(rows)]

    metadata = _section(export, "metadata")
    tool_version = metadata.get("promptfooVersion")
    if not isinstance(tool_version, str) or not tool_version:
        tool_version = None
        gaps.add("tool version",
                 "the export carries no `metadata.promptfooVersion`, so the receipt "
                 "cannot say which build produced it")

    claimed_times = {
        key: metadata[key] for key in ("exportedAt", "evaluationCreatedAt")
        if isinstance(metadata.get(key), str)
    }
    timestamp = _section(export, "results").get("timestamp")
    if isinstance(timestamp, str):
        claimed_times["results.timestamp"] = timestamp
    if not claimed_times:
        gaps.add("claimed times",
                 "the export carries no timestamp of its own, so there is not even a "
                 "claim about when it ran")

    runtime = _section(export, "runtimeOptions")
    declared_repeat = runtime.get("repeat")
    counts: dict[str, int] = {}
    for item in observations:
        counts[item.case_id] = counts.get(item.case_id, 0) + 1
    n_repeats = max(counts.values())

    warnings: list[str] = [f"CASE IDS FROM {id_rule}."]
    if len(set(counts.values())) > 1:
        warnings.append(
            "UNEVEN REPEATS: cases in this export do not all have the same number of "
            f"rows ({min(counts.values())} to {max(counts.values())}). Every interval "
            "in this receipt is over the rows that case actually has.")
    if isinstance(declared_repeat, int) and declared_repeat != n_repeats:
        warnings.append(
            f"DECLARED REPEATS DISAGREE: `runtimeOptions.repeat` is {declared_repeat} "
            f"and the busiest case has {n_repeats} row(s). The rows are what this "
            "receipt counts.")
    if runtime.get("cache"):
        warnings.append(
            "CACHING WAS ENABLED (`runtimeOptions.cache: true`). A cached repeat is "
            "one draw served twice, and this export does not say per row which were "
            "cached - so the repeats behind every interval here may not be "
            "independent draws. See docs/statistical-contract.md.")
        gaps.add("repeat independence",
                 "promptfoo cached by default for this run and the export does not "
                 "mark which rows were served from cache")

    # Built before the provenance below, because they add to `gaps` and pydantic
    # copies a list when it validates one - so a gap added after `ImportProvenance`
    # exists would be in the warnings and missing from the receipt's own list.
    scorer = _scorer(rows, export, gaps)
    target = _target(rows, gaps)

    imported = ImportProvenance(
        tool=TOOL,
        tool_version=tool_version,
        export_schema=str(_section(export, "results").get("version")),
        source_path=str(path),
        source_sha256=source_sha,
        source_bytes=len(raw),
        imported_at=started_at,
        gaps=gaps.entries,
        claimed_times=claimed_times,
    )
    config = _section(export, "config")
    description = config.get("description")
    suite = SuiteProvenance(
        name=description if isinstance(description, str) else None,
        path=str(path),
        hash=source_sha,
    )
    return build_record(
        observations=observations,
        imported=imported,
        gaps=gaps,
        # The case set, not the export: two exports of the same questions should agree
        # here even though their files differ byte for byte.
        dataset_hash=digest(sorted({o.case_id for o in observations})),
        suite=suite,
        scorer=scorer,
        target=target,
        n_repeats=n_repeats,
        warnings=warnings,
        prev_hash=prev_hash,
        started_at=started_at,
    )


def _source(path: Path) -> tuple[str, bytes]:
    from .base import source_digest

    if not path.is_file():
        raise ImportRefused(f"{path} does not exist or is not a file.")
    return source_digest(path)
