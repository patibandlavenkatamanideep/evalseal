from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import ScorerKind
from .target import Target, TargetResponse

_VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b")

# How models mark a final answer, most trustworthy first. The phrase form demands a
# number, because "answer" also appears in ordinary prose ("...asks for the answer at
# the end, which implies...") and a loose pattern captures that sentence instead.
_HASH_RE = re.compile(r"####\s*([^\n]+)")
_BOXED_RE = re.compile(r"\\boxed\{([^}]*)\}")
_ANSWER_NUMBER_RE = re.compile(
    r"(?:final answer|answer)\s*(?:is|:|=)\s*\$?(-?\d[\d,]*(?:\.\d+)?)", re.I
)
_ANSWER_TEXT_RE = re.compile(r"(?:final answer|answer)\s*(?:is|:|=)\s*([^\n.]{1,40})", re.I)
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _as_number(text: str) -> float | None:
    """Parse one number, tolerating $ , % and stray words around it."""
    m = _NUMBER_RE.search(text.replace("$", "").replace("%", ""))
    if m is None:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:  # pragma: no cover - regex already constrains the shape
        return None


def extract_answer(text: str) -> str:
    """The model's final answer.

    Order of preference: an explicit `#### x` or `\\boxed{x}` marker, then "the answer
    is <number>", then the last number in the reply, then an "answer is <text>" phrase
    for non-numeric answers, then the whole reply. Later markers win over earlier ones,
    so a model that revises itself is graded on where it ended up.
    """
    for marker in (_HASH_RE, _BOXED_RE, _ANSWER_NUMBER_RE):
        found = marker.findall(text)
        if found:
            return found[-1].strip().rstrip(".").strip()
    numbers = _NUMBER_RE.findall(text)
    if numbers:
        return numbers[-1].strip()
    phrases = _ANSWER_TEXT_RE.findall(text)
    if phrases:
        return phrases[-1].strip().rstrip(".").strip()
    return text.strip()


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
class AnswerMatchScorer:
    """Compares the response's final answer to `expected` — the scorer a benchmark needs.

    Numbers compare numerically ("1,000" == "1000.0"), everything else compares as
    case-folded text. Extraction is deliberately simple and documented, because a
    clever extractor would hide model variance behind its own guesswork.
    """
    tolerance: float = 1e-6
    kind: ScorerKind = "answer_match"

    def score(self, prompt, response_text, expected):
        if expected is None:
            raise ValueError("answer_match scoring needs `expected` on every case")
        got = extract_answer(response_text)
        want = expected.strip()
        got_n, want_n = _as_number(got), _as_number(want)
        if got_n is not None and want_n is not None:
            ok = abs(got_n - want_n) <= self.tolerance
        else:
            ok = got.casefold() == want.casefold()
        return ScoreResult(float(ok), True, int(ok))


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
