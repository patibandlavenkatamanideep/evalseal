from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

ParamsSource = Literal["explicit", "provider_default"]
#: Where a response came from.
#:   live    - a real provider call was made for it
#:   replay  - it was served out of a cassette; no provider was contacted
#:   local   - an in-process callable produced it; there is no provider at all
#:   unknown - a custom Target did not say. Never assume "live" from a silence: it is
#:             the strongest claim a receipt makes.
#:   imported - the number came from another tool's export, read by an importer. No
#:              provider was contacted by EvalSeal and none of this run's timing
#:              describes a measurement.
CallOrigin = Literal["live", "replay", "local", "unknown", "imported"]
#: The origins of a whole run's calls, collapsed. `mixed` when they disagree - a run
#: that replayed half its calls is not a live run, and saying so is the point.
ExecutionOrigin = Literal["live", "replay", "local", "unknown", "mixed", "imported"]
#: Which side of a judged measurement a call belongs to.
CallRole = Literal["target", "judge"]
#: How a call ended. `truncated` means the reply hit the token limit, so the answer is
#: absent rather than wrong; `empty` means the provider returned no text at all.
CallStatus = Literal["complete", "truncated", "empty"]
#: Why a sampling parameter has no value in a receipt. `null` alone could not say.
#:   inherited   - we did not send one, so the provider applied its own default. This
#:                 receipt does not know what that default was.
#:   unsupported - this API has no such parameter, so there was never one to send.
#: Reported as the same `null`, those two read as "the effective value was nothing",
#: which is a fabricated effective value rather than an honest absence.
ParamAbsence = Literal["inherited", "unsupported"]
ScorerKind = Literal["exact", "regex", "llm_judge", "answer_match"]
FailOn = Literal["none", "unstable", "borderline"]

# 1.2 seals the evaluator configuration (suite, judge prompt, code, environment) and
# per-case verdict detail. Any added field changes record hashes, hence the bump.
# 1.4 adds the fields the evaluator fingerprint was missing: the provider and
# `max_tokens` for the target and the judge, and a hash of the scorer's own settings.
# Older records still verify, re-hashed under the schema that sealed them.
# 1.3 adds no field; it changes what `scorer.judge_prompt_hash` means. Under 1.2 it
# hashed the last judge prompt sent, which varied with the target's responses and with
# scheduling. Under 1.3 it hashes the judge prompt template. A 1.2 judge record and a
# 1.3 one therefore disagree on this hash for that reason alone.
# 1.7 adds the execution manifest: what each provider call was for, where it came from,
# and what it took. Until 1.7 `manifest.target` was the provenance of call number one
# standing in for every call, and nothing said whether a run had called a provider or
# replayed a cassette - so a replayed run and a live one produced receipts that no field
# could tell apart. It changes no fingerprint: it describes what ran, not how it was
# graded, so no pin moves.
# 1.6 adds the fields scheme 3 of the evaluator fingerprint needs: a digest of the
# scoring *code* (`scorer.impl_hash`), the judge's API version and system prompt hash,
# and the per-parameter reason a sampling value is absent. Until 1.6 a fork that changed
# answer extraction, a judge given a different system prompt, and a judge reached
# through a different API version all produced the same evaluator fingerprint.
# 1.5 adds what the environment claimed about the CI job, so a pre-registration can
# require that a receipt came from CI rather than from someone's laptop. It is a claim
# and is named as one; see CIProvenance. It also records the evaluator and target
# section digests of a version-2 suite file.
# 1.8 adds what Phase D needs to report honestly. `results[].unscorable_count` and
# `aggregate.n_unscorable` separate "the grader judged this wrong" from "the grader
# could not judge it": until 1.8 an empty judge reply, a provider error string and a
# reply with no answer in it all arrived as a graded FAIL, so a rate-limited judge
# looked like a model that failed every case. `execution.calls[].source_key` names the
# cassette entry that served a call, which is the only evidence that distinguishes two
# independently recorded draws that happened to agree from one recorded draw counted
# twice. It changes no fingerprint: it describes what was measured and how well, not
# how the grading was configured.
# 1.9 adds `manifest.imported`: where a receipt's numbers came from when they were read
# out of another tool's export rather than measured here. It carries the producing tool
# and its version, the export's digest and size, and - the field that does the work -
# the list of EvalSeal fields the export could not fill. A receipt that cannot say how
# it was graded must not be mistaken for one that can, and two receipts missing the same
# field are not thereby comparable.
SCHEMA_VERSION = "1.9"

Stability = Literal["stable_pass", "stable_fail", "unstable", "insufficient_runs"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class EffectiveParams(BaseModel):
    """Sampling parameters that change what a model returns, and so what it scores.

    `max_tokens` is here from schema 1.4. The Anthropic adapter always sends it, but
    nothing recorded it, so a receipt could not explain a truncated verdict.
    """
    temperature: float | None = None
    seed: int | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    #: Parameter name -> why it has no value, for the ones that have none. From 1.6.
    #: A parameter carrying a value never appears here, so the two cannot disagree.
    unset: dict[str, ParamAbsence] = Field(default_factory=dict)


class TargetProvenance(BaseModel):
    provider: str | None = None            # openai_compatible | anthropic | local
    requested_model: str
    served_model: str | None = None        # from response; WARN if != requested
    system_fingerprint: str | None = None
    base_url: str
    effective_params: EffectiveParams
    params_source: ParamsSource = "explicit"
    #: The provider API version the request declared, when the wire format carries one
    #: (Anthropic's `anthropic-version` header). It decides how the request is
    #: interpreted, so for a judge it is part of the instrument. None where the format
    #: has no version to declare, which is not the same as not having recorded it.
    api_version: str | None = None
    #: Hash, never the text: a system prompt can be as private as the rubric it
    #: carries. None means no system prompt was sent, which is a different claim from
    #: an empty one - an empty string hashes to a value.
    system_prompt_hash: str | None = None


class ScorerProvenance(BaseModel):
    type: ScorerKind
    #: Digest of the code that turns a response into a score - the scoring method and
    #: every same-module helper and constant it reaches, normalized so that comments
    #: and docstrings do not count. From 1.6. A changed extraction or normalization
    #: algorithm is a changed instrument even when `config_hash` is identical, and
    #: until this field existed the receipt could not say so. See identity.py.
    impl_hash: str | None = None
    # Hash of the scorer's own verdict-affecting settings: a regex pattern, a numeric
    # tolerance. Two runs graded by different regexes used to fingerprint identically.
    config_hash: str | None = None
    judge: TargetProvenance | None = None  # the judge is a target too
    rubric_hash: str | None = None
    # The rubric is what a user edits; the judge prompt is what the model actually saw,
    # including the instruction wrapper. Sealing both means a wrapper change is visible.
    judge_prompt_hash: str | None = None
    judge_prompt: str | None = None        # only when --store-judge-prompt is passed


class SuiteProvenance(BaseModel):
    name: str | None = None
    path: str | None = None
    hash: str | None = None
    # Version 2 suites split the file into sections. These digest the two that matter
    # for reading a difference: the grading setup and the thing being measured. The
    # whole-file `hash` cannot tell them apart, which is why it is not fingerprinted.
    # None for a flat-format suite, which has no sections.
    evaluator_hash: str | None = None
    target_hash: str | None = None


class CodeProvenance(BaseModel):
    commit: str | None = None
    dirty: bool | None = None              # True means the commit does not describe the run


class CIProvenance(BaseModel):
    """What the environment said about the CI job, sealed as a claim.

    `claimed` is the honest name for the whole object, and `require_ci_claim` is the
    honest name for the rule that checks it. **This is a claim, never proof.** Every
    field is an environment variable, and a laptop can export the same ones:
    `GITHUB_ACTIONS=true evalseal run` produces a receipt that claims CI.

    So it catches a receipt produced locally and attached to a pull request by accident
    or convenience. It does not stop anyone who sets the variable on purpose. A policy
    that wants evidence has to lean on `run_url` being resolvable and on the ledger
    being signed by a key the CI job holds and a developer does not.
    """
    claimed: bool = False
    provider: str | None = None            # github_actions | gitlab_ci | ... | unknown
    run_id: str | None = None
    run_url: str | None = None             # what a third party can open and check
    repository: str | None = None
    ref: str | None = None
    commit: str | None = None
    event: str | None = None


class EnvironmentProvenance(BaseModel):
    evalseal_version: str | None = None
    python_version: str | None = None
    platform: str | None = None
    implementation: str | None = None
    ci: CIProvenance | None = None         # None means: no CI markers in the environment


ArtifactKind = Literal["hashed", "embedded", "external", "mutable"]


class ArtifactProvenance(BaseModel):
    """A file the run depended on, bound to the receipt by hash.

    `kind` says what the receipt holds, so a reader is never left guessing:
      hashed   - the digest is here, the bytes are not. The default, and the only kind
                 that is safe for a file holding prompts or responses.
      embedded - the content itself is in the receipt, because someone asked for it.
      external - the file was named but not readable when the record was sealed, so
                 there is nothing to check it against.
      mutable  - hashed, but the file is one later runs append to or re-draw. A
                 mismatch here says the file moved on, which is not the same claim as
                 "the bytes behind this receipt were altered" and must not be reported
                 as one. The live cassette is the case this exists for: its immutable
                 copy is the `cassette_snapshot` artifact, and a mismatch *there* is a
                 tamper signal. See docs/adr/0002-cassette-writer-strategy.md.
    """
    role: str                              # cassette | dataset | suite | ...
    kind: ArtifactKind = "hashed"
    sha256: str | None = None
    path: str | None = None
    note: str | None = None


class ImportProvenance(BaseModel):
    """A receipt whose numbers were read from another tool's export. From 1.9.

    Its presence is the claim: **nothing here was measured by EvalSeal.** No provider
    was contacted, no cassette was replayed, and the timings in `execution` are when
    the import ran. `origin` is `imported` on every call for the same reason.

    `gaps` is the load-bearing field. It names every EvalSeal field the export could not
    fill, so a reader can see what the receipt does not know rather than inferring it
    from a null. Two receipts with the same gaps are two receipts that both cannot
    answer the same questions - which is not the same as agreeing on the answers.
    """
    #: The producing tool, lowercase: `promptfoo`.
    tool: str
    #: The version the export says produced it, read from the export itself. None when
    #: the export does not record one - which is a gap, not a default.
    tool_version: str | None = None
    #: The export's own schema version, where it has one.
    export_schema: str | None = None
    source_path: str | None = None
    #: The digest of the exact bytes that were read. This is what `verify --artifacts`
    #: re-checks, and all it establishes is that the retained file still matches.
    source_sha256: str
    source_bytes: int
    imported_at: str
    #: EvalSeal fields this export could not fill, by name, each with why.
    gaps: list[str] = Field(default_factory=list)
    #: Timestamps the export asserts, kept as the claims they are. Never used as
    #: measurement time: a string in a file the author controls is not a clock.
    claimed_times: dict[str, str] = Field(default_factory=dict)


class DatasetProvenance(BaseModel):
    hash: str
    n_cases: int
    path: str | None = None
    case_ids: list[str] = Field(default_factory=list)


class CallProvenance(BaseModel):
    """One provider call, bound to the measurement it was made for.

    **Everything a provider reports here is that provider's claim.** `served_model` and
    `reported_fingerprint` are strings it chose to send. They are not proof of which
    weights answered, not proof the request was served at all, and not proof that no
    other call happened. What they are good for is noticing a *change*: a fingerprint
    that moves mid-run says the backend moved under you, which is worth seeing even
    though neither value can be checked from outside.

    `request_digest` and `response_digest` are ours, not the provider's: the canonical
    digests of the request as keyed and the response bytes this call used. On a replay
    the response digest is of the cassette's bytes, because those are the bytes that
    produced the score.
    """
    case_id: str
    repeat: int
    role: CallRole
    #: Ordinal of this call within its (case, repeat, role), from 0. Normally one call,
    #: but a scorer that asks twice must not have the second overwrite the first.
    sequence: int = 0
    origin: CallOrigin = "unknown"
    request_digest: str | None = None
    response_digest: str | None = None
    provider_request_id: str | None = None     # the provider's claim
    requested_model: str
    served_model: str | None = None            # the provider's claim
    reported_fingerprint: str | None = None    # the provider's claim
    status: CallStatus = "complete"
    #: HTTP attempts this call took. 0 means none was made - it was replayed, or served
    #: by an in-process callable. 1 means it succeeded first time.
    attempts: int = 0
    #: The cassette entry this call was served by or recorded into. From 1.8.
    #:
    #: It is the only field that can tell two independent draws which happened to
    #: return identical text from one recorded draw reused twice: the texts are equal
    #: either way, the entries are not. None means no cassette was involved (an
    #: in-process callable) or the receipt predates 1.8 - in which case independence is
    #: **unknown**, and is reported as unknown rather than assumed. See
    #: docs/metrics.md §12.
    source_key: str | None = None


class ExecutionProvenance(BaseModel):
    """What happened when the numbers were produced, as opposed to what was configured.

    The summary fields are sealed in the record and the per-call detail with them, so a
    count that disagrees with its own calls is a validation failure rather than a thing
    a reader has to notice.
    """
    #: This run. Not a claim about uniqueness across machines - it is a handle for
    #: keying measurements and for telling two runs of one suite apart.
    session_id: str
    #: The origins of every call, collapsed: one value when they agree, `mixed` when
    #: they do not. A `mixed` run is not a live run, which is the point of saying so.
    origin: ExecutionOrigin
    #: Around the calls themselves, not around the process: a receipt that counts
    #: argument parsing and report writing as measurement time overstates both.
    started_at: str
    ended_at: str
    n_calls: int = 0
    n_live: int = 0
    n_replayed: int = 0
    #: Total HTTP attempts across every call. Higher than `n_live` means retries.
    attempts: int = 0
    n_truncated: int = 0
    calls: list[CallProvenance] = Field(default_factory=list)


class RunConfig(BaseModel):
    n_repeats: int = 5
    concurrency: int = 1
    harness_version: str = "evalseal/2.2.0"
    started_at: str = Field(default_factory=_now)


class ProvenanceManifest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    target: TargetProvenance
    scorer: ScorerProvenance
    dataset: DatasetProvenance
    run_config: RunConfig
    suite: SuiteProvenance | None = None
    code: CodeProvenance = Field(default_factory=CodeProvenance)
    environment: EnvironmentProvenance = Field(default_factory=EnvironmentProvenance)
    # The files this run depended on, by digest. The cassette is the one that matters:
    # it holds the responses the verdicts came from, and until 1.4 nothing bound it to
    # the receipt, so the responses could be swapped without the receipt noticing.
    artifacts: list[ArtifactProvenance] = Field(default_factory=list)
    #: From 1.7. None means this receipt keeps no execution detail - which is what every
    #: receipt sealed before 1.7 looks like, and is why it is nullable rather than
    #: defaulted to an empty manifest that would read as "nothing happened".
    execution: ExecutionProvenance | None = None
    #: From 1.9. Present only on a receipt built by an importer; None means this run was
    #: measured here. See ImportProvenance.
    imported: ImportProvenance | None = None


class CaseResult(BaseModel):
    case_id: str
    scores: list[float]
    mean: float
    ci95: tuple[float, float]
    flip_rate: float
    stability: str                         # STABLE | BORDERLINE | UNSTABLE (unchanged)
    stability_label: Stability = "insufficient_runs"   # the finer taxonomy
    majority_verdict: int | None = None
    seconds: float = 0.0
    # Per-case verdict detail. `verdicts` is the observed sequence in run order, so a
    # reader can see *when* a case flipped rather than only how often.
    verdicts: list[int] = Field(default_factory=list)
    pass_count: int = 0
    fail_count: int = 0
    other_count: int = 0                   # non-binary or unparseable outcomes
    flip_count: int = 0                    # verdicts disagreeing with the majority
    #: Repeats the grader could not reach a verdict on. From 1.8. They keep their 0.0
    #: in `mean` - a run must not score better by becoming less legible - and are left
    #: out of `flip_rate`, `ci95` and `stability`, because a repeat with no verdict
    #: cannot disagree with a majority. See docs/metrics.md §8.
    unscorable_count: int = 0
    #: Why, for the first unscorable repeat. One sentence, no response text.
    unscorable_reason: str | None = None

    @property
    def n_runs(self) -> int:
        return len(self.scores)


class Aggregate(BaseModel):
    n_cases: int
    mean_score: float
    n_stable: int
    n_borderline: int
    n_unstable: int
    warnings: list[str] = []
    #: Repeats across the whole run that could not be graded, and the total number of
    #: repeats they are out of. From 1.8. Both, because a count with no denominator
    #: cannot be read: 12 unscorable is a detail at 10,000 repeats and the whole story
    #: at 20.
    n_unscorable: int = 0
    n_observations: int = 0


class RunRecord(BaseModel):
    manifest: ProvenanceManifest
    results: list[CaseResult]
    aggregate: Aggregate
    prev_hash: str
    hash: str = ""            # filled by the ledger at seal time
    created_at: str = Field(default_factory=_now)
