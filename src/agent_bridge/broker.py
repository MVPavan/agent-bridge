"""Broker orchestration loop.

Spec: docs/architecture.md §2 + §3 + §5.

The Broker is the long-running process that owns the conversation. It:

- Spawns Claude #2 (Shape A permission boundary via PreToolUse hook).
- Spawns Codex (Shape B per-turn capability declaration via `codex exec`).
- Routes messages worker_a ↔ broker ↔ worker_b with idempotency keys.
- Enforces the outbox FSM (pending → sent → delivered → answered).
- Persists everything to SQLite via Store.
- Surfaces approvals to Claude #1 via MCP (broker_backend.BrokerBackend).
- Recovers from crash by reading active conversations and re-dispatching
  outbox rows per §5.2.

Scope of THIS module: orchestration. Persistence is store.py. Workers are
claude_worker.py / codex_worker.py. Permission boundary is permission.py.
Protocol stop rules are protocol.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

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
from .outbox import (
    advance_outbox,
    recovery_action_for,
    validate_worker_reply,
)
from .permission import (
    PermissionCallback,
    PermissionDecision,
    PermissionDecisionKind,
)
from .protocol import (
    assert_transition,
    is_terminal,
    needs_user_escalation,
    planning_consensus_reached,
    planning_should_force_consensus,
)
from .store import OutboxRecipient, Store

log = structlog.get_logger(__name__)


# ---------- Local helpers -----------------------------------------------------


def _content_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:12]}"


def _new_delivery_id() -> str:
    return f"dlv_{uuid.uuid4().hex[:12]}"


def _new_idempotency_key() -> str:
    return f"ik_{uuid.uuid4().hex[:16]}"


# Bridge-ack marker the worker is instructed to echo at the end of its reply.
# The broker validates the worker-supplied values against the broker-known
# pending delivery (§3.5 step 4). Without the marker the reply is quarantined.
_BRIDGE_ACK_RE = re.compile(r"<<BRIDGE-ACK\s+ik=([A-Za-z0-9_\-]+)\s+ver=(\d+)>>")


def parse_bridge_ack(reply_text: str) -> tuple[str | None, int | None]:
    """Extract `(idempotency_key, turn_token_version)` from worker reply text.

    Workers are instructed to echo the bridge-ack marker at the end of their
    reply. If the marker is missing or malformed this returns `(None, None)`
    and the broker will reject the reply per §3.5 step 4 validator. This is
    the fix for codex P0 `review-mpf7lig2-p6fxyi` — without it, the broker
    was self-certifying its own assignment and stale/stray replies could be
    accepted as the current turn.
    """
    m = _BRIDGE_ACK_RE.search(reply_text)
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


# ---------- WorkerDriver protocol --------------------------------------------
#
# The broker talks to workers through this abstract surface. Real adapters
# are in claude_worker.py and codex_worker.py; tests can pass mocks.


@dataclass(frozen=True)
class WorkerSend:
    """One message the broker hands to a worker."""

    conversation_id: str
    idempotency_key: str
    turn_token_version: int
    prompt: str


@dataclass(frozen=True)
class WorkerReply:
    """One reply a worker returned to the broker.

    Concrete drivers MUST populate `in_reply_to_idempotency_key` and
    `observed_turn_token_version` by parsing the bridge-ack marker out of
    the worker's reply text via `parse_bridge_ack`. If either field is
    `None`, the broker rejects the reply (§3.5 step 4). This prevents the
    broker from self-certifying its own assignment, which was the codex P0
    `review-mpf7lig2-p6fxyi`.
    """

    text: str
    session_or_thread_id: str | None
    raw_events: tuple[Any, ...] = ()
    error: str | None = None
    in_reply_to_idempotency_key: str | None = None
    observed_turn_token_version: int | None = None


class WorkerDriver:
    """Interface every worker adapter implements.

    Implementations: `ClaudeWorkerDriver` (Shape A — claude-agent-sdk),
    `CodexWorkerDriver` (Shape B — codex exec subprocess).
    """

    worker_id: TurnHolder

    async def send(self, msg: WorkerSend) -> WorkerReply:
        raise NotImplementedError


# ---------- Approval surface fed back to the MCP cockpit ---------------------


@dataclass(frozen=True)
class PendingApproval:
    """Lightweight view of an unresolved approval, surfaced via the MCP layer."""

    approval_id: str
    category: str
    payload: dict[str, Any]
    requested_at: datetime


# ---------- The broker --------------------------------------------------------


@dataclass(frozen=True)
class BrokerConfig:
    """All knobs collected in one place for ease of override in tests."""

    max_planning_rounds: int = 6
    max_fix_loops: int = 4
    turn_timeout_seconds: float = 300.0


class Broker:
    """The orchestrator. One instance per process; many conversations per instance."""

    def __init__(
        self,
        *,
        store: Store,
        worker_a: WorkerDriver,
        worker_b: WorkerDriver,
        config: BrokerConfig | None = None,
    ) -> None:
        self.store = store
        self.worker_a = worker_a
        self.worker_b = worker_b
        self.config = config or BrokerConfig()
        self._lock = asyncio.Lock()  # serializes turn advances per process

    # -------- Event helpers --------

    async def _append(
        self,
        *,
        conversation_id: str,
        sender: TurnHolder,
        recipient: TurnHolder,
        kind: EventKind,
        content: str,
        phase: Phase,
        round_: int,
        requires_reply: bool = False,
        metadata: EventMetadata | None = None,
        in_reply_to_idempotency_key: str | None = None,
        observed_turn_token_version: int | None = None,
    ) -> Event:
        seq = await self.store.next_seq(conversation_id)
        ev = Event(
            event_id=_new_event_id(),
            conversation_id=conversation_id,
            seq=seq,
            round=round_,
            phase=phase,
            sender=sender,
            recipient=recipient,
            kind=kind,
            content=content,
            content_hash=_content_hash(content),
            requires_reply=requires_reply,
            metadata=metadata or EventMetadata(),
            in_reply_to_idempotency_key=in_reply_to_idempotency_key,
            observed_turn_token_version=observed_turn_token_version,
            created_at=utcnow(),
        )
        await self.store.append_event(ev)
        return ev

    # -------- Public surface (MCP backend) --------

    async def dispatch_goal(
        self, goal: str, *, worktree_path: Path | None = None
    ) -> str:
        # Atomically create the conversation + its seed event. Proactive
        # symmetric-defect fix — used to be two separate commits.
        seed_content = f"Goal: {goal}"
        conv = await self.store.create_conversation_atomic(
            goal=goal,
            seed_event_id=_new_event_id(),
            seed_content=seed_content,
            seed_content_hash=_content_hash(seed_content),
            max_rounds=self.config.max_planning_rounds,
            worktree_path=worktree_path,
        )
        log.info("dispatch_goal", conversation_id=conv.conversation_id, goal=goal)
        return conv.conversation_id

    async def get_status_snapshot(
        self, conversation_id: str
    ) -> dict[str, Any] | None:
        conv = await self.store.get_conversation(conversation_id)
        if conv is None:
            return None
        pending = await self.store.pending_approval_for(conversation_id)
        last_events = await self.store.list_events(conversation_id, limit=1000)
        last_seq = last_events[-1].seq if last_events else 0
        return {
            "conversation_id": conv.conversation_id,
            "phase": conv.phase,
            "round": conv.round,
            "current_turn": conv.current_turn,
            "pending_approval_id": pending,
            "last_seq": last_seq,
            "status": conv.status,
        }

    async def approve(
        self,
        approval_id: str,
        decision: str,
        *,
        decided_by: str | None = None,
        note: str | None = None,
    ) -> None:
        """Record an approval decision AND advance the conversation per category.

        Round-4 codex P0 (`review-mpfa3kk6-...`) flagged the old version as a
        dead end: it stamped the row decided and logged, but did NOT inspect
        the category or transition the conversation. The cockpit could
        report "approved" while the conversation stayed stuck in `consensus`.

        Category-specific transitions (decision in `approved`/`rejected`):

        - `consensus_to_implement` + approved → phase: implementing
        - `consensus_to_implement` + rejected → phase: aborted
        - `worker_loss` (either decision) → status: aborted
          (retry-from-scratch is a future-work follow-up; the safe default
          today is to surface the loss and let the operator decide next steps
          outside the loop)
        - other categories → just record the decision; no state transition

        Every decision also appends an `APPROVAL_DECISION` event to the log
        so the transcript shows what happened. Decisions on missing or
        already-decided approvals raise (via `Store.decide_approval`).
        """
        if decision not in ("approved", "rejected"):
            raise ValueError(
                f"decision must be 'approved' or 'rejected', got {decision!r}"
            )
        info = await self.store.get_approval(approval_id)
        if info is None:
            raise KeyError(f"approval not found: {approval_id}")
        conversation_id, category, _payload_json, prior = info
        if prior is not None:
            raise ValueError(
                f"approval {approval_id} already decided: {prior!r}"
            )
        conv = await self.store.get_conversation(conversation_id)
        if conv is None:
            raise KeyError(f"conversation not found: {conversation_id}")
        # Determine the category-specific transition (None means no change).
        target_phase: Phase | None = None
        target_status: str | None = None
        if category == "consensus_to_implement":
            if decision == "approved":
                target_phase = Phase.IMPLEMENTING
            else:
                target_phase = Phase.ABORTED
                target_status = "aborted"
        elif category == "worker_loss":
            target_phase = Phase.ABORTED
            target_status = "aborted"
        # Other categories don't drive a transition in the current dispatch path.

        # Build the audit event in the *current* phase (before any transition).
        audit_content = (
            f"approval {approval_id} (category={category}) {decision}"
            + (f": {note}" if note else "")
        )
        decision_event = Event(
            event_id=_new_event_id(),
            conversation_id=conversation_id,
            seq=1,  # overridden inside decide_approval_atomic
            round=conv.round,
            phase=conv.phase,
            sender=TurnHolder.USER,
            recipient=TurnHolder.BROKER,
            kind=EventKind.APPROVAL_DECISION,
            content=audit_content,
            content_hash=_content_hash(audit_content),
            requires_reply=False,
            metadata=EventMetadata(),
            created_at=utcnow(),
        )
        # Round-5 codex P0 `review-mpf9wsgb-imyv0u` flagged the old version
        # for splitting decide + event + transition across separate commits.
        # decide_approval_atomic puts all three in one BEGIN IMMEDIATE
        # transaction with explicit rollback on failure.
        await self.store.decide_approval_atomic(
            approval_id,
            decision=decision,
            decided_by=decided_by,
            decision_event=decision_event,
            phase=target_phase,
            status=target_status,
        )
        log.info(
            "approval_decided",
            approval_id=approval_id,
            category=category,
            decision=decision,
            conversation_id=conversation_id,
            target_phase=target_phase.value if target_phase else None,
            target_status=target_status,
            note=note,
        )

    async def cancel(self, conversation_id: str) -> None:
        # Atomically set status='aborted' + append the cancel event. Proactive
        # symmetric-defect fix — used to be two separate commits.
        cancel_event = Event(
            event_id=_new_event_id(),
            conversation_id=conversation_id,
            seq=1,  # overridden by Store.cancel_conversation_atomic
            round=0,
            phase=Phase.ABORTED,
            sender=TurnHolder.USER,
            recipient=TurnHolder.BROKER,
            kind=EventKind.SYSTEM_NOTE,
            content="user requested cancel",
            content_hash=_content_hash("user requested cancel"),
            requires_reply=False,
            metadata=EventMetadata(),
            created_at=utcnow(),
        )
        await self.store.cancel_conversation_atomic(conversation_id, cancel_event)

    async def tail_transcript(
        self, conversation_id: str, after_event_id: str | None = None
    ) -> list[Event]:
        # Resolve after_event_id -> after_seq.
        # Round-8 codex P1 `review-mpfb42vk-lajla7` flagged the previous
        # implementation as broken: it called `list_events(limit=1)` which
        # only returns the FIRST event, so any cursor other than event #1
        # silently fell back to seq=0 and the cockpit got the full
        # transcript replayed. The direct lookup below correctly resolves
        # any event_id.
        after_seq = 0
        if after_event_id:
            resolved = await self.store.get_event_seq(
                conversation_id, after_event_id
            )
            if resolved is not None:
                after_seq = resolved
        return await self.store.list_events(conversation_id, after_seq=after_seq)

    # -------- Per-turn machinery (the §3.5 outbox FSM applied) --------

    async def deliver_turn_to(
        self,
        conversation_id: str,
        recipient: TurnHolder,
        prompt: str,
        *,
        phase: Phase,
        round_: int,
    ) -> Event:
        """Enqueue + send + collect a reply from `recipient`.

        Implements §3.5 happy path: pending → sent → delivered/answered.
        Validates the worker's reply with `validate_worker_reply` before
        advancing the turn (§3.5 step 4).
        """
        if recipient not in (TurnHolder.WORKER_A, TurnHolder.WORKER_B):
            raise ValueError(f"deliver_turn_to recipient must be a worker, got {recipient}")
        recipient_typed: OutboxRecipient = (
            TurnHolder.WORKER_A
            if recipient is TurnHolder.WORKER_A
            else TurnHolder.WORKER_B
        )

        conv = await self.store.get_conversation(conversation_id)
        if conv is None:
            raise KeyError(conversation_id)

        # Steps 1+2 atomic: append event, enqueue outbox, transition turn token,
        # all in one SQLite transaction. The previous three-call sequence
        # (append_event → enqueue_outbox → set_turn) committed independently,
        # so a crash between any two of the writes left state ambiguous.
        # Codex review `review-mpf7lig2-p6fxyi` flagged this as a P0; this
        # uses Store.dispatch_turn to restore the §3.5 atomicity invariant.
        delivery_id = _new_delivery_id()
        idempotency_key = _new_idempotency_key()
        now = utcnow()
        # dispatch_turn allocates seq inside its own transaction (P1 fix from
        # round-3 review); the seq we pass here is a placeholder that gets
        # overridden by the in-txn allocator.
        prompt_event = Event(
            event_id=_new_event_id(),
            conversation_id=conversation_id,
            seq=1,  # overridden by Store.dispatch_turn
            round=round_,
            phase=phase,
            sender=TurnHolder.BROKER,
            recipient=recipient,
            kind=EventKind.PROPOSAL,
            content=prompt,
            content_hash=_content_hash(prompt),
            requires_reply=True,
            metadata=EventMetadata(),
            created_at=now,
        )
        outbox = OutboxRow(
            delivery_id=delivery_id,
            conversation_id=conversation_id,
            event_id=prompt_event.event_id,
            recipient=recipient_typed,
            idempotency_key=idempotency_key,
            status=OutboxStatus.PENDING,
            enqueued_at=now,
        )
        version = await self.store.dispatch_turn(
            prompt_event, outbox, turn_target=recipient
        )

        # 3. Transition to SENT, then dispatch the worker.
        outbox = advance_outbox(
            outbox,
            to_status=OutboxStatus.SENT,
            now=utcnow(),
            attempt_count_delta=1,
        )
        await self.store.update_outbox(outbox)

        driver = self.worker_a if recipient is TurnHolder.WORKER_A else self.worker_b
        send = WorkerSend(
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            turn_token_version=version,
            prompt=self._wrap_prompt_with_correlation(prompt, idempotency_key, version),
        )
        try:
            reply = await asyncio.wait_for(
                driver.send(send), timeout=self.config.turn_timeout_seconds
            )
        except TimeoutError:
            # Round-6 codex P0 `review-mpfa8dcn-c2jnpw`: outbox→FAILED + the
            # worker_loss approval used to commit separately. A crash between
            # them left a FAILED outbox row with no operator-visible approval,
            # and recovery skips FAILED rows. fail_turn_atomic bundles error
            # event + outbox update + approval in one transaction.
            now_fail = utcnow()
            outbox = advance_outbox(
                outbox,
                to_status=OutboxStatus.FAILED,
                now=now_fail,
                last_error="turn timed out",
            )
            err_event = Event(
                event_id=_new_event_id(),
                conversation_id=conversation_id,
                seq=1,  # overridden by Store.fail_turn_atomic
                round=round_,
                phase=phase,
                sender=TurnHolder.BROKER,
                recipient=recipient,
                kind=EventKind.ERROR,
                content="turn timed out",
                content_hash=_content_hash("turn timed out"),
                requires_reply=False,
                metadata=EventMetadata(),
                created_at=now_fail,
            )
            await self.store.fail_turn_atomic(
                outbox,
                err_event,
                worker_loss_payload={"worker": recipient.value, "reason": "timeout"},
                requesting_worker=recipient,
            )
            raise

        # 4. Persist session id if surfaced (turn 1 / new session).
        if reply.session_or_thread_id:
            await self.store.upsert_session(
                conversation_id=conversation_id,
                worker=recipient,
                session_id=reply.session_or_thread_id,
                state="live",
                permission_mode=self._declared_permission_mode(recipient),
            )

        # 5. Validate the reply per §3.5 step 4.
        # Reply correlation: the worker is instructed (via §_wrap_prompt_with_correlation)
        # to echo a `<<BRIDGE-ACK ik=<key> ver=<n>>>` marker at the end of its
        # reply. The driver SHOULD populate `reply.in_reply_to_idempotency_key`
        # and `reply.observed_turn_token_version` by parsing that marker; if it
        # didn't, the broker parses the text itself as a fallback. Either way
        # the values handed to validate_worker_reply are the WORKER-SUPPLIED
        # values, NOT the broker's own assignment. This is what makes the
        # validator non-tautological (codex P0 `review-mpf7lig2-p6fxyi`).
        worker_ik = reply.in_reply_to_idempotency_key
        worker_ver = reply.observed_turn_token_version
        if worker_ik is None and worker_ver is None:
            worker_ik, worker_ver = parse_bridge_ack(reply.text)
        pending_row = await self.store.get_outbox(delivery_id)
        conv2 = await self.store.get_conversation(conversation_id)
        assert pending_row is not None and conv2 is not None
        validation = validate_worker_reply(
            pending_row=pending_row,
            conversation_turn_token_version=conv2.turn_token_version,
            reply_in_reply_to_idempotency_key=worker_ik,
            reply_observed_turn_token_version=worker_ver,
        )
        if not validation.accept:
            # Round-6 codex P0 `review-mpfa8dcn-c2jnpw`: previously this path
            # appended the error event and updated outbox to FAILED in two
            # commits and never created an approval — recovery skips FAILED
            # rows, so the conversation could remain `running` with no
            # operator-visible path forward. fail_turn_atomic now bundles
            # error event + outbox update + worker_loss approval atomically.
            now_fail = utcnow()
            err_event = Event(
                event_id=_new_event_id(),
                conversation_id=conversation_id,
                seq=1,  # overridden by Store.fail_turn_atomic
                round=round_,
                phase=phase,
                sender=recipient,
                recipient=TurnHolder.BROKER,
                kind=EventKind.ERROR,
                content=f"reply validation failed: {validation.reason}",
                content_hash=_content_hash(
                    f"reply validation failed: {validation.reason}"
                ),
                requires_reply=False,
                metadata=EventMetadata(),
                created_at=now_fail,
            )
            outbox = advance_outbox(
                outbox,
                to_status=OutboxStatus.FAILED,
                now=now_fail,
                last_error=validation.reason or "validation failed",
            )
            await self.store.fail_turn_atomic(
                outbox,
                err_event,
                worker_loss_payload={
                    "worker": recipient.value,
                    "reason": "reply_validation_failed",
                    "detail": validation.reason,
                },
                requesting_worker=recipient,
            )
            raise RuntimeError(f"worker reply rejected: {validation.reason}")

        # 6. Happy path: append the reply event + transition outbox → ANSWERED
        # + hand the turn back to the broker, ALL in one SQLite transaction.
        # The completion path used to split this into three durable writes,
        # mirroring the enqueue bug — codex round-2 review
        # `review-mpf8nsmd-w82pki` flagged it as a P0. Store.complete_turn
        # makes the completion atomic.
        now_complete = utcnow()
        # seq overridden by Store.complete_turn's in-transaction allocator.
        reply_event = Event(
            event_id=_new_event_id(),
            conversation_id=conversation_id,
            seq=1,  # overridden by Store.complete_turn
            round=round_,
            phase=phase,
            sender=recipient,
            recipient=TurnHolder.BROKER,
            kind=EventKind.CRITIQUE,
            content=reply.text,
            content_hash=_content_hash(reply.text),
            requires_reply=False,
            metadata=EventMetadata(),
            in_reply_to_idempotency_key=idempotency_key,
            observed_turn_token_version=version,
            created_at=now_complete,
        )
        outbox = advance_outbox(
            outbox,
            to_status=OutboxStatus.ANSWERED,
            now=now_complete,
            reply_event_id=reply_event.event_id,
        )
        await self.store.complete_turn(
            reply_event, outbox, next_turn=TurnHolder.BROKER
        )
        return reply_event

    def _wrap_prompt_with_correlation(
        self, prompt: str, idempotency_key: str, turn_token_version: int
    ) -> str:
        """Embed the correlation tokens in the prompt body AND instruct the
        worker to echo them back in a literal ack marker.

        Workers cannot natively populate the §3.1 envelope fields; the broker
        injects them in-band and requires the worker to echo the values in a
        machine-readable form at the end of its reply. The reply parser uses
        `parse_bridge_ack` to extract those echoed values; the broker then
        validates them against the broker-known pending delivery (§3.5 step
        4). A missing or mismatched ack causes the reply to be quarantined —
        which is the fix for codex P0 `review-mpf7lig2-p6fxyi`.
        """
        ack_line = f"<<BRIDGE-ACK ik={idempotency_key} ver={turn_token_version}>>"
        return (
            f"[broker_meta] idempotency_key={idempotency_key} "
            f"turn_token_version={turn_token_version}\n\n"
            f"{prompt}\n\n"
            f"BRIDGE PROTOCOL — end your reply with the literal line below "
            f"(verbatim, no surrounding text):\n"
            f"{ack_line}"
        )

    def _declared_permission_mode(self, worker: TurnHolder) -> str:
        if worker is TurnHolder.WORKER_A:
            return "claude.default+pre_tool_use_hook"
        return "codex.shape_b.per_turn_capability"

    # -------- Phase orchestration --------

    async def run_planning_loop(
        self,
        conversation_id: str,
        *,
        first_prompt_for_codex: str,
        first_prompt_for_claude_template: str,
    ) -> str:
        """Drive the planning phase: alternating Codex/Claude turns until
        consensus is reached or max_rounds is hit. Returns the consensus
        artifact text (or escalation reason).
        """
        conv = await self.store.get_conversation(conversation_id)
        assert conv is not None
        if conv.phase is not Phase.PLANNING:
            raise RuntimeError(
                f"planning loop requires phase=planning, got {conv.phase}"
            )

        proposal = first_prompt_for_codex
        for r in range(1, self.config.max_planning_rounds + 1):
            await self.store.increment_round(conversation_id)
            # Codex proposes
            codex_reply = await self.deliver_turn_to(
                conversation_id, TurnHolder.WORKER_B,
                proposal, phase=Phase.PLANNING, round_=r,
            )
            # Claude critiques
            claude_prompt = first_prompt_for_claude_template.format(
                codex_proposal=codex_reply.content
            )
            claude_reply = await self.deliver_turn_to(
                conversation_id, TurnHolder.WORKER_A,
                claude_prompt, phase=Phase.PLANNING, round_=r,
            )
            # Stop rules
            events = await self.store.list_events(conversation_id)
            if planning_consensus_reached(
                events,
                round_count=r,
                max_rounds=self.config.max_planning_rounds,
            ):
                await self._transition_phase(conversation_id, Phase.CONSENSUS)
                return claude_reply.content
            if planning_should_force_consensus(
                r, self.config.max_planning_rounds
            ):
                if needs_user_escalation(
                    events,
                    round_count=r,
                    max_rounds=self.config.max_planning_rounds,
                ):
                    await self.store.create_approval(
                        conversation_id=conversation_id,
                        category="consensus_to_implement",
                        payload={
                            "reason": "max_rounds reached without consensus",
                            "round": r,
                        },
                    )
                await self._transition_phase(conversation_id, Phase.CONSENSUS)
                return claude_reply.content
            proposal = (
                f"Worker A critiqued your proposal:\n\n{claude_reply.content}\n\n"
                "Revise the plan addressing the critique."
            )

        return "max_rounds exhausted"

    async def _transition_phase(
        self, conversation_id: str, to_phase: Phase
    ) -> None:
        conv = await self.store.get_conversation(conversation_id)
        assert conv is not None
        assert_transition(conv.phase, to_phase)
        await self.store.set_phase(conversation_id, to_phase)
        await self._append(
            conversation_id=conversation_id,
            sender=TurnHolder.BROKER,
            recipient=TurnHolder.USER,
            kind=EventKind.SYSTEM_NOTE,
            content=f"phase transition {conv.phase.value} → {to_phase.value}",
            phase=to_phase,
            round_=conv.round,
        )
        if is_terminal(to_phase):
            await self.store.set_status(
                conversation_id, "done" if to_phase is Phase.DONE else "aborted"
            )

    # -------- Recovery (§5.2) --------

    async def recover_on_startup(self) -> list[str]:
        """Re-attach all running conversations and re-dispatch outbox rows.

        Returns the list of conversation_ids that were recovered.
        """
        recovered: list[str] = []
        active = await self.store.list_active_conversations()
        for conv in active:
            rows = await self.store.list_outbox_needing_recovery(conv.conversation_id)
            for row in rows:
                action = recovery_action_for(row)
                log.info(
                    "recovery_action",
                    conversation_id=conv.conversation_id,
                    delivery_id=row.delivery_id,
                    status=row.status.value,
                    action=action,
                )
                # Round-7 codex P1 `review-mpfaq...`: skip if an unresolved
                # worker_loss approval already exists for this delivery_id.
                # Without this guard, repeated broker restarts produced N
                # duplicate approvals for the same lost turn.
                if action in (
                    "redispatch", "probe_and_reconcile"
                ) and await self.store.has_pending_worker_loss_for_delivery(
                    conv.conversation_id, row.delivery_id
                ):
                    log.info(
                        "recovery_skip_already_escalated",
                        conversation_id=conv.conversation_id,
                        delivery_id=row.delivery_id,
                    )
                    continue

                if action == "redispatch":
                    # Round-3 codex review `review-mpf9p59f-rrcc8u` flagged
                    # this branch as a P0: it previously just logged and
                    # passed, silently stranding pending/sent turns on
                    # restart. Same "symmetric defect" axis as P0-2 enqueue
                    # vs completion atomicity — applied to the recovery
                    # path. Match the delivered-side behavior: escalate via
                    # worker_loss so the user can decide retry vs abort,
                    # rather than leaving the conversation in-flight forever.
                    await self.store.create_approval(
                        conversation_id=conv.conversation_id,
                        category="worker_loss",
                        payload={
                            "delivery_id": row.delivery_id,
                            "idempotency_key": row.idempotency_key,
                            "outbox_status": row.status.value,
                            "reason": (
                                "broker restart caught a pending/sent outbox row; "
                                "the worker either never received the prompt or "
                                "never replied. Escalating for user decision."
                            ),
                        },
                    )
                elif action == "probe_and_reconcile":
                    # §3.5 delivered-but-unanswered: escalate worker_loss.
                    await self.store.create_approval(
                        conversation_id=conv.conversation_id,
                        category="worker_loss",
                        payload={
                            "delivery_id": row.delivery_id,
                            "idempotency_key": row.idempotency_key,
                            "reason": "delivered but no reply observed pre-restart",
                        },
                    )
            recovered.append(conv.conversation_id)
        return recovered

    async def _raise_worker_loss(
        self, conversation_id: str, worker: TurnHolder, reason: str
    ) -> None:
        await self.store.create_approval(
            conversation_id=conversation_id,
            category="worker_loss",
            payload={"worker": worker.value, "reason": reason},
            requesting_worker=worker,
        )

    # -------- Build a per-conversation PermissionCallback for Claude #2 --------

    def build_permission_callback(
        self,
        conversation_id: str,
        *,
        allowlist: tuple[str, ...] = (),
        denylist: tuple[str, ...] = (),
    ) -> PermissionCallback:
        """Return a callback the Claude worker passes to the SDK hook.

        Records every approval request as an `approval_request` event and
        decides synchronously by allowlist / denylist (the broker's
        deterministic policy). User-approval branches will plug in here later.
        """

        async def cb(
            *,
            worker: str,
            tool_name: str,
            tool_input: dict[str, object],
        ) -> PermissionDecision:
            # Record the request as an event for the audit trail.
            payload = {
                "worker": worker,
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
            await self._append(
                conversation_id=conversation_id,
                sender=TurnHolder.WORKER_A,
                recipient=TurnHolder.BROKER,
                kind=EventKind.APPROVAL_REQUEST,
                content=f"PreToolUse:{tool_name}",
                metadata=EventMetadata(),
                phase=Phase.PLANNING,  # broker-level event; phase doesn't gate enforcement
                round_=0,
            )
            # Denylist wins.
            for pattern in denylist:
                if pattern in tool_name or any(
                    pattern in str(v) for v in tool_input.values()
                ):
                    return PermissionDecision(
                        decision=PermissionDecisionKind.DENY,
                        reason=f"denylist match: {pattern}",
                    )
            # Allowlist permits, else deny by default.
            if tool_name in allowlist:
                return PermissionDecision(decision=PermissionDecisionKind.ALLOW)
            _ = payload  # captured intentionally for future user-approval path
            return PermissionDecision(
                decision=PermissionDecisionKind.DENY,
                reason=f"tool {tool_name!r} is not on the allowlist",
            )

        return cb
