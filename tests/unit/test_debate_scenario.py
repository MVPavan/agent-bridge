"""Five-round debate scenario test.

Per architecture.md §3.4 and CP8 verification: a 5-round debate scenario
produces a consensus artifact and stops at the correct round.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from agent_bridge.events import (
    ConsensusSignal,
    Event,
    EventKind,
    EventMetadata,
    Phase,
    Worker,
)
from agent_bridge.protocol import (
    can_transition,
    needs_user_escalation,
    planning_consensus_reached,
    planning_should_force_consensus,
)


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _ev(
    seq: int,
    sender: Worker,
    recipient: Worker,
    kind: EventKind,
    content: str,
    *,
    round_: int,
    consensus_signal: ConsensusSignal | None = None,
) -> Event:
    return Event(
        event_id=f"evt_{seq:04d}",
        conversation_id="conv_5round",
        seq=seq,
        round=round_,
        phase=Phase.PLANNING,
        sender=sender,
        recipient=recipient,
        kind=kind,
        content=content,
        content_hash=_hash(content),
        requires_reply=False,
        metadata=EventMetadata(consensus_signal=consensus_signal),
        created_at=datetime.now(UTC),
    )


def test_five_round_debate_reaches_consensus() -> None:
    """Simulate 5 alternating rounds ending in agreement from both workers."""
    events: list[Event] = []
    seq = 0
    max_rounds = 6

    # Round 1: Codex proposes, Claude critiques.
    seq += 1
    events.append(_ev(seq, Worker.WORKER_B, Worker.WORKER_A, EventKind.PROPOSAL,
                      "Extract auth refresh into AuthSessionManager.", round_=1))
    seq += 1
    events.append(_ev(seq, Worker.WORKER_A, Worker.WORKER_B, EventKind.CRITIQUE,
                      "AuthSessionManager couples concerns; suggest two smaller classes.", round_=1))

    # Round 2: Codex revises, Claude requests clarification.
    seq += 1
    events.append(_ev(seq, Worker.WORKER_B, Worker.WORKER_A, EventKind.PROPOSAL,
                      "Split into AuthTokenStore and AuthRefresher.", round_=2))
    seq += 1
    events.append(_ev(seq, Worker.WORKER_A, Worker.WORKER_B, EventKind.CLARIFICATION_REQUEST,
                      "Where does AuthRefresher live in package layout?", round_=2))

    # Round 3: Codex clarifies, Claude critiques tests.
    seq += 1
    events.append(_ev(seq, Worker.WORKER_B, Worker.WORKER_A, EventKind.CLARIFICATION_REPLY,
                      "src/auth/refresher.py with companion test under tests/auth/.", round_=3))
    seq += 1
    events.append(_ev(seq, Worker.WORKER_A, Worker.WORKER_B, EventKind.CRITIQUE,
                      "Add an integration test exercising token rotation under load.", round_=3))

    # Round 4: Codex amends, Claude warming.
    seq += 1
    events.append(_ev(seq, Worker.WORKER_B, Worker.WORKER_A, EventKind.PROPOSAL,
                      "Final plan + integration test design.", round_=4))
    seq += 1
    events.append(_ev(seq, Worker.WORKER_A, Worker.WORKER_B, EventKind.AGREEMENT,
                      "LGTM for the layout. One nit on naming.", round_=4,
                      consensus_signal=ConsensusSignal.AGREED))

    # Round 5: Codex resolves the nit, both AGREED.
    seq += 1
    events.append(_ev(seq, Worker.WORKER_B, Worker.WORKER_A, EventKind.AGREEMENT,
                      "Naming nit accepted. Final.", round_=5,
                      consensus_signal=ConsensusSignal.AGREED))

    # Consensus reached BEFORE max rounds.
    round_count = 5
    assert planning_consensus_reached(events, round_count=round_count, max_rounds=max_rounds)
    assert not planning_should_force_consensus(round_count=round_count, max_rounds=max_rounds)
    assert not needs_user_escalation(events, round_count=round_count, max_rounds=max_rounds)

    # Phase transition: planning → consensus is allowed and is the next move.
    assert can_transition(Phase.PLANNING, Phase.CONSENSUS)


def test_five_round_debate_without_agreement_escalates_at_max_rounds() -> None:
    """Same alternating shape, but no one ever signals AGREED. Must escalate."""
    events: list[Event] = []
    seq = 0
    max_rounds = 5

    for r in range(1, max_rounds + 1):
        seq += 1
        events.append(_ev(seq, Worker.WORKER_B, Worker.WORKER_A, EventKind.PROPOSAL,
                          f"proposal round {r}", round_=r))
        seq += 1
        events.append(_ev(seq, Worker.WORKER_A, Worker.WORKER_B, EventKind.REJECTION,
                          f"rejected round {r}", round_=r,
                          consensus_signal=ConsensusSignal.DISAGREE))

    assert not planning_consensus_reached(events, round_count=max_rounds, max_rounds=max_rounds)
    assert planning_should_force_consensus(round_count=max_rounds, max_rounds=max_rounds)
    assert needs_user_escalation(events, round_count=max_rounds, max_rounds=max_rounds)
