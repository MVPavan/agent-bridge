"""SQLite-backed durable store.

Spec: docs/architecture.md §4. SQLite is the source of truth; JSONL is
best-effort replication (§4.3 row 2). All durability-critical writes use
WAL mode + `synchronous=FULL`.

This module is pure persistence. The orchestration layer (broker.py)
calls it to enqueue/transition outbox rows, append events, and resume
state on restart. Schema matches §4.2.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from .events import (
    Event,
    EventKind,
    EventMetadata,
    OutboxRow,
    OutboxStatus,
    Phase,
    TurnHolder,
    utcnow,
)

OutboxRecipient = Literal[TurnHolder.WORKER_A, TurnHolder.WORKER_B]


def _as_outbox_recipient(raw: str) -> OutboxRecipient:
    """Narrow a DB recipient string to the Literal type OutboxRow expects."""
    worker = TurnHolder(raw)
    if worker is TurnHolder.WORKER_A:
        return TurnHolder.WORKER_A
    if worker is TurnHolder.WORKER_B:
        return TurnHolder.WORKER_B
    raise ValueError(f"outbox recipient must be worker_a or worker_b, got {raw!r}")

# ---------- DDL ---------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id       TEXT PRIMARY KEY,
    goal                  TEXT NOT NULL,
    phase                 TEXT NOT NULL,
    round                 INTEGER NOT NULL DEFAULT 0,
    max_rounds            INTEGER NOT NULL,
    status                TEXT NOT NULL,
    current_turn          TEXT NOT NULL,
    turn_token_version    INTEGER NOT NULL DEFAULT 0,
    pending_delivery_id   TEXT,
    next_seq              INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    worktree_path         TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    conversation_id   TEXT NOT NULL,
    worker            TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    pid               INTEGER,
    state             TEXT NOT NULL,
    permission_mode   TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    PRIMARY KEY (conversation_id, worker)
);

CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    round             INTEGER NOT NULL,
    phase             TEXT NOT NULL,
    sender            TEXT NOT NULL,
    recipient         TEXT NOT NULL,
    kind              TEXT NOT NULL,
    content           TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    metadata_json     TEXT,
    requires_reply    INTEGER NOT NULL,
    in_reply_to_idempotency_key TEXT,
    observed_turn_token_version INTEGER,
    created_at        TEXT NOT NULL,
    UNIQUE (conversation_id, seq),
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_events_conv_round ON events(conversation_id, round);

CREATE TABLE IF NOT EXISTS outbox (
    delivery_id        TEXT PRIMARY KEY,
    conversation_id    TEXT NOT NULL,
    event_id           TEXT NOT NULL,
    recipient          TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL UNIQUE,
    status             TEXT NOT NULL,
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    next_retry_at      TEXT,
    last_error         TEXT,
    enqueued_at        TEXT NOT NULL,
    sent_at            TEXT,
    delivered_at       TEXT,
    answered_at        TEXT,
    reply_event_id     TEXT,
    FOREIGN KEY (event_id) REFERENCES events(event_id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, next_retry_at);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id       TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    category          TEXT NOT NULL,
    requesting_worker TEXT,
    payload_json      TEXT NOT NULL,
    decision          TEXT,
    requested_at      TEXT NOT NULL,
    decided_at        TEXT,
    decided_by        TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id       TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    relative_path     TEXT NOT NULL,
    kind              TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- Trigger: events are append-only (invariant I-3).
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only (invariant I-3)');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only (invariant I-3)');
END;
"""


# ---------- Domain dataclasses returned to the orchestrator -------------------


@dataclass(frozen=True)
class ConversationRow:
    conversation_id: str
    goal: str
    phase: Phase
    round: int
    max_rounds: int
    status: str  # running | paused | done | aborted
    current_turn: TurnHolder
    turn_token_version: int
    pending_delivery_id: str | None
    next_seq: int
    created_at: datetime
    updated_at: datetime
    worktree_path: Path | None


# ---------- Helpers -----------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------- Store -------------------------------------------------------------


class Store:
    """Async SQLite wrapper. Single broker process, single writer.

    Open via `await Store.open(path)` or `async with Store.connect(path) as s`.
    """

    def __init__(self, conn: aiosqlite.Connection, *, db_path: Path) -> None:
        self._conn = conn
        self.db_path = db_path

    @classmethod
    async def open(cls, db_path: Path) -> Store:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path))
        # WAL + synchronous=FULL for durability-critical writes per §4.3.
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = FULL")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn, db_path=db_path)

    async def close(self) -> None:
        await self._conn.close()

    async def __aenter__(self) -> Store:  # pragma: no cover — convenience
        return self

    async def __aexit__(self, *exc: object) -> None:  # pragma: no cover — convenience
        await self.close()

    # ---------- Conversation lifecycle ---------------------------------------

    async def create_conversation(
        self,
        *,
        goal: str,
        max_rounds: int = 6,
        worktree_path: Path | None = None,
    ) -> ConversationRow:
        cid = _new_id("conv")
        now = utcnow()
        await self._conn.execute(
            """INSERT INTO conversations
                 (conversation_id, goal, phase, round, max_rounds, status,
                  current_turn, turn_token_version, pending_delivery_id,
                  next_seq, created_at, updated_at, worktree_path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cid, goal, Phase.PLANNING.value, 0, max_rounds, "running",
                TurnHolder.BROKER.value, 0, None, 1,
                _iso(now), _iso(now),
                str(worktree_path) if worktree_path else None,
            ),
        )
        await self._conn.commit()
        return ConversationRow(
            conversation_id=cid, goal=goal, phase=Phase.PLANNING, round=0,
            max_rounds=max_rounds, status="running",
            current_turn=TurnHolder.BROKER, turn_token_version=0,
            pending_delivery_id=None, next_seq=1,
            created_at=now, updated_at=now,
            worktree_path=worktree_path,
        )

    async def get_conversation(self, conversation_id: str) -> ConversationRow | None:
        async with self._conn.execute(
            "SELECT * FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description] if cur.description else []
        rec = dict(zip(cols, row, strict=False))
        return ConversationRow(
            conversation_id=rec["conversation_id"],
            goal=rec["goal"],
            phase=Phase(rec["phase"]),
            round=rec["round"],
            max_rounds=rec["max_rounds"],
            status=rec["status"],
            current_turn=TurnHolder(rec["current_turn"]),
            turn_token_version=rec["turn_token_version"],
            pending_delivery_id=rec["pending_delivery_id"],
            next_seq=rec["next_seq"],
            created_at=datetime.fromisoformat(rec["created_at"]),
            updated_at=datetime.fromisoformat(rec["updated_at"]),
            worktree_path=Path(rec["worktree_path"]) if rec["worktree_path"] else None,
        )

    async def list_active_conversations(self) -> list[ConversationRow]:
        rows: list[ConversationRow] = []
        async with self._conn.execute(
            "SELECT conversation_id FROM conversations WHERE status IN ('running','paused')"
        ) as cur:
            async for (cid,) in cur:
                r = await self.get_conversation(cid)
                if r is not None:
                    rows.append(r)
        return rows

    async def set_phase(self, conversation_id: str, phase: Phase) -> None:
        await self._conn.execute(
            "UPDATE conversations SET phase=?, updated_at=? WHERE conversation_id=?",
            (phase.value, _iso(utcnow()), conversation_id),
        )
        await self._conn.commit()

    async def set_status(self, conversation_id: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE conversations SET status=?, updated_at=? WHERE conversation_id=?",
            (status, _iso(utcnow()), conversation_id),
        )
        await self._conn.commit()

    async def set_turn(
        self,
        conversation_id: str,
        *,
        turn: TurnHolder,
        bump_version: bool = True,
        pending_delivery_id: str | None = None,
    ) -> int:
        """Atomically set turn holder and (optionally) bump the version.

        Returns the new turn_token_version.
        """
        now = _iso(utcnow())
        if bump_version:
            await self._conn.execute(
                """UPDATE conversations
                     SET current_turn=?, turn_token_version=turn_token_version+1,
                         pending_delivery_id=?, updated_at=?
                   WHERE conversation_id=?""",
                (turn.value, pending_delivery_id, now, conversation_id),
            )
        else:
            await self._conn.execute(
                """UPDATE conversations
                     SET current_turn=?, pending_delivery_id=?, updated_at=?
                   WHERE conversation_id=?""",
                (turn.value, pending_delivery_id, now, conversation_id),
            )
        await self._conn.commit()
        async with self._conn.execute(
            "SELECT turn_token_version FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ) as cur:
            r = await cur.fetchone()
        return int(r[0]) if r else 0

    async def increment_round(self, conversation_id: str) -> int:
        await self._conn.execute(
            "UPDATE conversations SET round=round+1, updated_at=? WHERE conversation_id=?",
            (_iso(utcnow()), conversation_id),
        )
        await self._conn.commit()
        async with self._conn.execute(
            "SELECT round FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ) as cur:
            r = await cur.fetchone()
        return int(r[0]) if r else 0

    # ---------- Events --------------------------------------------------------

    async def dispatch_turn(
        self,
        event: Event,
        outbox_row: OutboxRow,
        *,
        turn_target: TurnHolder,
    ) -> int:
        """Atomically persist event + outbox + turn transition in one transaction.

        Replaces the three-call sequence (append_event → enqueue_outbox →
        set_turn) used by `Broker.deliver_turn_to` so the §3.5 atomicity
        invariant holds across crashes between any two of the writes. Each of
        those callers committed independently, leaving recovery ambiguous if
        the broker crashed mid-sequence (the codex adversarial-review job
        `review-mpf7lig2-p6fxyi` flagged this as a P0).

        Returns the new `turn_token_version`.
        """
        meta_json = (
            event.metadata.model_dump_json() if event.metadata else None
        )
        now = _iso(utcnow())
        # Single transaction — explicit BEGIN IMMEDIATE so a failure of any
        # statement triggers a rollback that removes ALL writes in this turn
        # transition. Without IMMEDIATE the connection's deferred default
        # could let a later statement partially commit on certain error
        # paths in aiosqlite.
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Allocate the event seq inside the same transaction so a
            # rollback also rolls back the seq counter — no durable seq
            # gaps on failed turn writes. Round-3 codex review
            # `review-mpf9p59f-rrcc8u` flagged the prior pre-transaction
            # allocation as P1.
            fresh_seq = await self._alloc_seq_in_txn(event.conversation_id)
            event_to_insert = event.model_copy(update={"seq": fresh_seq})
            await self._conn.execute(
                """INSERT INTO events
                     (event_id, conversation_id, seq, round, phase, sender, recipient,
                      kind, content, content_hash, metadata_json, requires_reply,
                      in_reply_to_idempotency_key, observed_turn_token_version,
                      created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_to_insert.event_id, event_to_insert.conversation_id,
                    event_to_insert.seq, event_to_insert.round,
                    event_to_insert.phase.value, event_to_insert.sender.value,
                    event_to_insert.recipient.value,
                    event_to_insert.kind.value, event_to_insert.content,
                    event_to_insert.content_hash,
                    meta_json, int(event_to_insert.requires_reply),
                    event_to_insert.in_reply_to_idempotency_key,
                    event_to_insert.observed_turn_token_version,
                    _iso(event_to_insert.created_at),
                ),
            )
            await self._conn.execute(
                """INSERT INTO outbox
                     (delivery_id, conversation_id, event_id, recipient, idempotency_key,
                      status, attempt_count, next_retry_at, last_error,
                      enqueued_at, sent_at, delivered_at, answered_at, reply_event_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    outbox_row.delivery_id, outbox_row.conversation_id, outbox_row.event_id,
                    outbox_row.recipient.value, outbox_row.idempotency_key,
                    outbox_row.status.value, outbox_row.attempt_count,
                    _iso(outbox_row.next_retry_at) if outbox_row.next_retry_at else None,
                    outbox_row.last_error, _iso(outbox_row.enqueued_at),
                    _iso(outbox_row.sent_at) if outbox_row.sent_at else None,
                    _iso(outbox_row.delivered_at) if outbox_row.delivered_at else None,
                    _iso(outbox_row.answered_at) if outbox_row.answered_at else None,
                    outbox_row.reply_event_id,
                ),
            )
            await self._conn.execute(
                """UPDATE conversations
                     SET current_turn=?, turn_token_version=turn_token_version+1,
                         pending_delivery_id=?, updated_at=?
                   WHERE conversation_id=?""",
                (turn_target.value, outbox_row.delivery_id, now, event.conversation_id),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        async with self._conn.execute(
            "SELECT turn_token_version FROM conversations WHERE conversation_id=?",
            (event.conversation_id,),
        ) as cur:
            r = await cur.fetchone()
        return int(r[0]) if r else 0

    async def complete_turn(
        self,
        reply_event: Event,
        outbox_row: OutboxRow,
        *,
        next_turn: TurnHolder = TurnHolder.BROKER,
    ) -> None:
        """Atomically commit a worker reply + outbox→ANSWERED + clear turn.

        Symmetric counterpart to `dispatch_turn`. The completion path
        previously split into three durable writes (append reply event,
        update outbox to ANSWERED, set_turn to broker), reopening the same
        crash-window class as the enqueue side. Codex round-2 review
        `review-mpf8nsmd-w82pki` flagged this as a P0; this method puts all
        three in one transaction with explicit rollback on error.

        `outbox_row` must carry the new ANSWERED state and `reply_event_id`
        already populated (via `advance_outbox(...)`).
        """
        meta_json = (
            reply_event.metadata.model_dump_json() if reply_event.metadata else None
        )
        now = _iso(utcnow())
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Same in-transaction seq allocation as dispatch_turn — round-3
            # codex review fix for the seq-gap-on-rollback P1.
            fresh_seq = await self._alloc_seq_in_txn(reply_event.conversation_id)
            reply_to_insert = reply_event.model_copy(update={"seq": fresh_seq})
            await self._conn.execute(
                """INSERT INTO events
                     (event_id, conversation_id, seq, round, phase, sender, recipient,
                      kind, content, content_hash, metadata_json, requires_reply,
                      in_reply_to_idempotency_key, observed_turn_token_version,
                      created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    reply_to_insert.event_id, reply_to_insert.conversation_id,
                    reply_to_insert.seq, reply_to_insert.round,
                    reply_to_insert.phase.value,
                    reply_to_insert.sender.value, reply_to_insert.recipient.value,
                    reply_to_insert.kind.value, reply_to_insert.content,
                    reply_to_insert.content_hash,
                    meta_json, int(reply_to_insert.requires_reply),
                    reply_to_insert.in_reply_to_idempotency_key,
                    reply_to_insert.observed_turn_token_version,
                    _iso(reply_to_insert.created_at),
                ),
            )
            await self._conn.execute(
                """UPDATE outbox
                     SET status=?, attempt_count=?, next_retry_at=?, last_error=?,
                         sent_at=?, delivered_at=?, answered_at=?, reply_event_id=?
                   WHERE delivery_id=?""",
                (
                    outbox_row.status.value, outbox_row.attempt_count,
                    _iso(outbox_row.next_retry_at) if outbox_row.next_retry_at else None,
                    outbox_row.last_error,
                    _iso(outbox_row.sent_at) if outbox_row.sent_at else None,
                    _iso(outbox_row.delivered_at) if outbox_row.delivered_at else None,
                    _iso(outbox_row.answered_at) if outbox_row.answered_at else None,
                    outbox_row.reply_event_id,
                    outbox_row.delivery_id,
                ),
            )
            # Clear the pending delivery and hand the turn back to the broker
            # for next-step decisioning. Note: turn_token_version is NOT
            # bumped here — it tracks broker→worker dispatches per §3.3, and
            # parking the token with the broker after a completed reply does
            # not constitute a new dispatch.
            await self._conn.execute(
                """UPDATE conversations
                     SET current_turn=?, pending_delivery_id=NULL, updated_at=?
                   WHERE conversation_id=?""",
                (next_turn.value, now, reply_event.conversation_id),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def append_event(self, event: Event) -> None:
        meta_json = (
            event.metadata.model_dump_json() if event.metadata else None
        )
        await self._conn.execute(
            """INSERT INTO events
                 (event_id, conversation_id, seq, round, phase, sender, recipient,
                  kind, content, content_hash, metadata_json, requires_reply,
                  in_reply_to_idempotency_key, observed_turn_token_version,
                  created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.event_id, event.conversation_id, event.seq, event.round,
                event.phase.value, event.sender.value, event.recipient.value,
                event.kind.value, event.content, event.content_hash,
                meta_json, int(event.requires_reply),
                event.in_reply_to_idempotency_key,
                event.observed_turn_token_version,
                _iso(event.created_at),
            ),
        )
        await self._conn.commit()

    async def _alloc_seq_in_txn(self, conversation_id: str) -> int:
        """Reserve next_seq WITHOUT committing. Caller must be inside a transaction.

        Used by `dispatch_turn` and `complete_turn` so a transaction rollback
        also rolls back the seq increment, eliminating durable seq gaps on
        failed turn writes (round-3 codex review `review-mpf9p59f-rrcc8u`).
        """
        async with self._conn.execute(
            "SELECT next_seq FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ) as cur:
            r = await cur.fetchone()
        if r is None:
            raise KeyError(conversation_id)
        seq = int(r[0])
        await self._conn.execute(
            "UPDATE conversations SET next_seq=next_seq+1 WHERE conversation_id=?",
            (conversation_id,),
        )
        return seq

    async def next_seq(self, conversation_id: str) -> int:
        """Reserve and return the next sequence number for the conversation.

        Standalone (non-transactional) version. Use only when the caller is
        NOT going through `dispatch_turn`/`complete_turn` — for example,
        direct `append_event` writes in tests or recovery scaffolding.
        Production turn writes use the in-transaction allocator instead.
        """
        async with self._conn.execute(
            "SELECT next_seq FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ) as cur:
            r = await cur.fetchone()
        if r is None:
            raise KeyError(conversation_id)
        seq = int(r[0])
        await self._conn.execute(
            "UPDATE conversations SET next_seq=next_seq+1 WHERE conversation_id=?",
            (conversation_id,),
        )
        await self._conn.commit()
        return seq

    async def get_event_seq(
        self, conversation_id: str, event_id: str
    ) -> int | None:
        """Return the `seq` of an event by `event_id`, or None if not found.

        Round-8 codex P1 `review-mpfb42vk-lajla7`: `Broker.tail_transcript`
        used to call `list_events(limit=1)` to resolve the cursor, which
        only matched the very first event. This direct lookup makes the
        cursor work for any event_id.
        """
        async with self._conn.execute(
            "SELECT seq FROM events WHERE conversation_id=? AND event_id=?",
            (conversation_id, event_id),
        ) as cur:
            r = await cur.fetchone()
        return None if r is None else int(r[0])

    async def list_events(
        self,
        conversation_id: str,
        *,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[Event]:
        out: list[Event] = []
        async with self._conn.execute(
            """SELECT event_id, conversation_id, seq, round, phase, sender,
                      recipient, kind, content, content_hash, metadata_json,
                      requires_reply, in_reply_to_idempotency_key,
                      observed_turn_token_version, created_at
                 FROM events
                WHERE conversation_id=? AND seq > ?
                ORDER BY seq ASC LIMIT ?""",
            (conversation_id, after_seq, limit),
        ) as cur:
            async for row in cur:
                (
                    event_id, cid, seq, rnd, phase, sender, recipient, kind,
                    content, content_hash, meta_json, requires_reply,
                    in_reply_to_idempotency_key, observed_turn_token_version,
                    created_at,
                ) = row
                metadata = (
                    EventMetadata.model_validate_json(meta_json)
                    if meta_json
                    else EventMetadata()
                )
                out.append(
                    Event(
                        event_id=event_id, conversation_id=cid, seq=seq, round=rnd,
                        phase=Phase(phase),
                        sender=TurnHolder(sender), recipient=TurnHolder(recipient),
                        kind=EventKind(kind),
                        content=content, content_hash=content_hash,
                        requires_reply=bool(requires_reply),
                        metadata=metadata,
                        in_reply_to_idempotency_key=in_reply_to_idempotency_key,
                        observed_turn_token_version=observed_turn_token_version,
                        created_at=datetime.fromisoformat(created_at),
                    )
                )
        return out

    # ---------- Outbox --------------------------------------------------------

    async def enqueue_outbox(self, row: OutboxRow) -> None:
        await self._conn.execute(
            """INSERT INTO outbox
                 (delivery_id, conversation_id, event_id, recipient, idempotency_key,
                  status, attempt_count, next_retry_at, last_error,
                  enqueued_at, sent_at, delivered_at, answered_at, reply_event_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row.delivery_id, row.conversation_id, row.event_id,
                row.recipient.value, row.idempotency_key, row.status.value,
                row.attempt_count,
                _iso(row.next_retry_at) if row.next_retry_at else None,
                row.last_error, _iso(row.enqueued_at),
                _iso(row.sent_at) if row.sent_at else None,
                _iso(row.delivered_at) if row.delivered_at else None,
                _iso(row.answered_at) if row.answered_at else None,
                row.reply_event_id,
            ),
        )
        await self._conn.commit()

    async def update_outbox(self, row: OutboxRow) -> None:
        await self._conn.execute(
            """UPDATE outbox SET status=?, attempt_count=?, next_retry_at=?,
                                 last_error=?, sent_at=?, delivered_at=?,
                                 answered_at=?, reply_event_id=?
                WHERE delivery_id=?""",
            (
                row.status.value, row.attempt_count,
                _iso(row.next_retry_at) if row.next_retry_at else None,
                row.last_error,
                _iso(row.sent_at) if row.sent_at else None,
                _iso(row.delivered_at) if row.delivered_at else None,
                _iso(row.answered_at) if row.answered_at else None,
                row.reply_event_id, row.delivery_id,
            ),
        )
        await self._conn.commit()

    async def get_outbox(self, delivery_id: str) -> OutboxRow | None:
        async with self._conn.execute(
            "SELECT * FROM outbox WHERE delivery_id=?", (delivery_id,)
        ) as cur:
            row = await cur.fetchone()
            cols = [c[0] for c in cur.description] if cur.description else []
        if row is None:
            return None
        rec = dict(zip(cols, row, strict=False))
        return OutboxRow(
            delivery_id=rec["delivery_id"],
            conversation_id=rec["conversation_id"],
            event_id=rec["event_id"],
            recipient=_as_outbox_recipient(rec["recipient"]),
            idempotency_key=rec["idempotency_key"],
            status=OutboxStatus(rec["status"]),
            attempt_count=rec["attempt_count"],
            next_retry_at=_parse_dt(rec["next_retry_at"]),
            last_error=rec["last_error"],
            enqueued_at=datetime.fromisoformat(rec["enqueued_at"]),
            sent_at=_parse_dt(rec["sent_at"]),
            delivered_at=_parse_dt(rec["delivered_at"]),
            answered_at=_parse_dt(rec["answered_at"]),
            reply_event_id=rec["reply_event_id"],
        )

    async def list_outbox_needing_recovery(
        self, conversation_id: str
    ) -> list[OutboxRow]:
        """Per §5.2 step 3: all rows in pending/sent/delivered for this conversation."""
        out: list[OutboxRow] = []
        async with self._conn.execute(
            """SELECT delivery_id FROM outbox
                WHERE conversation_id=? AND status IN ('pending','sent','delivered')""",
            (conversation_id,),
        ) as cur:
            async for (did,) in cur:
                r = await self.get_outbox(did)
                if r is not None:
                    out.append(r)
        return out

    # ---------- Approvals -----------------------------------------------------

    async def create_approval(
        self,
        *,
        conversation_id: str,
        category: str,
        payload: dict[str, Any],
        requesting_worker: TurnHolder | None = None,
    ) -> str:
        approval_id = _new_id("appr")
        await self._conn.execute(
            """INSERT INTO approvals
                 (approval_id, conversation_id, category, requesting_worker,
                  payload_json, decision, requested_at, decided_at, decided_by)
               VALUES (?,?,?,?,?,NULL,?,NULL,NULL)""",
            (
                approval_id, conversation_id, category,
                requesting_worker.value if requesting_worker else None,
                json.dumps(payload), _iso(utcnow()),
            ),
        )
        await self._conn.commit()
        return approval_id

    async def has_pending_worker_loss_for_delivery(
        self, conversation_id: str, delivery_id: str
    ) -> bool:
        """Idempotency probe for `recover_on_startup`.

        Round-7 codex P1 `review-mpfaq…` flagged that the recovery pass
        unconditionally INSERTed a fresh worker_loss approval per
        pending/sent/delivered outbox row, so repeated broker restarts
        produced N duplicate approvals for the same lost turn. This probe
        lets the broker check first and skip if an unresolved approval
        already exists.

        Matches by JSON substring on `delivery_id` since approval payloads
        are stored as a JSON blob; the payload is broker-controlled, so
        the substring match is safe.
        """
        async with self._conn.execute(
            """SELECT 1 FROM approvals
                 WHERE conversation_id=?
                   AND category='worker_loss'
                   AND decision IS NULL
                   AND payload_json LIKE ?
                 LIMIT 1""",
            (conversation_id, f'%"delivery_id": "{delivery_id}"%'),
        ) as cur:
            r = await cur.fetchone()
        return r is not None

    async def get_approval(
        self, approval_id: str
    ) -> tuple[str, str, str, str | None] | None:
        """Return `(conversation_id, category, payload_json, decision)` or None.

        Used by `Broker.approve` to look up category before dispatching the
        category-specific state transition (round-4 codex P0
        `review-mpfa3kk6-...`).
        """
        async with self._conn.execute(
            """SELECT conversation_id, category, payload_json, decision
                 FROM approvals
                WHERE approval_id=?""",
            (approval_id,),
        ) as cur:
            r = await cur.fetchone()
        if r is None:
            return None
        return (str(r[0]), str(r[1]), str(r[2]), None if r[3] is None else str(r[3]))

    async def create_conversation_atomic(
        self,
        *,
        goal: str,
        seed_event_id: str,
        seed_content: str,
        seed_content_hash: str,
        max_rounds: int = 6,
        worktree_path: Path | None = None,
    ) -> ConversationRow:
        """Atomically create a conversation AND append its seed event.

        Proactively fixed pre-R7 — same symmetric-defect pattern as the other
        atomicity gaps. A crash between INSERT conversation and INSERT seed
        event used to leave a conversation row with no transcript anchor.
        Bundles both in one `BEGIN IMMEDIATE` transaction with explicit
        rollback on error.
        """
        cid = _new_id("conv")
        now = utcnow()
        now_iso = _iso(now)
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            await self._conn.execute(
                """INSERT INTO conversations
                     (conversation_id, goal, phase, round, max_rounds, status,
                      current_turn, turn_token_version, pending_delivery_id,
                      next_seq, created_at, updated_at, worktree_path)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cid, goal, Phase.PLANNING.value, 0, max_rounds, "running",
                    TurnHolder.BROKER.value, 0, None, 1,
                    now_iso, now_iso,
                    str(worktree_path) if worktree_path else None,
                ),
            )
            # Allocate seq 1 (the seed event) inside the same transaction.
            fresh_seq = await self._alloc_seq_in_txn(cid)
            await self._conn.execute(
                """INSERT INTO events
                     (event_id, conversation_id, seq, round, phase, sender, recipient,
                      kind, content, content_hash, metadata_json, requires_reply,
                      in_reply_to_idempotency_key, observed_turn_token_version,
                      created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    seed_event_id, cid, fresh_seq, 0, Phase.PLANNING.value,
                    TurnHolder.USER.value, TurnHolder.BROKER.value,
                    EventKind.SYSTEM_NOTE.value, seed_content, seed_content_hash,
                    None, 0, None, None, now_iso,
                ),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return ConversationRow(
            conversation_id=cid,
            goal=goal,
            phase=Phase.PLANNING,
            round=0,
            max_rounds=max_rounds,
            status="running",
            current_turn=TurnHolder.BROKER,
            turn_token_version=0,
            pending_delivery_id=None,
            next_seq=fresh_seq + 1,
            created_at=now,
            updated_at=now,
            worktree_path=worktree_path,
        )

    async def cancel_conversation_atomic(
        self,
        conversation_id: str,
        cancel_event: Event,
    ) -> None:
        """Atomically set status='aborted' AND append the cancel event.

        Proactively fixed pre-R7 — same symmetric-defect pattern. A crash
        between set_status and append_event left an aborted conversation
        with no transcript record of why.
        """
        meta_json = (
            cancel_event.metadata.model_dump_json() if cancel_event.metadata else None
        )
        now_iso = _iso(utcnow())
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            await self._conn.execute(
                """UPDATE conversations
                     SET status=?, phase=?, updated_at=?
                   WHERE conversation_id=?""",
                ("aborted", Phase.ABORTED.value, now_iso, conversation_id),
            )
            fresh_seq = await self._alloc_seq_in_txn(conversation_id)
            event_to_insert = cancel_event.model_copy(update={"seq": fresh_seq})
            await self._conn.execute(
                """INSERT INTO events
                     (event_id, conversation_id, seq, round, phase, sender, recipient,
                      kind, content, content_hash, metadata_json, requires_reply,
                      in_reply_to_idempotency_key, observed_turn_token_version,
                      created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_to_insert.event_id, event_to_insert.conversation_id,
                    event_to_insert.seq, event_to_insert.round,
                    event_to_insert.phase.value, event_to_insert.sender.value,
                    event_to_insert.recipient.value,
                    event_to_insert.kind.value, event_to_insert.content,
                    event_to_insert.content_hash,
                    meta_json, int(event_to_insert.requires_reply),
                    event_to_insert.in_reply_to_idempotency_key,
                    event_to_insert.observed_turn_token_version,
                    _iso(event_to_insert.created_at),
                ),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def fail_turn_atomic(
        self,
        outbox_row: OutboxRow,
        error_event: Event,
        *,
        worker_loss_payload: dict[str, Any] | None = None,
        requesting_worker: TurnHolder | None = None,
    ) -> str | None:
        """Atomically mark an outbox row FAILED + append the error event +
        (optionally) raise a `worker_loss` approval.

        Round-6 codex P0 `review-mpfa8dcn-c2jnpw` flagged the worker-failure
        paths in `Broker.deliver_turn_to` as the seventh symmetric-defect axis:
        the timeout path updated outbox→FAILED then created the approval in
        two separate commits, and the validation-failure path appended the
        error event and updated outbox without ever creating an approval —
        so recovery (which skips FAILED rows) couldn't surface the stuck
        conversation either. This method bundles all three writes in one
        `BEGIN IMMEDIATE` transaction with explicit rollback on failure.

        Pass `worker_loss_payload=None` to skip approval creation (use this
        only when the failure is already operator-visible some other way —
        the default is to always create the approval).

        Returns the new approval_id, or None if `worker_loss_payload` was
        None.
        """
        meta_json = (
            error_event.metadata.model_dump_json() if error_event.metadata else None
        )
        now = _iso(utcnow())
        approval_id: str | None = None
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. Append the error event.
            fresh_seq = await self._alloc_seq_in_txn(error_event.conversation_id)
            event_to_insert = error_event.model_copy(update={"seq": fresh_seq})
            await self._conn.execute(
                """INSERT INTO events
                     (event_id, conversation_id, seq, round, phase, sender, recipient,
                      kind, content, content_hash, metadata_json, requires_reply,
                      in_reply_to_idempotency_key, observed_turn_token_version,
                      created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_to_insert.event_id, event_to_insert.conversation_id,
                    event_to_insert.seq, event_to_insert.round,
                    event_to_insert.phase.value, event_to_insert.sender.value,
                    event_to_insert.recipient.value,
                    event_to_insert.kind.value, event_to_insert.content,
                    event_to_insert.content_hash,
                    meta_json, int(event_to_insert.requires_reply),
                    event_to_insert.in_reply_to_idempotency_key,
                    event_to_insert.observed_turn_token_version,
                    _iso(event_to_insert.created_at),
                ),
            )
            # 2. Transition the outbox row to FAILED.
            await self._conn.execute(
                """UPDATE outbox
                     SET status=?, attempt_count=?, next_retry_at=?, last_error=?,
                         sent_at=?, delivered_at=?, answered_at=?, reply_event_id=?
                   WHERE delivery_id=?""",
                (
                    outbox_row.status.value, outbox_row.attempt_count,
                    _iso(outbox_row.next_retry_at) if outbox_row.next_retry_at else None,
                    outbox_row.last_error,
                    _iso(outbox_row.sent_at) if outbox_row.sent_at else None,
                    _iso(outbox_row.delivered_at) if outbox_row.delivered_at else None,
                    _iso(outbox_row.answered_at) if outbox_row.answered_at else None,
                    outbox_row.reply_event_id,
                    outbox_row.delivery_id,
                ),
            )
            # 3. Raise a worker_loss approval, unless caller said skip.
            if worker_loss_payload is not None:
                approval_id = _new_id("appr")
                await self._conn.execute(
                    """INSERT INTO approvals
                         (approval_id, conversation_id, category, requesting_worker,
                          payload_json, decision, requested_at, decided_at, decided_by)
                       VALUES (?,?,?,?,?,NULL,?,NULL,NULL)""",
                    (
                        approval_id, outbox_row.conversation_id, "worker_loss",
                        requesting_worker.value if requesting_worker else None,
                        json.dumps(worker_loss_payload),
                        now,
                    ),
                )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return approval_id

    async def decide_approval_atomic(
        self,
        approval_id: str,
        *,
        decision: str,
        decided_by: str | None,
        decision_event: Event,
        phase: Phase | None = None,
        status: str | None = None,
    ) -> None:
        """Atomically decide an approval + append audit event + transition phase/status.

        Round-5 codex P0 `review-mpf9wsgb-imyv0u` flagged the split-commit
        pattern in `Broker.approve` (decide → append event → set phase) as
        the sixth symmetric-defect axis. A crash after the decide write left
        the approval no-longer-pending while the conversation was still in
        `consensus`, creating a durable dead-end on the human approval gate
        (decisions on already-decided approvals raise).

        Wraps all three writes (plus the implicit seq allocation for the
        audit event) in one `BEGIN IMMEDIATE` transaction with explicit
        rollback on failure. Validates that the approval exists and is
        pending before mutating; on rollback, the approval remains pending
        so the user can re-decide.

        `phase` and `status` are optional — if `None`, those rows are not
        updated. Pass them when the approval category demands a transition.
        """
        meta_json = (
            decision_event.metadata.model_dump_json() if decision_event.metadata else None
        )
        now = _iso(utcnow())
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. Validate approval exists and is pending.
            async with self._conn.execute(
                "SELECT decision FROM approvals WHERE approval_id=?",
                (approval_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"approval not found: {approval_id}")
            prior = None if row[0] is None else str(row[0])
            if prior is not None:
                raise ValueError(
                    f"approval {approval_id} already decided: {prior!r}"
                )
            # 2. Record the decision.
            await self._conn.execute(
                """UPDATE approvals
                      SET decision=?, decided_at=?, decided_by=?
                    WHERE approval_id=?""",
                (decision, now, decided_by, approval_id),
            )
            # 3. Allocate seq + append the audit event in this transaction.
            fresh_seq = await self._alloc_seq_in_txn(decision_event.conversation_id)
            event_to_insert = decision_event.model_copy(update={"seq": fresh_seq})
            await self._conn.execute(
                """INSERT INTO events
                     (event_id, conversation_id, seq, round, phase, sender, recipient,
                      kind, content, content_hash, metadata_json, requires_reply,
                      in_reply_to_idempotency_key, observed_turn_token_version,
                      created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_to_insert.event_id, event_to_insert.conversation_id,
                    event_to_insert.seq, event_to_insert.round,
                    event_to_insert.phase.value, event_to_insert.sender.value,
                    event_to_insert.recipient.value,
                    event_to_insert.kind.value, event_to_insert.content,
                    event_to_insert.content_hash,
                    meta_json, int(event_to_insert.requires_reply),
                    event_to_insert.in_reply_to_idempotency_key,
                    event_to_insert.observed_turn_token_version,
                    _iso(event_to_insert.created_at),
                ),
            )
            # 4. Apply category-specific transition (if requested).
            if phase is not None:
                await self._conn.execute(
                    "UPDATE conversations SET phase=?, updated_at=? WHERE conversation_id=?",
                    (phase.value, now, decision_event.conversation_id),
                )
            if status is not None:
                await self._conn.execute(
                    "UPDATE conversations SET status=?, updated_at=? WHERE conversation_id=?",
                    (status, now, decision_event.conversation_id),
                )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def decide_approval(
        self, approval_id: str, *, decision: str, decided_by: str | None
    ) -> None:
        """Mark an approval decided. Raises if missing or already decided.

        Round-4 codex review flagged the blind UPDATE pattern as part of
        the dead-end approval P0; now we verify the row exists and is
        pending before mutating.
        """
        existing = await self.get_approval(approval_id)
        if existing is None:
            raise KeyError(f"approval not found: {approval_id}")
        _, _, _, prior_decision = existing
        if prior_decision is not None:
            raise ValueError(
                f"approval {approval_id} already decided: {prior_decision!r}"
            )
        await self._conn.execute(
            """UPDATE approvals
                  SET decision=?, decided_at=?, decided_by=?
                WHERE approval_id=?""",
            (decision, _iso(utcnow()), decided_by, approval_id),
        )
        await self._conn.commit()

    async def pending_approval_for(self, conversation_id: str) -> str | None:
        async with self._conn.execute(
            """SELECT approval_id FROM approvals
                WHERE conversation_id=? AND decision IS NULL
                ORDER BY requested_at LIMIT 1""",
            (conversation_id,),
        ) as cur:
            r = await cur.fetchone()
        return str(r[0]) if r else None

    # ---------- Sessions ------------------------------------------------------

    async def upsert_session(
        self,
        *,
        conversation_id: str,
        worker: TurnHolder,
        session_id: str,
        state: str,
        permission_mode: str,
        pid: int | None = None,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO sessions
                 (conversation_id, worker, session_id, pid, state,
                  permission_mode, last_seen_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(conversation_id, worker) DO UPDATE SET
                 session_id=excluded.session_id,
                 pid=excluded.pid,
                 state=excluded.state,
                 permission_mode=excluded.permission_mode,
                 last_seen_at=excluded.last_seen_at""",
            (
                conversation_id, worker.value, session_id, pid, state,
                permission_mode, _iso(utcnow()),
            ),
        )
        await self._conn.commit()

    async def get_session(
        self, conversation_id: str, worker: TurnHolder
    ) -> tuple[str, str, str] | None:
        """Return (session_id, state, permission_mode) or None."""
        async with self._conn.execute(
            """SELECT session_id, state, permission_mode
                 FROM sessions WHERE conversation_id=? AND worker=?""",
            (conversation_id, worker.value),
        ) as cur:
            r = await cur.fetchone()
        return (str(r[0]), str(r[1]), str(r[2])) if r else None
