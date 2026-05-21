"""Claude #2 worker adapter.

Spec: docs/architecture.md §6.0.1 and CP2 D-2.

Wraps `claude-agent-sdk`'s `ClaudeSDKClient` + `PreToolUse` hook so the
broker can drive a Claude Code session with a synchronous permission
boundary. The adapter's only job is to bridge the SDK's hook callback
shape to the broker's `PermissionCallback` protocol.

This module imports `claude_agent_sdk` lazily so unit tests that only
exercise the adapter's pure-logic surface (hook plumbing, message-text
extraction) do not require the SDK to be installed at collection time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .permission import PermissionCallback, PermissionDecisionKind

if TYPE_CHECKING:  # pragma: no cover
    from claude_agent_sdk.types import (
        ClaudeAgentOptions,
        PreToolUseHookInput,
    )

# ---------- Public dataclasses ------------------------------------------------


@dataclass(frozen=True)
class WorkerReply:
    """One full turn's worth of output from Claude #2."""

    text: str
    session_id: str | None
    raw_messages: tuple[Any, ...]


# ---------- Pure helpers (testable without the SDK installed) -----------------


def extract_text(message: Any) -> str:
    """Pull human-readable text out of one SDK message.

    Walks `message.content` (a list of blocks; `TextBlock` has `.text`) and
    falls back to `message.result` for `ResultMessage`. Returns "" if there
    is nothing useful.
    """
    parts: list[str] = []
    blocks = getattr(message, "content", None)
    if isinstance(blocks, list):
        for block in blocks:
            t = getattr(block, "text", None)
            if isinstance(t, str) and t:
                parts.append(t)
    result_str = getattr(message, "result", None)
    if isinstance(result_str, str) and result_str:
        parts.append(result_str)
    return "".join(parts)


def hook_decision_dict(
    decision: PermissionDecisionKind, *, reason: str | None
) -> dict[str, Any]:
    """Adapt a broker `PermissionDecision` to the SDK's hook return shape.

    The SDK's PreToolUse hook expects a dict with `permissionDecision` set
    to `"allow"` or `"deny"` and an optional `reason`.
    """
    payload: dict[str, Any] = {"permissionDecision": decision.value}
    if reason:
        payload["reason"] = reason
    return payload


# ---------- Live adapter (uses claude-agent-sdk) ------------------------------


def make_pre_tool_use_hook(
    *,
    worker_label: str,
    callback: PermissionCallback,
) -> Any:
    """Build a PreToolUse hook bound to the broker's `callback`.

    Returns a coroutine the SDK will call before any tool execution. The
    coroutine awaits the broker's decision and returns the SDK's expected
    decision dict.
    """

    async def hook(
        input_data: PreToolUseHookInput | dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        # SDK passes a dict-like payload; both attribute and dict access
        # work on the live HookInput type.
        if isinstance(input_data, dict):
            tool_name = str(input_data.get("tool_name", ""))
            tool_input = input_data.get("tool_input") or {}
        else:  # pragma: no cover — live SDK path
            tool_name = str(getattr(input_data, "tool_name", ""))
            tool_input = getattr(input_data, "tool_input", {}) or {}
        decision = await callback(
            worker=worker_label,
            tool_name=tool_name,
            tool_input=dict(tool_input),
        )
        return hook_decision_dict(decision.decision, reason=decision.reason)

    return hook


def build_options(
    *,
    session_id: str | None,
    resume: str | None,
    permission_callback: PermissionCallback,
    worker_label: str = "worker_a",
    system_prompt: str | None = None,
) -> ClaudeAgentOptions:
    """Build a `ClaudeAgentOptions` matching CP2 D-2.

    Either `session_id` (to pin a fresh session ID) OR `resume` (to resume
    an existing one) must be supplied, not both. Always `permission_mode='default'`
    so the broker's hook is the active enforcement layer.
    """
    if session_id and resume:
        raise ValueError("supply session_id OR resume, not both")
    from claude_agent_sdk import (  # local import keeps test collection lightweight
        ClaudeAgentOptions,
        HookMatcher,
    )

    hook = make_pre_tool_use_hook(
        worker_label=worker_label, callback=permission_callback
    )
    kwargs: dict[str, Any] = {
        "permission_mode": "default",
        "hooks": {
            "PreToolUse": [
                # Match all Bash, Edit, Write, etc. — the broker decides per call.
                HookMatcher(matcher=None, hooks=[hook]),
            ],
        },
    }
    if system_prompt is not None:
        kwargs["system_prompt"] = system_prompt
    if session_id:
        kwargs["session_id"] = session_id
    if resume:
        kwargs["resume"] = resume
    return ClaudeAgentOptions(**kwargs)
