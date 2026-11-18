from __future__ import annotations

import json
from pathlib import Path

import pytest

from ara import task_session


def _write_loadout(state: Path) -> None:
    (state / "armors" / "blacksmith" / "absolutes").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "absolutes" / "default.md").write_text("armor\n")
    (state / "weapons" / "w-task" / "chunks").mkdir(parents=True)
    (state / "weapons" / "w-task" / "weapon.json").write_text("{}")
    (state / "weapons" / "w-task" / "seal.json").write_text("{}")
    (state / "armors" / "blacksmith" / "trims" / "trim-a").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "trims" / "trim-a" / "meta.json").write_text("{}")
    (state / "armors" / "blacksmith" / "temper" / "proposals").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "temper" / "proposals" / "temp-a.json").write_text("{}")


def test_task_run_session_persists_loadout_snapshot(tmp_path: Path) -> None:
    _write_loadout(tmp_path)

    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        window_end="2026-06-22T02:00:00Z",
        action_budget=2,
        task_id="task-test",
    )

    loaded = task_session.TaskRunSession.load(tmp_path, "task-test")
    assert loaded.task_id == "task-test"
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.armor_id == "blacksmith"
    assert loaded.weapon_id == "w-task"
    assert loaded.trim_id == "trim-a"
    assert loaded.temper_id == "temp-a"
    assert loaded.action_budget == 2
    assert loaded.loadout_hashes["weapon"]
    assert (tmp_path / "task-runs" / "task-test" / "session.json").is_file()


def test_task_run_action_debt_blocks_until_commit(tmp_path: Path) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )

    first = run.record_action("exec.spawn", {"command": "python3"})
    assert first.allowed is True
    assert run.phase == task_session.TaskPhase.COMMIT_REQUIRED

    second = run.record_action("exec.spawn", {"command": "python3"})
    assert second.allowed is False
    assert second.reason == "TASK_COMMIT_REQUIRED"

    run.record_commit("fact.assert")
    assert run.phase == task_session.TaskPhase.WORK
    assert run.actions_since_commit == 0
    assert run.accepted_commit_count == 1


def test_task_run_branch_debt_requires_branch_update_channel(tmp_path: Path) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=2,
        task_id="task-test",
    )

    assert run.record_action("exec.spawn", {"command": "python3"}).allowed is True
    run.record_commit("fact.assert")
    assert run.phase == task_session.TaskPhase.WORK
    assert run.actions_since_branch_update == 1

    assert run.record_action("exec.spawn", {"command": "python3"}).allowed is True
    assert run.phase == task_session.TaskPhase.BRANCH_REQUIRED

    denied = run.record_action("exec.spawn", {"command": "python3"})
    assert denied.allowed is False
    assert denied.reason == "TASK_BRANCH_REQUIRED"

    run.record_commit("fact.assert")
    assert run.phase == task_session.TaskPhase.BRANCH_REQUIRED
    assert run.actions_since_commit == 0
    assert run.actions_since_branch_update == 2

    run.record_commit("lead.file")
    assert run.phase == task_session.TaskPhase.WORK
    assert run.actions_since_branch_update == 0
    assert run.accepted_branch_update_count == 1


def test_lead_file_increments_branch_updates_since_chunk_transition(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        task_id="task-test",
    )

    run.record_commit("lead.file")
    assert run.branch_updates_since_chunk_transition == 1

    run.record_commit("finding.file")
    assert run.branch_updates_since_chunk_transition == 2


def test_chunks_emit_tag_resets_branch_updates_since_chunk_transition_to_zero(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        task_id="task-test",
    )
    run.record_commit("lead.file")
    run.record_commit("finding.file")

    run.record_commit("chunks.emit_tag")

    assert run.branch_updates_since_chunk_transition == 0
    assert run.chunk_gated is False


def test_chunk_gated_lead_file_does_not_clear_branch_debt(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    assert run.record_action("exec.spawn", {"command": "python3"}).allowed is True
    run.record_commit("lead.file")
    assert run.phase == task_session.TaskPhase.WORK
    assert run.chunk_gated is True

    assert run.record_action("exec.spawn", {"command": "python3"}).allowed is True
    assert run.phase == task_session.TaskPhase.BRANCH_REQUIRED
    run.record_commit("lead.file")

    assert run.phase == task_session.TaskPhase.BRANCH_REQUIRED
    assert run.actions_since_branch_update == 1
    assert run.accepted_branch_update_count == 1
    assert run.branch_updates_since_chunk_transition == 1


def test_chunk_gated_chunks_emit_tag_clears_branch_debt(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})
    run.record_commit("lead.file")
    run.record_action("exec.spawn", {"command": "python3"})

    run.record_commit("chunks.emit_tag")

    assert run.phase == task_session.TaskPhase.WORK
    assert run.actions_since_branch_update == 0
    assert run.branch_updates_since_chunk_transition == 0
    assert run.chunk_gated is False


def test_system_repair_does_not_increment_branch_updates_since_chunk_transition(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})

    run.record_system_repair("lead.file")

    assert run.branch_updates_since_chunk_transition == 0
    assert run.system_repair_count == 1


def test_system_repair_rejects_non_commit_channel_without_mutating(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})

    with pytest.raises(ValueError, match="non-commit repair channel"):
        run.record_system_repair("evidence.search")

    loaded = task_session.TaskRunSession.load(tmp_path, "task-test")
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED
    assert loaded.actions_since_commit == 1
    assert loaded.accepted_commit_count == 0
    assert loaded.system_repair_count == 0


def test_task_run_detects_loadout_hash_mismatch(tmp_path: Path) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        task_id="task-test",
    )

    assert run.current_loadout_matches() is True
    (tmp_path / "weapons" / "w-task" / "seal.json").write_text('{"changed": true}')

    assert run.current_loadout_matches() is False
    loaded = task_session.TaskRunSession.load(tmp_path, "task-test")
    assert loaded.blocked_reason == "LOADOUT_HASH_MISMATCH"
    assert loaded.phase == task_session.TaskPhase.BLOCKED


def test_task_run_loads_legacy_session_without_branch_fields(
    tmp_path: Path,
) -> None:
    _write_loadout(tmp_path)
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    path = task_session.task_session_path(tmp_path, run.task_id)
    data = json.loads(path.read_text())
    for key in [
        "branch_action_budget",
        "actions_since_branch_update",
        "accepted_branch_update_count",
        "branch_updates_since_chunk_transition",
        "branch_chunk_gate_threshold",
    ]:
        data.pop(key)
    path.write_text(json.dumps(data))

    loaded = task_session.TaskRunSession.load(tmp_path, run.task_id)
    assert loaded.branch_action_budget == 3
    assert loaded.actions_since_branch_update == 0
    assert loaded.accepted_branch_update_count == 0
    assert loaded.branch_updates_since_chunk_transition == 0
    assert loaded.branch_chunk_gate_threshold == 5
