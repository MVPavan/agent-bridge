# Agent Bridge — CP2 Tech-Stack Decisions

**Created:** 2026-05-20
**Status:** decisions made on surface evidence; Stage-B probes pending user sign-off may force revision.
**Companion:** `feasibility-probes.md`.

Every decision below names an alternative and the trade. Decisions are revisable if a probe falsifies them.

---

## D-1 — Language: **Python**

**Decision:** the agent-bridge product code is Python 3.12+, managed by `uv`, with `pyproject.toml` co-located at `agent-bridge/pyproject.toml` (separate uv project from the parent Bodha repo to keep dependency surfaces independent).

**Alternative considered:** TypeScript on Node 22.

**Trade:**

- Python matches repo conventions (Bodha is Python with `uv`, `structlog`, `pydantic`). Subagents, CLAUDE.md rules, and lint configuration are all Python-shaped.
- `claude-agent-sdk` is published as a Python package (verified at v0.2.82 on PyPI).
- `codex-client` is published as a Python package (verified at v0.1.0 on PyPI).
- TypeScript would have ergonomic SDK access via the Claude/Codex official SDKs and slightly tighter alignment with the Claude Code CLI's own implementation language, but it splits the toolchain and forces a Node build step inside an otherwise-Python repo. Not worth it for ~zero benefit.

**Sharp edges:** `claude-agent-sdk` pulls in `mcp`, `starlette`, `uvicorn`, `sse-starlette` — a non-trivial dep tree. Pinning matters.

---

## D-2 — Claude #2 inbound primitive: **`claude-agent-sdk` Python**

**Decision:** spawn / resume / drive Claude #2 sessions via `claude-agent-sdk`'s `ClaudeSDKClient` and `ClaudeAgentOptions`, using:

- `session_id=<uuid>` to pin the session ID (or `resume=<sid>` to resume).
- `permission_mode="default"` (broker uses hooks for enforcement, NOT the looser `acceptEdits` / `bypassPermissions` modes).
- `hooks={"PreToolUse": [...]}` for the §6.0.1 permission callback — synchronous broker-controlled decision before any tool side effect.
- **Session persistence is ON for any probe or production code that needs resume (FP-1/FP-2 specifically). Disable persistence only for genuinely one-shot probes** — do not use `--no-session-persistence` between the FP-1 and FP-2 turns, or FP-2 will fail for setup reasons instead of telling us anything about the Claude primitive.

**Alternatives considered:**

| Alternative | Why not |
|-------------|---------|
| `claude --print --session-id <uuid> --input-format stream-json --output-format stream-json` driven via subprocess | Works but more brittle — bidirectional JSONL over pipes is harder to type-check and reason about than an async Python API. Useful as the FP-1 fallback only. |
| Claude Code Channels | Per docs-researcher: preview only; permission flow is user-mediated through chat apps (Telegram/Discord/iMessage); no programmatic broker-controlled callback. **Does not satisfy §6.0.1.** Eliminated. |
| Hooks-only (no SDK) | Possible but you lose the structured async client. Not worth giving up. |

**§6.0.1 satisfied:** PreToolUse hook is documented synchronous-blocking; broker's deny decision blocks execution before side effect. **Confidence pending FP-5 confirmation.**

---

## D-3 — Codex inbound/outbound primitive: **`codex exec --json` subprocess-per-turn with per-turn capability declaration**

**Decision:** drive Codex through `codex exec --json` invocations, one per turn. Each invocation declares the full capability surface up front:

- `--sandbox <read-only|workspace-write>` — OS sandbox bounds.
- `-C <worktree>` — pinned cwd.
- `--add-dir <scoped-scratch>` — additional writable roots (rare).
- `-c approval_policy=<mode>` and `-c sandbox_permissions=[...]` — fine-grained overrides.
- Resume by `thread_id`: `codex exec resume --json <thread_id> "<prompt>"` (verified by FP-3 and FP-4).

This is Shape B of architecture.md §6.0.2 and is the CP2 baseline.

**Why this over the originally-recommended `codex remote-control` + JSON-RPC approval callback?**

The standalone-codex install (`curl -fsSL https://chatgpt.com/codex/install.sh | sh`) was blocked by the auto-mode classifier during CP2 Stage B. We probed the remaining `codex 0.131.0` transports — `codex mcp-server`, `codex exec-server`, and `codex exec` — and found that only the subprocess transport reliably honors per-invocation sandbox + capability flags (`codex exec-server` is still a stub; `codex mcp-server` exposes only synchronous tool requests with no broker insertion point). FP-6-alt empirically proved that `codex exec -C <wt> -s read-only` blocks a `/tmp/canary` write at the OS layer and surfaces the failure as a visible `agent_message` event. The broker therefore has both authorization (chosen per turn) and observability (events in the JSON stream).

**Alternatives kept as documented optional upgrades:**

| Alternative | Status |
|-------------|--------|
| `codex remote-control` + JSON-RPC approval callback (Shape A of §6.0.2) | Optional upgrade when the standalone codex install is performed. The broker's external contract is unchanged; only the internal Codex transport switches. CP2 does NOT require it. |
| `codex mcp-server` | Rejected: no broker insertion point in the protocol. |
| `codex exec-server --listen ws://...` | Rejected: methods return `exec-server stub does not implement X yet` in `codex 0.131.0`. |
| `codex-client` (PyPI v0.1.0) | Rejected: the v0.1.0 API doesn't capture `conversation_id` against `codex 0.131.0`'s MCP server. Probe failure documented in `feasibility-probes.md`. |

**Risk:** the subprocess-per-turn shape has higher per-turn latency than a persistent thread connection. Acceptable for the bridge's debate/implement/review cadence (turns are seconds-to-minutes, not milliseconds). The original approval-callback granularity is replaced by turn-boundary granularity, which is sufficient for the bridge's threat model — see architecture.md §6.0.2 trade-off table.

---

## D-4 — Broker process model: **single Python daemon, async**

**Decision:** the broker is a single async Python process, run as a long-lived daemon. Concurrency through `asyncio`. SQLite accessed via `aiosqlite` (or thin sync wrapper if needed).

**Startup:** initially `python -m agent_bridge.broker` started manually under the user's shell during CP3–CP6; introduce systemd/launchd later (deferred to CP10/11 if needed). Keep the supervisor question off the critical path.

**Alternative considered:**

- Multi-process broker (one per conversation): more isolation, more overhead. Premature.
- Sync threaded broker: harder to model with three concurrent I/O streams (Claude #2, Codex, Claude #1).

**Sharp edge:** SDK hook callbacks may need to be synchronous Python functions even though everything else is async — bridge them via `asyncio.run_coroutine_threadsafe()` or a queue.

---

## D-5 — Storage: **SQLite (`aiosqlite`) + per-conversation JSONL replication**

**Decision:** SQLite at `agent-bridge/state/bridge.sqlite`, WAL mode, `synchronous=FULL` on durability-critical writes. Schema per `architecture.md` §4.2. Each conversation gets a paired `state/events/conv_<id>.jsonl` for replication and disaster recovery. SQLite is the only normal-path read source (invariant I-11).

**Alternative considered:**

- Postgres / SQLite-with-litestream / Redis: overkill for a single-user local broker. Add later if multi-user.
- JSONL-only: lacks indexable state for `outbox.status` queries and recovery.

**Migrations:** use `alembic` if schema grows; for CP3 a hand-written `CREATE TABLE IF NOT EXISTS` migration runner is sufficient.

---

## D-6 — Claude #1 ↔ broker transport: **MCP tool**

**Decision:** the broker hosts an MCP server (over local stdio or unix socket). Claude #1 attaches to it with `--mcp-config` (this session, when we get to CP7, will literally point at `agent-bridge/mcp-config.json`). Tools exposed:

- `bridge_dispatch_goal(goal: str) -> conversation_id`
- `bridge_get_status(conversation_id) -> {phase, round, current_turn, pending_approval?}`
- `bridge_approve(approval_id, decision, note) -> ok`
- `bridge_cancel(conversation_id) -> ok`
- `bridge_tail_transcript(conversation_id, after_event_id) -> [events]`

**Alternative considered:**

- Unix socket + bespoke CLI: more flexible but loses Claude Code's native MCP plumbing. The MCP path means I (Claude #1) can call the bridge with a tool call and stream results as a normal tool response.
- HTTP server: overhead with no benefit for single-machine use.

**Fallback for transcript surfacing:** if MCP streaming proves clunky, Claude #1 can also `Read` `agent-bridge/state/events/conv_<id>.jsonl` directly between MCP polls.

---

## D-7 — Logging: **`structlog` matching parent repo**

**Decision:** `structlog` configured with JSON output to `agent-bridge/state/broker.log` plus colorized console. Field conventions: `conversation_id`, `event_id`, `idempotency_key`, `worker`, `phase`, `round`.

---

## D-8 — Data modeling: **Pydantic v2, frozen models**

**Decision:** all message envelopes, outbox rows, event records modeled as Pydantic `BaseModel` with `model_config = ConfigDict(frozen=True)`. Matches parent repo's invariant 21 (per `CLAUDE.md` Python coding-style rules).

---

## D-9 — Testing: **`pytest` + integration markers**

**Decision:** `pytest` with `pytest-asyncio` for async tests. Integration tests that spawn real Claude / Codex sessions tagged `@pytest.mark.live` and excluded from default runs to avoid burning subscription on every test invocation. CI policy deferred.

---

## D-10 — Codex plugin: **NEVER imported in product code** (re-affirmed)

**Decision:** restating invariant I-9. The bridge's communication with Codex goes through `app-server daemon` / `codex-client` / direct JSON-RPC. **It does NOT import, exec, or shell out to `.claude/plugins/codex/`** or the `use-codex.md` codepath. The plugin is used freely during *this development work* (rescue / review / adversarial-review on docs and diffs) but leaves no trace in `agent-bridge/src/`.

Enforcement: `grep -rE "(use-codex|\\.claude/plugins/codex|codex:rescue|codex:review|codex:adversarial-review)" agent-bridge/src/` runs in CI from CP3 onward; first execution at CP3.

---

## D-11 — Process supervision: **shell-managed for now, deferred**

**Decision:** for CP3–CP9 the user starts the broker manually (`uv run python -m agent_bridge.broker`) and stops it with Ctrl-C. Daemonization (systemd unit, launchd plist, or `tmux`-managed) deferred to CP10 or beyond.

---

## D-12 — Repository placement: **`agent-bridge/` at repo root, uv-isolated**

**Decision:** the entire bridge lives under `/data/codes/bodha/agent-bridge/`:

```text
agent-bridge/
├── GOAL.md
├── STATE.md
├── pyproject.toml             # separate uv project
├── docs/
│   ├── architecture.md
│   ├── feasibility-probes.md
│   └── tech-stack.md
├── src/agent_bridge/
│   ├── __init__.py
│   ├── broker.py
│   ├── claude_worker.py
│   ├── codex_worker.py
│   ├── outbox.py
│   ├── permission.py
│   ├── snapshot.py
│   ├── mcp_server.py
│   └── ...
├── tests/
│   ├── unit/
│   └── integration/
├── state/                     # gitignored
│   ├── bridge.sqlite
│   └── events/
└── scratchpad/                # gitignored — for probes and experiments
```

The whole tree may be left untracked initially; commit timing is a CP3 decision.

---

## Verification gate (CP2 close)

1. All Stage-A items in `feasibility-probes.md` complete (✅ as of writing).
2. Stage-B probes FP-1 through FP-8 run with results recorded; any failure either revises this doc or revises `architecture.md`.
3. One `codex review` (prose) round on the `feasibility-probes.md` + `tech-stack.md` pair; address P0/P1 findings.
4. STATE.md updated with evidence pointers (`scratchpad/probes/*` outputs + review job ID).

If Stage-B reveals a primitive doesn't support its §6.0.x contract, CP2 does NOT close — the architecture must be revised first.
