"""Unit tests for the MCP server skeleton.

Exercises the tool surface end-to-end via FastMCP's in-process `call_tool`
without binding a network port. The full transport test (stdio session with
a real client) lives at CP7 close.
"""

from __future__ import annotations

import json

import pytest

from agent_bridge.events import Phase, Worker
from agent_bridge.mcp_server import (
    DispatchGoalResponse,
    StubBackend,
    build_mcp_server,
    build_stub_server,
)


def _parse_tool_result(result: object) -> dict[str, object]:
    """FastMCP's `call_tool` returns a (content_blocks, structured_content) tuple;
    we expect structured content with the response model."""
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        if isinstance(structured, dict):
            return dict(structured)
        if isinstance(content, list):
            for block in content:
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    try:
                        return dict(json.loads(t))
                    except json.JSONDecodeError:
                        continue
    raise AssertionError(f"unexpected tool result shape: {result!r}")


# ---------- Stub-backed end-to-end tests --------------------------------------


async def test_dispatch_goal_returns_new_conversation_id() -> None:
    server = build_stub_server()
    result = await server.call_tool("bridge_dispatch_goal", {"goal": "add a util"})
    parsed = _parse_tool_result(result)
    cid = parsed["conversation_id"]
    assert isinstance(cid, str)
    assert cid.startswith("conv_stub_")
    assert parsed["phase"] == Phase.PLANNING.value
    assert parsed["stub"] is True


async def test_get_status_returns_planning_for_stub() -> None:
    server = build_stub_server()
    result = await server.call_tool(
        "bridge_get_status", {"conversation_id": "conv_stub_0001"}
    )
    parsed = _parse_tool_result(result)
    assert parsed["phase"] == Phase.PLANNING.value
    assert parsed["round"] == 0
    assert parsed["current_turn"] == Worker.BROKER.value
    assert parsed["pending_approval_id"] is None
    assert parsed["stub"] is True


async def test_approve_validates_decision_pattern() -> None:
    server = build_stub_server()
    # valid
    result = await server.call_tool(
        "bridge_approve",
        {"approval_id": "appr_1", "decision": "approved", "note": "looks good"},
    )
    parsed = _parse_tool_result(result)
    assert parsed["accepted_decision"] == "approved"
    assert parsed["stub"] is True


async def test_approve_rejects_invalid_decision() -> None:
    server = build_stub_server()
    with pytest.raises(Exception):  # noqa: B017 — pydantic/MCP raise multiple distinct exception types here  # FastMCP raises on schema violation
        await server.call_tool(
            "bridge_approve",
            {"approval_id": "appr_1", "decision": "maybe", "note": None},
        )


async def test_cancel_returns_canceled_true() -> None:
    server = build_stub_server()
    result = await server.call_tool(
        "bridge_cancel", {"conversation_id": "conv_stub_0001"}
    )
    parsed = _parse_tool_result(result)
    assert parsed["canceled"] is True


async def test_tail_transcript_returns_canned_event() -> None:
    server = build_stub_server()
    result = await server.call_tool(
        "bridge_tail_transcript",
        {"conversation_id": "conv_stub_0001", "after_event_id": None},
    )
    parsed = _parse_tool_result(result)
    assert parsed["conversation_id"] == "conv_stub_0001"
    events_obj = parsed["events"]
    assert isinstance(events_obj, list)
    assert len(events_obj) == 1
    first = events_obj[0]
    assert isinstance(first, dict)
    assert first["kind"] == "system_note"
    assert "stub" in first["content"]


async def test_server_lists_all_five_tools() -> None:
    server = build_stub_server()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "bridge_dispatch_goal",
        "bridge_get_status",
        "bridge_approve",
        "bridge_cancel",
        "bridge_tail_transcript",
    }


async def test_backend_protocol_satisfied_by_stub() -> None:
    # Smoke test: StubBackend can be wired as a BrokerBackend without errors.
    server = build_mcp_server(StubBackend())
    result = await server.call_tool("bridge_dispatch_goal", {"goal": "anything"})
    parsed = _parse_tool_result(result)
    assert parsed["phase"] == Phase.PLANNING.value


async def test_dispatch_goal_response_is_a_frozen_pydantic_model() -> None:
    r = DispatchGoalResponse(conversation_id="x", phase=Phase.PLANNING)
    with pytest.raises(Exception):  # noqa: B017 — pydantic/MCP raise multiple distinct exception types here
        r.conversation_id = "y"
