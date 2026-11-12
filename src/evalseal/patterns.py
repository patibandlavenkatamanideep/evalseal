"""Refuse a grading pattern that can take longer than the evaluation it grades.

## Why this exists

`RegexScorer`'s pattern comes from a scorer config file. In a pull-request workflow that
file can come from a fork, and Python's `re` is a backtracking engine with no timeout.
Measured on this machine, on the committed build before this module:

    RegexScorer('^(a+)+$') on 31 characters of input: 25.12s

Each extra character roughly doubles it. At forty characters that is hours, inside a CI
job holding a cassette lock, with nothing to see but a hung step. It needs no hostility
to happen either - `([a-z]+)+` is the kind of thing someone writes by accident.

## Why the fix is a refusal and not a timeout

CPython's `sre` does not release the GIL while matching, so a watchdog thread cannot
interrupt it; `signal.alarm` is POSIX-only, main-thread-only, and this runs under a
thread pool. A subprocess per score would work and would cost more than the scoring.

So the pattern is **rejected before anything runs**, by the one structural property that
makes catastrophic backtracking possible: an unbounded quantifier applied to a group
that already contains an unbounded quantifier. That is what turns a linear scan into an
exponential one, because the same input can be divided between the two quantifiers in
exponentially many ways.

## What this does and does not catch

It catches the nested-quantifier family - `(a+)+`, `(a*)*`, `(\\d+)*`, `([a-z]+){2,}`,
`(x+)+$` - which is the family that blows up on inputs of a few dozen characters.

**It is not a proof of safety.** Alternation with overlapping branches under a
quantifier, such as `(a|ab)*`, is also exponential and is not rejected: deciding that in
general is deciding regular-expression ambiguity, which is more than a config check
should pretend to do. A linear-time engine would be the real answer, and swapping
`re` for one is a larger change than this phase.

What it does guarantee is that the cheap, common, accidental version is refused with a
message naming the construct, and that the restriction is documented rather than
discovered in a stuck job. See docs/operations.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Quantifiers that may repeat their target without an upper bound. `{n}` and `{n,m}`
#: are bounded, so nesting them cannot be exponential in the input length.
_UNBOUNDED = ("*", "+")


class UnsafePattern(ValueError):
    """A pattern whose matching time is not bounded by the length of the input."""


@dataclass(frozen=True)
class _Group:
    start: int          # index of the "("
    body_start: int     # index just past the group's opening syntax
    has_unbounded: bool = False


def _is_escaped(pattern: str, index: int) -> bool:
    """Whether `pattern[index]` is preceded by an odd number of backslashes."""
    backslashes = 0
    i = index - 1
    while i >= 0 and pattern[i] == "\\":
        backslashes += 1
        i -= 1
    return backslashes % 2 == 1


def _quantifier_at(pattern: str, index: int) -> str | None:
    """The unbounded quantifier starting at `index`, or None.

    `{2,}` counts: it repeats without an upper bound, so the same ambiguity applies.
    """
    if index >= len(pattern):
        return None
    char = pattern[index]
    if char in _UNBOUNDED:
        return char
    if char == "{":
        close = pattern.find("}", index)
        if close == -1:
            return None
        inside = pattern[index + 1:close]
        # `{2,}` is unbounded; `{2}` and `{2,5}` are not.
        if inside.endswith(",") and inside[:-1].isdigit():
            return pattern[index:close + 1]
    return None


def find_nested_quantifier(pattern: str) -> tuple[str, int] | None:
    """The first nested unbounded quantifier, as (construct, offset), or None.

    Walks the pattern once, tracking open groups and whether each has seen an unbounded
    quantifier at its own level. When a group closes and is itself quantified without an
    upper bound, that is the construct.

    Character classes are skipped: `[+*]` is two literals, not two quantifiers.
    """
    stack: list[_Group] = []
    i = 0
    in_class = False
    while i < len(pattern):
        char = pattern[i]
        if _is_escaped(pattern, i):
            i += 1
            continue
        if in_class:
            if char == "]":
                in_class = False
            i += 1
            continue
        if char == "[":
            in_class = True
            i += 1
            continue
        if char == "(":
            # Skip the group's opening syntax so `(?:` and `(?<name>` do not read as
            # content. A lookaround is still a group for this purpose: `(?=a+)+` is
            # not valid anyway, and `(?:a+)+` very much is.
            body = i + 1
            if pattern.startswith("(?", i):
                marker = pattern.find(">", i) if pattern.startswith("(?P<", i) else -1
                body = marker + 1 if marker != -1 else i + 3
            stack.append(_Group(start=i, body_start=body))
            i += 1
            continue
        if char == ")":
            if not stack:
                i += 1
                continue
            group = stack.pop()
            quantifier = _quantifier_at(pattern, i + 1)
            if quantifier is not None and group.has_unbounded:
                return (pattern[group.start:i + 1 + len(quantifier)], group.start)
            if quantifier is not None and stack:
                # A quantified group is itself an unbounded repetition at its parent's
                # level: `((a)+)+` has to be caught as well.
                stack[-1] = _Group(stack[-1].start, stack[-1].body_start, True)
            i += 1 + (len(quantifier) if quantifier else 0)
            continue
        quantifier = _quantifier_at(pattern, i)
        if quantifier is not None and stack:
            stack[-1] = _Group(stack[-1].start, stack[-1].body_start, True)
        i += len(quantifier) if quantifier else 1
    return None


def check_pattern(pattern: str, *, what: str = "the scorer pattern") -> None:
    """Raise `UnsafePattern` for a pattern this build will not run. Compiles it too.

    Compiling here means a malformed pattern is reported where the pattern is
    configured, rather than on the first case scored - by which point a cassette is
    open and a run is in flight.
    """
    try:
        re.compile(pattern)
    except re.error as e:
        raise UnsafePattern(
            f"{what} {pattern!r} is not a valid regular expression: {e}") from None
    found = find_nested_quantifier(pattern)
    if found is None:
        return
    construct, offset = found
    raise UnsafePattern(
        f"{what} {pattern!r} nests an unbounded quantifier inside another at offset "
        f"{offset} ({construct!r}). Python's regex engine backtracks, so matching this "
        f"is exponential in the length of the response: '^(a+)+$' takes 25 seconds on "
        f"31 characters and twice that for each one after. Rewrite it without the "
        f"nesting - {_suggest(construct)} - or grade with `answer_match` instead. "
        "docs/operations.md records the limit and what it does not cover."
    )


def _suggest(construct: str) -> str:
    """A concrete rewrite for the common shapes, so the error is actionable."""
    inner = construct[1:construct.rfind(")")]
    if inner.endswith(("+", "*")) and "(" not in inner:
        return f"`{inner}` alone usually matches the same strings"
    return "a single quantifier over the group's contents is usually equivalent"
