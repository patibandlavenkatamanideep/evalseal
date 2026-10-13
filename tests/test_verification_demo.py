"""Run the third-party verification walkthrough, including every way it breaks.

A walkthrough whose output has drifted from its README teaches the wrong thing with
total confidence. These tests execute the real commands against a copy of the bundle
and assert both the passes *and* the tamper outcomes the page promises.

The tamper tests work on a copy, because a receipt records the paths its artifacts were
sealed at: editing the committed files in place would be visible to every other test in
the suite.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "examples" / "verification-demo"
LEDGER = "ledger.jsonl"


def _readme() -> str:
    return (DEMO / "README.md").read_text(encoding="utf-8")


@pytest.fixture
def bundle(tmp_path):
    """A working copy, laid out so the receipt's recorded paths still resolve.

    The receipt names `examples/verification-demo/cassette.json`, so the copy keeps
    that shape and commands run from the copy's root.
    """
    root = tmp_path / "repo"
    target = root / "examples" / "verification-demo"
    target.parent.mkdir(parents=True)
    shutil.copytree(DEMO, target)
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**__import__("os").environ,
           "PATH": f"{Path(sys.executable).parent}{__import__('os').pathsep}"
                   f"{__import__('os').environ['PATH']}"}
    env.pop("EVALSEAL_RECORD", None)
    return subprocess.run(["evalseal", *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=180)


def _flat(text: str) -> str:
    """Whitespace-normalised, with markdown blockquote markers dropped.

    The page uses blockquotes for its warnings, so a sentence that wraps inside one
    would otherwise read as "a digest can only be > matched against a file".
    """
    return " ".join(text.replace("\n> ", "\n").split())


def _path(name: str) -> str:
    return f"examples/verification-demo/{name}"


# --- the bundle is complete, and carries no private key -------------------------------

def test_the_bundle_has_everything_the_readme_lists():
    """Checked against *git*, not the filesystem.

    `.gitignore` carries `report.json`, to stop generated receipts being committed from
    the worktree root. It silently swallowed this example's receipt too, so every test
    reading it passed locally and failed on every platform in CI, where the checkout is
    fresh. A filesystem check cannot see that; `git ls-files` can.
    """
    expected = {"report.json", "ledger.jsonl", "ledger.jsonl.sig.jsonl", "signing.pub",
                "anchor.json", "cassette.json", "README.md", "fixture-tsa/ca.pem"}
    tracked = set(subprocess.run(
        ["git", "ls-files", "examples/verification-demo/"], cwd=REPO,
        capture_output=True, text=True, check=True).stdout.split())
    names = {p.removeprefix("examples/verification-demo/") for p in tracked}
    assert expected <= names, expected - names


def test_nothing_in_the_bundle_is_gitignored():
    """Catches a *newly added* bundle file that an ignore rule would swallow.

    Weaker than the test above and deliberately kept: `git check-ignore` does not
    report a file that is already tracked, so this cannot re-detect the bug that
    prompted it. What it does catch is the next person dropping `report.md` or
    `receipt.html` into this directory and not noticing it never landed.
    """
    files = [str(p.relative_to(REPO)) for p in DEMO.rglob("*") if p.is_file()]
    ignored = subprocess.run(["git", "check-ignore", *files], cwd=REPO,
                             capture_output=True, text=True).stdout.split()
    assert ignored == [], f"ignored and so never committed: {ignored}"


def test_no_private_key_was_committed():
    """The bundle is signed; only the public half may ship."""
    for path in DEMO.rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        assert b"PRIVATE KEY" not in blob, path
        assert path.suffix != ".key", path


def test_no_stray_lock_file():
    assert not (DEMO / "ledger.jsonl.lock").exists()


# --- the five passing steps -----------------------------------------------------------

def test_step_1_chain_verifies(bundle):
    result = _run(bundle, "verify", "--ledger", _path(LEDGER))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Chain intact: 1 record(s)." in _flat(result.stdout)


def test_step_2_artifacts_verify(bundle):
    result = _run(bundle, "verify", "--ledger", _path(LEDGER), "--artifacts")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cassette" in _flat(result.stdout)
    assert "matches the sealed digest" in _flat(result.stdout)


def test_step_3_signature_verifies(bundle):
    result = _run(bundle, "verify", "--ledger", _path(LEDGER),
                  "--public-key", _path("signing.pub"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 signature(s) valid" in _flat(result.stdout)


def test_step_4_anchor_verifies_with_the_fixture_root(bundle):
    result = _run(bundle, "anchor-verify", _path("anchor.json"), _path("report.json"),
                  "--ledger", _path(LEDGER),
                  "--tsa-ca", _path("fixture-tsa/ca.pem"))
    assert result.returncode == 0, result.stdout + result.stderr
    out = _flat(result.stdout)
    assert "Anchor verified." in out
    # Every layer named separately, as the README shows.
    for layer in ("anchor integrity", "receipt integrity", "receipt identity",
                  "ledger head", "sealed:cassette", "created_at (not verified)",
                  "external proof (rfc3161)"):
        assert layer in out, layer
    assert "signature verified against the certificate you supplied" in out
    assert "revocation not checked" in out


def test_step_4_without_a_root_makes_the_smaller_claim(bundle):
    """Dropping --tsa-ca must not silently keep the stronger sentence."""
    result = _run(bundle, "anchor-verify", _path("anchor.json"), _path("report.json"),
                  "--ledger", _path(LEDGER))
    assert result.returncode == 0, result.stdout + result.stderr
    out = _flat(result.stdout)
    assert "message imprint checked, TSA signature NOT verified by EvalSeal" in out
    assert "signature verified against the certificate you supplied" not in out


def test_the_receipt_scores_what_the_readme_claims(bundle):
    result = _run(bundle, "report", _path("report.json"), "--json")
    payload = json.loads(result.stdout)
    assert payload["summary"]["mean_score"] == 0.9
    assert payload["summary"]["n_cases"] == 2


# --- now break it ---------------------------------------------------------------------

def test_editing_a_score_in_the_ledger_is_named(bundle):
    path = bundle / "examples" / "verification-demo" / LEDGER
    record = json.loads(path.read_text(encoding="utf-8").strip())
    record["results"][0]["scores"][0] = 0.0
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = _run(bundle, "verify", "--ledger", _path(LEDGER))
    assert result.returncode == 1
    out = _flat(result.stdout)
    assert "TAMPER DETECTED at record 0" in out
    assert "content hash does not match" in out


def test_editing_the_bound_artifact_is_changed_not_tampered(bundle):
    """The chain is intact - nobody touched the ledger. The world the verdicts came
    from changed, which is a separate check with a separate answer."""
    cassette = bundle / "examples" / "verification-demo" / "cassette.json"
    cassette.write_text('{"tampered": true}', encoding="utf-8")

    result = _run(bundle, "verify", "--ledger", _path(LEDGER), "--artifacts")
    assert result.returncode == 1
    out = _flat(result.stdout)
    assert "Chain intact" in out
    assert "changed" in out
    assert "does not match the digest sealed with this record" in out
    assert "TAMPER" not in out


def test_deleting_the_artifact_is_missing_never_passing(bundle):
    (bundle / "examples" / "verification-demo" / "cassette.json").unlink()
    result = _run(bundle, "verify", "--ledger", _path(LEDGER), "--artifacts")
    out = _flat(result.stdout)
    assert "missing" in out
    assert "its digest cannot be checked" in out
    assert "ok cassette" not in out


def test_editing_the_receipt_fails_the_anchor(bundle):
    path = bundle / "examples" / "verification-demo" / "report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["results"][0]["scores"][0] = 0.0
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    result = _run(bundle, "anchor-verify", _path("anchor.json"), _path("report.json"),
                  "--ledger", _path(LEDGER))
    assert result.returncode == 1
    assert "receipt integrity" in _flat(result.stdout)
    assert "does not match its sealed hash" in _flat(result.stdout)


def test_the_wrong_trust_root_fails_the_proof(bundle):
    """Which is the point of supplying the root yourself."""
    sys.path.insert(0, str(REPO / "tests"))
    from tsa_fixture import build_tsa

    wrong = bundle / "wrong-ca.pem"
    wrong.write_bytes(build_tsa().ca_pem)

    result = _run(bundle, "anchor-verify", _path("anchor.json"), _path("report.json"),
                  "--ledger", _path(LEDGER), "--tsa-ca", "wrong-ca.pem")
    assert result.returncode == 1
    out = _flat(result.stdout)
    assert "verification FAILED" in out
    assert "not issued by any root you supplied" in out


# --- the page's own honesty -----------------------------------------------------------

def test_the_readme_labels_the_fixture_authority_loudly():
    """A reader following a demo is exactly who might mistake a fixture for a timestamp."""
    text = _flat(_readme())
    assert "The timestamp in this bundle is a fixture, not a timestamp" in text
    assert "attests **nothing**" in text or "attests nothing" in text
    assert "throwaway authority generated by this repository's own tests" in text


def test_the_readme_shows_failures_not_only_passes():
    text = _readme()
    assert "## Now break it" in text
    assert text.count("exit: 1") >= 3
    assert "A demo where" in text and "green teaches nothing" in text


def test_the_readme_says_what_it_does_not_cover():
    """`evalseal evidence` lives on another branch, so the page must not promise it -
    but it must still say that integrity is not precision."""
    text = _flat(_readme())
    assert "What this walkthrough does not cover" in text
    assert "Integrity and precision are different questions" in text
    assert "evalseal evidence" not in text


def test_the_readme_closes_with_what_cannot_be_established():
    text = _flat(_readme())
    assert "What you still cannot establish" in text
    for limit in ("That the eval ran at all", "served the model it named",
                  "the inputs were honest", "When any of it happened",
                  "that this was the only run"):
        assert limit.lower() in text.lower(), limit
    assert "do not establish truth" in text


def test_the_readme_says_to_run_from_the_repository_root():
    """A receipt records paths; running elsewhere reports `missing`, not `ok`."""
    text = _flat(_readme())
    assert "Run every command from the repository root" in text
    assert "a digest can only be matched against a file you can find" in text


def test_the_readme_needs_no_key_or_network():
    text = _flat(_readme())
    assert "No API key" in text
    assert "No network" in text
