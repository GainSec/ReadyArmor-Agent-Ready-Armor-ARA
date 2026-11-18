"""Run Task action gate.

This module is intentionally small: it turns a persisted TaskRunSession into
a policy decision before target work executes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from . import debug_log
from . import task_session
from .policy import Decision


TASK_COMMIT_MCP_TOOL_ORDER = (
    "mcp_ara_forge_lead_file",
    "mcp_ara_forge_chunks_emit_tag",
    "mcp_ara_forge_fact_assert",
    "mcp_ara_forge_finding_file",
)
TASK_BRANCH_MCP_TOOL_ORDER = (
    "mcp_ara_forge_lead_file",
    "mcp_ara_forge_chunks_emit_tag",
    "mcp_ara_forge_finding_file",
)
TASK_CHUNK_TRANSITION_MCP_TOOLS = (
    "mcp_ara_forge_chunks_emit_tag",
)


def evaluate_query(
    state_root: Path,
    msg: dict,
    *,
    environ: Mapping[str, str] | None = None,
) -> Decision | None:
    env = os.environ if environ is None else environ
    task_id = str(env.get("ARA_TASK_RUN_ID") or "").strip()
    if not task_id:
        debug_log.emit(
            "task-gate", "skip.no_task",
            state_root=state_root,
            op=msg.get("op"),
            target=_target_label(msg),
        )
        return None
    if is_task_control_plane_action(msg):
        debug_log.emit(
            "task-gate", "skip.control_plane",
            state_root=state_root,
            task_id=task_id,
            op=msg.get("op"),
            target=_target_label(msg),
        )
        return None
    if not _is_target_action(msg):
        debug_log.emit(
            "task-gate", "skip.not_target",
            state_root=state_root,
            task_id=task_id,
            op=msg.get("op"),
            target=_target_label(msg),
        )
        return None
    try:
        run = task_session.TaskRunSession.load(state_root, task_id)
    except Exception as exc:
        debug_log.emit_exception(
            "task-gate", "decision.session_missing",
            exc,
            state_root=state_root,
            task_id=task_id,
            op=msg.get("op"),
            target=_target_label(msg),
        )
        return Decision(
            verdict="deny",
            reason="TASK_SESSION_MISSING",
            rule_id="task.run.missing",
            touched=_target_label(msg),
        )
    action = run.record_action(str(msg.get("op") or ""), msg)
    if action.allowed:
        debug_log.emit(
            "task-gate", "decision.allow",
            state_root=state_root,
            task_id=task_id,
            op=msg.get("op"),
            target=_target_label(msg),
        )
        return None
    debug_log.emit(
        "task-gate", "decision.deny",
        state_root=state_root,
        task_id=task_id,
        op=msg.get("op"),
        target=_target_label(msg),
        reason=action.reason or "TASK_ACTION_DENIED",
    )
    return Decision(
        verdict="deny",
        reason=action.reason or "TASK_ACTION_DENIED",
        rule_id="task.run.action_gate",
        touched=_target_label(msg),
    )


def record_commit(
    state_root: Path,
    channel: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    if not is_commit_channel(channel):
        debug_log.emit(
            "task-gate", "commit.ignored_non_commit_channel",
            state_root=state_root,
            channel=channel,
        )
        return False
    env = os.environ if environ is None else environ
    task_id = str(env.get("ARA_TASK_RUN_ID") or "").strip()
    if not task_id:
        debug_log.emit(
            "task-gate", "commit.ignored_no_task",
            state_root=state_root,
            channel=channel,
        )
        return False
    try:
        run = task_session.TaskRunSession.load(state_root, task_id)
    except Exception as exc:
        debug_log.emit_exception(
            "task-gate", "commit.session_missing",
            exc,
            state_root=state_root,
            task_id=task_id,
            channel=channel,
        )
        return False
    reason = commit_channel_rejection_reason(run, channel)
    if reason:
        debug_log.emit(
            "task-gate", "commit.rejected",
            state_root=state_root,
            task_id=task_id,
            channel=channel,
            reason=reason,
            phase=run.phase.value,
            chunk_gated=run.chunk_gated,
        )
        return False
    run.record_commit(channel)
    debug_log.emit(
        "task-gate", "commit.recorded",
        state_root=state_root,
        task_id=task_id,
        channel=channel,
    )
    return True


def commit_channel_rejection_reason(
    run: task_session.TaskRunSession,
    channel: str,
) -> str | None:
    channel = str(channel or "")
    if not run.chunk_gated:
        return None
    if (
        run.phase == task_session.TaskPhase.BRANCH_REQUIRED
        and channel != "chunks.emit_tag"
    ):
        return "CHUNK_TRANSITION_REQUIRED"
    return None


def task_commit_channel_rejection_reason(
    state_root: Path,
    channel: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    if not is_commit_channel(channel):
        return None
    env = os.environ if environ is None else environ
    task_id = str(env.get("ARA_TASK_RUN_ID") or "").strip()
    if not task_id:
        return None
    try:
        run = task_session.TaskRunSession.load(state_root, task_id)
    except Exception as exc:
        debug_log.emit_exception(
            "task-gate", "commit_rejection.session_missing",
            exc,
            state_root=state_root,
            task_id=task_id,
            channel=channel,
        )
        return "TASK_SESSION_MISSING"
    return commit_channel_rejection_reason(run, channel)


def task_debt_mcp_tool_names(
    state_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...] | None:
    env = os.environ if environ is None else environ
    if env.get("ARA_TASK_SUPERVISOR") != "armory-hermes-loop":
        return None
    task_id = str(env.get("ARA_TASK_RUN_ID") or "").strip()
    if not task_id:
        return None
    try:
        run = task_session.TaskRunSession.load(state_root, task_id)
    except Exception as exc:
        debug_log.emit_exception(
            "task-gate", "debt_tools.session_missing",
            exc,
            state_root=state_root,
            task_id=task_id,
        )
        return None
    if run.phase == task_session.TaskPhase.COMMIT_REQUIRED:
        return TASK_COMMIT_MCP_TOOL_ORDER
    if run.phase == task_session.TaskPhase.BRANCH_REQUIRED:
        if run.chunk_gated:
            return TASK_CHUNK_TRANSITION_MCP_TOOLS
        return TASK_BRANCH_MCP_TOOL_ORDER
    return None


def is_commit_channel(channel: str) -> bool:
    return str(channel or "") in task_session.COMMIT_CHANNELS


def _is_target_action(msg: dict) -> bool:
    op = str(msg.get("op") or "")
    if op == "exec.spawn":
        if _is_hermes_shell_tool_transport(msg):
            return False
        return True
    if op == "browser.nav":
        return True
    return False


def _is_internal_task_action(msg: dict) -> bool:
    op = str(msg.get("op") or "")
    if op == "net.connect":
        host = str(msg.get("host") or "").strip().lower()
        return _is_control_plane_host(host)
    if op != "exec.spawn":
        return False
    command = Path(str(msg.get("command") or "")).name
    args = [str(a) for a in (msg.get("args") or [])]
    joined = " ".join(args)
    if command == "hermes":
        return (
            "--continue" in args and any(a.startswith("ara-task-") for a in args)
        ) or _is_task_recovery_prompt(args)
    if command == "timeout":
        return (
            "/hermes" in joined and "--continue" in joined and "ara-task-" in joined
        ) or (
            "/hermes" in joined and _is_task_recovery_prompt(args)
        )
    if command in {"bash", "sh"} and _is_hermes_shell_bootstrap(args):
        return True
    if command in {"python3.9", "python3", "python"}:
        if args[:3] == ["-m", "ara.cli", "forge-mcp"]:
            return True
        if args == ["-m", "ara.cli", "task-repair-commit"]:
            return True
        if _is_task_loop_python_helper(args):
            return True
        if args[:3] == ["-m", "pip", "--version"]:
            return True
        if args[:2] == ["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"]:
            return True
        if len(args) == 2 and args[0] == "-c" and "EXTERNALLY-MANAGED" in args[1]:
            return True
    if command == "file":
        operands = [a for a in args if not a.startswith("-")]
        return len(operands) == 1 and (
            "/python" in operands[0]
            or operands[0].endswith("/python3")
            or operands[0].endswith("/python3.9")
            or operands[0].endswith("/python3.11")
        )
    if command == "pip":
        return args == ["--version"]
    if command == "uname":
        return args in (["-p"], ["-m"])
    return False


def is_task_control_plane_action(msg: dict) -> bool:
    return _is_internal_task_action(msg)


def is_task_shell_transport_action(msg: dict) -> bool:
    if str(msg.get("op") or "") != "exec.spawn":
        return False
    command = Path(str(msg.get("command") or "")).name
    return command in {"bash", "sh"}


def _is_hermes_shell_tool_transport(msg: dict) -> bool:
    if str(msg.get("op") or "") != "exec.spawn":
        return False
    command = Path(str(msg.get("command") or "")).name
    if command not in {"bash", "sh"}:
        return False
    joined = "\n".join(str(a) for a in (msg.get("args") or []))
    return _is_hermes_shell_bootstrap([joined])


def _is_hermes_shell_bootstrap(args: list[str]) -> bool:
    joined = "\n".join(args)
    if "hermes-snap-" in joined and "export -p >" in joined and "\neval " not in joined:
        return True
    return False


def _is_task_recovery_prompt(args: list[str]) -> bool:
    return any(
        a.startswith("COMMIT_REQUIRED. You have taken a target action")
        or a.startswith("BRANCH_REQUIRED. The investigation ledger needs")
        for a in args
    )


def _is_task_loop_python_helper(args: list[str]) -> bool:
    if len(args) != 2 or args[0] != "-c":
        return False
    script = args[1]
    if "ARA_STATE_ROOT" not in script:
        return False
    if (
        "events.jsonl" in script
        and "channel." in script
        and "status" in script
    ):
        return True
    if (
        "task-runs" in script
        and "session.json" in script
        and "phase" in script
    ):
        return True
    return False


def _is_control_plane_host(host: str) -> bool:
    if not host:
        return False
    allowed = {
        "openrouter.ai",
        "api.openai.com",
        "api.anthropic.com",
    }
    if host in allowed:
        return True
    return any(host.endswith("." + suffix) for suffix in allowed)


def _target_label(msg: dict) -> str:
    op = str(msg.get("op") or "")
    if op == "exec.spawn":
        return " ".join([
            str(msg.get("command") or ""),
            *[str(a) for a in (msg.get("args") or [])],
        ]).strip()
    if msg.get("url"):
        return str(msg.get("url"))
    if msg.get("host"):
        return f"{msg.get('host')}:{msg.get('port') or ''}"
    return op


__all__ = [
    "commit_channel_rejection_reason",
    "evaluate_query",
    "is_commit_channel",
    "is_task_control_plane_action",
    "is_task_shell_transport_action",
    "record_commit",
    "task_commit_channel_rejection_reason",
    "task_debt_mcp_tool_names",
]
