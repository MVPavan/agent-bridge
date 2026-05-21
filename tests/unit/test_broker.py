"""Unit tests for the broker orchestration loop.

Uses in-memory `FakeWorkerDriver`s so the broker can be exercised end-to-end
without spawning real Claude or Codex sessions.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
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
from agent_bridge.events import (
    EventKind,
    OutboxStatus,
    Phase,
    TurnHolder,
)
from agent_bridge.store import Store


@dataclass
class FakeWorkerDriver(WorkerDriver):
    worker_id: TurnHolder
    reply_fn: Callable[[WorkerSend], str] = lambda _msg: "ok"
    seen: list[WorkerSend] = field(default_factory=list)
    session_id: str = "sess_fake"
    inject_session_each_call: bool = True
    # When True (default), the fake worker echoes the bridge-ack marker that
    # the broker injected, mimicking a well-behaved real worker. Tests that
    # want to simulate a misbehaving worker set this to False.
    echo_ack: bool = True
    # When set, the fake echoes a custom (key, version) instead of the actual
    # ones the broker sent — used by quarantine regression tests.
    ack_override: tuple[str, int] | None = None

    async def send(self, msg: WorkerSend) -> WorkerReply:
        self.seen.append(msg)
        text = self.reply_fn(msg)
        ik: str | None = None
        ver: int | None = None
        if self.echo_ack:
            if self.ack_override is not None:
                ik, ver = self.ack_override
            else:
                ik, ver = msg.idempotency_key, msg.turn_token_version
            text = f"{text}\n<<BRIDGE-ACK ik={ik} ver={ver}>>"
        return WorkerReply(
            text=text,
            session_or_thread_id=(
                self.session_id if self.inject_session_each_call else None
            ),
            in_reply_to_idempotency_key=ik,
            observed_turn_token_version=ver,
        )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[Store, None]:
    s = await Store.open(tmp_path / "bridge.sqlite")
    yield s
    await s.close()


@pytest.fixture
def broker(store: Store) -> Broker:
    return Broker(
        store=store,
        worker_a=FakeWorkerDriver(worker_id=TurnHolder.WORKER_A, session_id="sess_a"),
        worker_b=FakeWorkerDriver(worker_id=TurnHolder.WORKER_B, session_id="sess_b"),
        config=BrokerConfig(max_planning_rounds=3),
    )


# ---------- dispatch_goal -----------------------------------------------------


async def test_dispatch_goal_creates_conversation_and_seed_event(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("add a util function")
    conv = await store.get_conversation(cid)
    assert conv is not None
    assert conv.goal == "add a util function"
    assert conv.phase is Phase.PLANNING
    events = await store.list_events(cid)
    assert len(events) == 1
    assert events[0].kind is EventKind.SYSTEM_NOTE
    assert "Goal:" in events[0].content


# ---------- deliver_turn_to ---------------------------------------------------


async def test_deliver_turn_advances_outbox_through_full_fsm(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("x")
    reply = await broker.deliver_turn_to(
        cid, TurnHolder.WORKER_B, "Reply with the letter A.",
        phase=Phase.PLANNING, round_=1,
    )
    # The reply event was appended.
    assert reply.kind is EventKind.CRITIQUE  # broker's generic worker-reply kind
    # An outbox row exists in ANSWERED state.
    events = await store.list_events(cid)
    # We expect 3 events: the initial goal SYSTEM_NOTE, the broker→worker prompt,
    # and the worker→broker reply.
    assert len(events) == 3
    # The reply carries the correlation tokens the broker assigned.
    reply_event = events[-1]
    assert reply_event.in_reply_to_idempotency_key is not None
    assert reply_event.observed_turn_token_version is not None


async def test_deliver_turn_quarantines_reply_without_ack(
    store: Store,
) -> None:
    """Regression for codex P0 `review-mpf7lig2-p6fxyi`.

    Worker did not echo the bridge-ack marker. The broker MUST refuse to
    accept the reply as a turn completion: it appends an `error` event to
    the log, marks the outbox row `FAILED`, and raises so the planning loop
    surfaces the failure.
    """
    silent_worker = FakeWorkerDriver(
        worker_id=TurnHolder.WORKER_B,
        session_id="sess_silent",
        echo_ack=False,
    )
    broker = Broker(
        store=store,
        worker_a=FakeWorkerDriver(worker_id=TurnHolder.WORKER_A, session_id="sess_a"),
        worker_b=silent_worker,
        config=BrokerConfig(max_planning_rounds=2),
    )
    cid = await broker.dispatch_goal("x")
    with pytest.raises(RuntimeError, match="missing"):
        await broker.deliver_turn_to(
            cid, TurnHolder.WORKER_B, "Reply with the letter A.",
            phase=Phase.PLANNING, round_=1,
        )
    # Error event was appended before the raise.
    events = await store.list_events(cid)
    error_events = [e for e in events if e.kind is EventKind.ERROR]
    assert len(error_events) == 1
    assert "missing" in error_events[0].content.lower()
    # Round-6 fix: failure path now atomically raises a worker_loss approval
    # so recovery doesn't strand the conversation.
    assert await store.pending_approval_for(cid) is not None


async def test_deliver_turn_quarantines_reply_with_stale_ack(
    store: Store,
) -> None:
    """Regression for codex P0 `review-mpf7lig2-p6fxyi`.

    Worker echoed a stale/stray idempotency key (not the current pending
    delivery's). The broker MUST reject the reply.
    """
    stray_worker = FakeWorkerDriver(
        worker_id=TurnHolder.WORKER_B,
        session_id="sess_stray",
        echo_ack=True,
        ack_override=("ik_stale_xxxxxxxxxxxxx", 999),
    )
    broker = Broker(
        store=store,
        worker_a=FakeWorkerDriver(worker_id=TurnHolder.WORKER_A, session_id="sess_a"),
        worker_b=stray_worker,
        config=BrokerConfig(max_planning_rounds=2),
    )
    cid = await broker.dispatch_goal("x")
    with pytest.raises(RuntimeError, match=r"mismatch|stale"):
        await broker.deliver_turn_to(
            cid, TurnHolder.WORKER_B, "Reply with the letter A.",
            phase=Phase.PLANNING, round_=1,
        )
    events = await store.list_events(cid)
    error_events = [e for e in events if e.kind is EventKind.ERROR]
    assert len(error_events) == 1
    body = error_events[0].content.lower()
    assert "mismatch" in body or "stale" in body
    # Round-6 fix: failure path raises an atomic worker_loss approval.
    assert await store.pending_approval_for(cid) is not None


async def test_deliver_turn_timeout_atomically_raises_worker_loss(
    store: Store,
) -> None:
    """Round-6 codex P0 `review-mpfa8dcn-c2jnpw`: the timeout path used to
    commit outbox→FAILED and the worker_loss approval separately. Now
    fail_turn_atomic bundles them; a stuck worker produces a single atomic
    failure state visible to recovery.
    """
    import asyncio

    class HangingDriver(WorkerDriver):
        def __init__(self, worker_id: TurnHolder) -> None:
            self.worker_id = worker_id

        async def send(self, msg: WorkerSend) -> WorkerReply:
            del msg  # intentionally unused
            await asyncio.sleep(10)
            raise AssertionError("should have timed out")

    broker = Broker(
        store=store,
        worker_a=FakeWorkerDriver(worker_id=TurnHolder.WORKER_A, session_id="sess_a"),
        worker_b=HangingDriver(TurnHolder.WORKER_B),
        config=BrokerConfig(max_planning_rounds=2, turn_timeout_seconds=0.05),
    )
    cid = await broker.dispatch_goal("x")
    with pytest.raises(TimeoutError):
        await broker.deliver_turn_to(
            cid, TurnHolder.WORKER_B, "Reply with the letter A.",
            phase=Phase.PLANNING, round_=1,
        )
    # Worker_loss approval was raised atomically with the FAILED outbox.
    assert await store.pending_approval_for(cid) is not None
    events = await store.list_events(cid)
    error_events = [e for e in events if e.kind is EventKind.ERROR]
    assert len(error_events) == 1
    assert "timed out" in error_events[0].content.lower()


async def test_deliver_turn_persists_session_id_on_first_dispatch(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("x")
    await broker.deliver_turn_to(
        cid, TurnHolder.WORKER_A, "prompt",
        phase=Phase.PLANNING, round_=1,
    )
    sess = await store.get_session(cid, TurnHolder.WORKER_A)
    assert sess is not None
    sid, state, permission_mode = sess
    assert sid == "sess_a"
    assert state == "live"
    assert "claude" in permission_mode


async def test_deliver_turn_to_non_worker_recipient_raises(broker: Broker) -> None:
    cid = await broker.dispatch_goal("x")
    with pytest.raises(ValueError, match="must be a worker"):
        await broker.deliver_turn_to(
            cid, TurnHolder.BROKER, "x", phase=Phase.PLANNING, round_=1
        )


# ---------- planning loop -----------------------------------------------------


async def test_planning_loop_reaches_consensus_within_max_rounds(
    store: Store,
) -> None:
    # Both workers signal agreement immediately by including AGREED markers
    # in their replies — but the broker's stop-rule uses metadata, not text.
    # We arrange that the reply event's content carries the right shape, then
    # rely on planning_should_force_consensus to force the phase change
    # at max_rounds.
    broker = Broker(
        store=store,
        worker_a=FakeWorkerDriver(worker_id=TurnHolder.WORKER_A, session_id="sess_a"),
        worker_b=FakeWorkerDriver(worker_id=TurnHolder.WORKER_B, session_id="sess_b"),
        config=BrokerConfig(max_planning_rounds=2),
    )
    cid = await broker.dispatch_goal("x")
    await broker.run_planning_loop(
        cid,
        first_prompt_for_codex="Propose a plan.",
        first_prompt_for_claude_template="Critique: {codex_proposal}",
    )
    conv = await store.get_conversation(cid)
    assert conv is not None and conv.phase is Phase.CONSENSUS


# ---------- approval surface --------------------------------------------------


async def test_approve_records_decision(broker: Broker, store: Store) -> None:
    cid = await broker.dispatch_goal("x")
    appr_id = await store.create_approval(
        conversation_id=cid,
        category="consensus_to_implement",
        payload={"plan": "ok"},
    )
    await broker.approve(appr_id, decision="approved", decided_by="user")
    assert await store.pending_approval_for(cid) is None


async def test_approve_consensus_to_implement_transitions_to_implementing(
    broker: Broker, store: Store
) -> None:
    """Regression for round-4 codex P0 `review-mpfa3kk6-...`.

    Approving a `consensus_to_implement` row must actually flip the
    conversation phase to `implementing` — not just decide the approval row.
    """
    cid = await broker.dispatch_goal("x")
    # Conversation starts in PLANNING; nudge to CONSENSUS to be ready for the
    # next legal transition.
    await store.set_phase(cid, Phase.CONSENSUS)
    appr_id = await store.create_approval(
        conversation_id=cid,
        category="consensus_to_implement",
        payload={"plan": "ok"},
    )
    await broker.approve(appr_id, decision="approved", decided_by="user")
    conv = await store.get_conversation(cid)
    assert conv is not None and conv.phase is Phase.IMPLEMENTING
    # And an audit event was appended.
    events = await store.list_events(cid)
    assert any(e.kind is EventKind.APPROVAL_DECISION for e in events)


async def test_approve_consensus_rejected_marks_aborted(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("x")
    await store.set_phase(cid, Phase.CONSENSUS)
    appr_id = await store.create_approval(
        conversation_id=cid,
        category="consensus_to_implement",
        payload={"plan": "ok"},
    )
    await broker.approve(appr_id, decision="rejected", decided_by="user")
    conv = await store.get_conversation(cid)
    assert conv is not None
    assert conv.phase is Phase.ABORTED
    assert conv.status == "aborted"


async def test_approve_worker_loss_aborts_conversation(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("x")
    appr_id = await store.create_approval(
        conversation_id=cid,
        category="worker_loss",
        payload={"reason": "lost"},
    )
    await broker.approve(appr_id, decision="approved", decided_by="user")
    conv = await store.get_conversation(cid)
    assert conv is not None
    assert conv.phase is Phase.ABORTED
    assert conv.status == "aborted"


async def test_approve_double_decide_raises(broker: Broker, store: Store) -> None:
    cid = await broker.dispatch_goal("x")
    await store.set_phase(cid, Phase.CONSENSUS)
    appr_id = await store.create_approval(
        conversation_id=cid,
        category="consensus_to_implement",
        payload={"plan": "ok"},
    )
    await broker.approve(appr_id, decision="approved", decided_by="user")
    with pytest.raises(ValueError, match="already decided"):
        await broker.approve(appr_id, decision="approved", decided_by="user")


async def test_approve_unknown_approval_raises(broker: Broker) -> None:
    with pytest.raises(KeyError):
        await broker.approve("appr_does_not_exist", decision="approved", decided_by="u")


async def test_approve_rejects_invalid_decision_string(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("x")
    appr_id = await store.create_approval(
        conversation_id=cid,
        category="consensus_to_implement",
        payload={"plan": "ok"},
    )
    with pytest.raises(ValueError, match=r"approved.*rejected"):
        await broker.approve(appr_id, decision="maybe", decided_by="u")


async def test_cancel_marks_aborted_and_appends_event(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("x")
    await broker.cancel(cid)
    conv = await store.get_conversation(cid)
    assert conv is not None and conv.status == "aborted"
    events = await store.list_events(cid)
    assert any("cancel" in e.content for e in events)


# ---------- permission callback factory --------------------------------------


async def test_permission_callback_denies_by_default(broker: Broker) -> None:
    cid = await broker.dispatch_goal("x")
    cb = broker.build_permission_callback(cid)
    decision = await cb(worker="worker_a", tool_name="Bash", tool_input={"cmd": "ls"})
    assert decision.decision.value == "deny"
    assert decision.reason is not None
    assert "allowlist" in decision.reason


async def test_permission_callback_allows_allowlisted_tool(broker: Broker) -> None:
    cid = await broker.dispatch_goal("x")
    cb = broker.build_permission_callback(cid, allowlist=("Read",))
    d_allow = await cb(worker="worker_a", tool_name="Read", tool_input={"path": "x"})
    assert d_allow.decision.value == "allow"


async def test_permission_callback_denylist_overrides_allowlist(broker: Broker) -> None:
    cid = await broker.dispatch_goal("x")
    cb = broker.build_permission_callback(
        cid, allowlist=("Bash",), denylist=("rm -rf",)
    )
    d_deny = await cb(
        worker="worker_a",
        tool_name="Bash",
        tool_input={"command": "rm -rf /"},
    )
    assert d_deny.decision.value == "deny"
    assert d_deny.reason is not None
    assert "denylist" in d_deny.reason


# ---------- RealBrokerBackend (MCP wiring) -----------------------------------


async def test_real_backend_round_trips_dispatch_status_tail(
    broker: Broker, store: Store
) -> None:
    backend = RealBrokerBackend(broker)
    dispatch = await backend.dispatch_goal("hello")
    assert dispatch.conversation_id.startswith("conv_")
    assert dispatch.stub is False

    # Drive one turn via the broker so there are events to tail.
    await broker.deliver_turn_to(
        dispatch.conversation_id,
        TurnHolder.WORKER_B,
        "Say A.",
        phase=Phase.PLANNING,
        round_=1,
    )

    status = await backend.get_status(dispatch.conversation_id)
    assert status.phase is Phase.PLANNING
    assert status.last_seq >= 3

    transcript = await backend.tail_transcript(
        dispatch.conversation_id, after_event_id=None
    )
    assert transcript.stub is False
    assert len(transcript.events) >= 3


async def test_real_backend_cancel_returns_canceled_true(
    broker: Broker, store: Store
) -> None:
    backend = RealBrokerBackend(broker)
    cid = await broker.dispatch_goal("x")
    res = await backend.cancel(cid)
    assert res.canceled is True
    assert res.stub is False


# ---------- recovery (§5.2) ---------------------------------------------------


async def test_recover_on_startup_escalates_delivered_unanswered(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("x")
    # Manually inject a delivered-but-unanswered outbox row to simulate a crash.
    seq = await store.next_seq(cid)
    from agent_bridge.broker import _content_hash, _new_event_id  # internal helpers
    from agent_bridge.events import Event, EventMetadata, OutboxRow, utcnow

    ev = Event(
        event_id=_new_event_id(),
        conversation_id=cid,
        seq=seq,
        round=1,
        phase=Phase.PLANNING,
        sender=TurnHolder.BROKER,
        recipient=TurnHolder.WORKER_A,
        kind=EventKind.PROPOSAL,
        content="injected",
        content_hash=_content_hash("injected"),
        requires_reply=True,
        metadata=EventMetadata(),
        created_at=utcnow(),
    )
    await store.append_event(ev)
    await store.enqueue_outbox(
        OutboxRow(
            delivery_id="dlv_crash_1",
            conversation_id=cid,
            event_id=ev.event_id,
            recipient=TurnHolder.WORKER_A,
            idempotency_key="ik_crash_1",
            status=OutboxStatus.DELIVERED,
            enqueued_at=utcnow(),
            sent_at=utcnow(),
            delivered_at=utcnow(),
        )
    )
    recovered = await broker.recover_on_startup()
    assert cid in recovered
    # A worker_loss approval was raised.
    assert await store.pending_approval_for(cid) is not None


async def test_recover_on_startup_escalates_pending_and_sent(
    broker: Broker, store: Store
) -> None:
    """Regression for round-3 codex P0 `review-mpf9p59f-rrcc8u`.

    Recovery used to silently skip pending/sent outbox rows with `pass`,
    stranding conversations in-flight forever. Recovery must now raise a
    `worker_loss` approval for those rows too — same axis as the DELIVERED
    handling, just the third symmetric instance after enqueue/completion.
    """
    from agent_bridge.broker import _content_hash, _new_event_id
    from agent_bridge.events import Event, EventMetadata, OutboxRow, utcnow

    cid = await broker.dispatch_goal("x")
    # Inject one PENDING row and one SENT row to simulate a mid-dispatch crash
    # and a mid-send crash respectively.
    for i, status in enumerate([OutboxStatus.PENDING, OutboxStatus.SENT], start=1):
        seq = await store.next_seq(cid)
        ev = Event(
            event_id=_new_event_id(),
            conversation_id=cid,
            seq=seq,
            round=1,
            phase=Phase.PLANNING,
            sender=TurnHolder.BROKER,
            recipient=TurnHolder.WORKER_A,
            kind=EventKind.PROPOSAL,
            content=f"injected {i}",
            content_hash=_content_hash(f"injected {i}"),
            requires_reply=True,
            metadata=EventMetadata(),
            created_at=utcnow(),
        )
        await store.append_event(ev)
        await store.enqueue_outbox(
            OutboxRow(
                delivery_id=f"dlv_recov_{i}",
                conversation_id=cid,
                event_id=ev.event_id,
                recipient=TurnHolder.WORKER_A,
                idempotency_key=f"ik_recov_{i}",
                status=status,
                enqueued_at=utcnow(),
                sent_at=utcnow() if status is OutboxStatus.SENT else None,
            )
        )
    recovered = await broker.recover_on_startup()
    assert cid in recovered
    # An approval was raised for the in-flight row (one of the two — broker
    # raises one per row; pending_approval_for returns the first).
    assert await store.pending_approval_for(cid) is not None


async def test_tail_transcript_cursor_resolves_for_any_event(
    broker: Broker, store: Store
) -> None:
    """Round-8 codex P1 `review-mpfb42vk-lajla7`.

    The previous implementation only resolved `after_event_id` when it
    happened to be the very first event in the conversation. With a cursor
    pointing at the second (or later) event, the broker would replay the
    full transcript from the beginning. This test exercises a cursor at a
    middle event and asserts only later events come back.
    """
    cid = await broker.dispatch_goal("x")
    # Dispatch two turns to populate the transcript with several events.
    await broker.deliver_turn_to(
        cid, TurnHolder.WORKER_B, "p1",
        phase=Phase.PLANNING, round_=1,
    )
    await broker.deliver_turn_to(
        cid, TurnHolder.WORKER_A, "p2",
        phase=Phase.PLANNING, round_=1,
    )
    all_events = await store.list_events(cid)
    assert len(all_events) >= 4  # seed + 2 prompts + 2 replies
    # Pick a middle event as the cursor.
    cursor = all_events[len(all_events) // 2]
    tail = await broker.tail_transcript(cid, after_event_id=cursor.event_id)
    # Only events with seq strictly greater than the cursor's seq must come back.
    assert tail, "tail should not be empty"
    assert all(e.seq > cursor.seq for e in tail)
    # The cursor itself must NOT appear in the result.
    assert cursor.event_id not in {e.event_id for e in tail}


async def test_recover_on_startup_is_idempotent_across_restarts(
    broker: Broker, store: Store
) -> None:
    """Regression for round-7 codex P1 `review-mpfaq...`.

    Repeated recovery passes must not create duplicate worker_loss approvals
    for the same delivery_id.
    """
    from agent_bridge.broker import _content_hash, _new_event_id
    from agent_bridge.events import Event, EventMetadata, OutboxRow, utcnow

    cid = await broker.dispatch_goal("x")
    seq = await store.next_seq(cid)
    ev = Event(
        event_id=_new_event_id(),
        conversation_id=cid,
        seq=seq,
        round=1,
        phase=Phase.PLANNING,
        sender=TurnHolder.BROKER,
        recipient=TurnHolder.WORKER_A,
        kind=EventKind.PROPOSAL,
        content="injected",
        content_hash=_content_hash("injected"),
        requires_reply=True,
        metadata=EventMetadata(),
        created_at=utcnow(),
    )
    await store.append_event(ev)
    await store.enqueue_outbox(
        OutboxRow(
            delivery_id="dlv_idem_1",
            conversation_id=cid,
            event_id=ev.event_id,
            recipient=TurnHolder.WORKER_A,
            idempotency_key="ik_idem_1",
            status=OutboxStatus.DELIVERED,
            enqueued_at=utcnow(),
            sent_at=utcnow(),
            delivered_at=utcnow(),
        )
    )
    # Three restarts. Only ONE approval should exist for this delivery.
    for _ in range(3):
        await broker.recover_on_startup()

    async with store._conn.execute(
        """SELECT COUNT(*) FROM approvals
             WHERE conversation_id=? AND category='worker_loss'
               AND decision IS NULL
               AND payload_json LIKE ?""",
        (cid, '%"delivery_id": "dlv_idem_1"%'),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert int(row[0]) == 1, f"expected 1 worker_loss approval, got {row[0]}"
