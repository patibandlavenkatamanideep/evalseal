"""Build the pre-registered comparison sample: 150 GSM8K test problems.

Drawn with a fixed seed from the official test split, excluding the 40 problems already
in examples/gsm8k so the two suites never share an item. Same instruction wording as
that suite, so the prompts differ only in which problem they carry.

Re-running reproduces the committed dataset.jsonl byte for byte.

    python examples/gsm8k_compare/build_dataset.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import httpx

SOURCE = (
    "https://raw.githubusercontent.com/openai/grade-school-math/"
    "master/grade_school_math/data/test.jsonl"
)
SEED = 20260922
N_ITEMS = 150
EXISTING_SEED = 20260918      # the seed examples/gsm8k was drawn with
EXISTING_N = 40
INSTRUCTION = "Solve the problem. End your reply with the final number on its own line."


def main() -> None:
    raw = httpx.get(SOURCE, timeout=60, follow_redirects=True).raise_for_status().text
    problems = [json.loads(line) for line in raw.splitlines() if line.strip()]

    # Reconstruct the existing suite's sample exactly, then exclude it by question text.
    existing = random.Random(EXISTING_SEED).sample(problems, EXISTING_N)
    taken = {p["question"] for p in existing}
    pool = [p for p in problems if p["question"] not in taken]
    sample = random.Random(SEED).sample(pool, N_ITEMS)

    out = Path(__file__).with_name("dataset.jsonl")
    with out.open("w") as f:
        for i, p in enumerate(sample, start=1):
            expected = p["answer"].split("####")[-1].strip()
            f.write(json.dumps({
                "case_id": f"cmp{i:03d}",
                "prompt": f"{p['question']}\n\n{INSTRUCTION}",
                "expected": expected,
            }) + "\n")
    print(f"wrote {out}: {N_ITEMS} of {len(pool)} eligible problems "
          f"({len(problems)} in the test split, {EXISTING_N} excluded), seed {SEED}")


if __name__ == "__main__":
    main()
