"""Signing closes the gap the hash chain leaves: who produced this ledger?"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from conftest import make_dataset, scripted
from typer.testing import CliRunner

from evalseal.adapters.scorer import RegexScorer
from evalseal.adapters.target import LocalCallableTarget
from evalseal.cli import app
from evalseal.executor import run_eval
from evalseal.ledger import last_hash, seal_and_append
from evalseal.signing import (
    generate_keypair,
    load_signatures,
    sign_head,
    signatures_path,
    verify_signatures,
)

runner = CliRunner()


def _seal(path: Path, outputs=("yes",) * 5):
    target = LocalCallableTarget(scripted({"q": list(outputs)}))
    rec = run_eval(make_dataset("q"), target, RegexScorer("yes"),
                   n_repeats=len(outputs), prev_hash=last_hash(path))
    return seal_and_append(rec, path)


def _keys(tmp_path):
    pub = generate_keypair(tmp_path / "k.key", tmp_path / "k.pub")
    return tmp_path / "k.key", tmp_path / "k.pub", pub


def test_sign_then_verify(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _seal(ledger)
    key, pub_path, pub = _keys(tmp_path)

    entry = sign_head(ledger, key)
    assert entry["algorithm"] == "ed25519" and entry["record_index"] == 0
    assert signatures_path(ledger).exists()

    ok, msg = verify_signatures(ledger, str(pub_path))
    assert ok, msg
    assert "1 signature(s) valid" in msg
    assert verify_signatures(ledger, pub)[0]        # base64 key works too


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes do not exist on Windows")
def test_private_key_is_not_world_readable(tmp_path):
    key, _, _ = _keys(tmp_path)
    assert key.stat().st_mode & 0o077 == 0


def test_keygen_refuses_to_overwrite(tmp_path):
    _keys(tmp_path)
    with pytest.raises(FileExistsError):
        generate_keypair(tmp_path / "k.key", tmp_path / "k2.pub")


def test_a_different_key_does_not_verify(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _seal(ledger)
    key, _, _ = _keys(tmp_path)
    sign_head(ledger, key)

    other = generate_keypair(tmp_path / "other.key", tmp_path / "other.pub")
    ok, msg = verify_signatures(ledger, other)
    assert not ok and "different key" in msg


def test_rewriting_the_ledger_after_signing_is_caught(tmp_path):
    """The whole point: a forger who rebuilds a consistent chain still can't sign it."""
    ledger = tmp_path / "ledger.jsonl"
    _seal(ledger, ("yes", "no", "yes", "no", "yes"))
    key, _, pub = _keys(tmp_path)
    sign_head(ledger, key)

    # Rewrite the record with flattering scores and re-hash it so the chain looks fine.
    from evalseal.ledger import _content_hash, load_all
    rec = load_all(ledger)[0]
    rec.results[0].scores = [1.0] * 5
    rec.hash = _content_hash(rec)
    ledger.write_text(rec.model_dump_json() + "\n")

    from evalseal.ledger import verify_chain
    assert verify_chain(ledger)[0]                  # chain alone is fooled
    ok, msg = verify_signatures(ledger, pub)        # the signature is not
    assert not ok and "rewritten after signing" in msg


def test_forged_signature_bytes_are_rejected(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _seal(ledger)
    key, _, pub = _keys(tmp_path)
    sign_head(ledger, key)

    entry = load_signatures(ledger)[0]
    entry["signature"] = base64.b64encode(b"\x00" * 64).decode()
    signatures_path(ledger).write_text(json.dumps(entry) + "\n")

    ok, msg = verify_signatures(ledger, pub)
    assert not ok and "invalid" in msg


def test_unsigned_ledger_reports_missing_signatures(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _seal(ledger)
    ok, msg = verify_signatures(ledger)
    assert not ok and "No signatures found" in msg


def test_refuses_to_sign_a_broken_ledger(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _seal(ledger, ("yes", "no", "yes", "no", "yes"))
    d = json.loads(ledger.read_text())
    d["results"][0]["scores"] = [1.0] * 5          # tampered, hash no longer matches
    ledger.write_text(json.dumps(d) + "\n")
    key, _, _ = _keys(tmp_path)

    with pytest.raises(ValueError, match="does not verify"):
        sign_head(ledger, key)


def test_cli_keygen_sign_verify_roundtrip(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _seal(ledger)

    out = runner.invoke(app, ["keygen", "--private-key", str(tmp_path / "k.key"),
                              "--public-key", str(tmp_path / "k.pub")])
    assert out.exit_code == 0, out.output

    signed = runner.invoke(app, ["sign", "--ledger", str(ledger), "--key", str(tmp_path / "k.key")])
    assert signed.exit_code == 0, signed.output

    good = runner.invoke(app, ["verify", "--ledger", str(ledger),
                               "--public-key", str(tmp_path / "k.pub")])
    assert good.exit_code == 0, good.output
    assert "signature(s) valid" in good.output

    # An unsigned ledger passes plain verify but fails --signed.
    other = tmp_path / "other.jsonl"
    _seal(other)
    assert runner.invoke(app, ["verify", "--ledger", str(other)]).exit_code == 0
    strict = runner.invoke(app, ["verify", "--ledger", str(other), "--signed"])
    assert strict.exit_code == 1
    assert "No signatures found" in strict.output
