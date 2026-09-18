from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import ScorerKind
from .target import Target, TargetResponse

_VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b")


@dataclass
class ScoreResult:
    score: float            # 0/1 for binary scorers
    binary: bool
    verdict: int | None  # 0/1 or None
    judge_response: TargetResponse | None = None


@runtime_checkable
class Scorer(Protocol):
    kind: ScorerKind
    def score(self, prompt: str, response_text: str, expected: str | None) -> ScoreResult: ...


@dataclass
class ExactMatchScorer:
    kind: ScorerKind = "exact"

    def score(self, prompt, response_text, expected):
        ok = expected is not None and response_text.strip() == expected.strip()
        return ScoreResult(float(ok), True, int(ok))


@dataclass
class RegexScorer:
    pattern: str
    kind: ScorerKind = "regex"

    def score(self, prompt, response_text, expected):
        ok = re.search(self.pattern, response_text) is not None
        return ScoreResult(float(ok), True, int(ok))


def parse_verdict(text: str) -> int:
    """First standalone PASS/FAIL token wins; anything unparseable counts as FAIL.
    (A plain substring check would read "FAIL — this does not PASS" as a pass.)"""
    m = _VERDICT_RE.search(text.upper())
    return 1 if m and m.group(1) == "PASS" else 0


@dataclass
class LLMJudgeScorer:
    """Grades a response pass/fail using another model. The judge is a Target, so it
    gets its own recording + provenance. This is where flips are most visible."""
    judge: Target
    rubric: str
    kind: ScorerKind = "llm_judge"

    @property
    def rubric_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.rubric.encode()).hexdigest()

    def score(self, prompt, response_text, expected):
        judge_prompt = (
            f"{self.rubric}\n\n"
            f"USER PROMPT:\n{prompt}\n\n"
            f"RESPONSE TO GRADE:\n{response_text}\n\n"
            f"Answer with exactly one word: PASS or FAIL."
        )
        jr = self.judge.generate(judge_prompt)
        verdict = parse_verdict(jr.text)
        return ScoreResult(float(verdict), True, verdict, judge_response=jr)
