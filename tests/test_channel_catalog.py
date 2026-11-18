"""Tests for forge.catalog (4.1)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from ara.forge.catalog.doc import render_catalog_md
from ara.forge.catalog.generator import (
    ChannelEntry,
    detect_isolated,
    generate_catalog,
    write_catalog_json,
)
from ara.runtime import audit as raudit
from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel, ChannelContext


_CLASS_SNAPSHOT: dict = {}


def setup_function(_fn) -> None:
    global _CLASS_SNAPSHOT
    registry.unregister_for_tests()
    # The class registry is process-wide; snapshot before clearing so
    # production channels are restored after the test runs.
    _CLASS_SNAPSHOT = registry.snapshot_classes_for_tests()
    registry.unregister_classes_for_tests()
    raudit.reset_registry_for_tests()


def teardown_function(_fn) -> None:
    registry.restore_classes_for_tests(_CLASS_SNAPSHOT)


@dataclass
class _BodyA:
    x: int


@dataclass
class _BodyB:
    y: str = "default"


class _ChannelA(Channel):
    name = "test.a"
    schema = _BodyA
    sink_subsystem = "facts.jsonl"

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        # Static reference to test.b for cross-wiring detection
        target = "test.b"
        return {"target": target}


class _ChannelB(Channel):
    name = "test.b"
    schema = _BodyB
    sink_subsystem = "leads.jsonl"

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        return {}


class _IsolatedChannel(Channel):
    name = "test.isolated"
    schema = _BodyB
    sink_subsystem = "lonely.jsonl"

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        return {}


class _AuditOnlyChannel(Channel):
    name = "test.audit_only"
    schema = _BodyB
    sink_subsystem = "audit_only"

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        return {}


def test_generate_catalog_includes_registered_channels() -> None:
    registry.register_class(_ChannelA)
    registry.register_class(_ChannelB)
    cat = generate_catalog(weapon_id="w1", seal_id="s1")
    names = [c.name for c in cat.channels]
    assert "test.a" in names and "test.b" in names


def test_introspects_dataclass_fields() -> None:
    registry.register_class(_ChannelA)
    cat = generate_catalog("w", "s")
    a = next(c for c in cat.channels if c.name == "test.a")
    assert a.schema_name == "_BodyA"
    assert any(f.name == "x" and f.required for f in a.schema_fields)


def test_optional_fields_not_required() -> None:
    registry.register_class(_ChannelB)
    cat = generate_catalog("w", "s")
    b = next(c for c in cat.channels if c.name == "test.b")
    assert any(f.name == "y" and not f.required for f in b.schema_fields)


def test_cross_wiring_edge_detected() -> None:
    registry.register_class(_ChannelA)
    registry.register_class(_ChannelB)
    cat = generate_catalog("w", "s")
    edge_pairs = {(e.from_channel, e.to_channel) for e in cat.edges}
    assert ("test.a", "test.b") in edge_pairs
    a_entry = next(c for c in cat.channels if c.name == "test.a")
    b_entry = next(c for c in cat.channels if c.name == "test.b")
    assert "test.b" in a_entry.sinks
    assert "test.a" in b_entry.sources


def test_isolated_detection() -> None:
    registry.register_class(_ChannelA)
    registry.register_class(_ChannelB)
    registry.register_class(_IsolatedChannel)
    cat = generate_catalog("w", "s")
    iso = detect_isolated(cat)
    assert iso == ["test.isolated"]


def test_audit_only_subsystem_excluded() -> None:
    registry.register_class(_AuditOnlyChannel)  # otherwise isolated
    cat = generate_catalog("w", "s")
    iso = detect_isolated(cat)
    assert iso == []


def test_write_catalog_json_round_trip(tmp_path: Path) -> None:
    registry.register_class(_ChannelA)
    registry.register_class(_ChannelB)
    cat = generate_catalog("w1", "s1")
    out = tmp_path / "channel_catalog.json"
    write_catalog_json(out, cat)
    import json
    data = json.loads(out.read_text())
    assert data["weapon_id"] == "w1"
    assert len(data["channels"]) == 2
    assert any(e["from"] == "test.a" for e in data["edges"])


def test_render_catalog_md_contains_channel_names() -> None:
    registry.register_class(_ChannelA)
    registry.register_class(_ChannelB)
    cat = generate_catalog("w", "s")
    md = render_catalog_md(cat)
    assert "`test.a`" in md
    assert "`test.b`" in md
    assert "Cross-wiring graph" in md


def test_real_facts_channels_have_known_sinks(tmp_path: Path) -> None:
    """When facts channels are installed, their cross-wiring is empty
    (they don't statically reference other channels) but they aren't
    isolated because they're called BY other channels (lead.file, etc.)."""
    from ara.runtime.facts.channels import install_fact_channels
    from ara.runtime.leads.channels import install_lead_channels
    install_fact_channels(sessions_root=tmp_path)
    install_lead_channels(sessions_root=tmp_path)
    cat = generate_catalog("w", "s")
    names = {c.name for c in cat.channels}
    assert "fact.assert" in names
    assert "lead.file" in names
