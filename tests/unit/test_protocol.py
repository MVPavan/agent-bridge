"""Unit tests for the phase state machine and stop-rule predicates."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from agent_bridge.events import (
    ConsensusSignal,
    Event,
    EventKind,
    EventMetadata,
    Phase,
    Severity,
    Worker,
)
from agent_bridge.protocol import (
    ALLOWED_TRANSITIONS,
    TERMINAL,
    IllegalTransition,
    assert_transition,
    can_transition,
    is_terminal,
    needs_user_escalation,
    planning_consensus_reached,
    planning_should_force_consensus,
    review_should_finish,
)


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _ev(
    *,
    seq: int,
    sender: Worker,
    recipient: Worker,
    phase: Phase,
    kind: EventKind,
    content: str = "",
    consensus_signal: ConsensusSignal | None = None,
    severity: Severity | None = None,
    round_: int = 0,
) -> Event:
    return Event(
        event_id=f"evt_{seq:04d}",
        conversation_id="conv_0001",
        seq=seq,
        round=round_,
        phase=phase,
        sender=sender,
        recipient=recipient,
        kind=kind,
        content=content,
        content_hash=_hash(content),
        requires_reply=False,
        metadata=EventMetadata(
            consensus_signal=consensus_signal,
            severity=severity,
        ),
        created_at=datetime.now(UTC),
    )


# ---------- Transition tests ---------------------------------------------------


_SORTED_TRANSITIONS: list[tuple[Phase, Phase]] = sorted(
    ALLOWED_TRANSITIONS, key=lambda p: (p[0].value, p[1].value)
)


@pytest.mark.parametrize("frm,to", _SORTED_TRANSITIONS)
def test_allowed_transitions_pass(frm: Phase, to: Phase) -> None:
    assert can_transition(frm, to)
    assert_transition(frm, to)  # must not raise


def test_disallowed_transition_raises() -> None:
    # planning → done is not whitelisted (must go through consensus → implementing → reviewing → done)
    assert not can_transition(Phase.PLANNING, Phase.DONE)
    with pytest.raises(IllegalTransition):
        assert_transition(Phase.PLANNING, Phase.DONE)


def test_terminal_phases_have_no_outbound_transitions() -> None:
    for term in TERMINAL:
        for other in Phase:
            assert not can_transition(term, other), (
                f"terminal phase {term} should not have outbound {other}"
            )


def test_is_terminal() -> None:
    assert is_terminal(Phase.DONE)
    assert is_terminal(Phase.ABORTED)
    assert not is_terminal(Phase.PLANNING)


# ---------- Consensus tests ----------------------------------------------------


def test_consensus_when_both_workers_agree_within_max_rounds() -> None:
    events = [
        _ev(
            seq=1, sender=Worker.WORKER_B, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.PROPOSAL, content="propose X",
        ),
        _ev(
            seq=2, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.AGREEMENT, content="I agree", consensus_signal=ConsensusSignal.AGREED,
        ),
        _ev(
            seq=3, sender=Worker.WORKER_B, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.AGREEMENT, content="confirmed", consensus_signal=ConsensusSignal.AGREED,
        ),
    ]
    assert planning_consensus_reached(events, round_count=3, max_rounds=6)


def test_no_consensus_when_only_one_worker_agrees() -> None:
    events = [
        _ev(
            seq=1, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.AGREEMENT, content="ok", consensus_signal=ConsensusSignal.AGREED,
        ),
    ]
    assert not planning_consensus_reached(events, round_count=1, max_rounds=6)


def test_disagreement_overrides_earlier_agreement_for_consensus() -> None:
    events = [
        _ev(
            seq=1, sender=Worker.WORKER_B, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.AGREEMENT, content="ok", consensus_signal=ConsensusSignal.AGREED,
        ),
        _ev(
            seq=2, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.AGREEMENT, content="ok", consensus_signal=ConsensusSignal.AGREED,
        ),
        _ev(
            seq=3, sender=Worker.WORKER_B, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.REJECTION, content="actually no", consensus_signal=ConsensusSignal.DISAGREE,
        ),
    ]
    assert not planning_consensus_reached(events, round_count=3, max_rounds=6)


def test_max_rounds_forces_consensus_phase() -> None:
    assert planning_should_force_consensus(round_count=6, max_rounds=6)
    assert not planning_should_force_consensus(round_count=5, max_rounds=6)


# ---------- Review tests -------------------------------------------------------


def test_review_finishes_when_tests_pass_and_findings_resolved() -> None:
    events = [
        _ev(
            seq=1, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.REVIEWING,
            kind=EventKind.DIFF, content="diff body",
        ),
        _ev(
            seq=2, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.REVIEWING,
            kind=EventKind.TEST_RESULT, content="tests pass",
        ),
        _ev(
            seq=3, sender=Worker.WORKER_B, recipient=Worker.BROKER, phase=Phase.REVIEWING,
            kind=EventKind.REVIEW_FINDING, content="P1 something", severity=Severity.P1,
        ),
        _ev(
            seq=4, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.FIXING,
            kind=EventKind.FIX_RESPONSE, content="fixed",
        ),
    ]
    assert review_should_finish(events, fix_loop_count=1, max_fix_loops=4)


def test_review_does_not_finish_when_tests_have_not_passed() -> None:
    events = [
        _ev(
            seq=1, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.REVIEWING,
            kind=EventKind.DIFF, content="diff body",
        ),
    ]
    assert not review_should_finish(events, fix_loop_count=1, max_fix_loops=4)


def test_review_does_not_finish_when_open_findings_outnumber_fixes() -> None:
    events = [
        _ev(
            seq=1, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.REVIEWING,
            kind=EventKind.TEST_RESULT, content="tests pass",
        ),
        _ev(
            seq=2, sender=Worker.WORKER_B, recipient=Worker.BROKER, phase=Phase.REVIEWING,
            kind=EventKind.REVIEW_FINDING, content="P0 boom", severity=Severity.P0,
        ),
        _ev(
            seq=3, sender=Worker.WORKER_B, recipient=Worker.BROKER, phase=Phase.REVIEWING,
            kind=EventKind.REVIEW_FINDING, content="P1 minor", severity=Severity.P1,
        ),
    ]
    assert not review_should_finish(events, fix_loop_count=1, max_fix_loops=4)


# ---------- Escalation tests ---------------------------------------------------


def test_escalation_on_max_rounds_without_consensus() -> None:
    events = [
        _ev(
            seq=1, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.REJECTION, content="no", consensus_signal=ConsensusSignal.DISAGREE,
        ),
    ]
    assert needs_user_escalation(events, round_count=6, max_rounds=6)


def test_no_escalation_when_consensus_reached() -> None:
    events = [
        _ev(
            seq=1, sender=Worker.WORKER_A, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.AGREEMENT, content="ok", consensus_signal=ConsensusSignal.AGREED,
        ),
        _ev(
            seq=2, sender=Worker.WORKER_B, recipient=Worker.BROKER, phase=Phase.PLANNING,
            kind=EventKind.AGREEMENT, content="ok", consensus_signal=ConsensusSignal.AGREED,
        ),
    ]
    assert not needs_user_escalation(events, round_count=2, max_rounds=6)


def test_escalation_on_error_event() -> None:
    events = [
        _ev(
            seq=1, sender=Worker.BROKER, recipient=Worker.USER, phase=Phase.PLANNING,
            kind=EventKind.ERROR, content="worker died",
        ),
    ]
    assert needs_user_escalation(events, round_count=1, max_rounds=6)
