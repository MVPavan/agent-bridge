"""Outbox state machine.

Spec: docs/architecture.md §3.5 and §5.2.

The outbox separates **receipt** (the worker acknowledged the message arrived)
from **completion** (the worker actually emitted its reply, which is durably
appended to events). Conflating those would silently lose a turn whenever a
worker acked but then died before replying.

This module is pure logic. The broker (CP6 full) will pair this FSM with
SQLite persistence; recovery (§5.2) reads the outbox table and dispatches
according to the table below.

States: `pending → sent → delivered → answered` is the happy path. Recovery
states are `failed` and `abandoned`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .events import OutboxRow, OutboxStatus

# Whitelist of allowed (from_status, to_status) transitions.
ALLOWED_OUTBOX_TRANSITIONS: frozenset[tuple[OutboxStatus, OutboxStatus]] = frozenset(
    {
        # happy path
        (OutboxStatus.PENDING, OutboxStatus.SENT),
        (OutboxStatus.SENT, OutboxStatus.DELIVERED),
        (OutboxStatus.SENT, OutboxStatus.ANSWERED),  # reply arrived without explicit ack
        (OutboxStatus.DELIVERED, OutboxStatus.ANSWERED),
        # failure paths
        (OutboxStatus.PENDING, OutboxStatus.FAILED),
        (OutboxStatus.SENT, OutboxStatus.FAILED),
        (OutboxStatus.DELIVERED, OutboxStatus.FAILED),
        # escalation paths (user worker_loss decision)
        (OutboxStatus.DELIVERED, OutboxStatus.ABANDONED),
        (OutboxStatus.FAILED, OutboxStatus.ABANDONED),
    }
)

TERMINAL_OUTBOX_STATUSES: frozenset[OutboxStatus] = frozenset(
    {OutboxStatus.ANSWERED, OutboxStatus.ABANDONED}
)


class IllegalOutboxTransition(ValueError):
    """Raised when an outbox status transition is not whitelisted."""


def can_outbox_transition(frm: OutboxStatus, to: OutboxStatus) -> bool:
    return (frm, to) in ALLOWED_OUTBOX_TRANSITIONS


def assert_outbox_transition(frm: OutboxStatus, to: OutboxStatus) -> None:
    if not can_outbox_transition(frm, to):
        raise IllegalOutboxTransition(
            f"outbox status transition {frm.value} -> {to.value} is not allowed"
        )


def is_terminal_outbox(status: OutboxStatus) -> bool:
    return status in TERMINAL_OUTBOX_STATUSES


# ---------- Recovery action enumeration ---------------------------------------


class RecoveryAction(str):
    """What the broker should do with an outbox row on restart.

    Used by §5.2 step 3 of the architecture.
    """

    REDISPATCH = "redispatch"  # send (or re-send) with same idempotency_key
    PROBE_AND_RECONCILE = "probe_and_reconcile"  # delivered, no reply — see §3.5 row
    NO_ACTION = "no_action"
    ESCALATE_WORKER_LOSS = "escalate_worker_loss"


def recovery_action_for(row: OutboxRow) -> str:
    """Return the recovery action that §5.2 prescribes for `row.status`."""
    if row.status is OutboxStatus.PENDING:
        return RecoveryAction.REDISPATCH
    if row.status is OutboxStatus.SENT:
        return RecoveryAction.REDISPATCH
    if row.status is OutboxStatus.DELIVERED:
        return RecoveryAction.PROBE_AND_RECONCILE
    if row.status in {OutboxStatus.ANSWERED, OutboxStatus.FAILED, OutboxStatus.ABANDONED}:
        return RecoveryAction.NO_ACTION
    raise ValueError(f"unknown outbox status: {row.status}")


# ---------- Reply correlation validation --------------------------------------


@dataclass(frozen=True)
class ReplyValidation:
    """Result of validating a worker reply against the pending outbox row.

    `accept=True` means the broker should append the reply event and transition
    the outbox row to `answered`. `accept=False` means the broker MUST
    quarantine the reply as an `error` event and NOT advance the turn.
    """

    accept: bool
    reason: str | None = None


def validate_worker_reply(
    *,
    pending_row: OutboxRow | None,
    conversation_turn_token_version: int,
    reply_in_reply_to_idempotency_key: str | None,
    reply_observed_turn_token_version: int | None,
) -> ReplyValidation:
    """Per §3.5 step 4: gate every worker output through this check.

    The broker MUST call this for every worker emission. There is no parallel
    "reconcile by content_hash" path — that anti-pattern was explicitly
    flagged in round 4 of the CP1 codex review.
    """
    if pending_row is None:
        return ReplyValidation(
            accept=False,
            reason="no pending_delivery_id — conversation state inconsistent",
        )
    if pending_row.status in TERMINAL_OUTBOX_STATUSES:
        return ReplyValidation(
            accept=False,
            reason=f"pending row is already terminal ({pending_row.status.value})",
        )
    if reply_in_reply_to_idempotency_key is None:
        return ReplyValidation(
            accept=False,
            reason="reply missing in_reply_to_idempotency_key",
        )
    if reply_in_reply_to_idempotency_key != pending_row.idempotency_key:
        return ReplyValidation(
            accept=False,
            reason=(
                f"idempotency key mismatch: reply claims "
                f"{reply_in_reply_to_idempotency_key!r}, expected "
                f"{pending_row.idempotency_key!r}"
            ),
        )
    if reply_observed_turn_token_version is None:
        return ReplyValidation(
            accept=False,
            reason="reply missing observed_turn_token_version",
        )
    if reply_observed_turn_token_version != conversation_turn_token_version:
        return ReplyValidation(
            accept=False,
            reason=(
                f"stale turn token: reply observed v"
                f"{reply_observed_turn_token_version}, current is v"
                f"{conversation_turn_token_version}"
            ),
        )
    return ReplyValidation(accept=True)


# ---------- Pure-function FSM step --------------------------------------------


def advance_outbox(
    row: OutboxRow,
    *,
    to_status: OutboxStatus,
    now: datetime,
    reply_event_id: str | None = None,
    last_error: str | None = None,
    attempt_count_delta: int = 0,
) -> OutboxRow:
    """Return a new OutboxRow with the requested transition applied.

    Raises `IllegalOutboxTransition` if `to_status` is not whitelisted.
    Pure — does not touch any store.
    """
    assert_outbox_transition(row.status, to_status)

    fields: dict[str, object] = {"status": to_status}
    fields["attempt_count"] = row.attempt_count + attempt_count_delta
    if to_status is OutboxStatus.SENT and row.sent_at is None:
        fields["sent_at"] = now
    if to_status is OutboxStatus.DELIVERED and row.delivered_at is None:
        fields["delivered_at"] = now
    if to_status is OutboxStatus.ANSWERED:
        fields["answered_at"] = now
        if reply_event_id is None:
            raise ValueError("advance_outbox to ANSWERED requires reply_event_id")
        fields["reply_event_id"] = reply_event_id
    if last_error is not None:
        fields["last_error"] = last_error
    return row.model_copy(update=fields)


def rows_needing_recovery(rows: Iterable[OutboxRow]) -> list[OutboxRow]:
    """Filter for rows whose recovery action is not NO_ACTION."""
    return [r for r in rows if recovery_action_for(r) != RecoveryAction.NO_ACTION]
