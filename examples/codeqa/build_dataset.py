"""Build a code-comprehension eval from real repositories.

Every expected answer is derived from the Python AST, not written by hand: the question
asks about a fact the parser can state exactly (a default value, a parameter count, the
exception a function raises, its declared return type). That keeps the eval objective —
`answer_match` can grade it without a judge — and lets anyone regenerate it.

    python examples/codeqa/build_dataset.py <dir-of-repos> [n]

`<dir-of-repos>` is a directory containing checked-out repositories.
"""
from __future__ import annotations

import ast
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

SEED = 20260918
MAX_SNIPPET_LINES = 90          # keep prompts small enough to stay cheap
MAX_ANSWER_CHARS = 40
SKIP_PARTS = {".git", "tests", "test", "node_modules", "venv", ".venv", "migrations"}

INSTRUCTION = "Answer with only the value, no quotes, no explanation, no code fences."


@dataclass
class Item:
    kind: str
    repo: str
    path: str
    func: str
    question: str
    expected: str
    snippet: str


def _literal(node: ast.expr) -> str | None:
    """Render a literal default as the plain text a person would say out loud."""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, str):
            return value or None          # empty string is not a fair question
        if value is None or isinstance(value, bool):
            return str(value)
        if isinstance(value, (int, float)):
            return repr(value)
    return None


def _looks_like_class(name: str) -> bool:
    return name[:1].isupper()


def _unique_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    seen: dict[str, ast.FunctionDef | ast.AsyncFunctionDef | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seen[node.name] = None if node.name in seen else node
    return {name: node for name, node in seen.items() if node is not None}


def _snippet(source_lines: list[str], node: ast.AST) -> str | None:
    start, end = node.lineno - 1, getattr(node, "end_lineno", node.lineno)
    if end - start > MAX_SNIPPET_LINES:
        return None
    return "\n".join(source_lines[start:end])


def _items_for(func, repo: str, path: str, lines: list[str]) -> list[Item]:
    snippet = _snippet(lines, func)
    if snippet is None:
        return []
    out: list[Item] = []

    # 1. Default value of a keyword parameter.
    args = func.args
    positional = args.posonlyargs + args.args
    defaults = dict(zip([a.arg for a in positional[len(positional) - len(args.defaults):]],
                        args.defaults, strict=True))
    defaults.update({a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d})
    for name, default in defaults.items():
        value = _literal(default)
        if value and len(value) <= MAX_ANSWER_CHARS and name not in {"self", "cls"}:
            out.append(Item(
                "default_value", repo, path, func.name,
                f"What is the default value of the `{name}` parameter of `{func.name}`?",
                value, snippet,
            ))

    # 2. Parameter count (self/cls excluded, stated in the question).
    names = [a.arg for a in positional + args.kwonlyargs if a.arg not in {"self", "cls"}]
    if names:
        out.append(Item(
            "param_count", repo, path, func.name,
            f"How many parameters does `{func.name}` accept, not counting `self` or `cls` "
            f"and not counting *args or **kwargs?",
            str(len(names)), snippet,
        ))

    # 3. The exception it raises, when there is exactly one kind.
    #    `raise some_var` re-raises a *value*, so the type is not in the source — asking
    #    about it produces an unanswerable question. Require a class-looking name, and
    #    skip functions with nested defs so an inner raise is not attributed to the outer.
    has_nested = any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not func
        for n in ast.walk(func)
    )
    raised = {
        r.exc.func.id if isinstance(r.exc, ast.Call) and isinstance(r.exc.func, ast.Name)
        else r.exc.id if isinstance(r.exc, ast.Name) else None
        for r in ast.walk(func) if isinstance(r, ast.Raise) and r.exc is not None
    }
    raised.discard(None)
    if len(raised) == 1 and not has_nested and _looks_like_class(next(iter(raised))):
        out.append(Item(
            "raises", repo, path, func.name,
            f"Which exception type does `{func.name}` raise explicitly with a `raise` "
            f"statement in its own body? Ignore exceptions that functions it calls might "
            f"propagate. Name the single exception class.",
            raised.pop(), snippet,
        ))

    # 4. Declared return type, when annotated with a plain name.
    if isinstance(func.returns, ast.Name):
        out.append(Item(
            "return_type", repo, path, func.name,
            f"What is the declared return type annotation of `{func.name}`?",
            func.returns.id, snippet,
        ))
    return out


def collect(root: Path) -> list[Item]:
    items: list[Item] = []
    for repo_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for file in sorted(repo_dir.rglob("*.py")):
            rel = file.relative_to(repo_dir)
            if SKIP_PARTS & set(rel.parts) or rel.name.startswith("test_"):
                continue
            try:
                source = file.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError):
                continue
            lines = source.splitlines()
            for func in _unique_functions(tree).values():
                items += _items_for(func, repo_dir.name, str(rel), lines)
    return items


def main(root: str, n: int = 60) -> None:
    pool = collect(Path(root))
    rng = random.Random(SEED)
    by_kind: dict[str, list[Item]] = {}
    for item in pool:
        by_kind.setdefault(item.kind, []).append(item)

    picked: list[Item] = []
    per_kind = max(1, n // len(by_kind))
    for kind in sorted(by_kind):
        candidates = sorted(by_kind[kind], key=lambda i: (i.repo, i.path, i.func, i.question))
        picked += rng.sample(candidates, min(per_kind, len(candidates)))
    picked = picked[:n]
    rng.shuffle(picked)

    out = Path(__file__).with_name("dataset.jsonl")
    with out.open("w") as f:
        for i, item in enumerate(picked, start=1):
            prompt = (
                f"Here is a function from `{item.repo}/{item.path}`:\n\n"
                f"```python\n{item.snippet}\n```\n\n"
                f"Question: {item.question}\n{INSTRUCTION}"
            )
            f.write(json.dumps({
                "case_id": f"cq{i:03d}",
                "prompt": prompt,
                "expected": item.expected,
                "kind": item.kind,
                "source": f"{item.repo}/{item.path}::{item.func}",
            }) + "\n")
    counts: dict[str, int] = {}
    for item in picked:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    print(f"wrote {out} — {len(picked)} cases from a pool of {len(pool)}; by kind: {counts}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60)
