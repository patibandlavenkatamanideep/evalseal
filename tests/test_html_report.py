"""The HTML receipt: self-contained, deterministic, and safe to hand to someone."""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.dataset import Case, Dataset
from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.executor import run_eval
from evalseal.htmlreport import to_html, write_html
from evalseal.ledger import seal_and_append
from evalseal.report import write_json

runner = CliRunner()


def _record(**scripts):
    scripts = scripts or {
        "stable": ["yes"],
        "unstable": ["yes", "no", "yes", "no", "yes"],
    }
    target = LocalCallableTarget(scripted(scripts))
    ds = make_dataset(*scripts)
    return run_eval(ds, target, RegexScorer(r"^yes$"), n_repeats=5)


class _WellFormed(HTMLParser):
    """Enough of a parser to catch unbalanced tags in a hand-built template."""

    VOID = {"meta", "br", "hr", "img", "input", "link"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack:
            self.errors.append(f"</{tag}> with nothing open")
        elif self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes <{self.stack[-1]}>")
        else:
            self.stack.pop()


def test_html_is_well_formed_and_declares_a_doctype():
    html = to_html(_record())
    assert html.startswith("<!doctype html>")
    parser = _WellFormed()
    parser.feed(html)
    assert parser.errors == []
    assert parser.stack == []


def test_html_fetches_nothing_external():
    """A receipt opened offline a year later must render exactly as it does today."""
    html = to_html(_record())
    for pattern in ("http://", "https://", "<script", "<link", "src="):
        assert pattern not in html, f"report reaches for {pattern!r}"


def test_html_is_deterministic():
    """Same record, same bytes: the receipt can itself be hashed."""
    record = _record()
    assert to_html(record) == to_html(record)


def test_case_ids_are_escaped_not_injected():
    """Case ids come from a dataset file, which is not necessarily trusted input."""
    hostile = "<img src=x onerror=alert(1)>"
    ds = Dataset([Case(hostile, "p")], hash="sha256:test")
    record = run_eval(ds, LocalCallableTarget(scripted({"p": ["yes"]})),
                      RegexScorer(r"^yes$"), n_repeats=5)

    html = to_html(record)
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_verdict_strip_has_one_cell_per_run_in_order():
    record = _record(flipper=["yes", "no", "yes", "yes", "yes"])
    html = to_html(record)
    cells = re.findall(r'<i( class="f")? title="run (\d+): (pass|fail)"></i>', html)
    assert [c[2] for c in cells] == ["pass", "fail", "pass", "pass", "pass"]
    assert [c[1] for c in cells] == ["1", "2", "3", "4", "5"]


def test_payloads_are_never_embedded_only_hashes():
    """The receipt is meant to be shareable, so it must not carry the dataset."""
    scripts = {"secret-prompt-text": ["yes"]}
    target = LocalCallableTarget(scripted(scripts))
    ds = make_dataset(*scripts)
    record = run_eval(ds, target, RegexScorer(r"^yes$"), n_repeats=5)
    record.manifest.scorer.judge_prompt = "verbatim judge prompt with case text"

    html = to_html(record)
    assert "secret-prompt-text" not in html
    assert "verbatim judge prompt" not in html
    assert record.manifest.dataset.hash in html


def test_warnings_are_surfaced():
    record = _record()
    record.aggregate.warnings = ["served model gpt-4o-2024 != requested gpt-4o"]
    assert "served model gpt-4o-2024 != requested gpt-4o" in to_html(record)


def test_unstable_only_drops_the_stable_cases():
    record = _record()
    # Match the case-id cell, not the bare string: `c0` also occurs inside hex hashes.
    def ids(html):
        return set(re.findall(r'<td class="mono">(c\d+)</td>', html))

    assert ids(to_html(record)) == {"c0", "c1"}
    assert ids(to_html(record, unstable_only=True)) == {"c1"}


def test_empty_selection_says_so_rather_than_rendering_an_empty_table():
    record = _record(allgood=["yes"])
    assert "No unstable cases." in to_html(record, unstable_only=True)


def test_per_case_halfwidth_and_flip_rate_appear_in_the_summary():
    html = to_html(_record())
    assert "per-case halfwidth" in html
    assert "noise floor" not in html          # the misleading name is gone
    assert "mean flip rate" in html
    # The receipt has to say what the number does and does not describe.
    assert "describes a single item at this N, not the suite mean" in html


def test_write_html_uses_utf8_regardless_of_locale(tmp_path):
    """Windows defaults to cp1252, which cannot encode the separators this page uses."""
    out = tmp_path / "r.html"
    write_html(_record(), out)
    assert "·" in out.read_text(encoding="utf-8")


def test_cli_writes_html_from_a_receipt_file(tmp_path):
    record = seal_and_append(_record(), tmp_path / "ledger.jsonl")
    receipt = tmp_path / "report.json"
    write_json(record, receipt)
    out = tmp_path / "out.html"

    result = runner.invoke(app, ["report", str(receipt), "--html", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists() and record.hash[:20] in out.read_text(encoding="utf-8")


def test_cli_writes_html_from_a_ledger_index(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    seal_and_append(_record(), ledger)
    out = tmp_path / "out.html"

    result = runner.invoke(app, ["report", "-1", "--ledger", str(ledger),
                                 "--html", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_negative_ledger_indices_are_not_parsed_as_options(tmp_path):
    """`diff 0 -1` is the documented spelling; click reads bare -1 as an option."""
    ledger = tmp_path / "ledger.jsonl"
    for _ in range(2):
        seal_and_append(_record(), ledger, relink=True)

    result = runner.invoke(app, ["diff", "0", "-1", "--ledger", str(ledger)])
    assert result.exit_code == 0, result.output
    assert "Comparable" in result.output


def test_an_actual_unknown_option_is_still_an_error(tmp_path):
    """Accepting -1 must not turn every typo into a silently ignored flag."""
    ledger = tmp_path / "ledger.jsonl"
    seal_and_append(_record(), ledger)
    result = runner.invoke(app, ["diff", "0", "0", "--ledger", str(ledger), "--jsonn"])
    assert result.exit_code == 2


def test_cli_report_without_a_source_still_uses_the_ledger(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    seal_and_append(_record(), ledger)
    result = runner.invoke(app, ["report", "--ledger", str(ledger)])
    assert result.exit_code == 0, result.output
    assert "flip_rate" in result.output or "flip rate" in result.output


def test_cli_rejects_a_source_that_is_neither_file_nor_index(tmp_path):
    result = runner.invoke(app, ["report", "nope.json", "--ledger",
                                 str(tmp_path / "ledger.jsonl")])
    assert result.exit_code == 2
    assert "neither an existing file nor a ledger index" in result.output


@pytest.mark.parametrize("flag", ["--json", "--unstable-only"])
def test_report_flags_still_work_with_a_positional_source(tmp_path, flag):
    record = seal_and_append(_record(), tmp_path / "ledger.jsonl")
    receipt = tmp_path / "report.json"
    write_json(record, receipt)
    result = runner.invoke(app, ["report", str(receipt), flag])
    assert result.exit_code == 0, result.output


def _pair(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    before = seal_and_append(_record(), ledger, relink=True)
    after = seal_and_append(_record(stable=["yes"], unstable=["yes"]), ledger, relink=True)
    return ledger, before, after


def test_diff_html_leads_with_comparability(tmp_path):
    from evalseal.diffing import diff_records
    from evalseal.htmlreport import diff_to_html

    _, before, after = _pair(tmp_path)
    html = diff_to_html(diff_records(before, after))

    body = html.split("</style>", 1)[1]
    assert body.index("comparable") < body.index("score")


def test_diff_html_flags_an_incomparable_pair(tmp_path):
    from evalseal.diffing import diff_records
    from evalseal.htmlreport import diff_to_html

    _, before, after = _pair(tmp_path)
    after.manifest.scorer.rubric_hash = "sha256:different"
    html = diff_to_html(diff_records(before, after))

    assert "Not directly comparable" in html
    assert "rubric" in html


def test_diff_html_is_self_contained_and_deterministic(tmp_path):
    from evalseal.diffing import diff_records
    from evalseal.htmlreport import diff_to_html

    _, before, after = _pair(tmp_path)
    result = diff_records(before, after)
    html = diff_to_html(result)

    assert html.startswith("<!doctype html>")
    assert diff_to_html(result) == html
    for pattern in ("http://", "https://", "<script", "<link", "src="):
        assert pattern not in html


def test_cli_diff_writes_html(tmp_path):
    ledger, _, _ = _pair(tmp_path)
    out = tmp_path / "drift.html"
    result = runner.invoke(app, ["diff", "0", "-1", "--ledger", str(ledger),
                                 "--html", str(out)])
    assert result.exit_code == 0, result.output
    assert "EvalSeal drift report" in out.read_text(encoding="utf-8")


def test_run_writes_the_html_receipt_alongside_the_other_artifacts(tmp_path, monkeypatch):
    """`--html` on the run itself, so CI can attach the receipt without a second command."""
    import httpx
    answers = iter(["yes", "no"] * 10)
    monkeypatch.setenv("EVALSEAL_RECORD", "1")
    monkeypatch.setenv("EVALSEAL_API_KEY", "k")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(
        200, json={"model": "m", "choices": [{"message": {"content": next(answers)}}]},
        request=httpx.Request("POST", url)))

    (tmp_path / "ds.jsonl").write_text('{"case_id": "only", "prompt": "p"}\n')
    (tmp_path / "t.json").write_text('{"model": "m"}')
    (tmp_path / "s.json").write_text('{"type": "regex", "pattern": "yes"}')
    out = tmp_path / "receipt.html"

    result = runner.invoke(app, [
        "run", "--dataset", str(tmp_path / "ds.jsonl"),
        "--target-config", str(tmp_path / "t.json"),
        "--scorer-config", str(tmp_path / "s.json"),
        "--cassette", str(tmp_path / "c.json"), "--n", "5",
        "--concurrency", "1", "--fail-on", "none", "--html", str(out),
    ])
    assert result.exit_code == 0, result.output
    assert "EvalSeal receipt" in out.read_text(encoding="utf-8")


def test_html_and_json_together_emit_both(tmp_path):
    """--html short-circuits the terminal view, but must not swallow --json."""
    ledger = tmp_path / "ledger.jsonl"
    seal_and_append(_record(), ledger)
    out = tmp_path / "r.html"

    result = runner.invoke(app, ["report", "--ledger", str(ledger),
                                 "--html", str(out), "--json"])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "sealed_hash" in result.output
