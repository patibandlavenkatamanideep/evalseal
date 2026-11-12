from __future__ import annotations

import contextlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as rich_escape
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from .adapters.dataset import Dataset
from .adapters.recording import Cassette, CassetteBusy, CassetteFormatError
from .adapters.scorer import (
    AnswerMatchScorer,
    ExactMatchScorer,
    LLMJudgeScorer,
    RegexScorer,
)
from .adapters.target import AnthropicTarget, OpenAICompatibleTarget
from .agent_artifacts import validate_agent_artifact
from .anchor import (
    AnchorError,
    anchor_passed,
    available_backends,
    build_anchor,
    check_artifacts,
    register_backend,
    temporary_backends,
    verify_anchor,
    write_anchor,
)
from .config import (
    AnthropicTargetConfig,
    ConfigError,
    judge_config,
    load_scorer_config,
    load_target_config,
)
from .decompose import decompose as run_decompose
from .decompose import render as render_decomposition
from .diffing import ReceiptError, diff_records, load_receipt, mean_flip_rate
from .drift import analyze_drift
from .drift import render as render_drift
from .evidence import CAVEAT as EVIDENCE_CAVEAT
from .evidence import (
    DEFAULT_CONFIDENCE,
    DEFAULT_TARGET_MARGIN,
    CaseEvidence,
    analyze_evidence,
)
from .evidence import render as render_evidence
from .executor import run_eval
from .htmlreport import write_diff_html, write_html
from .independence import audit_independence
from .inputs import InputSnapshot, capture_all
from .ledger import (
    LEDGER_PATH,
    config_fingerprint,
    evaluator_fingerprint,
    explain_fingerprint_mismatch,
    last_hash,
    load_all,
    seal_and_append,
    verify_chain,
)
from .models import FailOn, RunRecord, SuiteProvenance
from .policy import Check, PolicyError, evaluate, load_policy
from .power import (
    BERNOULLI,
    DETERMINISTIC,
    METRIC,
    NotBinary,
    analytic_deterministic_items,
    estimate,
    estimate_from_record,
    min_discordant_items,
    required_items,
    required_repeats,
)
from .prereg import (
    AnchorState,
    PreregError,
    build_preregistration,
    check_preregistration,
    load_preregistration,
)
from .provenance import text_hash
from .report import (
    case_rows,
    failing_cases,
    independence_line,
    origin_line,
    render_diff,
    to_case_table,
    to_markdown,
    verdict_sequence,
    write_json,
    write_junit,
    write_markdown,
)
from .rfc3161 import Rfc3161Backend
from .signing import (
    generate_keypair,
    sign_head,
    signatures_path,
    verify_signatures,
)
from .transport import MAX_RESPONSE_BYTES
from .validation import InvalidInput
from .verification import (
    DEFAULT_APPROVED_BACKENDS,
    ExternalEvidence,
    ExternalTrust,
    load_verified,
    verify_external_evidence,
    verify_receipt_signature,
)
from .verification import FAILED as EXTERNAL_FAILED

# Distinct from 1 (uncaught error) and 2 (usage error) so CI can tell
# "the eval is unstable" apart from "the tool broke".
EXIT_UNSTABLE = 3
EXIT_BAD_INPUT = 2       # the file you passed is not the file this command wanted
EXIT_INTERRUPTED = 130   # conventional 128 + SIGINT

app = typer.Typer(add_completion=False, help="Reproducibility receipts for LLM evals.")


def _utf8_stream(stream: object) -> None:
    """Make a standard stream carry UTF-8, replacing anything it cannot encode.

    The report prints "⚠" and "·". When stdout is a terminal Python usually picks an
    encoding that can carry them, but when it is redirected it falls back to the locale
    encoding, which on Windows is cp1252 - so `evalseal run > out.txt` died with
    UnicodeEncodeError while the same command in a terminal worked, and so did any CI
    step that captured the output. report.py already writes every file as UTF-8 for
    exactly this reason; this is the console half of the same decision.

    errors="replace" rather than raising: a character a stream cannot show is a
    presentation problem, and losing a report over it is worse than a "?".
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:      # pytest's capture objects, and any non-TextIOWrapper
        return
    # Already detached or closed: nothing to reconfigure, and nothing to report.
    with contextlib.suppress(ValueError, OSError):
        reconfigure(encoding="utf-8", errors="replace")


_utf8_stream(sys.stdout)
_utf8_stream(sys.stderr)
console = Console()

LedgerOpt = typer.Option(LEDGER_PATH, "--ledger", help="Path to the ledger JSONL file.")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from ./.env. Variables already in the environment win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("\"'")
        if key and value:
            os.environ.setdefault(key, value)


def _show_version(value: bool) -> None:
    """Print the version that would be sealed into a receipt, and exit.

    Two strings, because they can disagree and the difference matters: `__version__` is
    what this build says it is, and `harness_version` is what a receipt produced by it
    records. An editable checkout whose metadata lags is exactly when someone needs to
    see both.
    """
    if not value:
        return
    from . import __version__
    from .models import SCHEMA_VERSION, RunConfig

    console.print(
        f"evalseal {__version__}\n"
        f"sealed as {RunConfig().harness_version} · record schema {SCHEMA_VERSION}"
    )
    raise typer.Exit(code=0)


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_show_version, is_eager=True,
        help="Show the version and the record schema it seals, then exit."),
) -> None:
    load_dotenv()


_FLAT_SUITE_KEYS = {
    "dataset", "target", "scorer", "n_repeats", "concurrency", "cassette",
    "fail_on", "max_retries", "timeout", "ledger", "junit_xml", "sign_key",
    "max_response_bytes", "deadline",
}
_PATH_KEYS = ("dataset", "target", "scorer", "cassette", "ledger", "junit_xml", "sign_key")

# Version 2 splits the file into sections so a reader can see which parts describe the
# instrument and which describe the thing being measured. See docs/suite-format.md.
_V2_SECTIONS = {"version", "evaluator", "target", "dataset", "cases", "run", "policy"}
_V2_KEYS: dict[str, set[str]] = {
    "evaluator": {"scorer"},
    "target": {"config"},
    "dataset": {"path"},
    "cases": {"only"},
    "run": {"n_repeats", "concurrency", "cassette", "fail_on", "max_retries", "timeout",
            "ledger", "junit_xml", "sign_key", "max_response_bytes", "deadline"},
    "policy": {"file"},
}


def _flatten_v2(suite: dict, path: Path) -> dict:
    """Turn a sectioned suite into the flat mapping the rest of the CLI already uses.

    One internal shape, two file formats. Translating at the edge keeps every command
    below this line unchanged, which is what makes supporting both cheap enough to be
    worth doing rather than a migration nobody finishes.
    """
    unknown = set(suite) - _V2_SECTIONS
    if unknown:
        raise typer.BadParameter(
            f"unknown section(s) in {path}: {', '.join(sorted(unknown))}")
    flat: dict = {}
    for section, allowed in _V2_KEYS.items():
        body = suite.get(section)
        if body is None:
            continue
        # `"cases": ["a", "b"]` is sugar for `{"only": [...]}`. A bare list is the
        # obvious thing to write, and rejecting it would be pedantry; anything else
        # being a list is a mistake worth naming.
        if section == "cases" and isinstance(body, list):
            body = {"only": body}
        if not isinstance(body, dict):
            raise typer.BadParameter(f"{path}: section {section!r} must be a mapping")
        extra = set(body) - allowed
        if extra:
            raise typer.BadParameter(
                f"unknown key(s) in {path} section {section!r}: {', '.join(sorted(extra))}")
        flat.update(body)
    # Section-local names, mapped onto the flat ones the commands read.
    if "config" in flat:
        flat["target"] = flat.pop("config")
    if "path" in flat:
        flat["dataset"] = flat.pop("path")
    if "file" in flat:
        flat["policy"] = flat.pop("file")
    return flat


def _load_suite(path: Path | None) -> dict:
    """A suite file names the dataset, target, scorer and run settings in one place.

    Two formats are accepted. Version 2 is sectioned; anything without a `version` key
    is the original flat format and keeps working unchanged, because a file format
    that breaks on upgrade is a reason not to upgrade.

    Explicit flags still win, so a suite is a default, not a cage.
    """
    if path is None:
        return {}
    suite = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(suite, dict):
        raise typer.BadParameter(f"{path} must contain a mapping at the top level")

    if suite.get("version") == 2:
        suite = _flatten_v2(suite, path)
    elif "version" in suite:
        raise typer.BadParameter(
            f"{path}: unknown suite version {suite['version']!r}; expected 2, or omit "
            "the key for the original flat format")
    else:
        unknown = set(suite) - _FLAT_SUITE_KEYS
        if unknown:
            raise typer.BadParameter(
                f"unknown key(s) in {path}: {', '.join(sorted(unknown))}")

    base = path.parent

    def resolve(value: str) -> str:      # paths are relative to the suite file
        return str((base / value).resolve()) if value else value

    for key in (*_PATH_KEYS, "policy"):
        if key in suite:
            suite[key] = resolve(suite[key])
    return suite


def _suite_section_hashes(path: Path | None,
                          data: bytes | None = None) -> dict[str, str | None]:
    """Digest the evaluator and target sections separately.

    Why separate: the whole-file suite hash cannot go into the evaluator fingerprint,
    because the file also carries the target model, and swapping the model under test
    is the reason to run a benchmark. Hashing the sections apart records what the
    whole-file hash cannot distinguish - "the grading changed" from "the model
    changed" - and `evalseal diff` can then name which one moved.

    These digests are recorded, not fingerprinted. Scheme 2 already covers the
    evaluator's substance through the scorer config, judge identity, rubric and prompt
    template hashes, so adding this would change every existing pin without covering
    anything new. See docs/suite-format.md.
    """
    out: dict[str, str | None] = {"evaluator_hash": None, "target_hash": None}
    # `data` when the caller has already read the file, so the sections hashed here and
    # the whole-file digest in the receipt come from one read of it.
    if data is None:
        if path is None:
            return out
        try:
            data = path.read_bytes()
        except OSError:
            return out
    try:
        suite = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return out
    if not isinstance(suite, dict) or suite.get("version") != 2:
        return out          # the flat format has no sections to hash apart
    for section, field in (("evaluator", "evaluator_hash"), ("target", "target_hash")):
        if section in suite:
            out[field] = text_hash(
                json.dumps(suite[section], sort_keys=True, separators=(",", ":")))
    return out


_RESERVED_ROLES = {"cassette", "dataset", "suite"}

# Shown beside `anchor --list-backends`, so nobody reaches for an experimental
# backend without being told which half of its check EvalSeal actually performs.
_BACKEND_NOTES = {
    "local": "   attests nothing: binds files together, establishes no time",
    "rfc3161": " needs --tsa URL to submit. Verification checks the signature when "
               "you supply a root with --tsa-ca, and the imprint alone without one. "
               "Revocation is never checked",
}


def _parse_artifacts(pairs: list[str]) -> dict[str, str]:
    """Turn `--artifact role=path` into the mapping `run_eval` seals.

    A missing file is not rejected here: `run_eval` seals it as `external` with a note
    saying it was unreadable, which is the honest record. Refusing the run would push
    people towards not binding the file at all.
    """
    out: dict[str, str] = {}
    for pair in pairs:
        role, sep, path = pair.partition("=")
        role, path = role.strip(), path.strip()
        if not sep or not role or not path:
            raise typer.BadParameter(
                f"--artifact expects ROLE=PATH, got {pair!r} "
                "(for example: --artifact tool_acl=tools.json)")
        if role in _RESERVED_ROLES:
            raise typer.BadParameter(
                f"--artifact role {role!r} is already sealed by the run itself; "
                "pick another name so the two cannot be confused")
        if role in out:
            raise typer.BadParameter(f"--artifact role {role!r} given twice")
        out[role] = path
    return out


def _check_agent_artifacts(captured: dict[str, InputSnapshot]) -> None:
    """Fail closed on a malformed agent artifact, judging the bytes that were read.

    Same reason an `external` artifact fails closed: a digest over a broken file
    produces a receipt that verifies perfectly and describes an environment nobody can
    reconstruct. The bytes come from the capture rather than from a second read of the
    file, so the file this validates is the file whose digest is sealed.
    """
    problems = [
        (snapshot.role, problem)
        for snapshot in captured.values()
        for problem in validate_agent_artifact(snapshot.role, snapshot.path, snapshot.data)
    ]
    if problems:
        listed = "\n".join(f"  {role}: {problem}" for role, problem in problems)
        raise typer.BadParameter(
            f"{len(problems)} problem(s) in the agent artifact(s) you are sealing:\n"
            f"{listed}\nThe schema is in docs/agent-eval-receipts.md. EvalSeal checks "
            "the shape of these files, not whether the agent really made these calls.")


def _build_target(
    cfg: dict, cassette: Cassette, max_retries: int, timeout: float,
    prefix: str = "", max_bytes: int = MAX_RESPONSE_BYTES,
    deadline: float | None = None,
) -> AnthropicTarget | OpenAICompatibleTarget:
    """Build a target from config, rejecting anything this build would not transmit.

    The config is validated by `config.py` first. It used to be read key by key with
    `cfg.get(...)`, which meant `top_p` and `max_tokens` were silently dropped for an
    OpenAI-compatible target, and a misspelled key was indistinguishable from a
    decision not to set it.

    `prefix` lets the judge reuse this with its own `judge_*` keys, so a judge is
    configured exactly like a target rather than through a second, smaller vocabulary.
    """
    if prefix:
        # The judge's keys are translated into target vocabulary, so one schema
        # governs both and the judge cannot accept a key a target would reject.
        try:
            scorer = load_scorer_config(cfg, what="the scorer config")
        except ConfigError as e:
            raise typer.BadParameter(str(e)) from None
        cfg = judge_config(scorer)

    try:
        validated = load_target_config(
            cfg, what="the judge config" if prefix else "the target config")
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from None

    if isinstance(validated, AnthropicTargetConfig):
        return AnthropicTarget(
            model=validated.model,
            cassette=cassette,
            base_url=validated.base_url,
            max_tokens=validated.max_tokens,
            temperature=validated.temperature,
            top_p=validated.top_p,
            system=validated.system,
            api_key_env=validated.api_key_env,
            max_retries=max_retries,
            timeout=timeout,
            max_response_bytes=max_bytes,
            deadline=deadline,
        )
    return OpenAICompatibleTarget(
        model=validated.model,
        cassette=cassette,
        base_url=validated.base_url,
        temperature=validated.temperature,  # omit to surface the "unset" warning
        top_p=validated.top_p,
        max_tokens=validated.max_tokens,
        seed=validated.seed,
        system=validated.system,
        api_key_env=validated.api_key_env,
        max_retries=max_retries,
        timeout=timeout,
        max_response_bytes=max_bytes,
        deadline=deadline,
    )


def _build_scorer(cfg: dict, cassette: Cassette, max_retries: int, timeout: float,
                  max_bytes: int = MAX_RESPONSE_BYTES, deadline: float | None = None):
    """Build a scorer from config, rejecting keys this build does not read."""
    try:
        validated = load_scorer_config(cfg)
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from None

    if validated.type == "exact":
        return ExactMatchScorer()
    if validated.type == "regex":
        if validated.pattern is None:
            raise typer.BadParameter("a regex scorer needs a `pattern`")
        return RegexScorer(pattern=validated.pattern)
    if validated.type == "answer_match":
        return AnswerMatchScorer(
            tolerance=1e-6 if validated.tolerance is None else validated.tolerance)
    if validated.type == "llm_judge":
        if validated.rubric is None:
            raise typer.BadParameter("an llm_judge scorer needs a `rubric`")
        judge = _build_target(cfg, cassette, max_retries, timeout, prefix="judge_",
                              max_bytes=max_bytes, deadline=deadline)
        return LLMJudgeScorer(judge=judge, rubric=validated.rubric)
    raise typer.BadParameter(f"unknown scorer type {validated.type!r}")


@app.command()
def run(
    suite: Path | None = typer.Option(
        None, exists=True, dir_okay=False,
        help="JSON suite file supplying any of the options below; flags override it.",
    ),
    dataset: Path | None = typer.Option(None, exists=True, dir_okay=False),
    target_config: Path | None = typer.Option(None, exists=True, dir_okay=False),
    scorer_config: Path | None = typer.Option(None, exists=True, dir_okay=False),
    n: int | None = typer.Option(None, min=1, help="Repeats per case (flip rate needs N>=5)."),
    cassette: Path | None = typer.Option(None),
    cassette_mode: str | None = typer.Option(
        None, "--cassette-mode",
        help="replay (no provider calls; a miss fails) | resume (reuse what is "
             "recorded, call for the rest) | fresh (ignore what is recorded and draw "
             "again). Defaults to $EVALSEAL_MODE, or replay unless EVALSEAL_RECORD=1.",
    ),
    ledger: Path = LedgerOpt,
    concurrency: int | None = typer.Option(None, min=1, help="Parallel requests in flight."),
    max_retries: int | None = typer.Option(None, min=0, help="Retries per request on 429/5xx."),
    max_response_bytes: int | None = typer.Option(
        None, "--max-response-bytes", min=1024,
        help="Most bytes read from one provider response, after decompression. "
             f"Default {MAX_RESPONSE_BYTES // 1024 // 1024} MiB. A larger body is an "
             "error, never a truncation."),
    deadline: float | None = typer.Option(
        None, "--deadline", min=1.0,
        help="Total seconds for one provider call including its retries and backoff. "
             "Derived from --timeout and --max-retries when unset, and enforced "
             "either way."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress the progress bar."),
    timeout: float | None = typer.Option(None, min=1.0, help="Per-request timeout in seconds."),
    fail_on: FailOn | None = typer.Option(
        None, help="Which stability classes fail the run: none | unstable | borderline."
    ),
    junit_xml: Path | None = typer.Option(
        None, help="Also write JUnit XML here, for CI test reporting."
    ),
    html: Path | None = typer.Option(
        None, "--html", help="Also write a self-contained HTML receipt here."
    ),
    sign_key: Path | None = typer.Option(
        None, help="Ed25519 private key; signs the sealed record after the run."
    ),
    only: str | None = typer.Option(
        None, help="Run only these case ids (comma-separated), e.g. to re-examine a flip."
    ),
    show_cases: bool = typer.Option(
        False, "--show-cases", help="Print the per-case verdict table."
    ),
    unstable_only: bool = typer.Option(
        False, "--unstable-only", help="With --show-cases, list only cases that flipped."
    ),
    store_judge_prompt: bool = typer.Option(
        False, "--store-judge-prompt",
        help="Seal the judge prompt verbatim, not only its hash. It embeds case text.",
    ),
    out_dir: Path | None = typer.Option(
        None, "--out-dir",
        help="This run's output directory. Defaults to runs/<timestamp>-<session>, one "
             "per run, so two runs cannot overwrite each other's report. report.json "
             "and report.md in the working directory are also written, as aliases for "
             "the latest run.",
    ),
    snapshot: bool = typer.Option(
        True, "--snapshot/--no-snapshot",
        help="Copy the cassette entries this run used into the output directory and "
             "bind them into the receipt, so a later run recording into the same "
             "cassette cannot invalidate the bytes behind this receipt.",
    ),
    artifact: list[str] = typer.Option(
        [], "--artifact", metavar="ROLE=PATH",
        help="Bind another file into the receipt by digest, as `tool_acl=tools.json`. "
             "Repeatable. For agent evals this is how the tool ACL and the frozen tool "
             "responses are sealed; see docs/agent-eval-receipts.md.",
    ),
):
    """Run an eval N times, seal the result, emit report.json + report.md."""
    extra_artifacts = _parse_artifacts(artifact)
    cfg = _load_suite(suite)
    dataset = dataset or (Path(cfg["dataset"]) if "dataset" in cfg else None)
    target_config = target_config or (Path(cfg["target"]) if "target" in cfg else None)
    scorer_config = scorer_config or (Path(cfg["scorer"]) if "scorer" in cfg else None)
    if dataset is None or target_config is None or scorer_config is None:
        missing = [
            name for name, value in
            (("--dataset", dataset), ("--target-config", target_config),
             ("--scorer-config", scorer_config))
            if value is None
        ]
        raise typer.BadParameter(f"missing {', '.join(missing)} (or supply them via --suite)")
    n = n or cfg.get("n_repeats", 5)
    cassette = cassette or Path(cfg.get("cassette", "tests/cassettes/run.json"))
    concurrency = concurrency or cfg.get("concurrency", 4)
    max_retries = max_retries if max_retries is not None else cfg.get("max_retries", 5)
    timeout = timeout or cfg.get("timeout", 60.0)
    max_response_bytes = (max_response_bytes
                          or cfg.get("max_response_bytes", MAX_RESPONSE_BYTES))
    deadline = deadline or cfg.get("deadline")
    fail_on = fail_on or cfg.get("fail_on", "unstable")
    junit_xml = junit_xml or (Path(cfg["junit_xml"]) if "junit_xml" in cfg else None)
    sign_key = sign_key or (Path(cfg["sign_key"]) if "sign_key" in cfg else None)

    # Read every input once, here, and use what was read from here on. An input that
    # was used at the start and hashed at the end left receipts sealing bytes that
    # produced no score - and `verify --artifacts` then reported `ok` against the file
    # nothing ran on. See evalseal/inputs.py.
    captured = capture_all({
        "dataset": str(dataset),
        **({"suite": str(suite)} if suite else {}),
        **extra_artifacts,
    })
    _check_agent_artifacts(captured)
    ds_bytes = captured["dataset"].data
    if ds_bytes is None:
        raise typer.BadParameter(f"cannot read the dataset {dataset}")
    ds = Dataset.from_bytes(ds_bytes, what=str(dataset))
    # A v2 suite's `cases.only` is a list; --only is a comma-separated string. The flag
    # wins, as it does for every other suite key.
    wanted_ids = ([c.strip() for c in only.split(",") if c.strip()] if only
                  else list(cfg.get("only") or []))
    if wanted_ids:
        wanted = wanted_ids
        missing = [c for c in wanted if c not in {case.case_id for case in ds.cases}]
        if missing:
            raise typer.BadParameter(f"case id(s) not in {dataset}: {', '.join(missing)}")
        ds = Dataset([c for c in ds.cases if c.case_id in wanted], ds.hash)
    # Named before the run, because the receipt binds files that go in here and the
    # receipt is sealed before anything is written out.
    session_id = uuid.uuid4().hex
    run_dir = out_dir or Path("runs") / (
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{session_id[:8]}")
    try:
        cass = Cassette(cassette, mode=cassette_mode)
    except CassetteBusy as e:
        # Two runs sharing a cassette path used to lose data silently: each holds the
        # whole table in memory and rewrites the whole file, so the last one to finish
        # erased the other's responses. See docs/adr/0002-cassette-writer-strategy.md.
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    except CassetteFormatError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    except ValueError as e:                     # an unknown mode name
        raise typer.BadParameter(str(e)) from None
    target_cfg = json.loads(target_config.read_text(encoding="utf-8"))
    scorer_cfg = json.loads(scorer_config.read_text(encoding="utf-8"))
    target = _build_target(target_cfg, cass, max_retries, timeout,
                           max_bytes=max_response_bytes, deadline=deadline)
    scorer = _build_scorer(scorer_cfg, cass, max_retries, timeout,
                           max_bytes=max_response_bytes, deadline=deadline)

    total = len(ds.cases) * n
    show_progress = not quiet and sys.stderr.isatty()
    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total} calls"),
            TimeElapsedColumn(),
            console=Console(stderr=True),
            transient=True,
            disable=not show_progress,
        ) as progress:
            task = progress.add_task(f"{len(ds.cases)} cases x {n} runs", total=total)
            record = run_eval(
                ds, target, scorer,
                n_repeats=n,
                prev_hash=last_hash(ledger),
                concurrency=concurrency,
                on_unit_done=lambda: progress.advance(task),
                suite=SuiteProvenance(
                    name=suite.stem, path=str(suite),
                    hash=captured["suite"].sha256,
                    **_suite_section_hashes(suite, captured["suite"].data),
                ) if suite else None,
                dataset_path=str(dataset),
                store_judge_prompt=store_judge_prompt,
                # Hashed inside run_eval, once the units have finished: the cassette is
                # still being written while they run.
                artifact_paths={
                    "cassette": str(cassette),
                    "dataset": str(dataset),
                    **({"suite": str(suite)} if suite else {}),
                    **extra_artifacts,
                },
                captured=captured,
                session_id=session_id,
                snapshot_dir=run_dir if snapshot else None,
            )
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    except KeyboardInterrupt:
        console.print(
            "[yellow]Interrupted. Responses recorded so far are kept in the cassette; "
            "re-run the same command to resume.[/yellow]"
        )
        raise typer.Exit(code=EXIT_INTERRUPTED) from None
    except httpx.HTTPStatusError as e:
        resp = e.response
        console.print(f"[red]HTTP {resp.status_code} from provider: {resp.text[:300]}[/red]")
        if resp.status_code == 429:
            console.print(
                "[yellow]Rate limited after retries. Responses recorded so far are saved in the "
                "cassette; re-run the same command later to resume.[/yellow]"
            )
        raise typer.Exit(code=1) from e
    record = seal_and_append(record, ledger, relink=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(record, run_dir / "report.json")
    write_markdown(record, run_dir / "report.md")
    # The working-directory copies are aliases for the latest run, kept because every
    # existing README, workflow and script reads `report.json` from here. They are
    # overwritten by the next run - the per-run directory is the one to archive.
    write_json(record)
    write_markdown(record)
    if junit_xml is not None:
        write_junit(record, junit_xml, fail_on)
    if html is not None:
        write_html(record, html)
    if sign_key is not None:
        entry = sign_head(ledger, sign_key)
        console.print(
            f"[green]Signed record {entry['record_index']} "
            f"-> {signatures_path(ledger)}[/green]"
        )
    console.print(Markdown(to_markdown(record)))
    console.print(
        f"[dim]This run: {run_dir}/ (report.json, report.md"
        f"{', cassette snapshot' if snapshot else ''}). "
        f"./report.json and ./report.md are aliases for the latest run.[/dim]")
    if show_cases:
        console.print(Markdown(to_case_table(record, unstable_only=unstable_only)))

    # CI gate: dedicated non-zero exit when the policy is violated.
    failed = failing_cases(record, fail_on)
    if failed:
        ids = ", ".join(r.case_id for r in failed)
        console.print(
            f"[red]{len(failed)} case(s) fail --fail-on {fail_on}: {ids}[/red]"
        )
        raise typer.Exit(code=EXIT_UNSTABLE)


@app.command()
def verify(
    ledger: Path = LedgerOpt,
    public_key: str | None = typer.Option(
        None, "--public-key",
        help="Also require valid signatures, made by this key (path or base64).",
    ),
    signed: bool = typer.Option(
        False, "--signed", help="Require signatures, whoever made them."
    ),
    artifacts: bool = typer.Option(
        False, "--artifacts",
        help="Also re-hash the files the records were sealed against (cassette, dataset, "
             "suite) and report any that changed or are absent.",
    ),
    index: int = typer.Option(-1, help="Which record's artifacts to check; -1 = latest."),
):
    """Check the ledger chain integrity, and signatures or artifacts when asked."""
    try:
        ok, msg = verify_chain(ledger)
    except InvalidInput as e:
        # A malformed ledger is an input error with a documented exit code, not a
        # traceback. `verify` used to let pydantic's ValidationError reach the user.
        console.print(f"[red]Cannot read the ledger:[/red] {rich_escape(str(e))}")
        raise typer.Exit(code=EXIT_BAD_INPUT) from None
    console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
    if not ok:
        raise typer.Exit(code=1)
    # An intact chain does not mean every field in the record is covered by it. A
    # record sealed under an older schema is hashed with the fields that schema lacked
    # stripped out, so a value written into one of them leaves the chain intact and
    # this command saying so. Reporting "Chain intact" and nothing else is how that
    # value gets read as sealed evidence further down.
    unsealed = load_verified(ledger)
    for result in unsealed.results:
        if result.quarantined:
            console.print(
                f"[yellow]Unsealed field(s) in record {result.index}:[/yellow] "
                f"{rich_escape(', '.join(result.quarantined))} - present in the file "
                f"but not covered by the hash that just verified, because schema "
                f"{result.sealed_under} did not seal them. Nothing here vouches for "
                "their contents, and `gate` refuses them.")
    if public_key is not None or signed:
        sig_ok, sig_msg = verify_signatures(ledger, public_key)
        console.print(f"[{'green' if sig_ok else 'red'}]{sig_msg}[/{'green' if sig_ok else 'red'}]")
        if not sig_ok:
            raise typer.Exit(code=1)
    if artifacts:
        recs = load_all(ledger)
        if not -len(recs) <= index < len(recs):
            raise typer.BadParameter(
                f"index {index} out of range; ledger has {len(recs)} record(s)")
        results = check_artifacts(recs[index])
        if not results:
            console.print("[yellow]No artifacts are sealed in this record.[/yellow] "
                          "Records sealed before schema 1.4 bind none.")
        for r in results:
            colour = {"ok": "green", "changed": "red", "moved": "yellow",
                      "missing": "yellow", "unverifiable": "yellow"}[r.status]
            console.print(
                f"[{colour}]{r.status:<12}[/{colour}] {rich_escape(r.role)}: "
                f"{rich_escape(r.detail)}")
        if any(r.status == "changed" for r in results):
            raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@app.command()
def keygen(
    private_key: Path = typer.Option(Path("evalseal.key"), help="Where to write the private key."),
    public_key: Path = typer.Option(Path("evalseal.pub"), help="Where to write the public key."),
    overwrite_public: bool = typer.Option(
        False, "--overwrite-public",
        help="Replace an existing public key file. Needed when rotating: the private "
             "key path must still be new, and anyone verifying against the old public "
             "key has to be told. See docs/operations.md."),
):
    """Generate an Ed25519 keypair for signing ledgers.

    Neither file is overwritten by accident. The private key is created with
    owner-only permissions and without following a symbolic link, so it cannot be
    created world-readable for an instant or written somewhere other than the path
    you named.
    """
    try:
        pub = generate_keypair(private_key, public_key,
                               overwrite_public=overwrite_public)
    except (FileExistsError, OSError) as e:
        console.print(f"[red]{rich_escape(str(e))}[/red]")
        raise typer.Exit(code=1) from e
    mode = "" if os.name == "nt" else " (mode 600 — keep it secret)"
    console.print(f"[green]private key: {private_key}{mode}[/green]")
    if os.name == "nt":
        console.print(
            "[yellow]Windows inherits the parent directory's ACL; this file is only as "
            "private as the directory it is in.[/yellow]")
    console.print(f"[green]public key:  {public_key}[/green]\n{pub}")


@app.command()
def sign(
    ledger: Path = LedgerOpt,
    key: Path = typer.Option(Path("evalseal.key"), exists=True, help="Ed25519 private key."),
):
    """Sign the ledger head, so others can verify the chain came from you."""
    try:
        entry = sign_head(ledger, key)
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    console.print(
        f"[green]Signed record {entry['record_index']} ({entry['hash'][:20]}...)[/green]\n"
        f"signature -> {signatures_path(ledger)}\npublic key: {entry['public_key']}"
    )


def _case_evidence_block(ev: CaseEvidence | None) -> dict[str, object] | None:
    """The uncertainty view of one case, for `report --json`.

    A subset of what `evalseal evidence` prints: the fields a CI reviewer or dashboard
    acts on, without the repeat-count arithmetic that belongs to that command.
    """
    if ev is None:
        return None
    return {
        "label": ev.label,
        # Both margins, named. They are different numbers whenever the interval
        # straddles 0.5, and a consumer thresholding one must know which it has.
        "pass_rate_margin": ev.pass_rate_margin,
        "flip_rate_margin": ev.flip_rate_margin,
        "margin": ev.pass_rate_margin,
        "flip_rate_ci": list(ev.flip_rate_ci) if ev.flip_rate_ci else None,
        "n_graded": ev.n_graded,
        "underpowered": ev.underpowered,
        "action": ev.action,
    }


def _read_receipt(source: str | Path) -> RunRecord:
    """Load a receipt, or exit with a message that names what the file turned out to be.

    Handing `evalseal report --json` output to a command that wants a receipt is an
    ordinary mistake - both get called report.json - and an ordinary mistake must not
    print a stack trace at someone.
    """
    try:
        return load_receipt(source)
    except ReceiptError as e:
        console.print(f"[red]Not a receipt:[/red] {rich_escape(str(e))}")
        raise typer.Exit(code=EXIT_BAD_INPUT) from None


def _anchor_state(
    anchor_file: Path | None, record: RunRecord, ledger: Path | None
) -> AnchorState:
    """Verify an anchor for the pre-registration gate, reporting failures as failures.

    An unreadable or failing anchor produces `verified=False` with the reason, rather
    than an exception: `require_anchor` is a clause of a contract, and a clause that
    cannot be evaluated has to fail visibly like every other one.
    """
    if anchor_file is None:
        return AnchorState(detail="no anchor was supplied to check (pass --anchor)")
    try:
        anchor_obj = json.loads(anchor_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return AnchorState(present=True, detail=f"{anchor_file} could not be read: {e}")
    checks = verify_anchor(anchor_obj, record, ledger=ledger,
                           base_dir=anchor_file.parent)
    ok = anchor_passed(checks)
    failed = [c.name for c in checks if not c.passed and c.required]
    return AnchorState(
        present=True,
        verified=ok,
        detail=f"anchor verified ({anchor_file})" if ok
        else f"anchor failed: {', '.join(failed) or 'see anchor-verify'}",
    )


def _external_state(
    anchor_file: Path | None,
    *,
    tsa_ca: Path | None = None,
    approved: list[str] | None = None,
) -> ExternalEvidence | None:
    """Verify the anchor's external proofs under trust this invocation supplied.

    This replaces `bool(anchor["external_proofs"])`, which satisfied
    `require_external_anchor` for `[{}]`, for a backend that does not exist, and for
    the local backend that attests nothing. Returning None when there is no anchor to
    read makes the clause fail as "not checked" rather than pass.
    """
    if anchor_file is None:
        return None
    try:
        anchor_obj = json.loads(anchor_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return ExternalEvidence(
            EXTERNAL_FAILED, (),
            f"{anchor_file} could not be read, so no external proof was checked: {e}")
    if not isinstance(anchor_obj, dict):
        return ExternalEvidence(
            EXTERNAL_FAILED, (), f"{anchor_file} does not hold an anchor object")
    return verify_external_evidence(anchor_obj, trust=ExternalTrust(
        tsa_ca_pem=tsa_ca.read_bytes() if tsa_ca is not None else None,
        approved_backends=(tuple(approved) if approved
                           else DEFAULT_APPROVED_BACKENDS)))


def _resolve_record(token: str, ledger: Path) -> RunRecord:
    """A receipt path or a ledger index, whichever the user typed.

    Indices are resolved against the ledger only after the path lookup fails, so a file
    literally named `0` still wins - the filesystem is the less surprising reading.
    """
    path = Path(token)
    if path.exists():
        return _read_receipt(path)
    try:
        index = int(token)
    except ValueError:
        raise typer.BadParameter(
            f"{token!r} is neither an existing file nor a ledger index"
        ) from None
    recs = load_all(ledger)
    if not -len(recs) <= index < len(recs):
        raise typer.BadParameter(
            f"index {index} out of range; ledger has {len(recs)} record(s)"
        )
    return recs[index]


@app.command(context_settings={"ignore_unknown_options": True})
def diff(
    a: str = typer.Argument(..., help="Baseline: a receipt file, or a ledger index."),
    b: str = typer.Argument(..., help="Candidate: a receipt file, or a ledger index."),
    ledger: Path = LedgerOpt,
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    html: Path | None = typer.Option(
        None, "--html", help="Write a self-contained HTML drift report here."
    ),
):
    """Compare two runs: score, stability, and whether they are comparable at all.

    Both arguments accept either a path to a receipt (report.json) or an index into the
    ledger, so `evalseal diff baseline.json current.json` and `evalseal diff 0 -1` both
    work. Exit status is always 0: this command reports, it does not gate. Use
    `evalseal gate` to fail a build.
    """
    before = _resolve_record(a, ledger)
    after = _resolve_record(b, ledger)
    result = diff_records(before, after)

    if html is not None:
        write_diff_html(result, html)
        console.print(f"[green]HTML drift report -> {html}[/green]")
        if not as_json:
            raise typer.Exit(code=0)

    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        raise typer.Exit(code=0)

    console.print(Markdown(render_diff(result)))
    raise typer.Exit(code=0)


@app.command(context_settings={"ignore_unknown_options": True})
def report(
    source: str | None = typer.Argument(
        None, help="A receipt file or ledger index; defaults to the latest sealed record."
    ),
    ledger: Path = LedgerOpt,
    index: int = typer.Option(-1, help="Ledger index to report on; -1 = latest."),
    unstable_only: bool = typer.Option(
        False, "--unstable-only", help="List only cases that flipped."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    html: Path | None = typer.Option(
        None, "--html", help="Write a self-contained HTML receipt here."
    ),
):
    """Per-case verdict distribution for a sealed record.

    `evalseal report receipt.json --html out.html` and `evalseal report --index -2` both
    work: the positional argument takes a receipt path or a ledger index, and without one
    the latest sealed record is used.
    """
    if source is not None:
        record = _resolve_record(source, ledger)
    else:
        recs = load_all(ledger)
        if not -len(recs) <= index < len(recs):
            raise typer.BadParameter(
                f"index {index} out of range; ledger has {len(recs)} record(s)"
            )
        record = recs[index]

    if html is not None:
        write_html(record, html, unstable_only=unstable_only)
        console.print(f"[green]HTML receipt -> {html}[/green]")
        if not as_json:
            raise typer.Exit(code=0)

    if as_json:
        # Computed once for both the per-case blocks and the suite summary below.
        # Every key added here is new: a 2.2.0 consumer reading `flip_rate` or
        # `stability_label` sees exactly what it saw before, and a test asserts that.
        ev = analyze_evidence(record)
        by_case = {c.case_id: c for c in ev.cases}
        cases = [
            {
                "case_id": c.case_id,
                "runs": len(c.scores),
                "verdict_distribution": {
                    "pass": c.pass_count, "fail": c.fail_count, "other": c.other_count
                },
                "verdict_sequence": verdict_sequence(c),
                "flip_count": c.flip_count,
                "flip_rate": c.flip_rate,
                "majority_verdict": c.majority_verdict,
                "stability": c.stability,
                "stability_label": c.stability_label,
                "ci95": list(c.ci95),
                # How many of those repeats could not be graded. A 0.0 mean over five
                # ungraded repeats and one over five graded failures are the same
                # number and not the same result. docs/metrics.md §8.
                "unscorable_count": c.unscorable_count,
                "unscorable_reason": c.unscorable_reason,
                # How much the flip rate above is worth reading. `stability_label` says
                # which way the case goes; this says how precisely the rate is known.
                "evidence": _case_evidence_block(by_case.get(c.case_id)),
            }
            for c in case_rows(record, unstable_only)
        ]
        m = record.manifest
        payload = {
            "summary": {
                "mean_score": record.aggregate.mean_score,
                "flip_rate": mean_flip_rate(record),
                "n_cases": record.aggregate.n_cases,
                "n_stable": record.aggregate.n_stable,
                "n_unstable": record.aggregate.n_unstable,
                "n_unscorable": record.aggregate.n_unscorable,
                "n_observations": record.aggregate.n_observations,
                "sealed_hash": record.hash,
            },
            # How many independent draws are behind the numbers above. `unknown` for a
            # receipt that cannot say, which is every receipt sealed before 1.8.
            "independence": audit_independence(record).to_dict(),
            "uncertainty": {
                "label": ev.label,
                "confidence": ev.confidence,
                "target_margin": ev.target_margin,
                # Self-describing: a consumer reading this block should not have to
                # reach into `summary` to learn what the counts are out of.
                "n_cases": len(ev.cases),
                "n_underpowered": len(ev.underpowered),
                "underpowered_cases": [c.case_id for c in ev.underpowered],
                "min_graded_repeats_per_case": ev.min_runs,
                "worst_pass_rate_margin": ev.worst_margin,
                "worst_flip_rate_margin": ev.worst_flip_margin,
                "n_not_binary": len(ev.not_binary),
                "counts": ev.counts,
                "summary": ev.summary(),
                "caveat": EVIDENCE_CAVEAT,
            },
            "provenance": {
                # Both, so a pin can be copied from here: the evaluator fingerprint for
                # "comparable to this", the config fingerprint for "exactly this".
                "evaluator_fingerprint": evaluator_fingerprint(record),
                "config_fingerprint": config_fingerprint(record),
                "target_model": m.target.requested_model,
                "served_model": m.target.served_model,
                "scorer_type": m.scorer.type,
                "judge_model": m.scorer.judge.requested_model if m.scorer.judge else None,
                "judge_prompt_hash": m.scorer.judge_prompt_hash,
                "rubric_hash": m.scorer.rubric_hash,
                "dataset_hash": m.dataset.hash,
                "suite_hash": m.suite.hash if m.suite else None,
                "evalseal_version": m.environment.evalseal_version,
                "git_commit": m.code.commit,
                "git_dirty": m.code.dirty,
            },
            "cases": cases,
        }
        console.print_json(json.dumps(payload))
        raise typer.Exit(code=0)

    # The table alone left out the three things that decide what it means: what the
    # suite scored, how many of its observations were gradeable, and where the numbers
    # came from. A reviewer running this command should not have to also open
    # report.md to find out that a third of the repeats were never graded.
    agg = record.aggregate
    header = (f"**{agg.n_cases} case(s)** · mean **{agg.mean_score:.2f}** · "
              f"{agg.n_stable} stable / {agg.n_borderline} borderline / "
              f"{agg.n_unstable} unstable")
    if agg.n_observations:
        header += f" · {agg.n_observations} repeat(s)"
    if agg.n_unscorable:
        header += (f" · **{agg.n_unscorable} ungraded** "
                   f"({agg.n_unscorable / agg.n_observations:.1%})")
    console.print(Markdown(header))
    if (origin := origin_line(record)) is not None:
        console.print(Markdown(origin))
        console.print(Markdown(independence_line(record)))
    console.print(Markdown(to_case_table(record, unstable_only=unstable_only)))
    for warning in agg.warnings:
        console.print(f"[yellow]{rich_escape(warning)}[/yellow]")
    raise typer.Exit(code=0)


@app.command()
def gate(
    ledger: Path = LedgerOpt,
    index: int = typer.Option(-1, help="Ledger index to gate on; -1 = latest."),
    min_score: float | None = typer.Option(None, help="Fail if mean score is below this."),
    max_flip_rate: float | None = typer.Option(
        None, help="Fail if any case's flip rate exceeds this."
    ),
    critical: str | None = typer.Option(
        None, help="Comma-separated case ids that must not flip at all."
    ),
    expect_config: str | None = typer.Option(
        None,
        help="Fail unless the full config fingerprint equals this: what exactly ran, "
             "target model included. Stricter than --expect-evaluator.",
    ),
    expect_evaluator: str | None = typer.Option(
        None,
        help="Fail unless the evaluator fingerprint equals this: how the run was graded, "
             "which decides whether its score is comparable to a baseline.",
    ),
    verify_ledger: bool = typer.Option(
        True, help="Also require the hash chain to verify."
    ),
    policy: Path | None = typer.Option(
        None, "--policy", exists=True, dir_okay=False,
        help="A policy file (.json/.yml) of thresholds, critical cases and drift rules.",
    ),
    suite: Path | None = typer.Option(
        None, "--suite", exists=True, dir_okay=False,
        help="A version-2 suite file, whose `policy.file` names the policy to apply. "
             "--policy wins if both are given.",
    ),
    prereg: Path | None = typer.Option(
        None, "--prereg", exists=True, dir_okay=False,
        help="A pre-registration written by `evalseal preregister` before the run. "
             "Checks the receipt against the evaluation that was declared: suite, "
             "dataset, case set, repeat count, grading setup and required artifacts.",
    ),
    anchor_file: Path | None = typer.Option(
        None, "--anchor", exists=True, dir_okay=False,
        help="An anchor to check, when the pre-registration requires one.",
    ),
    trusted_key: list[str] = typer.Option(
        [], "--trusted-key", metavar="KEY",
        help="Public key(s) to trust for `require_signature`, overriding the "
             "contract's own list. Supplied by the verifier, because a key the "
             "producer chose establishes nothing. Repeatable.",
    ),
    tsa_ca: Path | None = typer.Option(
        None, "--tsa-ca", exists=True, dir_okay=False,
        help="PEM trust root for an RFC 3161 proof, for `require_external_anchor`. "
             "Without it a timestamp token's signature cannot be checked, so the "
             "proof counts as imprint-only and the requirement fails.",
    ),
    approved_backend: list[str] = typer.Option(
        [], "--approved-backend", metavar="NAME",
        help="Anchor backend(s) you accept as sources of external evidence "
             f"(default: {', '.join(DEFAULT_APPROVED_BACKENDS)}). `local` is never "
             "accepted: it attests nothing. Repeatable.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit every check as machine-readable JSON."
    ),
):
    """Apply CI thresholds to a sealed record. Exit 3 means the gate failed.

    Thresholds come from flags, from `--policy evalseal.yml`, or both: the two sets are
    additive, so a flag can tighten a checked-in policy but never silently loosen it.

    `--prereg` adds a different kind of check. A policy asks "is this number good
    enough"; a pre-registration asks "is this the evaluation you said you would run".
    """
    # Loaded through verification rather than `load_all`, so a field the selected
    # record's schema did not seal is removed before any check reads it. A receipt
    # sealed under 1.1 is re-hashed with the post-1.1 fields stripped, which is what
    # keeps it verifiable - and also leaves those fields free to write to without
    # disturbing the hash. A `manifest.suite.hash` written into one used to satisfy a
    # pre-registration that pinned that digest.
    verified = load_verified(ledger)
    if not verified.records:
        console.print(f"[red]Cannot read the ledger:[/red] "
                      f"{rich_escape(verified.detail)}")
        raise typer.Exit(code=EXIT_BAD_INPUT)
    recs = verified.records
    if not -len(recs) <= index < len(recs):
        raise typer.BadParameter(f"index {index} out of range; ledger has {len(recs)} record(s)")
    record = recs[index]
    selected = verified.results[index % len(recs)]
    failures: list[str] = []

    integrity_checks = [Check(
        "ledger.record_verified", selected.verified, selected.detail)]
    if not selected.verified:
        failures.append(f"ledger.record_verified: {selected.detail}")
    if selected.quarantined:
        # Only reachable by writing a value into space the hash does not cover: a
        # genuine old receipt leaves those fields at their defaults.
        integrity_checks.append(Check(
            "ledger.unsealed_fields", False,
            f"record {index} carries {len(selected.quarantined)} field(s) its schema "
            f"({selected.sealed_under}) did not seal: "
            f"{', '.join(selected.quarantined)}. They were removed before any check "
            "read them, and nothing here vouches for them"))
        failures.append(
            f"ledger.unsealed_fields: unsealed value(s) in "
            f"{', '.join(selected.quarantined)}")
    else:
        integrity_checks.append(Check(
            "ledger.unsealed_fields", True,
            f"every field present was sealed under schema {selected.sealed_under}"))
    if selected.problems:
        # Separate from `record_verified` because they are different claims: a
        # fabricated summary is sealed along with everything else, so the hash matches
        # and the numbers are still impossible. On the baseline `mean_score = 42.0`
        # cleared `--min-score 0.9` and `n_cases = 999` on a two-case run cleared
        # `--min-score 0.95`.
        integrity_checks.append(Check(
            "ledger.data_invariants", False,
            f"record {index} fails {len(selected.problems)} data invariant(s): "
            + "; ".join(selected.problems[:3])
            + ("" if len(selected.problems) <= 3
               else f" (and {len(selected.problems) - 3} more)")))
        failures.append(
            f"ledger.data_invariants: {len(selected.problems)} invariant(s) do not hold")
    else:
        integrity_checks.append(Check(
            "ledger.data_invariants", True,
            "scores are finite and in range, and the summary matches the cases"))

    if policy is None and suite is not None:
        # A `policy` section that nothing reads would be a threshold silently doing
        # nothing, which is the failure policy.py exists to prevent.
        named = _load_suite(suite).get("policy")
        if named is None:
            raise typer.BadParameter(f"{suite} has no policy section to apply")
        policy = Path(named)
        if not policy.exists():
            raise typer.BadParameter(f"{suite} names a policy that is not there: {policy}")

    policy_checks = [*integrity_checks]
    if policy is not None:
        try:
            loaded = load_policy(policy)
            result = evaluate(loaded, record, ledger=ledger, policy_dir=policy.parent)
        except PolicyError as e:
            # A broken policy is a broken build. Falling back to "no thresholds" would
            # turn a typo into a green check.
            console.print(f"[red]Policy error:[/red] {e}")
            raise typer.Exit(code=2) from None
        policy_checks = result.checks
        failures += [f"{c.rule}: {c.detail}" for c in result.violations]

    if prereg is not None:
        try:
            contract = load_preregistration(prereg)
        except PreregError as e:
            # Same rule as a broken policy: a contract that cannot be read is a broken
            # build, never "no contract".
            console.print(f"[red]Pre-registration error:[/red] {rich_escape(str(e))}")
            raise typer.Exit(code=EXIT_BAD_INPUT) from None
        prereg_checks = check_preregistration(
            contract, record,
            anchor=_anchor_state(anchor_file, record, ledger),
            # Verified against the record the gate actually selected, so a signature
            # over an earlier prefix cannot satisfy the requirement.
            signature=verify_receipt_signature(
                ledger, index,
                trusted_keys=(list(trusted_key) or contract.trusted_public_keys or None)),
            # Verified here, under a root this invocation supplied, rather than read
            # as a boolean off the anchor the producer wrote.
            external=_external_state(
                anchor_file, tsa_ca=tsa_ca, approved=list(approved_backend)),
        )
        policy_checks = [*policy_checks, *prereg_checks]
        failures += [f"{c.rule}: {c.detail}" for c in prereg_checks if not c.passed]
        if contract.policy is not None:
            embedded = evaluate(
                contract.policy, record, ledger=ledger, policy_dir=prereg.parent)
            policy_checks = [*policy_checks, *embedded.checks]
            failures += [f"{c.rule}: {c.detail}" for c in embedded.violations]

    if verify_ledger:
        ok, msg = verify_chain(ledger)
        if not ok:
            failures.append(f"ledger verification failed: {msg}")

    if min_score is not None and record.aggregate.mean_score < min_score:
        failures.append(
            f"mean score {record.aggregate.mean_score:.3f} is below --min-score {min_score:.3f}"
        )

    if max_flip_rate is not None:
        over = [c for c in record.results if c.flip_rate > max_flip_rate]
        if over:
            worst = max(over, key=lambda c: c.flip_rate)
            failures.append(
                f"{len(over)} case(s) exceed --max-flip-rate {max_flip_rate:.2f} "
                f"(worst: {worst.case_id} at {worst.flip_rate:.0%})"
            )

    if critical:
        wanted = {c.strip() for c in critical.split(",") if c.strip()}
        known = {c.case_id for c in record.results}
        missing = wanted - known
        if missing:
            failures.append(f"critical case(s) not in this record: {', '.join(sorted(missing))}")
        flipped = [c.case_id for c in record.results if c.case_id in wanted and c.flip_count]
        if flipped:
            failures.append(f"critical case(s) flipped: {', '.join(sorted(flipped))}")

    if expect_evaluator is not None:
        actual = evaluator_fingerprint(record)
        if actual != expect_evaluator:
            failures.append(
                explain_fingerprint_mismatch("evaluator", expect_evaluator, actual))

    if expect_config is not None:
        actual = config_fingerprint(record)
        if actual != expect_config:
            failures.append(
                explain_fingerprint_mismatch("config", expect_config, actual))

    execution = record.manifest.execution
    if as_json:
        console.print_json(json.dumps({
            "passed": not failures,
            "checks": [c.to_dict() for c in policy_checks],
            # The failing subset, so a consumer does not have to filter `checks`
            # itself. `failures` is the flat list of fingerprint-pin messages, which
            # are not policy checks and have no rule name.
            "violations": [c.to_dict() for c in policy_checks if not c.passed],
            "failures": failures,
            "mean_score": record.aggregate.mean_score,
            "sealed_hash": record.hash,
            "evaluator_fingerprint": evaluator_fingerprint(record),
            "config_fingerprint": config_fingerprint(record),
            # Not a check: replaying a cassette is a legitimate way to run a gate. It
            # is reported because a gate that passes on replayed numbers and one that
            # passes on live ones mean different things, and nothing used to say which.
            # None for a receipt sealed before schema 1.7.
            "origin": execution.origin if execution else None,
        }))
        raise typer.Exit(code=EXIT_UNSTABLE if failures else 0)

    # Print what passed as well as what failed: a gate that only speaks up on failure
    # cannot be told apart from a gate that checked nothing.
    for check in policy_checks:
        if check.passed:
            console.print(
                f"[green]ok  [/green] {rich_escape(check.rule)}: {rich_escape(check.detail)}")

    if execution is not None and execution.origin != "live":
        console.print(
            f"[yellow]note[/yellow] origin: {rich_escape(execution.origin)} "
            f"({execution.n_replayed} of {execution.n_calls} call(s) replayed) - these "
            "numbers are not a fresh measurement of the model")

    if failures:
        for f in failures:
            console.print(f"[red]FAIL[/red] {rich_escape(f)}")
        raise typer.Exit(code=EXIT_UNSTABLE)

    console.print(
        f"[green]PASS[/green] mean {record.aggregate.mean_score:.3f} · "
        f"{record.aggregate.n_unstable} unstable case(s) · "
        f"evaluator {evaluator_fingerprint(record)[:20]}... · "
        f"config {config_fingerprint(record)[:20]}..."
    )
    raise typer.Exit(code=0)


@app.command(context_settings={"ignore_unknown_options": True})
def power(
    baseline: float = typer.Option(
        0.9, min=0.0, max=1.0, help="Accuracy the suite scores today."
    ),
    items: int = typer.Option(40, min=1, help="Number of cases in the suite."),
    repeats: int = typer.Option(5, min=1, help="Repeats per case."),
    mdd: float = typer.Option(
        0.05, min=0.0001, max=1.0,
        help="Minimum detectable difference: the drop you want to be able to see.",
    ),
    model: str = typer.Option(
        DETERMINISTIC,
        help=f"Item model: {DETERMINISTIC} (items pass or fail every time) "
             f"or {BERNOULLI} (every repeat is an independent coin flip).",
    ),
    from_receipt: str | None = typer.Option(
        None, "--from-receipt",
        help="Use the per-item pass rates of a real run instead of a synthetic model.",
    ),
    ledger: Path = LedgerOpt,
    target_power: float = typer.Option(
        0.8, min=0.0, max=1.0, help="Power to solve for when reporting what it would take."
    ),
    alpha: float = typer.Option(0.05, min=0.0001, max=0.5, help="Significance level."),
    trials: int = typer.Option(2000, min=100, help="Simulated experiments per estimate."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """How big an experiment would have to be to detect a difference you care about.

    `diff` answers "inconclusive at this N" honestly and often. This answers the next
    question: how many items, or how many repeats, would it take? The method is
    simulation against the same exact McNemar test `diff` uses; `evalseal power --help`
    and the module docstring describe the item models and their assumptions.
    """
    if from_receipt is not None:
        record = _resolve_record(from_receipt, ledger)
        try:
            est = estimate_from_record(
                record, mdd, n_repeats=repeats, alpha=alpha, trials=trials)
        except NotBinary as e:
            # An actionable diagnostic and a documented exit code, not a traceback and
            # not a number for a test that would never be run on this receipt.
            console.print(f"[red]{rich_escape(str(e))}[/red]")
            raise typer.Exit(code=EXIT_BAD_INPUT) from None
    else:
        if model not in (DETERMINISTIC, BERNOULLI):
            raise typer.BadParameter(
                f"unknown model {model!r}; expected {DETERMINISTIC!r} or {BERNOULLI!r}"
            )
        est = estimate(baseline, items, repeats, mdd,
                       model=model, alpha=alpha, trials=trials)

    floor = min_discordant_items(alpha)
    need_items = required_items(
        est.baseline, est.n_repeats, mdd, target_power,
        model=DETERMINISTIC if est.model == "from_receipt" else est.model, alpha=alpha)
    need_repeats = required_repeats(
        est.baseline, est.n_items, mdd, target_power, model=BERNOULLI, alpha=alpha)

    payload = {
        **est.to_dict(),
        "target_power": target_power,
        "min_discordant_items": floor,
        "required_items": need_items,
        "required_repeats_bernoulli": need_repeats,
        "analytic_items_deterministic": analytic_deterministic_items(mdd, alpha),
    }
    if as_json:
        console.print_json(json.dumps(payload))
        raise typer.Exit(code=0)

    console.print(
        f"[bold]Power {est.power:.1%}[/bold] to detect a {mdd:.3f} drop "
        f"from {est.baseline:.3f}, with {est.n_items} item(s) at N={est.n_repeats} "
        f"(model: {est.model}, {est.trials} simulated experiments, alpha {alpha})."
    )
    console.print(
        f"[dim]Power for: {METRIC}. Not the power to detect a change in mean score "
        "across repeats - a shift that lowers every item's pass rate without flipping "
        "a majority is near-invisible to this test and plainly visible in `diff`'s "
        "interval.[/dim]"
    )
    console.print(
        f"\nA paired test needs at least [bold]{floor}[/bold] items to change verdict "
        f"in the same direction before any result can be significant at alpha {alpha}: "
        f"2 x 0.5^{floor} = {2 * 0.5 ** floor:.4f}. A suite where fewer than {floor} "
        "items can move cannot produce a significant result at any number of repeats."
    )
    if need_items is not None:
        console.print(
            f"\nFor {target_power:.0%} power you would need about "
            f"[bold]{need_items}[/bold] items at N={est.n_repeats}."
        )
    else:
        console.print(
            f"\nNo item count up to the search limit reaches {target_power:.0%} power "
            "for this difference."
        )
    if need_repeats is not None:
        console.print(
            f"Raising repeats to [bold]{need_repeats}[/bold] would reach it with "
            f"{est.n_items} items, but only if items behave like independent coin "
            "flips near the 50% boundary."
        )
    else:
        console.print(
            "Adding repeats does not help here. Repeats sharpen each item toward its "
            "own majority verdict; when a shift does not move items across that "
            "boundary, more repeats remove the disagreement the test feeds on. "
            "More items is the dial that works."
        )
    raise typer.Exit(code=0)


@app.command()
def decompose(
    suite: Path | None = typer.Option(
        None, exists=True, dir_okay=False, help="Suite file, as `run` takes."
    ),
    dataset: Path | None = typer.Option(None, exists=True, dir_okay=False),
    target_config: Path | None = typer.Option(None, exists=True, dir_okay=False),
    scorer_config: Path | None = typer.Option(None, exists=True, dir_okay=False),
    n: int | None = typer.Option(None, min=2, help="Repeats per arm."),
    cassette: Path | None = typer.Option(
        None, help="Base cassette path; each arm gets its own file beside it."
    ),
    concurrency: int | None = typer.Option(
        None, min=1, help="Parallel requests in flight. Does not change the result."
    ),
    max_retries: int | None = typer.Option(None, min=0),
    timeout: float | None = typer.Option(None, min=1.0),
    out: Path | None = typer.Option(None, "--out", help="Write the Markdown report here."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Split an LLM-judged suite's flips into the judge's share and the target's.

    Two arms over the same suite, holding the task fixed: one samples the target once
    and judges that fixed response N times, the other samples the target N times and
    judges each once. Comparing them separates grader variance from model variance,
    which two different suites side by side cannot do.
    """
    cfg = _load_suite(suite)
    dataset = dataset or (Path(cfg["dataset"]) if "dataset" in cfg else None)
    target_config = target_config or (Path(cfg["target"]) if "target" in cfg else None)
    scorer_config = scorer_config or (Path(cfg["scorer"]) if "scorer" in cfg else None)
    if dataset is None or target_config is None or scorer_config is None:
        raise typer.BadParameter(
            "need --dataset, --target-config and --scorer-config (or --suite)")

    scorer_cfg = json.loads(scorer_config.read_text(encoding="utf-8"))
    if scorer_cfg.get("type") != "llm_judge":
        raise typer.BadParameter(
            f"decompose only applies to an llm_judge scorer, not {scorer_cfg.get('type')!r}. "
            "With a deterministic grader there is no judge variance to separate out."
        )

    n = n or cfg.get("n_repeats", 5)
    base = cassette or Path(cfg.get("cassette", "tests/cassettes/decompose.json"))
    max_retries = max_retries if max_retries is not None else cfg.get("max_retries", 5)
    timeout = timeout or cfg.get("timeout", 60.0)
    concurrency = concurrency or cfg.get("concurrency", 4)
    target_cfg = json.loads(target_config.read_text(encoding="utf-8"))
    ds = Dataset.from_jsonl(dataset)

    # Separate cassettes: one shared file would let the arms collide whenever the
    # target's first response equals a later one, coupling the two measurements.
    arms = {}
    # Inside the try: a cassette rejects an incompatible format, or an owner it cannot
    # take the lock from, while it is being *constructed*. The handler used to sit
    # around `run_decompose` alone, so both of those reached the user as a traceback.
    try:
        for arm in ("judge_only", "full"):
            path = base.with_name(f"{base.stem}.{arm}{base.suffix}")
            cas = Cassette(path, record=os.environ.get("EVALSEAL_RECORD") == "1")
            arms[arm] = (
                _build_target(target_cfg, cas, max_retries, timeout),
                _build_scorer(scorer_cfg, cas, max_retries, timeout),
            )
    except (CassetteBusy, CassetteFormatError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None

    try:
        result = run_decompose(
            ds,
            judge_only_target=arms["judge_only"][0],
            judge_only_scorer=arms["judge_only"][1],
            full_target=arms["full"][0],
            full_scorer=arms["full"][1],
            n_repeats=n,
            concurrency=concurrency,
        )
    except (CassetteBusy, CassetteFormatError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None

    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        raise typer.Exit(code=0)

    text = render_decomposition(result)
    if out is not None:
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]Decomposition -> {out}[/green]")
    console.print(Markdown(text))
    raise typer.Exit(code=0)


def _looks_like_ledger(path: Path) -> bool:
    """Ledgers are .jsonl and receipts .json, as everywhere else in EvalSeal.

    Decided by name rather than by sniffing content: a one-record ledger is a single line
    of valid JSON, indistinguishable from a compact receipt, and treating it as a receipt
    would silently drop the ledger binding the anchor exists to record.
    """
    return path.suffix == ".jsonl"


@app.command()
def anchor(
    source: Path | None = typer.Argument(
        None, exists=True, dir_okay=False,
        help="The sealed receipt to anchor: the report.json `evalseal run` writes, or a "
             ".jsonl ledger, whose head is then the receipt. Not the output of "
             "`evalseal report --json`, which summarises a receipt rather than being one."),
    ledger: Path | None = typer.Option(
        None, "--ledger", exists=True, dir_okay=False,
        help="The ledger the receipt was sealed into. Defaults to SOURCE when it is one."),
    artifact: list[Path] = typer.Option(
        [], "--artifact", exists=True, dir_okay=False,
        help="Another file to bind by hash, such as receipt.html. Repeatable."),
    out: Path = typer.Option(Path("anchor.json"), "--out", help="Where to write the anchor."),
    with_backend: list[str] = typer.Option(
        [], "--with",
        help="Also obtain an external attestation of this anchor from a backend. "
             "Repeatable. Run `evalseal anchor --list-backends` to see what is available.",
    ),
    tsa: str | None = typer.Option(
        None, "--tsa", metavar="URL",
        help="Timestamp authority for `--with rfc3161`. There is no default: a default "
             "would make one service part of this tool's trust root, and would make "
             "`anchor` contact a third party without being asked. Only the subject "
             "digest is sent."),
    list_backends: bool = typer.Option(
        False, "--list-backends", help="List anchor backends and exit."),
):
    """Bind a receipt, its ledger head and its signature state into one checkable file.

    This is a local anchor. It lets a third party confirm, with no access to your
    machine, that a receipt is the one that was sealed and that the ledger still holds
    it. It is not a timestamp: created_at is this machine's clock, and external_proof is
    null. See docs/external-anchoring.md for what an independent anchor adds.
    """
    if list_backends:
        for name in available_backends():
            console.print(f"{name}{_BACKEND_NOTES.get(name, '')}")
        raise typer.Exit(code=0)
    if tsa:
        register_backend(Rfc3161Backend(tsa))
    if source is None:
        raise typer.BadParameter("a receipt or ledger to anchor is required")
    if ledger is None and _looks_like_ledger(source):
        ledger = source
    record = _read_receipt(source)
    try:
        result = build_anchor(record, source, ledger, list(artifact),
                              backends=list(with_backend))
    except AnchorError as e:
        console.print(f"[red]Not anchored:[/red] {rich_escape(str(e))}")
        raise typer.Exit(code=1) from None
    write_anchor(result, out)
    console.print(
        f"[green]Anchored[/green] {rich_escape(record.hash[:20])}... -> {rich_escape(str(out))}\n"
        f"subject digest {rich_escape(result['subject_digest'])}\n"
        + ("signed by " + rich_escape(result["public_key_fingerprint"])
           if result["signature_present"] else "not signed")
        + " · local anchor, not an external timestamp"
    )
    raise typer.Exit(code=0)


@app.command(name="anchor-verify")
def anchor_verify(
    anchor_file: Path = typer.Argument(..., exists=True, dir_okay=False,
                                       help="The anchor.json to check."),
    source: Path = typer.Argument(
        ..., exists=True, dir_okay=False,
        help="The receipt (or ledger, whose head is the receipt) the anchor should cover."),
    ledger: Path | None = typer.Option(
        None, "--ledger", exists=True, dir_okay=False,
        help="Ledger to check against. Defaults to SOURCE when it is one, else the path "
             "recorded in the anchor."),
    tsa_ca: Path | None = typer.Option(
        None, "--tsa-ca", exists=True, dir_okay=False,
        help="PEM trust root for an RFC 3161 proof. Supplied by you, the verifier - "
             "never read from the anchor, since a root the author chose would defeat "
             "the point. Without it the proof's imprint is checked and its signature "
             "is not."),
    as_json: bool = typer.Option(False, "--json", help="Emit every check as JSON."),
):
    """Re-check every claim an anchor makes. Exit 1 if any required check fails.

    Checks that could not run because a file is absent are reported as not checked,
    never as passed.
    """
    try:
        anchor_obj = json.loads(anchor_file.read_text(encoding="utf-8"))
    except ValueError as e:
        console.print(f"[red]Not an anchor file:[/red] {rich_escape(str(e))}")
        raise typer.Exit(code=1) from None
    if ledger is None and _looks_like_ledger(source):
        ledger = source
    record = _read_receipt(source)
    # Configured for this verification only, so the signature check becomes possible
    # without the anchor having any say in which root is trusted - and without the
    # root outliving the call, which `register_backend` would have let it do.
    overrides = ([Rfc3161Backend(ca_pem=tsa_ca.read_bytes())]
                 if tsa_ca is not None else [])
    with temporary_backends(*overrides):
        checks = verify_anchor(anchor_obj, record, ledger=ledger)
    passed = anchor_passed(checks)

    if as_json:
        console.print_json(json.dumps({
            "verified": passed,
            "subject_digest": anchor_obj.get("subject_digest"),
            "checks": [{"check": c.name, "passed": c.passed, "required": c.required,
                        "detail": c.detail} for c in checks],
        }))
        raise typer.Exit(code=0 if passed else 1)

    for c in checks:
        if not c.required and not c.passed:
            mark = "[yellow]skip[/yellow]"
        else:
            mark = "[green]ok  [/green]" if c.passed else "[red]FAIL[/red]"
        console.print(f"{mark} {rich_escape(c.name)}: {rich_escape(c.detail)}")
    console.print(
        "[green]Anchor verified.[/green]" if passed
        else "[red]Anchor does not verify.[/red]"
    )
    raise typer.Exit(code=0 if passed else 1)


@app.command(context_settings={"ignore_unknown_options": True})
def drift(
    a: str = typer.Argument(..., help="The earlier run: a receipt file, or a ledger index."),
    b: str = typer.Argument(..., help="The later run: a receipt file, or a ledger index."),
    ledger: Path = LedgerOpt,
    as_json: bool = typer.Option(False, "--json", help="Emit the whole report as JSON."),
    out: Path | None = typer.Option(None, "--out", help="Write the Markdown report here."),
):
    """Did the judge behave the same way when the suite was run again?

    `diff` asks whether the score moved. This asks what moved underneath it: whether the
    grading setup changed between the two runs, which cases came back with a different
    verdict, and whether that can be blamed on the judge, on the target, or on neither
    because the instrument itself changed. Exit status is always 0: it reports.
    """
    report = analyze_drift(_resolve_record(a, ledger), _resolve_record(b, ledger))

    if as_json:
        console.print_json(json.dumps(report.to_dict()))
        raise typer.Exit(code=0)

    text = render_drift(report)
    if out is not None:
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]Drift report -> {out}[/green]")
    console.print(Markdown(text))
    raise typer.Exit(code=0)


@app.command()
def preregister(
    suite: Path | None = typer.Option(
        None, "--suite", exists=True, dir_okay=False,
        help="The suite file this evaluation is declared against."),
    dataset: Path | None = typer.Option(
        None, "--dataset", exists=True, dir_okay=False,
        help="The dataset file. Read from --suite when that names one."),
    n_repeats: int | None = typer.Option(
        None, "--n-repeats", min=1,
        help="How many times each case is declared to run. Defaults to the suite's."),
    min_repeats_per_case: int | None = typer.Option(
        None, "--min-repeats-per-case", min=1,
        help="Floor for a single case's score count. Defaults to --n-repeats."),
    pin_from: Path | None = typer.Option(
        None, "--pin-from", exists=True, dir_okay=False,
        help="A receipt whose evaluator fingerprint is pinned into the contract. A "
             "fingerprint cannot be computed from files alone: it covers the judge's "
             "served identity, so it needs one real run."),
    require_artifact: list[str] = typer.Option(
        [], "--require-artifact",
        help="An artifact role the receipt must seal by digest, such as `cassette`. "
             "Repeatable."),
    require_ci_claim: bool = typer.Option(
        False, "--require-ci-claim",
        help="Require the receipt to carry CI markers. Named `claim` because that is "
             "all it is: any shell can export GITHUB_ACTIONS=true, so this catches a "
             "receipt produced on a laptop by accident, not one produced there on "
             "purpose. Stronger evidence needs a resolvable run URL and a ledger "
             "signature from a key only CI holds. See docs/pre-registration.md."),
    require_signature: bool = typer.Option(
        False, "--require-signature",
        help="Require the selected receipt to carry a valid signature that covers it. "
             "A signature over an earlier prefix of the ledger does not count."),
    trusted_key: list[str] = typer.Option(
        [], "--trusted-key", metavar="KEY",
        help="A public key the *verifier* trusts, as base64, PEM or a path. "
             "Repeatable, which is how key rotation works. Without any, a signature "
             "shows that someone signed, not that your signer did."),
    require_anchor: bool = typer.Option(
        False, "--require-anchor", help="Require a verifying anchor at gate time."),
    require_external_anchor: bool = typer.Option(
        False, "--require-external-anchor",
        help="Require an externally VERIFIED proof of time on that anchor: a token "
             "from an approved backend, checked against a root the verifier supplies "
             "with `gate --tsa-ca`. An unverified imprint does not satisfy it."),
    policy: Path | None = typer.Option(
        None, "--policy", exists=True, dir_okay=False,
        help="A policy file to embed, so thresholds are fixed before the number exists."),
    baseline: str | None = typer.Option(
        None, "--baseline", help="A baseline receipt this run will be compared against."),
    note: str | None = typer.Option(
        None, "--note", help="Why this evaluation is being run. Free text."),
    out: Path = typer.Option(
        Path("prereg.json"), "--out", help="Where to write the pre-registration."),
):
    """Declare an evaluation before running it, so the receipt can be checked against it.

    A policy asks whether a number is good enough. A pre-registration asks whether this
    is the evaluation that was declared: the same suite, the same dataset, the same case
    ids, the same repeat count, the same grading setup.

    It cannot prove that nobody ran the suite privately first - nothing that runs on the
    author's machine can. What it does is fix the contract before any number exists, so
    that dropping the cases that failed, quietly halving the repeat count or loosening a
    threshold after the fact becomes a visible change rather than a silent one.
    """
    cfg = _load_suite(suite)
    dataset_path = dataset or (Path(cfg["dataset"]) if cfg.get("dataset") else None)
    if dataset_path is None:
        raise typer.BadParameter(
            "no dataset: pass --dataset, or a --suite that names one")

    loaded = Dataset.from_jsonl(dataset_path)
    case_ids = [c.case_id for c in loaded.cases]
    declared_repeats = n_repeats or cfg.get("n_repeats", 5)

    pin = None
    if pin_from is not None:
        pin = evaluator_fingerprint(_read_receipt(pin_from))

    contract = build_preregistration(
        suite=suite,
        dataset=dataset_path,
        dataset_hash=loaded.hash,
        n_repeats=declared_repeats,
        min_repeats_per_case=min_repeats_per_case,
        case_ids=case_ids,
        evaluator_fingerprint_pin=pin,
        required_artifacts=list(require_artifact),
        require_ci_claim=require_ci_claim,
        require_signature=require_signature,
        trusted_public_keys=list(trusted_key),
        require_anchor=require_anchor,
        require_external_anchor=require_external_anchor,
        baseline=baseline,
        policy=load_policy(policy) if policy else None,
        note=note,
    )
    out.write_text(contract.to_json(), encoding="utf-8")

    console.print(
        f"[green]Pre-registered[/green] {len(case_ids)} case(s) x {declared_repeats} "
        f"repeat(s) -> {rich_escape(str(out))}\n"
        f"case set {rich_escape(contract.dataset.case_set_hash or 'none')[:27]}...\n"
        + (f"evaluator pinned {rich_escape(pin or '')[:34]}...\n" if pin else
           "evaluator not pinned: re-run with --pin-from once you have a receipt\n")
        + "Commit this file before running. It is a declaration, not evidence: it fixes "
          "what was promised, and cannot prove no private run happened."
    )
    raise typer.Exit(code=0)


@app.command()
def evidence(
    source: Path = typer.Argument(
        ..., exists=True, dir_okay=False,
        help="The sealed receipt to read: the report.json `evalseal run` writes, or a "
             "ledger whose head is the receipt."),
    case: str | None = typer.Option(
        None, "--case", help="Report one case instead of the whole suite."),
    target_margin: float = typer.Option(
        DEFAULT_TARGET_MARGIN, "--target-margin", min=0.001, max=0.5,
        help="How tightly you want a flip rate pinned down. The repeat "
             "recommendations aim at the flip-rate interval's half-width, which is "
             "what this names - not the pass-rate interval's, which is a different "
             "number whenever the interval straddles 0.5."),
    confidence: float = typer.Option(
        DEFAULT_CONFIDENCE, "--confidence", min=0.5, max=0.9999,
        help="Interval confidence. 0.95 matches the intervals in `run` and `report`."),
    out: Path | None = typer.Option(
        None, "--out", help="Write the Markdown report here."),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON for CI or a dashboard."),
):
    """How much this run actually tells you, and how many repeats would tell you more.

    `evalseal power` answers a different question - how large an experiment would have
    to be to *detect a difference* between two runs. This answers how precisely the
    repeats you already have pin down each case's behaviour.

    At five repeats the only flip rates that exist are 0, 0.2 and 0.4, so a case
    reported at "20%" had exactly one run disagree rather than a twenty-percent
    tendency. This command puts an interval around that number and says plainly when
    the interval is too wide to read a magnitude from.

    Intervals are over the repeats that got a verdict. An ungraded repeat is not an
    observation of the pass proportion, so it is excluded here and reported beside the
    repeat count.

    Guidance only. It says where to spend repeats; it makes no claim about the model.
    """
    record = _read_receipt(source)
    try:
        result = analyze_evidence(
            record, confidence=confidence, target_margin=target_margin, case_id=case)
    except ValueError as e:
        console.print(f"[red]{rich_escape(str(e))}[/red]")
        raise typer.Exit(code=EXIT_BAD_INPUT) from None

    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        raise typer.Exit(code=0)

    text = render_evidence(result)
    if out is not None:
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]Evidence report -> {rich_escape(str(out))}[/green]")
    console.print(Markdown(text))
    raise typer.Exit(code=0)
