"""Non-live unit tests for the Codex worker adapter (Shape B).

Live integration covering FP-6-B sub-probes lives under `@pytest.mark.live`
and is excluded from the default test invocation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.codex_worker import (
    CapabilityProfile,
    CodexEventType,
    CodexSandboxMode,
    build_exec_argv,
    implementation_profile,
    parse_event_stream,
    planning_profile,
    resume_profile,
    review_profile,
)

# ---------- CapabilityProfile validation --------------------------------------


def test_capability_profile_is_frozen() -> None:
    p = CapabilityProfile(
        sandbox_mode=CodexSandboxMode.READ_ONLY, cwd=Path("/tmp/wt")
    )
    with pytest.raises(Exception):  # noqa: B017 — pydantic raises ValidationError or related
        p.sandbox_mode = CodexSandboxMode.WORKSPACE_WRITE


def test_capability_profile_rejects_danger_mode_via_enum() -> None:
    # `danger-full-access` is not in CodexSandboxMode at all — banned under Shape B
    # per architecture.md §6.0.2.
    values = {m.value for m in CodexSandboxMode}
    assert values == {"read-only", "workspace-write"}
    assert "danger-full-access" not in values


def test_capability_profile_rejects_extra_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017 — pydantic raises ValidationError or related
        CapabilityProfile(
            sandbox_mode=CodexSandboxMode.READ_ONLY,
            cwd=Path("/tmp/wt"),
            bogus_field=True,  # type: ignore[call-arg]
        )


# ---------- argv builder ------------------------------------------------------


def test_build_argv_first_turn_emits_sandbox_and_cwd() -> None:
    p = CapabilityProfile(
        sandbox_mode=CodexSandboxMode.READ_ONLY,
        cwd=Path("/tmp/wt"),
    )
    argv = build_exec_argv(p, "hello")
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "resume" not in argv
    assert "--json" in argv
    assert "--skip-git-repo-check" in argv
    assert "-s" in argv and "read-only" in argv
    assert "-C" in argv and "/tmp/wt" in argv
    # Prompt is the last positional.
    assert argv[-1] == "hello"


def test_build_argv_resume_omits_sandbox_flag() -> None:
    # `codex exec resume` rejects -s; the FSM-style restriction is encoded here.
    # `accept_inherited_sandbox=True` is the caller's explicit acknowledgment of
    # the sandbox-inheritance semantics (P0-3 fix from `review-mpf7lig2-p6fxyi`).
    p = CapabilityProfile(
        sandbox_mode=CodexSandboxMode.READ_ONLY,
        cwd=Path("/tmp/wt"),
        resume_thread_id="thr_abc",
        accept_inherited_sandbox=True,
    )
    argv = build_exec_argv(p, "next prompt")
    assert "resume" in argv
    assert "thr_abc" in argv
    # Sandbox flag must NOT appear on resume (verified locally on codex 0.131.0
    # where `codex exec resume -s read-only ...` errored "unexpected argument '-s' found").
    assert "-s" not in argv
    assert "read-only" not in argv


def test_build_argv_resume_without_inherited_ack_raises() -> None:
    """Regression for codex P0 — silent sandbox downgrade on resume.

    A profile with `resume_thread_id` but no `accept_inherited_sandbox=True`
    must raise. This forces the broker to either (a) acknowledge the
    inherited-sandbox semantics, or (b) start a fresh thread with the
    desired sandbox.
    """
    p = CapabilityProfile(
        sandbox_mode=CodexSandboxMode.READ_ONLY,
        cwd=Path("/tmp/wt"),
        resume_thread_id="thr_abc",
        # accept_inherited_sandbox left at default False
    )
    with pytest.raises(ValueError, match="accept_inherited_sandbox"):
        build_exec_argv(p, "next prompt")


def test_resume_profile_factory_sets_inherited_ack(tmp_path: Path) -> None:
    """`resume_profile()` derived profiles must carry the inheritance ack."""
    from agent_bridge.codex_worker import planning_profile, resume_profile

    base = planning_profile(tmp_path)
    resumed = resume_profile(base, "thr_xyz")
    assert resumed.resume_thread_id == "thr_xyz"
    assert resumed.accept_inherited_sandbox is True
    # And the argv build no longer raises.
    argv = build_exec_argv(resumed, "do thing")
    assert "resume" in argv


def test_build_argv_emits_add_dir_and_config_overrides_in_order() -> None:
    p = CapabilityProfile(
        sandbox_mode=CodexSandboxMode.WORKSPACE_WRITE,
        cwd=Path("/tmp/wt"),
        add_writable_dirs=(Path("/tmp/scratch1"), Path("/tmp/scratch2")),
        config_overrides=(("approval_policy", "untrusted"), ("model", "o3")),
    )
    argv = build_exec_argv(p, "do work")
    # Both --add-dir flags present in order
    assert argv.count("--add-dir") == 2
    assert "/tmp/scratch1" in argv
    assert "/tmp/scratch2" in argv
    # Config overrides as -c key=value pairs
    assert "-c" in argv
    assert "approval_policy=untrusted" in argv
    assert "model=o3" in argv


def test_build_argv_uses_alternate_binary() -> None:
    p = CapabilityProfile(
        sandbox_mode=CodexSandboxMode.READ_ONLY, cwd=Path("/tmp/wt")
    )
    argv = build_exec_argv(p, "x", binary="/usr/local/bin/codex-test")
    assert argv[0] == "/usr/local/bin/codex-test"


# ---------- Stream parser -----------------------------------------------------


def test_parse_stream_extracts_thread_id_and_agent_text() -> None:
    raw = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "thread.started", "thread_id": "thr_xyz"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "A"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
    )
    parsed = parse_event_stream(raw)
    assert parsed.thread_id == "thr_xyz"
    assert parsed.agent_text == "A"
    assert parsed.turn_completed
    assert len(parsed.events) == 4


def test_parse_stream_handles_multiple_agent_messages() -> None:
    raw = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "thread.started", "thread_id": "thr_1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "first "}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "second"}},
            {"type": "turn.completed"},
        ]
    )
    parsed = parse_event_stream(raw)
    assert parsed.agent_text == "first second"


def test_parse_stream_ignores_unknown_event_types() -> None:
    raw = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "thread.started", "thread_id": "thr_1"},
            {"type": "weird.event", "blob": 42},
            {"type": "turn.completed"},
        ]
    )
    parsed = parse_event_stream(raw)
    assert any(ev.type is CodexEventType.OTHER for ev in parsed.events)
    assert parsed.thread_id == "thr_1"


def test_parse_stream_skips_non_json_lines() -> None:
    raw = (
        "Reading additional input from stdin...\n"
        '{"type":"thread.started","thread_id":"thr_q"}\n'
        "2026-05-21T03:04:35Z ERROR codex_memories_write::phase2: Phase 2 no changes\n"
        '{"type":"item.completed","item":{"type":"agent_message","text":"A"}}\n'
        '{"type":"turn.completed"}'
    )
    parsed = parse_event_stream(raw)
    assert parsed.thread_id == "thr_q"
    assert parsed.agent_text == "A"
    assert parsed.turn_completed


def test_parse_stream_handles_bytes_input() -> None:
    raw = b'{"type":"thread.started","thread_id":"thr_b"}\n{"type":"turn.completed"}\n'
    parsed = parse_event_stream(raw)
    assert parsed.thread_id == "thr_b"


def test_parse_stream_empty_returns_empty_result() -> None:
    parsed = parse_event_stream("")
    assert parsed.thread_id is None
    assert parsed.agent_text == ""
    assert not parsed.turn_completed
    assert parsed.events == ()


# ---------- Profile presets ---------------------------------------------------


def test_planning_profile_is_read_only_pinned_to_worktree(tmp_path: Path) -> None:
    p = planning_profile(tmp_path)
    assert p.sandbox_mode is CodexSandboxMode.READ_ONLY
    assert p.cwd == tmp_path.resolve()
    assert p.add_writable_dirs == ()
    assert p.resume_thread_id is None


def test_review_profile_matches_planning(tmp_path: Path) -> None:
    assert review_profile(tmp_path) == planning_profile(tmp_path)


def test_implementation_profile_is_workspace_write_with_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    p = implementation_profile(tmp_path, scratch_dirs=[scratch])
    assert p.sandbox_mode is CodexSandboxMode.WORKSPACE_WRITE
    assert scratch.resolve() in p.add_writable_dirs


def test_resume_profile_preserves_base_and_adds_thread_id(tmp_path: Path) -> None:
    base = planning_profile(tmp_path)
    resumed = resume_profile(base, "thr_resume_42")
    assert resumed.resume_thread_id == "thr_resume_42"
    assert resumed.sandbox_mode == base.sandbox_mode
    assert resumed.cwd == base.cwd


# ---------- Live FP-6-B family — gated by @pytest.mark.live -------------------


@pytest.mark.live
async def test_live_fp6b_planning_read_only_blocks_outside_write(tmp_path: Path) -> None:
    """FP-6-B-planning per architecture.md §6.0.2.4.

    Read-only sandbox must block both an inside-worktree write and an
    outside-worktree write.
    """
    from agent_bridge.codex_worker import run_turn

    canary_outside = Path("/tmp/probe_fp6b_planning_canary")
    canary_outside.unlink(missing_ok=True)
    canary_inside = tmp_path / "new.txt"

    profile = planning_profile(tmp_path)
    result = await run_turn(
        profile,
        prompt=(
            f"Please attempt: echo HACKED > {canary_inside} "
            f"AND echo HACKED > {canary_outside}. Report whether each worked."
        ),
        timeout_seconds=180.0,
    )
    try:
        assert not canary_inside.exists(), "inside-worktree write should be blocked"
        assert not canary_outside.exists(), "outside-worktree write should be blocked"
        assert result.parsed.turn_completed
    finally:
        canary_outside.unlink(missing_ok=True)
