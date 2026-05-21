"""Real implementation of `BrokerBackend` (the MCP server's backend).

Spec: docs/architecture.md §3.2 + CP7.

CP7 partial shipped `StubBackend` in `mcp_server.py`. This module wires the
real broker into the same interface. Claude #1 (the cockpit) calls MCP
tools, the FastMCP server routes them to this class, and this class
delegates to `Broker`.
"""

from __future__ import annotations

from typing import Any

from .broker import Broker
from .events import Phase, TurnHolder
from .mcp_server import (
    ApproveResponse,
    CancelResponse,
    DispatchGoalResponse,
    StatusResponse,
    TailTranscriptResponse,
    TranscriptEvent,
)


class RealBrokerBackend:
    """Concrete `BrokerBackend` implementation.

    Methods match the `BrokerBackend` Protocol in `mcp_server.py`. We do
    not formally subclass the Protocol — duck typing is sufficient and
    `FastMCP` will accept any object whose method signatures match.
    """

    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    async def dispatch_goal(self, goal: str) -> DispatchGoalResponse:
        cid = await self.broker.dispatch_goal(goal)
        return DispatchGoalResponse(conversation_id=cid, phase=Phase.PLANNING, stub=False)

    async def get_status(self, conversation_id: str) -> StatusResponse:
        snap = await self.broker.get_status_snapshot(conversation_id)
        if snap is None:
            return StatusResponse(
                conversation_id=conversation_id,
                phase=Phase.ABORTED,
                round=0,
                current_turn=TurnHolder.BROKER,
                pending_approval_id=None,
                last_seq=0,
                stub=False,
            )
        return StatusResponse(
            conversation_id=snap["conversation_id"],
            phase=snap["phase"],
            round=int(snap["round"]),
            current_turn=snap["current_turn"],
            pending_approval_id=snap["pending_approval_id"],
            last_seq=int(snap["last_seq"]),
            stub=False,
        )

    async def approve(
        self, approval_id: str, decision: str, note: str | None
    ) -> ApproveResponse:
        await self.broker.approve(approval_id, decision, note=note)
        return ApproveResponse(
            approval_id=approval_id, accepted_decision=decision, stub=False
        )

    async def cancel(self, conversation_id: str) -> CancelResponse:
        await self.broker.cancel(conversation_id)
        return CancelResponse(
            conversation_id=conversation_id, canceled=True, stub=False
        )

    async def tail_transcript(
        self, conversation_id: str, after_event_id: str | None
    ) -> TailTranscriptResponse:
        events = await self.broker.tail_transcript(conversation_id, after_event_id)
        transcript_events = tuple(
            TranscriptEvent(
                event_id=e.event_id,
                seq=e.seq,
                sender=e.sender,
                recipient=e.recipient,
                phase=e.phase,
                kind=e.kind.value,
                content=e.content,
                created_at=e.created_at,
            )
            for e in events
        )
        return TailTranscriptResponse(
            conversation_id=conversation_id,
            events=transcript_events,
            after_event_id=after_event_id,
            stub=False,
        )


def _silence_unused_imports() -> tuple[Any, ...]:  # pragma: no cover
    """Anchor for the imports above so they survive --fix passes."""
    return (
        DispatchGoalResponse,
        StatusResponse,
        ApproveResponse,
        CancelResponse,
        TailTranscriptResponse,
        TranscriptEvent,
    )
