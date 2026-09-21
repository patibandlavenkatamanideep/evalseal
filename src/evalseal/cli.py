from __future__ import annotations

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
from .diffing import diff_records, load_receipt, mean_flip_rate
from .executor import run_eval
from .htmlreport import write_diff_html, write_html
from .ledger import (
    LEDGER_PATH,
    config_fingerprint,
    last_hash,
    load_all,
    seal_and_append,
    verify_chain,
)
from .models import FailOn, RunRecord, SuiteProvenance
from .policy import PolicyError, evaluate, load_policy
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
):
    """Check the ledger chain integrity, and signatures when asked."""
    ok, msg = verify_chain(ledger)
    console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
    if not ok:
        raise typer.Exit(code=1)
    if public_key is not None or signed:
        sig_ok, sig_msg = verify_signatures(ledger, public_key)
        console.print(f"[{'green' if sig_ok else 'red'}]{sig_msg}[/{'green' if sig_ok else 'red'}]")
        if not sig_ok:
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
        None, help="Fail unless the evaluator config fingerprint equals this."
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

    if expect_config is not None:
        actual = config_fingerprint(record)
        if actual != expect_config:
            failures.append(
                "Not directly comparable: evaluator configuration changed "
                f"(expected {expect_config[:20]}..., got {actual[:20]}...)"
            )

    if as_json:
        console.print_json(json.dumps({
            "passed": not failures,
            "checks": [c.to_dict() for c in policy_checks],
            "failures": failures,
            "mean_score": record.aggregate.mean_score,
            "sealed_hash": record.hash,
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
        f"config {config_fingerprint(record)[:20]}..."
    )
    raise typer.Exit(code=0)
