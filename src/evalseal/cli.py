from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from .adapters.dataset import Dataset
from .adapters.recording import Cassette, CassetteFormatError
from .adapters.scorer import (
    AnswerMatchScorer,
    ExactMatchScorer,
    LLMJudgeScorer,
    RegexScorer,
)
from .adapters.target import OpenAICompatibleTarget
from .executor import run_eval
from .ledger import LEDGER_PATH, last_hash, load_all, seal_and_append, verify_chain
from .models import FailOn
from .report import failing_cases, to_markdown, write_json, write_junit, write_markdown
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
    for line in path.read_text().splitlines():
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
    suite = json.loads(path.read_text())
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
    cfg: dict, cassette: Cassette, max_retries: int, timeout: float
) -> OpenAICompatibleTarget:
    return OpenAICompatibleTarget(
        model=cfg["model"],
        cassette=cassette,
        base_url=cfg.get("base_url", "https://api.openai.com/v1"),
        temperature=cfg.get("temperature"),   # omit in config to surface the "unset" warning
        seed=cfg.get("seed"),
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
        judge = OpenAICompatibleTarget(
            model=cfg["judge_model"],
            cassette=cassette,
            base_url=cfg.get("base_url", "https://api.openai.com/v1"),
            temperature=cfg.get("judge_temperature"),  # leave unset to demonstrate flips
            seed=cfg.get("judge_seed"),
            max_retries=max_retries,
            timeout=timeout,
        )
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
    sign_key: Path | None = typer.Option(
        None, help="Ed25519 private key; signs the sealed record after the run."
    ),
    only: str | None = typer.Option(
        None, help="Run only these case ids (comma-separated), e.g. to re-examine a flip."
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
    target = _build_target(json.loads(target_config.read_text()), cass, max_retries, timeout)
    scorer = _build_scorer(json.loads(scorer_config.read_text()), cass, max_retries, timeout)

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
    record = seal_and_append(record, ledger)
    write_json(record)
    write_markdown(record)
    if junit_xml is not None:
        write_junit(record, junit_xml, fail_on)
    if sign_key is not None:
        entry = sign_head(ledger, sign_key)
        console.print(
            f"[green]Signed record {entry['record_index']} "
            f"-> {signatures_path(ledger)}[/green]"
        )
    console.print(Markdown(to_markdown(record)))

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


@app.command()
def diff(
    a: int = typer.Argument(..., help="Ledger index of the baseline run (negative ok)."),
    b: int = typer.Argument(..., help="Ledger index of the candidate run (negative ok)."),
    ledger: Path = LedgerOpt,
):
    """Compare two runs; state whether a score change exceeds the noise floor."""
    recs = load_all(ledger)
    for i in (a, b):
        if not -len(recs) <= i < len(recs):
            raise typer.BadParameter(f"index {i} out of range; ledger has {len(recs)} record(s)")
    ra, rb = recs[a], recs[b]
    ma, mb = ra.aggregate.mean_score, rb.aggregate.mean_score

    # Noise floor: widest per-case CI half-width across both runs. Deliberately
    # conservative — "REAL CHANGE" is only claimed when no single case's noise explains it.
    def halfwidth(r):
        return max(((c.ci95[1] - c.ci95[0]) / 2 for c in r.results), default=0.0)

    noise = max(halfwidth(ra), halfwidth(rb))
    delta = mb - ma
    verdict = "within noise" if abs(delta) <= noise else "REAL CHANGE"
    console.print(
        f"mean {ma:.3f} -> {mb:.3f}  (delta {delta:+.3f}, noise floor ±{noise:.3f}) => {verdict}"
    )
    if ra.manifest.dataset.hash != rb.manifest.dataset.hash:
        console.print("[yellow]Warning: runs used different datasets.[/yellow]")
