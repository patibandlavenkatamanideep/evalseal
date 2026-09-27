"""Declare the evaluation before running it, then check the receipt against it.

## The objection this answers, and how far it gets

*"What stops someone running the suite until they get a good number and sealing only
that one?"*

Nothing in this file, and nothing that runs on the same machine as the eval, can prove
a private attempt never happened. A ledger records what was appended to it. Deleting
the ledger and starting again leaves no trace inside EvalSeal, because there is no
outside observer to notice.

What a pre-registration does is narrow the room to move **before** any number exists:

- The suite, dataset and exact case ids are fixed by hash. Dropping the eight items
  that failed is no longer a quiet edit; it changes `case_set_hash` and the gate says
  so by name.
- The repeat count is fixed. Reporting the best of five single runs instead of one
  five-repeat run is visible, because each case must carry the declared number of
  scores.
- The grading setup can be pinned, so the judge cannot be swapped after seeing the
  score.
- The thresholds are fixed in the same file, before the number exists. A gate loosened
  after a failure shows up in `git log` as a commit someone made.
- Requiring CI moves receipt production off the machine of the person being measured.

That is a shift from "trust this number" to "this number came from the evaluation that
was declared, and here is what was declared". It is not proof of no cherry-picking, and
`docs/pre-registration.md` says so in the same words.

The honest one-line version: **a pre-registration makes the contract checkable, not the
author honest.**
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ledger import (
    _case_set_hash,
    _hash_ids,
    evaluator_fingerprint,
    explain_fingerprint_mismatch,
)
from .models import RunRecord
from .policy import Check, Policy
from .provenance import file_hash, git_provenance

PREREG_VERSION: Literal[1] = 1


class _Strict(BaseModel):
    # As in policy.py: a typo must not read as "no opinion".
    model_config = ConfigDict(extra="forbid")


class FileContract(_Strict):
    """A file the evaluation is declared against, pinned by digest."""
    path: str | None = None
    sha256: str | None = None


class DatasetContract(FileContract):
    n_cases: int | None = None
    # The ids that are supposed to be scored, by hash. This is the field that makes
    # quietly dropping the cases that failed into a visible change.
    case_set_hash: str | None = None


class RunContract(_Strict):
    """How many times the suite is declared to run."""
    n_repeats: int = Field(default=5, ge=1)
    # Defaults to n_repeats when unset; a case with fewer scores than this fails.
    min_repeats_per_case: int | None = Field(default=None, ge=1)


class Preregistration(_Strict):
    version: Literal[1] = PREREG_VERSION
    created_at: str
    note: str | None = None

    suite: FileContract = FileContract()
    dataset: DatasetContract = DatasetContract()
    runs: RunContract = RunContract()

    # Pinned after a first run, since a fingerprint cannot be computed from files
    # alone: it covers the judge's served identity as well as the rubric.
    evaluator_fingerprint: str | None = None

    required_artifacts: list[str] = Field(default_factory=list)
    # Roles allowed to be sealed as `external` - named but unreadable at seal time.
    # Explicit, because "the cassette was not there" must never pass by default.
    allow_external_artifacts: list[str] = Field(default_factory=list)

    require_ci: bool = False
    require_signature: bool = False
    require_anchor: bool = False
    require_external_anchor: bool = False

    baseline: str | None = None
    policy: Policy | None = None

    git_commit: str | None = None
    git_branch: str | None = None

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


class PreregError(ValueError):
    """The pre-registration file itself is wrong, as opposed to the run failing it."""


def load_preregistration(path: str | Path) -> Preregistration:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise PreregError(f"{p} could not be read: {e.strerror}") from None
    if not text.strip():
        raise PreregError(f"{p} is empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PreregError(f"{p} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise PreregError(f"{p} must contain a mapping at the top level")
    try:
        return Preregistration.model_validate(data)
    except ValidationError as e:
        raise PreregError(f"{p} is not a valid pre-registration:\n{e}") from None


def build_preregistration(
    *,
    suite: Path | None = None,
    dataset: Path | None = None,
    dataset_hash: str | None = None,
    n_repeats: int = 5,
    min_repeats_per_case: int | None = None,
    case_ids: list[str] | None = None,
    evaluator_fingerprint_pin: str | None = None,
    required_artifacts: list[str] | None = None,
    require_ci: bool = False,
    require_signature: bool = False,
    require_anchor: bool = False,
    require_external_anchor: bool = False,
    baseline: str | None = None,
    policy: Policy | None = None,
    note: str | None = None,
) -> Preregistration:
    """Read the declared inputs and freeze their digests into a contract.

    Paths are stored relative to the working directory where possible. A
    pre-registration is meant to be committed and read by other people, and an
    absolute path both leaks a home directory and means nothing on their machine.
    Only the digests are load-bearing; the paths are there so a reader knows what was
    declared.
    """
    git = git_provenance()
    return Preregistration(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        note=note,
        suite=FileContract(
            path=_readable_path(suite),
            sha256=file_hash(suite) if suite else None),
        dataset=DatasetContract(
            path=_readable_path(dataset),
            # `dataset_hash` is the digest a *receipt* carries, which is over the
            # decoded text rather than the raw bytes. The caller passes it because the
            # two are not the same value: reading a CRLF file translates newlines, so
            # `file_hash` and `Dataset.hash` disagree on Windows, and a contract pinned
            # with the wrong one fails the dataset clause for a reason that has nothing
            # to do with the dataset. One definition, as with `_hash_ids`.
            sha256=dataset_hash or (file_hash(dataset) if dataset else None),
            n_cases=len(case_ids) if case_ids else None,
            case_set_hash=_hash_ids(case_ids) if case_ids else None),
        runs=RunContract(n_repeats=n_repeats, min_repeats_per_case=min_repeats_per_case),
        evaluator_fingerprint=evaluator_fingerprint_pin,
        required_artifacts=sorted(required_artifacts or []),
        require_ci=require_ci,
        require_signature=require_signature,
        require_anchor=require_anchor,
        require_external_anchor=require_external_anchor,
        baseline=baseline,
        policy=policy,
        git_commit=git["commit"],
        git_branch=_git_branch(),
    )


def _readable_path(path: Path | None) -> str | None:
    """Relative to the working directory when it is under it, else as given."""
    if path is None:
        return None
    try:
        return Path(path).resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def _git_branch() -> str | None:
    from .provenance import _git

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    return branch if branch and branch != "HEAD" else None


@dataclass
class AnchorState:
    """What the caller found out about an anchor, so this module need not re-verify it."""
    present: bool = False
    verified: bool = False
    has_external_proof: bool = False
    detail: str = ""


def check_preregistration(
    prereg: Preregistration,
    record: RunRecord,
    *,
    anchor: AnchorState | None = None,
) -> list[Check]:
    """Check one receipt against the contract, one named check per clause.

    Every declared clause produces a check, passed or failed. A clause that cannot be
    evaluated fails rather than being skipped - the rule policy.py already follows,
    for the same reason: a gate that silently checks nothing turns a green build into
    false evidence.
    """
    checks: list[Check] = []
    m = record.manifest

    if prereg.suite.sha256 is not None:
        actual = m.suite.hash if m.suite else None
        checks.append(Check(
            "prereg.suite", actual == prereg.suite.sha256,
            f"suite digest matches ({_short(actual)})" if actual == prereg.suite.sha256
            else f"suite digest {_short(actual)} does not match the declared "
                 f"{_short(prereg.suite.sha256)}"
                 + ("; this record sealed no suite" if actual is None else "")))

    if prereg.dataset.sha256 is not None:
        actual = m.dataset.hash
        checks.append(Check(
            "prereg.dataset", actual == prereg.dataset.sha256,
            f"dataset digest matches ({_short(actual)})" if actual == prereg.dataset.sha256
            else f"dataset digest {_short(actual)} does not match the declared "
                 f"{_short(prereg.dataset.sha256)}"))

    if prereg.dataset.case_set_hash is not None:
        actual = _case_set_hash(record)
        ok = actual == prereg.dataset.case_set_hash
        detail = f"the declared {len(record.results)} case(s) ran"
        if not ok:
            declared = prereg.dataset.n_cases
            detail = (
                f"the set of cases scored is not the declared one: {len(record.results)} "
                f"case(s) ran"
                + (f" against {declared} declared" if declared is not None else "")
                + ". Adding or dropping cases after declaring them changes this hash."
            )
        checks.append(Check("prereg.case_set", ok, detail))

    checks.extend(_repeat_checks(prereg.runs, record))

    if prereg.evaluator_fingerprint is not None:
        actual = evaluator_fingerprint(record)
        ok = actual == prereg.evaluator_fingerprint
        checks.append(Check(
            "prereg.evaluator_fingerprint", ok,
            f"grading setup matches the pin ({actual[:28]}...)" if ok
            else explain_fingerprint_mismatch(
                "evaluator", prereg.evaluator_fingerprint, actual)))

    checks.extend(_artifact_checks(prereg, record))

    if prereg.require_ci:
        ci = record.manifest.environment.ci
        ok = bool(ci and ci.claimed)
        where = f" ({ci.provider}" + (f", {ci.run_url}" if ci and ci.run_url else "") + ")" \
            if ci and ci.claimed else ""
        checks.append(Check(
            "prereg.require_ci", ok,
            f"the receipt claims it was produced in CI{where}. This is the environment's "
            "own claim: check the run URL, and sign the ledger with a key only CI holds"
            if ok else
            "the receipt carries no CI markers, so it was produced outside CI (or by a "
            "runner EvalSeal does not recognise)"))

    checks.extend(_anchor_checks(prereg, anchor))
    return checks


def _repeat_checks(runs: RunContract, record: RunRecord) -> list[Check]:
    """The declared repeat count, checked against what each case actually carries.

    Both halves matter. `run_config.n_repeats` is what the run was asked for; the
    per-case score counts are what it delivered. A run interrupted halfway has the
    right config and short cases.
    """
    checks = []
    declared = runs.n_repeats
    actual = record.manifest.run_config.n_repeats
    checks.append(Check(
        "prereg.n_repeats", actual == declared,
        f"ran {actual} repeat(s) as declared" if actual == declared
        else f"ran {actual} repeat(s), not the declared {declared}"))

    floor = runs.min_repeats_per_case or declared
    short = sorted((c.case_id, c.n_runs) for c in record.results if c.n_runs < floor)
    detail = f"every case carries at least {floor} score(s)"
    if short:
        listed = ", ".join(f"{cid} ({n})" for cid, n in short[:5])
        detail = (f"{len(short)} case(s) carry fewer than {floor} scores: {listed}"
                  + (" ..." if len(short) > 5 else ""))
    checks.append(Check("prereg.min_repeats_per_case", not short, detail))
    return checks


def _artifact_checks(prereg: Preregistration, record: RunRecord) -> list[Check]:
    """Every declared artifact role must be sealed with a digest, or be allowed external.

    "Not sealed" and "sealed as external" are different failures and are reported
    differently: the first means the run never bound the file, the second means the
    file was named but unreadable, so there is a digest-shaped hole where the evidence
    should be.
    """
    if not prereg.required_artifacts:
        return []
    by_role = {a.role: a for a in record.manifest.artifacts}
    checks = []
    for role in prereg.required_artifacts:
        artifact = by_role.get(role)
        if artifact is None:
            checks.append(Check(
                f"prereg.artifact[{role}]", False,
                "not sealed into this receipt at all"))
        elif artifact.kind == "external":
            allowed = role in prereg.allow_external_artifacts
            checks.append(Check(
                f"prereg.artifact[{role}]", allowed,
                "sealed as external, which the pre-registration allows: there is no "
                "digest, so nothing here can be re-checked" if allowed
                else "sealed as `external` - it was named but not readable when the "
                     "record was sealed, so no digest was recorded. Add it to "
                     "allow_external_artifacts to accept that deliberately"))
        else:
            checks.append(Check(
                f"prereg.artifact[{role}]", True,
                f"sealed by digest ({_short(artifact.sha256)})"))
    return checks


def _anchor_checks(prereg: Preregistration, anchor: AnchorState | None) -> list[Check]:
    if not (prereg.require_anchor or prereg.require_external_anchor):
        return []
    state = anchor or AnchorState()
    checks = [Check(
        "prereg.require_anchor", state.present and state.verified,
        state.detail or ("anchor verified" if state.verified
                         else "no anchor was supplied to check (pass --anchor)"))]
    if prereg.require_external_anchor:
        checks.append(Check(
            "prereg.require_external_anchor", state.has_external_proof,
            "the anchor carries an external proof" if state.has_external_proof
            else "the anchor carries no external proof, so nothing establishes when it "
                 "was made. A local anchor is this machine's clock"))
    return checks


def _short(digest: str | None) -> str:
    return "none" if digest is None else digest[:20] + "..."
