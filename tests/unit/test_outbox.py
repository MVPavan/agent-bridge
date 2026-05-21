"""Unit tests for the outbox state machine and reply-correlation validator."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_bridge.events import OutboxRow, OutboxStatus, Worker
from agent_bridge.outbox import (
    ALLOWED_OUTBOX_TRANSITIONS,
    TERMINAL_OUTBOX_STATUSES,
    IllegalOutboxTransition,
    RecoveryAction,
    ReplyValidation,
    advance_outbox,
    assert_outbox_transition,
    can_outbox_transition,
    is_terminal_outbox,
    recovery_action_for,
    rows_needing_recovery,
    validate_worker_reply,
)


def _row(status: OutboxStatus = OutboxStatus.PENDING, key: str = "ik_test") -> OutboxRow:
    return OutboxRow(
        delivery_id=f"dlv_{key}",
        conversation_id="conv_0001",
        event_id="evt_0001",
        recipient=Worker.WORKER_A,
        idempotency_key=key,
        status=status,
        enqueued_at=datetime.now(UTC),
    )


# ---------- Transition whitelist tests ----------------------------------------


_SORTED_OUTBOX_TRANSITIONS: list[tuple[OutboxStatus, OutboxStatus]] = sorted(
    ALLOWED_OUTBOX_TRANSITIONS, key=lambda p: (p[0].value, p[1].value)
)


@pytest.mark.parametrize("frm,to", _SORTED_OUTBOX_TRANSITIONS)
def test_allowed_outbox_transitions(frm: OutboxStatus, to: OutboxStatus) -> None:
    assert can_outbox_transition(frm, to)
    assert_outbox_transition(frm, to)


def test_disallowed_outbox_transition_raises() -> None:
    # pending -> answered is not a legal direct hop (must go through sent at least)
    assert not can_outbox_transition(OutboxStatus.PENDING, OutboxStatus.ANSWERED)
    with pytest.raises(IllegalOutboxTransition):
        assert_outbox_transition(OutboxStatus.PENDING, OutboxStatus.ANSWERED)


def test_terminal_outbox_statuses_have_no_outbound_transitions() -> None:
    for term in TERMINAL_OUTBOX_STATUSES:
        for other in OutboxStatus:
            assert not can_outbox_transition(term, other)


def test_is_terminal_outbox() -> None:
    assert is_terminal_outbox(OutboxStatus.ANSWERED)
    assert is_terminal_outbox(OutboxStatus.ABANDONED)
    assert not is_terminal_outbox(OutboxStatus.FAILED)  # may still ABANDONE


# ---------- advance_outbox tests ----------------------------------------------


def test_advance_outbox_pending_to_sent_sets_sent_at() -> None:
    r = _row()
    now = datetime.now(UTC)
    r2 = advance_outbox(r, to_status=OutboxStatus.SENT, now=now, attempt_count_delta=1)
    assert r2.status is OutboxStatus.SENT
    assert r2.sent_at == now
    assert r2.attempt_count == 1
    # original row is untouched (frozen model)
    assert r.status is OutboxStatus.PENDING


def test_advance_outbox_to_answered_requires_reply_event_id() -> None:
    r = _row(status=OutboxStatus.DELIVERED)
    with pytest.raises(ValueError, match="reply_event_id"):
        advance_outbox(r, to_status=OutboxStatus.ANSWERED, now=datetime.now(UTC))


def test_advance_outbox_to_answered_sets_reply_event_id_and_answered_at() -> None:
    r = _row(status=OutboxStatus.DELIVERED)
    now = datetime.now(UTC)
    r2 = advance_outbox(
        r, to_status=OutboxStatus.ANSWERED, now=now, reply_event_id="evt_reply_0001"
    )
    assert r2.status is OutboxStatus.ANSWERED
    assert r2.reply_event_id == "evt_reply_0001"
    assert r2.answered_at == now


def test_advance_outbox_invalid_transition_raises() -> None:
    r = _row(status=OutboxStatus.ANSWERED)
    with pytest.raises(IllegalOutboxTransition):
        advance_outbox(r, to_status=OutboxStatus.SENT, now=datetime.now(UTC))


# ---------- Recovery action tests ---------------------------------------------


def test_recovery_action_per_status() -> None:
    assert recovery_action_for(_row(OutboxStatus.PENDING)) == RecoveryAction.REDISPATCH
    assert recovery_action_for(_row(OutboxStatus.SENT)) == RecoveryAction.REDISPATCH
    assert (
        recovery_action_for(_row(OutboxStatus.DELIVERED))
        == RecoveryAction.PROBE_AND_RECONCILE
    )
    assert recovery_action_for(_row(OutboxStatus.ANSWERED)) == RecoveryAction.NO_ACTION
    assert recovery_action_for(_row(OutboxStatus.FAILED)) == RecoveryAction.NO_ACTION
    assert recovery_action_for(_row(OutboxStatus.ABANDONED)) == RecoveryAction.NO_ACTION


def test_rows_needing_recovery_filters_terminal() -> None:
    rows = [
        _row(OutboxStatus.PENDING, key="k1"),
        _row(OutboxStatus.SENT, key="k2"),
        _row(OutboxStatus.DELIVERED, key="k3"),
        _row(OutboxStatus.ANSWERED, key="k4"),
        _row(OutboxStatus.FAILED, key="k5"),
    ]
    keys = {r.idempotency_key for r in rows_needing_recovery(rows)}
    assert keys == {"k1", "k2", "k3"}


# ---------- Reply correlation tests -------------------------------------------


def test_validate_reply_accepts_matching_key_and_version() -> None:
    row = _row(status=OutboxStatus.SENT, key="ik_42")
    res = validate_worker_reply(
        pending_row=row,
        conversation_turn_token_version=7,
        reply_in_reply_to_idempotency_key="ik_42",
        reply_observed_turn_token_version=7,
    )
    assert res == ReplyValidation(accept=True)


def test_validate_reply_rejects_missing_pending_row() -> None:
    res = validate_worker_reply(
        pending_row=None,
        conversation_turn_token_version=7,
        reply_in_reply_to_idempotency_key="ik_42",
        reply_observed_turn_token_version=7,
    )
    assert not res.accept
    assert res.reason is not None
    assert "inconsistent" in res.reason


def test_validate_reply_rejects_terminal_row() -> None:
    row = _row(status=OutboxStatus.ANSWERED, key="ik_42")
    res = validate_worker_reply(
        pending_row=row,
        conversation_turn_token_version=7,
        reply_in_reply_to_idempotency_key="ik_42",
        reply_observed_turn_token_version=7,
    )
    assert not res.accept
    assert res.reason is not None
    assert "terminal" in res.reason


def test_validate_reply_rejects_missing_key() -> None:
    row = _row(status=OutboxStatus.SENT, key="ik_42")
    res = validate_worker_reply(
        pending_row=row,
        conversation_turn_token_version=7,
        reply_in_reply_to_idempotency_key=None,
        reply_observed_turn_token_version=7,
    )
    assert not res.accept
    assert res.reason is not None
    assert "missing in_reply_to_idempotency_key" in res.reason


def test_validate_reply_rejects_mismatched_key() -> None:
    row = _row(status=OutboxStatus.SENT, key="ik_42")
    res = validate_worker_reply(
        pending_row=row,
        conversation_turn_token_version=7,
        reply_in_reply_to_idempotency_key="ik_OTHER",
        reply_observed_turn_token_version=7,
    )
    assert not res.accept
    assert res.reason is not None
    assert "idempotency key mismatch" in res.reason


def test_validate_reply_rejects_stale_token_version() -> None:
    row = _row(status=OutboxStatus.SENT, key="ik_42")
    res = validate_worker_reply(
        pending_row=row,
        conversation_turn_token_version=8,
        reply_in_reply_to_idempotency_key="ik_42",
        reply_observed_turn_token_version=7,
    )
    assert not res.accept
    assert res.reason is not None
    assert "stale turn token" in res.reason
