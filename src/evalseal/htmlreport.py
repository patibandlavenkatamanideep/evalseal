"""A single-file HTML receipt: the run, its variance, and what it does not claim.

Three constraints shape this file, and each one is a decision rather than a default.

*Self-contained.* No stylesheet, script, font or image is fetched. A report attached to
a CI run is usually opened months later on a machine with no network, and a report that
renders differently then is not a receipt.

*Deterministic.* The same record renders byte-identical HTML. Nothing here reads the
clock or the environment - the only timestamp shown is the one sealed into the record.
That means the HTML can itself be hashed and attached to the chain.

*Quiet about payloads.* Prompts, responses and the judge prompt are never written out,
only their hashes. A receipt is meant to be shareable; a shareable file that quietly
carries a private dataset is a leak waiting for someone to forward it.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from .diffing import DiffResult, mean_flip_rate, noise_floor
from .ledger import config_fingerprint, evaluator_fingerprint
from .models import CaseResult, RunRecord
from .report import case_rows, verdict_sequence

# Deliberately small. Every rule here earns its place by making variance legible;
# there is no design system to maintain and no CDN to go down.
_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --fg: #1a1a18; --muted: #6b6b66; --line: #e3e3df; --card: #ffffff;
  --pass: #2f7d4f; --fail: #b3261e; --warn: #8a5a00; --warn-bg: #fff6e0;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --fg: #e9e9e6; --muted: #9a9a94; --line: #2c2e33; --card: #1d1f23;
    --pass: #58b87a; --fail: #ef7a72; --warn: #e0b355; --warn-bg: #2e2617;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
h2 { font-size: 1.05rem; margin: 2.5rem 0 .75rem; letter-spacing: -.01em; }
.sub { color: var(--muted); margin: 0 0 1.75rem; }
code, .mono { font-family: var(--mono); font-size: .86em; }

.cards { display: flex; flex-wrap: wrap; gap: .75rem; }
.card {
  flex: 1 1 8.5rem; background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: .8rem .9rem;
}
.card .k { color: var(--muted); font-size: .76rem; text-transform: uppercase;
  letter-spacing: .05em; }
.card .v { font-size: 1.45rem; font-variant-numeric: tabular-nums; margin-top: .15rem; }
.card .n { color: var(--muted); font-size: .78rem; }

.warn {
  background: var(--warn-bg); border: 1px solid var(--warn); border-radius: 10px;
  padding: .8rem 1rem; margin: 1.5rem 0 0;
}
.warn strong { color: var(--warn); }
.warn ul { margin: .4rem 0 0; padding-left: 1.2rem; }

table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td {
  text-align: left; padding: .45rem .55rem; border-bottom: 1px solid var(--line);
  vertical-align: middle;
}
th { color: var(--muted); font-weight: 600; font-size: .76rem;
  text-transform: uppercase; letter-spacing: .05em; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
tbody tr:hover { background: color-mix(in srgb, var(--fg) 4%, transparent); }

.strip { display: inline-flex; gap: 2px; }
.strip i {
  width: 11px; height: 16px; border-radius: 2px; display: inline-block;
  background: var(--pass);
}
.strip i.f { background: var(--fail); }
.strip i.o { background: var(--muted); }

.tag { font-size: .74rem; padding: .1rem .45rem; border-radius: 999px;
  border: 1px solid var(--line); white-space: nowrap; }
.tag.UNSTABLE { color: var(--fail); border-color: var(--fail); }
.tag.BORDERLINE { color: var(--warn); border-color: var(--warn); }
.tag.STABLE { color: var(--pass); border-color: var(--pass); }

.prov { width: 100%; }
.prov td:first-child { color: var(--muted); width: 14rem; }
.prov td:last-child { font-family: var(--mono); font-size: .82rem; word-break: break-all; }

footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line);
  color: var(--muted); font-size: .85rem; }
footer li { margin: .2rem 0; }

.scroll { overflow-x: auto; }
@media (max-width: 40rem) { body { padding: 1.25rem .75rem 3rem; } }
@media print {
  body { background: #fff; color: #000; padding: 0; }
  .card, .warn { break-inside: avoid; }
}
"""


def _page(title: str, subtitle: str, body: str) -> str:
    """The shared shell, so a receipt and a drift report look like the same artifact."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<h1>{escape(title)}</h1>
<p class="sub">{subtitle}</p>
{body}
</main>
</body>
</html>
"""


def _strip(case: CaseResult) -> str:
    """One cell per repeat, in run order, so a reader sees *when* it flipped."""
    cells = []
    for i, letter in enumerate(verdict_sequence(case), start=1):
        cls = "" if letter == "P" else ' class="f"'
        label = "pass" if letter == "P" else "fail"
        cells.append(f'<i{cls} title="run {i}: {label}"></i>')
    return '<span class="strip">' + "".join(cells) + "</span>"


def _card(key: str, value: str, note: str = "") -> str:
    note_html = f'<div class="n">{escape(note)}</div>' if note else ""
    return (f'<div class="card"><div class="k">{escape(key)}</div>'
            f'<div class="v">{escape(value)}</div>{note_html}</div>')


def _provenance_rows(record: RunRecord) -> list[tuple[str, str | None]]:
    m = record.manifest
    judge = m.scorer.judge
    return [
        ("target model", m.target.requested_model),
        ("served model", m.target.served_model),
        ("system fingerprint", m.target.system_fingerprint),
        ("temperature", f"{m.target.effective_params.temperature} ({m.target.params_source})"),
        ("scorer", m.scorer.type),
        ("judge model", (judge.served_model or judge.requested_model) if judge else None),
        ("rubric hash", m.scorer.rubric_hash),
        ("judge prompt hash", m.scorer.judge_prompt_hash),
        ("dataset hash", m.dataset.hash),
        ("suite hash", m.suite.hash if m.suite else None),
        ("repeats", str(m.run_config.n_repeats)),
        ("harness", m.run_config.harness_version),
        ("evalseal version", m.environment.evalseal_version),
        ("python", m.environment.python_version),
        ("platform", m.environment.platform),
        ("git commit", m.code.commit),
        ("git worktree", None if m.code.dirty is None else
         ("dirty - the commit does not describe this run" if m.code.dirty else "clean")),
        ("evaluator fingerprint", evaluator_fingerprint(record)),
        ("config fingerprint", config_fingerprint(record)),
        ("sealed hash", record.hash or None),
        ("previous hash", record.prev_hash or None),
        ("sealed at", record.created_at),
    ]


def to_html(record: RunRecord, unstable_only: bool = False) -> str:
    """Render a sealed record as one self-contained HTML page."""
    a = record.aggregate
    rows = case_rows(record, unstable_only)
    title = "EvalSeal receipt"

    cards = [
        _card("mean score", f"{a.mean_score:.3f}",
              f"over {record.manifest.run_config.n_repeats} repeats per case"),
        _card("noise floor", f"±{noise_floor(record):.3f}", "widest per-case CI"),
        _card("mean flip rate", f"{mean_flip_rate(record):.1%}",
              f"{a.n_unstable} unstable case(s)"),
        _card("cases", str(a.n_cases),
              f"{a.n_stable} stable · {a.n_borderline} borderline · "
              f"{a.n_unstable} unstable"),
    ]

    warn_html = ""
    if a.warnings:
        items = "".join(f"<li>{escape(w)}</li>" for w in a.warnings)
        warn_html = ('<div class="warn"><strong>Provenance warnings</strong>'
                     f"<ul>{items}</ul></div>")

    body_rows = []
    for c in rows:
        majority = "-" if c.majority_verdict is None else ("PASS" if c.majority_verdict else "FAIL")
        stability = escape(c.stability)
        body_rows.append(
            f'<tr><td class="mono">{escape(c.case_id)}</td>'
            f"<td>{_strip(c)}</td>"
            f'<td class="num">{c.mean:.2f}</td>'
            f'<td class="num">[{c.ci95[0]:.2f}, {c.ci95[1]:.2f}]</td>'
            f'<td class="num">{c.flip_count}</td>'
            f'<td class="num">{c.flip_rate:.0%}</td>'
            f'<td class="num">{majority}</td>'
            f'<td><span class="tag {stability}">{stability}</span></td></tr>'
        )
    if not body_rows:
        empty = "No unstable cases." if unstable_only else "No cases."
        table = f"<p>{empty}</p>"
    else:
        table = (
            '<div class="scroll"><table><thead><tr>'
            "<th>case</th><th>verdicts (run order)</th><th>mean</th><th>95% CI</th>"
            "<th>flips</th><th>flip rate</th><th>majority</th><th>stability</th>"
            "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>"
        )

    prov = "".join(
        f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>"
        for k, v in _provenance_rows(record)
        if v
    )

    subtitle = (
        f'{escape(record.manifest.target.requested_model)} · '
        f'{escape(record.manifest.scorer.type)} · sealed '
        f'<span class="mono">{escape((record.hash or "unsealed")[:20])}</span>'
    )
    body = f"""<div class="cards">{"".join(cards)}</div>
{warn_html}

<h2>Per-case reproducibility</h2>
{table}

<h2>Provenance</h2>
<div class="scroll"><table class="prov"><tbody>{prov}</tbody></table></div>

<footer>
<p>What this receipt claims, and what it does not:</p>
<ul>
<li>Each case was run {record.manifest.run_config.n_repeats} times. The mean and the
95% interval are estimates from those runs, not properties of the model.</li>
<li>A flip is a verdict disagreeing with the majority for that case. A case that never
flipped here can still flip on the next run.</li>
<li>A score move smaller than the noise floor above is not evidence that anything
changed. Compare two sealed runs with <code>evalseal diff</code>.</li>
<li>Prompts and responses are not included - only their hashes. Verify the record
against the ledger with <code>evalseal verify</code>.</li>
</ul>
</footer>"""
    return _page(title, subtitle, body)


def write_html(record: RunRecord, path: str | Path = "report.html",
               unstable_only: bool = False) -> None:
    Path(path).write_text(to_html(record, unstable_only), encoding="utf-8")


def diff_to_html(result: DiffResult) -> str:
    """Render a drift comparison as one self-contained HTML page.

    Comparability leads, exactly as it does in the terminal output. A reader who skims
    only the top of this page should learn whether the delta below is allowed to mean
    anything before they read the delta.
    """
    if result.comparable:
        banner = ('<div class="card" style="flex-basis:100%"><div class="k">comparable'
                  '</div><div class="v" style="font-size:1rem">Yes - same grading setup, '
                  "so the scores mean the same thing.</div></div>")
    else:
        rows = "".join(
            f"<tr><td>{escape(c.name)}</td><td>{escape(str(c.before))}</td>"
            f"<td>{escape(str(c.after))}</td></tr>"
            for c in result.config_changes
        )
        banner = (
            '<div class="warn"><strong>Not directly comparable: the evaluator '
            "configuration changed.</strong> The score delta below is reported, but it "
            "mixes a change in the grader with any change in the model."
            '<div class="scroll"><table><thead><tr><th>what changed</th><th>before</th>'
            f"<th>after</th></tr></thead><tbody>{rows}</tbody></table></div></div>"
        )

    delta = "no change" if result.score_delta == 0 else f"{result.score_delta:+.3f}"
    cards = "".join([
        _card("score", f"{result.score_after:.3f}",
              f"was {result.score_before:.3f} · {delta}"),
        _card("verdict", result.score_verdict,
              f"noise floor ±{result.noise_floor:.3f}"),
        _card("mean flip rate", f"{result.flip_rate_after:.1%}",
              f"was {result.flip_rate_before:.1%} · {result.flip_rate_delta:+.1%}"),
        _card("cases that flipped", str(len(result.unstable_after)),
              f"was {len(result.unstable_before)}"),
    ])

    movements = []
    for label, cases in (
        ("Newly unstable", result.newly_unstable),
        ("Now stable", result.now_stable),
        ("Still unstable", result.still_unstable),
        ("Cases added", result.cases_added),
        ("Cases removed", result.cases_removed),
    ):
        if cases:
            ids = ", ".join(escape(c) for c in cases)
            movements.append(f'<li><strong>{label}:</strong> <span class="mono">{ids}</span></li>')
    movement_html = (
        "<h2>Which cases moved</h2><ul>" + "".join(movements) + "</ul>"
        if movements else
        "<h2>Which cases moved</h2><p>No case changed stability class.</p>"
    )

    changed = [c for c in result.provenance if c.changed]
    prov_html = ""
    if changed:
        rows = "".join(
            f"<tr><td>{escape(c.name)}</td><td>{escape(str(c.before))}</td>"
            f"<td>{escape(str(c.after))}</td></tr>" for c in changed
        )
        prov_html = (
            "<h2>Other differences</h2>"
            '<div class="scroll"><table><thead><tr><th>field</th><th>before</th>'
            f"<th>after</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )

    body = f"""<div class="cards">{banner}</div>
<div class="cards" style="margin-top:.75rem">{cards}</div>
{movement_html}
{prov_html}
<footer>
<p>A score move smaller than the noise floor is not evidence that anything changed.
The floor is the widest per-case 95% interval across both runs, so it is deliberately
conservative.</p>
</footer>"""
    # The subtitle names the two records rather than repeating the verdict in the
    # banner directly beneath it.
    sealed = next((c for c in result.provenance if c.name == "sealed hash"), None)
    subtitle = (
        f'<span class="mono">{escape(str(sealed.before))}</span> to '
        f'<span class="mono">{escape(str(sealed.after))}</span>'
        if sealed else "two sealed runs"
    )
    return _page("EvalSeal drift report", subtitle, body)


def write_diff_html(result: DiffResult, path: str | Path = "diff.html") -> None:
    Path(path).write_text(diff_to_html(result), encoding="utf-8")
