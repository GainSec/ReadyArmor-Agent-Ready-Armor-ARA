"""Tests for the supervisor's inline chunk-swap tag scanner.

Covers:
- payload-shape extraction (Anthropic, OpenAI, plain string)
- only fires for tags that match the active chunk's exits
- skips random brackets in agent prose
- doesn't fire when no engagement / no chunk runtime mounted
- caller is hard-coded to "agent" (operator-only exits reject)
- duplicate tags in the same turn fire once
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

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


def _seal_minimal_weapon(state_root: Path, *, weapon_id: str = "w-it") -> None:
    from ara.forge.chunks.chunkdef import ChunkDef, ExitTarget, write_chunkdef
    wdir = state_root / "weapons" / weapon_id
    (wdir / "chunks").mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps({"weapon_target_id": weapon_id}))
    (wdir / "seal.json").write_text(json.dumps({"weapon_id": weapon_id}))
    write_chunkdef(wdir / "chunks" / "enum.json", ChunkDef(
        id="enum", title="x", prompt_fragment="x",
        tool_surface=["fact.assert"], is_entry=True,
        exits={
            "<go>": ExitTarget(tag="<go>", next_chunk_id="probe",
                                requires_caller="agent"),
            "<halt>": ExitTarget(tag="<halt>", next_chunk_id="end",
                                  requires_caller="operator"),
        },
    ))
    write_chunkdef(wdir / "chunks" / "probe.json", ChunkDef(
        id="probe", title="x", prompt_fragment="x",
        tool_surface=["lead.file"],
    ))
    write_chunkdef(wdir / "chunks" / "end.json", ChunkDef(
        id="end", title="x", prompt_fragment="x", is_terminal=True,
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


# ---------- payload-shape extraction ----------

def test_payload_to_text_handles_string() -> None:
    from ara.supervisor import _payload_to_text
    assert _payload_to_text("hello") == "hello"


def test_payload_to_text_anthropic_content_blocks() -> None:
    from ara.supervisor import _payload_to_text
    payload = {
        "content": [
            {"type": "text", "text": "first part"},
            {"type": "tool_use", "name": "x", "input": {}},
            {"type": "text", "text": "second part"},
        ],
    }
    out = _payload_to_text(payload)
    assert "first part" in out
    assert "second part" in out
    assert "tool_use" not in out  # ignored


def test_payload_to_text_openai_choices() -> None:
    from ara.supervisor import _payload_to_text
    payload = {
        "choices": [
            {"message": {"content": "from choice 0"}},
            {"text": "from choice 1"},
        ],
    }
    out = _payload_to_text(payload)
    assert "from choice 0" in out
    assert "from choice 1" in out


def test_payload_to_text_plain_content_string() -> None:
    from ara.supervisor import _payload_to_text
    assert "hello world" in _payload_to_text({"content": "hello world"})


def test_payload_to_text_non_dict_returns_empty() -> None:
    from ara.supervisor import _payload_to_text
    assert _payload_to_text(None) == ""
    assert _payload_to_text(42) == ""


# ---------- scanner: no-op cases ----------

def test_scan_no_op_without_engagement(tmp_path: Path) -> None:
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()  # no weapon equipped → engagement None
    # Should be a silent no-op (no exception, no state change)
    sup._scan_inline_tags("done <go> moving on")


def test_scan_no_op_without_chunk_runtime(tmp_path: Path) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-it")
    # Build engagement but force chunk_runtime=None
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    sup.engagement = type(sup.engagement)(
        channels=sup.engagement.channels,
        crumb_tracker=sup.engagement.crumb_tracker,
        autofirer=None,
        weapon_shapes=None,
        chunk_runtime=None,
        activation_manager=sup.engagement.activation_manager,
    )
    sup._scan_inline_tags("done <go> moving on")  # no exception


# ---------- scanner: actual swap firing ----------

def test_scan_fires_chunk_swap_on_matching_tag(tmp_path: Path) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-it")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    rt = sup.engagement.chunk_runtime
    assert rt.current_chunk_id() == "enum"
    sup._scan_inline_tags("Surface mapping done. <go> Moving to probe.")
    assert rt.current_chunk_id() == "probe"


def test_scan_ignores_random_brackets_in_prose(tmp_path: Path) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-it")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    rt = sup.engagement.chunk_runtime
    sup._scan_inline_tags(
        "Saw <thinking> tags and <reflection> tags but not exit tags."
    )
    assert rt.current_chunk_id() == "enum"  # no swap


def test_scan_does_not_fire_operator_only_exits(tmp_path: Path) -> None:
    """The agent emitting <halt> in prose must not swap to end —
    that exit requires caller=operator."""
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-it")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    rt = sup.engagement.chunk_runtime
    sup._scan_inline_tags("Done with everything <halt>")
    # Stayed at enum (caller_mismatch on agent + operator-required tag)
    assert rt.current_chunk_id() == "enum"


def test_scan_dedups_repeated_tags_in_same_turn(tmp_path: Path) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-it")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    rt = sup.engagement.chunk_runtime
    # Two <go> markers — should fire once, second is dedup'd
    sup._scan_inline_tags("<go> moving <go> definitely")
    assert rt.current_chunk_id() == "probe"


# ---------- end-to-end via _on_prompt_intercept ----------

def test_prompt_intercept_assistant_role_triggers_scan(tmp_path: Path) -> None:
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-it")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    rt = sup.engagement.chunk_runtime
    res = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept",
        "role": "response",
        "payload": {"content": [
            {"type": "text", "text": "All set <go> let's probe"},
        ]},
        "seq": 1,
    }))
    assert res["action"] == "allow"
    assert rt.current_chunk_id() == "probe"


def test_prompt_intercept_request_role_does_not_scan(tmp_path: Path) -> None:
    """Scanner must only run on assistant/response/completion roles —
    user input that happens to contain <go> must NOT trigger a swap."""
    _seal_minimal_weapon(tmp_path)
    weapons_mod.set_equipped_weapon(tmp_path, "w-it")
    sup = _make_supervisor(tmp_path)
    sup._maybe_install_engagement()
    rt = sup.engagement.chunk_runtime
    asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept",
        "role": "request",
        "payload": {"content": "user said <go> but they're not the agent"},
        "seq": 1,
    }))
    assert rt.current_chunk_id() == "enum"


def test_prompt_intercept_with_no_engagement_is_safe(tmp_path: Path) -> None:
    sup = _make_supervisor(tmp_path)
    res = asyncio.run(sup._on_prompt_intercept({
        "type": "prompt_intercept",
        "role": "response",
        "payload": {"content": "anything <go> here"},
    }))
    assert res["action"] == "allow"
    # Engagement is None — no chunk swap could happen even if the tag matched
    assert sup.engagement is None
