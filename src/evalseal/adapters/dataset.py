from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Case:
    case_id: str
    prompt: str
    expected: str | None = None


@dataclass
class Dataset:
    cases: list[Case]
    hash: str

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "Dataset":
        raw = Path(path).read_text()
        cases = []
        seen: set[str] = set()
        for lineno, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d["case_id"] in seen:
                raise ValueError(f"{path}:{lineno}: duplicate case_id {d['case_id']!r}")
            seen.add(d["case_id"])
            cases.append(Case(d["case_id"], d["prompt"], d.get("expected")))
        h = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        return cls(cases, h)
