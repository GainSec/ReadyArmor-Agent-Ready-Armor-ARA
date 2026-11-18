"""Tests for runtime.bootstrap."""
from __future__ import annotations

from pathlib import Path

import pytest

from ara.runtime import audit as raudit
from ara.runtime.activation import ActivationManager
from ara.runtime.binder.api import EvidenceBinder
from ara.runtime.bootstrap import (
    engagement_context_factory,
    install_engagement_channels,
)
from ara.runtime.channels import registry


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    registry.unregister_for_tests()
    raudit.reset_registry_for_tests()
    yield
    registry.unregister_for_tests()
    raudit.reset_registry_for_tests()


def test_installs_full_subsystem_set(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    weapon_dir = tmp_path / "weapon"
    weapon_dir.mkdir()

    chans = install_engagement_channels(
        sessions_root=sessions_root, weapon_dir=weapon_dir,
    )

    expected = {
        "fact.assert", "facts.query", "facts.history", "facts.neighbors",
        "lead.file", "lead.investigate", "lead.touch", "lead.dismiss",
        "lead.promote", "lead.confirm", "lead.priority_override",
        "evidence.search", "evidence.get",
        "finding.file", "finding.sign_off", "finding.rescind",
        "finding.enrich", "finding.enrichment_review",
        "anomaly.emit", "anomaly.promote", "anomaly.dismiss",
    }
    assert expected.issubset(set(chans))
    # Each one is also globally registered
    for name in expected:
        assert registry.get(name) is chans[name]


def test_install_style_opt_in(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    chans = install_engagement_channels(
        sessions_root=sessions_root, install_style=True,
    )
    assert "style.score" in chans
    # Re-install of the non-style subset doesn't crash on duplicate-distinct
    # (idempotent installers), and style.score remains globally registered.
    install_engagement_channels(sessions_root=sessions_root)
    assert registry.has("style.score")


def test_engagement_context_factory_pulls_current_activation(tmp_path: Path) -> None:
    eng_dir = tmp_path / "sessions" / "eng-1"
    eng_dir.mkdir(parents=True)
    am = ActivationManager(eng_dir)
    initial = am.current()

    make_ctx = engagement_context_factory(
        engagement_id="eng-1",
        activation_manager=am,
        audit_sink=eng_dir / "events.jsonl",
    )
    ctx_a = make_ctx()
    assert ctx_a.engagement_id == "eng-1"
    assert ctx_a.activation_id == initial
    assert ctx_a.caller == "agent"

    ctx_o = make_ctx("operator")
    assert ctx_o.caller == "operator"
    assert ctx_o.activation_id == initial

    # After a chunk swap, the next ctx picks up the new id
    am.mint(reason="chunk_swap", chunk_id="writeup")
    ctx_b = make_ctx()
    assert ctx_b.activation_id != initial


def test_install_engagement_wires_chunk_runtime(tmp_path: Path) -> None:
    from ara.forge.chunks.chunkdef import ChunkDef, ExitTarget, write_chunkdef
    from ara.runtime.bootstrap import install_engagement

    weapon_dir = tmp_path / "weapon"
    cdir = weapon_dir / "chunks"
    write_chunkdef(cdir / "enum.json", ChunkDef(
        id="enum", title="x", prompt_fragment="x",
        tool_surface=["fact.assert"], is_entry=True,
        exits={"<go>": ExitTarget(tag="<go>", next_chunk_id="probe",
                                    requires_caller="agent")},
    ))
    write_chunkdef(cdir / "probe.json", ChunkDef(
        id="probe", title="x", prompt_fragment="x",
        tool_surface=["lead.file"], is_terminal=True,
    ))
    sessions_root = tmp_path / "sessions"
    wiring = install_engagement(
        sessions_root=sessions_root, engagement_id="eng-1",
        weapon_dir=weapon_dir,
    )
    assert wiring.chunk_runtime is not None
    assert wiring.chunk_runtime.current_chunk_id() == "enum"
    # chunks.emit_tag channel installed
    assert "chunks.emit_tag" in wiring.channels
    # Tool-surface gating reflects active chunk
    assert wiring.chunk_runtime.is_tool_allowed("fact.assert")
    assert not wiring.chunk_runtime.is_tool_allowed("lead.file")


def test_install_engagement_skips_chunk_runtime_without_weapon(tmp_path: Path) -> None:
    from ara.runtime.bootstrap import install_engagement
    sessions_root = tmp_path / "sessions"
    wiring = install_engagement(
        sessions_root=sessions_root, engagement_id="eng-1",
    )
    assert wiring.chunk_runtime is None
    assert "chunks.emit_tag" not in wiring.channels


def test_end_to_end_via_bootstrap(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    eng_dir = sessions_root / "eng-1"
    eng_dir.mkdir(parents=True)

    # One bootstrap call replaces what used to be 4 separate installs
    chans = install_engagement_channels(
        sessions_root=sessions_root,
    )
    am = ActivationManager(eng_dir)
    make_ctx = engagement_context_factory(
        engagement_id="eng-1",
        activation_manager=am,
        audit_sink=eng_dir / "events.jsonl",
    )

    binder = EvidenceBinder(eng_dir)
    rec = binder.put(b"x", source="tool:curl", source_metadata={}, summary="s",
                     activation_id=am.current())
    evidence_search = chans["evidence.search"].call({
        "query": "s",
    }, make_ctx())
    assert evidence_search["results"][0]["record"]["id"] == rec.id
    evidence_get = chans["evidence.get"].call({
        "evidence_id": rec.id,
    }, make_ctx())
    assert evidence_get["record"]["id"] == rec.id
    assert evidence_get["excerpt"] == "x"

    out = chans["fact.assert"].call({
        "predicate": "target.host", "value": "example.com",
        "asserted_by_crumb": "c1", "evidence_refs": [rec.id],
    }, make_ctx())
    assert out["fact"]["predicate"] == "target.host"

    lead_out = chans["lead.file"].call({
        "title": "t", "reasoning": "r", "evidence_refs": [rec.id],
        "fact_refs": [out["fact"]["id"]],
    }, make_ctx())
    assert lead_out["lead"]["status"] == "open"
