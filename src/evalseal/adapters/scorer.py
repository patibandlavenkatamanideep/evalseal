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


_DECORATION = "`'\" \t"


def strip_decoration(value: str) -> str:
    """Remove markdown/quote decoration around an answer.

    Models routinely return `RetrievalResult` when asked for a bare identifier. That is
    the same answer in code formatting, not a different one, and scoring it wrong turns a
    formatting habit into fake model variance — which is exactly what happened here: 11 of
    11 "wrong" answers in one run were the right identifier wrapped in backticks. This
    normalizes presentation only; it never rewrites the value itself.
    """
    return value.strip().strip(_DECORATION).strip()


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


def scorer_config(scorer: object) -> dict:
    """The scorer's own settings that change a verdict, for the receipt to seal.

    The scorer type alone is not the instrument: `regex "^yes$"` and `regex "^no$"` are
    different graders, and until schema 1.4 they fingerprinted identically, so `diff`
    called two runs comparable that measured opposite things.
    """
    config = getattr(scorer, "config", None)
    return dict(config()) if callable(config) else {}


@dataclass
class ExactMatchScorer:
    kind: ScorerKind = "exact"

    def config(self) -> dict:
        return {}

    def score(self, prompt, response_text, expected):
        ok = expected is not None and response_text.strip() == expected.strip()
        return ScoreResult(float(ok), True, int(ok))


@dataclass
class RegexScorer:
    pattern: str
    kind: ScorerKind = "regex"

    def config(self) -> dict:
        return {"pattern": self.pattern}

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

    def config(self) -> dict:
        return {"tolerance": self.tolerance}

    def score(self, prompt, response_text, expected):
        if expected is None:
            raise ValueError("answer_match scoring needs `expected` on every case")
        got = strip_decoration(extract_answer(response_text))
        want = strip_decoration(expected)
        got_n, want_n = _as_number(got), _as_number(want)
        if got_n is not None and want_n is not None:
            ok = abs(got_n - want_n) <= self.tolerance
        else:
            ok = got.casefold() == want.casefold()
        return ScoreResult(float(ok), True, int(ok))


def build_judge_prompt(rubric: str, prompt: str, response: str) -> str:
    """The exact text sent to the judge.

    One function both sends the prompt and defines the sealed template, so the two
    cannot drift apart. Changing a byte here changes every judge request and therefore
    every judge cassette key, which is correct: it is a different question to the judge.
    """
    return (
        f"{rubric}\n\n"
        f"USER PROMPT:\n{prompt}\n\n"
        f"RESPONSE TO GRADE:\n{response}\n\n"
        f"Answer with exactly one word: PASS or FAIL."
    )


@dataclass
class LLMJudgeScorer:
    """Grades a response pass/fail using another model. The judge is a Target, so it
    gets its own recording + provenance. This is where flips are most visible."""
    judge: Target
    rubric: str
    kind: ScorerKind = "llm_judge"

    def config(self) -> dict:
        # The rubric and prompt template are hashed separately; the judge is a target
        # and carries its own provenance.
        return {}
    # Informational only, and racy under concurrency. Never sealed: until schema 1.3 the
    # receipt hashed this, which made the judge prompt hash depend on whichever case was
    # scored last and on that case's response.
    last_judge_prompt: str | None = None

    @property
    def rubric_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.rubric.encode()).hexdigest()

    @property
    def judge_prompt_template(self) -> str:
        """The judge prompt with the rubric in place and the case left as placeholders.

        This is what the receipt seals. It is constant for a given rubric and wrapper,
        so its hash changes when the way the judge is asked changes - the rubric, or the
        instruction wrapper around it - and not when the target answers differently. It
        also carries no case text, so storing it verbatim leaks nothing from the dataset.
        """
        return build_judge_prompt(self.rubric, "{prompt}", "{response}")

    def score(self, prompt, response_text, expected):
        judge_prompt = build_judge_prompt(self.rubric, prompt, response_text)
        self.last_judge_prompt = judge_prompt
        jr = self.judge.generate(judge_prompt)
        verdict = parse_verdict(jr.text)
        return ScoreResult(float(verdict), True, verdict, judge_response=jr)
