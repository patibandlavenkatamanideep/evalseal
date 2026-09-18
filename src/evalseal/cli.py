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
from .adapters.scorer import ExactMatchScorer, LLMJudgeScorer, RegexScorer
from .adapters.target import OpenAICompatibleTarget
from .executor import run_eval
from .ledger import LEDGER_PATH, last_hash, load_all, seal_and_append, verify_chain
from .models import FailOn
from .report import failing_cases, to_markdown, write_json, write_junit, write_markdown

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
    dataset: Path = typer.Option(..., exists=True, dir_okay=False),
    target_config: Path = typer.Option(..., exists=True, dir_okay=False),
    scorer_config: Path = typer.Option(..., exists=True, dir_okay=False),
    n: int = typer.Option(5, min=1, help="Repeats per case (flip rate needs N>=5)."),
    cassette: Path = typer.Option(Path("tests/cassettes/run.json")),
    ledger: Path = LedgerOpt,
    concurrency: int = typer.Option(4, min=1, help="Parallel requests in flight."),
    max_retries: int = typer.Option(5, min=0, help="Retries per request on 429/5xx."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress the progress bar."),
    timeout: float = typer.Option(60.0, min=1.0, help="Per-request timeout in seconds."),
    fail_on: FailOn = typer.Option(
        "unstable", help="Which stability classes fail the run: none | unstable | borderline."
    ),
    junit_xml: Path | None = typer.Option(
        None, help="Also write JUnit XML here, for CI test reporting."
    ),
):
    """Run an eval N times, seal the result, emit report.json + report.md."""
    ds = Dataset.from_jsonl(dataset)
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
def verify(ledger: Path = LedgerOpt):
    """Check the ledger chain integrity (tamper detection)."""
    ok, msg = verify_chain(ledger)
    color = "green" if ok else "red"
    console.print(f"[{color}]{msg}[/{color}]")
    raise typer.Exit(code=0 if ok else 1)


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
