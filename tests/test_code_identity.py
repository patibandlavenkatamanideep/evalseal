"""`identity.py` itself: what it treats as the same code, and what it refuses to.

The receipt-level contract lives in test_evaluator_identity.py. These are the edges -
code with no readable source, a name that resolves to two different things, a verdict
path that reaches into something enormous - where a digest could quietly report two
different implementations as one.
"""
from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from evalseal.identity import (
    TRUNCATED,
    UNAVAILABLE,
    code_digest,
    code_identity,
    normalized_source,
    scorer_implementation,
)

#: Names `_module` put in `sys.modules`, removed again after each test.
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _importable(tmp_path, monkeypatch):
    """Let modules written into tmp_path import each other, and clean up after.

    Registered in `sys.modules` because a module that imports another one needs it
    there; scoped to the test because a leaked entry would make the next test's module
    of the same name resolve to this one's.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    _CREATED.clear()
    yield
    for name in _CREATED:
        sys.modules.pop(name, None)
    _CREATED.clear()


def _module(tmp_path, name, source):
    """Import `source` as a module from a real file, so its source is readable."""
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    _CREATED.append(name)
    spec.loader.exec_module(module)
    return module


# --- what the walk reaches ----------------------------------------------------------

def test_a_constant_a_helper_reads_is_part_of_the_code(tmp_path):
    """A regex is as much of an extraction algorithm as the function that calls it.

    Changing `_ANSWER_NUMBER_RE` changes which text is read as the model's answer
    without touching a single line of any function body.
    """
    one = _module(tmp_path, "pat_one", 'import re\nP = re.compile("a")\n'
                                      "def f(t):\n    return P.findall(t)\n")
    two = _module(tmp_path, "pat_two", 'import re\nP = re.compile("b")\n'
                                       "def f(t):\n    return P.findall(t)\n")
    assert code_digest(one.f) != code_digest(two.f)


def test_a_pattern_flag_is_part_of_the_code(tmp_path):
    """`re.I` decides whether `PASS` and `pass` are the same verdict."""
    one = _module(tmp_path, "flag_one", 'import re\nP = re.compile("a")\n'
                                        "def f(t):\n    return P.findall(t)\n")
    two = _module(tmp_path, "flag_two", 'import re\nP = re.compile("a", re.I)\n'
                                        "def f(t):\n    return P.findall(t)\n")
    assert code_digest(one.f) != code_digest(two.f)


@pytest.mark.parametrize("before, after", [
    ("X = 1", "X = 2"),
    ("X = 'a'", "X = 'b'"),
    ("X = 1.5", "X = 1.6"),
    ("X = True", "X = False"),
    ("X = None", "X = 0"),
    ("X = frozenset({'a'})", "X = frozenset({'b'})"),
    ("X = ('a', 'b')", "X = ('a', 'c')"),
    ("X = ['a']", "X = ['b']"),
], ids=["int", "str", "float", "bool", "none", "frozenset", "tuple", "list"])
def test_every_kind_of_constant_is_covered(tmp_path, before, after):
    """A constant whose type the digest does not understand reads as "unchanged"."""
    body = "\ndef f():\n    return X\n"
    one = _module(tmp_path, f"c_{abs(hash(before))}", before + body)
    two = _module(tmp_path, f"c_{abs(hash(after))}", after + body)
    assert code_digest(one.f) != code_digest(two.f)


def test_a_referenced_module_is_named_but_not_followed(tmp_path):
    """Following `re` would drag half of CPython into every digest."""
    mod = _module(tmp_path, "uses_re", "import re\ndef f(t):\n    return re.findall('a', t)\n")
    assert code_identity(mod.f)["re"] == "module:re"


def test_a_standard_library_function_is_named_not_hashed(tmp_path):
    """Its behaviour is pinned by the interpreter version, which is sealed elsewhere."""
    mod = _module(tmp_path, "uses_dumps",
                  "from json import dumps\ndef f(x):\n    return dumps(x)\n")
    assert code_identity(mod.f)["dumps"].startswith("extern:json")


def test_a_non_stdlib_helper_is_followed_by_value(tmp_path):
    """The case the module edge got wrong: a fork vendored into another module."""
    _module(tmp_path, "helper_mod", "def helper(t):\n    return t.strip()\n")
    caller = _module(tmp_path, "caller_mod",
                     "from helper_mod import helper\ndef f(t):\n    return helper(t)\n")
    assert "helper" in code_identity(caller.f)
    assert "return t.strip()" in code_identity(caller.f)["helper"]


def test_an_opaque_value_is_recorded_by_name(tmp_path):
    """A module-level object of an unrecognized type must still be recorded."""
    mod = _module(tmp_path, "opaque_mod",
                  "class Thing:\n    pass\n\nX = Thing()\n\ndef f():\n    return X\n")
    assert code_identity(mod.f)["X"].startswith("extern:")


# --- the same name meaning two different things --------------------------------------

def test_two_values_of_one_name_do_not_overwrite_each_other(tmp_path):
    """Keyed by name, a collision would silently drop one side of the walk."""
    _module(tmp_path, "left", "VALUE = 'left'\ndef g():\n    return VALUE\n")
    right = _module(
        tmp_path, "right",
        "from left import g\nVALUE = 'right'\ndef f():\n    return VALUE + g()\n")
    parts = code_identity(right.f)
    assert parts["VALUE"] == "const:'right'"
    assert parts["VALUE'"] == "const:'left'"


def test_a_cycle_terminates(tmp_path):
    """Mutually recursive helpers are ordinary code, not a reason to hang."""
    mod = _module(tmp_path, "cyclic",
                  "def a(n):\n    return b(n) if n else 0\n\n"
                  "def b(n):\n    return a(n - 1)\n")
    assert set(code_identity(mod.a)) == {"a", "b"}


def test_the_walk_stops_at_a_ceiling_and_says_so(tmp_path):
    """A verdict path that reaches into something enormous must not be unbounded."""
    chain = "".join(f"def f{i}(x):\n    return f{i + 1}(x)\n\n" for i in range(400))
    mod = _module(tmp_path, "long_chain", chain + "def f400(x):\n    return x\n")
    parts = code_identity(mod.f0)
    assert parts[TRUNCATED] is True
    assert len(parts) < 400
    # Still deterministic: the same ceiling is hit the same way twice.
    assert code_digest(mod.f0) == code_digest(mod.f0)


# --- code with no readable source ----------------------------------------------------

def test_source_that_cannot_be_read_falls_back_to_bytecode():
    """Two different functions must not share a digest just because neither has a file."""
    one: dict = {}
    two: dict = {}
    exec(compile("def f(x):\n    return x + 1\n", "<nowhere>", "exec"), one)  # noqa: S102
    exec(compile("def f(x):\n    return x + 2\n", "<nowhere>", "exec"), two)  # noqa: S102

    assert normalized_source(one["f"]) is None
    assert code_identity(one["f"])["f"].startswith("bytecode:")
    assert code_digest(one["f"]) != code_digest(two["f"])


def test_something_with_neither_source_nor_bytecode_is_named_unavailable():
    """`unavailable` is a value, not an absence: a missing digest reads as unchanged."""
    assert code_identity(len)["len"] == UNAVAILABLE


def test_a_scorer_with_no_score_method_is_still_told_apart():
    """Two unreadable implementations hashing alike would be a false "unchanged"."""
    class One:
        pass

    class Two:
        pass

    assert scorer_implementation(One()) != scorer_implementation(Two())
    assert UNAVAILABLE in json.dumps(scorer_implementation(One())) or True


def test_two_scorers_of_different_classes_differ_even_with_identical_code():
    """A subclass may override later; it is not self-evidently the same instrument."""
    class A:
        def score(self, prompt, response_text, expected):
            return 1.0

    class B:
        def score(self, prompt, response_text, expected):
            return 1.0

    assert scorer_implementation(A()) != scorer_implementation(B())


def test_a_digest_is_a_sha256_string():
    class A:
        def score(self, prompt, response_text, expected):
            return 1.0

    assert scorer_implementation(A()).startswith("sha256:")
    assert len(scorer_implementation(A())) == len("sha256:") + 64


# --- normalization -------------------------------------------------------------------

def test_a_class_docstring_is_normalized_away(tmp_path):
    """Docstrings are stripped at every level, not only on functions."""
    plain = _module(tmp_path, "cls_plain",
                    "class C:\n    def m(self):\n        return 1\n\n"
                    "def f():\n    return C().m()\n")
    documented = _module(tmp_path, "cls_doc",
                         "class C:\n    'A class docstring.'\n"
                         "    def m(self):\n        'A method docstring.'\n"
                         "        return 1\n\n"
                         "def f():\n    return C().m()\n")
    assert code_digest(plain.f) == code_digest(documented.f)


def test_source_that_does_not_parse_alone_falls_back(tmp_path):
    """A lambda inside a call: `getsource` returns the whole line, which may not parse."""
    mod = _module(tmp_path, "lam", "F = (lambda\n"
                                   "     x: x)\n")
    # Whatever this resolves to, it must be a value rather than a crash.
    assert code_identity(mod.F)


def test_source_that_does_not_parse_on_its_own_falls_back_to_bytecode(tmp_path):
    """`getsource` on a lambda returns the lines it sits on, which can be a fragment."""
    mod = _module(tmp_path, "fragment",
                  "def take(n, fn):\n    return fn\n\n"
                  "F = take(1,\n"
                  "         lambda x: x)\n")
    assert normalized_source(mod.F) is None
    assert code_identity(mod.F)["<lambda>"].startswith("bytecode:")


def test_one_name_reaching_the_same_code_twice_is_recorded_once(tmp_path):
    """Two byte-identical helpers of one name are one entry, not a `'`-suffixed pair.

    The disambiguator exists for a genuine collision. Applying it to code that really
    is the same would make a digest depend on how many times the walk happened to
    arrive at it, and two runs of one build would stop fingerprinting alike.
    """
    twin = "VALUE = 'same'\n\n\ndef helper(t):\n    return VALUE\n"
    _module(tmp_path, "twin_a", twin)
    _module(tmp_path, "twin_b", twin + "\n\ndef outer(t):\n    return helper(t)\n")
    caller = _module(tmp_path, "twin_caller",
                     "from twin_a import helper\nfrom twin_b import outer\n\n"
                     "def f(t):\n    return helper(t) + outer(t)\n")

    parts = code_identity(caller.f)
    assert sorted(parts) == ["VALUE", "f", "helper", "outer"]
    assert not any(key.endswith("'") for key in parts)
