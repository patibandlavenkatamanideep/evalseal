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
    def from_jsonl(cls, path: str | Path) -> Dataset:
        """Load from a file. Prefer `from_bytes` when the bytes are already captured.

        Reading the file here means the parse and any digest taken elsewhere are of two
        separate reads, and a file rewritten between them leaves a receipt describing
        bytes that produced no score. `evalseal run` captures the dataset once and calls
        `from_bytes`; this stays for every other caller.
        """
        return cls.from_bytes(Path(path).read_bytes(), what=str(path))

    @classmethod
    def from_bytes(cls, data: bytes, what: str = "<bytes>") -> Dataset:
        """Parse the exact bytes given, and hash the same bytes.

        `what` names the source in an error message only. The hash is of the decoded
        text with line endings normalized, which is what `read_text` returns and
        therefore what every dataset hash sealed so far was computed over - a CRLF
        dataset keeps the digest it has always had.
        """
        path = what
        raw = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
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
