"""Unit tests for the SQLite-backed store."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_bridge.events import (
    Event,
    EventKind,
    OutboxRow,
    OutboxStatus,
    Phase,
    TurnHolder,
    utcnow,
)
from agent_bridge.store import Store


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[Store, None]:
    s = await Store.open(tmp_path / "bridge.sqlite")
    yield s
    await s.close()


# ---------- Conversation lifecycle --------------------------------------------


async def test_create_and_get_conversation(store: Store) -> None:
    conv = await store.create_conversation(goal="add a util")
    again = await store.get_conversation(conv.conversation_id)
    assert again is not None
    assert again.goal == "add a util"
    assert again.phase is Phase.PLANNING
    assert again.status == "running"
    assert again.current_turn is TurnHolder.BROKER
    assert again.turn_token_version == 0
    assert again.next_seq == 1


async def test_get_missing_conversation_returns_none(store: Store) -> None:
    assert await store.get_conversation("conv_does_not_exist") is None


async def test_set_phase_persists(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    await store.set_phase(conv.conversation_id, Phase.CONSENSUS)
    again = await store.get_conversation(conv.conversation_id)
    assert again is not None and again.phase is Phase.CONSENSUS


async def test_set_turn_bumps_version_and_records_pending_delivery(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    v1 = await store.set_turn(
        conv.conversation_id, turn=TurnHolder.WORKER_A, pending_delivery_id="dlv_42"
    )
    assert v1 == 1
    v2 = await store.set_turn(
        conv.conversation_id, turn=TurnHolder.WORKER_B, pending_delivery_id=None
    )
    assert v2 == 2
    again = await store.get_conversation(conv.conversation_id)
    assert again is not None and again.current_turn is TurnHolder.WORKER_B
    assert again.pending_delivery_id is None


async def test_increment_round(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    r1 = await store.increment_round(conv.conversation_id)
    r2 = await store.increment_round(conv.conversation_id)
    assert (r1, r2) == (1, 2)


async def test_list_active_conversations_filters_status(store: Store) -> None:
    a = await store.create_conversation(goal="a")
    b = await store.create_conversation(goal="b")
    await store.set_status(b.conversation_id, "done")
    active = await store.list_active_conversations()
    assert {c.conversation_id for c in active} == {a.conversation_id}


# ---------- Events + append-only triggers -------------------------------------


def _evt(conversation_id: str, seq: int) -> Event:
    body = f"event {seq}"
    return Event(
        event_id=f"evt_{seq:04d}",
        conversation_id=conversation_id,
        seq=seq,
        round=0,
        phase=Phase.PLANNING,
        sender=TurnHolder.WORKER_B,
        recipient=TurnHolder.BROKER,
        kind=EventKind.PROPOSAL,
        content=body,
        content_hash=_hash(body),
        requires_reply=True,
        created_at=utcnow(),
    )


async def test_append_event_and_list_events(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    seq1 = await store.next_seq(conv.conversation_id)
    seq2 = await store.next_seq(conv.conversation_id)
    e1 = _evt(conv.conversation_id, seq1)
    e2 = _evt(conv.conversation_id, seq2)
    await store.append_event(e1)
    await store.append_event(e2)
    fetched = await store.list_events(conv.conversation_id)
    assert [e.seq for e in fetched] == [seq1, seq2]
    assert fetched[0].content == "event 1"


async def test_event_log_is_append_only_update_aborted(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    seq = await store.next_seq(conv.conversation_id)
    await store.append_event(_evt(conv.conversation_id, seq))
    # Direct UPDATE must be rejected by the trigger (invariant I-3).
    import aiosqlite

    with pytest.raises(aiosqlite.IntegrityError):
        await store._conn.execute(
            "UPDATE events SET content='tampered' WHERE event_id='evt_0001'"
        )
        await store._conn.commit()


async def test_event_log_is_append_only_delete_aborted(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    seq = await store.next_seq(conv.conversation_id)
    await store.append_event(_evt(conv.conversation_id, seq))
    import aiosqlite

    with pytest.raises(aiosqlite.IntegrityError):
        await store._conn.execute(
            "DELETE FROM events WHERE event_id='evt_0001'"
        )
        await store._conn.commit()


async def test_list_events_after_seq_filter(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    for _ in range(3):
        s = await store.next_seq(conv.conversation_id)
        await store.append_event(_evt(conv.conversation_id, s))
    later = await store.list_events(conv.conversation_id, after_seq=1)
    assert [e.seq for e in later] == [2, 3]


# ---------- Outbox -------------------------------------------------------------


def _outbox_row(
    conversation_id: str, event_id: str, key: str, status: OutboxStatus
) -> OutboxRow:
    return OutboxRow(
        delivery_id=f"dlv_{key}",
        conversation_id=conversation_id,
        event_id=event_id,
        recipient=TurnHolder.WORKER_A,
        idempotency_key=key,
        status=status,
        enqueued_at=datetime.now(UTC),
    )


async def test_dispatch_turn_atomically_writes_event_outbox_and_turn(store: Store) -> None:
    """§3.5 atomicity invariant — all three writes commit together."""
    conv = await store.create_conversation(goal="x")
    seq = await store.next_seq(conv.conversation_id)
    e = _evt(conv.conversation_id, seq)
    row = _outbox_row(conv.conversation_id, e.event_id, "ik_disp_1", OutboxStatus.PENDING)
    v = await store.dispatch_turn(e, row, turn_target=TurnHolder.WORKER_A)
    # Conversation turn token bumped from 0 → 1.
    assert v == 1
    conv_after = await store.get_conversation(conv.conversation_id)
    assert conv_after is not None
    assert conv_after.current_turn is TurnHolder.WORKER_A
    assert conv_after.turn_token_version == 1
    assert conv_after.pending_delivery_id == row.delivery_id
    # Event row present.
    events = await store.list_events(conv.conversation_id)
    assert [ev.event_id for ev in events] == [e.event_id]
    # Outbox row present and pending.
    fetched = await store.get_outbox(row.delivery_id)
    assert fetched is not None and fetched.status is OutboxStatus.PENDING


async def test_dispatch_turn_rolls_back_all_writes_on_failure(store: Store) -> None:
    """If any INSERT fails mid-transaction, neither event nor outbox lands.

    Regression test for codex P0 finding `review-mpf7lig2-p6fxyi` — the prior
    three-call sequence committed each write independently, so a crash between
    them could leave a durable event with no outbox row. dispatch_turn must
    roll back atomically.
    """
    import aiosqlite

    conv = await store.create_conversation(goal="x")
    # Pre-insert an outbox row that will conflict on idempotency_key.
    seq1 = await store.next_seq(conv.conversation_id)
    pre_event = _evt(conv.conversation_id, seq1)
    await store.append_event(pre_event)
    pre_row = _outbox_row(
        conv.conversation_id, pre_event.event_id, "dup_key", OutboxStatus.PENDING
    )
    await store.enqueue_outbox(pre_row)

    # Now try to dispatch_turn with the same idempotency_key. The outbox INSERT
    # will hit the UNIQUE constraint; the event INSERT that came before it must
    # be rolled back.
    seq2 = await store.next_seq(conv.conversation_id)
    failing_event = _evt(conv.conversation_id, seq2)
    failing_row = _outbox_row(
        conv.conversation_id,
        failing_event.event_id,
        "dup_key",  # same key — triggers UNIQUE violation
        OutboxStatus.PENDING,
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await store.dispatch_turn(
            failing_event, failing_row, turn_target=TurnHolder.WORKER_A
        )
    # The new event must NOT be in the events table.
    events = await store.list_events(conv.conversation_id)
    assert failing_event.event_id not in {e.event_id for e in events}
    # Turn token must NOT have advanced from the pre-existing state.
    conv_after = await store.get_conversation(conv.conversation_id)
    assert conv_after is not None
    assert conv_after.turn_token_version == 0  # never bumped


async def test_complete_turn_atomically_writes_reply_outbox_and_clears_turn(
    store: Store,
) -> None:
    """Symmetric counterpart to dispatch_turn atomicity.

    Regression test for codex P0 `review-mpf8nsmd-w82pki` — the reply
    completion used to split into three durable writes (append event, update
    outbox to ANSWERED, set_turn to broker), reopening the same crash-window
    class fixed by dispatch_turn on the enqueue side.
    """
    from agent_bridge.outbox import advance_outbox

    conv = await store.create_conversation(goal="x")
    # Dispatch a turn first so there's an outbox row to complete.
    seq1 = await store.next_seq(conv.conversation_id)
    prompt_event = _evt(conv.conversation_id, seq1)
    pending = _outbox_row(
        conv.conversation_id, prompt_event.event_id, "ik_complete_1", OutboxStatus.PENDING
    )
    await store.dispatch_turn(prompt_event, pending, turn_target=TurnHolder.WORKER_A)
    # Advance pending → sent (the broker does this between dispatch and reply).
    sent = advance_outbox(pending, to_status=OutboxStatus.SENT, now=utcnow())
    await store.update_outbox(sent)
    # Build the reply event and the ANSWERED outbox state.
    seq2 = await store.next_seq(conv.conversation_id)
    reply_event = Event(
        event_id="evt_reply",
        conversation_id=conv.conversation_id,
        seq=seq2,
        round=0,
        phase=Phase.PLANNING,
        sender=TurnHolder.WORKER_A,
        recipient=TurnHolder.BROKER,
        kind=EventKind.CRITIQUE,
        content="here is my reply",
        content_hash=_hash("here is my reply"),
        requires_reply=False,
        in_reply_to_idempotency_key="ik_complete_1",
        observed_turn_token_version=1,
        created_at=utcnow(),
    )
    answered = advance_outbox(
        sent,
        to_status=OutboxStatus.ANSWERED,
        now=utcnow(),
        reply_event_id=reply_event.event_id,
    )
    await store.complete_turn(reply_event, answered, next_turn=TurnHolder.BROKER)
    # All three writes landed atomically.
    events = await store.list_events(conv.conversation_id)
    assert reply_event.event_id in {e.event_id for e in events}
    outbox_after = await store.get_outbox(pending.delivery_id)
    assert outbox_after is not None
    assert outbox_after.status is OutboxStatus.ANSWERED
    assert outbox_after.reply_event_id == reply_event.event_id
    conv_after = await store.get_conversation(conv.conversation_id)
    assert conv_after is not None
    assert conv_after.current_turn is TurnHolder.BROKER
    assert conv_after.pending_delivery_id is None


async def test_dispatch_turn_rollback_does_not_consume_a_seq(store: Store) -> None:
    """Regression for round-3 codex P1 — seq must roll back with the txn.

    Previously next_seq committed its UPDATE before dispatch_turn ran, so a
    failed dispatch_turn left a durable seq gap. Now seq is allocated inside
    the dispatch_turn transaction; a rollback restores the counter.
    """
    import aiosqlite

    conv = await store.create_conversation(goal="x")
    # Pre-existing row with idempotency_key=ik_seq_dup; same trick as
    # test_dispatch_turn_rolls_back_all_writes_on_failure.
    seq1 = await store.next_seq(conv.conversation_id)
    pre_event = _evt(conv.conversation_id, seq1)
    await store.append_event(pre_event)
    pre_row = _outbox_row(
        conv.conversation_id, pre_event.event_id, "ik_seq_dup", OutboxStatus.PENDING
    )
    await store.enqueue_outbox(pre_row)

    # Snapshot the counter — it currently sits at seq1+1 (=2) waiting for the
    # next allocation.
    async with store._conn.execute(
        "SELECT next_seq FROM conversations WHERE conversation_id=?",
        (conv.conversation_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    counter_before = int(row[0])

    # Provoke a UNIQUE-violation rollback inside dispatch_turn.
    failing_event = _evt(conv.conversation_id, 999)  # placeholder seq, ignored
    failing_outbox = _outbox_row(
        conv.conversation_id, failing_event.event_id, "ik_seq_dup", OutboxStatus.PENDING
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await store.dispatch_turn(
            failing_event, failing_outbox, turn_target=TurnHolder.WORKER_A
        )

    # The counter must NOT have advanced — the rollback returned it to its
    # pre-call value.
    async with store._conn.execute(
        "SELECT next_seq FROM conversations WHERE conversation_id=?",
        (conv.conversation_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    counter_after = int(row[0])
    assert counter_after == counter_before, (
        f"next_seq leaked: before={counter_before} after={counter_after}"
    )


async def test_complete_turn_rolls_back_on_failure(store: Store) -> None:
    """If any write inside complete_turn fails, none of them land."""
    import aiosqlite

    from agent_bridge.outbox import advance_outbox

    conv = await store.create_conversation(goal="x")
    seq1 = await store.next_seq(conv.conversation_id)
    prompt_event = _evt(conv.conversation_id, seq1)
    pending = _outbox_row(
        conv.conversation_id, prompt_event.event_id, "ik_complete_2", OutboxStatus.PENDING
    )
    await store.dispatch_turn(prompt_event, pending, turn_target=TurnHolder.WORKER_A)
    sent = advance_outbox(pending, to_status=OutboxStatus.SENT, now=utcnow())
    await store.update_outbox(sent)
    # Pre-insert a reply event so the next event INSERT triggers a PK violation.
    seq2 = await store.next_seq(conv.conversation_id)
    pre_reply = Event(
        event_id="evt_reply_pre",
        conversation_id=conv.conversation_id,
        seq=seq2,
        round=0,
        phase=Phase.PLANNING,
        sender=TurnHolder.WORKER_A,
        recipient=TurnHolder.BROKER,
        kind=EventKind.CRITIQUE,
        content="prior",
        content_hash=_hash("prior"),
        requires_reply=False,
        created_at=utcnow(),
    )
    await store.append_event(pre_reply)
    # Construct a reply event with the SAME event_id → PRIMARY KEY violation.
    failing_reply = Event(
        event_id="evt_reply_pre",
        conversation_id=conv.conversation_id,
        seq=seq2 + 1,
        round=0,
        phase=Phase.PLANNING,
        sender=TurnHolder.WORKER_A,
        recipient=TurnHolder.BROKER,
        kind=EventKind.CRITIQUE,
        content="duplicate-id reply",
        content_hash=_hash("duplicate-id reply"),
        requires_reply=False,
        in_reply_to_idempotency_key="ik_complete_2",
        observed_turn_token_version=1,
        created_at=utcnow(),
    )
    answered = advance_outbox(
        sent,
        to_status=OutboxStatus.ANSWERED,
        now=utcnow(),
        reply_event_id=failing_reply.event_id,
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await store.complete_turn(failing_reply, answered, next_turn=TurnHolder.BROKER)
    # Outbox must still be in SENT (advanced before the failed complete_turn).
    outbox_after = await store.get_outbox(pending.delivery_id)
    assert outbox_after is not None
    assert outbox_after.status is OutboxStatus.SENT
    # Conversation turn must still point at WORKER_A with the pending delivery_id.
    conv_after = await store.get_conversation(conv.conversation_id)
    assert conv_after is not None
    assert conv_after.current_turn is TurnHolder.WORKER_A
    assert conv_after.pending_delivery_id == pending.delivery_id


async def test_enqueue_and_get_outbox(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    seq = await store.next_seq(conv.conversation_id)
    e = _evt(conv.conversation_id, seq)
    await store.append_event(e)
    row = _outbox_row(conv.conversation_id, e.event_id, "ik_1", OutboxStatus.PENDING)
    await store.enqueue_outbox(row)
    fetched = await store.get_outbox(row.delivery_id)
    assert fetched is not None
    assert fetched.status is OutboxStatus.PENDING
    assert fetched.idempotency_key == "ik_1"


async def test_update_outbox_transitions(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    seq = await store.next_seq(conv.conversation_id)
    e = _evt(conv.conversation_id, seq)
    await store.append_event(e)
    row = _outbox_row(conv.conversation_id, e.event_id, "ik_2", OutboxStatus.PENDING)
    await store.enqueue_outbox(row)
    updated = row.model_copy(
        update={"status": OutboxStatus.SENT, "sent_at": datetime.now(UTC)}
    )
    await store.update_outbox(updated)
    fetched = await store.get_outbox(row.delivery_id)
    assert fetched is not None
    assert fetched.status is OutboxStatus.SENT
    assert fetched.sent_at is not None


async def test_list_outbox_needing_recovery_picks_pending_sent_delivered(
    store: Store,
) -> None:
    conv = await store.create_conversation(goal="x")
    for key, status in [
        ("k1", OutboxStatus.PENDING),
        ("k2", OutboxStatus.SENT),
        ("k3", OutboxStatus.DELIVERED),
        ("k4", OutboxStatus.ANSWERED),
        ("k5", OutboxStatus.FAILED),
    ]:
        seq = await store.next_seq(conv.conversation_id)
        e = _evt(conv.conversation_id, seq)
        await store.append_event(e)
        await store.enqueue_outbox(_outbox_row(conv.conversation_id, e.event_id, key, status))
    pending = await store.list_outbox_needing_recovery(conv.conversation_id)
    keys = {r.idempotency_key for r in pending}
    assert keys == {"k1", "k2", "k3"}


# ---------- Approvals ---------------------------------------------------------


async def test_decide_approval_atomic_rolls_back_on_failure(store: Store) -> None:
    """Round-5 codex P0 `review-mpf9wsgb-imyv0u`: approval write + event +
    transition must commit together or not at all.

    Induce a UNIQUE-violation on the audit event INSERT (by pre-inserting an
    event with the same event_id) and verify that the approval row is NOT
    flipped to decided.
    """
    import aiosqlite

    conv = await store.create_conversation(goal="x")
    aid = await store.create_approval(
        conversation_id=conv.conversation_id,
        category="consensus_to_implement",
        payload={"plan": "ok"},
    )
    # Pre-insert an event whose ID we will reuse below to trigger PK violation
    # inside decide_approval_atomic.
    pre_seq = await store.next_seq(conv.conversation_id)
    pre_event = Event(
        event_id="evt_collide_approval",
        conversation_id=conv.conversation_id,
        seq=pre_seq,
        round=0,
        phase=Phase.PLANNING,
        sender=TurnHolder.USER,
        recipient=TurnHolder.BROKER,
        kind=EventKind.SYSTEM_NOTE,
        content="x",
        content_hash=_hash("x"),
        requires_reply=False,
        created_at=utcnow(),
    )
    await store.append_event(pre_event)
    # Use the same event_id for the audit event we're trying to write
    # atomically; the INSERT should fail with PK violation.
    failing_audit = pre_event.model_copy(update={"content": "collision"})
    with pytest.raises(aiosqlite.IntegrityError):
        await store.decide_approval_atomic(
            aid,
            decision="approved",
            decided_by="user",
            decision_event=failing_audit,
            phase=Phase.IMPLEMENTING,
        )
    # Approval must still be pending — the decision write was rolled back.
    assert await store.pending_approval_for(conv.conversation_id) == aid
    # Conversation phase must be unchanged.
    conv_after = await store.get_conversation(conv.conversation_id)
    assert conv_after is not None and conv_after.phase is Phase.PLANNING


async def test_create_and_decide_approval(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    aid = await store.create_approval(
        conversation_id=conv.conversation_id,
        category="consensus_to_implement",
        payload={"plan": "do thing"},
    )
    pending = await store.pending_approval_for(conv.conversation_id)
    assert pending == aid
    await store.decide_approval(aid, decision="approved", decided_by="user")
    assert await store.pending_approval_for(conv.conversation_id) is None


# ---------- Sessions ---------------------------------------------------------


async def test_upsert_and_get_session(store: Store) -> None:
    conv = await store.create_conversation(goal="x")
    await store.upsert_session(
        conversation_id=conv.conversation_id,
        worker=TurnHolder.WORKER_A,
        session_id="sess_abc",
        state="live",
        permission_mode="claude.default+hook",
    )
    got = await store.get_session(conv.conversation_id, TurnHolder.WORKER_A)
    assert got == ("sess_abc", "live", "claude.default+hook")
    # Update via upsert
    await store.upsert_session(
        conversation_id=conv.conversation_id,
        worker=TurnHolder.WORKER_A,
        session_id="sess_abc",
        state="dead",
        permission_mode="claude.default+hook",
    )
    got2 = await store.get_session(conv.conversation_id, TurnHolder.WORKER_A)
    assert got2 is not None and got2[1] == "dead"
