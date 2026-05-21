# Agent Bridge — Goal

**Created:** 2026-05-20

## Mission

Build a bidirectional Claude–Claude–Codex development bridge with this topology:

- **Claude #1 (cockpit):** the user's only interaction point. Orchestrates the bridge. Surfaces all results.
- **Claude #2 (peer worker):** a second Claude Code session that debates / plans / implements / reviews.
- **Codex (peer worker):** a Codex session that debates / plans / reviews alongside Claude #2.

## Communication flow

- User ↔ Claude #1 (interactive)
- Claude #1 → Claude #2, Codex (dispatch goals, pull results)
- Claude #2 ↔ Codex (live, bidirectional, multi-turn — the debate loop)
- Claude #1 ← Claude #2, Codex (transcripts, decisions, diffs flow back here)

## Hard constraints

1. **The product MUST NOT use the codex plugin** (`.claude/commands/use-codex.md`, `.claude/plugins/codex/*`, etc.). The plugin is a one-way critic. The product is a peer-to-peer broker. Different beast. Verified by `grep -r` returning no plugin imports under `agent-bridge/src/` at final check.
2. **During this development work (meta)**, the codex plugin CAN be used for `rescue` / `review` / `adversarial-review` on architecture docs, plans, and diffs. Follow `.claude/commands/use-codex.md`.
3. **No third-party web UI.** Claude #1 (this session's interface) is the UI.
4. **Session/thread IDs preserved** for the full lifetime of any task — Claude #2 session ID and Codex thread ID must survive across all turns.
5. **All agent-to-agent messages logged** in a durable event store.
6. **Loop limits + approval gates** are not optional — required from CP1's design forward.
7. **Implementation happens in git worktrees**, never directly on the user's working branch.

## Reference

The research that motivated this design lives at `claude_codex_bidirectional_agent_bridge_research.md` in the repo root. Treat it as a starting reference, not a spec. CP1 will produce the actual spec.

## Definition of Done — final smoke test

End-to-end run from Claude #1:

1. Dispatch a small coding goal (e.g., "add a one-line utility function in a throwaway test repo").
2. Bridge spins up Claude #2 + Codex sessions, both IDs preserved.
3. Claude #2 ↔ Codex debate produces a consensus plan (≤ 5 rounds, stop rules respected).
4. Claude #2 implements in a worktree.
5. Codex reviews, Claude #2 fixes, both agree.
6. Results, transcript, and diff stream back into Claude #1's view.
7. Event log captures every agent-to-agent message with session IDs intact.
8. All safety gates (max rounds, approval prompts, command allowlist) functional.

## Final verification (all must pass before exiting the loop)

- [ ] All 11 checkpoints in `STATE.md` marked complete with fresh evidence each.
- [ ] `grep -rE "(use-codex|\\.claude/plugins/codex)" agent-bridge/src/` returns no matches.
- [ ] One `codex adversarial-review` round on the final integrated system: 0 P0, ≤ 2 P1 findings; each P0/P1 either fixed or explicitly deferred with reason.
- [ ] Smoke test (above) reproducible end-to-end on a clean run.

## Notes

- The user (the human) is the chairperson. Claude #1 is the meeting secretary. Claude #2 and Codex are the debaters.
- The bridge is the meeting room. The repo/worktree is the shared workspace. The event log is the memory. The consensus protocol is the meeting discipline.
