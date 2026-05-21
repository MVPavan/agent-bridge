"""MCP server exposing broker tools to Claude #1 (the cockpit session).

Spec: docs/architecture.md §3.2 + CP2 D-6.

The cockpit Claude attaches to this server via `--mcp-config` and calls:

  - `bridge_dispatch_goal(goal)`          -> conversation_id
  - `bridge_get_status(conversation_id)`  -> phase/round/current_turn/pending_approval
  - `bridge_approve(approval_id, ...)`    -> ok
  - `bridge_cancel(conversation_id)`      -> ok
  - `bridge_tail_transcript(...)`         -> events[]

This module is the **skeleton**: tool surfaces and request/response shapes
are real (typed with Pydantic, matching architecture.md §4.2 schema), but
the broker backend is still a stub. Full wiring lands in CP6 full + CP7
full. Stub responses are clearly tagged with a `stub` flag in the output
so a smoke test from Claude #1 can confirm the wiring is alive without
mistaking it for a real broker turn.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Protocol

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from .events import Phase, Worker

# ---------- Tool I/O shapes ---------------------------------------------------


class DispatchGoalResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    conversation_id: str
    phase: Phase
    stub: bool = False


class StatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    conversation_id: str
    phase: Phase
    round: int
    current_turn: Worker
    pending_approval_id: str | None = None
    last_seq: int
    stub: bool = False


class ApproveResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    approval_id: str
    accepted_decision: str
    stub: bool = False


class CancelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    conversation_id: str
    canceled: bool
    stub: bool = False


class TranscriptEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_id: str
    seq: int
    sender: Worker
    recipient: Worker
    phase: Phase
    kind: str
    content: str
    created_at: datetime


class TailTranscriptResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    conversation_id: str
    events: tuple[TranscriptEvent, ...]
    after_event_id: str | None
    stub: bool = False


# ---------- Backend protocol ---------------------------------------------------
#
# The broker (built in CP6 full) implements this. CP7 partial ships a
# `StubBackend` for smoke testing.


class BrokerBackend(Protocol):
    async def dispatch_goal(self, goal: str) -> DispatchGoalResponse: ...

    async def get_status(self, conversation_id: str) -> StatusResponse: ...

    async def approve(
        self, approval_id: str, decision: str, note: str | None
    ) -> ApproveResponse: ...

    async def cancel(self, conversation_id: str) -> CancelResponse: ...

    async def tail_transcript(
        self, conversation_id: str, after_event_id: str | None
    ) -> TailTranscriptResponse: ...


# ---------- Stub backend (CP7 partial) ----------------------------------------


class StubBackend:
    """Memory-only stand-in for the real broker.

    Lets us prove the MCP wiring end-to-end before the broker exists.
    Every response carries `stub=True`.
    """

    def __init__(self) -> None:
        self._next_conv = 1
        self._dispatched: dict[str, str] = {}

    async def dispatch_goal(self, goal: str) -> DispatchGoalResponse:
        cid = f"conv_stub_{self._next_conv:04d}"
        self._next_conv += 1
        self._dispatched[cid] = goal
        return DispatchGoalResponse(conversation_id=cid, phase=Phase.PLANNING, stub=True)

    async def get_status(self, conversation_id: str) -> StatusResponse:
        return StatusResponse(
            conversation_id=conversation_id,
            phase=Phase.PLANNING,
            round=0,
            current_turn=Worker.BROKER,
            pending_approval_id=None,
            last_seq=0,
            stub=True,
        )

    async def approve(
        self, approval_id: str, decision: str, note: str | None
    ) -> ApproveResponse:
        _ = note  # noted, ignored in stub
        return ApproveResponse(
            approval_id=approval_id, accepted_decision=decision, stub=True
        )

    async def cancel(self, conversation_id: str) -> CancelResponse:
        return CancelResponse(conversation_id=conversation_id, canceled=True, stub=True)

    async def tail_transcript(
        self, conversation_id: str, after_event_id: str | None
    ) -> TailTranscriptResponse:
        # Return one canned event so the cockpit can verify the round-trip.
        ev = TranscriptEvent(
            event_id="evt_stub_0001",
            seq=1,
            sender=Worker.BROKER,
            recipient=Worker.USER,
            phase=Phase.PLANNING,
            kind="system_note",
            content="agent-bridge stub backend — broker not yet wired",
            created_at=datetime.now(UTC),
        )
        return TailTranscriptResponse(
            conversation_id=conversation_id,
            events=(ev,),
            after_event_id=after_event_id,
            stub=True,
        )


# ---------- FastMCP wiring -----------------------------------------------------


def build_mcp_server(backend: BrokerBackend, *, name: str = "agent-bridge") -> FastMCP:
    """Construct a FastMCP server that routes its 5 tools to `backend`."""
    server: FastMCP = FastMCP(
        name=name,
        instructions=(
            "agent-bridge: broker control surface for the cockpit Claude session. "
            "Tools dispatch goals, query status, approve actions, cancel, and tail "
            "the transcript. See docs/architecture.md §3.2."
        ),
    )

    @server.tool(
        name="bridge_dispatch_goal",
        description=(
            "Start a new conversation with the given goal. Returns the new "
            "conversation_id and current phase."
        ),
    )
    async def bridge_dispatch_goal(
        goal: Annotated[str, Field(min_length=1, description="The user's coding goal.")],
    ) -> DispatchGoalResponse:
        return await backend.dispatch_goal(goal)

    @server.tool(
        name="bridge_get_status",
        description="Return phase, round, current_turn, and any pending approval.",
    )
    async def bridge_get_status(conversation_id: str) -> StatusResponse:
        return await backend.get_status(conversation_id)

    @server.tool(
        name="bridge_approve",
        description=(
            "Decide on a pending approval. `decision` must be 'approved' or 'rejected'."
        ),
    )
    async def bridge_approve(
        approval_id: str,
        decision: Annotated[str, Field(pattern="^(approved|rejected)$")],
        note: str | None = None,
    ) -> ApproveResponse:
        return await backend.approve(approval_id, decision, note)

    @server.tool(
        name="bridge_cancel",
        description="Cancel a running conversation. Idempotent.",
    )
    async def bridge_cancel(conversation_id: str) -> CancelResponse:
        return await backend.cancel(conversation_id)

    @server.tool(
        name="bridge_tail_transcript",
        description=(
            "Stream events for the conversation since the given event_id. "
            "Pass after_event_id=null to start from the beginning."
        ),
    )
    async def bridge_tail_transcript(
        conversation_id: str, after_event_id: str | None = None
    ) -> TailTranscriptResponse:
        return await backend.tail_transcript(conversation_id, after_event_id)

    return server


def build_stub_server(name: str = "agent-bridge-stub") -> FastMCP:
    """Convenience: server backed by `StubBackend`. Used for FP-8a-style probes."""
    return build_mcp_server(StubBackend(), name=name)
