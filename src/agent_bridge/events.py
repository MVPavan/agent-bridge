"""Event envelope and related Pydantic models.

Spec: docs/architecture.md §3.1.

Every agent-to-agent message and every system event uses the same `Event`
shape. Models are frozen — events are immutable once created, which is what
the append-only event log requires.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    """tz-aware now in UTC. Use this everywhere instead of `datetime.now()`."""
    return datetime.now(UTC)

# ---------- Enums --------------------------------------------------------------


class Worker(StrEnum):
    """Identity of an agent or actor in the conversation."""

    WORKER_A = "worker_a"  # Claude #2
    WORKER_B = "worker_b"  # Codex
    BROKER = "broker"
    USER = "user"


# Domain-language alias: when a piece of code is reasoning about who currently
# holds the turn token (§3.3), prefer `TurnHolder`. The set of values is the
# same as `Worker` — this is purely a readability alias.
TurnHolder = Worker


class Phase(StrEnum):
    """Conversation phase. See architecture.md §2.3 + §3.4."""

    PLANNING = "planning"
    CONSENSUS = "consensus"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    DONE = "done"
    ABORTED = "aborted"


class EventKind(StrEnum):
    """The semantic kind of an event. See architecture.md §3.1."""

    PROPOSAL = "proposal"
    CRITIQUE = "critique"
    AGREEMENT = "agreement"
    REJECTION = "rejection"
    CLARIFICATION_REQUEST = "clarification_request"
    CLARIFICATION_REPLY = "clarification_reply"
    DIFF = "diff"
    TEST_RESULT = "test_result"
    REVIEW_FINDING = "review_finding"
    FIX_RESPONSE = "fix_response"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_DECISION = "approval_decision"
    SYSTEM_NOTE = "system_note"
    ERROR = "error"


class ConsensusSignal(StrEnum):
    """Worker-emitted signal used by the consensus detector."""

    AGREED = "agreed"
    DISAGREE = "disagree"
    NEEDS_INFO = "needs_info"


class Severity(StrEnum):
    """Severity for review findings."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


# ---------- Models -------------------------------------------------------------


class EventMetadata(BaseModel):
    """Optional, kind-specific metadata attached to an Event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consensus_signal: ConsensusSignal | None = None
    severity: Severity | None = None
    diff_artifact: str | None = None
    test_log_artifact: str | None = None
    review_artifact: str | None = None
    # `unexpected_mutation` is set by the content-snapshot validator
    # (architecture.md §6.3.1.1).
    unexpected_mutation: bool = False
    unexpected_mutation_paths: tuple[str, ...] = ()


class Event(BaseModel):
    """One agent-to-agent message or system event. Immutable once created.

    Workers MUST populate `in_reply_to_idempotency_key` and
    `observed_turn_token_version` when they reply to a broker-originated
    message. The broker validates these in the §3.5 step-4 completion path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    conversation_id: str
    seq: int = Field(ge=1, description="Monotonic per-conversation sequence number.")
    round: int = Field(ge=0)
    phase: Phase
    sender: Worker
    recipient: Worker
    kind: EventKind
    content: str
    content_hash: str = Field(description="sha256 hex of `content`. Used by JSONL reconciliation.")
    requires_reply: bool
    metadata: EventMetadata = Field(default_factory=EventMetadata)
    in_reply_to_idempotency_key: str | None = None
    observed_turn_token_version: int | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (use utcnow())")
        return v


class OutboxStatus(StrEnum):
    """Outbox state machine. See architecture.md §3.5."""

    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    ANSWERED = "answered"
    FAILED = "failed"
    ABANDONED = "abandoned"


class OutboxRow(BaseModel):
    """Durable record of one broker → worker delivery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    delivery_id: str
    conversation_id: str
    event_id: str
    recipient: Literal[Worker.WORKER_A, Worker.WORKER_B]
    idempotency_key: str
    status: OutboxStatus
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    last_error: str | None = None
    enqueued_at: datetime
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    answered_at: datetime | None = None
    reply_event_id: str | None = None
