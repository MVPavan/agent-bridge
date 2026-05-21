"""Unit tests for the worktree manager."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from agent_bridge.snapshot import ExpectedDelta, ExpectedDeltaEntry
from agent_bridge.worktree import WorktreeError, WorktreeManager


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    """Initialize a tiny git repo to serve as the base for worktrees."""
    repo = tmp_path / "base"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "seed.py").write_text("print('seed')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def manager(base_repo: Path, tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(
        base_repo=base_repo, worktrees_root=tmp_path / "worktrees"
    )


# ---------- create / destroy --------------------------------------------------


async def test_create_worktree_initializes_branch_and_dir(
    manager: WorktreeManager,
) -> None:
    spec = await manager.create("conv_xyz")
    assert spec.path.is_dir()
    assert (spec.path / "seed.py").exists()
    assert spec.branch == "agent-bridge/conv_xyz"


async def test_create_twice_for_same_conversation_raises(
    manager: WorktreeManager,
) -> None:
    await manager.create("conv_dup")
    with pytest.raises(WorktreeError, match="already exists"):
        await manager.create("conv_dup")


async def test_destroy_removes_dir_and_branch(
    manager: WorktreeManager, base_repo: Path
) -> None:
    spec = await manager.create("conv_del")
    assert spec.path.exists()
    await manager.destroy(spec, force=True)
    assert not spec.path.exists()
    # Branch should be gone too.
    result = subprocess.run(
        ["git", "branch", "--list", spec.branch],
        cwd=base_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


async def test_destroy_force_handles_dirty_worktree(
    manager: WorktreeManager,
) -> None:
    spec = await manager.create("conv_dirty")
    (spec.path / "scratch.txt").write_text("uncommitted")
    # force=True must succeed despite the dirty state.
    await manager.destroy(spec, force=True)
    assert not spec.path.exists()


# ---------- snapshot integration ----------------------------------------------


async def test_snapshot_then_validate_clean(manager: WorktreeManager) -> None:
    spec = await manager.create("conv_snap")
    try:
        pre = manager.snapshot(spec)
        post = manager.snapshot(spec)
        result = manager.validate(spec, pre, post, ExpectedDelta())
        assert result.ok
    finally:
        await manager.destroy(spec, force=True)


async def test_snapshot_catches_undeclared_mutation(
    manager: WorktreeManager,
) -> None:
    spec = await manager.create("conv_mutate")
    try:
        pre = manager.snapshot(spec)
        (spec.path / "seed.py").write_text("print('mutated')\n")
        post = manager.snapshot(spec)
        result = manager.validate(spec, pre, post, ExpectedDelta())
        assert not result.ok
        assert any(f.path == "seed.py" for f in result.findings)
    finally:
        await manager.destroy(spec, force=True)


async def test_snapshot_accepts_declared_mutation(
    manager: WorktreeManager,
) -> None:
    spec = await manager.create("conv_declared")
    try:
        pre = manager.snapshot(spec)
        (spec.path / "seed.py").write_text("print('v2')\n")
        post = manager.snapshot(spec)
        expected = ExpectedDelta(
            entries=(
                ExpectedDeltaEntry(
                    path="seed.py",
                    pre_hash=pre.hashes["seed.py"],
                    post_hash=post.hashes["seed.py"],
                ),
            )
        )
        result = manager.validate(spec, pre, post, expected)
        assert result.ok
    finally:
        await manager.destroy(spec, force=True)


# ---------- diff utilities ----------------------------------------------------


async def test_diff_for_review_returns_diff_of_changes(
    manager: WorktreeManager,
) -> None:
    spec = await manager.create("conv_diff")
    try:
        (spec.path / "seed.py").write_text("print('updated')\n")
        diff = await manager.diff_for_review(spec)
        assert "-print('seed')" in diff
        assert "+print('updated')" in diff
    finally:
        await manager.destroy(spec, force=True)


async def test_status_porcelain_reflects_changes(
    manager: WorktreeManager,
) -> None:
    spec = await manager.create("conv_status")
    try:
        (spec.path / "seed.py").write_text("print('changed')\n")
        (spec.path / "new.py").write_text("print('new')\n")
        status = await manager.status_porcelain(spec)
        assert "seed.py" in status
        assert "new.py" in status
    finally:
        await manager.destroy(spec, force=True)


# ---------- concurrency sanity ------------------------------------------------


async def test_multiple_worktrees_concurrent_creation(
    manager: WorktreeManager,
) -> None:
    """Two conversations can hold their own worktrees simultaneously."""
    specs = await asyncio.gather(
        manager.create("conv_par_1"),
        manager.create("conv_par_2"),
    )
    try:
        assert specs[0].path != specs[1].path
        assert (specs[0].path / "seed.py").exists()
        assert (specs[1].path / "seed.py").exists()
    finally:
        await asyncio.gather(
            *(manager.destroy(s, force=True) for s in specs)
        )
