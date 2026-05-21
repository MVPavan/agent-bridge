# Agent Bridge — Architecture (CP1)

**Status:** draft 1 — to be hardened by `codex adversarial-review` before CP1 closes.
**Date:** 2026-05-20
**Scope:** abstract architecture for the bidirectional Claude–Claude–Codex bridge. Concrete primitive selection is CP2's job. This document defines *what* the system must be; CP2 picks *how*.

---

## 1. Purpose and scope

The bridge enables a human user to drive a persistent, peer-to-peer development conversation between two coding agents (a second Claude Code session and a Codex session), while interacting with only one "cockpit" Claude Code session.

### 1.1 In scope

- Process topology of the three sessions plus a broker process.
- Message routing rules and turn-taking discipline.
- State and event store schema (durable record of every agent-to-agent exchange).
- Failure modes and recovery strategy.
- Approval gates and human-in-the-loop semantics.
- The hard rule that the product code must not import the codex plugin.

### 1.2 Out of scope (this document)

- Concrete language/library choices (CP2).
- Wire-level protocol details for Claude Channels and Codex app-server (CP2 after empirical probing).
- Web UI, IDE plugins, multi-user concurrency, hosted deployment.
- Replacing the user's existing Bodha toolchain. The bridge is an independent tool that happens to live in this repo.

### 1.3 Terminology

| Term | Meaning |
|------|---------|
| **Claude #1 / cockpit** | The interactive Claude Code session the user is talking to (the session writing this doc). Sole UI. |
| **Claude #2 / worker-A** | A second Claude Code session, spawned and driven by the broker. |
| **Codex / worker-B** | A Codex session, spawned and driven by the broker. |
| **Broker** | The long-running process that owns the conversation, both worker sessions, and the event log. |
| **Conversation** | A single end-to-end task lifecycle: goal → debate → consensus → implement → review → result. Has a stable `conversation_id`. |
| **Phase** | One of: `planning`, `consensus`, `implementing`, `reviewing`, `fixing`, `done`, `aborted`. |
| **Round** | One full back-and-forth between the two workers within a phase. |

---

## 2. Process topology

### 2.1 Four processes, three sessions

```
                ┌──────────────────────────────┐
                │           Human              │
                │     (user / chairperson)     │
                └─────────────┬────────────────┘
                              │ interactive terminal
                ┌─────────────▼────────────────┐
                │   Claude #1  —  cockpit      │  ← this Claude session
                │   (interactive Claude Code)  │
                │                              │
                │   calls bridge via MCP tool  │
                │   reads transcript via Read  │
                └─────────────┬────────────────┘
                              │ MCP / local socket
                ┌─────────────▼────────────────┐
                │          Broker              │  ← long-running daemon
                │  • conversation registry     │
                │  • event log (SQLite+JSONL)  │
                │  • turn controller           │
                │  • approval gate             │
                │  • worktree manager          │
                └──────┬───────────────┬───────┘
                       │               │
              inbox/reply         JSON-RPC over
              (Claude Code        stdio / socket
               Channels or SDK)   (Codex app-server)
                       │               │
              ┌────────▼────────┐ ┌────▼──────────────┐
              │   Claude #2     │ │    Codex          │
              │   worker-A      │ │    worker-B       │
              │   headless,     │ │   persistent      │
              │   resumable     │ │   thread          │
              └─────────────────┘ └───────────────────┘
                       │               │
                       └──────┬────────┘
                              │
                  shared filesystem: git worktree
                  + artifacts dir for diffs, logs, decisions
```

### 2.2 Why a separate broker process

Claude #1 cannot directly own the conversation because:

- **Claude #1 is interactive**: blocking it on a long-running debate freezes the user's cockpit.
- **Claude #1 has its own context limit**: the agent-to-agent transcript would eat its context budget for the user's actual work.
- **Claude #1 has no clean way to push events into Claude #2 or Codex**: the primitives (Channels, app-server) belong outside an interactive Claude session.
- **Restart resilience**: Claude #1 may be closed and re-opened. The broker must survive that.

The broker is a normal OS process — daemonized or run under a process supervisor (CP2 decides). It exposes a small, stable interface to Claude #1 and owns everything else.

### 2.3 Lifecycle of a conversation

```
┌─ user gives goal in Claude #1
│
├─ Claude #1 calls broker.dispatch_goal(goal) → returns conversation_id
│
├─ broker:
│    1. allocate conversation_id, create artifacts dir
│    2. spawn Claude #2 (or resume from pool)
│    3. spawn / resume Codex thread
│    4. seed both with goal + system prompts + protocol rules
│    5. open turn to worker-B (Codex proposes architecture first)
│
├─ debate loop (planning phase):
│    – worker-B sends proposal → event log → broker hands turn to worker-A
│    – worker-A critiques → event log → turn back to worker-B
│    – round counter increments
│    – consensus detector fires on each turn
│    – broker streams every event to a transcript file Claude #1 can tail
│
├─ consensus reached OR max rounds → phase: consensus
│    – broker writes consensus.md to artifacts
│    – broker signals Claude #1: "ready for approval"
│
├─ Claude #1 surfaces consensus to user; user approves via broker.approve(...)
│
├─ implementation phase: worker-A implements in worktree
├─ review phase: worker-B reviews diff, worker-A fixes, loop converges
├─ phase: done → broker writes final report → Claude #1 surfaces to user
│
└─ user merges manually (out of scope for broker)
```

### 2.4 Spawning and resuming workers

**Default policy: fresh sessions per conversation.** Each conversation gets a freshly spawned Claude #2 session and a freshly created Codex thread. On `done` / `aborted` the worker is torn down. This is the CP1 baseline because no proven reset protocol exists yet that can guarantee isolation of model context, transcript history, tool permissions, MCP server bindings, environment, working directory, and worktree handle across reuse.

**Resume across broker restarts is allowed and required** (different from pooling): session IDs and thread IDs are persisted so that if the broker crashes mid-conversation, it can reattach to still-live workers. This is *resumption of the same conversation*, never reassignment to a different one.

**Warm pooling is explicitly deferred to a later phase** and gated on a written reset-protocol contract that must prove:

1. Model context window is fully cleared (or the session is recreated with the same `session_id` if the platform supports it without context bleed).
2. Tool permission state is reset to the default-deny baseline (see §6.0).
3. Working directory and worktree handle are unbound and rebound to the new conversation's worktree.
4. MCP server bindings, environment variables, and any per-conversation secrets are purged.
5. An automated test proves no transcript content, decision, or artifact from conversation N is observable in conversation N+1.

Until that contract exists and the test passes, the broker MUST NOT reuse a worker across conversations.

---

## 3. Message routing

### 3.1 Message envelope

Every agent-to-agent message and every system event uses one envelope shape:

```jsonc
{
  "event_id": "evt_00042",            // monotonic, broker-assigned
  "conversation_id": "conv_0001",
  "round": 3,                          // 0 for non-debate events
  "phase": "planning",                 // see §2.3
  "from": "worker_b",                  // worker_a | worker_b | broker | user
  "to":   "worker_a",                  // worker_a | worker_b | broker | user
  "kind": "proposal" |
          "critique" |
          "agreement" |
          "rejection" |
          "clarification_request" |
          "clarification_reply" |
          "diff" |
          "test_result" |
          "review_finding" |
          "fix_response" |
          "approval_request" |
          "approval_decision" |
          "system_note" |
          "error",
  "content": "<markdown body>",
  "requires_reply": true,
  "in_reply_to_idempotency_key": "ik_abc123",   // worker → broker only; MUST match the delivery being replied to
  "observed_turn_token_version": 17,            // worker → broker only; the turn_token_version the worker observed on receipt
  "metadata": {                        // optional, kind-specific
    "consensus_signal": "agreed" | "disagree" | "needs_info" | null,
    "severity": "P0" | "P1" | "P2" | null,
    "diff_artifact": "artifacts/conv_0001/round03.diff",
    "test_log_artifact": "artifacts/conv_0001/round03.test.log",
    "tool_calls": [...]
  },
  "created_at": "2026-05-20T15:30:00+05:30"
}
```

Rationale: one shape simplifies the event log, the broker logic, the transcript renderer, and (later) replay/debug tools.

### 3.2 Routing rules per direction

| From | To | Transport | Notes |
|------|------|-----------|-------|
| Claude #1 | Broker | MCP tool call (preferred) or local Unix socket | Tools: `dispatch_goal`, `get_status`, `approve`, `cancel`, `tail_transcript`. |
| Broker | Claude #1 | MCP tool response + transcript file | Claude #1 polls via `tail_transcript` or reads the artifact directory directly. |
| Broker | Worker-A | Claude Code inbound message primitive (Channel push or SDK message injection — CP2 decides) | One message at a time. |
| Worker-A | Broker | Worker-A's reply primitive (Channel reply tool or SDK return value) | Triggered by worker-A's agent loop completing a turn. |
| Broker | Worker-B | Codex app-server `inject_user_message` (or equivalent) | One message at a time. |
| Worker-B | Broker | Codex app-server event stream | Broker consumes streamed events, picks the terminal one per turn. |
| Worker-A | Worker-B | **never direct** | All cross-worker traffic goes through the broker. |
| Worker-B | Worker-A | **never direct** | Same. |

The "never direct" rule is critical: it gives the broker a chokepoint to enforce turn-taking, redaction, max-rounds, and audit logging.

### 3.3 Turn-taking discipline

At any time exactly one of `worker_a`, `worker_b`, `broker`, or `user` holds the *turn token*. The broker tracks turn state in durable storage — never in memory only — using three persistent fields per conversation (see §4.2):

- `current_turn` — who currently holds the token.
- `turn_token_version` — a monotonic integer incremented on every transfer. Used to reject stale acks and detect lost transitions on recovery.
- `pending_delivery_id` — the outbox row representing the in-flight message to the current turn holder, or `NULL` if no delivery is in flight.

When a worker emits a message marked `requires_reply: true`, the broker (a) atomically writes the event, (b) creates an outbox row addressed to the recipient with a fresh `idempotency_key`, (c) bumps `turn_token_version` and sets `current_turn` to the recipient, all in one SQLite transaction. The recipient acknowledges receipt (see §3.5), which moves the outbox row to `delivered`. If a worker emits `requires_reply: false`, the broker takes the token (`current_turn = "broker"`) and decides the next action (consensus check, round end, approval request).

Strict turn-taking prevents both workers from "talking over" each other and makes the transcript linear and reviewable. Persisted turn state also makes the §5.2 crash-recovery story actually possible.

### 3.5 Delivery, acknowledgment, and idempotency

Every broker → worker message goes through the `outbox` table with a state machine that separates **receipt** from **completion**. The distinction matters: a worker that acknowledged receipt but then died before emitting its reply still owes the conversation a turn. Conflating ack with completion would allow that turn to be silently lost.

**Outbox status states:**

| State | Meaning |
|-------|---------|
| `pending` | Row written, transport send not yet attempted. |
| `sent` | Transport dispatch in flight (no ack yet). |
| `delivered` | Worker acknowledged receipt via the transport (transport-level ack or platform-level echo of the idempotency key). The worker has the message but may or may not have processed it yet. |
| `answered` | Worker's reply event has been durably appended to `events`. Turn is complete. `outbox.reply_event_id` is populated. |
| `failed` | Send attempts exhausted; recovery escalates. |
| `abandoned` | User chose to abandon during a `worker_loss` approval. |

**Flow:**

1. **Enqueue**: broker writes the `events` row (the broker-originated message), the `outbox` row (`status='pending'`, fresh `idempotency_key`, `reply_event_id=NULL`), and the turn-token transition in a single SQLite transaction. WAL mode + `synchronous=FULL` ensures durability across crash.
2. **Send**: broker sets `status='sent'`, dispatches to the worker through the chosen transport, passing the `idempotency_key` both in the transport-level metadata and embedded in the message body (defense against metadata stripping).
3. **Receipt ack**: when the worker acknowledges receipt, broker sets `status='delivered'`, `delivered_at=now`. *The turn is not yet complete.*
4. **Completion**: when the worker emits a message, it MUST include `in_reply_to_idempotency_key` and `observed_turn_token_version` in the envelope (§3.1). The broker validates: the key matches the conversation's `pending_delivery_id` outbox row, **and** `observed_turn_token_version == conversations.turn_token_version`. On match, the broker appends the event to `events`, then in the same SQLite transaction sets the matching outbox row to `status='answered'`, `reply_event_id=<new event_id>`, and advances the turn token to the next holder. On mismatch — missing key, wrong key, or stale version — the broker quarantines the event with `kind='error'`, does NOT advance the turn, and emits an `error` event referencing the offending message; if mismatches exceed a small threshold, raises a `worker_loss` approval. This is the rule that prevents a stale or stray worker emission from being silently accepted as the answer to the current delivery.
5. **Reply implies receipt**: on platforms without explicit ack, the worker's reply event itself serves as the implicit receipt — broker transitions `sent → answered` directly, skipping `delivered`.
6. **Retry**: on send failure or timeout while in `pending` or `sent`, broker increments `attempt_count` and retries with the *same* `idempotency_key`. Workers MUST dedupe by key — already-processed keys are silently dropped.

**Recovery rules — by status at crash time:**

| Status at crash | Recovery action |
|-----------------|-----------------|
| `pending` | Re-dispatch (transport may never have seen it). |
| `sent` | Re-dispatch with same key; worker dedupes if duplicate. |
| `delivered` | The worker received but never replied — this is the lost-turn risk. Recovery: (a) probe the worker for its last emitted message via the transport's read-buffer / event-replay API if available, reconcile by `idempotency_key`. If a reply is found, durably append and transition to `answered`. (b) If no reply and the worker is alive, resend with the same `idempotency_key`; the worker dedupes the message but re-emits its reply if it had not yet done so (platforms that drop already-acked messages require option (c)). (c) If neither (a) nor (b) yields a reply within the configured `delivered_no_reply_timeout`, raise a `worker_loss` approval request — the broker MUST NOT silently abandon a delivered-but-unanswered turn. |
| `answered` | Turn already complete; no action. |
| `failed` | Already escalated via `worker_loss`; no resend. |

The `outbox.status` field is the authoritative "did this turn finish?" answer. A row that is `delivered` but never reaches `answered` is a known recovery case, not an undefined state.

Idempotency keys are also written into the worker-facing message body so that, in the rare case the transport strips metadata, the worker can still dedupe by content-and-key match.

### 3.4 Phase transitions

```
       planning ──(consensus detected | max rounds)──> consensus
       consensus ──(user approves)──> implementing
       implementing ──(worker-A signals done)──> reviewing
       reviewing ──(worker-B has findings)──> fixing
       reviewing ──(no findings + tests pass)──> done
       fixing ──(worker-A signals done)──> reviewing
       any ──(unrecoverable error | user cancel)──> aborted
```

Stop rules (see GOAL.md and the research doc §6.3): each transition has a written, deterministic condition the broker can evaluate.

---

## 4. State and event store

### 4.1 Storage layout

```
agent-bridge/
├─ state/
│   ├─ bridge.sqlite              # tables below
│   └─ events/
│       └─ conv_0001.jsonl        # append-only event log per conversation
├─ artifacts/
│   └─ conv_0001/
│       ├─ goal.md
│       ├─ codex-proposal.md
│       ├─ claude-critique.md
│       ├─ consensus.md
│       ├─ round03.diff
│       ├─ round03.test.log
│       └─ review.md
├─ worktrees/
│   └─ conv_0001-add-utility/     # ephemeral git worktree
```

SQLite carries indexable structured state; JSONL carries the verbatim event stream. Either is recoverable if the other is lost (SQLite can be rebuilt from JSONL; JSONL can be reconstructed from SQLite if events are stored with full payloads).

### 4.2 Schema (SQLite, abstract)

```sql
-- SQLite is the source of truth. WAL mode + synchronous=FULL on durability-critical writes.

CREATE TABLE conversations (
    conversation_id       TEXT PRIMARY KEY,
    goal                  TEXT NOT NULL,
    phase                 TEXT NOT NULL,
    round                 INTEGER NOT NULL DEFAULT 0,
    max_rounds            INTEGER NOT NULL,
    status                TEXT NOT NULL,           -- running | paused | done | aborted
    current_turn          TEXT NOT NULL,           -- worker_a | worker_b | broker | user | none
    turn_token_version    INTEGER NOT NULL DEFAULT 0,
    pending_delivery_id   TEXT,                    -- FK to outbox.delivery_id or NULL
    next_seq              INTEGER NOT NULL DEFAULT 1,   -- monotonic per-conversation event seq
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    worktree_path         TEXT
);

CREATE TABLE sessions (
    conversation_id   TEXT NOT NULL,
    worker            TEXT NOT NULL,        -- worker_a | worker_b
    session_id        TEXT NOT NULL,        -- Claude session id OR Codex thread id
    pid               INTEGER,
    state             TEXT NOT NULL,        -- spawning | live | resumable | dead
    permission_mode   TEXT NOT NULL,        -- see §6.0 — restrictive default-deny baseline
    last_seen_at      TEXT NOT NULL,
    PRIMARY KEY (conversation_id, worker)
);

CREATE TABLE events (
    event_id          TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    seq               INTEGER NOT NULL,     -- monotonic per conversation; gap = lost write
    round             INTEGER NOT NULL,
    phase             TEXT NOT NULL,
    sender            TEXT NOT NULL,
    recipient         TEXT NOT NULL,
    kind              TEXT NOT NULL,
    content           TEXT NOT NULL,
    content_hash      TEXT NOT NULL,        -- sha256(content) — used by JSONL reconciliation
    metadata_json     TEXT,
    requires_reply    INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
    UNIQUE (conversation_id, seq)
);
CREATE INDEX idx_events_conv_round ON events(conversation_id, round);

-- Outbox: durable record of every broker → worker delivery.
-- Source of truth for "did this turn finish?". Status separates receipt (delivered)
-- from completion (answered) — see §3.5.
CREATE TABLE outbox (
    delivery_id        TEXT PRIMARY KEY,
    conversation_id    TEXT NOT NULL,
    event_id           TEXT NOT NULL,            -- the broker-originated message
    recipient          TEXT NOT NULL,            -- worker_a | worker_b
    idempotency_key    TEXT NOT NULL UNIQUE,
    status             TEXT NOT NULL,            -- pending | sent | delivered | answered | failed | abandoned
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    next_retry_at      TEXT,
    last_error         TEXT,
    enqueued_at        TEXT NOT NULL,
    sent_at            TEXT,
    delivered_at       TEXT,
    answered_at        TEXT,
    reply_event_id     TEXT,                     -- FK to the worker's reply event, set on answered
    FOREIGN KEY (event_id) REFERENCES events(event_id),
    FOREIGN KEY (reply_event_id) REFERENCES events(event_id)
);
CREATE INDEX idx_outbox_pending  ON outbox(status, next_retry_at) WHERE status IN ('pending', 'sent');
CREATE INDEX idx_outbox_delivered ON outbox(status, delivered_at) WHERE status = 'delivered';

CREATE TABLE approvals (
    approval_id       TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    category          TEXT NOT NULL,        -- consensus_to_implement | risky_command |
                                            -- destructive_action | merge | worker_loss |
                                            -- budget_exceeded
    requesting_worker TEXT,                 -- worker_a | worker_b | NULL if broker-originated
    payload_json      TEXT NOT NULL,        -- the action description, command, plan, etc.
    decision          TEXT,                 -- approved | rejected | pending
    requested_at      TEXT NOT NULL,
    decided_at        TEXT,
    decided_by        TEXT                  -- user identifier
);
CREATE INDEX idx_approvals_pending ON approvals(decision) WHERE decision IS NULL;

CREATE TABLE artifacts (
    artifact_id       TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    relative_path     TEXT NOT NULL,
    kind              TEXT NOT NULL,        -- goal | proposal | critique | consensus | diff | test_log | review | report
    created_at        TEXT NOT NULL
);
```

The `outbox` table is what makes the turn-token invariant durable. The `seq` column on `events` provides the monotonic ordering used by JSONL reconciliation. `permission_mode` on `sessions` is the audit field for the §6.0 permission boundary — the broker refuses to dispatch to a session whose `permission_mode` does not match the declared baseline.

### 4.3 Durability guarantees

**SQLite is the single source of truth.** JSONL is a verbatim replicated stream for portability, debugging, and disaster recovery — it is not a parallel write path the broker reads back from in normal operation.

1. **Atomic enqueue.** The broker writes the `events` row, the `outbox` row, and the turn-token transition in **one SQLite transaction** with `WAL` journaling and `synchronous=FULL` for durability-critical writes (event + outbox + turn). Either all three land or none do.
2. **JSONL is best-effort replication.** After the SQLite transaction commits, the broker appends the same event to `state/events/conv_<id>.jsonl` with `fsync`. JSONL append failures are logged but never block delivery. On startup, the broker can rebuild JSONL from SQLite, and a separate reconciliation tool can rebuild SQLite from JSONL using `seq` and `content_hash` for ordering and dedup.
3. **Delivery is durable, not optimistic.** A message is considered "sent" only when its `outbox` row is `delivered`. Until then, on every broker startup the row is re-dispatched with the same `idempotency_key`. Workers MUST dedupe by key.
4. **Session/thread IDs are persisted on assignment.** Recorded in the same transaction that creates the session row. A broker restart can reattach to live workers from this record.
5. **No state in memory only.** Anything the broker needs across restart — turn token, pending delivery, current phase, round count, permission baseline — lives in SQLite.
6. **Append-only event log.** Events are never mutated. Corrections are appended as new events of kind `system_note`. The `seq` column is monotonic per conversation; a gap is a hard error and aborts the conversation.
7. **Crash-window invariant.** Because delivery is wrapped by the outbox state machine (§3.5), a crash *between* SQLite commit and transport send results in safe retry with the same key. A crash *between* transport send and ack results in safe retry. A crash *after* ack but before the next state advance results in the broker re-reading the `delivered` row, observing the worker's already-emitted reply, and continuing.

---

## 5. Failure modes and recovery

### 5.1 Worker session/thread death

| Failure | Detection | Response |
|---------|-----------|----------|
| Claude #2 process exits | inbox/reply primitive closes or times out | Mark session `dead`; attempt resume via stored session_id; on second failure escalate to user via Claude #1 (`approval_request` with category `worker_loss`). |
| Codex thread errors | app-server emits error event | Same pattern — try one resume, then escalate. |
| Worker hangs (no reply within turn timeout) | broker-side timer | Cancel the turn, log `error` event, retry once with same prompt, then escalate. |

Turn timeout is configurable per phase (planning: 5 min; implementing: 30 min default). All retries are bounded — never infinite.

### 5.2 Broker crash

SQLite is the source of truth (§4.3). The broker's recovery is mechanical, deterministic, and exercised by integration tests in CP6 and CP10.

On restart:

1. Load every conversation with `status ∈ {running, paused}`.
2. For each conversation, reattach each worker session by stored `session_id` / `thread_id`. Re-assert the declared `permission_mode` (§6.0.5). If reattach fails inside the bounded window or `permission_mode` drift is detected, mark the session `dead` and raise a `worker_loss` approval request (§6.1).
3. Find every `outbox` row whose `conversation_id` is still live and process by status per the §3.5 recovery table: `pending` → re-dispatch; `sent` → re-dispatch with same `idempotency_key` (worker dedupes); `delivered` → run the delivered-but-unanswered recovery (probe transport for reply, else resend with same key, else `worker_loss` approval — never silent abandonment); `answered` / `failed` / `abandoned` → no action. Workers MUST dedupe by key.
4. Read each conversation's `current_turn`, `turn_token_version`, and `pending_delivery_id`. If the turn holder is a worker, there MUST be a corresponding outbox row in status `sent` or `delivered` (handled in step 3). If there is no such row, the conversation is in an inconsistent state: the broker resets `current_turn` to `broker`, bumps `turn_token_version` (invalidating any in-flight worker reply that referenced the old version), appends a `system_note` event describing the anomaly, raises a `worker_loss` approval, and refuses to accept any worker output until the user reconciles. **All worker output received in any state — recovery or steady-state — flows through the §3.5 step-4 validator. There is no parallel "reconcile by content_hash" path.** Outputs whose `in_reply_to_idempotency_key` or `observed_turn_token_version` does not match the current pending delivery are quarantined as `error` events and never appended as turn completions.
5. Resume the turn timer using `updated_at` to compute remaining budget.

The crash-window invariants in §4.3.7 (and the outbox state machine in §3.5) are what make this recovery sound. The CP6 integration test must demonstrate: kill the broker between (a) SQLite commit and transport send, (b) transport send and ack, (c) ack and next state advance — for each case, no duplicate edit, no duplicate review, no lost reply.

### 5.3 Network or external-service errors

- Bounded retries with exponential backoff for transient errors.
- After max retries: log `error` event, mark conversation `paused`, escalate to user.
- Never silently swallow exceptions — every catch path emits an event.

### 5.4 Context limits

Workers will hit their own context limits during long debates or large diffs. The broker mitigates this by:

- **Summarization checkpoints**: after every N events (configurable, default 6), the broker asks the worker to emit a structured `system_note` summary of decisions so far. The next time the broker has to seed context (e.g., on resume), it uses the latest summary instead of the full transcript.
- **Phase boundaries**: when transitioning phases, the broker injects a fresh summary to both workers.
- **Hard limit**: if a worker reports a context error, the broker pauses the conversation and escalates.

### 5.5 Tool execution errors inside a worker

Workers run their own tools (Read, Edit, Bash, etc.). Errors there are *normal events* — the worker reports them in its next message, the other worker sees them, the debate continues. The broker does not try to interpret or fix tool errors; it only records them.

### 5.6 Infinite loops and runaway costs

- `max_rounds` per phase (default 6 planning, 4 review/fix).
- `max_total_rounds` per conversation (default 20).
- Cost/usage estimate logged per event (when worker emits usage data); the broker enforces a per-conversation budget.
- If a worker emits the same proposal-shape twice in a row (detected by content hash), the broker forces a phase transition or escalates.

---

## 6. Approval gates

### 6.0 Worker permission boundary — the enforcement point

Approval gates are only meaningful if the broker can actually *prevent* a worker from running a non-approved action. The broker does **not** intercept tool calls by reading transcripts after the fact — that is unenforceable. Instead, each worker is started with a restrictive native permission boundary that routes every tool request the broker cares about back to the broker as an approval request, before execution.

This subsection is the contract every CP2 primitive choice must satisfy. CP4 and CP5 verify the contract with integration tests; the contract is itself an invariant (§9).

#### 6.0.1 Claude #2 boundary

The broker MUST start Claude #2 with all of the following:

1. A **default-deny baseline**: shell execution, file-write tools, network tools, and any MCP servers that could escape the sandbox are denied unless explicitly allowlisted (§6.3).
2. A **permission callback the broker controls**. The chosen primitive (Claude Agent SDK permission callback / Claude Code Channels permission-prompt tool / hooks `PreToolUse` blocking hook — CP2 picks) must, for every non-allowlisted tool call, suspend the tool, emit an event to the broker, and wait for the broker's decision before resuming. The exact API is CP2's choice; the *behavior* — synchronous block-until-broker-decides — is non-negotiable.
3. A **bounded working directory**: the worker's cwd is restricted to its assigned worktree path. Path traversal attempts are denied at the boundary.
4. **No access to the broker's own state directory** (`agent-bridge/state/`), no access to the user's git config, no access to host secrets.
5. A persisted `permission_mode` on the `sessions` row that the broker re-asserts on every reattach. If a reattached session does not report the expected `permission_mode`, the broker MUST mark it `dead` and refuse to use it.

#### 6.0.2 Codex worker boundary

Two implementation shapes are defined. **They are not equivalent** — Shape B is materially weaker than Shape A, and the threat-model implications are written out below. The bridge's deployment chooses which shape to require; choice is recorded on the `sessions` row's `permission_mode` and is enforced per turn.

**Shape A — synchronous broker-controlled approval callback (strong contract; default for production).** The broker drives Codex through `codex remote-control` (the managed standalone app-server) and wires the JSON-RPC `item/commandExecution/requestApproval` / `item/permissions/requestApproval` callbacks back to the broker. Codex pauses **before** each non-allowlisted action; the broker emits an approval request event, decides; Codex resumes or aborts. This is the per-tool-call gate originally specified by this document and the only shape that satisfies the "broker must produce an approval request before execution" requirement of §6.0 and invariant I-6-A.

**Shape B — per-turn capability declaration (weaker; CP2 fallback when the standalone install is unavailable).** When `codex remote-control` cannot be used, the broker drives Codex through `codex exec --json` (one subprocess per turn) and declares the worker's full capability surface *before each turn*:

- `--sandbox <read-only|workspace-write>` for the OS-level filesystem/network sandbox. **`danger-full-access` is banned under Shape B** — see the warning paragraph at the end of this section.
- `-C <worktree>` to pin cwd.
- `--add-dir <scoped-scratch-dir>` for explicitly-permitted write-outside-worktree paths.
- `-c approval_policy=<mode>` / `-c sandbox_permissions=[...]` for fine-grained overrides.

Inside the turn, Codex cannot escape the declared envelope. Many blocked attempts surface as `agent_message` content in the JSON event stream and the broker logs those as `system_note` (or `error` when severe). FP-6-B verified one such case: `codex exec -C <wt> -s read-only`, prompted to write `/tmp/canary`, results in `Read-only file system` and no file creation. **Important honesty note:** the bridge cannot rely on Codex surfacing *every* failed attempt — Codex may summarize, retry silently, or omit a denial from the agent message. Under Shape B the broker's audit trail is "what Codex chose to surface," plus the *invocation arguments* recorded at turn start and the post-turn content snapshot (§6.3.1.1). Shape A is materially stronger because it sees every non-allowlisted request before execution.

**Shape B explicitly does NOT permit `danger-full-access`.** The list of allowed `--sandbox` values for Shape B is exactly `{read-only, workspace-write}`. `danger-full-access` would defeat the only safety property Shape B actually provides — an OS-enforced capability envelope — and Shape B has no per-call approval callback to compensate. Any need for unfettered access is treated as a separate, human-approved out-of-band operation, not a bridge feature.

##### 6.0.2.1 Honest comparison of Shape A and Shape B

| Property | Shape A | Shape B |
|----------|---------|---------|
| Authorization granularity | per tool call | per turn |
| Broker observes attempt **before** execution | yes (approval request RPC) | **no** — only after the OS blocks |
| Broker can deny one specific call and let the rest of the turn continue | yes | **no** — a denied action terminates that side effect for the whole turn |
| Broker can authorize one transient elevation mid-turn ("just this one command") | yes (resume after approval) | **no** — capabilities are fixed at turn boundary; broader rights require a new turn |
| Side effect produced by denied action | none | none (OS sandbox blocks) |
| Denied attempt observable post-hoc | yes (log of approval decision) | yes (`agent_message` in event stream) |
| Susceptible to "agent tries 100 forbidden commands until one happens to be in profile" | filtered by broker per call | filtered by OS sandbox per syscall |
| Required infrastructure | standalone codex install + daemon | system codex CLI only |

##### 6.0.2.2 Threat-model statement for Shape B

The bridge accepts Shape B only when:

1. The conversation's expected capabilities are stable across the planned phase (planning turns are read-only; implementation turns are workspace-write in one fixed worktree). The bridge's debate/implement/review structure satisfies this — no mid-turn capability changes are needed in normal flow.
2. The user has been told, in the cockpit Claude #1 prompt for the conversation, that this conversation runs under Shape B and lacks per-call pre-execution approvals.
3. The cockpit shows a banner / status field indicating which shape is active for the running conversation.

Under Shape B the bridge gives up these properties:

- No per-call pre-execution approval event in the broker's event log.
- No ability to authorize a single forbidden command mid-turn.
- The broker's audit trail for what the worker *attempted* is whatever Codex chose to surface in its agent message, not a separate broker-visible request before execution.

##### 6.0.2.3 What both shapes guarantee

Regardless of shape:

1. No worktree side effect happens outside the declared capability profile for that turn (OS sandbox or approval callback enforces).
2. **Shape-specific audit trail.** Under **Shape A** every non-allowlisted tool-call request lands as a broker-visible `approval_request` event *before* execution — the broker is the source of truth for what the worker tried. Under **Shape B** the broker logs the chosen invocation arguments at turn start, every JSON event Codex emits during the turn (including agent messages that report OS denials), and the post-turn content snapshot validation (§6.3.1.1). Attempts that Codex never surfaces are not directly observed; the broker relies on the OS sandbox to make them impossible to have side effects, and on §6.3.1.1 to catch any mutation that *did* land.
3. The persisted `permission_mode` on the `sessions` row records which shape is in force; the broker refuses to dispatch into a session whose declared shape does not match the current intent.
4. Cwd pinned to the conversation's worktree.
5. Default-deny network unless the capability profile explicitly opens hosts.
6. Capability changes (e.g., planning → implementation) happen only at turn boundaries.

##### 6.0.2.4 Shape B coverage probes (CP5 + CP10 obligations)

Because Shape B's safety rests on the OS sandbox and the post-turn content snapshot, **FP-6-B is not a single canary** — it is the family of profile-specific probes below. CP5 (Codex integration) must include automated tests covering each entry; CP10 (safety gates) rerun them as regression. Failure of any probe disqualifies Shape B for that profile until fixed.

| Sub-probe | Profile under test | Adversarial action | Pass condition |
|-----------|--------------------|--------------------|----------------|
| FP-6-B-planning | `-s read-only`, `-C <wt>` | Prompt the worker to write `<wt>/new.txt` AND `/tmp/probe_canary` | Neither file is created. Worktree state byte-identical before/after. |
| FP-6-B-review | `-s read-only`, `-C <wt>`, planning consensus already in transcript | Prompt the worker to modify a tracked source file via diff suggestion | No tracked file changes (verified by content snapshot). |
| FP-6-B-impl | `-s workspace-write`, `-C <wt>`, `--add-dir <wt>/.scratch` | Prompt the worker to write `<wt>/src/foo.py` (allowed) AND `<wt>/../escape.txt` (forbidden) | `src/foo.py` created; `../escape.txt` not created. |
| FP-6-B-add-dir | `-s workspace-write`, `-C <wt>`, `--add-dir <wt>/.scratch` | Prompt the worker to write `<wt>/.scratch/cache.json` (allowed) AND `/etc/passwd` (forbidden) | Scratch file created; /etc untouched. |
| FP-6-B-net-denied | Default Shape B (no network policy override) | Prompt the worker to `curl` an external host | curl fails; agent_message records the failure; no outbound connection succeeded (verify with strace or local network monitor in the test). |
| FP-6-B-denied-cmd | `-s read-only`, plus an explicit `-c approval_policy=untrusted` | Prompt the worker to run `sudo rm -rf /etc/passwd` | Command does NOT execute; agent_message reports refusal or OS denial; the broker logs the attempt. |

Every Shape B sub-probe must additionally assert that the post-turn content snapshot validation (§6.3.1.1) catches any leaked mutation the OS sandbox missed. **This is the second line of defense** — Shape B's audit trail is "OS sandbox + content snapshot," not "agent message alone."

#### 6.0.3 What this rules out

This contract makes the following impossible (when correctly implemented and verified):

- A worker shelling out to `rm -rf` without producing a `risky_command` approval row first.
- A worker editing files outside its assigned worktree.
- A worker installing or invoking an MCP server the broker did not authorize.
- A worker contacting the network without going through the broker's network policy.

Failure of any of these guarantees in CP4/CP5 integration tests is a CP1 architecture failure, not just a CP2 implementation bug — it would mean the topology cannot enforce its own approval rules and the design must change.

### 6.1 Categories

| Category | Trigger | Default policy |
|----------|---------|----------------|
| `consensus_to_implement` | Broker detects consensus or hits max planning rounds | **Require user approval.** |
| `risky_command` | Worker requests a shell command not in the allowlist | Require user approval. |
| `destructive_action` | Detected commands matching the deny-pattern (rm -rf, force-push, drop table, etc.) | Hard-block — even approval cannot run these automatically; user must run them manually. |
| `merge` | End of conversation — merging the worktree | Always manual. Bridge stops at `done`. |
| `worker_loss` | A worker died and resume failed | Require user approval to retry-from-scratch or abort. |
| `budget_exceeded` | Per-conversation budget threshold crossed | Require user approval to continue. |

### 6.2 Approval flow

```
worker emits action requiring approval
        │
        ▼
broker creates approval row, kind=approval_request
broker writes event to log
broker exposes pending approval via get_status / tail_transcript
        │
        ▼
Claude #1 surfaces to user
        │
        ▼
user replies in Claude #1
Claude #1 calls broker.approve(approval_id, "approved"|"rejected", note)
        │
        ▼
broker records decision, emits approval_decision event
broker resumes the conversation (or aborts on rejection)
```

### 6.3 Command allowlist — capability profiles, not bare prefixes

Naive prefix-based allowlists undercut the §6.0 boundary: once `pytest`, `uv run`, or any command that loads project code is allowed, the worker can execute arbitrary repo code, write files, spawn subprocesses, and contact the network without producing any further broker-visible tool request. The native permission callback only sees the top-level command.

The allowlist is therefore defined as **capability profiles**, not command strings. Each profile names a command, its allowed argv shape, and an independent sandbox envelope that constrains what the command can do regardless of what code it loads. **The sandbox envelope is the load-bearing piece** — the command-level allowlist is a convenience layer on top.

Each profile must specify:

1. **Exact argv constraints.** Argv pattern (regex or structured matcher) the command must satisfy. Wildcards must be path-bounded.
2. **Writeable-path bounds.** Filesystem writes restricted to the conversation's worktree (and a scratch tmpdir scoped to the conversation). Writes elsewhere are denied by the sandbox, not the allowlist.
3. **Network policy.** Default: no outbound network. Profiles that require network (rare) must explicitly declare allowed hosts.
4. **Subprocess policy.** Whether the command may spawn subprocesses, and if so, whether spawned subprocesses inherit the same sandbox (default: yes; sandbox must be inherited, not re-asserted).
5. **Wall-clock and resource limits.** Timeout, max memory, max output size; on breach the command is killed and the event logged.

#### 6.3.1 Profiles for the load-bearing CP9/CP10 commands

These two carry the most risk because they execute repository code. They are illustrative — CP2 ratifies the final list.

- **`pytest_in_worktree`**: argv must start with `pytest` followed only by paths under the conversation worktree. **Source files are read-only**: writes denied to any path tracked by git in the worktree, denied to any non-tracked source-shaped file, allowed only to a scoped subset (`<worktree>/.pytest_cache/`, `<worktree>/.cache/`, `<worktree>/scratch/`, and a per-conversation tmpdir). Network denied. Subprocesses inherit sandbox. 10-minute timeout.
- **`uv_run_in_worktree`**: argv must be `uv run <script-or-module>` where the script/module is under the worktree. Same read-only-source policy; writes confined to the same scoped subset above. Network denied (`uv` lockfile resolution must happen outside this profile — explicit `dependency_resolve` profile with network access, used only on operator approval). Subprocesses inherit sandbox. 10-minute timeout.

The sandbox envelope is what makes these safe even though they load arbitrary repo code. If the worker's `pytest` invocation tries to `open('/etc/passwd', 'w')` or `socket.connect()` to an external host, the sandbox denies the syscall and the broker logs a `risky_command_blocked` event.

#### 6.3.1.1 Content snapshot — defense against silent worktree mutation

Read-only-over-tracked-files is enforced by the sandbox, but the broker adds a content-level second layer. Coarse summaries (`git diff --stat`, `git status --porcelain`) are not sufficient — they miss content-preserving line shuffles, same-line-count rewrites, and mutations to files already covered by an approved diff event. The broker therefore operates on **per-file content hashes**, not stat summaries.

**Snapshot:** before dispatching any allowlisted command in an implementation/review phase, the broker computes a SHA-256 hash of **every non-ignored file in the worktree — tracked and existing untracked alike** (ignored files per `.gitignore` are excluded; the broker enforces a max-files / max-size budget to bound the snapshot). The hash map is persisted in the command's event metadata. Storing only the untracked filename list (without content hashes) would leave a symmetric blind spot: existing untracked source or artifact files could be silently mutated without changing the filename set. Hashing both classes closes that branch.

**Worker write contract:** every file write a worker performs via an approved tool MUST produce a broker-visible `diff` event whose metadata includes `path`, the `pre_hash` from the snapshot, and the `post_hash` the worker computed after writing. The collection of `(path, post_hash)` pairs from emitted diff events forms the *expected delta* for the command.

**Validation:** after the command returns, the broker recomputes hashes for every tracked file and enumerates the worktree for new untracked files. For each tracked file `F`:

- `hash(F)_post == hash(F)_pre` → unchanged, OK.
- `F` is in the expected delta and `hash(F)_post == expected_post_hash(F)` → changed exactly as declared, OK.
- Otherwise → **unexpected mutation.**

Any new untracked file the worker did not declare via a write event is also an unexpected mutation. Any file declared in the expected delta whose `post_hash` does not match the recomputed hash is an unexpected mutation (worker lied or sandbox failed).

**On unexpected mutation:**

1. Mark the command's event with `metadata.unexpected_mutation = true` and the offending paths.
2. Raise a `risky_command` approval request asking the user to accept, revert, or abort.
3. Block phase advance until the user decides.

This catches every case where `pytest` plugins, `conftest.py` side effects, malformed test code, or in-process patching mutate tracked files without producing a faithful broker-visible write event. The sandbox denies most such attempts at the syscall layer; the content snapshot catches whatever the sandbox missed and turns silent mutation into a visible approval point with the exact list of offending paths.

#### 6.3.2 Deny-list — hard block, not approvable

Hard-block patterns are denied even under approval: `rm -rf`, `--no-verify`, `git push --force` to protected refs, `sudo`, `curl|wget` to non-allowlisted hosts, any command that targets `agent-bridge/state/`, any modification of the host git config. The user must run these manually if they are ever genuinely needed.

#### 6.3.3 Verification

The capability-profile model is verified by FP-5 and FP-6 (§8.1), plus an explicit CP10 test obligation: under each allowlisted profile, prove that representative attempts to write outside the worktree, contact the network, or invoke a denied command produce a broker-visible blocked event and zero side effects. If `pytest` or `uv run` can escape the sandbox during these tests, the profile is broken and CP10 fails — not a future-improvement note.

---

## 7. Why the product does NOT use the codex plugin

### 7.1 What the plugin is

The codex plugin (`.claude/commands/use-codex.md` plus the `codex:rescue` / `review` / `adversarial-review` subagents) is a one-way critic invocation: Claude calls Codex through a subprocess for a single round of analysis. It uses the `codex` CLI under the hood, runs at `--effort low` by default, has no thread persistence, and is designed to be cheap and disposable.

It is excellent for what it is: getting a quick second opinion on a doc, plan, or diff.

### 7.2 Why it is the wrong shape for this product

The bridge requires:

| Bridge needs | Plugin provides |
|--------------|-----------------|
| Persistent multi-turn threads with a stable `thread_id` | Disposable single-shot invocations |
| Codex initiating messages to Claude | Only Claude → Codex direction |
| Live event streaming | Final report only |
| Approval gates around Codex's tool use | Plugin is the tool |
| Programmatic control over Codex's model, effort, sandbox, working dir | Plugin opinionatedly sets these |
| Codex peer behavior (debating, asking clarifying questions) | Critique-only mode |

Layering the bridge on top of the plugin would mean rebuilding Codex thread state, message routing, and event semantics inside a wrapper that the plugin actively tries to constrain. That is not a productive starting point.

### 7.3 Hard rule and enforcement

- No file under `agent-bridge/src/` may import, exec, or shell out to the plugin codepath. The bridge talks to Codex directly through the chosen primitive (CP2 will pick: app-server, MCP server, or SDK).
- Final verification: `grep -rE "(use-codex|\\.claude/plugins/codex|codex:rescue|codex:review|codex:adversarial-review)" agent-bridge/src/` must return zero matches.
- This rule is bridge-product-only. During *this development work* (writing docs, plans, diffs) we invoke the plugin freely per `.claude/commands/use-codex.md` — that's how we get second opinions on the bridge itself. That meta-use leaves no trace in the bridge's source code.

### 7.4 What this implies for CP2

CP2 must pick a Codex primitive that gives:

- Thread creation with returned, persistable `thread_id`.
- Send-message-to-thread with streamed event response.
- Resume-thread by `thread_id` after broker restart.
- Programmatic control over model, effort, sandbox mode, working directory, approval policy.

Leading candidates: Codex app-server JSON-RPC; Codex MCP server. The CLI's `--resume` may suffice as a stopgap if app-server proves immature, but CP2 must verify.

---

## 8. CP2 prerequisites

### 8.1 Feasibility probes — required gate before CP2 locks any primitive

The architecture above commits to a topology — bidirectional, broker-mediated, durable, approval-gated. That commitment is sound *only if* the underlying primitives actually support what the topology demands. CP1 closes with a doc, but CP2's first job is to run a short, empirical, throwaway-grade set of probes that confirm the load-bearing assumptions. **Each probe is binary pass/fail. CP2 cannot lock a tech-stack decision before all probes pass with the chosen primitive.** A failed probe is not a CP2 implementation problem — it is a CP1 architecture problem, and the doc must be revised.

| ID | Probe | Pass condition |
|----|-------|----------------|
| FP-1 | Spawn Claude #2 headlessly, send 3 messages, observe replies, verify `session_id` constant across the 3 turns | Same `session_id` in all 3 turns; broker receives all 3 replies. |
| FP-2 | Kill and resume the Claude #2 session by `session_id`; send a 4th message that references context from turn 2 | Worker correctly references turn-2 content. |
| FP-3 | Spawn Codex thread, send 3 messages via the chosen primitive (app-server / MCP / CLI), observe replies, verify `thread_id` constant | Same `thread_id` in all 3 turns; broker receives all 3 replies. |
| FP-4 | Kill and resume the Codex thread by `thread_id`; send a 4th message that references context from turn 2 | Thread correctly references turn-2 content. |
| FP-5 | Start Claude #2 with the §6.0.1 permission baseline; from inside the worker attempt one non-allowlisted shell command | Command does NOT execute; broker observes an approval request event before any side effect. |
| FP-6-A | (Shape A) Start Codex via `codex remote-control` with the JSON-RPC approval callback wired to the broker; from inside the worker attempt one non-allowlisted shell command. | Command does NOT execute. Broker observes an `item/commandExecution/requestApproval` (or `item/permissions/requestApproval`) event **before any side effect**. Declining the approval prevents the action; the worker remains alive. |
| FP-6-B | (Shape B) Run the full **§6.0.2.4 sub-probe family**: FP-6-B-planning, FP-6-B-review, FP-6-B-impl, FP-6-B-add-dir, FP-6-B-net-denied, FP-6-B-denied-cmd. **A single read-only canary alone is not sufficient** — Shape B's safety rests on the OS sandbox AND the post-turn content snapshot, and every Shape B capability profile the broker will use must be probed. | Each sub-probe in §6.0.2.4 passes its stated pass condition AND the post-turn content snapshot validation (§6.3.1.1) reports zero unexpected mutations. The agent reply happening to include the OS error is welcome evidence but not required — visibility is best-effort under Shape B per §6.0.2.3 #2. |
| FP-7 | From the broker, send one message to Claude #2 and one to Codex through the chosen broker→worker primitives, with idempotency_key in payload; resend each with the same key | Both workers process the first delivery and dedupe the duplicate. |
| FP-8 | Surface a broker-emitted message to Claude #1 (this session) through the chosen Claude#1↔broker interface | Message is visible in the Claude #1 transcript via the interface defined in §3.2. |

The probes are *throwaway-grade*: shell scripts, ad-hoc Python, or even manual reproduction is fine. They are not the real bridge code. Their only output is a written "passed / failed / changed-design" log under `agent-bridge/docs/feasibility-probes.md`. If any probe forces a topology change, the architecture doc is updated before CP2 closes.

### 8.2 Open decisions for CP2

These are normal choices CP2 will make based on the probes:

1. **Claude #2 inbound primitive**: Channels vs Agent SDK `query()` with `--resume` vs hooks. The §6.0.1 permission contract is the gating constraint — pick the primitive that actually provides a synchronous broker-controlled permission callback.
2. **Codex inbound/outbound primitive**: app-server JSON-RPC vs MCP server vs CLI `--resume`. Same gating constraint via §6.0.2.
3. **Claude #1 → broker transport**: MCP tool exposed by a broker-side MCP server is the leading choice. Alternative: local Unix socket with a thin CLI wrapper.
4. **Broker language**: Python (matches repo conventions: `uv`, `structlog`, `pydantic`) vs TypeScript (closer to Claude/Codex SDK surface). Likely Python for repo coherence — confirm SDK availability for both Claude Agent SDK and Codex on Python first.
5. **Process supervision**: systemd / launchd / thin in-tree supervisor / shell-managed. Lowest friction first.
6. **Transcript surfacing to Claude #1**: poll-via-Read of a tail-able JSONL file is the simplest pattern; streaming MCP tool responses is nicer but requires careful chunking. Pick the simpler one first; upgrade later.

---

## 9. Architectural invariants (must hold at all times)

These are the load-bearing rules. Each has a stated, mechanically checkable verification — invariants that cannot be checked do not belong here.

| # | Invariant | How verified |
|---|-----------|--------------|
| I-1 | No direct worker-to-worker traffic. All cross-worker messages route through the broker. | CP6 integration test: worker-A and worker-B share no network/IPC handle; broker is the only path. |
| I-2 | Session/thread IDs persisted before first use. No transient-only worker handles. | CP4/CP5 test: kill broker immediately after spawn; on restart, broker reattaches by stored ID. |
| I-3 | Append-only event log; no mutation of `events` rows. | Unit test attempts UPDATE/DELETE on `events`; SQLite trigger denies. |
| I-4 | One turn token per conversation, durably persisted. | Schema enforces `current_turn` + `turn_token_version` columns; CP6 crash test confirms no turn lost or duplicated. |
| I-5 | Every broker → worker delivery has an outbox row with a unique `idempotency_key`; workers dedupe by key. | CP6 test FP-7-equivalent: resend same key, worker ignores duplicate. |
| I-6-A | (Shape A, Claude #2 and Codex when `codex remote-control` is wired) §6.0 permission boundary in its strong form: no worker tool action executes without either (a) being on the allowlist, or (b) producing a broker-visible approval request **before** execution. | CP4 (Claude side, FP-5): PreToolUse hook fires, broker records the request, tool denied, no side effect. CP5 (Codex side, FP-6 Shape A): JSON-RPC approval RPC fires, broker records it, tool denied, no side effect. |
| I-6-B | (Shape B, Codex under `codex exec` per-turn capability declaration) Weakened per-turn form: every Codex turn runs inside an OS-enforced capability profile chosen by the broker before invocation. **The broker's guarantees are: (i) no side effect lands outside the declared capability profile; (ii) the chosen invocation arguments and every JSON event Codex emits during the turn are durably recorded; (iii) the post-turn content snapshot (§6.3.1.1) catches any mutation the sandbox missed.** The broker does NOT guarantee that every blocked attempt is visible — Codex may summarize or omit denials from its agent messages. Approval under Shape B is the invocation argument set, recorded at turn start. | CP5 + CP10 run the §6.0.2.4 sub-probe family (FP-6-B-planning, -review, -impl, -add-dir, -net-denied, -denied-cmd). Each asserts: no side effect outside declared profile **and** post-turn content snapshot detects any leak. The single read-only canary alone is not sufficient. |
| I-7 | Workers never reuse across conversations until §2.4 reset contract is implemented and tested. | Static: broker code refuses to assign a worker session to a different `conversation_id` than the one it was created for, unless a `reset_protocol_version` field is set on the session row. |
| I-8 | Workers cannot edit outside their assigned worktree. | §6.0 boundary; verified by FP-5/FP-6 attempting writes outside the worktree. |
| I-9 | No codex-plugin imports under `agent-bridge/src/`. | Final-verification grep: `grep -rE "(use-codex\|\\.claude/plugins/codex\|codex:rescue\|codex:review\|codex:adversarial-review)" agent-bridge/src/` returns empty. |
| I-10 | No silent exception swallowing — every catch emits an `error` event. | Lint rule + code review; spot-checked in CP9/CP10 tests. |
| I-11 | SQLite is the source of truth; JSONL is replicated for portability. The broker never reads JSONL in normal operation. | Static code review + unit test asserting normal-path read access is SQLite-only. |
| I-12 | Crash-window safety: a broker crash at any point between SQLite commit / transport send / ack / next state never produces duplicate edits, duplicate reviews, or lost replies. | CP6 chaos test: kill broker at each of the three boundaries; assert outcome invariant. |

---

## 10. References

- `claude_codex_bidirectional_agent_bridge_research.md` (repo root) — the research that motivated this design. This doc is the spec; that one is the survey.
- `GOAL.md` — mission, hard constraints, definition of done.
- `STATE.md` — checkpoint progress and active checkpoint.
- `.claude/commands/use-codex.md` — invocation rules for the codex plugin (meta-tool only; not used in product code).
