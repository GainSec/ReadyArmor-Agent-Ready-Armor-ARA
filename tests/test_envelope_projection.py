from __future__ import annotations

import json
from pathlib import Path

import pytest

from ara import armors as armors_mod
from ara import task_session
from ara.mitm_proxy import MitmProxy


def _write_task_loadout(state: Path) -> None:
    (state / "armors" / "hermes-default" / "absolutes").mkdir(
        parents=True, exist_ok=True,
    )
    (state / "armors" / "hermes-default" / "absolutes" / "default.md").write_text("armor\n")
    (state / "weapons" / "w-task").mkdir(parents=True, exist_ok=True)
    (state / "weapons" / "w-task" / "seal.json").write_text("{}")
    (state / "armors" / "hermes-default" / "trims" / "trim-a").mkdir(
        parents=True, exist_ok=True,
    )
    (state / "armors" / "hermes-default" / "trims" / "trim-a" / "meta.json").write_text("{}")
    (state / "armors" / "hermes-default" / "temper" / "proposals").mkdir(
        parents=True, exist_ok=True,
    )
    (state / "armors" / "hermes-default" / "temper" / "proposals" / "temp-a.json").write_text("{}")


def test_mitm_absolute_envelope_rewrites_chat_completions(short_tmp: Path) -> None:
    state = short_tmp / "state"
    session = short_tmp / "session"
    state.mkdir()
    session.mkdir()
    armors_mod.create_armor(state, "hermes-default", description="test")
    armor = armors_mod.load_armor(state, "hermes-default")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("ARA ENVELOPE")
    armors_mod.set_active_armor(state, "hermes-default")
    armors_mod.set_absolute_enforce(state, "hermes-default", True)

    proxy = MitmProxy(state, session)
    body = {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": "Hermes persona"},
            {"role": "user", "content": "hello"},
        ],
    }
    rewritten = proxy._try_apply_request_rewrite(
        "api.openai.com",
        443,
        {
            "start": "POST /v1/chat/completions HTTP/1.1",
            "headers": {"content-type": "application/json"},
        },
        json.dumps(body).encode(),
        "openai",
    )
    assert rewritten is not None
    sent = json.loads(rewritten)
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][0]["content"] == "ARA ENVELOPE"
    assert "Hermes persona" not in sent["messages"][0]["content"]
    assert sent["messages"][1]["content"] == "hello"


def test_mitm_absolute_envelope_rewrites_codex_instructions(short_tmp: Path) -> None:
    state = short_tmp / "state"
    session = short_tmp / "session"
    state.mkdir()
    session.mkdir()
    armors_mod.create_armor(state, "codex-default", description="test")
    armor = armors_mod.load_armor(state, "codex-default")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("ARA ENVELOPE")
    armors_mod.set_active_armor(state, "codex-default")
    armors_mod.set_absolute_enforce(state, "codex-default", True)

    proxy = MitmProxy(state, session)
    body = {"instructions": "old", "input": "hello"}
    rewritten = proxy._try_apply_request_rewrite(
        "api.openai.com",
        443,
        {
            "start": "POST /codex/responses HTTP/1.1",
            "headers": {"content-type": "application/json"},
        },
        json.dumps(body).encode(),
        "openai",
    )
    assert rewritten is not None
    sent = json.loads(rewritten)
    assert sent["instructions"] == "ARA ENVELOPE"
    assert sent["input"] == "hello"


def test_mitm_absolute_envelope_uses_task_temper_projection(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = short_tmp / "state"
    session = short_tmp / "session"
    state.mkdir()
    session.mkdir()
    armors_mod.create_armor(state, "hermes-default", description="test")
    armor = armors_mod.load_armor(state, "hermes-default")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text(
        "BLOATED ARMOR ABSOLUTE\nHardware firmware boundary",
    )
    projection = state / "task-runs" / "task-web" / "absolute.md"
    projection.parent.mkdir(parents=True)
    projection.write_text("PROJECTED WEB ABSOLUTE\nWeb-app boundary\n")
    armors_mod.set_active_armor(state, "hermes-default")
    armors_mod.set_absolute_enforce(state, "hermes-default", True)
    monkeypatch.setenv("ARA_TASK_RUN_ID", "task-web")

    proxy = MitmProxy(state, session)
    body = {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": "Hermes persona"},
            {"role": "user", "content": "hello"},
        ],
    }
    rewritten = proxy._try_apply_request_rewrite(
        "api.openai.com",
        443,
        {
            "start": "POST /v1/chat/completions HTTP/1.1",
            "headers": {"content-type": "application/json"},
        },
        json.dumps(body).encode(),
        "openai",
    )

    assert rewritten is not None
    sent = json.loads(rewritten)
    assert sent["messages"][0]["content"] == "PROJECTED WEB ABSOLUTE\nWeb-app boundary"
    assert "Hardware firmware boundary" not in sent["messages"][0]["content"]


def test_mitm_task_channel_debt_filters_tools_after_absolute_rewrite(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = short_tmp / "state"
    session = short_tmp / "session"
    state.mkdir()
    session.mkdir()
    (session / "events.jsonl").write_text(
        json.dumps({"kind": "spawn"}) + "\n"
        + json.dumps({"kind": "spawn"}) + "\n",
    )
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    monkeypatch.setenv("ARA_TASK_CHANNEL_EXEC_BUDGET", "3")

    armors_mod.create_armor(state, "hermes-default", description="test")
    armor = armors_mod.load_armor(state, "hermes-default")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("ARA ENVELOPE")
    armors_mod.set_active_armor(state, "hermes-default")
    armors_mod.set_absolute_enforce(state, "hermes-default", True)

    proxy = MitmProxy(state, session)
    body = {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": "Hermes persona"},
            {"role": "user", "content": "continue"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "terminal"}},
            {"type": "function", "function": {"name": "read_file"}},
            {
                "type": "function",
                "function": {"name": "mcp_ara_forge_fact_assert"},
            },
            {
                "type": "function",
                "function": {"name": "mcp_ara_forge_lead_file"},
            },
        ],
    }
    rewritten = proxy._try_apply_request_rewrite(
        "api.openai.com",
        443,
        {
            "start": "POST /v1/chat/completions HTTP/1.1",
            "headers": {"content-type": "application/json"},
        },
        json.dumps(body).encode(),
        "openai",
    )
    assert rewritten is not None
    sent = json.loads(rewritten)
    assert sent["messages"][0]["content"] == "ARA ENVELOPE"
    assert [
        tool["function"]["name"]
        for tool in sent["tools"]
    ] == [
        "mcp_ara_forge_lead_file",
        "mcp_ara_forge_fact_assert",
    ]
    assert sent["tool_choice"] == "required"


def test_mitm_task_channel_debt_filters_from_task_session_phase(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = short_tmp / "state"
    session = short_tmp / "session"
    state.mkdir()
    session.mkdir()
    armors_mod.create_armor(state, "hermes-default", description="test")
    _write_task_loadout(state)
    run = task_session.TaskRunSession.new(
        state,
        armor_id="hermes-default",
        weapon_id="w-task",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    assert run.record_action("net.connect").allowed is True
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    monkeypatch.setenv("ARA_TASK_RUN_ID", run.task_id)

    armor = armors_mod.load_armor(state, "hermes-default")
    assert armor is not None
    armors_mod.path_for_absolute(armor, "default").write_text("ARA ENVELOPE")
    armors_mod.set_active_armor(state, "hermes-default")
    armors_mod.set_absolute_enforce(state, "hermes-default", True)

    proxy = MitmProxy(state, session)
    body = {
        "model": "qwen",
        "messages": [{"role": "user", "content": "continue"}],
        "tools": [
            {"type": "function", "function": {"name": "terminal"}},
            {
                "type": "function",
                "function": {"name": "mcp_ara_forge_fact_assert"},
            },
            {
                "type": "function",
                "function": {"name": "mcp_ara_forge_evidence_get"},
            },
        ],
    }
    rewritten = proxy._try_apply_request_rewrite(
        "api.openai.com",
        443,
        {
            "start": "POST /v1/chat/completions HTTP/1.1",
            "headers": {"content-type": "application/json"},
        },
        json.dumps(body).encode(),
        "openai",
    )

    assert rewritten is not None
    sent = json.loads(rewritten)
    assert [
        tool["function"]["name"]
        for tool in sent["tools"]
    ] == ["mcp_ara_forge_fact_assert"]
    assert sent["tool_choice"] == "required"
