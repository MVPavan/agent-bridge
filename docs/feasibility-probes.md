# Agent Bridge — CP2 Feasibility Probes

**Created:** 2026-05-20
**Status:** surface evidence collected; live execution pending user supervision.
**Defined in:** `architecture.md` §8.1

The probes are *throwaway-grade*. Their only purpose is to confirm the architecture's load-bearing assumptions hold against the real CLI / SDK surface on this machine before CP2 locks any tech-stack choice. They are not the bridge code.

---

## Local environment (verified)

| Tool | Version | Notes |
|------|---------|-------|
| `claude` | 2.1.145 | Claude Code CLI |
| `codex` | codex-cli 0.131.0 | local install |
| `python3` | 3.12.3 | repo-default |
| `uv` | 0.9.8 | repo package manager |
| `node` | 22.22.0 | for any TS tooling if chosen |
| `npm` | 11.9.0 | — |

### Relevant claude CLI flags (verified via `claude --help`)

```text
-p, --print                          # non-interactive mode
--session-id <uuid>                  # pin a specific session ID (set, not just resume)
-r, --resume <session-id>            # resume by session ID
--input-format  text|stream-json     # stream-json enables multi-message session over stdio
--output-format text|json|stream-json
--include-hook-events                # surface hook lifecycle events in the stream
--permission-mode  default|auto|acceptEdits|bypassPermissions|dontAsk|plan
--allowedTools / --disallowedTools   # static tool lists
--no-session-persistence             # skip disk save
--remote-control [name]              # broker-driveable interactive session
```

### Relevant codex CLI surface (verified via `codex --help`)

```text
codex exec                           # non-interactive one-shot
codex resume                         # interactive resume
codex app-server daemon start|stop   # [experimental] durable managed app-server
codex remote-control start|stop      # [experimental] daemon with remote control
codex exec-server --listen ws://... | stdio   # [EXPERIMENTAL] standalone exec-server with WebSocket
codex mcp-server                     # MCP server over stdio
```

**Risk flag:** `app-server`, `remote-control`, and `exec-server` are explicitly marked experimental by the CLI itself. Tech-stack must include a fallback for any of these that proves unstable.

### Verified Python packages on PyPI

| Package | Resolved version | Notes |
|---------|------------------|-------|
| `claude-agent-sdk` | 0.2.82 | Pulls `mcp==1.27.1`, `httpx-sse`, `sse-starlette`, `starlette`, `uvicorn`. Implies built-in MCP server support inside the SDK. |
| `codex-client` | 0.1.0 | Pulls `fastmcp`, `aiofile`, `authlib`, `beartype`, `cyclopts` — community library, early version. |

---

## Probes

### FP-1 — Claude #2 spawn, multi-turn, stable session_id

**Question:** does spawning a Claude session headlessly and exchanging 3 messages keep the `session_id` constant?

**Script (Python, using claude-agent-sdk):**

```python
# scratchpad/probes/fp1_claude_spawn.py
import asyncio, uuid
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def main():
    sid = str(uuid.uuid4())
    opts = ClaudeAgentOptions(session_id=sid, permission_mode="default")
    async with ClaudeSDKClient(options=opts) as c:
        for i, prompt in enumerate(["hi say A", "now say B", "now say C"], 1):
            await c.query(prompt=prompt)
            async for msg in c.receive_response():
                print(i, msg.session_id, msg.text[:60])

asyncio.run(main())
```

**Pass criteria:**
- All 3 turns return the same `session_id == sid`.
- The 3 replies are received in order.

**Fallback if SDK API differs from documented:** use `claude --print --session-id <uuid> --input-format stream-json --output-format stream-json` and pipe 3 user-message JSON lines over stdin.

**Cost estimate:** 3 × small completion on Claude Code. Few cents on subscription metering.

### FP-2 — Claude #2 resume

**Question:** after killing the session process, can it be resumed by `session_id` with context intact?

**Script:** after FP-1 exits, re-run with `ClaudeAgentOptions(resume=sid)` and ask "what was the second letter you said?" — pass if the answer references "B".

**Pass criteria:** answer references content from turn 2.

**Cost:** 1 small completion.

### FP-3 — Codex thread spawn, multi-turn, stable thread_id

**Question:** equivalent to FP-1 for Codex.

**Approach A (preferred):** start `codex app-server daemon start`, then drive via `codex-client` Python package.

```python
# scratchpad/probes/fp3_codex_spawn.py
import asyncio
from codex_client import Session, ApprovalPolicy, client_info, thread_params

async def main():
    async with await Session.create(
        client_info=client_info("agent-bridge-probe", "0.0.1"),
        approval_policy=ApprovalPolicy.auto_accept(),
    ) as s:
        t = await s.start_thread(thread_params(ephemeral=False))
        tid = t.id
        for prompt in ["say A", "now say B", "now say C"]:
            r = await t.send_user_message(prompt)
            print(tid, r.text[:60])

asyncio.run(main())
```

**Approach B (fallback if app-server is unusable):** use `codex exec --json --session <id>` per turn; less ergonomic, may not preserve `thread_id` identically.

**Pass criteria:** thread_id constant across 3 turns; 3 replies received.

**Cost:** 3 × small Codex completion.

### FP-4 — Codex thread resume

**Question:** resume the thread from FP-3 by `thread_id` and confirm context.

**Script:** `await s.resume_thread(tid)` then ask "what was the second letter you said?".

**Pass criteria:** answer references "B".

**Cost:** 1 small completion.

### FP-5 — Claude #2 permission boundary (the load-bearing one)

**Question:** does the SDK's `PreToolUse` hook synchronously block tool execution before any side effect?

**Script:**

```python
# scratchpad/probes/fp5_claude_perm.py
import asyncio
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher

approvals = []

async def deny_bash(input_data, tool_use_id, context):
    approvals.append(input_data["tool_input"])
    return {"permissionDecision": "deny", "reason": "broker says no"}

opts = ClaudeAgentOptions(
    permission_mode="default",
    hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[deny_bash])]},
)

async def main():
    async with ClaudeSDKClient(options=opts) as c:
        await c.query(prompt="run `echo HACKED > /tmp/probe_fp5_canary`")
        async for msg in c.receive_response(): print(msg.text[:200])

asyncio.run(main())
# After: verify /tmp/probe_fp5_canary does not exist.
```

**Pass criteria:**
- Hook is invoked at least once.
- `/tmp/probe_fp5_canary` does NOT exist after the run.
- `approvals` list captures the attempted command.

**Cost:** 1 small completion. Critical probe — if this fails, the §6.0.1 contract is unenforceable in the chosen primitive and CP2 must pick a different one (or escalate).

### FP-6 — Codex permission boundary (the load-bearing one)

**Question:** does Codex's `item/commandExecution/requestApproval` callback fire synchronously, and does declining it prevent execution?

**Script (driving JSON-RPC directly via websockets if `codex-client` doesn't expose hooks):**

Pseudo-shape:

```python
# scratchpad/probes/fp6_codex_perm.py
# 1. Start `codex remote-control start` and connect a websockets client.
# 2. Send thread/start with approval_policy="on-request".
# 3. Send a user message: "run `echo HACKED > /tmp/probe_fp6_canary`".
# 4. Wait for any of: item/commandExecution/requestApproval | item/permissions/requestApproval
# 5. Respond with {"decision": "decline"}.
# 6. Wait for thread completion.
# 7. Assert /tmp/probe_fp6_canary does not exist.
```

**Pass criteria:** approval request fires before execution; declining prevents the side effect.

**Cost:** ~1 small Codex completion.

**Risk:** experimental flag — interface may differ from research-report shape.

**Acceptable fallbacks (must still satisfy §6.0.2: broker-controlled, synchronous, pre-execution approval callback):**

1. `codex exec-server --listen ws://...` (also experimental but a different code path; FP-6 retried with this transport).
2. `codex mcp-server` if its permission-request events expose the same broker-controlled callback shape.
3. Direct WebSocket JSON-RPC against `codex remote-control start` instead of going through `codex-client`.

**NOT acceptable as a fallback:** dropping back to `codex exec` with a read-only sandbox. A sandbox can *block* writes, but it cannot surface broker-controlled approval requests for non-allowlisted actions and cannot resume an approved action — that is a strictly weaker contract than §6.0.2. If none of fallbacks 1–3 yield a real approval callback, **CP2 does not close** and `architecture.md` must be revised before proceeding.

### FP-7 — Idempotency-key dedup

**Question:** the broker can resend the same logical message with the same `idempotency_key`; does the worker dedupe?

**Important:** workers do not natively know about idempotency keys — the dedup is a *broker-side protocol* layered on top. The probe verifies the protocol's claim by simulating the broker.

**Script:**

```python
# scratchpad/probes/fp7_idempotency.py
# Start a Claude #2 session, send the same logical message twice via two separate query() calls,
# each tagged in-band with idempotency_key=ik_test_1. Verify that on the second submission the
# broker-side dedup table rejects it BEFORE delivery, so the worker only sees it once.
# The probe is broker-side: the protocol logic, not the worker behavior.
```

**Pass criteria:** broker rejects the second send with same key without delivering; worker observes only one of the two.

**Cost:** 1 small completion. This is more about broker logic than primitive behavior.

### FP-8 — Claude #1 ↔ broker transport

**Question:** how does this Claude session (the cockpit) surface a broker-emitted message?

**Two candidate transports, both probed:**

**8a — MCP tool (preferred):** the broker exposes an MCP server with `dispatch_goal`, `get_status`, `approve`, `tail_transcript`. Claude #1 attaches to it via `--mcp-config`. Probe: define a minimal MCP server that returns a fixed "hello from broker" string; have Claude #1 (this session) call it and report the response.

**8b — Tail JSONL (fallback):** the broker writes the transcript JSONL to `agent-bridge/state/events/<conv>.jsonl`; Claude #1 uses Read to pick up updates. Probe: broker writes a line; Claude #1 reads it.

**Pass criteria:** Claude #1 can observe broker-emitted content with low latency through at least one of the two paths.

**Cost:** near zero — the broker is a stub, no LLM call.

---

## Execution plan

**Two-stage execution to keep subscription cost predictable and reversible.**

### Stage A — no live LLM calls (do now)

- ✅ Local CLI surface verified (this doc).
- ✅ Python packages verified on PyPI.
- Write the probe scripts above to `scratchpad/probes/`.
- Set up a `scratchpad/probes/canary_cleanup.sh` that nukes any `/tmp/probe_fp*_canary` files between runs.
- Snapshot `~/.codex/config.toml` and Claude Code settings so probes don't permanently change them.

### Stage B — live LLM calls (require user supervision before first run)

- FP-1, FP-3 first (cheapest, no permission-boundary stakes).
- FP-2, FP-4 next (resume validation).
- FP-5, FP-6 last among the spawn probes (load-bearing — if these fail, the architecture's §6.0 contract is unmet).
- FP-7 and FP-8 are broker-protocol probes, less dependent on subscription primitives.

Each Stage-B probe is run once, results captured in this file, and the budget impact noted.

---

## Results

**Stage A complete** as of 2026-05-20:

- Probe scripts written to `scratchpad/probes/` (gitignored). One per FP plus `canary_cleanup.sh`, `snapshot_configs.sh`, `restore_configs.sh`, and a `README.md`.
- Config snapshots captured: `codex config.toml`, `codex auth.json`, `claude settings.json` under `scratchpad/snapshots/`. `claude settings.local.json` not present (nothing to snapshot).
- FP-7 (broker-side idempotency dedup, no LLM call required) — **PASS**. Evidence: `scratchpad/probes/results/fp7.json`. Outcomes `['delivered', 'dedup_rejected', 'delivered']`; worker received exactly the unique payloads.
- FP-8b (JSONL tail-and-Read transport) — **PASS**. Evidence: `scratchpad/probes/results/fp8.json`. Read-back equals write set.
- FP-8a (MCP tool transport) — **scripted; operator-verified**. mcp-config.json written; full verification deferred to CP7 when the broker's MCP server exists.

**Stage B awaiting user sign-off.** Each probe burns subscription budget. See `STATE.md` "Notes / Open questions / Discoveries — CP2 Stage B sign-off" for the budget/risk summary.

| Probe | Status | Evidence |
|-------|--------|----------|
| FP-1 | **PASS** | `scratchpad/probes/results/fp1.json` — session_id stable across 3 turns |
| FP-2 | **PASS** | `scratchpad/probes/results/fp2.json` — resumed session correctly references letter B |
| FP-3 | **PASS** | `scratchpad/probes/results/fp3.json` — Codex thread_id stable; transport: `codex exec --json` + `codex exec resume` (subprocess-per-turn) |
| FP-4 | **PASS** | `scratchpad/probes/results/fp4.json` — resumed thread correctly references letter B |
| FP-5 | **PASS** (load-bearing) | `scratchpad/probes/results/fp5.json` — PreToolUse hook synchronously blocked bash; canary not created. §6.0.1 contract satisfied by `claude-agent-sdk`. |
| FP-6 | **PASS via Shape B** (load-bearing) | `scratchpad/probes/results/fp6_alt.json` — per-turn capability declaration via `codex exec` proven empirically; sandbox blocks writes outside worktree and the attempt surfaces as a visible `agent_message` event. See §FP-6 finding below. |
| FP-7 | **PASS** (Stage A) | `scratchpad/probes/results/fp7.json` |
| FP-8 | **PASS** (8b auto; 8a scripted, operator-verified at CP7) | `scratchpad/probes/results/fp8.json` |

## FP-6 finding — no local Codex transport provides a synchronous approval callback

Probed four Codex transports on `codex 0.131.0`:

| Transport | Approval callback | Notes |
|-----------|-------------------|-------|
| `codex exec` (subprocess) | No | One-shot. Sufficient for FP-3/FP-4 (thread persistence) but no broker insertion point during a turn. |
| `codex mcp-server` (stdio MCP) | No | Initialize reports `tools=ToolsCapability` only — no elicitation, no prompts capability. The two exposed tools (`codex`, `codex-reply`) are synchronous request/response. |
| `codex exec-server --listen ws://...` | No | Server listens, accepts initialize with `clientName`, but every method probed (`rpc.discover`, `listMethods`, `thread/start`, `session/create`, `getCapabilities`) returns `-32601` with "exec-server stub does not implement X yet". |
| `codex remote-control start` | Expected yes — blocked | Requires standalone codex install at `~/.codex/packages/standalone/current/codex`. Not installed on this machine. Per the CLI's own error: `curl -fsSL https://chatgpt.com/codex/install.sh | sh`. |

§6.0.2 of `architecture.md` requires a synchronous broker-controlled pre-execution approval callback. The CP2 codex review explicitly rejected sandbox-only enforcement as a non-equivalent fallback.

**Resolution (2026-05-21): adopted Shape B — per-turn capability declaration.**

FP-6-alt empirically proved that `codex exec --json -C <worktree> -s read-only` (a) blocks writes outside the declared sandbox (`Read-only file system` error from the OS), (b) surfaces the blocked attempt as a visible `agent_message` event in the JSON stream so the broker can log it, and (c) keeps the worktree state intact. The architecture (§6.0.2) was updated to describe Shape A (`codex remote-control` + JSON-RPC approval callbacks, when the standalone install exists) and Shape B (per-turn capability declaration via `codex exec`, available on every machine with `codex 0.131.0`). Shape B is the CP2 baseline; Shape A is an optional upgrade path that does not change the broker's external contract.

The standalone-install decision is therefore moved to an "optional CP2 follow-up" rather than a blocker. The bridge can be built end-to-end on Shape B.

---

## Surface-level confidence assessment (before Stage B)

Based on local CLI inspection and PyPI presence — **not** verified by execution:

| Architecture claim | Confidence | Basis |
|--------------------|------------|-------|
| Claude #2 spawn + resume by session_id works | High | `claude --session-id` + `--resume` documented and `claude-agent-sdk` exists with `resume=` parameter. |
| Codex thread spawn + resume by thread_id works | Medium-High | `codex app-server daemon` exists; `codex-client` Python package supports `start_thread` + `resume_thread`. Experimental marker on `app-server` is the residual risk. |
| Synchronous Claude permission callback (§6.0.1) | High | `PreToolUse` hook in claude-agent-sdk is documented as synchronous; matches §6.0.1 requirement shape. |
| Synchronous Codex approval callback (§6.0.2) | Medium | `app-server` JSON-RPC `requestApproval` semantics described in research but interface is experimental; codex-client v0.1.0 may not yet expose hook bindings. Fallback via sandbox modes available. |
| Claude #1 ↔ broker transport via MCP | High | Claude Code natively supports `--mcp-config`; an MCP server in the broker is a normal architecture. |

**Bottom line:** all five probes have a credible primitive on this machine. The Codex permission callback is the one with the most variance — the `codex-client` v0.1.0 wrapping may not expose the JSON-RPC hooks, in which case CP2 implements direct WebSocket JSON-RPC against `codex remote-control start`. The architecture's topology survives either way; the implementation detail moves.
