from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import ScorerKind
from ..patterns import check_pattern
from .target import Target, TargetResponse

_VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b")

#: The declared answer format. A reply that uses none of these forms has not given an
#: answer, and is *unscorable* rather than scored on a guess. See docs/metrics.md §11.
#:
#: There used to be one more rule: "otherwise, the last number appearing anywhere in
#: the reply". It made a non-answer indistinguishable from an answer. A refusal that
#: restated the problem - "I can't determine that ... it says he eats 4 on each of 5
#: weekdays, which is 20, and then 30" - scored a PASS against an expected 30, exactly
#: as a reply that worked the problem and concluded 30 did. The same rule graded a
#: reply truncated mid-calculation on whichever digit the cut landed on, reporting a
#: wrong answer where no answer had been given.
_HASH_RE = re.compile(r"####[ \t]*([^\n]+)")
_BOXED_RE = re.compile(r"\\boxed\{([^}]*)\}")
_PHRASE_RE = re.compile(r"(?:final answer|answer)\s*(?:is|:|=)\s*([^\n.]{1,40})", re.I)
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
#: A line that is one bare number, with the decoration a model puts around one. The
#: sign is captured separately so stripping the currency symbol cannot drop it.
_BARE_NUMBER_LINE = re.compile(r"^([-+]?)\$?(\d[\d,]*(?:\.\d+)?)\$?%?$")
#: A line that is one bare token: `float`, `BudgetExceeded`, `yes`. No whitespace, so a
#: closing sentence cannot be mistaken for an answer. A multi-word answer needs a
#: marker, which is a restriction the format states rather than a guess it makes.
_BARE_TOKEN_LINE = re.compile(r"^\S{1,40}$")

HASH = "hash_marker"
BOXED = "boxed_marker"
PHRASE = "answer_phrase"
FINAL_LINE = "final_line"
#: Which form the answer was found in, for a receipt to report. Ordered as the format
#: is documented, not by precedence: precedence is by position in the reply.
ANSWER_RULES = (HASH, BOXED, PHRASE, FINAL_LINE)


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


@dataclass(frozen=True)
class Answer:
    """One answer found in a reply, and which declared form carried it."""
    value: str
    rule: str


def _final_line_answer(text: str) -> tuple[int, Answer] | None:
    """The last non-empty line, when that line is the answer standing alone.

    `examples/gsm8k*` asks for exactly this ("End your reply with the final number on
    its own line"), so it is part of the format rather than a fallback. A line with
    internal whitespace is not accepted: a closing sentence would otherwise be read as
    an answer, which is the guessing this format exists to stop.

    Returns the line's end offset with the answer, so `find_answer` can rank it against
    the markers by position like any other candidate.
    """
    end = len(text)
    for raw in reversed(text.splitlines()):
        start = end - len(raw)
        candidate = strip_decoration(strip_decoration(raw).rstrip("."))
        if candidate:
            compact = candidate.replace(" ", "")
            number = _BARE_NUMBER_LINE.match(compact)
            if number is not None:
                sign = number.group(1) if number.group(1) == "-" else ""
                return (end, Answer(sign + number.group(2), FINAL_LINE))
            if _BARE_TOKEN_LINE.match(candidate):
                return (end, Answer(candidate, FINAL_LINE))
            return None
        end = max(0, start - 1)     # step back over this line and its newline
    return None


def find_answer(text: str) -> Answer | None:
    """The reply's final answer in the declared format, or None if it gave none.

    Precedence is **by position in the reply, not by which form it used**: whichever
    declared answer appears last wins, so a model that revises itself is graded on
    where it ended up. Taking the marker classes in a fixed order instead meant a
    chain of thought containing "the answer is 4" outranked the reply's own final line
    saying 7, and 37 of 750 recorded gemma replies were graded on a number the model
    had already moved past.
    """
    candidates: list[tuple[int, Answer]] = []
    for rule, pattern in ((HASH, _HASH_RE), (BOXED, _BOXED_RE), (PHRASE, _PHRASE_RE)):
        for match in pattern.finditer(text):
            value = strip_decoration(match.group(1).rstrip("."))
            if value:
                candidates.append((match.end(), Answer(value, rule)))
    tail = _final_line_answer(text)
    if tail is not None:
        candidates.append(tail)
    if not candidates:
        return None
    # max() keeps the first of equal keys; markers are appended before the final line,
    # so a marker that ends exactly where the line does - `#### 72` as the last line -
    # is preferred over reading that same line as a bare answer.
    return max(candidates, key=lambda c: c[0])[1]


def extract_answer(text: str) -> str | None:
    """The reply's final answer, or None when it did not give one in the format.

    Returned None rather than a guess since 2.3. Callers that treated the old return
    value as "always an answer" scored refusals and truncated replies as answers.
    """
    found = find_answer(text)
    return None if found is None else found.value


@dataclass
class ScoreResult:
    score: float            # 0/1 for binary scorers
    binary: bool
    verdict: int | None  # 0/1 or None
    judge_response: TargetResponse | None = None
    #: The grader could not reach a verdict at all: no answer in the declared format,
    #: a judge reply with no PASS/FAIL token, an empty reply. **Not** a FAIL. It keeps
    #: `score=0.0` so it can never inflate a mean, and is counted separately so a run
    #: cannot look better by becoming less legible. See docs/metrics.md §8.
    unscorable: bool = False
    #: Why, in a few words, for the receipt and the report.
    reason: str | None = None


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

    def __post_init__(self) -> None:
        # Checked at construction, not on the first case scored: by then a cassette is
        # open and a run is in flight. `^(a+)+$` took 25 seconds on 31 characters.
        check_pattern(self.pattern)

    def config(self) -> dict:
        return {"pattern": self.pattern}

    def score(self, prompt, response_text, expected):
        ok = re.search(self.pattern, response_text) is not None
        return ScoreResult(float(ok), True, int(ok))


def parse_verdict(text: str) -> int | None:
    """First standalone PASS/FAIL token wins. **None when the reply has neither.**

    A plain substring check would read "FAIL - this does not PASS" as a pass, hence the
    word boundaries. The None is the Phase D change: this returned 0 for anything it
    could not parse, so an empty reply, an apology and `ERROR: 503` were all graded as
    the judge having failed the case. A rate-limited judge then looked exactly like a
    model that got every case wrong. See docs/metrics.md §8.
    """
    m = _VERDICT_RE.search(text.upper())
    if m is None:
        return None
    return 1 if m.group(1) == "PASS" else 0


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
        # `extract_answer`, not `find_answer`: the extraction algorithm is the seam
        # the evaluator fingerprint is taken across, so the scorer calls the same
        # function a fork would replace. `find_answer` is its implementation.
        found = extract_answer(response_text)
        if found is None:
            # No answer in the declared format. Scored 0.0 so it cannot lift a mean,
            # and flagged so it is not read as the model having answered wrongly.
            return ScoreResult(
                0.0, True, None, unscorable=True,
                reason="the reply gives no answer in the declared format "
                       "(#### x, \\boxed{x}, 'answer: x', or the answer alone on the "
                       "last line)")
        got = strip_decoration(found)
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
        if verdict is None:
            # The judge did not grade. That is a broken measurement, not a failed case:
            # an empty reply, a refusal and a provider error used to arrive as FAIL.
            return ScoreResult(
                0.0, True, None, judge_response=jr, unscorable=True,
                reason="the judge reply contains no PASS or FAIL token"
                       + (" (the reply was empty)" if not jr.text.strip() else ""))
        return ScoreResult(float(verdict), True, verdict, judge_response=jr)
