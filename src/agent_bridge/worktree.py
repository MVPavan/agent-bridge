"""Git worktree management for conversation isolation.

Spec: docs/architecture.md §2.4 + §6.3.1.1 + invariant I-8.

Each conversation owns its own git worktree under
`agent-bridge/worktrees/<conversation_id>/`. The worktree is created from
a base ref (typically `main` or `HEAD`), used for one implementation
cycle, and torn down on conversation close.

This module is pure git plumbing. The broker calls it; tests can drive
it directly against a tmp repo.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from .snapshot import (
    ExpectedDelta,
    SnapshotValidation,
    WorktreeSnapshot,
    compute_snapshot,
    validate_post_command,
)


@dataclass(frozen=True)
class WorktreeSpec:
    """Where a conversation's worktree lives and what ref it tracks."""

    conversation_id: str
    path: Path
    base_repo: Path
    branch: str
    base_ref: str = "HEAD"


class WorktreeError(RuntimeError):
    """Raised when git plumbing fails."""


async def _git(cwd: Path, *args: str, timeout: float = 30.0) -> str:
    """Run a git command in `cwd`, return stdout, raise on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise WorktreeError(f"git {' '.join(args)} timed out") from e
    if proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed: {stderr.decode('utf-8', errors='replace').strip()}"
        )
    return stdout.decode("utf-8", errors="replace")


class WorktreeManager:
    """Per-broker manager that creates / destroys worktrees as conversations open and close.

    `worktrees_root` is where new worktrees are placed. `base_repo` is the
    repo they branch off; in production this is the project root.
    """

    def __init__(self, *, base_repo: Path, worktrees_root: Path) -> None:
        self.base_repo = base_repo.resolve()
        self.worktrees_root = worktrees_root.resolve()
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

    # ---------- Lifecycle ----------

    async def create(
        self,
        conversation_id: str,
        *,
        base_ref: str = "HEAD",
        branch_prefix: str = "agent-bridge",
    ) -> WorktreeSpec:
        """Spin up a new worktree for `conversation_id`."""
        path = self.worktrees_root / conversation_id
        if path.exists():
            raise WorktreeError(f"worktree already exists at {path}")
        branch = f"{branch_prefix}/{conversation_id}"
        await _git(
            self.base_repo, "worktree", "add", "-b", branch, str(path), base_ref
        )
        return WorktreeSpec(
            conversation_id=conversation_id,
            path=path.resolve(),
            base_repo=self.base_repo,
            branch=branch,
            base_ref=base_ref,
        )

    async def destroy(self, spec: WorktreeSpec, *, force: bool = False) -> None:
        """Remove the worktree from git's tracking and delete its dir.

        `force=True` allows removal even when the worktree has uncommitted
        changes; the broker uses this on conversation abort.
        """
        args = ["worktree", "remove", str(spec.path)]
        if force:
            args.insert(2, "--force")
        try:
            await _git(self.base_repo, *args)
        except WorktreeError:
            # Fall back to manual cleanup if git refuses (e.g., dir already gone).
            if spec.path.exists():
                shutil.rmtree(spec.path, ignore_errors=True)
        # Delete the branch too — we never want left-over branches accumulating.
        with contextlib.suppress(WorktreeError):
            await _git(self.base_repo, "branch", "-D", spec.branch)

    async def list_active(self) -> list[str]:
        """List all worktree paths registered by git (for housekeeping)."""
        out = await _git(self.base_repo, "worktree", "list", "--porcelain")
        return [
            line.removeprefix("worktree ").strip()
            for line in out.splitlines()
            if line.startswith("worktree ")
        ]

    # ---------- Snapshot integration (§6.3.1.1) ----------

    def snapshot(self, spec: WorktreeSpec) -> WorktreeSnapshot:
        """Hash every non-ignored file in the worktree."""
        return compute_snapshot(spec.path)

    def validate(
        self,
        spec: WorktreeSpec,
        pre: WorktreeSnapshot,
        post: WorktreeSnapshot,
        expected: ExpectedDelta,
    ) -> SnapshotValidation:
        """Per §6.3.1.1: every changed tracked-or-untracked file must be either
        unchanged or declared in `expected` with a matching post_hash."""
        if pre.worktree_root != spec.path or post.worktree_root != spec.path:
            raise WorktreeError("snapshot worktree_root does not match spec.path")
        return validate_post_command(pre, post, expected)

    # ---------- Diff utilities ----------

    async def status_porcelain(self, spec: WorktreeSpec) -> str:
        return await _git(spec.path, "status", "--porcelain")

    async def diff_for_review(self, spec: WorktreeSpec) -> str:
        """Return a unified diff vs the base ref for review handoff."""
        return await _git(spec.path, "diff", spec.base_ref, "--unified=3")
