"""Channel catalog generator.

Introspects ``runtime.channels.registry`` at weapon seal time to
produce a static, auditable catalog. The 4.1 cross-wiring principle:
no isolated subsystems — every registered channel must either be
``audit_only`` or have at least one inbound or outbound edge.
"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel  # noqa: F401  — kept for type hints


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    required: bool
    description: str | None = None


@dataclass(frozen=True)
class ChannelEntry:
    name: str
    sink_subsystem: str
    schema_name: str
    schema_fields: list[FieldSpec]
    callers: list[str]
    audit_logged: bool
    sources: list[str] = field(default_factory=list)
    sinks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChannelEdge:
    from_channel: str
    to_channel: str
    trigger_description: str


@dataclass(frozen=True)
class WeaponChannelCatalog:
    weapon_id: str
    seal_id: str
    generated_at: float
    channels: list[ChannelEntry]
    edges: list[ChannelEdge]


# ---------- introspection helpers ----------

def _introspect_dataclass(schema: type) -> list[FieldSpec]:
    if not dataclasses.is_dataclass(schema):
        return []
    out: list[FieldSpec] = []
    for f in dataclasses.fields(schema):
        required = (
            f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING  # type: ignore[misc]
        )
        ftype = getattr(f.type, "__name__", str(f.type))
        out.append(FieldSpec(name=f.name, type=ftype, required=required))
    return out


_KNOWN_CHANNEL_RE = re.compile(r'["\']([a-z_]+\.[a-z_.]+)["\']')


def _find_cross_channel_calls(channels: list[Channel]) -> list[ChannelEdge]:
    """v1 implementation: scan each channel module's source for known
    channel-name string literals. Forms the cross-wiring graph.
    """
    import inspect
    known_names = {c.name for c in channels}
    edges: list[ChannelEdge] = []
    seen: set[tuple[str, str]] = set()
    for ch in channels:
        try:
            src = inspect.getsource(type(ch))
        except (OSError, TypeError):
            continue
        for m in _KNOWN_CHANNEL_RE.finditer(src):
            target = m.group(1)
            if target == ch.name or target not in known_names:
                continue
            key = (ch.name, target)
            if key in seen:
                continue
            seen.add(key)
            edges.append(ChannelEdge(
                from_channel=ch.name, to_channel=target,
                trigger_description=f"{ch.name} class body references {target}",
            ))
    return edges


def generate_catalog(weapon_id: str, seal_id: str) -> WeaponChannelCatalog:
    """Build the catalog from the *class* registry.

    All catalog-relevant fields (name, schema, sink_subsystem,
    allowed_callers, schema fields, AST-grep edges) are class-level.
    Using class registry means the catalog is complete at seal time
    without needing engagement-bound channel instances.

    Falls back to the instance registry for any class-less channels —
    keeps backward compatibility with tests / callers that register an
    instance directly.
    """
    classes = registry.all_classes()
    instance_only = [
        type(ch) for ch in registry.all_channels()
        if not registry.has_class(ch.name)
    ]
    channel_classes: list[type[Channel]] = list(classes) + instance_only

    sources_map: dict[str, list[str]] = {c.name: [] for c in channel_classes}
    sinks_map: dict[str, list[str]] = {c.name: [] for c in channel_classes}
    edges = _find_cross_channel_calls_for_classes(channel_classes)
    for edge in edges:
        sources_map.setdefault(edge.to_channel, []).append(edge.from_channel)
        sinks_map.setdefault(edge.from_channel, []).append(edge.to_channel)

    entries: list[ChannelEntry] = []
    seen: set[str] = set()
    for cls in channel_classes:
        if cls.name in seen:
            continue
        seen.add(cls.name)
        entries.append(ChannelEntry(
            name=cls.name,
            sink_subsystem=cls.sink_subsystem,
            schema_name=getattr(cls.schema, "__name__", str(cls.schema)),
            schema_fields=_introspect_dataclass(cls.schema),
            callers=list(cls.allowed_callers),
            audit_logged=True,
            sources=sources_map.get(cls.name, []),
            sinks=sinks_map.get(cls.name, []),
        ))
    entries.sort(key=lambda e: e.name)
    return WeaponChannelCatalog(
        weapon_id=weapon_id, seal_id=seal_id,
        generated_at=time.time(),
        channels=entries, edges=edges,
    )


def _find_cross_channel_calls_for_classes(
    classes: list[type[Channel]],
) -> list[ChannelEdge]:
    """Class-aware variant of edge detection — same logic, different
    iterable. Inspects each class's source for known channel-name string
    literals.
    """
    import inspect
    known_names = {c.name for c in classes}
    edges: list[ChannelEdge] = []
    seen: set[tuple[str, str]] = set()
    for cls in classes:
        try:
            src = inspect.getsource(cls)
        except (OSError, TypeError):
            continue
        for m in _KNOWN_CHANNEL_RE.finditer(src):
            target = m.group(1)
            if target == cls.name or target not in known_names:
                continue
            key = (cls.name, target)
            if key in seen:
                continue
            seen.add(key)
            edges.append(ChannelEdge(
                from_channel=cls.name, to_channel=target,
                trigger_description=f"{cls.name} class body references {target}",
            ))
    return edges


def detect_isolated(catalog: WeaponChannelCatalog,
                    *, audit_only_subsystems: set[str] | None = None) -> list[str]:
    """Names of channels with neither inbound nor outbound edges,
    excluding ``audit_only`` subsystems.
    """
    audit_only = audit_only_subsystems or {"audit_only"}
    isolated: list[str] = []
    for ch in catalog.channels:
        if ch.sink_subsystem in audit_only:
            continue
        if not ch.sources and not ch.sinks:
            isolated.append(ch.name)
    return isolated


def write_catalog_json(path: Path, catalog: WeaponChannelCatalog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "weapon_id": catalog.weapon_id,
        "seal_id": catalog.seal_id,
        "generated_at": catalog.generated_at,
        "channels": [
            {
                "name": ch.name, "sink_subsystem": ch.sink_subsystem,
                "schema_name": ch.schema_name,
                "schema_fields": [
                    {"name": f.name, "type": f.type, "required": f.required,
                     "description": f.description}
                    for f in ch.schema_fields
                ],
                "callers": ch.callers, "audit_logged": ch.audit_logged,
                "sources": ch.sources, "sinks": ch.sinks,
            }
            for ch in catalog.channels
        ],
        "edges": [
            {"from": e.from_channel, "to": e.to_channel,
             "trigger_description": e.trigger_description}
            for e in catalog.edges
        ],
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2))
