"""CP10 — integration tests for safety gates.

These tests prove the gates that architecture.md §6.1, §6.3, §3.5, §I-* require
hold together when wired through Broker + Store + WorktreeManager. They use
fake workers so the broker exercises the full enforcement path without LLM
spend.

Verified properties:

- Max debate rounds: planning loop forces a CONSENSUS transition at
  max_rounds even without agreement.
- Outbox correlation: a reply whose `idempotency_key` doesn't match the
  pending delivery is rejected.
- Permission boundary: the broker-built PermissionCallback denies tools
  outside its allowlist; denylist patterns win even when allowlisted.
- Content snapshot: an undeclared tracked-file mutation inside a worktree
  is flagged by `WorktreeManager.validate`.
- Append-only events: direct UPDATE/DELETE on `events` is blocked by the
  schema trigger (invariant I-3).
- I-9 grep: no codex-plugin imports under agent-bridge/src/.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from agent_bridge.broker import (
    Broker,
    BrokerConfig,
    WorkerDriver,
    WorkerReply,
    WorkerSend,
)
from agent_bridge.events import (
    EventKind,
    OutboxRow,
    OutboxStatus,
    Phase,
    TurnHolder,
    utcnow,
)
from agent_bridge.outbox import validate_worker_reply
from agent_bridge.snapshot import ExpectedDelta
from agent_bridge.store import Store
from agent_bridge.worktree import WorktreeManager

# ---------- Fixtures ----------------------------------------------------------


class _SilentWorker(WorkerDriver):
    def __init__(self, worker_id: TurnHolder, session_id: str) -> None:
        self.worker_id = worker_id
        self._session_id = session_id

    async def send(self, msg: WorkerSend) -> WorkerReply:
        # Echo the bridge-ack the broker injected (P0-1 fix from codex
        # review `review-mpf7lig2-p6fxyi`).
        ack = f"<<BRIDGE-ACK ik={msg.idempotency_key} ver={msg.turn_token_version}>>"
        return WorkerReply(
            text=f"ok\n{ack}",
            session_or_thread_id=self._session_id,
            in_reply_to_idempotency_key=msg.idempotency_key,
            observed_turn_token_version=msg.turn_token_version,
        )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[Store, None]:
    s = await Store.open(tmp_path / "bridge.sqlite")
    yield s
    await s.close()


@pytest.fixture
def broker(store: Store) -> Broker:
    return Broker(
        store=store,
        worker_a=_SilentWorker(TurnHolder.WORKER_A, "sess_a"),
        worker_b=_SilentWorker(TurnHolder.WORKER_B, "sess_b"),
        config=BrokerConfig(max_planning_rounds=2),
    )


# ---------- Gate: max debate rounds ------------------------------------------


async def test_planning_loop_force_transitions_at_max_rounds(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("force consensus")
    await broker.run_planning_loop(
        cid,
        first_prompt_for_codex="Propose plan",
        first_prompt_for_claude_template="Critique: {codex_proposal}",
    )
    conv = await store.get_conversation(cid)
    assert conv is not None
    assert conv.phase is Phase.CONSENSUS  # forced even without explicit AGREED
    # A consensus_to_implement approval was raised because no real agreement.
    assert await store.pending_approval_for(cid) is not None


# ---------- Gate: outbox idempotency-key correlation -------------------------


async def test_outbox_validator_rejects_mismatched_idempotency_key() -> None:
    row = OutboxRow(
        delivery_id="dlv_x",
        conversation_id="conv_x",
        event_id="evt_x",
        recipient=TurnHolder.WORKER_A,
        idempotency_key="ik_correct",
        status=OutboxStatus.SENT,
        enqueued_at=utcnow(),
    )
    result = validate_worker_reply(
        pending_row=row,
        conversation_turn_token_version=3,
        reply_in_reply_to_idempotency_key="ik_WRONG",
        reply_observed_turn_token_version=3,
    )
    assert not result.accept
    assert "mismatch" in (result.reason or "")


async def test_outbox_validator_rejects_stale_turn_token_version() -> None:
    row = OutboxRow(
        delivery_id="dlv_x",
        conversation_id="conv_x",
        event_id="evt_x",
        recipient=TurnHolder.WORKER_A,
        idempotency_key="ik_1",
        status=OutboxStatus.SENT,
        enqueued_at=utcnow(),
    )
    result = validate_worker_reply(
        pending_row=row,
        conversation_turn_token_version=10,
        reply_in_reply_to_idempotency_key="ik_1",
        reply_observed_turn_token_version=7,
    )
    assert not result.accept
    assert "stale" in (result.reason or "")


# ---------- Gate: §6.0.1 permission boundary ---------------------------------


async def test_broker_permission_callback_denies_by_default(broker: Broker) -> None:
    cid = await broker.dispatch_goal("x")
    cb = broker.build_permission_callback(cid)
    d = await cb(worker="worker_a", tool_name="Bash", tool_input={"cmd": "ls"})
    assert d.decision.value == "deny"


async def test_broker_permission_callback_denylist_blocks_even_allowlisted_tool(
    broker: Broker,
) -> None:
    cid = await broker.dispatch_goal("x")
    cb = broker.build_permission_callback(
        cid, allowlist=("Bash",), denylist=("rm -rf",)
    )
    d = await cb(
        worker="worker_a", tool_name="Bash", tool_input={"command": "rm -rf /"}
    )
    assert d.decision.value == "deny"
    assert d.reason is not None and "denylist" in d.reason


async def test_broker_permission_callback_logs_request_as_event(
    broker: Broker, store: Store
) -> None:
    cid = await broker.dispatch_goal("x")
    cb = broker.build_permission_callback(cid)
    await cb(worker="worker_a", tool_name="Bash", tool_input={"cmd": "ls"})
    events = await store.list_events(cid)
    assert any(e.kind is EventKind.APPROVAL_REQUEST for e in events)


# ---------- Gate: content-snapshot mutation detection ------------------------


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "base"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "seed.py").write_text("print('seed')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


async def test_worktree_snapshot_catches_unexpected_mutation(
    base_repo: Path, tmp_path: Path
) -> None:
    mgr = WorktreeManager(base_repo=base_repo, worktrees_root=tmp_path / "wts")
    spec = await mgr.create("conv_cp10")
    try:
        pre = mgr.snapshot(spec)
        (spec.path / "seed.py").write_text("print('mutated')\n")  # not declared
        post = mgr.snapshot(spec)
        result = mgr.validate(spec, pre, post, ExpectedDelta())
        assert not result.ok
        # Snapshot validator surfaces the offending path so the broker can
        # raise the §6.3.1.1 risky_command approval.
        assert any(f.path == "seed.py" for f in result.findings)
    finally:
        await mgr.destroy(spec, force=True)


# ---------- Gate: append-only event log (invariant I-3) ----------------------


async def test_events_table_rejects_update(broker: Broker, store: Store) -> None:
    import aiosqlite

    cid = await broker.dispatch_goal("x")
    with pytest.raises(aiosqlite.IntegrityError):
        await store._conn.execute(
            f"UPDATE events SET content='tampered' WHERE conversation_id='{cid}'"
        )
        await store._conn.commit()


# ---------- Gate: I-9 (no codex-plugin imports in product code) --------------


def test_i9_grep_returns_empty() -> None:
    """Static guard: ensure no agent-bridge/src/ file references the codex plugin."""
    src_root = Path(__file__).resolve().parents[2] / "src"
    pattern = (
        "use-codex|"
        "\\.claude/plugins/codex|"
        "codex:rescue|"
        "codex:review|"
        "codex:adversarial-review"
    )
    result = subprocess.run(
        ["grep", "-rE", pattern, str(src_root)],
        capture_output=True,
        text=True,
    )
    # grep exits 1 on no matches — which is what we want.
    assert result.returncode == 1, (
        f"I-9 violation under {src_root}:\n{result.stdout}"
    )
