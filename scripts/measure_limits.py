#!/usr/bin/env python3
"""Measure what EvalSeal costs, so docs/operations.md can quote numbers it owns.

An SLA invented at a desk is worse than no number: someone plans against it. Everything
in docs/operations.md comes from this script, run on one machine whose specification is
printed alongside the results, and the document says so. Run it yourself and the numbers
will differ; the shapes - linear in cases, flat in concurrency for a replay - will not.

    python scripts/measure_limits.py            # the full set, a minute or two
    python scripts/measure_limits.py --json     # machine-readable
    python scripts/measure_limits.py --quick    # the small sizes only

Nothing here calls a provider: every run replays an in-process callable.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from evalseal.adapters.dataset import Case, Dataset  # noqa: E402
from evalseal.adapters.scorer import AnswerMatchScorer, RegexScorer  # noqa: E402
from evalseal.adapters.target import LocalCallableTarget  # noqa: E402
from evalseal.executor import run_eval  # noqa: E402
from evalseal.ledger import seal_and_append, verify_chain  # noqa: E402

#: A reply of about the length a real GSM8K answer runs to, so per-case memory is not
#: measured against a three-character string.
_REPLY = ("Working through it step by step. " * 40) + "\n42"


def _dataset(n_cases: int) -> Dataset:
    return Dataset([Case(f"c{i:05d}", f"question {i}", "42") for i in range(n_cases)],
                   hash="sha256:bench")


def measure_run(n_cases: int, n_repeats: int, concurrency: int) -> dict:
    """Peak memory and wall time for one sealed run."""
    dataset = _dataset(n_cases)
    target = LocalCallableTarget(lambda p: _REPLY, name="bench")
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    record = run_eval(dataset, target, AnswerMatchScorer(), n_repeats=n_repeats,
                      concurrency=concurrency)
    elapsed = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    observations = n_cases * n_repeats
    return {
        "cases": n_cases,
        "repeats": n_repeats,
        "concurrency": concurrency,
        "observations": observations,
        "seconds": round(elapsed, 3),
        "peak_mib": round(peak / 1024 / 1024, 1),
        "kib_per_observation": round(peak / 1024 / observations, 1),
        "receipt_bytes": len(record.model_dump_json()),
        "receipt_bytes_per_case": round(len(record.model_dump_json()) / n_cases),
    }


def measure_ledger(n_records: int, tmp: Path) -> dict:
    """How long verifying a chain takes as it grows."""
    path = tmp / f"ledger-{n_records}.jsonl"
    dataset = _dataset(5)
    target = LocalCallableTarget(lambda p: _REPLY, name="bench")
    for _ in range(n_records):
        record = run_eval(dataset, target, AnswerMatchScorer(), n_repeats=2)
        seal_and_append(record, path, relink=True)
    started = time.perf_counter()
    ok, _ = verify_chain(path)
    elapsed = time.perf_counter() - started
    return {
        "records": n_records,
        "verified": ok,
        "seconds": round(elapsed, 3),
        "ms_per_record": round(elapsed * 1000 / n_records, 2),
        "file_mib": round(path.stat().st_size / 1024 / 1024, 2),
    }


def measure_regex(body_bytes: int) -> dict:
    """What permitted patterns cost on a body at the transport limit.

    Two of them, because the difference is the whole story: an anchored pattern fails
    at the first character whatever the body's length, and an unanchored one scans it
    all. Quoting only the first would say a grader is free.
    """
    text = "x" * body_bytes
    timings = {}
    for label, pattern in (("anchored", r"^\s*42\s*$"), ("scanning", r"42")):
        scorer = RegexScorer(pattern)
        started = time.perf_counter()
        for _ in range(10):
            scorer.score("q", text, None)
        timings[label] = round((time.perf_counter() - started) / 10 * 1000, 3)
    return {"body_mib": round(body_bytes / 1024 / 1024, 2),
            "ms_anchored": timings["anchored"],
            "ms_scanning": timings["scanning"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    parser.add_argument("--quick", action="store_true", help="Small sizes only.")
    args = parser.parse_args()

    import tempfile

    sizes = [(40, 5), (200, 5)] if args.quick else [(40, 5), (200, 5), (1000, 5)]
    results: dict[str, object] = {
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor() or platform.machine(),
        },
        "runs": [],
        "concurrency": [],
        "ledger": [],
        "regex": [],
    }
    for cases, repeats in sizes:
        results["runs"].append(measure_run(cases, repeats, concurrency=1))
    for workers in (1, 4, 16):
        results["concurrency"].append(measure_run(200, 5, concurrency=workers))
    with tempfile.TemporaryDirectory() as tmp:
        for count in ([10, 50] if args.quick else [10, 50, 200]):
            results["ledger"].append(measure_ledger(count, Path(tmp)))
    for body in (64 * 1024, 1024 * 1024, 8 * 1024 * 1024):
        results["regex"].append(measure_regex(body))

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    machine = results["machine"]
    print(f"{machine['platform']}, Python {machine['python']}, "
          f"{machine['processor']}\n")
    print("A sealed run, replayed from an in-process callable")
    print(f"{'cases':>7} {'obs':>7} {'secs':>7} {'peak MiB':>9} "
          f"{'KiB/obs':>8} {'receipt B/case':>15}")
    for row in results["runs"]:
        print(f"{row['cases']:>7} {row['observations']:>7} {row['seconds']:>7} "
              f"{row['peak_mib']:>9} {row['kib_per_observation']:>8} "
              f"{row['receipt_bytes_per_case']:>15}")
    print("\nConcurrency, 200 cases x 5 repeats (a replay is CPU-bound, so flat)")
    print(f"{'workers':>8} {'secs':>7} {'peak MiB':>9}")
    for row in results["concurrency"]:
        print(f"{row['concurrency']:>8} {row['seconds']:>7} {row['peak_mib']:>9}")
    print("\nVerifying a ledger")
    print(f"{'records':>8} {'secs':>7} {'ms/record':>10} {'file MiB':>9}")
    for row in results["ledger"]:
        print(f"{row['records']:>8} {row['seconds']:>7} {row['ms_per_record']:>10} "
              f"{row['file_mib']:>9}")
    print("\nA permitted regex over one response body")
    print(f"{'body MiB':>9} {'ms anchored':>12} {'ms scanning':>12}")
    for row in results["regex"]:
        print(f"{row['body_mib']:>9} {row['ms_anchored']:>12} "
              f"{row['ms_scanning']:>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
