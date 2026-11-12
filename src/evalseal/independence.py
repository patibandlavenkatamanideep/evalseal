"""How many independent draws are behind a number, and when that cannot be told.

Every interval and every test in this project assumes a case's repeats are separate
draws. A replay reproduces the draws that were recorded, which is a perfectly good
regression check - and **replaying one recorded draw twice is one observation, not
two.** A Wilson interval over five repeats that all came from one cassette entry reports
the precision of five observations and has one.

## What distinguishes the two cases

Two calls that returned byte-identical text are *either* two independent draws that
happened to agree *or* one recorded draw served twice. The text cannot tell them apart,
and neither can the response digest: it is equal in both cases. What tells them apart is
the cassette entry each call was served by, which schema 1.8 seals as
`execution.calls[].source_key`:

* different entries - two draws. The model was asked twice, whether just now or when the
  cassette was recorded, and agreeing twice is a real observation about the model.
* the same entry - one draw. The second repeat asked nothing and learned nothing.

A format-3 cassette keys an entry by case, repeat and role, so a replay of one serves a
distinct entry per repeat and the draws behind it are as independent as the recording
was. A format-2 cassette keys by request and repeat only, so two *cases* that ask the
same question share an entry - and then the items are not independent either, which
matters to the cluster bootstrap and to McNemar, both of which resample or count items.

## Unknown is an answer

A receipt sealed before 1.8 carries no `source_key`, and one sealed before 1.7 carries
no execution manifest at all. Independence is then **unknown**, and this module says
unknown rather than assuming the favourable answer or deriving an effective sample size
a receipt cannot support. A call that no cassette served - an in-process callable, or a
live provider call in a run with no cassette - is a draw of its own, because it was
actually made.

Nothing here is a significance test or a correction to one. It is the count of
independent draws behind each number, so that a reader who sees "five repeats" can find
out whether five things happened.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import RunRecord

#: The schema that first sealed a per-call cassette key.
SOURCE_KEY_SCHEMA = (1, 8)

INDEPENDENT = "independent"
REUSED = "reused"
UNKNOWN = "unknown"


def _schema_tuple(version: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in (version or "").split("."))
    except ValueError:
        return None


def records_source_keys(record: RunRecord) -> bool:
    """Whether this receipt can answer the independence question at all."""
    if record.manifest.execution is None:
        return False
    sealed = _schema_tuple(record.manifest.schema_version)
    return sealed is not None and sealed >= SOURCE_KEY_SCHEMA


@dataclass(frozen=True)
class CaseIndependence:
    """One case's repeats, as draws rather than as rows."""
    case_id: str
    role: str
    n_repeats: int
    #: Distinct draws behind those repeats. Equal to `n_repeats` when each repeat was
    #: served by its own cassette entry or made directly.
    n_draws: int
    #: Cassette entries that served more than one repeat of this case, with how many.
    reused: dict[str, int] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        return INDEPENDENT if self.n_draws >= self.n_repeats else REUSED

    @property
    def note(self) -> str:
        if self.verdict == INDEPENDENT:
            return (f"{self.n_repeats} repeat(s) from {self.n_draws} separate draw(s); "
                    "repeats that returned the same text are separate observations "
                    "that agreed")
        worst = max(self.reused.values(), default=0)
        return (
            f"{self.n_repeats} repeat(s) from only {self.n_draws} draw(s): one "
            f"recorded response was served to {worst} of them. An interval over "
            f"{self.n_repeats} observations is not supported here - the measurement "
            f"was made {self.n_draws} time(s)."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "role": self.role,
            "n_repeats": self.n_repeats,
            "n_draws": self.n_draws,
            "verdict": self.verdict,
            "reused_entries": dict(sorted(self.reused.items())),
            "note": self.note,
        }


@dataclass
class IndependenceAudit:
    """What a receipt's own execution manifest says about its observations."""
    known: bool
    cases: list[CaseIndependence] = field(default_factory=list)
    #: Cassette entries that served calls for more than one case id, with the ids. A
    #: format-2 cassette does this whenever two cases ask the same question, and then
    #: the *items* are not independent either.
    shared_across_cases: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""

    @property
    def verdict(self) -> str:
        if not self.known:
            return UNKNOWN
        if any(c.verdict == REUSED for c in self.cases) or self.shared_across_cases:
            return REUSED
        return INDEPENDENT

    @property
    def reused_cases(self) -> list[CaseIndependence]:
        return [c for c in self.cases if c.verdict == REUSED]

    @property
    def n_repeats(self) -> int:
        return sum(c.n_repeats for c in self.cases)

    @property
    def n_draws(self) -> int:
        return sum(c.n_draws for c in self.cases)

    def summary(self) -> str:
        """One line that never claims more than the receipt supports."""
        if not self.known:
            return (
                f"Independence unknown: {self.reason} The repeats behind every interval "
                "in this receipt may be separate draws or one draw counted several "
                "times, and nothing here can tell. No effective sample size is derived "
                "from a receipt that cannot support one."
            )
        if self.verdict == INDEPENDENT:
            return (
                f"{self.n_repeats} repeat(s) came from {self.n_draws} separate draw(s). "
                "Repeats that returned identical text are separate observations that "
                "agreed, not one observation counted twice."
            )
        lines = [
            f"{self.n_repeats} repeat(s) came from only {self.n_draws} draw(s), so the "
            f"intervals in this receipt are over fewer observations than they count."
        ]
        if self.reused_cases:
            named = ", ".join(c.case_id for c in self.reused_cases[:6])
            lines.append(
                f"{len(self.reused_cases)} case(s) reused a recorded response across "
                f"their own repeats ({named}"
                + (" and more" if len(self.reused_cases) > 6 else "") + ")."
            )
        if self.shared_across_cases:
            groups = list(self.shared_across_cases.values())[:3]
            lines.append(
                f"{len(self.shared_across_cases)} recorded response(s) were served to "
                f"more than one case ({'; '.join(', '.join(g) for g in groups)}), so "
                "those items are not independent of each other either - which the "
                "cluster bootstrap and the paired test both assume they are."
            )
        return " ".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "known": self.known,
            "verdict": self.verdict,
            "reason": self.reason,
            "n_repeats": self.n_repeats,
            "n_draws": self.n_draws,
            "n_reused_cases": len(self.reused_cases),
            "shared_across_cases": {k: sorted(v)
                                    for k, v in sorted(self.shared_across_cases.items())},
            "summary": self.summary(),
            "cases": [c.to_dict() for c in self.cases],
        }


def audit_independence(record: RunRecord) -> IndependenceAudit:
    """Count the independent draws behind each case, or say the receipt cannot say."""
    if record.manifest.imported is not None:
        # An import knows nothing about how the other tool obtained its rows. promptfoo
        # caches by default and does not mark per row which were served from cache, so
        # a row count is not a draw count. Saying `independent` here because the rows
        # are distinct would be the exact mistake this module exists to prevent.
        tool = record.manifest.imported.tool
        return IndependenceAudit(
            known=False,
            reason=f"these numbers were imported from {tool}, which does not record "
                   "whether a repeat was a fresh draw or a cached one being served "
                   "again.")
    execution = record.manifest.execution
    if execution is None:
        return IndependenceAudit(
            known=False,
            reason="this receipt was sealed before schema 1.7, so it records no "
                   "per-call detail at all.")
    if not records_source_keys(record):
        return IndependenceAudit(
            known=False,
            reason=f"this receipt was sealed under schema "
                   f"{record.manifest.schema_version}, which does not record which "
                   f"cassette entry served each call.")

    by_unit: dict[tuple[str, str], list[str | None]] = defaultdict(list)
    cases_by_key: dict[str, set[str]] = defaultdict(set)
    for call in execution.calls:
        by_unit[(call.case_id, call.role)].append(call.source_key)
        if call.source_key is not None:
            cases_by_key[call.source_key].add(call.case_id)

    cases: list[CaseIndependence] = []
    for (case_id, role), keys in sorted(by_unit.items()):
        served = [k for k in keys if k is not None]
        # A call no cassette served is a draw of its own: it was actually made.
        direct = len(keys) - len(served)
        counts: dict[str, int] = defaultdict(int)
        for key in served:
            counts[key] += 1
        cases.append(CaseIndependence(
            case_id=case_id, role=role, n_repeats=len(keys),
            n_draws=len(counts) + direct,
            reused={k: n for k, n in sorted(counts.items()) if n > 1},
        ))

    return IndependenceAudit(
        known=True,
        cases=cases,
        shared_across_cases={k: sorted(ids) for k, ids in sorted(cases_by_key.items())
                             if len(ids) > 1},
    )
