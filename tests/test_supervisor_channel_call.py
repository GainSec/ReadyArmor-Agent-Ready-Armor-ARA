"""Tests for the supervisor's channel_call IPC handler.

Drives ``Supervisor._on_channel_call`` and ``_maybe_install_engagement``
directly without spinning up a real socket — those are tested through
the live IPC server in the existing supervisor test suite.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ara import weapons as weapons_mod
from ara import task_session
from ara.runtime import audit as raudit
from ara.runtime.channels import registry as channel_registry


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    raudit.reset_registry_for_tests()
    channel_registry.unregister_for_tests()
    yield
    raudit.reset_registry_for_tests()
    channel_registry.unregister_for_tests()


def _seal_minimal_weapon(state_root: Path, weapon_id: str = "w-test") -> Path:
    """Lay down the minimum on-disk shape that ``install_engagement``
    expects: a sealed weapon with weapon.json + chunks (entry chunk
    with a tool_surface)."""
    from ara.forge.chunks.chunkdef import ChunkDef, write_chunkdef

    wdir = state_root / "weapons" / weapon_id
    (wdir / "chunks").mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps({
        "weapon_target_id": weapon_id, "crumbs": [], "shapes": [],
    }))
    (wdir / "seal.json").write_text(json.dumps({
        "weapon_id": weapon_id, "weapon_hash": "0" * 64,
    }))
    write_chunkdef(wdir / "chunks" / "enum.json", ChunkDef(
        id="enum", title="Enum", prompt_fragment="x",
        tool_surface=["fact.assert", "lead.file", "facts.query", "evidence.search"],
        is_entry=True, is_terminal=True,
    ))
    return wdir


def _write_task_loadout(state_root: Path, weapon_id: str = "w-test") -> None:
    (state_root / "armors" / "blacksmith" / "absolutes").mkdir(parents=True)
    (state_root / "armors" / "blacksmith" / "absolutes" / "default.md").write_text("armor\n")
    (state_root / "armors" / "blacksmith" / "trims" / "trim-a").mkdir(parents=True)
    (state_root / "armors" / "blacksmith" / "trims" / "trim-a" / "meta.json").write_text("{}")
    (state_root / "armors" / "blacksmith" / "temper" / "proposals").mkdir(parents=True)
    (state_root / "armors" / "blacksmith" / "temper" / "proposals" / "temp-a.json").write_text("{}")


def _make_supervisor(state_root: Path):
    """Construct a supervisor without starting the IPC server.

    We bypass ``Supervisor.start()`` (which binds a unix socket)
    because the channel_call handler is reachable as an unbound
    coroutine method.
    """
    from ara.audit import JsonlWriter
    from ara.session import create_session
    from ara.supervisor import Supervisor
    from ara.walls import Walls

    state_root.mkdir(parents=True, exist_ok=True)
    session = create_session(state_root=state_root)
    sup = Supervisor(
        session=session,
        walls=Walls(path_rules=()),
        server=None,  # type: ignore[arg-type]
        events=JsonlWriter(session.events_path),
        policy_hits=JsonlWriter(session.policy_hits_path, chain=True),
        state_root=state_root,
    )
    return sup


def test_channel_call_no_engagement_returns_error(tmp_path: Path) -> None:
    sup = _make_supervisor(tmp_path)
    # No weapon equipped → engagement is None
    sup._maybe_install_engagement()
    assert sup.engagement is None
    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call", "channel": "fact.assert", "body": {},
        "seq": 1,
    }))
    assert res["ok"] is False
    assert res["error_kind"] == "no_weapon_equipped"
    assert res["seq"] == 1


def test_channel_call_invalid_channel_name(tmp_path: Path) -> None:
    weapon_dir = _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    assert sup.engagement is not None

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call", "body": {},
    }))
    assert res["ok"] is False
    assert res["error_kind"] == "invalid_channel"


def test_channel_call_invalid_body_type(tmp_path: Path) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call", "channel": "fact.assert",
        "body": "not a dict",
    }))
    assert res["ok"] is False
    assert res["error_kind"] == "invalid_body"


def test_channel_call_unknown_channel(tmp_path: Path) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    # Drop fact.assert from the registry to force "unknown" path
    channel_registry.unregister_for_tests("never.exists")
    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call", "channel": "never.exists", "body": {},
    }))
    assert res["ok"] is False
    assert res["error_kind"] == "unknown_channel"


def test_channel_call_tool_surface_enforced(tmp_path: Path) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    # finding.file is NOT in the entry chunk's tool_surface
    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call", "channel": "finding.file", "body": {},
    }))
    assert res["ok"] is False
    assert res["error_kind"] == "tool_not_in_surface"


def test_channel_call_happy_path_fact_assert(tmp_path: Path) -> None:
    from ara.runtime.binder.api import EvidenceBinder

    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    assert sup.engagement is not None

    # Drop a piece of evidence so fact.assert has something to cite
    eng_dir = tmp_path / "sessions" / sup.session.id
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(
        b"x", source="tool:curl", source_metadata={}, summary="s",
        activation_id=sup.engagement.activation_manager.current(),
    )

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call", "channel": "fact.assert",
        "body": {
            "predicate": "target.host", "value": "example.com",
            "asserted_by_crumb": "c1", "evidence_refs": [rec.id],
        },
        "seq": 7,
    }))
    assert res["ok"] is True, res
    assert res["seq"] == 7
    assert res["channel"] == "fact.assert"
    assert res["result"]["fact"]["predicate"] == "target.host"


def test_channel_call_fact_assert_uses_recent_evidence_when_refs_missing(tmp_path: Path) -> None:
    from ara.runtime.binder.api import EvidenceBinder

    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    assert sup.engagement is not None

    eng_dir = tmp_path / "sessions" / sup.session.id
    rec = EvidenceBinder(eng_dir).put(
        b"HTTP 200 from https://example.test/",
        source="tool:python",
        source_metadata={},
        summary="target http response",
        activation_id=sup.engagement.activation_manager.current(),
    )

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call",
        "channel": "fact.assert",
        "body": {
            "predicate": "target.http.status",
            "value": 200,
            "asserted_by_crumb": "c1",
        },
        "seq": 8,
    }))

    assert res["ok"] is True, res
    assert res["result"]["fact"]["evidence_refs"] == [rec.id]


def test_channel_call_fact_assert_replaces_unknown_evidence_refs(tmp_path: Path) -> None:
    from ara.runtime.binder.api import EvidenceBinder

    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    assert sup.engagement is not None

    eng_dir = tmp_path / "sessions" / sup.session.id
    rec = EvidenceBinder(eng_dir).put(
        b"HTTP 200 from target",
        source="tool:python",
        source_metadata={},
        summary="target homepage returned HTTP 200",
        activation_id=sup.engagement.activation_manager.current(),
    )

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call",
        "channel": "fact.assert",
        "body": {
            "predicate": "http.status_code",
            "value": 200,
            "asserted_by_crumb": "cycle1_main_recon",
            "evidence_refs": ["evidence_from_cycle1"],
            "source": "agent_assertion",
        },
        "seq": 10,
    }))

    assert res["ok"] is True, res
    assert res["result"]["fact"]["evidence_refs"] == [rec.id]


def test_channel_call_lead_file_uses_recent_evidence_when_refs_missing(tmp_path: Path) -> None:
    from ara.runtime.binder.api import EvidenceBinder

    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    assert sup.engagement is not None

    eng_dir = tmp_path / "sessions" / sup.session.id
    rec = EvidenceBinder(eng_dir).put(
        b"Login form present",
        source="tool:python",
        source_metadata={},
        summary="login form evidence",
        activation_id=sup.engagement.activation_manager.current(),
    )

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call",
        "channel": "lead.file",
        "body": {
            "title": "Login form needs follow-up",
            "reasoning": "Observed a login form during enumeration.",
        },
        "seq": 9,
    }))

    assert res["ok"] is True, res
    assert res["result"]["lead"]["evidence_refs"] == [rec.id]


def test_channel_call_clears_task_action_debt(monkeypatch, tmp_path: Path) -> None:
    from ara.runtime.binder.api import EvidenceBinder

    _seal_minimal_weapon(tmp_path)
    _write_task_loadout(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-test",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})
    assert task_session.TaskRunSession.load(
        tmp_path, "task-test",
    ).phase == task_session.TaskPhase.COMMIT_REQUIRED
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")

    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    assert sup.engagement is not None
    eng_dir = tmp_path / "sessions" / sup.session.id
    binder = EvidenceBinder(eng_dir)
    rec = binder.put(
        b"x", source="tool:curl", source_metadata={}, summary="s",
        activation_id=sup.engagement.activation_manager.current(),
    )

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call", "channel": "fact.assert",
        "body": {
            "predicate": "target.host", "value": "example.com",
            "asserted_by_crumb": "c1", "evidence_refs": [rec.id],
        },
        "seq": 7,
    }))

    assert res["ok"] is True, res
    loaded = task_session.TaskRunSession.load(tmp_path, "task-test")
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.actions_since_commit == 0
    assert loaded.accepted_commit_count == 1


def test_channel_call_adapted_fact_clears_task_action_debt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from ara.runtime.binder.api import EvidenceBinder

    _seal_minimal_weapon(tmp_path)
    _write_task_loadout(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-test",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")

    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    assert sup.engagement is not None
    eng_dir = tmp_path / "sessions" / sup.session.id
    EvidenceBinder(eng_dir).put(
        b"HTTP 200 from https://example.test/",
        source="tool:python",
        source_metadata={},
        summary="target http response",
        activation_id=sup.engagement.activation_manager.current(),
    )

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call",
        "channel": "fact.assert",
        "body": {
            "predicate": "target.host",
            "value": "example.com",
            "asserted_by_crumb": "c1",
        },
        "seq": 10,
    }))

    assert res["ok"] is True, res
    loaded = task_session.TaskRunSession.load(tmp_path, "task-test")
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.actions_since_commit == 0
    assert loaded.accepted_commit_count == 1


def test_evidence_search_does_not_clear_task_action_debt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from ara.runtime.binder.api import EvidenceBinder

    _seal_minimal_weapon(tmp_path)
    _write_task_loadout(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-test",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")

    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    assert sup.engagement is not None
    eng_dir = tmp_path / "sessions" / sup.session.id
    EvidenceBinder(eng_dir).put(
        b"HTTP 200 from target",
        source="tool:curl",
        source_metadata={"url": "https://example.test/"},
        summary="target http response",
        activation_id=sup.engagement.activation_manager.current(),
    )

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call",
        "channel": "evidence.search",
        "body": {"query": "target"},
        "seq": 8,
    }))

    assert res["ok"] is True, res
    loaded = task_session.TaskRunSession.load(tmp_path, "task-test")
    assert loaded.phase == task_session.TaskPhase.COMMIT_REQUIRED
    assert loaded.actions_since_commit == 1
    assert loaded.accepted_commit_count == 0


def test_native_channel_evidence_search_accepts_dotted_agent_query(tmp_path: Path) -> None:
    from ara.runtime.binder.api import EvidenceBinder

    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    assert sup.engagement is not None
    eng_dir = tmp_path / "sessions" / sup.session.id
    record = EvidenceBinder(eng_dir).put(
        b"TLSv1.3 target.tls_version is negotiated by the target",
        source="tool:curl",
        source_metadata={"url": "https://example.test/"},
        summary="tls version",
        activation_id=sup.engagement.activation_manager.current(),
    )

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call",
        "channel": "evidence.search",
        "body": {"query": "TLSv1.3 target.tls_version", "k": 5},
        "seq": 9,
    }))

    assert res["ok"] is True, res
    ids = [
        row["record"]["id"]
        for row in res["result"].get("results", [])
        if isinstance(row.get("record"), dict)
    ]
    assert record.id in ids


def test_supervisor_commit_debt_filter_excludes_evidence_lookup_tools(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _seal_minimal_weapon(tmp_path)
    _write_task_loadout(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-test",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    sup = _make_supervisor(tmp_path)
    payload = {
        "tools": [
            {"function": {"name": "terminal"}},
            {"function": {"name": "mcp_ara_forge_evidence_search"}},
            {"function": {"name": "mcp_ara_forge_lead_file"}},
            {"function": {"name": "mcp_ara_forge_chunks_emit_tag"}},
            {"function": {"name": "mcp_ara_forge_evidence_get"}},
        ],
    }

    out = sup._maybe_filter_task_channel_debt_tools(payload)

    assert out is not None
    assert out["tool_choice"] == "required"
    assert [
        tool["function"]["name"]
        for tool in out["tools"]
    ] == [
        "mcp_ara_forge_lead_file",
        "mcp_ara_forge_chunks_emit_tag",
    ]


def test_supervisor_chunk_gated_branch_debt_filter_keeps_only_chunk_transition(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _seal_minimal_weapon(tmp_path)
    _write_task_loadout(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-test",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})
    run.record_commit("lead.file")
    run.record_action("exec.spawn", {"command": "python3"})
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    sup = _make_supervisor(tmp_path)
    payload = {
        "tools": [
            {"function": {"name": "terminal"}},
            {"function": {"name": "mcp_ara_forge_fact_assert"}},
            {"function": {"name": "mcp_ara_forge_lead_file"}},
            {"function": {"name": "mcp_ara_forge_finding_file"}},
            {"function": {"name": "mcp_ara_forge_chunks_emit_tag"}},
        ],
    }

    out = sup._maybe_filter_task_channel_debt_tools(payload)

    assert out is not None
    assert out["tool_choice"] == {
        "type": "function",
        "function": {"name": "mcp_ara_forge_chunks_emit_tag"},
    }
    assert [
        tool["function"]["name"]
        for tool in out["tools"]
    ] == ["mcp_ara_forge_chunks_emit_tag"]


def test_channel_call_rejects_chunk_gated_lead_before_audit_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _seal_minimal_weapon(tmp_path)
    _write_task_loadout(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-test",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        branch_action_budget=1,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})
    run.record_commit("lead.file")
    run.record_action("exec.spawn", {"command": "python3"})
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call",
        "channel": "lead.file",
        "body": {
            "title": "More enum",
            "reasoning": "Keep enumerating instead of transitioning.",
        },
        "seq": 77,
    }))

    assert res["ok"] is False
    assert res["error_kind"] == "task_channel_rejection"
    assert res["error"] == "CHUNK_TRANSITION_REQUIRED"
    events_path = tmp_path / "sessions" / sup.session.id / "events.jsonl"
    if events_path.exists():
        assert "channel.lead.file" not in events_path.read_text()


def test_channel_call_accepts_chunk_gated_lead_while_working(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from ara.runtime.binder.api import EvidenceBinder

    _seal_minimal_weapon(tmp_path)
    _write_task_loadout(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-test",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=3,
        branch_action_budget=3,
        branch_chunk_gate_threshold=1,
        task_id="task-test",
    )
    run.record_action("exec.spawn", {"command": "python3"})
    run.record_commit("lead.file")
    assert task_session.TaskRunSession.load(
        tmp_path, "task-test",
    ).phase == task_session.TaskPhase.WORK
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    EvidenceBinder(tmp_path / "sessions" / sup.session.id).put(
        b"Route inventory still incomplete",
        source="tool:python",
        source_metadata={},
        summary="route inventory lead evidence",
        activation_id=sup.engagement.activation_manager.current(),
    )

    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call",
        "channel": "lead.file",
        "body": {
            "title": "More enum",
            "reasoning": "Store a useful lead while still in work phase.",
        },
        "seq": 78,
    }))

    assert res["ok"] is True, res
    loaded = task_session.TaskRunSession.load(tmp_path, "task-test")
    assert loaded.accepted_branch_update_count == 1
    assert loaded.branch_updates_since_chunk_transition == 1


def test_channel_call_rejection_surfaces(tmp_path: Path) -> None:
    """Channel-side validation failure (no evidence_ref + no fact_ref)
    becomes an IPC error response, not a supervisor crash."""
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    res = asyncio.run(sup._on_channel_call({
        "type": "channel_call", "channel": "fact.assert",
        "body": {
            "predicate": "p", "value": 1,
            "asserted_by_crumb": "c",
            # neither evidence_refs nor depends_on → ChannelRejection
        },
    }))
    assert res["ok"] is False
    assert res["error_kind"] == "channel_rejection"
    assert "empty" in res["error"]


def test_dispatch_routes_channel_call(tmp_path: Path) -> None:
    """The top-level _dispatch correctly forwards channel_call to the
    handler (regression guard for the dispatch wiring)."""
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-test")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    res = asyncio.run(sup._dispatch({
        "type": "channel_call", "channel": "facts.query",
        "body": {"predicate": "x"},
    }))
    assert res is not None
    assert res["type"] == "channel_call_response"
    # facts.query is in the entry chunk's tool_surface; should succeed
    assert res["ok"] is True
    assert res["result"]["facts"] == []
