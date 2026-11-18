"""Source descriptors for evidence binder put() callers.

Each source has a stable name + an expected metadata schema. v1 ships
four canonical sources; new ones added by registering a Source descriptor.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceDescriptor:
    name: str
    description: str
    required_metadata: tuple[str, ...] = field(default_factory=tuple)
    optional_metadata: tuple[str, ...] = field(default_factory=tuple)


_SOURCES: dict[str, SourceDescriptor] = {}


def register(d: SourceDescriptor) -> None:
    _SOURCES[d.name] = d


def get(name: str) -> SourceDescriptor | None:
    return _SOURCES.get(name)


def all_sources() -> list[SourceDescriptor]:
    return sorted(_SOURCES.values(), key=lambda s: s.name)


def validate_metadata(source: str, metadata: dict) -> list[str]:
    """Return list of error strings; empty if valid (or unknown source)."""
    desc = _SOURCES.get(source)
    if desc is None:
        return []  # unknown source — permissive
    errors = [
        f"missing required metadata field: {f}"
        for f in desc.required_metadata if f not in metadata
    ]
    allowed = set(desc.required_metadata) | set(desc.optional_metadata)
    if allowed:
        for k in metadata:
            if k not in allowed:
                errors.append(f"unexpected metadata field: {k}")
    return errors


# v1 canonical sources

register(SourceDescriptor(
    name="mitm:request",
    description="HTTP(S) request/response captured by ARA MITM proxy.",
    required_metadata=("url", "method"),
    optional_metadata=("status", "headers", "direction"),
))

register(SourceDescriptor(
    name="shim:read",
    description="Bytes returned from a libc-shim-wrapped read() syscall.",
    required_metadata=("path",),
    optional_metadata=("offset", "fd"),
))

register(SourceDescriptor(
    name="shim:spawn",
    description="Process spawn observed by the libc shim.",
    required_metadata=("command",),
    optional_metadata=("args", "pid", "url", "source"),
))

register(SourceDescriptor(
    name="shim:spawn_output",
    description="Captured stdout/stderr from a target-url subprocess.",
    required_metadata=("command",),
    optional_metadata=(
        "args", "pid", "url", "source", "exit_code", "signal",
        "stdout_truncated", "stderr_truncated",
    ),
))

register(SourceDescriptor(
    name="agent:stream",
    description="Captured stdout/stderr stream from agent execution.",
    required_metadata=("stream",),
    optional_metadata=("pid",),
))

register(SourceDescriptor(
    name="authorization:trim",
    description="The active absolute/ROE (rhema) rule in effect when an entry was filed.",
    required_metadata=(),
    optional_metadata=("rule_id", "channel_id", "engagement_id"),
))

register(SourceDescriptor(
    name="net:connection",
    description="Outbound connect() decision from the seccomp/shim egress enforcer.",
    required_metadata=("host",),
    optional_metadata=(
        "port", "verdict", "reason", "enforcer", "family", "op", "pid",
    ),
))


# Sources whose content IS an observed result (response / output / bytes) — a
# valid PRIMARY evidence for a fact/lead/finding. ``shim:spawn`` (command only)
# and ``net:connection`` (connection metadata) are provenance, not a primary.
_RESPONSE_BEARING_EXACT = frozenset({"mitm:request", "shim:spawn_output", "shim:read"})


def is_response_bearing(source: str) -> bool:
    """True iff evidence of this source substantiates a claim (holds the actual
    output/response/bytes), vs. command/connection metadata."""
    s = source or ""
    return s.startswith("tool:") or s in _RESPONSE_BEARING_EXACT


def register_tool_source(tool_name: str) -> SourceDescriptor:
    """Convenience for ``tool:<name>`` sources — registered lazily on first use."""
    full = f"tool:{tool_name}"
    existing = _SOURCES.get(full)
    if existing is not None:
        return existing
    d = SourceDescriptor(
        name=full,
        description=f"Output captured from agent tool {tool_name!r}.",
        required_metadata=(),
        optional_metadata=("args", "exit_code", "duration_ms"),
    )
    register(d)
    return d
