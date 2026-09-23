"""Run the pre-registered analysis on the two recorded arms, and nothing else.

The primary test is exactly the `evalseal diff` PREREGISTRATION.md names. Everything
else printed here is descriptive and labelled as such.

    python examples/gsm8k_compare/analyze.py [ledger]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from evalseal.diffing import diff_records  # noqa: E402
from evalseal.ledger import load_all, verify_chain  # noqa: E402


def main(ledger: Path) -> None:
    ok, msg = verify_chain(ledger)
    print(f"ledger: {msg}")
    records = load_all(ledger)
    by_model = {r.manifest.target.requested_model: r for r in records}
    flash = by_model["gemini-2.5-flash"]
    gemma = by_model["gemma-4-26b-a4b-it"]

    result = diff_records(flash, gemma)
    p = result.paired
    print("\n== primary analysis (pre-registered) ==")
    print(f"comparable:      {result.comparable}")
    print(f"items paired:    {p.n_items}")
    print(f"delta (gemma - flash): {p.delta:+.4f}")
    print(f"95% CI:          [{p.ci95[0]:+.4f}, {p.ci95[1]:+.4f}]")
    print(f"exact McNemar p: {p.p_value:.6g}")
    print(f"discordant:      {p.n_discordant} "
          f"(flash right / gemma wrong: {p.discordant_regressed}, "
          f"flash wrong / gemma right: {p.discordant_improved})")
    print(f"ambiguous:       {len(p.ambiguous_items)}")
    print(f"verdict:         {p.verdict}")

    print("\n== descriptive only, not tested ==")
    for name, rec in (("gemini-2.5-flash", flash), ("gemma-4-26b-a4b-it", gemma)):
        a = rec.aggregate
        flipped = sum(1 for c in rec.results if c.flip_rate > 0)
        print(f"{name:20s} accuracy {a.mean_score:.4f} | stable {a.n_stable} "
              f"borderline {a.n_borderline} unstable {a.n_unstable} | "
              f"items that flipped {flipped}")
        for w in a.warnings:
            print(f"   warning: {w}")

    fb = {c.case_id: c for c in flash.results}
    gb = {c.case_id: c for c in gemma.results}
    moved = [
        (i, "".join("P" if s >= 0.5 else "F" for s in fb[i].scores),
         "".join("P" if s >= 0.5 else "F" for s in gb[i].scores))
        for i in sorted(fb)
        if (sum(fb[i].scores) / len(fb[i].scores) > 0.5)
        != (sum(gb[i].scores) / len(gb[i].scores) > 0.5)
    ]
    print(f"\nitems whose majority verdict differs ({len(moved)}):")
    for i, f, g in moved:
        print(f"   {i}  flash {f}  gemma {g}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".evalseal/gsm8k_compare.jsonl"))
