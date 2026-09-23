from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as rich_escape
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from .adapters.dataset import Dataset
from .adapters.recording import Cassette, CassetteFormatError
from .adapters.scorer import (
    AnswerMatchScorer,
    ExactMatchScorer,
    LLMJudgeScorer,
    RegexScorer,
)
from .adapters.target import AnthropicTarget, OpenAICompatibleTarget
from .anchor import (
    AnchorError,
    anchor_passed,
    build_anchor,
    check_artifacts,
    verify_anchor,
    write_anchor,
)
from .decompose import decompose as run_decompose
from .decompose import render as render_decomposition
from .diffing import diff_records, load_receipt, mean_flip_rate
from .executor import run_eval
from .htmlreport import write_diff_html, write_html
from .ledger import (
    FINGERPRINT_SCHEME,
    LEDGER_PATH,
    config_fingerprint,
    evaluator_fingerprint,
    fingerprint_scheme,
    last_hash,
    load_all,
    seal_and_append,
    verify_chain,
)
from .models import FailOn, RunRecord, SuiteProvenance
from .policy import PolicyError, evaluate, load_policy
from .power import (
    BERNOULLI,
    DETERMINISTIC,
    analytic_deterministic_items,
    estimate,
    estimate_from_record,
    min_discordant_items,
    required_items,
    required_repeats,
)
from .provenance import file_hash
from .report import (
    case_rows,
    failing_cases,
    render_diff,
    to_case_table,
    to_markdown,
    verdict_sequence,
    write_json,
    write_junit,
    write_markdown,
)
from .signing import (
    generate_keypair,
    sign_head,
    signatures_path,
    verify_signatures,
)

# Distinct from 1 (uncaught error) and 2 (usage error) so CI can tell
# "the eval is unstable" apart from "the tool broke".
EXIT_UNSTABLE = 3
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


@app.callback()
def _main() -> None:
    load_dotenv()


def _load_suite(path: Path | None) -> dict:
    """A suite file names the dataset, target, scorer and run settings in one place.
    Explicit flags still win, so a suite is a default, not a cage."""
    if path is None:
        return {}
    suite = json.loads(path.read_text(encoding="utf-8"))
    unknown = set(suite) - {
        "dataset", "target", "scorer", "n_repeats", "concurrency", "cassette",
        "fail_on", "max_retries", "timeout", "ledger", "junit_xml", "sign_key",
    }
    if unknown:
        raise typer.BadParameter(f"unknown key(s) in {path}: {', '.join(sorted(unknown))}")
    base = path.parent

    def resolve(value: str) -> str:      # paths are relative to the suite file
        return str((base / value).resolve()) if value else value

    for key in ("dataset", "target", "scorer", "cassette", "ledger", "junit_xml", "sign_key"):
        if key in suite:
            suite[key] = resolve(suite[key])
    return suite


def _build_target(
    cfg: dict, cassette: Cassette, max_retries: int, timeout: float,
    prefix: str = "",
) -> AnthropicTarget | OpenAICompatibleTarget:
    """Build a target from config. `provider` picks the wire format, defaulting to the
    OpenAI-compatible one so every existing config and suite file keeps working.

    `prefix` lets the judge reuse this with its own `judge_*` keys, so a judge is
    configured exactly like a target rather than through a second, smaller vocabulary.
    """
    provider = cfg.get(f"{prefix}provider", cfg.get("provider", "openai"))
    model = cfg[f"{prefix}model"]

    if provider == "anthropic":
        return AnthropicTarget(
            model=model,
            cassette=cassette,
            base_url=cfg.get("base_url", "https://api.anthropic.com/v1"),
            max_tokens=cfg.get(f"{prefix}max_tokens", cfg.get("max_tokens", 1024)),
            temperature=cfg.get(f"{prefix}temperature"),
            top_p=cfg.get(f"{prefix}top_p"),
            system=cfg.get("system"),
            api_key_env=cfg.get("api_key_env", "ANTHROPIC_API_KEY"),
            max_retries=max_retries,
            timeout=timeout,
        )
    if provider != "openai":
        raise typer.BadParameter(
            f"unknown provider {provider!r}; expected 'openai' or 'anthropic'"
        )
    return OpenAICompatibleTarget(
        model=model,
        cassette=cassette,
        base_url=cfg.get("base_url", "https://api.openai.com/v1"),
        temperature=cfg.get(f"{prefix}temperature"),  # omit to surface the "unset" warning
        seed=cfg.get(f"{prefix}seed"),
        api_key_env=cfg.get("api_key_env", "EVALSEAL_API_KEY"),
        max_retries=max_retries,
        timeout=timeout,
    )


def _build_scorer(cfg: dict, cassette: Cassette, max_retries: int, timeout: float):
    if cfg["type"] == "exact":
        return ExactMatchScorer()
    if cfg["type"] == "regex":
        return RegexScorer(pattern=cfg["pattern"])
    if cfg["type"] == "answer_match":
        return AnswerMatchScorer(tolerance=cfg.get("tolerance", 1e-6))
    if cfg["type"] == "llm_judge":
        judge = _build_target(cfg, cassette, max_retries, timeout, prefix="judge_")
        return LLMJudgeScorer(judge=judge, rubric=cfg["rubric"])
    raise typer.BadParameter(f"unknown scorer type {cfg['type']!r}")


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
    ledger: Path = LedgerOpt,
    concurrency: int | None = typer.Option(None, min=1, help="Parallel requests in flight."),
    max_retries: int | None = typer.Option(None, min=0, help="Retries per request on 429/5xx."),
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
):
    """Run an eval N times, seal the result, emit report.json + report.md."""
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
    fail_on = fail_on or cfg.get("fail_on", "unstable")
    junit_xml = junit_xml or (Path(cfg["junit_xml"]) if "junit_xml" in cfg else None)
    sign_key = sign_key or (Path(cfg["sign_key"]) if "sign_key" in cfg else None)

    ds = Dataset.from_jsonl(dataset)
    if only:
        wanted = [c.strip() for c in only.split(",") if c.strip()]
        missing = [c for c in wanted if c not in {case.case_id for case in ds.cases}]
        if missing:
            raise typer.BadParameter(f"case id(s) not in {dataset}: {', '.join(missing)}")
        ds = Dataset([c for c in ds.cases if c.case_id in wanted], ds.hash)
    try:
        cass = Cassette(cassette)
    except CassetteFormatError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    target_cfg = json.loads(target_config.read_text(encoding="utf-8"))
    scorer_cfg = json.loads(scorer_config.read_text(encoding="utf-8"))
    target = _build_target(target_cfg, cass, max_retries, timeout)
    scorer = _build_scorer(scorer_cfg, cass, max_retries, timeout)

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
                    name=suite.stem, path=str(suite), hash=file_hash(suite)
                ) if suite else None,
                dataset_path=str(dataset),
                store_judge_prompt=store_judge_prompt,
                # Hashed inside run_eval, once the units have finished: the cassette is
                # still being written while they run.
                artifact_paths={
                    "cassette": str(cassette),
                    "dataset": str(dataset),
                    **({"suite": str(suite)} if suite else {}),
                },
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
    ok, msg = verify_chain(ledger)
    console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
    if not ok:
        raise typer.Exit(code=1)
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
            colour = {"ok": "green", "changed": "red", "missing": "yellow",
                      "unverifiable": "yellow"}[r.status]
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
):
    """Generate an Ed25519 keypair for signing ledgers."""
    try:
        pub = generate_keypair(private_key, public_key)
    except FileExistsError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from e
    console.print(f"[green]private key: {private_key} (mode 600 — keep it secret)[/green]")
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


def _pin_failure(kind: str, pinned: str, actual: str) -> str:
    """Explain a fingerprint pin that did not match, scheme difference first.

    A pin written under an older scheme cannot match a scheme-2 fingerprint, and saying
    "the grading setup changed" about it would be wrong: nothing changed except what the
    fingerprint covers. That needs re-pinning, not investigating.
    """
    pinned_scheme = fingerprint_scheme(pinned)
    if pinned_scheme != FINGERPRINT_SCHEME:
        named = f"scheme {pinned_scheme}" if pinned_scheme else "an unversioned scheme"
        return (
            f"The pinned {kind} fingerprint was made under {named}; this build computes "
            f"scheme {FINGERPRINT_SCHEME}, which covers fields the older scheme did not. "
            "The two cannot be compared. Re-pin from `evalseal report --json`."
        )
    if kind == "evaluator":
        return (
            "Not directly comparable: the evaluator fingerprint differs from the pinned "
            f"one (expected {pinned[:28]}..., got {actual[:28]}...). The grading setup "
            "changed, so this score cannot be compared with the baseline."
        )
    # A mismatched config hash says that something changed, not which side of it: a
    # different target model also changes it, and that leaves the runs comparable.
    return (
        f"Configuration differs from the pinned one (expected {pinned[:28]}..., got "
        f"{actual[:28]}...). Something about what ran changed; this alone does not mean "
        "the runs are incomparable, since a different target model also changes it. Pin "
        "--expect-evaluator to require the same grading setup."
    )


def _resolve_record(token: str, ledger: Path) -> RunRecord:
    """A receipt path or a ledger index, whichever the user typed.

    Indices are resolved against the ledger only after the path lookup fails, so a file
    literally named `0` still wins - the filesystem is the less surprising reading.
    """
    path = Path(token)
    if path.exists():
        return load_receipt(path)
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
                "sealed_hash": record.hash,
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

    console.print(Markdown(to_case_table(record, unstable_only=unstable_only)))
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
    as_json: bool = typer.Option(
        False, "--json", help="Emit every check as machine-readable JSON."
    ),
):
    """Apply CI thresholds to a sealed record. Exit 3 means the gate failed.

    Thresholds come from flags, from `--policy evalseal.yml`, or both: the two sets are
    additive, so a flag can tighten a checked-in policy but never silently loosen it.
    """
    recs = load_all(ledger)
    if not -len(recs) <= index < len(recs):
        raise typer.BadParameter(f"index {index} out of range; ledger has {len(recs)} record(s)")
    record = recs[index]
    failures: list[str] = []

    policy_checks = []
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
            failures.append(_pin_failure("evaluator", expect_evaluator, actual))

    if expect_config is not None:
        actual = config_fingerprint(record)
        if actual != expect_config:
            failures.append(_pin_failure("config", expect_config, actual))

    if as_json:
        console.print_json(json.dumps({
            "passed": not failures,
            "checks": [c.to_dict() for c in policy_checks],
            "failures": failures,
            "mean_score": record.aggregate.mean_score,
            "sealed_hash": record.hash,
            "evaluator_fingerprint": evaluator_fingerprint(record),
            "config_fingerprint": config_fingerprint(record),
        }))
        raise typer.Exit(code=EXIT_UNSTABLE if failures else 0)

    # Print what passed as well as what failed: a gate that only speaks up on failure
    # cannot be told apart from a gate that checked nothing.
    for check in policy_checks:
        if check.passed:
            console.print(
                f"[green]ok  [/green] {rich_escape(check.rule)}: {rich_escape(check.detail)}")

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
        est = estimate_from_record(
            record, mdd, n_repeats=repeats, alpha=alpha, trials=trials)
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
    for arm in ("judge_only", "full"):
        path = base.with_name(f"{base.stem}.{arm}{base.suffix}")
        cas = Cassette(path, record=os.environ.get("EVALSEAL_RECORD") == "1")
        arms[arm] = (
            _build_target(target_cfg, cas, max_retries, timeout),
            _build_scorer(scorer_cfg, cas, max_retries, timeout),
        )

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
    except CassetteFormatError as e:
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
    source: Path = typer.Argument(
        ..., exists=True, dir_okay=False,
        help="A receipt (report.json) or a ledger, whose head is then the receipt."),
    ledger: Path | None = typer.Option(
        None, "--ledger", exists=True, dir_okay=False,
        help="The ledger the receipt was sealed into. Defaults to SOURCE when it is one."),
    artifact: list[Path] = typer.Option(
        [], "--artifact", exists=True, dir_okay=False,
        help="Another file to bind by hash, such as receipt.html. Repeatable."),
    out: Path = typer.Option(Path("anchor.json"), "--out", help="Where to write the anchor."),
):
    """Bind a receipt, its ledger head and its signature state into one checkable file.

    This is a local anchor. It lets a third party confirm, with no access to your
    machine, that a receipt is the one that was sealed and that the ledger still holds
    it. It is not a timestamp: created_at is this machine's clock, and external_proof is
    null. See docs/external-anchoring.md for what an independent anchor adds.
    """
    if ledger is None and _looks_like_ledger(source):
        ledger = source
    try:
        record = load_receipt(source)
        result = build_anchor(record, source, ledger, list(artifact))
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
    record = load_receipt(source)
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
