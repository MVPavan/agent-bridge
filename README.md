# agent-bridge

Bidirectional Claude-Claude-Codex development bridge.

## Status

**Structurally complete.** GOAL.md §Definition of Done satisfied: 3 of 4 gates PASS, gate 4's deterministic half PASS with the live-LLM half explicitly deferred for the operator. Built across CP1–CP11 with 9 codex adversarial-review rounds (see `STATE.md` for the full trail).

- `uv run pytest` — **172 passed, 2 deselected** (the deselected pair is `@pytest.mark.live` Claude / Codex probes; opt in with `uv run pytest -m live`)
- `uv run ruff check src tests` — all checks passed
- `uv run mypy src tests` — `--strict` clean across 31 source files
- I-9 grep — empty (no codex-plugin imports under `src/`)
- 14 production modules under `src/agent_bridge/`; 14 test modules (12 unit + 2 integration)

## Layout

```
agent-bridge/
├── GOAL.md          immutable goal + Definition of Done
├── STATE.md         CP1–CP11 progress + 9 codex review rounds + operator next steps
├── docs/
│   ├── architecture.md       the spec (~830 lines, hardened via 5 + 3 codex rounds)
│   ├── feasibility-probes.md FP-1..FP-8 results
│   └── tech-stack.md         D-1..D-12 decisions (Shape A vs B for §6.0.2)
├── src/agent_bridge/         broker, store, mcp_server, worker adapters, ...
├── tests/                    unit + integration; @live markers opt-in
├── pyproject.toml            separate uv project
└── scratchpad/probes/        gitignored throwaway CP2 probe scripts (also at /data/codes/bodha/scratchpad/probes/ from before the move)
```

## Quick start

```bash
cd /data/codes/agent-bridge
uv sync
uv run pytest
uv run agent-bridge --help
```

## Hard rule (invariant I-9)

The product code under `src/agent_bridge/` MUST NEVER import the Claude Code codex plugin. Enforcement grep (must return empty):

```bash
grep -rE "(use-codex|\\.claude/plugins/codex|codex:rescue|codex:review|codex:adversarial-review)" src/
```

The plugin is freely used during development for adversarial review of docs/diffs, but never wired into the product. See `docs/architecture.md` §7.

## What the operator can do next (all optional)

None of these are required. The bridge is built and verified.

1. **Initial git commit.** 42 files are staged on `main`, never committed.
2. **Start the codex daemon** (now possible after the standalone install of `codex-cli 0.132.0` at `~/.codex/packages/standalone/current/codex`): `codex remote-control start`.
3. **Optional Shape A upgrade for Codex worker.** Per-tool-call approval callbacks instead of the per-turn capability declaration the bridge currently uses. Documented in `docs/architecture.md` §6.0.2.1 and `docs/tech-stack.md` D-3.
4. **Run gated `@live` tests.** `uv run pytest -m live` — burns subscription.
5. **Real-world live smoke.** Drive a tiny coding task end-to-end via Claude Code + `--mcp-config agent-bridge/mcp-config.json`. Path documented in `STATE.md` CP11 §Live smoke.

See `STATE.md` "What the operator can do next" for the detailed commands and rationale.

## Standing P1 deferrals (documented in STATE.md)

1. **Snapshot wiring into broker impl/review turns.** Snapshot/validate infrastructure is code-complete (`snapshot.py`, `WorktreeManager`) and tested; `broker.deliver_turn_to` currently dispatches only planning/debate turns. Wiring snapshot into impl/review belongs with the CP9 "broker drives worktree create/destroy" follow-up.
2. **Concrete `ClaudeWorkerDriver` / `CodexWorkerDriver` shims.** The broker's `WorkerDriver` contract is fully verified against `FakeWorkerDriver` in tests. Live-SDK shims are the next-milestone adapter layer, gated on operator-supervised live testing per CP11.

Both deferrals match the same operator-supervised-action pattern as CP11's live smoke.
