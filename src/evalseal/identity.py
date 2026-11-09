"""Identity of the code that decides a verdict.

## Why this module exists

Scheme 2 of the evaluator fingerprint sealed the scorer's *configuration* - a regex, a
numeric tolerance, the judge's model and sampling parameters - and nothing at all about
the algorithm that turns a response into a score. So this happened, and the receipt
could not see it:

    $ evalseal run ...                      # mean 1.0
    # change how `extract_answer` picks the answer out of a reply
    $ evalseal run ...                      # mean 0.0
    # both receipts: evaluator_fingerprint = evalseal-fp/2:sha256:1488ed19...

`--expect-evaluator` passed. `evalseal diff` reported `comparable: true` with no
configuration change. Two runs that measured opposite things were presented as
measuring the same thing with different results, which is the one claim an evaluation
receipt exists to refuse.

A changed extraction or normalization algorithm is a changed instrument even when the
user's config is untouched, so it has to change evaluator identity.

## What identity is taken from

The **normalized syntax tree** of the scoring method, and of every name it reaches that
is defined in the same module - transitively. `ast.parse` drops comments, and
docstrings are deleted explicitly, so prose and layout are not part of identity. Only
what the code *does* is.

Two properties follow, and both are tested:

- Changing any step of the scoring path - the method, a helper, or one of the module
  constants a helper reads, such as a regex - changes the digest.
- Rewriting a comment, a docstring or the line wrapping does not. This direction
  matters as much as the other: an over-sensitive digest re-pins every receipt in the
  repository every time someone improves a docstring, and a check that fires constantly
  for no reason is a check people turn off.

## Why the reachable set is computed rather than listed

A hand-maintained list of "the functions that matter" fails in the unsafe direction:
add a helper to the scoring path, forget to list it, and the helper is invisible to
every receipt from then on. Computing the set from the code means a new helper is
covered by default and someone has to work to exclude it.

The walk is bounded at the standard library. `float`, `re` and `json.dumps` are
recorded as qualified names and not followed: their behaviour is pinned by the
interpreter version, which is sealed separately in `manifest.environment`, and
following them would drag half of CPython into every digest.

Everything else is followed by value, including code from another module. That matters
for the case the module edge would get wrong: a fork of `extract_answer` vendored into
somebody's own module and installed over the original. Recorded by name, two different
forks sharing a name would be indistinguishable; recorded by value, the digest says
what each one actually does. The walk stops after `_MAX_PARTS` entries and says so, so
that a verdict path reaching into a large dependency cannot make the digest unbounded.

## What this does not claim

A digest over the shipped source says what the code in this build *is*. It does not
prove that code is what ran: a sealed digest is a record made by the same process it
describes. What it does do is make an undeclared change to the grading algorithm
visible to a reader comparing two receipts, which is the gap it was written to close.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import marshal
import re
import sys
import textwrap
from types import CodeType, ModuleType
from typing import Any

#: A hard ceiling on how much code one digest describes. Reached only by a verdict path
#: that calls into a large library; recorded as a part of its own so that hitting it is
#: visible in the digest rather than silently changing what was covered.
_MAX_PARTS = 256
TRUNCATED = "truncated"

#: Stands in for source that could not be read *and* bytecode that could not be
#: marshalled. It is a value, not an absence, so that `impl_hash` is never silently
#: omitted - a missing digest reads as "unchanged", which is the failure this module
#: exists to prevent.
UNAVAILABLE = "unavailable"


def _strip_docstrings(tree: ast.AST) -> None:
    """Delete every docstring in place, so prose is not part of code identity."""
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]


def normalized_source(obj: object) -> str | None:
    """`obj`'s source with comments, docstrings and layout normalized away.

    None when the source cannot be read or parsed - a function defined in a REPL, in a
    frozen build, or by `exec` without a real file behind it. Callers fall back to the
    bytecode rather than treating None as "nothing to see".
    """
    try:
        source = textwrap.dedent(inspect.getsource(obj))  # type: ignore[arg-type]
    except (OSError, TypeError, IndentationError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    _strip_docstrings(tree)
    return ast.unparse(tree)


def _bytecode_digest(func: object) -> str:
    """Last-resort identity for code with no readable source.

    Interpreter-dependent and more sensitive than the source digest: it moves with the
    Python version, with the docstring, and with the line the function sits on. That is
    acceptable only as a fallback, because the alternative is reporting two different
    functions as the same one. A receipt produced on a build where this path is reached
    should not be compared across interpreter versions.
    """
    code = getattr(func, "__code__", None)
    if not isinstance(code, CodeType):
        return UNAVAILABLE
    try:
        return "bytecode:" + hashlib.sha256(marshal.dumps(code)).hexdigest()[:32]
    except ValueError:  # pragma: no cover - unmarshallable constants are exotic
        return UNAVAILABLE


def _referenced_names(source: str) -> list[str]:
    """Global names the given source reads, in sorted order.

    Parameters and locals are included too. They simply fail to resolve against the
    module's globals and are dropped, which is cheaper than a full scope analysis and
    cannot miss a global.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - source came from ast.unparse
        return []
    names = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return sorted(names)


def _value_identity(value: object) -> str | None:
    """Identity of a non-callable module constant, or None if it is not one.

    A compiled pattern is as much a part of an extraction algorithm as the function
    that calls it: changing `_ANSWER_NUMBER_RE` changes which text is read as the
    model's answer without touching a single line of any function body.
    """
    if isinstance(value, re.Pattern):
        return f"re:{value.flags}:{value.pattern}"
    if isinstance(value, str | int | float | bool) or value is None:
        return f"const:{value!r}"
    if isinstance(value, frozenset | set):
        return "set:" + repr(sorted(repr(v) for v in value))
    if isinstance(value, tuple | list):
        return "seq:" + repr([repr(v) for v in value])
    return None


def _extern_name(value: object) -> str:
    module = getattr(value, "__module__", None) or "?"
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    return f"extern:{module}.{name}" if name else f"extern:{module}"


def _is_stdlib(value: object) -> bool:
    """Whether `value` comes from the standard library, which is pinned elsewhere."""
    module = (getattr(value, "__module__", None) or "").split(".")[0]
    return module in sys.stdlib_module_names


def code_identity(func: object) -> dict[str, Any]:
    """Everything `func` does, by value, as a flat name -> identity map.

    Keyed by the name each part is referenced under rather than by its module, so a
    byte-identical helper vendored into another module is the same instrument. A name
    that resolves to two different things in one walk gets a `'` suffix, so neither
    silently replaces the other.
    """
    parts: dict[str, Any] = {}
    _walk(func, parts, set())
    return parts


def _place(parts: dict[str, Any], name: str, identity: Any) -> bool:
    """Record `identity` under `name`; returns False if it was already there."""
    key = name
    while key in parts:
        if parts[key] == identity:
            return False
        key += "'"
    parts[key] = identity
    return True


def _walk(func: object, parts: dict[str, Any], seen: set[int]) -> None:
    """Add `func`, and everything outside the standard library it reaches, to `parts`."""
    if id(func) in seen:
        return
    seen.add(id(func))
    if len(parts) >= _MAX_PARTS:
        parts[TRUNCATED] = True
        return
    source = normalized_source(func)
    name = str(getattr(func, "__qualname__", None)
               or getattr(func, "__name__", None) or "?")
    identity = source if source is not None else _bytecode_digest(func)
    if not _place(parts, name, identity):
        return
    if source is None:
        # Nothing to walk: without source there is no reliable way to see what this
        # code reads, and the bytecode digest already covers its constants and names.
        return
    namespace = getattr(func, "__globals__", {})
    for referenced in _referenced_names(source):
        if referenced not in namespace:
            continue  # a local, a parameter, or a builtin: nothing module-specific
        value = namespace[referenced]
        if isinstance(value, ModuleType):
            _place(parts, referenced, f"module:{value.__name__}")
        elif inspect.isfunction(value) or inspect.isclass(value):
            if _is_stdlib(value):
                _place(parts, referenced, _extern_name(value))
            else:
                _walk(value, parts, seen)
        elif (constant := _value_identity(value)) is not None:
            _place(parts, referenced, constant)
        else:
            _place(parts, referenced, _extern_name(value))


def code_digest(func: object) -> str:
    """One `sha256:` digest of `func` and the same-module code it reaches."""
    blob = json.dumps(code_identity(func), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def scorer_implementation(scorer: object) -> str:
    """Digest of the code this scorer uses to turn a response into a score.

    The class is named in the digest as well as hashed, so two scorers whose source
    cannot be read are still told apart. Without that, an unreadable implementation
    would hash identically to every other unreadable one - a false "unchanged".
    """
    cls = type(scorer)
    score = getattr(cls, "score", None)
    payload = {
        "class": f"{cls.__module__}.{cls.__qualname__}",
        "score": code_identity(score) if score is not None else UNAVAILABLE,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


__all__ = [
    "TRUNCATED",
    "UNAVAILABLE",
    "code_digest",
    "code_identity",
    "normalized_source",
    "scorer_implementation",
]
