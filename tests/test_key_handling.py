"""How a signing key gets onto disk.

**Baseline reproductions (2.2.0).** `generate_keypair` did `exists()`, then
`write_bytes()`, then `chmod(0o600)`:

    === E3-1  the key is created world-readable and narrowed afterwards ===
      mode at creation : 0o644
      mode after chmod : 0o600

    === E3-3  a dangling symlink passes the exists() check ===
      created /tmp/.../nowhere through a dangling link

    === E3-4  the public key destination is silently overwritten ===
      shared.pub was 'SOMEONE-ELSES-PUBLIC-KEY', now 'Pa34neOQBLryiPuD3zLR'...

A non-dangling symlink was already refused, because `exists()` follows it.
"""
from __future__ import annotations

import os
import stat
import threading

import pytest
from typer.testing import CliRunner

from evalseal.cli import app
from evalseal.signing import PRIVATE_KEY_MODE, generate_keypair, write_private_key

runner = CliRunner()
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX modes and O_NOFOLLOW do not exist on Windows")


@posix_only
def test_the_private_key_is_owner_only_from_the_moment_it_exists(tmp_path, monkeypatch):
    """E3-1. There must be no window in which another local user can read it.

    Asserted by forbidding `chmod` outright: if the mode has to be corrected after the
    fact, there was a moment when it was wrong.
    """
    import pathlib

    def no_chmod(self, mode, **kw):
        raise AssertionError(
            f"the key was created with the wrong mode and narrowed to {mode:#o} "
            "afterwards; there was a window in which it was readable")

    monkeypatch.setattr(pathlib.Path, "chmod", no_chmod)
    generate_keypair(tmp_path / "a.key", tmp_path / "a.pub")
    mode = stat.S_IMODE((tmp_path / "a.key").stat().st_mode)
    assert mode == PRIVATE_KEY_MODE == 0o600, oct(mode)


@posix_only
def test_the_mode_holds_whatever_the_umask_is(tmp_path):
    """`write_bytes` honoured the umask; `os.open` with a mode does not depend on it."""
    old = os.umask(0o000)
    try:
        generate_keypair(tmp_path / "b.key", tmp_path / "b.pub")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "b.key").stat().st_mode) == 0o600


def test_an_existing_private_key_is_never_replaced(tmp_path):
    generate_keypair(tmp_path / "c.key", tmp_path / "c.pub")
    with pytest.raises(FileExistsError) as caught:
        generate_keypair(tmp_path / "c.key", tmp_path / "c2.pub")
    assert "refusing to overwrite a private key" in str(caught.value)
    assert "invalidates every signature made with it" in str(caught.value)


@posix_only
def test_a_symlink_at_the_private_key_path_is_refused(tmp_path):
    """E3-2 and E3-3: both the live link and the dangling one."""
    victim = tmp_path / "victim.txt"
    victim.write_text("important pre-existing content\n")
    live = tmp_path / "live.key"
    live.symlink_to(victim)
    with pytest.raises((FileExistsError, OSError)):
        generate_keypair(live, tmp_path / "live.pub")
    assert victim.read_text() == "important pre-existing content\n"

    dangling = tmp_path / "dangling.key"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises((FileExistsError, OSError)):
        generate_keypair(dangling, tmp_path / "dangling.pub")
    assert not (tmp_path / "nowhere").exists()


def test_the_public_key_destination_is_not_silently_replaced(tmp_path):
    """E3-4. A verifier pinned to the old key would see every signature as untrusted."""
    shared = tmp_path / "shared.pub"
    shared.write_text("SOMEONE-ELSES-PUBLIC-KEY\n")
    with pytest.raises(FileExistsError) as caught:
        generate_keypair(tmp_path / "d.key", shared)
    assert shared.read_text() == "SOMEONE-ELSES-PUBLIC-KEY\n"
    assert "--overwrite-public" in str(caught.value)
    # And the private key was not left behind by the refusal.
    assert not (tmp_path / "d.key").exists()


def test_rotation_is_possible_and_explicit(tmp_path):
    """Key rotation needs to replace a public key; it just has to say so."""
    shared = tmp_path / "rotate.pub"
    shared.write_text("OLD-KEY\n")
    new = generate_keypair(tmp_path / "new.key", shared, overwrite_public=True)
    assert shared.read_text().strip() == new


def test_only_one_of_two_racing_writers_creates_the_key(tmp_path):
    """Check-then-write is not exclusive; `O_CREAT | O_EXCL` makes creation the check."""
    path = tmp_path / "race.key"
    results: list[object] = []
    barrier = threading.Barrier(8)

    def attempt(n: int):
        barrier.wait()
        try:
            write_private_key(path, f"key-{n}".encode())
            results.append(n)
        except (FileExistsError, OSError) as e:
            results.append(e)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [r for r in results if isinstance(r, int)]
    assert len(winners) == 1, results
    assert path.read_bytes() == f"key-{winners[0]}".encode()


def test_a_failed_write_leaves_no_half_written_key(tmp_path):
    """A truncated key parses as nothing and fails at signing time, after a run."""
    import unittest.mock

    path = tmp_path / "partial.key"
    with (
        unittest.mock.patch("os.fdopen", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        write_private_key(path, b"pem")
    assert not path.exists()


def test_a_failed_write_does_not_leak_the_descriptor(tmp_path):
    """`fdopen` takes ownership only on success; on failure the fd is still ours.

    Leaking it also made the cleanup fail on Windows, where an open file cannot be
    deleted - so a failed write left a zero-length key behind, which is the one
    outcome `write_private_key` exists to prevent. Counted rather than asserted
    structurally, because the count is the thing that runs out.
    """
    import unittest.mock

    def open_descriptors() -> int:
        import pathlib as _pathlib

        probe = _pathlib.Path("/dev/fd")
        if not probe.is_dir():                   # pragma: no cover - not POSIX
            pytest.skip("no /dev/fd to count")
        return len(list(probe.iterdir()))

    before = open_descriptors()
    for i in range(50):
        with (
            unittest.mock.patch("os.fdopen", side_effect=OSError("disk full")),
            pytest.raises(OSError),
        ):
            write_private_key(tmp_path / f"k{i}.key", b"pem")
    assert open_descriptors() <= before + 2, "descriptors leaked on the failure path"
    assert not list(tmp_path.glob("k*.key"))


def test_the_key_still_signs_and_verifies(tmp_path):
    """The hardening must not change what the key is."""
    from conftest import make_dataset

    from evalseal.adapters.scorer import RegexScorer
    from evalseal.adapters.target import LocalCallableTarget
    from evalseal.executor import run_eval
    from evalseal.ledger import seal_and_append
    from evalseal.signing import sign_head, verify_signatures

    ledger = tmp_path / "l.jsonl"
    record = run_eval(make_dataset("q"),
                      LocalCallableTarget(lambda p: "yes", name="m"),
                      RegexScorer("yes"), n_repeats=2)
    seal_and_append(record, ledger)
    generate_keypair(tmp_path / "s.key", tmp_path / "s.pub")
    sign_head(ledger, tmp_path / "s.key")
    ok, message = verify_signatures(ledger, str(tmp_path / "s.pub"))
    assert ok, message


# --- the command ---------------------------------------------------------------------

def test_keygen_exits_1_and_explains_rather_than_overwriting(tmp_path):
    wide = CliRunner(env={"COLUMNS": "200"})
    assert wide.invoke(app, ["keygen", "--private-key", str(tmp_path / "k.key"),
                             "--public-key", str(tmp_path / "k.pub")]).exit_code == 0
    again = wide.invoke(app, ["keygen", "--private-key", str(tmp_path / "k.key"),
                              "--public-key", str(tmp_path / "k2.pub")])
    assert again.exit_code == 1
    assert "refusing to overwrite a private key" in again.output


def test_keygen_can_rotate_the_public_key_on_request(tmp_path):
    wide = CliRunner(env={"COLUMNS": "200"})
    assert wide.invoke(app, ["keygen", "--private-key", str(tmp_path / "a.key"),
                             "--public-key", str(tmp_path / "shared.pub")]).exit_code == 0
    rotated = wide.invoke(app, ["keygen", "--private-key", str(tmp_path / "b.key"),
                                "--public-key", str(tmp_path / "shared.pub"),
                                "--overwrite-public"])
    assert rotated.exit_code == 0, rotated.output
    assert (tmp_path / "shared.pub").read_text().strip() in rotated.output
