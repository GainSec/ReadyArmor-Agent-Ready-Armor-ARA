"""Persisted Run Task controller state.

The normal Armory Run Task path uses this state machine to make task
progress mechanical: target actions spend a small budget, and accepted ARA
channel commits clear that debt.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from uuid import uuid4

from . import debug_log


COMMIT_CHANNELS = frozenset({
    "fact.assert",
    "lead.file",
    "finding.file",
    "chunks.emit_tag",
})

BRANCH_UPDATE_CHANNELS = frozenset({
    "lead.file",
    "finding.file",
    "chunks.emit_tag",
})


class TaskPhase(str, Enum):
    PREPARE = "prepare"
    LAUNCH = "launch"
    WORK = "work"
    COMMIT_REQUIRED = "commit_required"
    BRANCH_REQUIRED = "branch_required"
    VERIFY_REQUIRED = "verify_required"
    COMPLETE = "complete"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ActionDecision:
    allowed: bool
    reason: str | None = None


@dataclass
class TaskRunSession:
    task_id: str
    state_root: Path
    phase: TaskPhase
    armor_id: str
    weapon_id: str
    trim_id: str
    temper_id: str
    loadout_hashes: dict[str, str]
    created_at: float
    updated_at: float
    window_end: str | None = None
    current_chunk: str | None = None
    action_budget: int = 1
    branch_action_budget: int = 3
    branch_chunk_gate_threshold: int = 5
    actions_since_commit: int = 0
    actions_since_branch_update: int = 0
    branch_updates_since_chunk_transition: int = 0
    accepted_commit_count: int = 0
    accepted_branch_update_count: int = 0
    system_repair_count: int = 0
    system_chunk_transition_count: int = 0
    # Verification checkpoint: after ``verify_budget`` findings are filed since
    # the last verification pass, the task trips VERIFY_REQUIRED — the same
    # forcing-function pattern as commit/branch budgets, but for verifying that
    # accumulated findings hold up (re-checked mechanically, drafts downgraded).
    verify_budget: int = 3
    findings_since_verify: int = 0
    blocked_reason: str | None = None

    @classmethod
    def new(
        cls,
        state_root: Path,
        *,
        armor_id: str,
        weapon_id: str,
        trim_id: str,
        temper_id: str,
        window_end: str | None = None,
        current_chunk: str | None = None,
        action_budget: int = 1,
        branch_action_budget: int = 3,
        branch_chunk_gate_threshold: int = 5,
        verify_budget: int = 3,
        task_id: str | None = None,
    ) -> "TaskRunSession":
        now = time.time()
        run = cls(
            task_id=task_id or f"task-{uuid4().hex[:12]}",
            state_root=Path(state_root),
            phase=TaskPhase.WORK,
            armor_id=armor_id,
            weapon_id=weapon_id,
            trim_id=trim_id,
            temper_id=temper_id,
            loadout_hashes=_loadout_hashes(
                Path(state_root), armor_id, weapon_id, trim_id, temper_id,
            ),
            created_at=now,
            updated_at=now,
            window_end=window_end,
            current_chunk=current_chunk,
            action_budget=max(1, int(action_budget or 1)),
            branch_action_budget=max(1, int(branch_action_budget or 1)),
            branch_chunk_gate_threshold=max(1, int(branch_chunk_gate_threshold or 5)),
            verify_budget=max(1, int(verify_budget or 3)),
        )
        run._persist()
        run._audit("task.created", {
            "armor_id": armor_id,
            "weapon_id": weapon_id,
            "trim_id": trim_id,
            "temper_id": temper_id,
            "window_end": window_end,
            "action_budget": run.action_budget,
            "branch_action_budget": run.branch_action_budget,
            "branch_chunk_gate_threshold": run.branch_chunk_gate_threshold,
        })
        debug_log.emit(
            "task-session", "task.created",
            state_root=state_root,
            task_id=run.task_id,
            armor_id=armor_id,
            weapon_id=weapon_id,
            trim_id=trim_id,
            temper_id=temper_id,
            window_end=window_end,
            action_budget=run.action_budget,
            branch_action_budget=run.branch_action_budget,
            branch_chunk_gate_threshold=run.branch_chunk_gate_threshold,
            loadout_hashes=run.loadout_hashes,
        )
        return run

    @classmethod
    def load(cls, state_root: Path, task_id: str) -> "TaskRunSession":
        path = task_session_path(Path(state_root), task_id)
        data = json.loads(path.read_text())
        data["state_root"] = Path(data["state_root"])
        data["phase"] = TaskPhase(data["phase"])
        data.setdefault("branch_action_budget", 3)
        data.setdefault("actions_since_branch_update", 0)
        data.setdefault("accepted_branch_update_count", 0)
        data.setdefault("branch_chunk_gate_threshold", 5)
        data.setdefault("branch_updates_since_chunk_transition", 0)
        data.setdefault("system_repair_count", 0)
        data.setdefault("system_chunk_transition_count", 0)
        data.setdefault("verify_budget", 3)
        data.setdefault("findings_since_verify", 0)
        return cls(**data)

    @property
    def chunk_gated(self) -> bool:
        return (
            self.branch_updates_since_chunk_transition
            >= max(1, self.branch_chunk_gate_threshold)
        )

    def record_action(self, op: str, detail: dict | None = None) -> ActionDecision:
        if self.phase == TaskPhase.BLOCKED:
            debug_log.emit(
                "task-session", "action.denied",
                state_root=self.state_root,
                task_id=self.task_id,
                op=op,
                reason=self.blocked_reason or "TASK_BLOCKED",
                phase=self.phase.value,
                detail=detail or {},
            )
            return ActionDecision(False, self.blocked_reason or "TASK_BLOCKED")
        if not self.current_loadout_matches():
            debug_log.emit(
                "task-session", "action.denied",
                state_root=self.state_root,
                task_id=self.task_id,
                op=op,
                reason=self.blocked_reason or "LOADOUT_HASH_MISMATCH",
                phase=self.phase.value,
                detail=detail or {},
            )
            return ActionDecision(False, self.blocked_reason or "LOADOUT_HASH_MISMATCH")
        if self.phase == TaskPhase.COMMIT_REQUIRED:
            self._audit("task.action.denied", {
                "op": op,
                "reason": "TASK_COMMIT_REQUIRED",
                "detail": detail or {},
            })
            debug_log.emit(
                "task-session", "action.denied",
                state_root=self.state_root,
                task_id=self.task_id,
                op=op,
                reason="TASK_COMMIT_REQUIRED",
                phase=self.phase.value,
                detail=detail or {},
            )
            return ActionDecision(False, "TASK_COMMIT_REQUIRED")
        if self.phase == TaskPhase.BRANCH_REQUIRED:
            self._audit("task.action.denied", {
                "op": op,
                "reason": "TASK_BRANCH_REQUIRED",
                "detail": detail or {},
            })
            debug_log.emit(
                "task-session", "action.denied",
                state_root=self.state_root,
                task_id=self.task_id,
                op=op,
                reason="TASK_BRANCH_REQUIRED",
                phase=self.phase.value,
                detail=detail or {},
            )
            return ActionDecision(False, "TASK_BRANCH_REQUIRED")
        if self.phase == TaskPhase.VERIFY_REQUIRED:
            self._audit("task.action.denied", {
                "op": op,
                "reason": "TASK_VERIFY_REQUIRED",
                "detail": detail or {},
            })
            debug_log.emit(
                "task-session", "action.denied",
                state_root=self.state_root,
                task_id=self.task_id,
                op=op,
                reason="TASK_VERIFY_REQUIRED",
                phase=self.phase.value,
                detail=detail or {},
            )
            return ActionDecision(False, "TASK_VERIFY_REQUIRED")
        self.actions_since_commit += 1
        self.actions_since_branch_update += 1
        if self.actions_since_branch_update >= self.branch_action_budget:
            self.phase = TaskPhase.BRANCH_REQUIRED
        elif self.actions_since_commit >= self.action_budget:
            self.phase = TaskPhase.COMMIT_REQUIRED
        self.updated_at = time.time()
        self._persist()
        self._audit("task.action.allowed", {
            "op": op,
            "actions_since_commit": self.actions_since_commit,
            "actions_since_branch_update": self.actions_since_branch_update,
            "phase": self.phase.value,
            "detail": detail or {},
        })
        debug_log.emit(
            "task-session", "action.allowed",
            state_root=self.state_root,
            task_id=self.task_id,
            op=op,
            actions_since_commit=self.actions_since_commit,
            actions_since_branch_update=self.actions_since_branch_update,
            phase=self.phase.value,
            action_budget=self.action_budget,
            branch_action_budget=self.branch_action_budget,
            detail=detail or {},
        )
        return ActionDecision(True)

    def record_commit(self, channel: str, *, _is_system_repair: bool = False) -> None:
        was_chunk_gated = self.chunk_gated
        self.accepted_commit_count += 1
        self.actions_since_commit = 0
        if channel == "finding.file":
            self.findings_since_verify += 1
        is_branch_update = channel in BRANCH_UPDATE_CHANNELS
        clears_branch_debt = False
        if is_branch_update:
            if channel == "chunks.emit_tag":
                self.branch_updates_since_chunk_transition = 0
                clears_branch_debt = True
            elif not was_chunk_gated:
                if not _is_system_repair:
                    self.branch_updates_since_chunk_transition += 1
                clears_branch_debt = True
            if clears_branch_debt:
                self.accepted_branch_update_count += 1
                self.actions_since_branch_update = 0
        if self.phase != TaskPhase.BLOCKED:
            if self.actions_since_branch_update >= self.branch_action_budget:
                self.phase = TaskPhase.BRANCH_REQUIRED
            elif self.findings_since_verify >= self.verify_budget:
                self.phase = TaskPhase.VERIFY_REQUIRED
            else:
                self.phase = TaskPhase.WORK
            self.blocked_reason = None
        self.updated_at = time.time()
        self._persist()
        self._audit("task.commit.accepted", {
            "channel": channel,
            "accepted_commit_count": self.accepted_commit_count,
            "accepted_branch_update_count": self.accepted_branch_update_count,
            "branch_update": is_branch_update,
            "branch_cleared": clears_branch_debt,
            "branch_updates_since_chunk_transition": (
                self.branch_updates_since_chunk_transition
            ),
            "chunk_gated": self.chunk_gated,
        })
        debug_log.emit(
            "task-session", "commit.accepted",
            state_root=self.state_root,
            task_id=self.task_id,
            channel=channel,
            accepted_commit_count=self.accepted_commit_count,
            accepted_branch_update_count=self.accepted_branch_update_count,
            branch_update=is_branch_update,
            branch_cleared=clears_branch_debt,
            branch_updates_since_chunk_transition=(
                self.branch_updates_since_chunk_transition
            ),
            chunk_gated=self.chunk_gated,
            phase=self.phase.value,
        )

    def record_verify_pass(self, *, downgraded: int = 0, kept: int = 0) -> None:
        """Clear verification debt after a verification checkpoint.

        Called by the system verification pass once it has re-checked the
        accumulated draft findings and downgraded any that no longer hold.
        Resets the findings-since-verify counter and, if the task was gated on
        VERIFY_REQUIRED, returns it to WORK.
        """
        self.findings_since_verify = 0
        if self.phase == TaskPhase.VERIFY_REQUIRED:
            self.phase = TaskPhase.WORK
            self.blocked_reason = None
        self.updated_at = time.time()
        self._persist()
        self._audit("task.verify.pass", {
            "downgraded": int(downgraded),
            "kept": int(kept),
            "phase": self.phase.value,
        })
        debug_log.emit(
            "task-session", "verify.pass",
            state_root=self.state_root,
            task_id=self.task_id,
            downgraded=int(downgraded),
            kept=int(kept),
            phase=self.phase.value,
        )

    def record_system_repair(self, channel: str) -> None:
        if channel not in COMMIT_CHANNELS:
            raise ValueError(f"non-commit repair channel: {channel}")
        self.system_repair_count += 1
        self._audit("task.system_repair.accepted", {
            "channel": channel,
            "system_repair_count": self.system_repair_count,
        })
        debug_log.emit(
            "task-session", "system_repair.accepted",
            state_root=self.state_root,
            task_id=self.task_id,
            channel=channel,
            system_repair_count=self.system_repair_count,
        )
        self.record_commit(channel, _is_system_repair=True)

    def record_system_chunk_transition(
        self,
        *,
        tag: str,
        from_chunk: str | None,
        to_chunk: str | None,
    ) -> None:
        self.system_chunk_transition_count += 1
        self._audit("task.system_chunk_transition.accepted", {
            "channel": "chunks.emit_tag",
            "tag": tag,
            "from_chunk": from_chunk,
            "to_chunk": to_chunk,
            "system_chunk_transition_count": self.system_chunk_transition_count,
        })
        debug_log.emit(
            "task-session", "system_chunk_transition.accepted",
            state_root=self.state_root,
            task_id=self.task_id,
            channel="chunks.emit_tag",
            tag=tag,
            from_chunk=from_chunk,
            to_chunk=to_chunk,
            system_chunk_transition_count=self.system_chunk_transition_count,
        )
        self.record_commit("chunks.emit_tag", _is_system_repair=True)

    def block(self, reason: str) -> None:
        self.phase = TaskPhase.BLOCKED
        self.blocked_reason = reason
        self.updated_at = time.time()
        self._persist()
        self._audit("task.blocked", {"reason": reason})
        debug_log.emit(
            "task-session", "task.blocked",
            state_root=self.state_root,
            task_id=self.task_id,
            reason=reason,
            phase=self.phase.value,
        )

    def current_loadout_matches(self) -> bool:
        current = _loadout_hashes(
            self.state_root,
            self.armor_id,
            self.weapon_id,
            self.trim_id,
            self.temper_id,
        )
        if current == self.loadout_hashes:
            return True
        self.block("LOADOUT_HASH_MISMATCH")
        return False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["state_root"] = str(self.state_root)
        data["phase"] = self.phase.value
        return data

    def _persist(self) -> None:
        path = task_session_path(self.state_root, self.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        os.replace(tmp, path)

    def _audit(self, kind: str, body: dict) -> None:
        path = task_audit_path(self.state_root, self.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "kind": kind,
                "body": body,
            }, sort_keys=True) + "\n")


def task_runs_root(state_root: Path) -> Path:
    return Path(state_root) / "task-runs"


def task_run_dir(state_root: Path, task_id: str) -> Path:
    return task_runs_root(state_root) / task_id


def task_session_path(state_root: Path, task_id: str) -> Path:
    return task_run_dir(state_root, task_id) / "session.json"


def task_audit_path(state_root: Path, task_id: str) -> Path:
    return task_run_dir(state_root, task_id) / "events.jsonl"


def _loadout_hashes(
    state_root: Path,
    armor_id: str,
    weapon_id: str,
    trim_id: str,
    temper_id: str,
) -> dict[str, str]:
    return {
        "armor": _hash_path(Path(state_root) / "armors" / armor_id),
        "weapon": _hash_path(Path(state_root) / "weapons" / weapon_id),
        "trim": _hash_path(
            Path(state_root) / "armors" / armor_id / "trims" / trim_id,
        ),
        "temper": _hash_path(
            Path(state_root) / "armors" / armor_id / "temper" / "proposals"
            / f"{temper_id}.json",
        ),
    }


def _hash_path(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.name.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        return "sha256:" + h.hexdigest()
    if path.is_dir():
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            rel = child.relative_to(path).as_posix()
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(child.read_bytes())
            h.update(b"\0")
        return "sha256:" + h.hexdigest()
    h.update(b"<missing>")
    h.update(str(path).encode("utf-8"))
    return "sha256:" + h.hexdigest()


__all__ = [
    "ActionDecision",
    "BRANCH_UPDATE_CHANNELS",
    "TaskPhase",
    "TaskRunSession",
    "task_audit_path",
    "task_run_dir",
    "task_runs_root",
    "task_session_path",
]
