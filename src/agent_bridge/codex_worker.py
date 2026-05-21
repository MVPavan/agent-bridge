"""Codex worker adapter (Shape B, the CP2 baseline).

Spec: docs/architecture.md §6.0.2 (Shape B = per-turn capability declaration
via `codex exec`).

Driving Codex through one-shot `codex exec --json` invocations gives the
broker per-turn authorization (it picks the capability profile up front),
OS-enforced sandbox containment, and a streaming JSON event log. The
broker has no per-call approval callback under this shape — see the
threat-model statement in §6.0.2.2 for what that gives up.

Shape A (the strong per-call approval callback via `codex remote-control`)
is not implemented in this module. It would require the standalone codex
install at `~/.codex/packages/standalone/current/codex`; see
`docs/feasibility-probes.md` §FP-6 family.

Public surface:

- `CapabilityProfile` — frozen Pydantic model of the per-turn capability set.
- `CodexEvent` — parsed JSON event from the `codex exec --json` stream.
- `CodexTurnResult` — full turn outcome (thread_id, agent text, all events,
  exit code, plus the capability profile recorded at turn start per §6.0.2.3 #2).
- `parse_event_stream` — pure parser: bytes → list[CodexEvent] + extracted summary.
- `build_exec_argv` — pure builder: capability profile + prompt → argv list.
- `run_turn` — async coroutine that spawns `codex exec` and collects the result.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------- Capability profile ------------------------------------------------


class CodexSandboxMode(StrEnum):
    """Allowed `--sandbox` values under Shape B.

    Per architecture.md §6.0.2: `danger-full-access` is **banned** under
    Shape B. Workflows that genuinely need it must be human-approved
    out-of-band, not a bridge feature.
    """

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class CapabilityProfile(BaseModel):
    """The full Shape B capability surface for one Codex turn.

    The broker constructs this **before** invoking Codex. It is the
    "approval object" of Shape B: persisted at turn start as evidence of
    what the worker was authorized to do.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sandbox_mode: CodexSandboxMode
    cwd: Path = Field(description="Conversation worktree root; absolute path.")
    add_writable_dirs: tuple[Path, ...] = ()
    # Free-form `-c key=value` overrides. The broker normalizes order so
    # the recorded profile is canonical and comparable across turns.
    config_overrides: tuple[tuple[str, str], ...] = ()
    skip_git_repo_check: bool = True
    # If set, the broker is resuming an existing thread; turn 1 leaves this None.
    resume_thread_id: str | None = None
    # When resuming, the `codex exec resume` CLI does NOT accept -s/--sandbox;
    # the resumed thread inherits the sandbox the thread was originally started
    # with. To prevent silent capability downgrade (codex P0
    # `review-mpf7lig2-p6fxyi` for §6.0.2 Shape B), the caller MUST explicitly
    # acknowledge inherited-sandbox semantics by setting this flag together
    # with `resume_thread_id`. `build_exec_argv` raises if `resume_thread_id`
    # is set without it.
    accept_inherited_sandbox: bool = False


# ---------- Event stream parsing ----------------------------------------------


class CodexEventType(StrEnum):
    """The subset of `codex exec --json` event types we care about.

    The CLI emits more types; anything outside this enum is captured as
    `CodexEventType.OTHER` with the raw payload preserved.
    """

    THREAD_STARTED = "thread.started"
    TURN_STARTED = "turn.started"
    ITEM_COMPLETED = "item.completed"
    TURN_COMPLETED = "turn.completed"
    OTHER = "other"


@dataclass(frozen=True)
class CodexEvent:
    """One parsed JSON event from the `codex exec --json` stream."""

    type: CodexEventType
    raw_type: str  # the original event_type before normalization
    payload: dict[str, Any]


@dataclass(frozen=True)
class ParsedStream:
    """Aggregated view of one full turn's event stream."""

    events: tuple[CodexEvent, ...]
    thread_id: str | None
    agent_text: str
    turn_completed: bool


def parse_event_stream(raw: str | bytes) -> ParsedStream:
    """Walk the JSON-lines output of `codex exec --json`."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    events: list[CodexEvent] = []
    thread_id: str | None = None
    agent_text_parts: list[str] = []
    turn_completed = False

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_type = str(payload.get("type", ""))
        try:
            etype = CodexEventType(raw_type)
        except ValueError:
            etype = CodexEventType.OTHER

        events.append(CodexEvent(type=etype, raw_type=raw_type, payload=payload))

        if etype is CodexEventType.THREAD_STARTED:
            tid = payload.get("thread_id")
            if isinstance(tid, str):
                thread_id = tid
        elif etype is CodexEventType.ITEM_COMPLETED:
            item = payload.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    agent_text_parts.append(text)
        elif etype is CodexEventType.TURN_COMPLETED:
            turn_completed = True

    return ParsedStream(
        events=tuple(events),
        thread_id=thread_id,
        agent_text="".join(agent_text_parts),
        turn_completed=turn_completed,
    )


# ---------- argv builder -------------------------------------------------------


def build_exec_argv(
    profile: CapabilityProfile,
    prompt: str,
    *,
    binary: str = "codex",
) -> list[str]:
    """Translate a CapabilityProfile + prompt into a `codex exec` argv list.

    If `profile.resume_thread_id` is set, we use `codex exec resume`;
    otherwise we use `codex exec`. The `--sandbox`, `-C`, `--add-dir`,
    and `-c key=value` flags are emitted in a canonical order so the
    recorded profile is reproducible.
    """
    argv: list[str] = [binary, "exec"]
    if profile.resume_thread_id:
        # Silent sandbox downgrade was a P0 in `review-mpf7lig2-p6fxyi`: a
        # `review` turn resumed onto an `implementation` thread would inherit
        # workspace-write rather than the broker-chosen read-only. Callers
        # must explicitly acknowledge inherited-sandbox semantics; if they
        # need a different sandbox they must start a fresh thread.
        if not profile.accept_inherited_sandbox:
            raise ValueError(
                "Resuming a thread with `resume_thread_id` set inherits the "
                "thread's original sandbox; `codex exec resume` does not "
                "accept `-s/--sandbox`. To avoid silent capability downgrade "
                "per §6.0.2 Shape B, set `accept_inherited_sandbox=True` "
                "explicitly when resuming (and ensure the requested "
                "`sandbox_mode` matches the thread's lineage), or start a "
                "fresh thread (`resume_thread_id=None`) for a different "
                "sandbox."
            )
        argv.append("resume")
        argv.append(profile.resume_thread_id)
    argv.append("--json")
    if profile.skip_git_repo_check:
        argv.append("--skip-git-repo-check")
    # `resume` does not accept -s / --sandbox; the original thread's sandbox
    # persists. Skip these on resume to avoid the CLI rejecting the flags.
    # The caller has already acknowledged this above via accept_inherited_sandbox.
    if not profile.resume_thread_id:
        argv.extend(["-s", profile.sandbox_mode.value])
    argv.extend(["-C", str(profile.cwd)])
    for d in profile.add_writable_dirs:
        argv.extend(["--add-dir", str(d)])
    for key, value in profile.config_overrides:
        argv.extend(["-c", f"{key}={value}"])
    argv.append(prompt)
    return argv


# ---------- Turn runner --------------------------------------------------------


@dataclass(frozen=True)
class CodexTurnResult:
    """Full outcome of one `codex exec` invocation.

    The `profile` field is what the broker durably appends to the event
    log at turn start (§6.0.2.3 #2 — Shape B's "approval object").
    """

    profile: CapabilityProfile
    parsed: ParsedStream
    exit_code: int
    stderr: str
    argv: tuple[str, ...]


class CodexInvocationError(RuntimeError):
    """Raised when `codex exec` exits non-zero AND emitted no `turn.completed`."""


async def run_turn(
    profile: CapabilityProfile,
    prompt: str,
    *,
    binary: str = "codex",
    timeout_seconds: float = 300.0,
) -> CodexTurnResult:
    """Spawn `codex exec`, drive one turn, return the structured result.

    The broker calls this once per Codex turn. Capability changes between
    turns require a new `CapabilityProfile`.
    """
    argv = build_exec_argv(profile, prompt, binary=binary)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(profile.cwd),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    parsed = parse_event_stream(stdout_bytes)
    exit_code = proc.returncode if proc.returncode is not None else -1

    if exit_code != 0 and not parsed.turn_completed:
        raise CodexInvocationError(
            f"codex exec exited {exit_code} without completing a turn. "
            f"stderr: {stderr_bytes.decode('utf-8', errors='replace')[:500]}"
        )

    return CodexTurnResult(
        profile=profile,
        parsed=parsed,
        exit_code=exit_code,
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        argv=tuple(argv),
    )


# ---------- Convenience: profile presets matching §6.0.2.4 --------------------


def planning_profile(worktree: Path) -> CapabilityProfile:
    """Read-only profile for planning/debate turns."""
    return CapabilityProfile(
        sandbox_mode=CodexSandboxMode.READ_ONLY, cwd=worktree.resolve()
    )


def review_profile(worktree: Path) -> CapabilityProfile:
    """Read-only profile for review turns."""
    return planning_profile(worktree)


def implementation_profile(
    worktree: Path, scratch_dirs: Iterable[Path] = ()
) -> CapabilityProfile:
    """workspace-write profile for implementation turns inside the worktree."""
    return CapabilityProfile(
        sandbox_mode=CodexSandboxMode.WORKSPACE_WRITE,
        cwd=worktree.resolve(),
        add_writable_dirs=tuple(Path(d).resolve() for d in scratch_dirs),
    )


def resume_profile(
    base: CapabilityProfile, thread_id: str
) -> CapabilityProfile:
    """Derive a resume-profile from a base profile.

    Sets `accept_inherited_sandbox=True` because by definition a resume on
    `codex exec resume` inherits the thread's original sandbox (the CLI does
    not accept `-s/--sandbox` on resume). The caller must ensure `base.sandbox_mode`
    matches the lineage; if it doesn't, start a fresh thread instead.
    """
    return base.model_copy(
        update={
            "resume_thread_id": thread_id,
            "accept_inherited_sandbox": True,
        }
    )
