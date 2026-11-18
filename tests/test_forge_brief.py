"""Tests for the auto-injected forge-v1 prompt brief."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ara import forge_brief
from ara import task_session
from ara import weapons as weapons_mod
from ara.runtime import audit as raudit
from ara.runtime.channels import registry as channel_registry


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    raudit.reset_registry_for_tests()
    channel_registry.unregister_for_tests()
    yield
    raudit.reset_registry_for_tests()
    channel_registry.unregister_for_tests()


# ---------- pure brief composition ----------

def test_build_brief_includes_sentinel_and_hermes_mcp_tool_hint() -> None:
    out = forge_brief.build_brief(
        chunk_id=None, chunk_title=None, chunk_prompt_fragment=None,
        tool_surface=[], exits={}, alphabet_pack_name=None,
        weapon_id=None,
    )
    assert "ARA_FORGE_BRIEF_V1" in out
    assert "Hermes exposes these as MCP" in out
    assert "tools named ``mcp_ara_forge_*``" in out
    assert "globalThis.ara.channel" not in out


def test_build_brief_requires_structured_channel_commits() -> None:
    out = forge_brief.build_brief(
        chunk_id="enum", chunk_title="Enumerate", chunk_prompt_fragment=None,
        tool_surface=["fact.assert", "lead.file"], exits={},
        alphabet_pack_name=None, weapon_id="webapp-recon",
    )

    assert "Phase completion requires structured ARA records" in out
    assert "fact.assert" in out
    assert "lead.file" in out
    assert "finding.file" in out
    assert "a prose report is not a substitute" in out


def test_build_brief_blocks_external_extractors_for_target_urls() -> None:
    out = forge_brief.build_brief(
        chunk_id="enum", chunk_title="Enumerate", chunk_prompt_fragment=None,
        tool_surface=["fact.assert"], exits={}, alphabet_pack_name=None,
        weapon_id="webapp-recon",
    )

    assert "Do not send target URLs to web_extract, web_search" in out
    assert "contained browser/terminal" in out
    assert "forge-sealed resources" in out


def test_build_brief_lists_active_chunk_tools_and_exits() -> None:
    out = forge_brief.build_brief(
        chunk_id="probe", chunk_title="Probe — investigate",
        chunk_prompt_fragment="You are in probe mode.",
        tool_surface=["fact.assert", "lead.file", "facts.query"],
        exits={"<readied>": "writeup", "<done>": "enum"},
        alphabet_pack_name=None, weapon_id="wstg-v1",
    )
    assert "Active chunk: `probe`" in out
    assert "`fact.assert`" in out
    assert "`mcp_ara_forge_fact_assert`" in out
    assert "`lead.file`" in out
    assert "`mcp_ara_forge_lead_file`" in out
    assert "<readied>" in out and "writeup" in out
    assert "wstg-v1" in out


def test_build_brief_filters_unavailable_task_mode_channels() -> None:
    out = forge_brief.build_brief(
        chunk_id="enum", chunk_title="Enumerate",
        chunk_prompt_fragment="You are in enumeration mode.",
        tool_surface=["fact.assert", "lead.file", "mats.search", "facts.query"],
        exits={}, alphabet_pack_name=None, weapon_id="webapp-recon",
        available_channels=["fact.assert", "lead.file"],
        runtime_mode="task",
    )

    assert "ARA task supervision" in out
    assert "forge-v1 supervision" not in out
    assert "`fact.assert`" in out
    assert "`lead.file`" in out
    assert "mats.search" not in out
    assert "facts.query" not in out


def test_build_brief_per_channel_examples_present() -> None:
    out = forge_brief.build_brief(
        chunk_id="enum", chunk_title="x", chunk_prompt_fragment="x",
        tool_surface=["fact.assert", "lead.file", "finding.file"],
        exits={}, alphabet_pack_name=None, weapon_id=None,
    )
    # Each channel known in the usage table emits its example
    assert "mcp_ara_forge_fact_assert" in out
    assert "predicate" in out  # fact.assert example
    assert "mcp_ara_forge_lead_file" in out
    assert "title" in out      # lead.file example
    assert "mcp_ara_forge_finding_file" in out
    assert "severity_cell" in out  # finding.file example


def test_build_brief_lists_weapon_resources() -> None:
    out = forge_brief.build_brief(
        chunk_id="probe", chunk_title="Probe", chunk_prompt_fragment=None,
        tool_surface=["lead.file"], exits={}, alphabet_pack_name=None,
        weapon_id="webapp-recon",
        resources=[
            {
                "id": "res_github_wordlists",
                "class": "wordlist",
                "name": "SecLists wordlists",
                "locator": "https://github.com/danielmiessler/SecLists",
                "allowed_uses": ["reference", "fetch"],
                "requires_approval_for": ["execute"],
            }
        ],
    )

    assert "Weapon resources" in out
    assert "res_github_wordlists" in out
    assert "https://github.com/danielmiessler/SecLists" in out
    assert "allowed: reference, fetch" in out
    assert "approval: execute" in out


def test_build_brief_marks_unresolved_weapon_resources() -> None:
    out = forge_brief.build_brief(
        chunk_id="probe", chunk_title="Probe", chunk_prompt_fragment=None,
        tool_surface=["lead.file"], exits={}, alphabet_pack_name=None,
        weapon_id="webapp-recon",
        resources=[
            {
                "id": "res_payloads",
                "class": "wordlist",
                "name": "Payload list",
                "status": "needs_resolution",
                "hints": ["Payload list", "public repo"],
                "allowed_uses": ["reference", "fetch"],
                "requires_approval_for": ["execute"],
            }
        ],
    )

    assert "`res_payloads` [wordlist] Payload list" in out
    assert "status: needs_resolution" in out
    assert "locator:" not in out
    assert "hints: Payload list, public repo" in out
    assert "no network access is granted yet" in out


def test_already_briefed_detection() -> None:
    assert forge_brief.already_briefed(
        "previous text\n<!-- ARA_FORGE_BRIEF_V1 -->\nrest"
    )
    assert not forge_brief.already_briefed("regular system prompt")
    assert not forge_brief.already_briefed("")


# ---------- payload injection: Anthropic shape ----------

def test_inject_into_anthropic_string_system() -> None:
    brief = forge_brief.build_brief(
        chunk_id="enum", chunk_title="x", chunk_prompt_fragment="x",
        tool_surface=["fact.assert"], exits={}, alphabet_pack_name=None,
        weapon_id=None,
    )
    payload = {"system": "You are an expert pen-tester.",
                "messages": [{"role": "user", "content": "begin"}]}
    out = forge_brief.inject_into_payload(payload, brief)
    assert out is not payload  # new dict
    assert out["system"].startswith("<!-- ARA_FORGE_BRIEF_V1 -->")
    assert "expert pen-tester" in out["system"]


def test_inject_idempotent_for_anthropic_string_system() -> None:
    brief = "<!-- ARA_FORGE_BRIEF_V1 -->\nbrief content\n"
    payload = {"system": brief + "existing prompt\n",
                "messages": [{"role": "user", "content": "begin"}]}
    out = forge_brief.inject_into_payload(payload, brief)
    assert out is payload  # no change


def test_inject_into_anthropic_block_system() -> None:
    brief = "<!-- ARA_FORGE_BRIEF_V1 -->\nb"
    payload = {
        "system": [{"type": "text", "text": "you are X"}],
        "messages": [{"role": "user", "content": "go"}],
    }
    out = forge_brief.inject_into_payload(payload, brief)
    assert isinstance(out["system"], list)
    assert out["system"][0]["text"] == brief


# ---------- payload injection: OpenAI shape ----------

def test_inject_into_openai_with_existing_system_message() -> None:
    brief = "<!-- ARA_FORGE_BRIEF_V1 -->\nB"
    payload = {"messages": [
        {"role": "system", "content": "you are X"},
        {"role": "user", "content": "hi"},
    ]}
    out = forge_brief.inject_into_payload(payload, brief)
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][0]["content"].startswith("<!-- ARA_FORGE_BRIEF_V1 -->")
    assert "you are X" in out["messages"][0]["content"]
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_inject_into_openai_without_system_message() -> None:
    brief = "<!-- ARA_FORGE_BRIEF_V1 -->\nB"
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    out = forge_brief.inject_into_payload(payload, brief)
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][0]["content"].startswith("<!-- ARA_FORGE_BRIEF_V1 -->")


def test_inject_idempotent_for_openai() -> None:
    brief = "<!-- ARA_FORGE_BRIEF_V1 -->\nB"
    payload = {"messages": [
        {"role": "system", "content": brief + "\n\nyou are X"},
        {"role": "user", "content": "hi"},
    ]}
    out = forge_brief.inject_into_payload(payload, brief)
    assert out is payload


def test_inject_passes_through_unknown_shape() -> None:
    brief = "<!-- ARA_FORGE_BRIEF_V1 -->\nB"
    out = forge_brief.inject_into_payload(
        {"random": "thing", "no_recognized_keys": True}, brief,
    )
    # Returns the same dict (no fields to inject into)
    assert out == {"random": "thing", "no_recognized_keys": True}


def test_inject_passes_through_non_dict() -> None:
    brief = "B"
    assert forge_brief.inject_into_payload("a string", brief) == "a string"
    assert forge_brief.inject_into_payload(None, brief) is None


# ---------- supervisor wiring ----------

def _seal_minimal_weapon(state_root: Path, *, weapon_id: str = "w-brief") -> None:
    from ara.forge.chunks.chunkdef import ChunkDef, ExitTarget, write_chunkdef
    wdir = state_root / "weapons" / weapon_id
    (wdir / "chunks").mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps({
        "weapon_target_id": weapon_id, "crumbs": [],
    }))
    (wdir / "seal.json").write_text(json.dumps({"weapon_id": weapon_id}))
    write_chunkdef(wdir / "chunks" / "enum.json", ChunkDef(
        id="enum", title="Enumerate",
        prompt_fragment="You are in enumeration mode.",
        tool_surface=["fact.assert", "facts.query"],
        is_entry=True,
        exits={"<go>": ExitTarget(tag="<go>", next_chunk_id="probe",
                                    requires_caller="agent")},
    ))
    write_chunkdef(wdir / "chunks" / "probe.json", ChunkDef(
        id="probe", title="Probe",
        prompt_fragment="You are in probe mode.",
        tool_surface=["fact.assert", "lead.file"],
        is_terminal=True,
    ))


def _make_supervisor(state_root: Path):
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


def test_request_with_engagement_returns_rewrite_with_brief(tmp_path: Path) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-brief")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    res = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept",
        "role": "request",
        "payload": {"system": "You are X.",
                    "messages": [{"role": "user", "content": "go"}]},
        "seq": 1,
    }))
    assert res["action"] == "rewrite"
    assert res["reason"] == "FORGE_BRIEF_INJECTED"
    new_payload = res["new_payload"]
    assert "<!-- ARA_FORGE_BRIEF_V1 -->" in new_payload["system"]
    assert "Active chunk: `enum`" in new_payload["system"]
    assert "`fact.assert`" in new_payload["system"]
    assert "<go>" in new_payload["system"]
    assert "w-brief" in new_payload["system"]


def test_request_without_engagement_no_rewrite(tmp_path: Path) -> None:
    sup = _make_supervisor(tmp_path)
    res = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept",
        "role": "request",
        "payload": {"system": "x", "messages": []},
        "seq": 1,
    }))
    assert res["action"] == "allow"


def test_response_role_does_not_trigger_inject(tmp_path: Path) -> None:
    """Auto-brief is request-only; assistant turns must not be rewritten."""
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-brief")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    res = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept",
        "role": "response",
        "payload": {"content": "assistant said something"},
    }))
    assert res["action"] == "allow"


def test_idempotent_re_mediation(tmp_path: Path) -> None:
    """Re-mediating the same (already-briefed) payload doesn't stack
    or change it."""
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-brief")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    payload = {"system": "x", "messages": []}
    first = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept", "role": "request",
        "payload": payload,
    }))
    assert first["action"] == "rewrite"
    rebriefed = first["new_payload"]
    second = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept", "role": "request",
        "payload": rebriefed,
    }))
    assert second["action"] == "allow"  # no second rewrite


def test_brief_reflects_active_chunk_after_swap(tmp_path: Path) -> None:
    """When the agent swaps to a different chunk, the next outbound
    request's brief lists that chunk's tool_surface, not the old one."""
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-brief")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    # First request: brief lists enum's tools
    r1 = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept", "role": "request",
        "payload": {"system": "x", "messages": []},
    }))
    sys_text = r1["new_payload"]["system"]
    assert "Active chunk: `enum`" in sys_text
    assert "`facts.query`" in sys_text
    assert "`lead.file`" not in sys_text   # not in enum's surface

    # Swap to probe via inline tag scanner
    sup._scan_inline_tags("done <go>")
    assert sup.engagement.chunk_runtime.current_chunk_id() == "probe"

    # Second request reflects probe's surface
    r2 = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept", "role": "request",
        "payload": {"system": "x", "messages": []},
    }))
    sys2 = r2["new_payload"]["system"]
    assert "Active chunk: `probe`" in sys2
    assert "`lead.file`" in sys2


def test_rhema_rule_takes_precedence_over_brief_rewrite(tmp_path: Path) -> None:
    """If a Rhema rule fires (deny / explicit rewrite), it wins over
    the brief auto-rewrite. The brief is only used as the default
    rewrite when no Rhema rule matched."""
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-brief")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    # Without a Rhema rule, brief rewrite happens (above tests cover it)
    # No Rhema rules to plant in tmp_path (Rhema reads from state_root);
    # this assertion just guards the behaviour shape.
    res = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept", "role": "request",
        "payload": {"system": "x", "messages": []},
    }))
    # Default case: brief wins, action=rewrite
    assert res["action"] == "rewrite"
    assert res.get("rule_id") is None  # Rhema didn't fire


def test_task_channel_debt_filters_action_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-brief")
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    monkeypatch.setenv("ARA_TASK_CHANNEL_EXEC_BUDGET", "3")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    with sup.session.events_path.open("w") as fh:
        for _ in range(2):
            fh.write(json.dumps({"kind": "spawn"}) + "\n")

    res = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept",
        "role": "request",
        "payload": {
            "messages": [{"role": "user", "content": "continue"}],
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
        },
        "seq": 1,
    }))

    assert res["action"] == "rewrite"
    names = [
        tool["function"]["name"]
        for tool in res["new_payload"]["tools"]
    ]
    assert names == [
        "mcp_ara_forge_lead_file",
        "mcp_ara_forge_fact_assert",
    ]
    assert res["new_payload"]["tool_choice"] == "required"


def test_task_channel_debt_filters_from_task_session_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-brief")
    run = task_session.TaskRunSession.new(
        tmp_path,
        armor_id="blacksmith",
        weapon_id="w-brief",
        trim_id="trim-a",
        temper_id="temp-a",
        action_budget=1,
        task_id="task-test",
    )
    assert run.record_action("net.connect").allowed is True
    monkeypatch.setenv("ARA_TASK_SUPERVISOR", "armory-hermes-loop")
    monkeypatch.setenv("ARA_TASK_RUN_ID", run.task_id)
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()

    res = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept",
        "role": "request",
        "payload": {
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
        },
        "seq": 1,
    }))

    assert res["action"] == "rewrite"
    assert [
        tool["function"]["name"]
        for tool in res["new_payload"]["tools"]
    ] == ["mcp_ara_forge_fact_assert"]
    assert res["new_payload"]["tool_choice"] == "required"
