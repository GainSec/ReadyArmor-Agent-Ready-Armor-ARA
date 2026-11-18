from __future__ import annotations

import json
from pathlib import Path

from ara import task_session
from ara import task_repair
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry as channel_registry


def _write_loadout(state: Path) -> None:
    (state / "armors" / "blacksmith" / "absolutes").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "absolutes" / "default.md").write_text("armor\n")
    (state / "weapons" / "w-task").mkdir(parents=True)
    (state / "weapons" / "w-task" / "seal.json").write_text("{}")
    (state / "armors" / "blacksmith" / "trims" / "trim-a").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "trims" / "trim-a" / "meta.json").write_text("{}")
    (state / "armors" / "blacksmith" / "temper" / "proposals").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "temper" / "proposals" / "temp-a.json").write_text("{}")


def _write_chunk(
    state: Path,
    chunk_id: str,
    exits: dict,
    *,
    is_entry: bool = False,
    can_emit_finding: bool = False,
    tool_surface: list[str] | None = None,
) -> None:
    path = state / "weapons" / "w-task" / "chunks" / f"{chunk_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": chunk_id,
        "title": chunk_id,
        "prompt_fragment": f"{chunk_id} prompt",
        "tool_surface": tool_surface or ["fact.assert", "lead.file", "chunks.emit_tag"],
        "exits": exits,
        "can_emit_finding": can_emit_finding,
        "is_entry": is_entry,
    }))


def _run_with_chunk_gated_branch_debt(state: Path) -> task_session.TaskRunSession:
    _write_loadout(state)
    _write_chunk(
        state,
        "enum",
        {
            "<self>": {"tag": "<self>", "next_chunk_id": "enum"},
            "<operator>": {"tag": "<operator>", "next_chunk_id": "operator_review"},
            "<probe>": {"tag": "<probe>", "next_chunk_id": "probe"},
        },
        is_entry=True,
    )
    _write_chunk(
        state,
        "probe",
        {"<write>": {"tag": "<write>", "next_chunk_id": "writeup"}},
    )
    _write_chunk(
        state,
        "writeup",
        {},
        can_emit_finding=True,
        tool_surface=["finding.file", "chunks.emit_tag"],
    )
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    assert run.record_action("net.connect").allowed is True
    run.record_commit("lead.file")
    assert run.record_action("net.connect").allowed is True
    return task_session.TaskRunSession.load(state, run.task_id)


def _run_with_debt(state: Path) -> task_session.TaskRunSession:
    _write_loadout(state)
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    assert run.record_action("net.connect").allowed is True
    return task_session.TaskRunSession.load(state, run.task_id)


def test_repair_commit_debt_writes_channel_lead_and_clears_phase(tmp_path: Path) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    run = _run_with_debt(state)
    channel_registry.unregister_for_tests()
    rec = EvidenceBinder(session).put(
        b"HTTP 200 from target",
        source="mitm:request",
        source_metadata={
            "url": "https://example.test/",
            "method": "GET",
            "status": 200,
        },
        summary="GET https://example.test/ -> 200",
        content_type="text/plain",
    )

    result = task_repair.repair_commit_debt(
        state,
        task_id=run.task_id,
        session_id="eng1",
        max_repairs=3,
    )

    assert result.ok is True
    assert result.repaired is True
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.actions_since_commit == 0
    assert loaded.accepted_commit_count == 1
    assert loaded.system_repair_count == 1
    events = [
        json.loads(line)
        for line in (session / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["kind"] == "channel.lead.file"
    assert events[-1]["body"]["status"] == "ok"
    assert events[-1]["body"]["channel"] == "lead.file"
    lead = json.loads((session / "leads.jsonl").read_text().splitlines()[-1])
    assert lead["origin"] == "system_repair"
    assert lead["evidence_refs"] == [rec.id]


def test_repair_commit_debt_respects_repair_limit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    run = _run_with_debt(state)
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    loaded.system_repair_count = 1
    loaded._persist()

    result = task_repair.repair_commit_debt(
        state,
        task_id=run.task_id,
        session_id="eng1",
        max_repairs=1,
    )

    assert result.ok is False
    assert result.reason == "REPAIR_LIMIT_EXCEEDED"
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED
    assert not (session / "events.jsonl").exists()


def test_repair_commit_debt_noops_when_phase_is_work(tmp_path: Path) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    _write_loadout(state)
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        task_id="task-test",
    )

    result = task_repair.repair_commit_debt(
        state,
        task_id=run.task_id,
        session_id="eng1",
        max_repairs=3,
    )

    assert result.ok is True
    assert result.repaired is False
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.system_repair_count == 0


def test_repair_chunk_transition_selects_finding_reachable_exit(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    run = _run_with_chunk_gated_branch_debt(state)
    channel_registry.unregister_for_tests()
    (session / "chunk_history.jsonl").write_text(json.dumps({
        "kind": "initialize",
        "to_chunk": "enum",
    }) + "\n")

    result = task_repair.repair_chunk_transition_debt(
        state,
        task_id=run.task_id,
        session_id="eng1",
        max_repairs=2,
    )

    assert result.ok is True
    assert result.repaired is True
    assert result.tag == "<probe>"
    assert result.from_chunk == "enum"
    assert result.to_chunk == "probe"
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.branch_updates_since_chunk_transition == 0
    assert loaded.system_chunk_transition_count == 1
    history = [
        json.loads(line)
        for line in (session / "chunk_history.jsonl").read_text().splitlines()
    ]
    assert history[-1]["kind"] == "swap"
    assert history[-1]["tag"] == "<probe>"
    assert history[-1]["caller"] == "agent"


def test_repair_chunk_transition_prefers_shortest_path_to_finding_chunk(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    _write_loadout(state)
    _write_chunk(
        state,
        "probe",
        {
            "<hypothesis_filed>": {
                "tag": "<hypothesis_filed>",
                "next_chunk_id": "probe",
            },
            "<investigation_done>": {
                "tag": "<investigation_done>",
                "next_chunk_id": "enum",
            },
            "<readied_for_writeup>": {
                "tag": "<readied_for_writeup>",
                "next_chunk_id": "intel",
            },
        },
    )
    _write_chunk(
        state,
        "enum",
        {
            "<surface_mapped>": {
                "tag": "<surface_mapped>",
                "next_chunk_id": "probe",
            },
        },
    )
    _write_chunk(
        state,
        "intel",
        {
            "<advance_to_writeup>": {
                "tag": "<advance_to_writeup>",
                "next_chunk_id": "writeup",
            },
            "<back_to_probe>": {
                "tag": "<back_to_probe>",
                "next_chunk_id": "probe",
            },
        },
    )
    _write_chunk(
        state,
        "writeup",
        {},
        can_emit_finding=True,
        tool_surface=["facts.query", "finding.file", "chunks.emit_tag"],
    )
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    run.record_action("net.connect")
    run.record_commit("lead.file")
    run.record_action("net.connect")
    (session / "chunk_history.jsonl").write_text(json.dumps({
        "kind": "initialize",
        "to_chunk": "probe",
    }) + "\n")

    result = task_repair.repair_chunk_transition_debt(
        state,
        task_id=run.task_id,
        session_id="eng1",
        max_repairs=2,
    )

    assert result.ok is True
    assert result.tag == "<readied_for_writeup>"
    assert result.from_chunk == "probe"
    assert result.to_chunk == "intel"


def test_repair_chunk_transition_returns_no_auto_exit_when_no_finding_reachable(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    _write_loadout(state)
    _write_chunk(
        state,
        "probe",
        {
            "<back_to_enum>": {"tag": "<back_to_enum>", "next_chunk_id": "enum"},
            "<operator>": {
                "tag": "<operator>",
                "next_chunk_id": "operator_review",
            },
        },
    )
    _write_chunk(state, "enum", {})
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    run.record_action("net.connect")
    run.record_commit("lead.file")
    run.record_action("net.connect")
    (session / "chunk_history.jsonl").write_text(json.dumps({
        "kind": "initialize",
        "to_chunk": "probe",
    }) + "\n")

    result = task_repair.repair_chunk_transition_debt(
        state,
        task_id=run.task_id,
        session_id="eng1",
        max_repairs=2,
    )

    assert result.ok is False
    assert result.reason == "NO_AUTO_EXIT_AVAILABLE"
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    assert loaded.phase == task_session.TaskPhase.BRANCH_REQUIRED
    assert loaded.system_chunk_transition_count == 0


def test_repair_chunk_transition_keeps_finding_capable_self_loop(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    _write_loadout(state)
    _write_chunk(
        state,
        "writeup",
        {
            "<finding_drafted>": {
                "tag": "<finding_drafted>",
                "next_chunk_id": "writeup",
            },
            "<back_to_probe>": {
                "tag": "<back_to_probe>",
                "next_chunk_id": "probe",
            },
        },
        can_emit_finding=True,
        tool_surface=["finding.file", "chunks.emit_tag"],
    )
    _write_chunk(
        state,
        "probe",
        {
            "<readied_for_writeup>": {
                "tag": "<readied_for_writeup>",
                "next_chunk_id": "writeup",
            },
        },
    )
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    run.record_action("net.connect")
    run.record_commit("finding.file")
    run.record_action("net.connect")
    (session / "chunk_history.jsonl").write_text(json.dumps({
        "kind": "initialize",
        "to_chunk": "writeup",
    }) + "\n")

    result = task_repair.repair_chunk_transition_debt(
        state,
        task_id=run.task_id,
        session_id="eng1",
        max_repairs=2,
    )

    assert result.ok is True
    assert result.tag == "<finding_drafted>"
    assert result.from_chunk == "writeup"
    assert result.to_chunk == "writeup"


def test_repair_chunk_transition_returns_no_auto_exit_when_only_operator_exits(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    _write_loadout(state)
    _write_chunk(
        state,
        "enum",
        {
            "<self>": {"tag": "<self>", "next_chunk_id": "enum"},
            "<operator>": {"tag": "<operator>", "next_chunk_id": "operator_review"},
        },
        is_entry=True,
    )
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    run.record_action("net.connect")
    run.record_commit("lead.file")
    run.record_action("net.connect")
    (session / "chunk_history.jsonl").write_text(json.dumps({
        "kind": "initialize",
        "to_chunk": "enum",
    }) + "\n")

    result = task_repair.repair_chunk_transition_debt(
        state,
        task_id=run.task_id,
        session_id="eng1",
        max_repairs=2,
    )

    assert result.ok is False
    assert result.reason == "NO_AUTO_EXIT_AVAILABLE"
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    assert loaded.phase == task_session.TaskPhase.BRANCH_REQUIRED
    assert loaded.system_chunk_transition_count == 0


def test_repair_chunk_transition_respects_repair_limit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    run = _run_with_chunk_gated_branch_debt(state)
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    loaded.system_chunk_transition_count = 1
    loaded._persist()
    (session / "chunk_history.jsonl").write_text(json.dumps({
        "kind": "initialize",
        "to_chunk": "enum",
    }) + "\n")

    result = task_repair.repair_chunk_transition_debt(
        state,
        task_id=run.task_id,
        session_id="eng1",
        max_repairs=1,
    )

    assert result.ok is False
    assert result.reason == "CHUNK_TRANSITION_REPAIR_LIMIT_EXCEEDED"
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    assert loaded.phase == task_session.TaskPhase.BRANCH_REQUIRED
