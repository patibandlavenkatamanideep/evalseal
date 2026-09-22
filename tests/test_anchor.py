"""Local anchors: what they bind, that they are deterministic, and every way they fail.

The point of an anchor is that a third party can re-check it, so most of these tests
change one thing after anchoring and require `anchor-verify` to notice.
"""
from __future__ import annotations

import json

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.anchor import (
    AnchorError,
    anchor_passed,
    build_anchor,
    subject_digest,
    verify_anchor,
    write_anchor,
)
from evalseal.cli import app
from evalseal.diffing import load_receipt
from evalseal.executor import run_eval
from evalseal.ledger import _content_hash, seal_and_append
from evalseal.models import RunRecord
from evalseal.report import write_json
from evalseal.signing import generate_keypair, public_key_fingerprint, sign_head

runner = CliRunner()


def _record(answer: str = "yes"):
    target = LocalCallableTarget(scripted({"a": [answer], "b": ["yes"]}))
    return run_eval(make_dataset("a", "b"), target, RegexScorer(r"^yes$"), n_repeats=5)


@pytest.fixture
def sealed(tmp_path):
    """A receipt sealed into a ledger, and the receipt written out the way `run` does."""
    ledger = tmp_path / "ledger.jsonl"
    record = seal_and_append(_record(), ledger, relink=True)
    receipt = tmp_path / "report.json"
    write_json(record, receipt)
    return record, receipt, ledger


def _checks_by_name(checks):
    return {c.name: c for c in checks}


def _tamper(receipt, *, rehash: bool):
    """Edit a score in a receipt file, optionally re-sealing the hash to cover the edit."""
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["results"][0]["scores"][0] = 0.0
    record = RunRecord.model_validate(data)
    if rehash:
        record.hash = _content_hash(record)
    receipt.write_text(record.model_dump_json(indent=2), encoding="utf-8")


# --- what an anchor contains -------------------------------------------------------

def test_an_anchor_binds_the_receipt_ledger_and_sealed_identity(sealed):
    record, receipt, ledger = sealed
    a = build_anchor(record, receipt, ledger)

    assert a["anchor_version"] == 1
    assert a["receipt_hash"] == record.hash
    assert a["ledger"] == {"path": ledger.as_posix(), "receipt_index": 0,
                           "length": 1, "head_hash": record.hash}
    assert a["sealed"]["dataset_hash"] == record.manifest.dataset.hash
    assert a["sealed"]["case_set_hash"].startswith("sha256:")
    assert a["sealed"]["n_cases"] == 2
    assert set(a["artifact_hashes"]) == {"receipt", "ledger"}
    assert a["signature_present"] is False
    assert a["public_key_fingerprint"] is None


def test_an_anchor_says_it_is_not_an_external_timestamp(sealed):
    a = build_anchor(*sealed)
    assert a["external_proof"] is None
    assert "not an independent timestamp" in a["notes"]
    assert "does not prove the evaluation ran as described" in a["notes"]


def test_an_anchor_carries_hashes_and_never_payloads(sealed):
    """Prompts and responses stay out; hashes let a later disclosure be matched."""
    text = json.dumps(build_anchor(*sealed))
    assert "yes" not in text          # the only response text in this fixture


# --- determinism -------------------------------------------------------------------

def test_everything_but_created_at_is_deterministic(sealed, tmp_path):
    first = build_anchor(*sealed, created_at="2026-01-01T00:00:00+00:00")
    second = build_anchor(*sealed, created_at="2026-12-31T23:59:59+00:00")
    assert first["subject_digest"] == second["subject_digest"]
    assert {k: v for k, v in first.items() if k != "created_at"} == \
           {k: v for k, v in second.items() if k != "created_at"}

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_anchor(first, a)
    write_anchor(build_anchor(*sealed, created_at="2026-01-01T00:00:00+00:00"), b)
    assert a.read_bytes() == b.read_bytes()


def test_the_subject_digest_ignores_fields_about_the_anchor_itself(sealed):
    a = build_anchor(*sealed)
    digest = a["subject_digest"]
    a["created_at"] = "some other time"
    a["notes"] = "different prose"
    a["external_proof"] = {"kind": "added later"}
    assert subject_digest(a) == digest


# --- verification passes when nothing changed -------------------------------------

def test_an_untouched_receipt_verifies(sealed):
    record, receipt, ledger = sealed
    checks = verify_anchor(build_anchor(record, receipt, ledger), load_receipt(receipt),
                           ledger=ledger)
    assert anchor_passed(checks), [(c.name, c.detail) for c in checks if not c.passed]
    names = _checks_by_name(checks)
    assert names["receipt identity"].passed
    assert names["ledger head"].passed
    assert names["receipt in ledger"].passed


def test_appending_to_the_ledger_after_anchoring_is_allowed(sealed):
    """An append-only ledger grows; the anchored head must still be where it was."""
    record, receipt, ledger = sealed
    anchor = build_anchor(record, receipt, ledger)
    seal_and_append(_record(), ledger, relink=True)
    assert anchor_passed(verify_anchor(anchor, load_receipt(receipt), ledger=ledger))


def test_external_proof_is_reported_as_absent_not_as_passed(sealed):
    record, receipt, ledger = sealed
    checks = verify_anchor(build_anchor(record, receipt, ledger), record, ledger=ledger)
    proof = _checks_by_name(checks)["external proof"]
    assert not proof.required
    assert "nothing here establishes when it was made" in proof.detail


# --- verification fails when something changed ------------------------------------

def test_an_edited_receipt_fails(sealed):
    """A score changed and the stored hash left alone: the content no longer matches."""
    record, receipt, ledger = sealed
    anchor = build_anchor(record, receipt, ledger)
    _tamper(receipt, rehash=False)

    checks = verify_anchor(anchor, load_receipt(receipt), ledger=ledger)
    assert not anchor_passed(checks)
    assert not _checks_by_name(checks)["receipt integrity"].passed


def test_an_edited_and_resealed_receipt_still_fails(sealed):
    """Recomputing the hash to cover the edit makes the receipt self-consistent - and a
    different receipt from the one anchored. That is what the anchor is for."""
    record, receipt, ledger = sealed
    anchor = build_anchor(record, receipt, ledger)
    _tamper(receipt, rehash=True)

    checks = verify_anchor(anchor, load_receipt(receipt), ledger=ledger)
    names = _checks_by_name(checks)
    assert names["receipt integrity"].passed          # it is consistent with itself
    assert not names["receipt identity"].passed       # but it is not the anchored one
    assert not anchor_passed(checks)


def test_an_edited_anchor_file_fails(sealed):
    record, receipt, ledger = sealed
    anchor = build_anchor(record, receipt, ledger)
    anchor["sealed"]["target_model"] = "a-better-model"

    checks = verify_anchor(anchor, record, ledger=ledger)
    assert not _checks_by_name(checks)["anchor integrity"].passed
    assert not anchor_passed(checks)


def test_a_rewritten_ledger_history_fails(sealed, tmp_path):
    """Swap the ledger for a different one that still contains a valid chain."""
    record, receipt, ledger = sealed
    anchor = build_anchor(record, receipt, ledger)
    ledger.unlink()
    seal_and_append(_record(answer="no"), ledger, relink=True)

    checks = verify_anchor(anchor, record, ledger=ledger)
    names = _checks_by_name(checks)
    assert not names["ledger head"].passed
    assert not names["receipt in ledger"].passed
    assert not anchor_passed(checks)


def test_an_unsupported_anchor_version_is_refused(sealed):
    anchor = build_anchor(*sealed)
    anchor["anchor_version"] = 99
    checks = verify_anchor(anchor, sealed[0])
    assert not anchor_passed(checks)
    assert "unsupported anchor" in checks[0].detail


# --- refusing to anchor what should not be anchored --------------------------------

def test_an_edited_receipt_cannot_be_anchored(sealed):
    record, receipt, ledger = sealed
    _tamper(receipt, rehash=False)
    with pytest.raises(AnchorError, match="must not vouch for an edited receipt"):
        build_anchor(load_receipt(receipt), receipt, ledger)


def test_a_receipt_is_only_anchored_against_the_ledger_it_is_in(sealed, tmp_path):
    record, receipt, _ = sealed
    other = tmp_path / "other.jsonl"
    seal_and_append(_record(answer="no"), other, relink=True)
    with pytest.raises(AnchorError, match="is not in"):
        build_anchor(record, receipt, other)


# --- signatures --------------------------------------------------------------------

def test_a_signed_ledger_records_the_key_fingerprint(sealed, tmp_path):
    record, receipt, ledger = sealed
    public = generate_keypair(tmp_path / "k.key", tmp_path / "k.pub")
    sign_head(ledger, tmp_path / "k.key")

    anchor = build_anchor(record, receipt, ledger)
    assert anchor["signature_present"] is True
    assert anchor["public_key_fingerprint"] == public_key_fingerprint(public)
    assert "signatures" in anchor["artifact_hashes"]
    checks = verify_anchor(anchor, record, ledger=ledger)
    assert _checks_by_name(checks)["signature"].passed
    assert anchor_passed(checks)


def test_a_signature_by_a_different_key_fails(sealed, tmp_path):
    record, receipt, ledger = sealed
    generate_keypair(tmp_path / "a.key", tmp_path / "a.pub")
    generate_keypair(tmp_path / "b.key", tmp_path / "b.pub")
    sign_head(ledger, tmp_path / "a.key")
    anchor = build_anchor(record, receipt, ledger)

    # Replace the signature with a valid one by another key.
    sig_file = ledger.with_suffix(ledger.suffix + ".sig.jsonl")
    sig_file.unlink()
    sign_head(ledger, tmp_path / "b.key")

    checks = verify_anchor(anchor, record, ledger=ledger)
    assert not _checks_by_name(checks)["signature"].passed
    assert not anchor_passed(checks)


def test_the_key_fingerprint_does_not_depend_on_encoding_whitespace():
    import base64
    raw = base64.b64encode(bytes(range(32))).decode()
    assert public_key_fingerprint(raw) == public_key_fingerprint(f"  {raw}\n")


# --- anchors without a ledger, and extra artifacts ---------------------------------

def test_a_receipt_only_anchor_verifies_without_a_ledger(sealed):
    record, receipt, _ = sealed
    anchor = build_anchor(record, receipt)
    assert anchor["ledger"] is None
    checks = verify_anchor(anchor, load_receipt(receipt))
    assert anchor_passed(checks)
    assert "ledger head" not in _checks_by_name(checks)


def test_an_extra_artifact_is_bound_and_its_change_is_caught(sealed, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record, receipt, ledger = sealed
    html = tmp_path / "receipt.html"
    html.write_text("<p>receipt</p>", encoding="utf-8")
    anchor = build_anchor(record, receipt, ledger, [html])
    assert "artifact:receipt.html" in anchor["artifact_hashes"]

    assert anchor_passed(verify_anchor(anchor, record, ledger=ledger, base_dir=tmp_path))
    html.write_text("<p>edited</p>", encoding="utf-8")
    checks = verify_anchor(anchor, record, ledger=ledger, base_dir=tmp_path)
    assert not _checks_by_name(checks)["artifact:receipt.html"].passed
    assert not anchor_passed(checks)


def test_a_missing_artifact_is_not_checked_rather_than_passed(sealed, tmp_path):
    record, receipt, ledger = sealed
    html = tmp_path / "receipt.html"
    html.write_text("<p>receipt</p>", encoding="utf-8")
    anchor = build_anchor(record, receipt, ledger, [html])
    html.unlink()

    checks = verify_anchor(anchor, record, ledger=ledger, base_dir=tmp_path)
    check = _checks_by_name(checks)["artifact:receipt.html"]
    assert not check.passed and not check.required
    assert check.detail.startswith("not checked")
    assert anchor_passed(checks)      # absence is reported, and does not fail the rest


# --- the CLI -----------------------------------------------------------------------

def test_cli_anchor_then_verify_round_trip(sealed, tmp_path):
    record, receipt, ledger = sealed
    out = tmp_path / "anchor.json"
    made = runner.invoke(app, ["anchor", str(receipt), "--ledger", str(ledger),
                               "--out", str(out)])
    assert made.exit_code == 0, made.output
    assert "local anchor, not an external timestamp" in " ".join(made.output.split())

    checked = runner.invoke(app, ["anchor-verify", str(out), str(receipt),
                                  "--ledger", str(ledger)])
    assert checked.exit_code == 0, checked.output
    assert "Anchor verified." in checked.output


def test_cli_verify_exits_one_on_a_changed_receipt(sealed, tmp_path):
    record, receipt, ledger = sealed
    out = tmp_path / "anchor.json"
    runner.invoke(app, ["anchor", str(receipt), "--ledger", str(ledger), "--out", str(out)])
    _tamper(receipt, rehash=True)

    result = runner.invoke(app, ["anchor-verify", str(out), str(receipt),
                                 "--ledger", str(ledger), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["verified"] is False
    failed = {c["check"] for c in payload["checks"] if not c["passed"] and c["required"]}
    assert "receipt identity" in failed


def test_cli_treats_a_jsonl_source_as_the_ledger(sealed, tmp_path):
    """A one-record ledger is a single line of valid JSON; the extension decides."""
    record, _, ledger = sealed
    out = tmp_path / "anchor.json"
    made = runner.invoke(app, ["anchor", str(ledger), "--out", str(out)])
    assert made.exit_code == 0, made.output
    anchor = json.loads(out.read_text(encoding="utf-8"))
    assert anchor["ledger"]["receipt_index"] == 0
    assert anchor["receipt_hash"] == record.hash


def test_cli_refuses_to_anchor_an_edited_receipt(sealed, tmp_path):
    _, receipt, ledger = sealed
    _tamper(receipt, rehash=False)
    result = runner.invoke(app, ["anchor", str(receipt), "--ledger", str(ledger),
                                 "--out", str(tmp_path / "a.json")])
    assert result.exit_code == 1
    assert "Not anchored" in result.output
    assert not (tmp_path / "a.json").exists()


def test_cli_rejects_a_file_that_is_not_an_anchor(sealed, tmp_path):
    _, receipt, _ = sealed
    bogus = tmp_path / "anchor.json"
    bogus.write_text("not json", encoding="utf-8")
    result = runner.invoke(app, ["anchor-verify", str(bogus), str(receipt)])
    assert result.exit_code == 1
    assert "Not an anchor file" in result.output


def test_an_embedded_signature_verifies_from_the_anchor_alone(sealed, tmp_path):
    """A third party with only the anchor and the receipt can check who signed it."""
    record, receipt, ledger = sealed
    generate_keypair(tmp_path / "k.key", tmp_path / "k.pub")
    sign_head(ledger, tmp_path / "k.key")
    anchor = build_anchor(record, receipt, ledger)
    assert anchor["signature"]["signature"] and anchor["signature"]["public_key"]

    anchor_only = {**anchor, "ledger": None}        # no ledger available to the verifier
    anchor_only["subject_digest"] = subject_digest(anchor_only)
    checks = _checks_by_name(verify_anchor(anchor_only, record))
    assert checks["embedded signature"].passed
    assert "signs this receipt's own record" in checks["embedded signature"].detail


def test_a_forged_embedded_signature_fails(sealed, tmp_path):
    import base64

    record, receipt, ledger = sealed
    generate_keypair(tmp_path / "k.key", tmp_path / "k.pub")
    sign_head(ledger, tmp_path / "k.key")
    anchor = build_anchor(record, receipt, ledger)
    anchor["signature"]["signature"] = base64.b64encode(b"\0" * 64).decode()
    anchor["subject_digest"] = subject_digest(anchor)   # a careful forger re-seals it

    checks = verify_anchor(anchor, record, ledger=ledger)
    assert not _checks_by_name(checks)["embedded signature"].passed
    assert not anchor_passed(checks)


def test_a_swapped_embedded_key_fails_the_fingerprint(sealed, tmp_path):
    record, receipt, ledger = sealed
    generate_keypair(tmp_path / "a.key", tmp_path / "a.pub")
    other = generate_keypair(tmp_path / "b.key", tmp_path / "b.pub")
    sign_head(ledger, tmp_path / "a.key")
    anchor = build_anchor(record, receipt, ledger)
    anchor["signature"]["public_key"] = other
    anchor["subject_digest"] = subject_digest(anchor)

    check = _checks_by_name(verify_anchor(anchor, record))["embedded signature"]
    assert not check.passed
    assert "does not match the recorded fingerprint" in check.detail
