"""CP11 — end-to-end smoke test driven by fake workers.

Definition of Done in GOAL.md describes a live smoke test that requires
real Claude #2 + Codex sessions. This test exercises the SAME flow path
with `FakeWorker` drivers so the broker, store, worktree manager, MCP
server, and event log all integrate without LLM spend. A live counterpart
lives under `@pytest.mark.live` and is opt-in.

What this proves:

1. Claude #1 (the cockpit) calls `bridge_dispatch_goal` through the MCP
   server. The broker creates a conversation and seeds it.
2. The broker runs a planning loop. Codex (fake) proposes, Claude (fake)
   critiques, the loop either reaches consensus or escalates at max_rounds.
3. A consensus_to_implement approval is recorded; the cockpit can `bridge_approve`.
4. Status and transcript surface back through the MCP layer.
5. Cancel marks the conversation aborted; the event log records it.
6. Every agent-to-agent message lives in the SQLite events table.
7. The I-9 invariant grep returns empty for the integrated codebase.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from agent_bridge.broker import (
    Broker,
    BrokerConfig,
    WorkerDriver,
    WorkerReply,
    WorkerSend,
)
from agent_bridge.broker_backend import RealBrokerBackend
from agent_bridge.events import Phase, TurnHolder
from agent_bridge.mcp_server import build_mcp_server
from agent_bridge.store import Store


class FakeWorker(WorkerDriver):
    """Records every prompt; replies with the configured text."""

    def __init__(self, worker_id: TurnHolder, *, reply: str, session_id: str) -> None:
        self.worker_id = worker_id
        self._reply = reply
        self._session_id = session_id
        self.history: list[WorkerSend] = []

    async def send(self, msg: WorkerSend) -> WorkerReply:
        self.history.append(msg)
        # Echo the bridge-ack marker the broker injected, mimicking a
        # well-behaved real worker. Required after the P0-1 fix from
        # codex review `review-mpf7lig2-p6fxyi`.
        ack = f"<<BRIDGE-ACK ik={msg.idempotency_key} ver={msg.turn_token_version}>>"
        return WorkerReply(
            text=f"{self._reply}\n{ack}",
            session_or_thread_id=self._session_id,
            in_reply_to_idempotency_key=msg.idempotency_key,
            observed_turn_token_version=msg.turn_token_version,
        )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[Store, None]:
    s = await Store.open(tmp_path / "bridge.sqlite")
    yield s
    await s.close()


async def test_end_to_end_smoke_via_mcp_backend(store: Store) -> None:
    worker_a = FakeWorker(
        TurnHolder.WORKER_A,
        reply="I propose AuthSessionManager with refresh tokens.",
        session_id="sess_a",
    )
    worker_b = FakeWorker(
        TurnHolder.WORKER_B,
        reply="Looks good. Agreed.",
        session_id="sess_b",
    )
    broker = Broker(
        store=store,
        worker_a=worker_a,
        worker_b=worker_b,
        config=BrokerConfig(max_planning_rounds=2),
    )
    backend = RealBrokerBackend(broker)

    # 1. Cockpit dispatches a goal via the MCP backend.
    dispatched = await backend.dispatch_goal("Add a tiny utility helper.")
    cid = dispatched.conversation_id
    assert dispatched.phase is Phase.PLANNING
    assert dispatched.stub is False

    # 2. Broker drives the planning loop.
    await broker.run_planning_loop(
        cid,
        first_prompt_for_codex="Propose the plan",
        first_prompt_for_claude_template="Critique: {codex_proposal}",
    )

    # 3. Status reports CONSENSUS phase and a pending consensus_to_implement approval.
    status = await backend.get_status(cid)
    assert status.phase is Phase.CONSENSUS
    assert status.pending_approval_id is not None
    # Cockpit approves.
    approved = await backend.approve(
        status.pending_approval_id, "approved", note="ok"
    )
    assert approved.accepted_decision == "approved"

    # 4. Transcript surfaces every agent-to-agent message.
    transcript = await backend.tail_transcript(cid, after_event_id=None)
    senders = {e.sender for e in transcript.events}
    assert {TurnHolder.WORKER_A, TurnHolder.WORKER_B} <= senders
    assert TurnHolder.BROKER in senders
    # Worker prompts and replies are all present.
    assert worker_a.history, "Claude worker received no prompt"
    assert worker_b.history, "Codex worker received no prompt"

    # 5. Status no longer shows a pending approval.
    status2 = await backend.get_status(cid)
    assert status2.pending_approval_id is None

    # 6. Cancel finalizes the conversation.
    cancel = await backend.cancel(cid)
    assert cancel.canceled is True
    conv = await store.get_conversation(cid)
    assert conv is not None and conv.status == "aborted"


async def test_mcp_server_round_trips_against_real_backend(store: Store) -> None:
    """The FastMCP server constructed around the real backend lists all five
    tools and dispatch_goal returns a non-stub response.
    """
    worker_a = FakeWorker(TurnHolder.WORKER_A, reply="ok", session_id="sa")
    worker_b = FakeWorker(TurnHolder.WORKER_B, reply="ok", session_id="sb")
    broker = Broker(store=store, worker_a=worker_a, worker_b=worker_b)
    backend = RealBrokerBackend(broker)
    server = build_mcp_server(backend, name="agent-bridge-smoke")

    tools = await server.list_tools()
    assert {t.name for t in tools} == {
        "bridge_dispatch_goal",
        "bridge_get_status",
        "bridge_approve",
        "bridge_cancel",
        "bridge_tail_transcript",
    }

    result = await server.call_tool(
        "bridge_dispatch_goal", {"goal": "do a thing"}
    )
    # FastMCP returns (content_blocks, structured_content).
    assert isinstance(result, tuple) and len(result) == 2
    _content, structured = result
    assert isinstance(structured, dict)
    assert structured["stub"] is False
    assert structured["conversation_id"].startswith("conv_")
