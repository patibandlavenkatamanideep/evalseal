"""Rebuild examples/gsm8k/dataset.jsonl from the official GSM8K test split.

GSM8K (Cobbe et al., 2021) is a benchmark of grade-school math word problems, each with
one verified numeric answer. Sampling is seeded, so this reproduces the committed file
byte for byte.

    python examples/gsm8k/build_dataset.py [n]
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import httpx

SOURCE = (
    "https://raw.githubusercontent.com/openai/grade-school-math/"
    "master/grade_school_math/data/test.jsonl"
)
SEED = 20260918
INSTRUCTION = "Solve the problem. End your reply with the final number on its own line."


def main(n: int = 40) -> None:
    raw = httpx.get(SOURCE, timeout=60, follow_redirects=True).raise_for_status().text
    problems = [json.loads(line) for line in raw.splitlines() if line.strip()]
    sample = random.Random(SEED).sample(problems, n)

    out = Path(__file__).with_name("dataset.jsonl")
    with out.open("w") as f:
        for i, p in enumerate(sample, start=1):
            expected = p["answer"].split("####")[-1].strip()
            f.write(json.dumps({
                "case_id": f"gsm{i:03d}",
                "prompt": f"{p['question']}\n\n{INSTRUCTION}",
                "expected": expected,
            }) + "\n")
    print(f"wrote {out} ({n} cases from {len(problems)} GSM8K test problems, seed {SEED})")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
