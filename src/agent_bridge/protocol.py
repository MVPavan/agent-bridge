"""Conversation phase state machine.

Spec: docs/architecture.md §2.3, §3.4, §6.3 (stop rules in the research doc
that the architecture inherits). Pure logic — no IO, no LLM.

The state machine is a strict whitelist of transitions. Any attempted move
that is not in the whitelist raises `IllegalTransition`.
"""

from __future__ import annotations

from collections.abc import Iterable

from .events import Event, EventKind, Phase, Worker

# Whitelist of allowed (from_phase, to_phase) pairs.
ALLOWED_TRANSITIONS: frozenset[tuple[Phase, Phase]] = frozenset(
    {
        # planning loop converges to consensus or hits max rounds
        (Phase.PLANNING, Phase.CONSENSUS),
        # user approves the consensus → implementation starts
        (Phase.CONSENSUS, Phase.IMPLEMENTING),
        # implementation done → review starts
        (Phase.IMPLEMENTING, Phase.REVIEWING),
        # review found issues → fix loop
        (Phase.REVIEWING, Phase.FIXING),
        # review found nothing blocking → done
        (Phase.REVIEWING, Phase.DONE),
        # fix done → re-review
        (Phase.FIXING, Phase.REVIEWING),
        # any phase can be aborted
        (Phase.PLANNING, Phase.ABORTED),
        (Phase.CONSENSUS, Phase.ABORTED),
        (Phase.IMPLEMENTING, Phase.ABORTED),
        (Phase.REVIEWING, Phase.ABORTED),
        (Phase.FIXING, Phase.ABORTED),
    }
)

# Terminal phases — no further transition allowed.
TERMINAL: frozenset[Phase] = frozenset({Phase.DONE, Phase.ABORTED})


class IllegalTransition(ValueError):
    """Raised when a phase transition is not in `ALLOWED_TRANSITIONS`."""


def can_transition(from_phase: Phase, to_phase: Phase) -> bool:
    """Return True iff `from_phase → to_phase` is whitelisted."""
    return (from_phase, to_phase) in ALLOWED_TRANSITIONS


def assert_transition(from_phase: Phase, to_phase: Phase) -> None:
    """Raise IllegalTransition if the move is not allowed."""
    if not can_transition(from_phase, to_phase):
        raise IllegalTransition(
            f"phase transition {from_phase.value} -> {to_phase.value} is not allowed"
        )


def is_terminal(phase: Phase) -> bool:
    return phase in TERMINAL


# ---------- Stop-rule predicates ----------------------------------------------
# Each predicate examines events in a given phase and returns whether the
# stop condition is met. These are intentionally simple — the broker calls
# them after every new event lands.


def planning_consensus_reached(
    events: Iterable[Event],
    *,
    round_count: int,
    max_rounds: int,
) -> bool:
    """Per §3.4 stop rules: stop planning when either

    - both workers' most recent messages carry `consensus_signal=AGREED`, or
    - no P0/P1 disagreements remain and one concrete proposal exists,

    subject to `round_count <= max_rounds`.

    For CP8 we implement the strict ANDed form: both workers signalled AGREED
    and no later disagreement event invalidated that. The "no P0/P1
    disagreements" weakening can be added when we have real disagreement
    metadata flowing.
    """
    if round_count > max_rounds:
        return False

    last_signal_by: dict[Worker, str] = {}
    for ev in events:
        if ev.sender in (Worker.WORKER_A, Worker.WORKER_B):
            sig = ev.metadata.consensus_signal
            if sig is not None:
                last_signal_by[ev.sender] = sig.value

    return (
        last_signal_by.get(Worker.WORKER_A) == "agreed"
        and last_signal_by.get(Worker.WORKER_B) == "agreed"
    )


def planning_should_force_consensus(round_count: int, max_rounds: int) -> bool:
    """Force-transition to CONSENSUS even without agreement when rounds maxed.

    Escalation: when the broker advances to CONSENSUS this way, it MUST raise
    a user approval request (the user is the chairperson).
    """
    return round_count >= max_rounds


def review_should_finish(
    events: Iterable[Event],
    *,
    fix_loop_count: int,
    max_fix_loops: int,
) -> bool:
    """Stop review/fix loop when

    - the latest test_result event passed, AND
    - the reviewer (worker_b) has no outstanding P0/P1 findings unaddressed.

    We model "addressed" as: every P0/P1 review_finding has a corresponding
    fix_response from worker_a or an agreement/rejection event after it.
    """
    if fix_loop_count > max_fix_loops:
        return False

    materialized = list(events)
    tests_passed_event_idx = -1
    for i, ev in enumerate(materialized):
        if ev.kind is EventKind.TEST_RESULT and "pass" in ev.content.lower():
            tests_passed_event_idx = i
    if tests_passed_event_idx < 0:
        return False

    open_findings: list[Event] = []
    for ev in materialized:
        if ev.kind is EventKind.REVIEW_FINDING and ev.metadata.severity in {None}:
            continue
        if (
            ev.kind is EventKind.REVIEW_FINDING
            and ev.metadata.severity is not None
            and ev.metadata.severity.value in {"P0", "P1"}
        ):
            open_findings.append(ev)

    # An open finding is "resolved" if a later fix_response, agreement, or
    # rejection event references it. For CP8 we use the simpler model:
    # the total count of P0/P1 review_finding events must be ≤ the total
    # count of fix_response events afterward.
    fix_responses_after = sum(
        1
        for i, ev in enumerate(materialized)
        if i > tests_passed_event_idx and ev.kind is EventKind.FIX_RESPONSE
    )
    return fix_responses_after >= len(open_findings)


def needs_user_escalation(
    events: Iterable[Event],
    *,
    round_count: int,
    max_rounds: int,
) -> bool:
    """Return True when the broker must escalate to the user.

    Escalation triggers (architecture.md §6.1 + §3.4):
      - workers disagree after max rounds,
      - a `risky_command` or `destructive_action` approval is pending,
      - any `error` event is unresolved,
      - context or budget limit hit (modeled separately).
    """
    materialized = list(events)
    if planning_should_force_consensus(round_count, max_rounds) and not planning_consensus_reached(
        materialized, round_count=round_count, max_rounds=max_rounds
    ):
        return True
    return any(ev.kind is EventKind.ERROR for ev in materialized)
