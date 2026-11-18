from __future__ import annotations

import json
from pathlib import Path

from ara import task_session
from ara.mitm_proxy import MitmProxy, _dispatch_pseudo_task_channel_calls
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.channels import registry as channel_registry


def _write_task_loadout(state: Path) -> None:
    (state / "armors" / "blacksmith" / "absolutes").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "absolutes" / "default.md").write_text("armor\n")
    (state / "weapons" / "w-task").mkdir(parents=True)
    (state / "weapons" / "w-task" / "seal.json").write_text("{}")
    (state / "armors" / "blacksmith" / "trims" / "trim-a").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "trims" / "trim-a" / "meta.json").write_text("{}")
    (state / "armors" / "blacksmith" / "temper" / "proposals").mkdir(parents=True)
    (state / "armors" / "blacksmith" / "temper" / "proposals" / "temp-a.json").write_text("{}")


def test_dispatch_pseudo_task_fact_assert_writes_channel_event(tmp_path: Path) -> None:
    state = tmp_path / "state"
    session = tmp_path / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
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
    payload = {
        "chunks": [{
            "choices": [{
                "delta": {
                    "reasoning_content": (
                        "<tool_call>\n"
                        "<function=terminal>\n"
                        "<parameter=command>whoami</parameter>\n"
                        "</function>\n"
                        "</tool_call>\n"
                        "<tool_call>\n"
                        "<function=mcp_ara_forge_fact_assert>\n"
                        "<parameter=predicate>http_status_code</parameter>\n"
                        "<parameter=value>200</parameter>\n"
                        "<parameter=evidence_refs>[\""
                        + rec.id
                        + "\"]</parameter>\n"
                        "<parameter=confidence>1.0</parameter>\n"
                        "</function>\n"
                        "</tool_call>\n"
                    ),
                },
            }],
        }],
    }

    count = _dispatch_pseudo_task_channel_calls(state, session, payload)

    assert count == 1
    rows = [
        json.loads(line)
        for line in (session / "events.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["kind"] == "channel.fact.assert"
    assert rows[-1]["body"]["status"] == "ok"
    assert rows[-1]["body"]["channel"] == "fact.assert"


def test_dispatch_pseudo_task_fact_assert_clears_task_debt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    _write_task_loadout(state)
    run = task_session.TaskRunSession.new(
        state,
        armor_id="blacksmith",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    monkeypatch.setenv("ARA_TASK_RUN_ID", run.task_id)
    assert run.record_action("net.connect").allowed is True
    assert task_session.TaskRunSession.load(
        state, run.task_id,
    ).phase == task_session.TaskPhase.COMMIT_REQUIRED

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
    payload = {
        "chunks": [{
            "choices": [{
                "delta": {
                    "reasoning_content": (
                        "<tool_call>\n"
                        "<function=mcp_ara_forge_fact_assert>\n"
                        "<parameter=predicate>http_status_code</parameter>\n"
                        "<parameter=value>200</parameter>\n"
                        "<parameter=evidence_refs>[\""
                        + rec.id
                        + "\"]</parameter>\n"
                        "</function>\n"
                        "</tool_call>\n"
                    ),
                },
            }],
        }],
    }

    count = _dispatch_pseudo_task_channel_calls(state, session, payload)

    assert count == 1
    loaded = task_session.TaskRunSession.load(state, run.task_id)
    assert loaded.phase == task_session.TaskPhase.WORK
    assert loaded.actions_since_commit == 0
    assert loaded.accepted_commit_count == 1


def test_dispatch_pseudo_task_fact_assert_attaches_latest_evidence(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = tmp_path / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    channel_registry.unregister_for_tests()
    EvidenceBinder(session).put(
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
    payload = {
        "chunks": [{
            "choices": [{
                "delta": {
                    "reasoning_content": (
                        "<tool_call>\n"
                        "<function=mcp_ara_forge_fact_assert>\n"
                        "<parameter=predicate>http_status_code</parameter>\n"
                        "<parameter=value>200</parameter>\n"
                        "</function>\n"
                        "</tool_call>\n"
                    ),
                },
            }],
        }],
    }

    count = _dispatch_pseudo_task_channel_calls(state, session, payload)

    assert count == 1
    rows = [
        json.loads(line)
        for line in (session / "events.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["kind"] == "channel.fact.assert"
    assert rows[-1]["body"]["status"] == "ok"


def test_dispatch_pseudo_task_fact_assert_replaces_unknown_evidence_ref(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = tmp_path / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    channel_registry.unregister_for_tests()
    EvidenceBinder(session).put(
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
    payload = {
        "chunks": [{
            "choices": [{
                "delta": {
                    "reasoning_content": (
                        "<tool_call>\n"
                        "<function=mcp_ara_forge_fact_assert>\n"
                        "<parameter=predicate>http.status_code</parameter>\n"
                        "<parameter=value>200</parameter>\n"
                        "<parameter=evidence_refs>"
                        "[\"evidence_from_cycle1\"]</parameter>\n"
                        "</function>\n"
                        "</tool_call>\n"
                    ),
                },
            }],
        }],
    }

    count = _dispatch_pseudo_task_channel_calls(state, session, payload)

    assert count == 1
    rows = [
        json.loads(line)
        for line in (session / "events.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["kind"] == "channel.fact.assert"
    assert rows[-1]["body"]["status"] == "ok"


def test_dispatch_pseudo_task_lead_file_synthesizes_missing_title(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = tmp_path / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    channel_registry.unregister_for_tests()
    EvidenceBinder(session).put(
        b"Next.js static chunks observed",
        source="mitm:request",
        source_metadata={
            "url": "https://example.test/_next/static/app.js",
            "method": "GET",
            "status": 200,
        },
        summary="Next.js static chunk response",
        content_type="text/plain",
    )
    payload = {
        "chunks": [{
            "choices": [{
                "delta": {
                    "reasoning_content": (
                        "<tool_call>\n"
                        "<function=mcp_ara_forge_lead_file>\n"
                        "<parameter=reasoning>"
                        "Enumerate exposed Next.js routes from static chunks."
                        "</parameter>\n"
                        "<parameter=proposed_test>"
                        "Review discovered chunk paths for route hints."
                        "</parameter>\n"
                        "</function>\n"
                        "</tool_call>\n"
                    ),
                },
            }],
        }],
    }

    count = _dispatch_pseudo_task_channel_calls(state, session, payload)

    assert count == 1
    rows = [
        json.loads(line)
        for line in (session / "events.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["kind"] == "channel.lead.file"
    assert rows[-1]["body"]["status"] == "ok"


def test_dispatch_pseudo_task_rejects_chunk_gated_lead_before_audit_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = state / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    _write_task_loadout(state)
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
    run.record_action("exec.spawn", {"command": "python3"})
    run.record_commit("lead.file")
    run.record_action("exec.spawn", {"command": "python3"})
    monkeypatch.setenv("ARA_TASK_RUN_ID", run.task_id)
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    channel_registry.unregister_for_tests()
    payload = {
        "chunks": [{
            "choices": [{
                "delta": {
                    "reasoning_content": (
                        "<tool_call>\n"
                        "<function=mcp_ara_forge_lead_file>\n"
                        "<parameter=title>More enum</parameter>\n"
                        "<parameter=reasoning>"
                        "Keep enumerating instead of transitioning."
                        "</parameter>\n"
                        "</function>\n"
                        "</tool_call>\n"
                    ),
                },
            }],
        }],
    }

    count = _dispatch_pseudo_task_channel_calls(state, session, payload)

    assert count == 0
    events_path = session / "events.jsonl"
    if events_path.exists():
        assert "channel.lead.file" not in events_path.read_text()


def test_mitm_commit_debt_filter_excludes_evidence_lookup_tools() -> None:
    proxy = MitmProxy.__new__(MitmProxy)
    proxy._task_channel_debt_active = lambda: True  # type: ignore[method-assign]
    payload = {
        "tools": [
            {"function": {"name": "terminal"}},
            {"function": {"name": "mcp_ara_forge_evidence_search"}},
            {"function": {"name": "mcp_ara_forge_lead_file"}},
            {"function": {"name": "mcp_ara_forge_chunks_emit_tag"}},
            {"function": {"name": "mcp_ara_forge_evidence_get"}},
        ],
    }

    out = proxy._maybe_filter_task_channel_debt_tools(payload)

    assert out is not None
    assert out["tool_choice"] == "required"
    assert [
        tool["function"]["name"]
        for tool in out["tools"]
    ] == [
        "mcp_ara_forge_lead_file",
        "mcp_ara_forge_chunks_emit_tag",
    ]


def test_mitm_chunk_gated_branch_debt_filter_keeps_only_chunk_transition(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    session = tmp_path / "sessions" / "eng1"
    state.mkdir()
    session.mkdir(parents=True)
    _write_task_loadout(state)
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
    run.record_action("exec.spawn", {"command": "python3"})
    run.record_commit("lead.file")
    run.record_action("exec.spawn", {"command": "python3"})
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-test")
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    proxy = MitmProxy.__new__(MitmProxy)
    proxy.state_root = state
    proxy.session_dir = session
    payload = {
        "tools": [
            {"function": {"name": "terminal"}},
            {"function": {"name": "mcp_ara_forge_fact_assert"}},
            {"function": {"name": "mcp_ara_forge_lead_file"}},
            {"function": {"name": "mcp_ara_forge_finding_file"}},
            {"function": {"name": "mcp_ara_forge_chunks_emit_tag"}},
        ],
    }

    out = proxy._maybe_filter_task_channel_debt_tools(payload)

    assert out is not None
    assert out["tool_choice"] == {
        "type": "function",
        "function": {"name": "mcp_ara_forge_chunks_emit_tag"},
    }
    assert [
        tool["function"]["name"]
        for tool in out["tools"]
    ] == ["mcp_ara_forge_chunks_emit_tag"]
