"""Worktree content snapshot.

Spec: docs/architecture.md §6.3.1.1.

Before any allowlisted command in an implementation/review phase, the broker
captures a SHA-256 hash of every non-ignored file in the worktree (tracked
AND existing untracked — round 5 of the CP1 review closed the symmetric
blind spot). After the command, it recomputes and validates that any file
whose hash changed is either:

  (a) explicitly declared by the worker in an approved `diff` event with a
      matching `post_hash`, or
  (b) the same content — i.e. no change at all.

Anything else is an unexpected mutation, which becomes a `risky_command`
approval request and blocks phase advance.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Reasonable defaults; CP2 D-5 left these tunable.
MAX_FILES_PER_SNAPSHOT = 50_000
MAX_FILE_SIZE_BYTES = 16 * 1024 * 1024  # 16 MiB


# ---------- Snapshot models ----------------------------------------------------


@dataclass(frozen=True)
class WorktreeSnapshot:
    """A SHA-256-keyed view of every non-ignored file in a worktree.

    Maps repo-relative POSIX path strings to their `sha256:<hex>` hash.
    """

    hashes: dict[str, str]
    worktree_root: Path


@dataclass(frozen=True)
class ExpectedDeltaEntry:
    """One file the worker explicitly says it changed during the command."""

    path: str
    pre_hash: str | None  # None if the file was newly created
    post_hash: str


@dataclass(frozen=True)
class ExpectedDelta:
    """Collection of (path, post_hash) pairs declared via `diff` events."""

    entries: tuple[ExpectedDeltaEntry, ...] = ()


@dataclass(frozen=True)
class MutationFinding:
    """One unexpected mutation."""

    path: str
    reason: str


@dataclass(frozen=True)
class SnapshotValidation:
    """Result of validating a post-command snapshot."""

    ok: bool
    findings: tuple[MutationFinding, ...] = field(default_factory=tuple)


# ---------- Hashing ------------------------------------------------------------


def _sha256_file(p: Path, *, max_size: int = MAX_FILE_SIZE_BYTES) -> str:
    """Hash one file. Reads in 1 MiB chunks; aborts if size exceeds max_size."""
    h = hashlib.sha256()
    size = 0
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            size += len(chunk)
            if size > max_size:
                raise ValueError(f"file exceeds max snapshot size: {p}")
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _git_ls_files(worktree: Path) -> list[str]:
    """Run `git ls-files --cached --others --exclude-standard` to list
    every non-ignored file in the worktree (tracked + untracked,
    excluding gitignored).

    Returns POSIX-style repo-relative paths.
    """
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed in {worktree}: {result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line]


def compute_snapshot(
    worktree: Path,
    *,
    max_files: int = MAX_FILES_PER_SNAPSHOT,
    max_file_size: int = MAX_FILE_SIZE_BYTES,
) -> WorktreeSnapshot:
    """Hash every non-ignored file in `worktree`."""
    worktree = worktree.resolve()
    if not worktree.is_dir():
        raise ValueError(f"worktree is not a directory: {worktree}")

    paths = _git_ls_files(worktree)
    if len(paths) > max_files:
        raise ValueError(
            f"worktree has {len(paths)} non-ignored files, exceeds limit {max_files}"
        )

    hashes: dict[str, str] = {}
    for rel in paths:
        abs_p = worktree / rel
        # `git ls-files --others` can include staged-but-deleted entries; skip.
        if not abs_p.is_file():
            continue
        hashes[rel] = _sha256_file(abs_p, max_size=max_file_size)
    return WorktreeSnapshot(hashes=hashes, worktree_root=worktree)


# ---------- Validation ---------------------------------------------------------


def validate_post_command(
    pre: WorktreeSnapshot,
    post: WorktreeSnapshot,
    expected: ExpectedDelta,
) -> SnapshotValidation:
    """Per §6.3.1.1: every changed tracked-or-untracked file must be either
    unchanged or explicitly declared in `expected` with matching post_hash.
    """
    if pre.worktree_root != post.worktree_root:
        raise ValueError("pre and post snapshots are for different worktrees")

    declared = {e.path: e for e in expected.entries}
    findings: list[MutationFinding] = []

    # All paths that exist in either snapshot.
    all_paths = set(pre.hashes) | set(post.hashes)

    for path in sorted(all_paths):
        pre_hash = pre.hashes.get(path)
        post_hash = post.hashes.get(path)

        if pre_hash == post_hash:
            # Unchanged (or missing in both — won't happen but defensive).
            continue

        decl = declared.get(path)
        if decl is None:
            findings.append(
                MutationFinding(
                    path=path,
                    reason=(
                        "tracked/untracked file changed but worker did not "
                        "declare it in a diff event"
                    ),
                )
            )
            continue

        if pre_hash is None and decl.pre_hash is not None:
            findings.append(
                MutationFinding(
                    path=path,
                    reason=(
                        f"file is new in post but worker declared a non-null "
                        f"pre_hash {decl.pre_hash!r}"
                    ),
                )
            )
            continue

        if pre_hash != decl.pre_hash:
            findings.append(
                MutationFinding(
                    path=path,
                    reason=(
                        f"declared pre_hash {decl.pre_hash!r} does not match "
                        f"actual {pre_hash!r}"
                    ),
                )
            )
            continue

        if post_hash != decl.post_hash:
            findings.append(
                MutationFinding(
                    path=path,
                    reason=(
                        f"declared post_hash {decl.post_hash!r} does not match "
                        f"actual {post_hash!r}"
                    ),
                )
            )
            continue

    return SnapshotValidation(ok=not findings, findings=tuple(findings))
