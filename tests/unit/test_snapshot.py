"""Unit tests for the content snapshot + post-command validator."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from agent_bridge.snapshot import (
    ExpectedDelta,
    ExpectedDeltaEntry,
    compute_snapshot,
    validate_post_command,
)


def _sha256(b: bytes) -> str:
    return f"sha256:{hashlib.sha256(b).hexdigest()}"


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """Initialize a tiny git worktree with two tracked files."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n")
    (tmp_path / "alpha.py").write_text("print('alpha')\n")
    (tmp_path / "beta.py").write_text("print('beta')\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "junk.txt").write_text("ignored\n")
    (tmp_path / "scratch.log").write_text("ignored log\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_snapshot_covers_tracked_and_untracked_but_skips_gitignored(worktree: Path) -> None:
    # Add an untracked file.
    (worktree / "gamma.py").write_text("print('gamma')\n")

    snap = compute_snapshot(worktree)
    paths = set(snap.hashes)
    assert paths == {".gitignore", "alpha.py", "beta.py", "gamma.py"}
    # Ignored content must not appear.
    assert "ignored/junk.txt" not in paths
    assert "scratch.log" not in paths


def test_snapshot_hashes_match_explicit_sha256(worktree: Path) -> None:
    snap = compute_snapshot(worktree)
    assert snap.hashes["alpha.py"] == _sha256(b"print('alpha')\n")
    assert snap.hashes["beta.py"] == _sha256(b"print('beta')\n")


def test_validate_clean_when_no_changes(worktree: Path) -> None:
    pre = compute_snapshot(worktree)
    post = compute_snapshot(worktree)
    res = validate_post_command(pre, post, ExpectedDelta())
    assert res.ok
    assert res.findings == ()


def test_validate_clean_when_declared_change_matches(worktree: Path) -> None:
    pre = compute_snapshot(worktree)
    (worktree / "alpha.py").write_text("print('alpha v2')\n")
    post = compute_snapshot(worktree)

    expected = ExpectedDelta(
        entries=(
            ExpectedDeltaEntry(
                path="alpha.py",
                pre_hash=pre.hashes["alpha.py"],
                post_hash=post.hashes["alpha.py"],
            ),
        )
    )
    res = validate_post_command(pre, post, expected)
    assert res.ok, res.findings


def test_validate_flags_undeclared_change_to_tracked_file(worktree: Path) -> None:
    pre = compute_snapshot(worktree)
    (worktree / "alpha.py").write_text("print('alpha v2')\n")  # not declared
    post = compute_snapshot(worktree)
    res = validate_post_command(pre, post, ExpectedDelta())
    assert not res.ok
    assert any(f.path == "alpha.py" for f in res.findings)
    assert any("did not declare" in f.reason for f in res.findings)


def test_validate_flags_undeclared_new_untracked_file(worktree: Path) -> None:
    pre = compute_snapshot(worktree)
    (worktree / "surprise.py").write_text("hi\n")  # appears in post only
    post = compute_snapshot(worktree)
    res = validate_post_command(pre, post, ExpectedDelta())
    assert not res.ok
    assert any(f.path == "surprise.py" for f in res.findings)


def test_validate_flags_post_hash_mismatch_even_when_declared(worktree: Path) -> None:
    pre = compute_snapshot(worktree)
    (worktree / "alpha.py").write_text("print('alpha v2')\n")
    post = compute_snapshot(worktree)
    # Worker lies about the post_hash.
    expected = ExpectedDelta(
        entries=(
            ExpectedDeltaEntry(
                path="alpha.py",
                pre_hash=pre.hashes["alpha.py"],
                post_hash="sha256:" + "0" * 64,
            ),
        )
    )
    res = validate_post_command(pre, post, expected)
    assert not res.ok
    assert any("post_hash" in f.reason for f in res.findings)


def test_validate_flags_pre_hash_mismatch(worktree: Path) -> None:
    pre = compute_snapshot(worktree)
    (worktree / "alpha.py").write_text("print('alpha v2')\n")
    post = compute_snapshot(worktree)
    # Worker lies about the pre_hash too.
    expected = ExpectedDelta(
        entries=(
            ExpectedDeltaEntry(
                path="alpha.py",
                pre_hash="sha256:" + "f" * 64,
                post_hash=post.hashes["alpha.py"],
            ),
        )
    )
    res = validate_post_command(pre, post, expected)
    assert not res.ok
    assert any("pre_hash" in f.reason for f in res.findings)


def test_snapshot_raises_on_nonexistent_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(ValueError, match="not a directory"):
        compute_snapshot(missing)
