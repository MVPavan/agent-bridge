# Claude Code + OpenAI Codex: Bidirectional Multi-Agent Development Bridge

**Date:** 2026-05-20  
**Goal:** Use existing Claude Code and OpenAI Codex subscriptions to create a continuous, bidirectional development conversation where either agent can be the user interface, both can debate/plan/review, and one can implement while the other supervises or challenges.

---

## 1. Executive summary

The desired system is possible, but the right design is **not** simple delegation such as “Claude asks Codex for an independent review” or “Codex gives Claude a task.” The correct design is a **persistent two-agent conversation broker**:

```text
User / UI
   ↓
Agent Bridge / Broker
   ├── persistent Claude Code session
   └── persistent Codex thread/session
```

The broker must preserve both session IDs, route messages both ways, enforce turn-taking, keep a shared event log, manage loop limits, and provide approval gates. This is the only design that gives you true “Claude ↔ Codex” discussion rather than repeated independent calls.

The closest existing project found is **`abhishekgahlot2/codex-claude-bridge`**, described as a bidirectional bridge between Claude Code and OpenAI Codex CLI, using Claude Code Channels and a real-time web UI. It appears to be the closest match to the requirement, but should be treated as a prototype/reference architecture rather than a fully mature production platform.

A second related project/class of tools is **`claude-codex-bridge` / MCP bridge variants**, which let Claude and Codex consult each other through MCP and local authenticated CLIs. These are useful but may not always provide a truly live shared debate loop.

The best production-ready direction is to build or fork a bridge that uses:

- **Claude Code Channels** or the Claude Agent SDK for live/resumable Claude sessions.
- **Codex app-server** or Codex SDK persistent threads for live/resumable Codex sessions.
- **MCP tools** on both sides so each agent can message the other.
- **SQLite event log** for durable conversation state.
- **Git worktrees** for isolated implementation branches.
- **Consensus and loop-control protocol** for debate, planning, review, and fix cycles.

---

## 2. What you actually want

You are not asking for:

```text
Claude → one-off Codex review
Codex → one-off Claude implementation
```

You are asking for:

```text
Claude session A  ←→  persistent broker  ←→  Codex session B
```

Where both agents can:

- debate architecture;
- challenge assumptions;
- ask each other for clarification;
- agree on a plan;
- implement;
- review;
- rework;
- summarize final decisions;
- preserve context over the full task.

The intermediate broker is essential because neither Claude Code nor Codex CLI currently provides a perfect native symmetric “agent-to-agent chatroom” abstraction on its own.

---

## 3. Current product capabilities as of 2026-05-20

### 3.1 OpenAI Codex

Relevant Codex capabilities:

- Codex CLI can run locally.
- Codex supports ChatGPT subscription login and API-key login.
- Codex app/server infrastructure exposes richer agent/thread behavior.
- Codex app-server supports JSON-RPC-style communication, streamed events, approvals, conversation history, and thread lifecycle operations.
- Codex can run as an MCP server, allowing other MCP clients to call Codex.
- Codex app and VS Code integrations are evolving toward multi-agent/project command-center workflows.
- Codex app includes multi-thread/project workflows, worktrees, automations, Git integration, and review panes.

Most important primitives for this design:

```text
codex login                     # subscription-based authentication
codex app-server                # rich client / thread protocol
codex mcp-server                # expose Codex to MCP clients
Codex SDK / app-server threads  # persistent/resumable state
```

### 3.2 Claude Code

Relevant Claude Code capabilities:

- Claude Code can run interactively or headlessly.
- `claude -p` supports non-interactive scripting.
- Claude Code supports resumable sessions.
- Claude Agent SDK gives programmatic control over Claude Code-style agent loops.
- Claude Code supports MCP tools and hooks.
- Claude Code Channels can push external events into an active Claude Code session.
- Channels can be one-way or two-way; a two-way channel can expose a reply tool so Claude can send messages back.

Most important primitives for this design:

```text
claude -p                       # headless non-interactive mode
claude --resume <session_id>    # resumable session flow
Claude Agent SDK                # programmatic agent control
Claude Code Channels            # push external messages into active session
MCP tools                       # expose bridge actions to Claude
```

Claude Code Channels are especially important because they solve a key limitation: sending external messages into a running Claude session rather than forcing a new one-off process each time.

---

## 4. Existing systems found

### 4.1 `codex-claude-bridge`

**Repository:** `abhishekgahlot2/codex-claude-bridge`  
**Positioning:** “Bidirectional bridge between Claude Code and OpenAI Codex CLI. Built on Claude Code Channels. Two AI agents, one conversation, real-time web UI.”

This is the closest match to the desired solution.

Likely strengths:

- Designed specifically for Claude Code + Codex CLI.
- Bidirectional concept, not just one-off review.
- Uses Claude Code Channels, which are the right primitive for pushing external events into Claude.
- Provides a real-time web UI.
- Works around the fact that Codex and Claude do not expose identical native protocols.

Likely limitations:

- Should be considered early/prototype-grade.
- Depends on Claude Code Channels, which are relatively new/research-preview style infrastructure.
- Codex-side push/inbound semantics are less straightforward than Claude Channels.
- May need hardening around persistence, loops, permissions, worktrees, and failure recovery.

Recommendation: **Start here first. Fork it if needed.**

---

### 4.2 `claude-codex-bridge` / MCP bridge packages

Several MCP bridge-style projects exist that expose one agent to another through local authenticated CLIs.

Typical model:

```text
Claude Code → MCP tool → Codex CLI
Codex CLI   → MCP tool / shell / bridge → Claude Code
```

Strengths:

- Usually subscription-compatible because they call local CLIs.
- Easier to install than building a full broker from scratch.
- Useful for review, planning, comparison, and cross-model consultation.

Limitations:

- Often closer to request/response than true persistent debate.
- May not preserve both sides’ full session context unless explicitly designed to do so.
- May not have robust loop control, event logs, consensus detection, or web UI.

Recommendation: Useful as building blocks, but verify whether they preserve **both Claude session ID and Codex thread ID** across turns.

---

### 4.3 Official Codex plugin for Claude Code

The official Codex plugin for Claude Code is useful but not sufficient for this goal.

It is good for:

- independent Codex review;
- adversarial review;
- rescue/delegated debugging;
- Claude asking Codex for a second opinion.

It is not enough for:

- persistent Claude ↔ Codex shared debate;
- bidirectional long-lived dialogue;
- consensus-driven planning;
- Codex proactively messaging Claude mid-task;
- a shared agent-to-agent event log.

Recommendation: Keep it installed, but treat it as a **tool**, not the full architecture.

---

### 4.4 GitHub Agent HQ / GitHub third-party agents

GitHub now supports third-party coding agents such as Claude and Codex alongside Copilot agents. The model is issue/PR/task-oriented:

```text
Issue / prompt → choose agent → agent works → PR/review → iterate
```

Strengths:

- Integrated with GitHub issues, PRs, mobile, and VS Code.
- Good for asynchronous task assignment.
- Good for enterprise workflows.
- Reduces need to manage local agent processes.

Limitations for your use case:

- Not designed as a live Claude ↔ Codex debate room.
- Agents are usually parallel or sequential workers, not necessarily peers in one shared conversation.
- May not use your separate Claude Code + Codex subscriptions in the same way; may depend on GitHub Copilot plan entitlements.
- Less local control over session routing and scratchpad state.

Recommendation: Good for PR-level workflows, not the best solution for your desired live bidirectional local bridge.

---

### 4.5 VS Code multi-agent sessions

VS Code has moved toward a multi-agent command-center model, with Claude and Codex agents available as third-party agents under GitHub Copilot-backed flows.

Strengths:

- Unified IDE session management.
- Can run different agents in the same development environment.
- Useful for switching between Claude/Codex/Copilot tasks.
- Strong editor integration.

Limitations:

- Again, not clearly a true continuous Claude ↔ Codex conversation.
- More like user choosing/steering multiple agents than agents debating each other autonomously.
- May rely on Copilot subscription/preview support rather than direct Claude Code + ChatGPT subscriptions.

Recommendation: Good UI layer, but not sufficient as the core bidirectional broker.

---

### 4.6 General multi-agent frameworks

Frameworks like LangGraph, AutoGen/AG2, CrewAI, OpenAI Agents SDK, Google ADK, and similar systems are useful for multi-agent orchestration.

Strengths:

- Mature patterns for agent graphs, roles, state machines, tools, memory, and handoffs.
- Good for building the broker logic.
- Better loop control than ad-hoc shell scripts.

Limitations for your requirement:

- Many expect API keys and usage-based billing.
- They may not directly support Claude Code subscription sessions or ChatGPT/Codex subscription sessions.
- They orchestrate models, not necessarily full coding-agent products with local file-editing loops, permissions, and IDE state.

Recommendation: Use these for design inspiration or the broker state machine, but do not assume they solve subscription-based Claude Code + Codex CLI integration out of the box.

---

## 5. Recommended architecture

### 5.1 Core idea

Build or use an **Agent Bridge** that owns the conversation.

```text
                           ┌─────────────────────┐
                           │        You          │
                           │ Web UI / Claude UI  │
                           │ Codex UI / CLI      │
                           └──────────┬──────────┘
                                      │
                             goal / approval
                                      │
                           ┌──────────▼──────────┐
                           │    Agent Bridge     │
                           │ SQLite event log    │
                           │ session registry    │
                           │ debate controller   │
                           │ approval gates      │
                           └───────┬───────┬─────┘
                                   │       │
                    persistent     │       │      persistent
                    Claude session │       │      Codex thread
                                   │       │
              ┌────────────────────▼┐   ┌──▼──────────────────┐
              │     Claude Code      │   │      Codex CLI       │
              │ implementer/critic   │   │ supervisor/critic    │
              │ Channels / SDK       │   │ app-server / SDK     │
              └─────────────────────┘   └─────────────────────┘
```

### 5.2 Required invariants

The bridge must guarantee:

1. Claude session ID is preserved for the whole task.
2. Codex thread ID is preserved for the whole task.
3. Every agent-to-agent message is recorded.
4. Each agent sees the other agent’s latest relevant message.
5. There is a maximum debate/review loop count.
6. Dangerous tool use requires human approval.
7. Implementation happens in isolated git worktrees.
8. Final merge is human-approved.

---

## 6. Conversation protocol

Use a protocol rather than free-form chatter.

### 6.1 Planning/debate mode

```text
1. User gives goal.
2. Bridge sends goal to Claude and Codex.
3. Codex proposes architecture.
4. Claude critiques feasibility and codebase fit.
5. Codex responds to critique.
6. Claude either agrees or raises blockers.
7. Bridge asks both for final consensus.
8. If consensus exists, move to implementation.
9. If no consensus after N rounds, ask user.
```

### 6.2 Implementation mode

```text
1. Claude implements in worktree.
2. Claude writes summary + diff + tests run.
3. Codex reviews diff.
4. Claude responds to each finding.
5. Codex verifies fixes.
6. Bridge stops when tests pass and both agents agree.
```

### 6.3 Stop rules

Stop planning when:

```text
- both agents say AGREED, or
- no P0/P1 disagreements remain, and
- one concrete implementation plan exists, and
- max debate rounds not exceeded.
```

Stop review/fix loop when:

```text
- tests pass, and
- Codex has no blocking findings, and
- Claude has resolved or explicitly rejected all findings with reasons, and
- max fix loops not exceeded.
```

Escalate to user when:

```text
- agents disagree after max rounds;
- risky command needs approval;
- DB migration/destructive action is required;
- tests cannot be run;
- context or budget limits are hit.
```

---

## 7. Suggested file layout

```text
.agentbridge/
  config.yaml
  state.sqlite
  tasks/
    0001.task.json
  sessions/
    0001.sessions.json
  events/
    0001.events.jsonl
  artifacts/
    0001.codex-plan.md
    0001.claude-critique.md
    0001.consensus.md
    0001.diff
    0001.test.log
    0001.review.md
  worktrees/
    0001-feature-name/
  prompts/
    claude_system.md
    codex_system.md
    debate_protocol.md
    implementation_protocol.md
```

Example session registry:

```json
{
  "conversation_id": "0001",
  "claude_session_id": "claude-session-abc",
  "codex_thread_id": "codex-thread-xyz",
  "worktree": ".agentbridge/worktrees/0001-feature-name",
  "status": "debating",
  "round": 2,
  "max_rounds": 6
}
```

Example message event:

```json
{
  "id": "evt_00042",
  "conversation_id": "0001",
  "from": "codex",
  "to": "claude",
  "phase": "planning",
  "type": "critique_request",
  "content": "I propose extracting token refresh into AuthSessionManager. Please critique feasibility against the current repo.",
  "requires_reply": true,
  "created_at": "2026-05-20T15:30:00+05:30"
}
```

---

## 8. Implementation options

### Option A: Fork `codex-claude-bridge`

Best starting point.

Add:

- persistent SQLite event log;
- session registry;
- worktree manager;
- consensus detector;
- approval gates;
- loop limits;
- replayable conversation history;
- task templates;
- local web UI improvements;
- test runner integration.

Recommended if you want fastest path to a working prototype.

---

### Option B: Build custom bridge with Claude Channels + Codex app-server

Best long-term architecture.

Components:

```text
bridge-server.ts
claude-channel-server.ts
codex-app-server-client.ts
mcp-tools.ts
state-store.ts
debate-controller.ts
worktree-manager.ts
web-ui.ts
```

Codex side:

```text
- start/resume Codex app-server thread
- stream Codex events
- inject user/agent messages into thread
- preserve thread ID
```

Claude side:

```text
- create/resume Claude Code session
- receive Codex messages through Channel
- expose reply tool back to bridge
- preserve Claude session ID
```

This gives the most “real” bidirectional behavior.

---

### Option C: Use Claude as UI and Codex as MCP/tool

Claude Code becomes your main interface. The bridge pushes Codex replies into Claude via Channels.

Pros:

- Claude Channels are well-suited for incoming external events.
- Claude Code is already strong as an interactive coding UI.
- Easy to make Claude the human-facing cockpit.

Cons:

- Codex may feel more like a strong peer/tool unless the bridge carefully preserves Codex thread state.
- Need to avoid Claude dominating the conversation.

Good if you personally prefer Claude Code as the daily coding interface.

---

### Option D: Use Codex app as UI and Claude as bridge peer

Codex app/app-server becomes the cockpit. Claude is connected via a bridge/MCP/Channel service.

Pros:

- Codex app is increasingly oriented around multi-thread/project workflows.
- Strong fit if you want Codex as supervisor/orchestrator.

Cons:

- Codex CLI/TUI does not appear to have exactly the same inbound channel-push semantics as Claude Channels.
- A custom app-server integration is likely needed.

Good if you want Codex to remain the lead planner/supervisor.

---

### Option E: Use GitHub/VS Code Agent HQ style workflow

Use GitHub/VS Code as multi-agent hub and assign tasks to Claude/Codex separately.

Pros:

- Most productized route.
- Good issue/PR integration.
- Less custom plumbing.

Cons:

- Not a real live Claude ↔ Codex conversation.
- Less direct use of your independent subscriptions.
- Less local control.

Good for team/PR workflows, not your exact desired system.

---

## 9. Recommended build plan

### Phase 1 — validate existing bridge

Install and test `codex-claude-bridge` or a similar MCP bridge.

Validation checklist:

```text
[ ] Uses local Claude Code login/subscription
[ ] Uses local Codex login/ChatGPT subscription
[ ] Preserves Claude session context across multiple turns
[ ] Preserves Codex thread context across multiple turns
[ ] Claude can initiate a message to Codex
[ ] Codex can initiate a message to Claude
[ ] Both replies appear in the same UI/event log
[ ] A 5-round planning debate works without manual copy-paste
[ ] Work happens inside a git worktree
[ ] You can stop/cancel safely
```

### Phase 2 — add durable state

Add:

```text
- SQLite database
- event log table
- session table
- task table
- artifacts table
- lock table
```

### Phase 3 — add protocols

Add commands:

```text
/dual:plan <goal>
/dual:debate <question>
/dual:consensus
/dual:implement
/dual:review
/dual:fix
/dual:stop
```

### Phase 4 — add safety

Add:

```text
- max rounds
- max spend / usage alerts
- risky command denylist
- approval gates
- destructive command confirmation
- test timeout
- rollback command
```

### Phase 5 — daily development flow

Final daily flow:

```text
1. You open bridge UI or Claude/Codex UI.
2. You run /dual:plan "feature or bug".
3. Claude and Codex debate.
4. Bridge summarizes consensus.
5. You approve implementation.
6. Claude implements.
7. Codex reviews.
8. Claude fixes.
9. Bridge prepares final diff + PR text.
10. You merge manually.
```

---

## 10. Risks and caveats

### 10.1 Subscription limits

This design can consume a lot of Claude and Codex usage. Recent reporting suggests subscription-style AI coding usage is under pressure, with providers introducing stricter limits, credits, or separate meters for agent/tool usage. Design the bridge with budget and loop controls from day one.

### 10.2 Tool policy instability

Claude Code Channels and Codex app-server are powerful but relatively fast-moving. APIs and behaviors may change. Keep the bridge modular.

### 10.3 Agent loops

Without a broker, two agents can get stuck in circular debate. Always enforce max rounds and structured stop conditions.

### 10.4 Context drift

Long debates can cause both agents to lose track of original requirements. The broker should periodically summarize:

```text
- agreed decisions
- open disagreements
- accepted constraints
- implementation plan
- rejected options
```

### 10.5 Security

Both agents can run tools and edit files. Use:

```text
- git worktrees
- sandbox modes
- allowlists for commands
- approval gates
- no secrets in prompts
- no production credentials
```

---

## 11. Final recommendation

The best path is:

```text
Start with codex-claude-bridge → harden into durable Agent Bridge → use Claude Channels + Codex app-server for persistent bidirectional sessions.
```

Do not depend only on the official Claude Codex plugin. It is useful for reviews but does not give the continuous bidirectional conversation you want.

Do not depend only on GitHub/VS Code multi-agent UX. It is useful for managing multiple agents, but it is not the same as a persistent Claude ↔ Codex debate and implementation loop.

The best mental model is:

```text
The bridge is the meeting room.
Claude and Codex are participants.
You are the chairperson.
The repo/worktree is the shared workspace.
SQLite/event log is the memory.
Consensus protocol is the meeting discipline.
```

---

## 12. Source notes

Key sources checked during research:

- OpenAI Codex authentication docs — subscription vs API key sign-in.
- OpenAI Codex app-server docs — rich client integration, events, approvals, conversation history.
- OpenAI Codex CLI reference — MCP server support.
- OpenAI Codex app docs/changelog — app/worktree/multi-thread direction.
- Anthropic Claude Code headless docs — `claude -p`, SDK, resumable sessions.
- Anthropic Claude Code Channels docs — pushing events into active Claude sessions and two-way channels.
- Anthropic Claude Agent SDK session docs — session persistence and resuming.
- GitHub repo `abhishekgahlot2/codex-claude-bridge` — bidirectional Claude Code/Codex bridge.
- LobeHub/PulseMCP bridge listings — MCP bridge variants.
- GitHub Agent HQ docs/blog — third-party agents such as Claude and Codex.
- VS Code multi-agent/third-party agent docs — unified Claude/Codex sessions.
- Recent May 2026 reporting on subscription limits and competitive AI coding tooling.
