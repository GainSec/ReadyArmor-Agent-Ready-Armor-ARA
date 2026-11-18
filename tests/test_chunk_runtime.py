"""Tests for runtime.chunks.runtime + chunks.emit_tag channel."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ara.forge.chunks.chunkdef import ChunkDef, ExitTarget, write_chunkdef
from ara.runtime import audit as raudit
from ara.runtime.activation import ActivationManager
from ara.runtime.anomaly.crumb_tracker import CrumbTracker
from ara.runtime.channels import registry
from ara.runtime.channels.base import ChannelContext, ChannelRejection
from ara.runtime.chunks.channels import (
    ChunksEmitTagChannel,
    install_chunks_channels,
)
from ara.runtime.chunks.runtime import (
    ChunkRuntime,
    SwapResult,
    WeaponChunkGraph,
    parse_inline_tags,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()
    yield
    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()


def _build_weapon(tmp_path: Path) -> Path:
    """Lay down a tiny chunks/ tree: enum (entry) → probe ↔ writeup → end."""
    weapon_dir = tmp_path / "weapon"
    cdir = weapon_dir / "chunks"
    enum = ChunkDef(
        id="enum", title="Enumerate", prompt_fragment="x",
        tool_surface=["fact.assert", "facts.query"],
        exits={
            "<go>": ExitTarget(tag="<go>", next_chunk_id="probe",
                                requires_caller="agent"),
        },
        is_entry=True,
    )
    probe = ChunkDef(
        id="probe", title="Probe", prompt_fragment="x",
        tool_surface=["lead.file", "facts.query", "fact.assert"],
        exits={
            "<readied>": ExitTarget(tag="<readied>", next_chunk_id="writeup",
                                     requires_caller="agent"),
            "<back>": ExitTarget(tag="<back>", next_chunk_id="enum",
                                  requires_caller="agent"),
        },
    )
    writeup = ChunkDef(
        id="writeup", title="Writeup", prompt_fragment="x",
        tool_surface=["finding.file", "finding.sign_off"],
        exits={
            "<done>": ExitTarget(tag="<done>", next_chunk_id="end",
                                  requires_caller="operator"),
            "<back>": ExitTarget(tag="<back>", next_chunk_id="probe",
                                  requires_caller="agent"),
        },
        can_emit_finding=True,
    )
    end = ChunkDef(id="end", title="End", prompt_fragment="x", is_terminal=True)
    for c in (enum, probe, writeup, end):
        write_chunkdef(cdir / f"{c.id}.json", c)
    return weapon_dir


# ---------- graph loading ----------

def test_weapon_graph_loads_entry_and_chunks(tmp_path: Path) -> None:
    weapon_dir = _build_weapon(tmp_path)
    graph = WeaponChunkGraph.load(weapon_dir)
    assert {c.id for c in graph.chunks.values()} == {"enum", "probe", "writeup", "end"}
    assert graph.entry_chunk().id == "enum"
    assert graph.has("probe")


def test_weapon_graph_with_no_entry(tmp_path: Path) -> None:
    weapon_dir = tmp_path / "weapon"
    cdir = weapon_dir / "chunks"
    write_chunkdef(cdir / "x.json", ChunkDef(id="x", title="x",
                                              prompt_fragment="x",
                                              is_terminal=True))
    graph = WeaponChunkGraph.load(weapon_dir)
    assert graph.entry_chunk() is None


# ---------- initialize ----------

def test_initialize_mounts_entry_and_mints_activation(tmp_path: Path) -> None:
    weapon_dir = _build_weapon(tmp_path)
    sd = tmp_path / "session"
    am = ActivationManager(sd)
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(weapon_dir),
        activation_manager=am,
        history_path=sd / "chunk_history.jsonl",
    )
    assert rt.current_chunk_id() is None
    res = rt.initialize()
    assert res.ok
    assert rt.current_chunk_id() == "enum"
    # Activation minted with chunk_id metadata
    history = am.history()
    assert len(history) == 1
    assert history[0].chunk_id == "enum"


def test_initialize_fails_without_entry_chunk(tmp_path: Path) -> None:
    cdir = tmp_path / "weapon" / "chunks"
    write_chunkdef(cdir / "x.json", ChunkDef(id="x", title="x",
                                              prompt_fragment="x",
                                              is_terminal=True))
    am = ActivationManager(tmp_path / "session")
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(tmp_path / "weapon"),
        activation_manager=am,
    )
    res = rt.initialize()
    assert not res.ok
    assert res.error_kind == "no_entry"


def test_double_initialize_rejected(tmp_path: Path) -> None:
    weapon_dir = _build_weapon(tmp_path)
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(weapon_dir),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    rt.initialize()
    res = rt.initialize()
    assert not res.ok
    assert res.error_kind == "already_initialized"


# ---------- emit_tag (agent-driven) ----------

def test_emit_tag_swaps_and_mints_new_activation(tmp_path: Path) -> None:
    weapon_dir = _build_weapon(tmp_path)
    sd = tmp_path / "session"
    am = ActivationManager(sd)
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(weapon_dir),
        activation_manager=am,
        history_path=sd / "chunk_history.jsonl",
    )
    rt.initialize()
    pre = am.current()
    res = rt.emit_tag("<go>", caller="agent")
    assert res.ok
    assert res.from_chunk == "enum"
    assert res.to_chunk == "probe"
    assert res.new_activation_id != pre
    assert rt.current_chunk_id() == "probe"


def test_emit_tag_rejects_unknown_tag(tmp_path: Path) -> None:
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    rt.initialize()
    res = rt.emit_tag("<not_real>", caller="agent")
    assert not res.ok
    assert res.error_kind == "invalid_tag"
    # Current chunk unchanged
    assert rt.current_chunk_id() == "enum"


def test_emit_tag_caller_mismatch(tmp_path: Path) -> None:
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    rt.initialize()
    rt.emit_tag("<go>", caller="agent")           # → probe
    rt.emit_tag("<readied>", caller="agent")      # → writeup
    res = rt.emit_tag("<done>", caller="agent")    # writeup→end requires operator
    assert not res.ok
    assert res.error_kind == "caller_mismatch"
    assert rt.current_chunk_id() == "writeup"


def test_emit_tag_before_initialize_rejected(tmp_path: Path) -> None:
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    res = rt.emit_tag("<go>", caller="agent")
    assert not res.ok
    assert res.error_kind == "not_initialized"


# ---------- tool surface enforcement ----------

def test_is_tool_allowed_follows_active_chunk(tmp_path: Path) -> None:
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    rt.initialize()
    # In enum: facts.query allowed, finding.file not
    assert rt.is_tool_allowed("facts.query")
    assert not rt.is_tool_allowed("finding.file")
    rt.emit_tag("<go>", caller="agent")           # → probe
    assert rt.is_tool_allowed("lead.file")
    assert not rt.is_tool_allowed("finding.file")
    rt.emit_tag("<readied>", caller="agent")      # → writeup
    assert rt.is_tool_allowed("finding.file")
    assert not rt.is_tool_allowed("lead.file")


def test_is_tool_allowed_when_unmounted(tmp_path: Path) -> None:
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    # Pre-initialize: pass-through (supervisor hasn't started gating)
    assert rt.is_tool_allowed("anything")


# ---------- crumb tracker integration ----------

def test_chunk_swap_clears_crumb_tracker(tmp_path: Path) -> None:
    tracker = CrumbTracker()
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
        crumb_tracker=tracker,
    )
    rt.initialize()
    tracker.set_active("c_probe_xss")
    assert tracker.current() == "c_probe_xss"
    rt.emit_tag("<go>", caller="agent")
    # Swap clears the tracker so stale crumb identity doesn't leak across chunks
    assert tracker.current() is None


# ---------- override tags ----------

def test_pause_resume(tmp_path: Path) -> None:
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    rt.initialize()
    res = rt.emit_tag("<pause>", caller="operator")
    assert res.ok
    assert rt.is_paused()
    res2 = rt.emit_tag("<resume>", caller="operator")
    assert res2.ok
    assert not rt.is_paused()


def test_kill_blocks_further_swaps(tmp_path: Path) -> None:
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    rt.initialize()
    res = rt.emit_tag("<kill>", caller="operator")
    assert res.ok
    assert rt.is_killed()
    res2 = rt.emit_tag("<go>", caller="agent")
    assert not res2.ok
    assert res2.error_kind == "killed"


def test_jump_forces_chunk(tmp_path: Path) -> None:
    sd = tmp_path / "session"
    am = ActivationManager(sd)
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=am,
        history_path=sd / "chunk_history.jsonl",
    )
    rt.initialize()
    res = rt.emit_tag("<jump:writeup>", caller="operator")
    assert res.ok
    assert rt.current_chunk_id() == "writeup"
    # New activation minted
    assert am.history()[-1].chunk_id == "writeup"


def test_jump_to_unknown_chunk_rejected(tmp_path: Path) -> None:
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    rt.initialize()
    res = rt.emit_tag("<jump:ghost>", caller="operator")
    assert not res.ok
    assert res.error_kind == "unknown_chunk"


def test_override_caller_must_be_operator_or_system(tmp_path: Path) -> None:
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(tmp_path / "session"),
    )
    rt.initialize()
    for tag in ("<pause>", "<resume>", "<kill>", "<jump:probe>", "<rewind:1>"):
        res = rt.emit_tag(tag, caller="agent")
        assert not res.ok, f"{tag!r} should reject agent caller"
        assert res.error_kind == "caller_mismatch"


def test_rewind_one_swap(tmp_path: Path) -> None:
    sd = tmp_path / "session"
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(sd),
        history_path=sd / "chunk_history.jsonl",
    )
    rt.initialize()
    rt.emit_tag("<go>", caller="agent")              # → probe
    rt.emit_tag("<readied>", caller="agent")         # → writeup
    res = rt.emit_tag("<rewind:1>", caller="operator")
    assert res.ok, res.error
    # Rewind 1 swap from writeup → probe
    assert rt.current_chunk_id() == "probe"


def test_rewind_out_of_range(tmp_path: Path) -> None:
    sd = tmp_path / "session"
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(sd),
        history_path=sd / "chunk_history.jsonl",
    )
    rt.initialize()
    res = rt.emit_tag("<rewind:5>", caller="operator")
    assert not res.ok
    assert res.error_kind == "rewind_out_of_range"


# ---------- history persistence ----------

def test_history_persists_each_transition(tmp_path: Path) -> None:
    sd = tmp_path / "session"
    history_path = sd / "chunk_history.jsonl"
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(sd),
        history_path=history_path,
    )
    rt.initialize()
    rt.emit_tag("<go>", caller="agent")
    rt.emit_tag("<readied>", caller="agent")
    lines = history_path.read_text().splitlines()
    assert len(lines) == 3
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds == ["initialize", "swap", "swap"]


# ---------- inline tag parser ----------

def test_parse_inline_tags_finds_brackets() -> None:
    tags = parse_inline_tags(
        "Done with enumeration <go>; deferring further analysis."
    )
    assert tags == ["<go>"]


def test_parse_inline_tags_multiple() -> None:
    tags = parse_inline_tags("first <a> then <b:c> and <d>")
    assert tags == ["<a>", "<b:c>", "<d>"]


def test_parse_inline_tags_handles_nested_punctuation() -> None:
    # No tag inside parens that don't match the regex form
    assert parse_inline_tags("just <plain> text and <another.one>") == [
        "<plain>", "<another.one>",
    ]


# ---------- chunks.emit_tag channel ----------

def test_chunks_emit_tag_channel_swaps_via_runtime(tmp_path: Path) -> None:
    sd = tmp_path / "session"
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(sd),
        history_path=sd / "chunk_history.jsonl",
    )
    rt.initialize()
    chans = install_chunks_channels(runtime=rt)
    ctx = ChannelContext(
        engagement_id="eng-1", activation_id="a-1",
        caller="agent", audit_sink=sd / "events.jsonl",
    )
    out = chans["chunks.emit_tag"].call({"tag": "<go>"}, ctx)
    assert out["to_chunk"] == "probe"
    assert rt.current_chunk_id() == "probe"


def test_chunks_emit_tag_channel_rejects_invalid_tag(tmp_path: Path) -> None:
    sd = tmp_path / "session"
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(sd),
    )
    rt.initialize()
    chans = install_chunks_channels(runtime=rt)
    ctx = ChannelContext(
        engagement_id="eng-1", activation_id="a-1",
        caller="agent", audit_sink=sd / "events.jsonl",
    )
    with pytest.raises(ChannelRejection, match="invalid_tag"):
        chans["chunks.emit_tag"].call({"tag": "<not_real>"}, ctx)


def test_chunks_emit_tag_channel_caller_mismatch_surfaced(tmp_path: Path) -> None:
    sd = tmp_path / "session"
    rt = ChunkRuntime(
        graph=WeaponChunkGraph.load(_build_weapon(tmp_path)),
        activation_manager=ActivationManager(sd),
    )
    rt.initialize()
    rt.emit_tag("<go>", caller="agent")
    rt.emit_tag("<readied>", caller="agent")
    chans = install_chunks_channels(runtime=rt)
    # writeup→end requires operator; agent caller should be rejected
    ctx = ChannelContext(
        engagement_id="eng-1", activation_id="a-1",
        caller="agent", audit_sink=sd / "events.jsonl",
    )
    with pytest.raises(ChannelRejection, match="caller_mismatch"):
        chans["chunks.emit_tag"].call({"tag": "<done>"}, ctx)
