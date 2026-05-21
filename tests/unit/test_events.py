"""Unit tests for the event envelope and value types.

Aligned with the API in `agent_bridge.events`: `Worker` (not `TurnHolder`),
no `utcnow()` helper. Tests cover the frozen-model contract, seq/round
validation, extra-field rejection, and the worker-reply correlation tokens
the §3.5 step-4 validator depends on.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from agent_bridge.events import (
    ConsensusSignal,
    Event,
    EventKind,
    EventMetadata,
    Phase,
    Severity,
    Worker,
)


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _basic_event(**overrides: object) -> Event:
    content = "I propose extracting token refresh into AuthSessionManager."
    base: dict[str, Any] = {
        "event_id": "evt_00001",
        "conversation_id": "conv_0001",
        "seq": 1,
        "round": 0,
        "phase": Phase.PLANNING,
        "sender": Worker.WORKER_B,
        "recipient": Worker.WORKER_A,
        "kind": EventKind.PROPOSAL,
        "content": content,
        "content_hash": _hash(content),
        "requires_reply": True,
        "created_at": _now(),
    }
    base.update(overrides)
    return Event(**base)


def test_event_round_trip() -> None:
    e = _basic_event()
    payload = e.model_dump_json()
    e2 = Event.model_validate_json(payload)
    assert e == e2


def test_event_is_frozen() -> None:
    e = _basic_event()
    with pytest.raises(ValidationError):
        e.seq = 99


def test_event_seq_must_be_at_least_1() -> None:
    with pytest.raises(ValidationError):
        _basic_event(seq=0)


def test_event_round_non_negative() -> None:
    _basic_event(round=0)  # ok
    with pytest.raises(ValidationError):
        _basic_event(round=-1)


def test_event_rejects_unknown_fields() -> None:
    """extra='forbid' is part of the contract; typos shouldn't silently pass."""
    with pytest.raises(ValidationError):
        Event(
            event_id="evt",
            conversation_id="c",
            seq=1,
            round=0,
            phase=Phase.PLANNING,
            sender=Worker.BROKER,
            recipient=Worker.WORKER_A,
            kind=EventKind.SYSTEM_NOTE,
            content="x",
            content_hash=_hash("x"),
            requires_reply=False,
            created_at=_now(),
            mispelled_field="oops",  # type: ignore[call-arg]
        )


def test_event_metadata_carries_consensus_signal() -> None:
    e = _basic_event(
        kind=EventKind.AGREEMENT,
        metadata=EventMetadata(consensus_signal=ConsensusSignal.AGREED),
    )
    assert e.metadata.consensus_signal is ConsensusSignal.AGREED


def test_event_worker_reply_correlation_fields() -> None:
    """Worker -> broker messages must carry the correlation token (§3.5)."""
    e = _basic_event(
        sender=Worker.WORKER_A,
        recipient=Worker.BROKER,
        kind=EventKind.CRITIQUE,
        in_reply_to_idempotency_key="ik_abc123",
        observed_turn_token_version=17,
        metadata=EventMetadata(severity=Severity.P1),
    )
    assert e.in_reply_to_idempotency_key == "ik_abc123"
    assert e.observed_turn_token_version == 17
    assert e.metadata.severity is Severity.P1
