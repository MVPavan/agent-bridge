"""Permission callback contract.

Spec: docs/architecture.md §6.0.

The broker implements `PermissionCallback`. Worker adapters (Claude #2, later
Codex) bridge their native pre-execution hooks to this callback so the broker
has a single, transport-agnostic decision point.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PermissionDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    """The broker's verdict on a worker tool call."""

    decision: PermissionDecisionKind
    reason: str | None = None


class PermissionCallback(Protocol):
    """Synchronous-from-the-worker's-perspective permission decision.

    Implementations may be async internally; the worker adapter awaits this
    coroutine before letting the tool execute.
    """

    async def __call__(
        self,
        *,
        worker: str,
        tool_name: str,
        tool_input: dict[str, object],
    ) -> PermissionDecision: ...


async def deny_all(
    *,
    worker: str,
    tool_name: str,
    tool_input: dict[str, object],
) -> PermissionDecision:
    """A trivial PermissionCallback that denies everything. Useful in tests."""
    return PermissionDecision(
        decision=PermissionDecisionKind.DENY,
        reason=f"deny_all default policy (worker={worker!r}, tool={tool_name!r})",
    )


async def allow_all(
    *,
    worker: str,
    tool_name: str,
    tool_input: dict[str, object],
) -> PermissionDecision:
    """Permissive callback for harness tests where you don't actually want to gate."""
    return PermissionDecision(decision=PermissionDecisionKind.ALLOW)
