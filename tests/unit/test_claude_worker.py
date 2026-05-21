"""Non-live unit tests for the Claude worker adapter.

These exercise the pure-logic surface (text extraction, hook plumbing,
options construction) without spawning a real Claude session. The live
3-turn integration test lives under `@pytest.mark.live` once the broker
is wired (CP4 close).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent_bridge.claude_worker import (
    extract_text,
    hook_decision_dict,
    make_pre_tool_use_hook,
)
from agent_bridge.permission import (
    PermissionDecision,
    PermissionDecisionKind,
    allow_all,
    deny_all,
)

# ---------- extract_text ------------------------------------------------------


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeAssistantMessage:
    content: list[Any]


@dataclass
class _FakeResultMessage:
    result: str


def test_extract_text_concatenates_text_blocks() -> None:
    msg = _FakeAssistantMessage(
        content=[_FakeTextBlock(text="hello "), _FakeTextBlock(text="world")]
    )
    assert extract_text(msg) == "hello world"


def test_extract_text_falls_back_to_result_attribute() -> None:
    msg = _FakeResultMessage(result="final")
    assert extract_text(msg) == "final"


def test_extract_text_ignores_unknown_blocks() -> None:
    msg = _FakeAssistantMessage(content=[object(), _FakeTextBlock(text="ok"), object()])
    assert extract_text(msg) == "ok"


def test_extract_text_returns_empty_for_message_without_anything() -> None:
    assert extract_text(object()) == ""


# ---------- hook_decision_dict ------------------------------------------------


def test_hook_decision_dict_allow_without_reason() -> None:
    payload = hook_decision_dict(PermissionDecisionKind.ALLOW, reason=None)
    assert payload == {"permissionDecision": "allow"}


def test_hook_decision_dict_deny_with_reason() -> None:
    payload = hook_decision_dict(PermissionDecisionKind.DENY, reason="not allowed")
    assert payload == {"permissionDecision": "deny", "reason": "not allowed"}


# ---------- make_pre_tool_use_hook (the bridge to PermissionCallback) ---------


async def test_pre_tool_use_hook_delegates_to_callback_and_returns_deny() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    async def cb(
        *, worker: str, tool_name: str, tool_input: dict[str, object]
    ) -> PermissionDecision:
        calls.append((worker, tool_name, tool_input))
        return PermissionDecision(
            decision=PermissionDecisionKind.DENY, reason="test denies all"
        )

    hook = make_pre_tool_use_hook(worker_label="worker_a", callback=cb)
    result = await hook(
        {"tool_name": "Bash", "tool_input": {"command": "echo HACKED"}},
        "toolu_abc",
        None,
    )
    assert result == {"permissionDecision": "deny", "reason": "test denies all"}
    assert calls == [("worker_a", "Bash", {"command": "echo HACKED"})]


async def test_pre_tool_use_hook_passes_through_allow() -> None:
    hook = make_pre_tool_use_hook(worker_label="worker_a", callback=allow_all)
    result = await hook(
        {"tool_name": "Read", "tool_input": {"path": "/etc/hostname"}},
        "toolu_xyz",
        None,
    )
    assert result == {"permissionDecision": "allow"}


async def test_pre_tool_use_hook_handles_missing_fields_gracefully() -> None:
    hook = make_pre_tool_use_hook(worker_label="worker_a", callback=deny_all)
    result = await hook({}, "toolu_empty", None)
    assert result["permissionDecision"] == "deny"
    assert "tool=''" in (result.get("reason") or "")


# ---------- Live spawn — gated by @pytest.mark.live ---------------------------


@pytest.mark.live
async def test_live_claude_spawn_three_turn_session(tmp_path: Any) -> None:
    """Live FP-1 equivalent — only runs with -m live and a real subscription.

    Excluded from the default `pytest` invocation per `pyproject.toml`.
    """
    import uuid

    from claude_agent_sdk import ClaudeSDKClient

    from agent_bridge.claude_worker import build_options

    sid = str(uuid.uuid4())
    seen_ids: set[str] = set()
    opts = build_options(
        session_id=sid,
        resume=None,
        permission_callback=deny_all,
        worker_label="worker_a_live_test",
        system_prompt="You are a feasibility probe target. Reply concisely.",
    )
    async with ClaudeSDKClient(options=opts) as client:
        for prompt in ("Reply with A only.", "Reply with B only.", "Reply with C only."):
            await client.query(prompt=prompt)
            async for msg in client.receive_response():
                mid = getattr(msg, "session_id", None)
                if isinstance(mid, str):
                    seen_ids.add(mid)
    assert seen_ids == {sid}, f"expected single session_id {sid}, saw {seen_ids}"
